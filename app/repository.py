"""数据访问与核心业务规则（全部在数据库事务中执行）。"""
import hashlib
import heapq
import json
from datetime import date, datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from .errors import ApiError

# v_certificates 视图的列顺序
_VIEW_COLS = (
    "cert_no, kind, device_id, device_name, calibrated_at, valid_from, valid_to, "
    "conclusion, parent_cert_no, operator, remark, status, revoked_reason, "
    "revoked_at, created_at"
)


# ---------------------------------------------------------------------------
# 公共查询
# ---------------------------------------------------------------------------

def _get_cert_row(conn, cert_no: str, lock: bool = False) -> dict | None:
    sql = f"SELECT {_VIEW_COLS} FROM v_certificates WHERE cert_no = %s"
    if lock:
        # 视图基于单表，FOR UPDATE 会落到底层 certificates 行上
        sql += " FOR UPDATE"
    return conn.execute(sql, (cert_no,)).fetchone()


def get_certificate(conn, cert_no: str) -> dict:
    row = _get_cert_row(conn, cert_no)
    if row is None:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
    return row


def get_chain(conn, cert_no: str) -> dict:
    """返回从追溯起点（基准证书）到目标证书的完整引用链。"""
    if _get_cert_row(conn, cert_no) is None:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")

    sql = f"""
        WITH RECURSIVE walk AS (
            SELECT cert_no, parent_cert_no, 0 AS depth
            FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, w.depth - 1
            FROM certificates c JOIN walk w ON c.cert_no = w.parent_cert_no
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT w.depth, vc.cert_no, vc.kind, vc.device_id, vc.device_name,
               vc.calibrated_at, vc.valid_from, vc.valid_to, vc.conclusion,
               vc.parent_cert_no, vc.operator, vc.remark, vc.status,
               vc.revoked_reason, vc.revoked_at, vc.created_at
        FROM walk w JOIN v_certificates vc ON vc.cert_no = w.cert_no
        WHERE NOT w.is_cycle
        ORDER BY w.depth ASC, vc.cert_no ASC
    """
    nodes = [
        {"depth": r.pop("depth"), "certificate": r}
        for r in conn.execute(sql, (cert_no,)).fetchall()
    ]
    if not nodes:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
    return {
        "cert_no": cert_no,
        "nodes": nodes,
        "root_cert_no": nodes[0]["certificate"]["cert_no"],
        "length": len(nodes),
    }


def list_certificates(
    conn,
    device_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    where, params = [], []
    if device_id:
        where.append("device_id = %s")
        params.append(device_id)
    if status:
        where.append("status = %s")
        params.append(status)
    if kind:
        where.append("kind = %s")
        params.append(kind)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT count(*) AS n FROM certificates {clause}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT {_VIEW_COLS} FROM v_certificates {clause} "
        f"ORDER BY created_at DESC, cert_no ASC LIMIT %s OFFSET %s",
        (*params, limit, offset),
    ).fetchall()
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


def get_device_history(conn, device_id: str) -> list[dict]:
    seen = conn.execute(
        "SELECT 1 FROM certificates WHERE device_id = %s LIMIT 1", (device_id,)
    ).fetchone()
    if seen is None:
        raise ApiError(404, "device_not_found", f"设备无任何证书记录: {device_id}")
    return conn.execute(
        f"SELECT {_VIEW_COLS} FROM v_certificates WHERE device_id = %s "
        f"ORDER BY calibrated_at DESC, created_at DESC",
        (device_id,),
    ).fetchall()


def get_downstream(conn, cert_no: str) -> dict:
    if _get_cert_row(conn, cert_no) is None:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
    sql = """
        WITH RECURSIVE d AS (
            SELECT cert_no, 1 AS depth, status
            FROM certificates WHERE parent_cert_no = %s
            UNION ALL
            SELECT c.cert_no, d.depth + 1, c.status
            FROM certificates c JOIN d ON c.parent_cert_no = d.cert_no
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT cert_no, depth, status::text AS status
        FROM d WHERE NOT is_cycle
        ORDER BY depth ASC, cert_no ASC
    """
    return {"cert_no": cert_no, "descendants": conn.execute(sql, (cert_no,)).fetchall()}


# ---------------------------------------------------------------------------
# 证书签发
# ---------------------------------------------------------------------------

def _validate_parent(conn, parent_no: str, calibrated_at) -> dict:
    """被引用的标准设备证书：必须存在、生效中、合格，且校准时刻落在有效期内。"""
    parent = _get_cert_row(conn, parent_no, lock=True)
    if parent is None:
        raise ApiError(422, "invalid_reference", f"引用的标准证书不存在: {parent_no}")
    if parent["status"] != "ACTIVE":
        raise ApiError(
            422,
            "reference_not_active",
            f"引用的标准证书 {parent_no} 当前状态为 {parent['status']}，"
            "已撤销/失效证书不得作为校准依据",
        )
    if parent["conclusion"] != "PASS":
        raise ApiError(
            422,
            "reference_not_qualified",
            f"引用的标准证书 {parent_no} 结论为不合格，不得作为校准依据",
        )
    covers = conn.execute(
        "SELECT tstzrange(valid_from, valid_to, '[)') @> %s AS ok FROM certificates "
        "WHERE cert_no = %s",
        (calibrated_at, parent_no),
    ).fetchone()
    if not covers or not covers["ok"]:
        raise ApiError(
            422,
            "reference_out_of_validity",
            f"校准时刻 {calibrated_at.isoformat()} 不在标准证书 {parent_no} 的"
            f"有效期 [{parent['valid_from'].isoformat()}, {parent['valid_to'].isoformat()}) 内",
        )
    return parent


def _assert_no_cycle(conn, parent_no: str, new_cert_no: str) -> None:
    """防御性成环检测：若新证书编号已出现在上游链中，则拒绝（正常情况下不会触发）。"""
    sql = """
        WITH RECURSIVE up AS (
            SELECT cert_no, parent_cert_no FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no
            FROM certificates c JOIN up u ON c.cert_no = u.parent_cert_no
        )
        SELECT 1 FROM up WHERE parent_cert_no = %s LIMIT 1
    """
    if conn.execute(sql, (parent_no, new_cert_no)).fetchone():
        raise ApiError(
            422,
            "reference_cycle",
            f"引用关系将形成环路（{new_cert_no} 已在 {parent_no} 的上游链中）",
        )


def _assert_no_overlap(conn, device_id: str, valid_from, valid_to, exclude_no: str | None) -> None:
    row = conn.execute(
        """
        SELECT cert_no FROM certificates
        WHERE device_id = %s AND status = 'ACTIVE' AND cert_no IS DISTINCT FROM %s
          AND tstzrange(valid_from, valid_to, '[)') && tstzrange(%s, %s, '[)')
        LIMIT 1
        """,
        (device_id, exclude_no, valid_from, valid_to),
    ).fetchone()
    if row:
        raise ApiError(
            422,
            "validity_overlap",
            f"设备 {device_id} 已生效证书 {row['cert_no']} 的有效期与本次签发区间重叠"
            "（区间按半开 [valid_from, valid_to) 判定）",
            details={"conflict_cert_no": row["cert_no"]},
        )


def _raw_insert_certificate(conn, *, kind: str, conclusion: str, parent_no: str | None, req) -> None:
    """插入证书行；数据库层异常原样抛出，由调用方按场景转换。"""
    conn.execute(
        """
        INSERT INTO certificates (
            cert_no, kind, device_id, device_name, calibrated_at,
            valid_from, valid_to, conclusion, parent_cert_no, operator, remark
        ) VALUES (
            %s, %s::cert_kind, %s, %s, %s, %s, %s, %s::cert_conclusion, %s, %s, %s
        )
        """,
        (
            req.cert_no, kind, req.device_id, req.device_name, req.calibrated_at,
            req.valid_from, req.valid_to, conclusion, parent_no, req.operator, req.remark,
        ),
    )


def _insert_certificate(conn, *, kind: str, conclusion: str, parent_no: str | None, req) -> dict:
    try:
        _raw_insert_certificate(
            conn, kind=kind, conclusion=conclusion, parent_no=parent_no, req=req
        )
    except psycopg.errors.UniqueViolation:
        raise ApiError(409, "cert_duplicate", f"证书编号已存在: {req.cert_no}")
    except psycopg.errors.ExclusionViolation:
        # 并发事务抢先插入了重叠区间——排他约束兜底
        raise ApiError(
            409,
            "validity_overlap_concurrent",
            f"设备 {req.device_id} 已存在与本次有效期重叠的生效证书（并发签发冲突），"
            f"证书编号 {req.cert_no} 未签发",
        )
    except psycopg.errors.ForeignKeyViolation:
        raise ApiError(422, "invalid_reference", f"引用的标准证书不存在: {parent_no}")
    except psycopg.errors.CheckViolation as exc:
        raise ApiError(422, "rule_violation", f"签发被数据库完整性规则拒绝: {exc}")
    return get_certificate(conn, req.cert_no)


def issue_certificate(conn, req) -> dict:
    """签发普通校准证书。"""
    with conn.transaction():
        _validate_parent(conn, req.parent_cert_no, req.calibrated_at)
        _assert_no_cycle(conn, req.parent_cert_no, req.cert_no)
        _assert_no_overlap(conn, req.device_id, req.valid_from, req.valid_to, req.cert_no)
        cert = _insert_certificate(
            conn, kind="CALIBRATION", conclusion=req.conclusion,
            parent_no=req.parent_cert_no, req=req,
        )
    chain = get_chain(conn, req.cert_no)
    return {"certificate": cert, "chain": chain}


def issue_reference(conn, req) -> dict:
    """登记基准设备合格证书（追溯起点，恒为合格、无上游）。"""
    with conn.transaction():
        _assert_no_overlap(conn, req.device_id, req.valid_from, req.valid_to, req.cert_no)
        cert = _insert_certificate(
            conn, kind="REFERENCE", conclusion="PASS", parent_no=None, req=req,
        )
    chain = get_chain(conn, req.cert_no)
    return {"certificate": cert, "chain": chain}


# ---------------------------------------------------------------------------
# 批次签发（离线采集交接批次：整批原子落库、批次键幂等）
# ---------------------------------------------------------------------------

def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def _json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)


def _canonical_request(req) -> tuple[str, dict]:
    """规范化请求：排序键的紧凑 JSON 及其 SHA-256，用于“同键是否同请求”判定。"""
    doc = req.model_dump(mode="json")
    canonical = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), doc


def _get_issue_batch_row(conn, batch_key: str) -> dict | None:
    return conn.execute(
        "SELECT batch_key, request_hash, response, cert_nos, created_at "
        "FROM issue_batches WHERE batch_key = %s",
        (batch_key,),
    ).fetchone()


def _replay_or_conflict(existing: dict, batch_key: str, req_hash: str) -> dict:
    """批次键已存在：同请求 → 重放首次签发结果快照；不同请求 → 409 冲突。"""
    if existing["request_hash"] != req_hash:
        raise ApiError(
            409,
            "batch_conflict",
            f"批次键 {batch_key} 已用于一份内容不同的签发请求；"
            "同一批次键只允许重放完全相同的请求",
            details={"batch_key": batch_key},
        )
    return existing["response"]


def _trace_cycle(by_no: dict, start: str) -> list[str]:
    """沿批内父引用从 start 向上走，返回成环路径（首尾相接）。"""
    seen: dict[str, int] = {}
    path: list[str] = []
    cur = start
    while cur not in seen:
        seen[cur] = len(path)
        path.append(cur)
        nxt = by_no[cur].parent_cert_no
        if nxt not in by_no:  # 理论不可达（未出队节点必有批内父），兜底
            return [start, start]
        cur = nxt
    return path[seen[cur]:] + [cur]


def _topo_sort_batch(items: list) -> list:
    """批内引用拓扑排序（父先子后，同层按编号排序保证确定性）；成环则拒绝。"""
    by_no = {it.cert_no: it for it in items}
    indeg = {it.cert_no: 0 for it in items}
    children: dict[str, list[str]] = {it.cert_no: [] for it in items}
    for it in items:
        if it.kind == "CALIBRATION" and it.parent_cert_no in by_no:
            indeg[it.cert_no] += 1
            children[it.parent_cert_no].append(it.cert_no)
    ready = [no for no, d in indeg.items() if d == 0]
    heapq.heapify(ready)
    ordered = []
    while ready:
        no = heapq.heappop(ready)
        ordered.append(by_no[no])
        for ch in children[no]:
            indeg[ch] -= 1
            if indeg[ch] == 0:
                heapq.heappush(ready, ch)
    if len(ordered) != len(items):
        start = next(no for no, d in indeg.items() if d > 0)
        cycle = _trace_cycle(by_no, start)
        raise ApiError(
            422,
            "reference_cycle",
            f"批内引用关系成环: {' -> '.join(cycle)}",
            details={"cert_no": cycle[0], "cycle": cycle},
        )
    return ordered


def _validate_batch(conn, req) -> list:
    """整批预检：批内编号唯一、不与库内冲突、引用可解析、批内无环。

    返回拓扑排序后的条目列表；任一不合规抛出 ApiError（整批不落库，
    出错编号置于 details.cert_no）。
    """
    items = list(req.certificates)
    # 1) 批内编号唯一
    seen: set[str] = set()
    for it in items:
        if it.cert_no in seen:
            raise ApiError(
                422,
                "batch_cert_duplicate",
                f"批次内证书编号重复: {it.cert_no}",
                details={"cert_no": it.cert_no},
            )
        seen.add(it.cert_no)
    # 2) 不得与库内已有证书编号冲突
    nos = [it.cert_no for it in items]
    existing = {
        r["cert_no"]
        for r in conn.execute(
            "SELECT cert_no FROM certificates WHERE cert_no = ANY(%s)", (nos,)
        ).fetchall()
    }
    for it in items:
        if it.cert_no in existing:
            raise ApiError(
                409,
                "cert_duplicate",
                f"证书编号已存在: {it.cert_no}（整批未签发）",
                details={"cert_no": it.cert_no},
            )
    # 3) 引用解析：普通证书的上游必须在本批或库内
    by_no = {it.cert_no: it for it in items}
    ext_parents = sorted(
        {
            it.parent_cert_no
            for it in items
            if it.kind == "CALIBRATION" and it.parent_cert_no not in by_no
        }
    )
    found = set()
    if ext_parents:
        found = {
            r["cert_no"]
            for r in conn.execute(
                "SELECT cert_no FROM certificates WHERE cert_no = ANY(%s)",
                (ext_parents,),
            ).fetchall()
        }
    for it in items:
        if (
            it.kind == "CALIBRATION"
            and it.parent_cert_no not in by_no
            and it.parent_cert_no not in found
        ):
            raise ApiError(
                422,
                "invalid_reference",
                f"证书 {it.cert_no} 引用的标准证书不存在: {it.parent_cert_no}",
                details={"cert_no": it.cert_no, "parent_cert_no": it.parent_cert_no},
            )
    # 4) 批内引用无环并拓扑排序（列表顺序不代表引用顺序）
    return _topo_sort_batch(items)


def _insert_batch_items(conn, ordered: list) -> None:
    """按拓扑序逐张校验并插入，复用单张签发的全部规则（上游有效性、区间重叠）。

    批内上游先于下游插入，因此下游对“本批证书”的引用与对库内证书的引用
    走完全相同的校验路径。数据库层唯一/排他冲突不在此转换，交由批次级统一
    处理，以便区分“并发同键重放”与“真实冲突”。
    """
    for it in ordered:
        try:
            if it.kind == "CALIBRATION":
                _validate_parent(conn, it.parent_cert_no, it.calibrated_at)
            _assert_no_overlap(
                conn, it.device_id, it.valid_from, it.valid_to, it.cert_no
            )
        except ApiError as exc:
            exc.details = {**exc.details, "cert_no": it.cert_no}
            raise
        _raw_insert_certificate(
            conn,
            kind=it.kind,
            conclusion="PASS" if it.kind == "REFERENCE" else it.conclusion,
            parent_no=None if it.kind == "REFERENCE" else it.parent_cert_no,
            req=it,
        )


def _build_batch_response(conn, req, issued_at: datetime) -> dict:
    """组装批次结果：批次键 + 各证书及其从基准到自身的追溯链（按请求顺序）。"""
    items = []
    for it in req.certificates:
        items.append(
            {
                "certificate": get_certificate(conn, it.cert_no),
                "chain": get_chain(conn, it.cert_no),
            }
        )
    return {
        "batch_key": req.batch_key,
        "count": len(items),
        "issued_at": issued_at,
        "items": items,
    }


def _batch_cert_duplicate_error(conn, req) -> ApiError:
    """唯一冲突且批次键不存在 → 与并发事务的证书编号冲突，定位冲突编号。"""
    nos = [it.cert_no for it in req.certificates]
    existing = {
        r["cert_no"]
        for r in conn.execute(
            "SELECT cert_no FROM certificates WHERE cert_no = ANY(%s)", (nos,)
        ).fetchall()
    }
    for it in req.certificates:
        if it.cert_no in existing:
            return ApiError(
                409,
                "cert_duplicate",
                f"证书编号已存在: {it.cert_no}（与并发签发冲突，整批未签发）",
                details={"cert_no": it.cert_no},
            )
    return ApiError(
        409, "cert_duplicate", f"批次 {req.batch_key} 与并发签发冲突，整批未签发"
    )


def _batch_overlap_error(conn, req) -> ApiError:
    """排他约束兜底：与并发事务（单张签发或另一批次）在同设备有效期上重叠。"""
    for it in req.certificates:
        row = conn.execute(
            """
            SELECT cert_no FROM certificates
            WHERE device_id = %s AND status = 'ACTIVE'
              AND tstzrange(valid_from, valid_to, '[)') && tstzrange(%s, %s, '[)')
            LIMIT 1
            """,
            (it.device_id, it.valid_from, it.valid_to),
        ).fetchone()
        if row:
            return ApiError(
                409,
                "validity_overlap_concurrent",
                f"设备 {it.device_id} 已生效证书 {row['cert_no']} 与证书 {it.cert_no} "
                "的有效期重叠（并发签发冲突，整批未签发）",
                details={"cert_no": it.cert_no, "conflict_cert_no": row["cert_no"]},
            )
    return ApiError(
        409,
        "validity_overlap_concurrent",
        f"批次 {req.batch_key} 与并发签发在有效期上重叠，整批未签发",
    )


def issue_batch(conn, req) -> tuple[dict, bool]:
    """批量签发交接批次，返回 (响应体, 是否首次签发)。

    - 整批原子：任一记录不合规 → 全部回滚，响应以 details.cert_no 定位出错编号及原因；
    - 批次键幂等：同键同请求 → 重放首次签发结果快照（即使证书事后被撤销）；
      同键不同请求 → 409 batch_conflict；
    - 并发同键提交由 issue_batches 主键兜底：后到事务回滚后按已提交结果重放/判冲突；
    - 与单张签发、撤销并发时，沿用同一套行锁、唯一约束与排他约束，规则一致。
    """
    req_hash, request_doc = _canonical_request(req)
    try:
        with conn.transaction():
            existing = _get_issue_batch_row(conn, req.batch_key)
            if existing is not None:
                return _replay_or_conflict(existing, req.batch_key, req_hash), False
            ordered = _validate_batch(conn, req)
            _insert_batch_items(conn, ordered)
            issued_at = datetime.now(timezone.utc)
            response = _build_batch_response(conn, req, issued_at)
            conn.execute(
                """
                INSERT INTO issue_batches
                    (batch_key, request_hash, request, response, cert_nos, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    req.batch_key,
                    req_hash,
                    Jsonb(request_doc),
                    Jsonb(response, dumps=_json_dumps),
                    [it.cert_no for it in req.certificates],
                    issued_at,
                ),
            )
            return response, True
    except psycopg.errors.UniqueViolation:
        # 事务已整体回滚：并发同键提交，或与并发事务的证书编号冲突
        existing = _get_issue_batch_row(conn, req.batch_key)
        if existing is not None:
            return _replay_or_conflict(existing, req.batch_key, req_hash), False
        raise _batch_cert_duplicate_error(conn, req)
    except psycopg.errors.ExclusionViolation:
        raise _batch_overlap_error(conn, req)
    except psycopg.errors.ForeignKeyViolation:
        raise ApiError(
            422, "invalid_reference", "引用的标准证书不存在（并发变更），整批未签发"
        )
    except psycopg.errors.CheckViolation as exc:
        raise ApiError(422, "rule_violation", f"签发被数据库完整性规则拒绝: {exc}")


def get_issue_batch(conn, batch_key: str) -> dict:
    """按批次键查询首次签发结果（与签发响应同构的快照）。"""
    row = _get_issue_batch_row(conn, batch_key)
    if row is None:
        raise ApiError(404, "batch_not_found", f"签发批次不存在: {batch_key}")
    return row["response"]


# ---------------------------------------------------------------------------
# 更正声明（展示字段纠错：设备名称 / 操作员 / 备注）
# ---------------------------------------------------------------------------

# 仅允许更正的展示字段及其长度上限（与签发模型一致）
CORRECTABLE_FIELDS = ("device_name", "operator", "remark")
_CORRECTION_MAX_LEN = {"device_name": 256, "operator": 128, "remark": 1024}

_DECL_COLS = "declaration_no, cert_no, seq, corrections, reason, created_at"


def _validate_correction_request(req) -> dict:
    """校验修正字段集合与更正原因；返回提交原样的 corrections（区分显式置空与未提交）。"""
    corrections = dict(req.corrections)
    if not corrections:
        raise ApiError(
            422, "correction_empty",
            "修正字段集合不能为空：至少提交一个待更正的展示字段",
        )
    unknown = sorted(set(corrections) - set(CORRECTABLE_FIELDS))
    if unknown:
        raise ApiError(
            422, "correction_field_not_allowed",
            f"仅允许更正展示字段 {', '.join(CORRECTABLE_FIELDS)}；"
            f"收到不允许更正的字段: {', '.join(unknown)}",
            details={"fields": unknown},
        )
    for field, value in corrections.items():
        if value is not None and len(value) > _CORRECTION_MAX_LEN[field]:
            raise ApiError(
                422, "correction_field_invalid",
                f"字段 {field} 长度超出上限 {_CORRECTION_MAX_LEN[field]} 字符",
                details={"field": field, "max_length": _CORRECTION_MAX_LEN[field]},
            )
    if not req.reason or not req.reason.strip():
        raise ApiError(422, "correction_reason_required", "更正原因不能为空")
    return corrections


def _correction_request_hash(cert_no: str, corrections: dict, reason: str) -> str:
    """规范化请求指纹：键序无关；显式置空（null）与未提交该字段是不同的请求。"""
    canonical = json.dumps(
        {"cert_no": cert_no, "corrections": corrections, "reason": reason},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_declaration_row(conn, declaration_no: str, with_hash: bool = False) -> dict | None:
    cols = _DECL_COLS + (", request_hash" if with_hash else "")
    return conn.execute(
        f"SELECT {cols} FROM correction_declarations WHERE declaration_no = %s",
        (declaration_no,),
    ).fetchone()


def _merged_display(certificate: dict, declarations: list[dict]) -> dict:
    """按登记顺序（seq 升序）将声明合并到原证书的展示字段上。

    同一字段后登记的声明覆盖先登记的；值为 None 表示显式置空，
    未提交的字段不参与合并。
    """
    display = {field: certificate[field] for field in CORRECTABLE_FIELDS}
    for decl in declarations:
        display.update(decl["corrections"])
    return display


def _correction_view(conn, cert_no: str) -> dict:
    """证书的更正视图：原证书 + 按登记顺序排列的声明历史 + 合并后的当前展示。"""
    certificate = get_certificate(conn, cert_no)
    declarations = conn.execute(
        f"SELECT {_DECL_COLS} FROM correction_declarations "
        "WHERE cert_no = %s ORDER BY seq ASC",
        (cert_no,),
    ).fetchall()
    return {
        "certificate": certificate,
        "declarations": declarations,
        "display": _merged_display(certificate, declarations),
    }


def _correction_response(conn, existing_row: dict, req_hash: str) -> dict:
    """声明编号已登记：同内容 → 返回首次声明及当前更正视图；异内容 → 409。"""
    row = dict(existing_row)
    row.pop("request_hash")
    if existing_row["request_hash"] != req_hash:
        raise ApiError(
            409, "correction_conflict",
            f"声明编号 {row['declaration_no']} 已用于一份内容不同的更正声明；"
            "同一声明编号只允许重放完全相同的请求",
            details={"declaration_no": row["declaration_no"]},
        )
    return {"declaration": row, **_correction_view(conn, row["cert_no"])}


def register_correction(conn, req) -> tuple[dict, bool]:
    """登记更正声明，返回 (响应体, 是否首次登记)。

    - 仅 device_name / operator / remark 可更正；修正集合非空、原因非空；
    - 已撤销（REVOKED）/已失效（INVALID）证书同样可更正展示资料，
      状态、有效期与签发/撤销判定不受影响（不触碰 certificates 表）；
    - 声明编号幂等：同编号同内容重试返回首次声明（不重复登记），
      同编号不同内容返回 409 correction_conflict；
    - 并发安全：登记前对被更正证书行加 FOR UPDATE 锁，同一证书的并发更正
      在此串行化，seq 按成功登记顺序分配，不丢失、不乱序；不同证书的相同
      声明编号并发提交由主键兜底，后到事务回滚后按已提交内容重放/判冲突。
    """
    corrections = _validate_correction_request(req)
    req_hash = _correction_request_hash(req.cert_no, corrections, req.reason)
    try:
        with conn.transaction():
            locked = conn.execute(
                "SELECT cert_no FROM certificates WHERE cert_no = %s FOR UPDATE",
                (req.cert_no,),
            ).fetchone()
            if locked is None:
                raise ApiError(404, "cert_not_found", f"证书不存在: {req.cert_no}")

            existing = _get_declaration_row(conn, req.declaration_no, with_hash=True)
            if existing is not None:
                return _correction_response(conn, existing, req_hash), False

            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
                "FROM correction_declarations WHERE cert_no = %s",
                (req.cert_no,),
            ).fetchone()["next_seq"]
            conn.execute(
                """
                INSERT INTO correction_declarations
                    (declaration_no, cert_no, seq, corrections, reason, request_hash)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    req.declaration_no, req.cert_no, seq,
                    Jsonb(corrections), req.reason, req_hash,
                ),
            )
            declaration = _get_declaration_row(conn, req.declaration_no)
    except psycopg.errors.UniqueViolation:
        # 并发同声明编号（多见于不同证书并行提交）：主键兜底，按已提交内容重放或判冲突
        existing = _get_declaration_row(conn, req.declaration_no, with_hash=True)
        if existing is not None:
            return _correction_response(conn, existing, req_hash), False
        raise
    return {"declaration": declaration, **_correction_view(conn, req.cert_no)}, True


def get_certificate_corrections(conn, cert_no: str) -> dict:
    """按证书查询更正视图：原证书、声明历史（按登记顺序）与合并后的当前展示。"""
    return {"cert_no": cert_no, **_correction_view(conn, cert_no)}


def get_correction_declaration(conn, declaration_no: str) -> dict:
    """按声明编号查询：首次登记的声明 + 所属证书的当前更正视图。"""
    row = _get_declaration_row(conn, declaration_no)
    if row is None:
        raise ApiError(404, "declaration_not_found", f"更正声明不存在: {declaration_no}")
    return {"declaration": row, **_correction_view(conn, row["cert_no"])}


# ---------------------------------------------------------------------------
# 撤销与级联失效
# ---------------------------------------------------------------------------

def _gather_active_descendants(conn, root_no: str) -> list[dict]:
    """找出所有状态仍为 ACTIVE 的下游证书。

    path 为“相对被撤销证书”的路径（不含 root 本身），
    完整追溯链在写入快照时与 root 自身的上游链拼接得到。
    """
    sql = """
        WITH RECURSIVE d AS (
            SELECT cert_no, 1 AS depth, ARRAY[cert_no] AS path
            FROM certificates
            WHERE parent_cert_no = %s AND status = 'ACTIVE'
            UNION ALL
            SELECT c.cert_no, d.depth + 1, d.path || c.cert_no
            FROM certificates c JOIN d ON c.parent_cert_no = d.cert_no
            WHERE c.status = 'ACTIVE'
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT cert_no, depth, path FROM d
        WHERE NOT is_cycle
    """
    return conn.execute(sql, (root_no,)).fetchall()


def _root_prefix_chain(conn, root_no: str) -> list[str]:
    """被撤销证书自身的链：从追溯起点（基准）到该证书，含两端。"""
    rows = conn.execute(
        """
        WITH RECURSIVE up AS (
            SELECT cert_no, parent_cert_no, 0 AS depth
            FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, up.depth - 1
            FROM certificates c JOIN up ON c.cert_no = up.parent_cert_no
        )
        SELECT cert_no FROM up ORDER BY depth ASC
        """,
        (root_no,),
    ).fetchall()
    return [r["cert_no"] for r in rows]


def revoke_certificate(conn, cert_no: str, reason: str, operator: str | None) -> dict:
    """主动撤销一张生效证书，并级联失效其全部下游证书。

    并发正确性：
      1. 先对被撤销证书加 FOR UPDATE 行锁；新插入的下游证书在外键检查时需获取
         该行的 FOR KEY SHARE 锁，会被阻塞到本事务提交之后；
      2. 递归收集下游并按 cert_no 排序加行锁后再次收集，直到集合不再扩大，
         覆盖“撤销进行中恰有下游证书提交”的竞态；
      3. 所有 UPDATE/INSERT 在同一事务中提交。
    """
    with conn.transaction():
        root = _get_cert_row(conn, cert_no, lock=True)
        if root is None:
            raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
        if root["status"] != "ACTIVE":
            raise ApiError(
                409,
                "cert_not_active",
                f"证书 {cert_no} 当前状态为 {root['status']}，"
                "仅生效中的证书可被撤销（已失效证书不能重复撤销）",
            )

        locked: set[str] = set()
        descendants: list[dict] = []
        # 循环直到“收集结果”与“已锁定集合”一致，保证不会漏掉新提交的下游
        while True:
            rows = _gather_active_descendants(conn, cert_no)
            fresh = [r for r in rows if r["cert_no"] not in locked]
            if not fresh:
                descendants = rows
                break
            nos = sorted(r["cert_no"] for r in fresh)
            conn.execute(
                "SELECT cert_no FROM certificates "
                "WHERE cert_no = ANY(%s) ORDER BY cert_no FOR UPDATE",
                (nos,),
            )
            locked.update(nos)

        batch = conn.execute(
            "INSERT INTO revocations (cert_no, reason, operator) "
            "VALUES (%s, %s, %s) RETURNING batch_id, revoked_at",
            (cert_no, reason, operator),
        ).fetchone()

        # 被撤销证书本身
        conn.execute(
            """
            UPDATE certificates
            SET status = 'REVOKED', revoked_reason = %s, revoked_at = %s
            WHERE cert_no = %s
            """,
            (reason, batch["revoked_at"], cert_no),
        )

        if descendants:
            # 完整引用链快照：追溯起点 -> 被撤销证书 -> 下游证书
            prefix = _root_prefix_chain(conn, cert_no)

            # 全部下游证书级联失效
            conn.execute(
                """
                UPDATE certificates AS c SET
                    status = 'INVALID',
                    revoked_reason = %s,
                    revoked_at = %s
                FROM unnest(%s::text[]) AS d(cert_no)
                WHERE c.cert_no = d.cert_no
                """,
                (
                    f"上游证书 {cert_no} 已撤销，引用链级联失效",
                    batch["revoked_at"],
                    [r["cert_no"] for r in descendants],
                ),
            )
            # 保存不可改写的失效明细与引用链快照
            full_chains = [(prefix, r) for r in descendants]
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO invalidations (batch_id, cert_no, chain, position)
                    VALUES (%s, %s, %s, %s)
                    """,
                    [
                        (
                            batch["batch_id"],
                            r["cert_no"],
                            [*prefix, *r["path"]],
                            len(prefix) + len(r["path"]),
                        )
                        for prefix, r in full_chains
                    ],
                )

    # 事务提交后组装返回内容
    revoked = get_certificate(conn, cert_no)
    affected = _build_invalidated_items(
        conn, batch["batch_id"], batch["revoked_at"], cert_no
    )
    return {
        "batch_id": batch["batch_id"],
        "revoked": revoked,
        "reason": reason,
        "operator": operator,
        "revoked_at": batch["revoked_at"],
        "affected_count": len(affected),
        "affected": affected,
    }


def _build_invalidated_items(conn, batch_id, revoked_at, root_no: str) -> list[dict]:
    rows = conn.execute(
        "SELECT cert_no, chain FROM invalidations WHERE batch_id = %s "
        "ORDER BY position, cert_no",
        (batch_id,),
    ).fetchall()
    if not rows:
        return []
    all_nos = {no for r in rows for no in r["chain"]}
    cert_map = {
        c["cert_no"]: c
        for c in conn.execute(
            f"SELECT {_VIEW_COLS} FROM v_certificates WHERE cert_no = ANY(%s)",
            (list(all_nos),),
        ).fetchall()
    }
    items = []
    for r in rows:
        chain = [cert_map[no] for no in r["chain"] if no in cert_map]
        items.append(
            {
                "cert_no": r["cert_no"],
                "chain": chain,
                # 深度按链上相对被撤销证书的层数计
                "depth": len(r["chain"]) - 1,
            }
        )
    return items


def get_revocation(conn, batch_id: int) -> dict:
    head = conn.execute(
        """
        SELECT r.batch_id, r.cert_no, r.reason, r.operator, r.revoked_at,
               (SELECT count(*) FROM invalidations i WHERE i.batch_id = r.batch_id) AS affected_count
        FROM revocations r WHERE r.batch_id = %s
        """,
        (batch_id,),
    ).fetchone()
    if head is None:
        raise ApiError(404, "revocation_not_found", f"撤销批次不存在: {batch_id}")
    cert_no = head.pop("cert_no")
    return {
        **head,
        "revoked": get_certificate(conn, cert_no),
        "affected": _build_invalidated_items(conn, batch_id, head["revoked_at"], cert_no),
    }


def list_revocations(conn, limit: int, offset: int) -> dict:
    total = conn.execute("SELECT count(*) AS n FROM revocations").fetchone()["n"]
    items = conn.execute(
        """
        SELECT r.batch_id, r.cert_no, r.reason, r.operator, r.revoked_at,
               (SELECT count(*) FROM invalidations i WHERE i.batch_id = r.batch_id) AS affected_count
        FROM revocations r
        ORDER BY r.batch_id DESC
        LIMIT %s OFFSET %s
        """,
        (limit, offset),
    ).fetchall()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# 客户交付归档包（冻结创建时刻的可交付追溯凭据：原始签发内容 + 追溯链 + 合并展示）
# ---------------------------------------------------------------------------

def _delivery_request_hash(cert_nos: list[str]) -> tuple[str, dict]:
    """规范化归档请求：有序列表原样参与指纹（顺序不同即不同请求）。"""
    doc = {"cert_nos": list(cert_nos)}
    canonical = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), doc


def _get_delivery_archive_row(conn, archive_no: str) -> dict | None:
    return conn.execute(
        "SELECT archive_no, request_hash, request, response, cert_nos, created_at "
        "FROM delivery_archives WHERE archive_no = %s",
        (archive_no,),
    ).fetchone()


def _delivery_replay_or_conflict(existing: dict, archive_no: str, req_hash: str) -> dict:
    """归档包编号已存在：同有序列表 → 重放首次快照；不同列表 → 409 冲突。"""
    if existing["request_hash"] != req_hash:
        raise ApiError(
            409,
            "archive_conflict",
            f"归档包编号 {archive_no} 已用于一份证书列表不同的交付请求；"
            "同一归档包编号只允许重放完全相同的有序列表",
            details={"archive_no": archive_no},
        )
    return existing["response"]


def _lock_delivery_universe(conn, cert_nos: list[str]) -> None:
    """锁定目标证书及其全部上游链（共享锁），保证整包对应同一个业务时点。

    加锁顺序按“从追溯起点到下游”的深度升序、同深度按编号升序——与撤销事务
    （被撤销证书先于下游、且总是祖先先于后代）及更正事务（仅锁目标行）的
    加锁顺序一致或为其链上的一段，因此与并发撤销/更正之间不会形成等待环：
    撤销/更正事务在本事务释放锁前无法提交，锁后的再读取看到的是统一状态，
    快照不会混合新旧状态。FOR SHARE 不阻塞签发事务的外键检查（FOR KEY
    SHARE 与之兼容），只阻塞会改状态/封印字段的撤销事务（FOR UPDATE）。
    """
    sql = """
        WITH RECURSIVE targets AS (
            SELECT DISTINCT cert_no
            FROM unnest(%s::text[]) AS t(cert_no)
        ),
        walk AS (
            SELECT cert_no, parent_cert_no, 0 AS dist
            FROM certificates
            WHERE cert_no IN (SELECT cert_no FROM targets)
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, w.dist + 1
            FROM certificates c JOIN walk w ON c.cert_no = w.parent_cert_no
        ),
        universe AS (
            SELECT cert_no, MIN(dist) AS dist
            FROM walk GROUP BY cert_no
        )
        SELECT c.cert_no
        FROM certificates c
        JOIN universe u ON u.cert_no = c.cert_no
        ORDER BY u.dist DESC, c.cert_no ASC
        FOR SHARE OF c
    """
    conn.execute(sql, (cert_nos,)).fetchall()


def _evaluate_delivery_targets(conn, cert_nos: list[str]) -> dict:
    """在统一锁定后校验每张目标证书：存在、CALIBRATION、自身与全链 ACTIVE/PASS。

    汇总全部不达标编号（整包拒绝时一次性指出），各分类按编号排序。
    """
    rows = {
        r["cert_no"]: r
        for r in conn.execute(
            f"SELECT {_VIEW_COLS} FROM v_certificates WHERE cert_no = ANY(%s)",
            (cert_nos,),
        ).fetchall()
    }
    missing, wrong_kind, not_active, not_qualified, chain_bad = [], [], [], [], []
    for no in cert_nos:
        row = rows.get(no)
        if row is None:
            missing.append(no)
            continue
        if row["kind"] != "CALIBRATION":
            # 基准证书不是可交付的“普通校准证书”，整包层面按目标不合格指出
            wrong_kind.append(no)
        if row["status"] != "ACTIVE":
            not_active.append(no)
        if row["conclusion"] != "PASS":
            not_qualified.append(no)

    # 仅对“存在且类型正确”的目标检查其完整上游链
    chain_sql = """
        WITH RECURSIVE walk AS (
            SELECT cert_no, parent_cert_no, 0 AS depth
            FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, w.depth - 1
            FROM certificates c JOIN walk w ON c.cert_no = w.parent_cert_no
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT vc.cert_no, vc.status, vc.conclusion
        FROM walk w JOIN v_certificates vc ON vc.cert_no = w.cert_no
        WHERE NOT w.is_cycle AND w.depth < 0
    """
    for no in cert_nos:
        row = rows.get(no)
        if row is None or row["kind"] != "CALIBRATION":
            continue
        bad_nodes = [
            r["cert_no"]
            for r in conn.execute(chain_sql, (no,)).fetchall()
            if r["status"] != "ACTIVE" or r["conclusion"] != "PASS"
        ]
        if bad_nodes:
            chain_bad.append(no)

    problems: set[str] = set(missing) | set(wrong_kind) | set(not_active)
    problems |= set(not_qualified) | set(chain_bad)
    return {
        "missing": sorted(missing),
        "wrong_kind": sorted(wrong_kind),
        "target_not_active": sorted(not_active),
        "target_not_qualified": sorted(not_qualified),
        "chain_not_eligible": sorted(chain_bad),
        "all": sorted(problems),
    }


def _not_deliverable_error(req, problems: dict) -> ApiError:
    categories = {
        "missing": problems["missing"],
        "not_calibration": problems["wrong_kind"],
        "target_not_active": problems["target_not_active"],
        "target_not_qualified": problems["target_not_qualified"],
        "chain_not_eligible": problems["chain_not_eligible"],
    }
    return ApiError(
        422,
        "archive_not_deliverable",
        "存在不存在或不满足交付条件的目标证书：仅接受目标均为 CALIBRATION，"
        "且目标及各自完整上游链均处于 ACTIVE、结论 PASS 的组合；整包未保存。"
        f"相关编号: {', '.join(problems['all'])}",
        details={"archive_no": req.archive_no, "cert_nos": problems["all"], **categories},
    )


def _build_delivery_response(conn, req, created_at: datetime) -> dict:
    """组装创建时刻快照：按提交顺序给出原始签发内容、追溯链与合并展示。"""
    items = []
    for no in req.cert_nos:
        certificate = get_certificate(conn, no)
        chain = get_chain(conn, no)
        declarations = conn.execute(
            f"SELECT {_DECL_COLS} FROM correction_declarations "
            "WHERE cert_no = %s ORDER BY seq ASC",
            (no,),
        ).fetchall()
        items.append({
            "cert_no": no,
            "certificate": certificate,
            "chain": chain,
            "display": _merged_display(certificate, declarations),
        })
    return {
        "archive_no": req.archive_no,
        "count": len(items),
        "created_at": created_at,
        "items": items,
    }


def _create_delivery_archive_once(conn, req, req_hash: str, request_doc: dict):
    """单次事务尝试；返回 (响应体, 是否首次创建)。唯一冲突交由外层处理。"""
    with conn.transaction():
        existing = _get_delivery_archive_row(conn, req.archive_no)
        if existing is not None:
            return _delivery_replay_or_conflict(existing, req.archive_no, req_hash), False
        _lock_delivery_universe(conn, req.cert_nos)
        problems = _evaluate_delivery_targets(conn, req.cert_nos)
        if problems["all"]:
            raise _not_deliverable_error(req, problems)
        created_at = datetime.now(timezone.utc)
        response = _build_delivery_response(conn, req, created_at)
        conn.execute(
            """
            INSERT INTO delivery_archives
                (archive_no, request_hash, request, response, cert_nos, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                req.archive_no,
                req_hash,
                Jsonb(request_doc),
                Jsonb(response, dumps=_json_dumps),
                list(req.cert_nos),
                created_at,
            ),
        )
        return response, True


def create_delivery_archive(conn, req) -> tuple[dict, bool]:
    """冻结客户交付凭据，返回 (响应体, 是否首次创建)。

    - 整包条件：目标编号互不重复、均为 CALIBRATION，且目标及各自完整上游链
      均 ACTIVE、结论 PASS；任一不满足整包不保存，错误详情分类列出相关编号；
    - 一致时点：目标与全部上游行在同一事务内按祖先优先顺序加 FOR SHARE 锁，
      并发撤销（FOR UPDATE）与并发更正（目标行 FOR UPDATE）要么先于本包提交
      （本包按新状态校验/合并），要么被阻塞到本包提交后（本包按旧状态快照），
      整包不混合新旧状态；
    - 幂等：同编号同有序列表重试返回首次快照（200，即使事后更正或撤销），
      同编号不同列表返回 409 archive_conflict；并发同编号由主键兜底，
      后到事务回滚后按已提交结果重放/判冲突；
    - 与撤销事务理论上的加锁等待环由死锁检测兜底，回滚后整体重试。
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for no in req.cert_nos:
        (duplicates if no in seen else seen).add(no)
    if duplicates:
        raise ApiError(
            422,
            "archive_cert_duplicate",
            f"归档包内证书编号必须互不重复，重复编号: {', '.join(sorted(duplicates))}",
            details={"archive_no": req.archive_no, "cert_nos": sorted(duplicates)},
        )
    req_hash, request_doc = _delivery_request_hash(req.cert_nos)
    for attempt in range(3):
        try:
            return _create_delivery_archive_once(conn, req, req_hash, request_doc)
        except psycopg.errors.DeadlockDetected:
            # 与并发撤销/更正理论上的加锁等待环由死锁检测终止：整体回滚后重试
            if attempt == 2:
                raise ApiError(
                    409,
                    "archive_busy",
                    f"归档包 {req.archive_no} 与并发撤销/更正竞争，重试后仍冲突，请稍后重试",
                    details={"archive_no": req.archive_no},
                )
        except psycopg.errors.UniqueViolation:
            # 并发同编号提交：主键兜底，后到事务回滚后按已提交结果重放/判冲突
            existing = _get_delivery_archive_row(conn, req.archive_no)
            if existing is not None:
                return _delivery_replay_or_conflict(existing, req.archive_no, req_hash), False
            raise


def get_delivery_archive(conn, archive_no: str) -> dict:
    """按归档包编号查询创建时刻快照；未知编号报不存在。"""
    row = _get_delivery_archive_row(conn, archive_no)
    if row is None:
        raise ApiError(404, "archive_not_found", f"归档包不存在: {archive_no}")
    return row["response"]


# ---------------------------------------------------------------------------
# 撤销批次的已交付凭据影响面（按归档包 + 包内目标证书逐行列出）
# ---------------------------------------------------------------------------

def get_revocation_archive_impact(conn, batch_id: int, limit: int, offset: int) -> dict:
    """按撤销批次追查已交付归档凭据的影响面（只读，覆盖全部已存在归档包）。

    清单行 = 一个归档包内的一张受影响目标证书：该目标在归档时刻冻结的追溯链
    （快照中的 chain.nodes）包含本批被撤销证书集合中的任一证书——集合含被主动
    撤销的证书（revocations.cert_no）与全部级联失效的下游证书（invalidations）。

    - 同一目标在同一包内只列一次（其链上可能命中多个本批证书，去重为一行）；
      同一目标出现在多个归档包时分别列出；同包未受影响项不出现；
    - 序位均从 1 开始：delivery_position 为原交付序位（提交列表顺序），
      revoked_chain_position 为本批被撤销证书在冻结链中的序位——失效证书必为
      被撤销证书的下游，故链中命中任一失效证书时，被撤销证书必在同一链上，
      该序位恒有定义；
    - 冻结链取自归档快照（response.items），不依赖当前证书状态，因此覆盖此前
      已存在的全部归档包，且不受事后撤销/更正影响；
    - 归档创建与撤销并发时按两者实际提交次序判定：撤销先提交则归档按新状态
      校验（含本批证书的包被拒、从不落库），归档先提交则其快照入清单；
      被拒创建的包不会出现在清单中；
    - delivery_archives / invalidations / revocations 均只增不改，按
      (archive_no, delivery_position) 稳定排序分页，重复查询结果稳定。
    """
    head = conn.execute(
        "SELECT batch_id, cert_no, revoked_at FROM revocations WHERE batch_id = %s",
        (batch_id,),
    ).fetchone()
    if head is None:
        raise ApiError(404, "revocation_not_found", f"撤销批次不存在: {batch_id}")

    rows = conn.execute(
        """
        WITH affected AS (
            -- 本批被撤销证书集合：被主动撤销的证书 + 级联失效的全部下游证书
            SELECT cert_no FROM invalidations WHERE batch_id = %s
            UNION ALL
            SELECT cert_no FROM revocations WHERE batch_id = %s
        ),
        delivered AS (
            -- 各归档包的目标证书及其冻结追溯链；ordinality 即原交付序位（从 1 开始）
            SELECT d.archive_no,
                   d.created_at AS archived_at,
                   t.ordinality AS delivery_position,
                   t.item ->> 'cert_no' AS cert_no,
                   t.item -> 'chain' -> 'nodes' AS nodes
            FROM delivery_archives d
            CROSS JOIN LATERAL jsonb_array_elements(d.response -> 'items')
                 WITH ORDINALITY AS t(item, ordinality)
        ),
        hits AS (
            -- 冻结链命中本批集合的目标；被撤销证书在链中的序位（从 1 开始）
            SELECT dl.archive_no, dl.cert_no, dl.delivery_position, dl.archived_at,
                   MIN(n.ordinality) FILTER (
                       WHERE n.node -> 'certificate' ->> 'cert_no' = %s
                   ) AS revoked_chain_position
            FROM delivered dl
            CROSS JOIN LATERAL jsonb_array_elements(dl.nodes)
                 WITH ORDINALITY AS n(node, ordinality)
            WHERE n.node -> 'certificate' ->> 'cert_no' IN (SELECT cert_no FROM affected)
            GROUP BY dl.archive_no, dl.cert_no, dl.delivery_position, dl.archived_at
        ),
        page AS (
            SELECT archive_no, cert_no, delivery_position, revoked_chain_position,
                   archived_at
            FROM hits
            ORDER BY archive_no ASC, delivery_position ASC
            LIMIT %s OFFSET %s
        )
        -- 单语句同时给出总数与本页：空页（offset 越过末页）时 total 仍为全部命中数
        SELECT p.archive_no, p.cert_no, p.delivery_position, p.revoked_chain_position,
               p.archived_at, t.total
        FROM (SELECT count(*) AS total FROM hits) t
        LEFT JOIN page p ON true
        """,
        (batch_id, batch_id, head["cert_no"], limit, offset),
    ).fetchall()

    total = rows[0]["total"] if rows else 0
    items = [
        {
            "archive_no": r["archive_no"],
            "cert_no": r["cert_no"],
            "delivery_position": r["delivery_position"],
            "revoked_chain_position": r["revoked_chain_position"],
            "archived_at": r["archived_at"],
        }
        for r in rows
        if r["archive_no"] is not None
    ]
    return {
        "batch_id": batch_id,
        "revoked_cert_no": head["cert_no"],
        "revoked_at": head["revoked_at"],
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
    }
