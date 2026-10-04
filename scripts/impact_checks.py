"""撤销批次对已交付归档凭据的影响面追查测试。

覆盖：命中判定（被撤销证书/级联失效证书在冻结链中）、两种 1 起始序位、
按 (归档包编号, 原交付序位) 稳定分页、同一目标多包分别列出、同包去重、
未受影响项不出现、空页、未知批次 404、撤销后新建/被拒归档不入清单、
归档与撤销并发按提交次序判定、既有归档/撤销响应契约不变。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/impact_checks.py [base_url]
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
results = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" :: {detail}"))


def err_code(body):
    return body.get("error", {}).get("code")


TZ = timezone(timedelta(hours=8))
t0 = datetime(2026, 7, 1, 0, 0, tzinfo=TZ)


def ts(offset_days=0):
    return (t0 + timedelta(days=offset_days)).isoformat()


def ref(no):
    return call("POST", "/api/v1/reference-certificates", {
        "cert_no": no, "device_id": "DEV-" + no,
        "valid_from": ts(0), "valid_to": ts(400), "calibrated_at": ts(0)})


def cal(no, parent):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": "DEV-" + no,
        "valid_from": ts(2), "valid_to": ts(300), "calibrated_at": ts(3),
        "conclusion": "PASS", "parent_cert_no": parent})


def archive(no, cert_nos):
    return call("POST", "/api/v1/delivery-archives",
                {"archive_no": no, "cert_nos": cert_nos})


def impact(batch_id, limit=None, offset=None):
    qs = ""
    params = []
    if limit is not None:
        params.append(f"limit={limit}")
    if offset is not None:
        params.append(f"offset={offset}")
    if params:
        qs = "?" + "&".join(params)
    return call("GET", f"/api/v1/revocations/{batch_id}/archive-impact{qs}")


def rows_of(body):
    return [(i["archive_no"], i["cert_no"], i["delivery_position"],
             i["revoked_chain_position"]) for i in body["items"]]


# ---------------------------------------------------------------------------
print("== 0. 前置数据：两条独立链 + 一条无归档链 ==")
# 链1：IA-REF -> IA-A -> IA-B -> IA-C
for s, _ in [ref("IA-REF"), cal("IA-A", "IA-REF"), cal("IA-B", "IA-A"), cal("IA-C", "IA-B")]:
    check("前置签发 201", s == 201, f"got {s}")
# 链2：IA-REF2 -> IA-D（与链1互不相干）
s, _ = ref("IA-REF2")
check("前置基准2 201", s == 201, f"got {s}")
s, _ = cal("IA-D", "IA-REF2")
check("前置二级2 201", s == 201, f"got {s}")
# 链3：IA-REF3 -> IA-E（不建任何归档包）
s, _ = ref("IA-REF3")
check("前置基准3 201", s == 201, f"got {s}")
s, _ = cal("IA-E", "IA-REF3")
check("前置二级3 201", s == 201, f"got {s}")

# ---------------------------------------------------------------------------
print("== A. 撤销前创建归档包 ==")
s, b1 = archive("IA-DA-1", ["IA-A", "IA-C"])
check("IA-DA-1 201", s == 201, f"got {s}")
s, b2 = archive("IA-DA-2", ["IA-B"])
check("IA-DA-2 201", s == 201, f"got {s}")
s, b3 = archive("IA-DA-3", ["IA-C", "IA-D"])
check("IA-DA-3 201", s == 201, f"got {s}")
s, b4 = archive("IA-DA-4", ["IA-D"])
check("IA-DA-4 201（不受影响包）", s == 201, f"got {s}")

# ---------------------------------------------------------------------------
print("== B. 中段撤销 IA-A：影响面清单（序位/去重/多包/未受影响项） ==")
s, rb = call("POST", "/api/v1/certificates/IA-A/revoke",
             {"reason": "期间核查超差", "operator": "qa"})
check("撤销 200", s == 200, f"got {s}")
batch_mid = rb["batch_id"]
check("级联失效 IA-B/IA-C",
      sorted(a["cert_no"] for a in rb["affected"]) == ["IA-B", "IA-C"],
      str(rb.get("affected_count")))

s, im = impact(batch_mid)
check("影响面查询 200", s == 200, f"got {s}")
check("批次上下文", im["batch_id"] == batch_mid
      and im["revoked_cert_no"] == "IA-A" and bool(im["revoked_at"]),
      json.dumps({k: im.get(k) for k in ("batch_id", "revoked_cert_no")}))
expected = [
    ("IA-DA-1", "IA-A", 1, 2),   # 链 IA-REF -> IA-A，被撤销证书在链中第 2 位
    ("IA-DA-1", "IA-C", 2, 2),   # 链 IA-REF -> IA-A -> IA-B -> IA-C
    ("IA-DA-2", "IA-B", 1, 2),
    ("IA-DA-3", "IA-C", 1, 2),
]
check("清单内容（含两种 1 起始序位）", rows_of(im) == expected,
      json.dumps(rows_of(im), ensure_ascii=False))
check("total 与分页参数回显", im["total"] == 4 and im["limit"] == 50 and im["offset"] == 0,
      f"total={im.get('total')}")
check("序位均从 1 开始",
      all(i["delivery_position"] >= 1 and i["revoked_chain_position"] >= 1
          for i in im["items"]))
# 目标 IA-C 的冻结链命中本批 3 张证书（IA-A/IA-B/IA-C），同包内只列一次
check("级联失效目标同包只列一次",
      sum(1 for r in rows_of(im) if r[:2] == ("IA-DA-1", "IA-C")) == 1
      and sum(1 for r in rows_of(im) if r[:2] == ("IA-DA-3", "IA-C")) == 1)
# 同一目标 IA-C 出现在两个归档包 -> 分别列出
check("同一目标跨包分别列出",
      sorted(r[0] for r in rows_of(im) if r[1] == "IA-C") == ["IA-DA-1", "IA-DA-3"])
# 未受影响项不出现：IA-D 与整包 IA-DA-4
check("未受影响目标与整包不出现",
      all(r[1] != "IA-D" for r in rows_of(im))
      and all(r[0] != "IA-DA-4" for r in rows_of(im)))
# 归档时刻与归档包创建时刻一致
s, snap1 = call("GET", "/api/v1/delivery-archives/IA-DA-1")
archived_at = {i["archive_no"]: i["archived_at"] for i in im["items"]}
check("归档时刻等于归档包创建时刻",
      archived_at["IA-DA-1"] == snap1["created_at"]
      and archived_at["IA-DA-1"] == archived_at["IA-DA-1"],
      f"{archived_at.get('IA-DA-1')} vs {snap1.get('created_at')}")

print("== B2. 稳定分页与重复查询 ==")
s, p1 = impact(batch_mid, limit=2, offset=0)
s, p2 = impact(batch_mid, limit=2, offset=2)
s, p3 = impact(batch_mid, limit=2, offset=4)
check("分页按 (归档包编号, 原交付序位) 稳定切页",
      rows_of(p1) == expected[0:2] and rows_of(p2) == expected[2:4]
      and p1["total"] == 4 and p2["total"] == 4,
      json.dumps(rows_of(p1) + rows_of(p2), ensure_ascii=False))
check("超出末页返回空 items、total 不变", p3["items"] == [] and p3["total"] == 4)
s, again = impact(batch_mid, limit=2, offset=0)
check("重复查询结果稳定", again == p1)
s, e = impact(batch_mid, limit=0)
check("limit=0 -> 422", s == 422, f"got {s}")
s, e = impact(batch_mid, offset=-1)
check("offset<0 -> 422", s == 422, f"got {s}")

# ---------------------------------------------------------------------------
print("== C. 撤销后：被拒/新建归档均不影响本批清单 ==")
s, e = archive("IA-DA-REJ", ["IA-B"])
check("含已失效目标的归档整包拒绝", s == 422 and err_code(e) == "archive_not_deliverable",
      f"{s} {err_code(e)}")
s, _ = archive("IA-DA-5", ["IA-D"])
check("无关证书新归档 201", s == 201, f"got {s}")
s, im2 = impact(batch_mid)
check("清单不增不减（被拒包与新无关包均不入列）", rows_of(im2) == expected,
      json.dumps(rows_of(im2), ensure_ascii=False))

# ---------------------------------------------------------------------------
print("== D. 再撤销根 IA-REF：下游已失效，仅被撤销证书命中，序位为 1 ==")
s, rb2 = call("POST", "/api/v1/certificates/IA-REF/revoke",
              {"reason": "基准超差", "operator": "qa"})
check("撤销根 200", s == 200, f"got {s}")
batch_root = rb2["batch_id"]
check("无新增级联失效（下游此前已失效）", rb2["affected_count"] == 0,
      str(rb2.get("affected_count")))
s, im_root = impact(batch_root)
expected_root = [(a, c, d, 1) for a, c, d, _ in expected]
check("根批次清单覆盖既有归档包、被撤销证书序位为 1",
      rows_of(im_root) == expected_root,
      json.dumps(rows_of(im_root), ensure_ascii=False))

# ---------------------------------------------------------------------------
print("== E. 无受影响归档的批次 -> 空页 ==")
s, rb3 = call("POST", "/api/v1/certificates/IA-E/revoke", {"reason": "停用"})
check("撤销无归档证书 200", s == 200, f"got {s}")
batch_none = rb3["batch_id"]
s, im_none = impact(batch_none)
check("空页：total=0 且 items=[]",
      s == 200 and im_none["total"] == 0 and im_none["items"] == [],
      f"{s} total={im_none.get('total')}")
s, im_none2 = impact(batch_none, limit=10, offset=5)
check("空页带分页参数同样为空", im_none2["items"] == [] and im_none2["total"] == 0)

# ---------------------------------------------------------------------------
print("== F. 未知批次 -> 404 revocation_not_found ==")
s, e = impact(10 ** 9)
check("404 revocation_not_found", s == 404 and err_code(e) == "revocation_not_found",
      f"{s} {err_code(e)}")

# ---------------------------------------------------------------------------
print("== G. 并发：归档 vs 撤销 —— 按实际提交次序判定，被拒包不入清单 ==")
K = 16
for i in range(K):
    s, _ = ref(f"IR-{i}")
    assert s == 201, s
    s, _ = cal(f"IM-{i}", f"IR-{i}")
    assert s == 201, s
    s, _ = cal(f"IL-{i}", f"IM-{i}")
    assert s == 201, s

barrier = threading.Barrier(K * 2 + 1)
outcomes = []
olock = threading.Lock()


def archive_worker(i):
    barrier.wait()
    s, b = archive(f"IDA-{i}", [f"IL-{i}"])
    with olock:
        outcomes.append(("archive", i, s, b))


def revoke_worker(i):
    barrier.wait()
    s, b = call("POST", f"/api/v1/certificates/IR-{i}/revoke",
                {"reason": "并发撤销", "operator": "t"})
    with olock:
        outcomes.append(("revoke", i, s, b))


threads = []
for i in range(K):
    threads.append(threading.Thread(target=archive_worker, args=(i,)))
    threads.append(threading.Thread(target=revoke_worker, args=(i,)))
for t in threads:
    t.start()
barrier.wait()
for t in threads:
    t.join()

arch_status = {i: s for kind, i, s, _ in outcomes if kind == "archive"}
rev_batches = {i: b["batch_id"] for kind, i, s, b in outcomes if kind == "revoke" and s == 200}
bad = [(k, i, s, err_code(b)) for k, i, s, b in outcomes
       if not ((k == "archive" and s in (201, 422)) or (k == "revoke" and s == 200))]
check("并发无意外错误码（归档仅 201/422，撤销全部 200）", not bad, str(bad[:3]))
check("两种结果都出现（部分包先于撤销提交、部分被拒）",
      201 in arch_status.values() and 422 in arch_status.values(),
      str(sorted(arch_status.values())))

mismatch = []
for i in range(K):
    s, im_i = impact(rev_batches[i])
    rows = rows_of(im_i)
    if arch_status[i] == 201:
        # 归档先提交：必入清单，序位 (1, 1)（链 IR -> IM -> IL，被撤销的根在链首）
        if rows != [(f"IDA-{i}", f"IL-{i}", 1, 1)] or im_i["total"] != 1:
            mismatch.append((i, "created archive missing", rows))
    else:
        # 撤销先提交：归档被拒、未落库，清单为空页
        if rows != [] or im_i["total"] != 0:
            mismatch.append((i, "rejected archive listed", rows))
check("每个并发批次清单与提交次序一致", not mismatch, str(mismatch[:3]))

# ---------------------------------------------------------------------------
print("== H. 既有契约不变：归档快照 / 归档查询 / 撤销响应 ==")
s, snap = call("GET", "/api/v1/delivery-archives/IA-DA-1")
check("归档快照仍冻结为撤销前状态",
      s == 200 and snap["count"] == 2
      and [i["cert_no"] for i in snap["items"]] == ["IA-A", "IA-C"]
      and all(n["certificate"]["status"] == "ACTIVE"
              for i in snap["items"] for n in i["chain"]["nodes"]),
      f"got {s}")
s, rev = call("GET", f"/api/v1/revocations/{batch_mid}")
check("撤销批次详情契约不变",
      s == 200 and rev["batch_id"] == batch_mid
      and rev["revoked"]["cert_no"] == "IA-A"
      and rev["affected_count"] == 2
      and sorted(a["cert_no"] for a in rev["affected"]) == ["IA-B", "IA-C"],
      f"got {s}")
s, lst = call("GET", "/api/v1/revocations?limit=5")
check("撤销批次列表契约不变", s == 200 and "items" in lst and "total" in lst, f"got {s}")

# ---------------------------------------------------------------------------
fails = [r for r in results if not r[1]]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)
