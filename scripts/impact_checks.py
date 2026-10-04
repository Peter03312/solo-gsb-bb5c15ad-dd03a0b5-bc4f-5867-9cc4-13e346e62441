"""撤销影响面追查测试：按撤销批次分页列出受影响的已交付凭据。

覆盖：两种序位（均从 1 开始）、按归档包编号+原交付序位稳定分页、同目标多包
分别列出、同包未受影响项不出现、目标只列一次、未知批次 404、空影响面空页、
归档创建与撤销并发按实际提交次序判定（被拒包不入清单）、重复查询结果稳定、
既有归档快照/归档查询/撤销响应契约不变。

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
        "cert_no": no, "device_id": "DEV-" + no, "device_name": "基准" + no,
        "valid_from": ts(0), "valid_to": ts(400), "calibrated_at": ts(0)})


def cal(no, parent):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": "仪表" + no,
        "valid_from": ts(2), "valid_to": ts(300), "calibrated_at": ts(3),
        "conclusion": "PASS", "parent_cert_no": parent})


def archive(no, cert_nos):
    return call("POST", "/api/v1/delivery-archives",
                {"archive_no": no, "cert_nos": cert_nos})


def revoke(cert_no):
    return call("POST", f"/api/v1/certificates/{cert_no}/revoke",
                {"reason": "影响面测试", "operator": "qa"})


def impact(batch_id, limit=None, offset=None):
    qs = ""
    if limit is not None:
        qs += f"&limit={limit}"
    if offset is not None:
        qs += f"&offset={offset}"
    return call("GET", f"/api/v1/revocations/{batch_id}/archive-impact?{qs}")


def row_key(item):
    return (item["archive_no"], item["delivery_position"])


# ---------------------------------------------------------------------------
print("== 0. 前置数据：三条链 + 四个归档包 ==")
# 链1：IM-R1 -> IM-A -> IM-B -> IM-C
assert ref("IM-R1")[0] == 201
assert cal("IM-A", "IM-R1")[0] == 201
assert cal("IM-B", "IM-A")[0] == 201
assert cal("IM-C", "IM-B")[0] == 201
# 链2：IM-R2 -> IM-D
assert ref("IM-R2")[0] == 201
assert cal("IM-D", "IM-R2")[0] == 201
# 链3（永不归档，用于空影响面）：IM-R4 -> IM-G
assert ref("IM-R4")[0] == 201
assert cal("IM-G", "IM-R4")[0] == 201

s, da1 = archive("DA-IMP-1", ["IM-C", "IM-A"])
check("归档包 DA-IMP-1 创建 201", s == 201, f"got {s}")
s, da2 = archive("DA-IMP-2", ["IM-B", "IM-D"])
check("归档包 DA-IMP-2 创建 201", s == 201, f"got {s}")
s, da3 = archive("DA-IMP-3", ["IM-D"])
check("归档包 DA-IMP-3 创建 201", s == 201, f"got {s}")
created = {"DA-IMP-1": da1["created_at"], "DA-IMP-2": da2["created_at"],
           "DA-IMP-3": da3["created_at"]}

# ---------------------------------------------------------------------------
print("== A. 撤销中间证书 IM-A：影响面按冻结链命中，两种序位从 1 开始 ==")
s, rb = revoke("IM-A")
check("撤销 IM-A 200", s == 200, f"got {s}")
b1 = rb["batch_id"]
check("级联失效 IM-B/IM-C",
      rb["affected_count"] == 2
      and {a["cert_no"] for a in rb["affected"]} == {"IM-B", "IM-C"},
      json.dumps(rb.get("affected_count")))

s, b = impact(b1)
check("影响面查询 200", s == 200, f"got {s}")
check("回显批次号与被撤销证书",
      b["batch_id"] == b1 and b["revoked_cert_no"] == "IM-A", json.dumps(b)[:120])
check("total=3（DA-IMP-1 两项 + DA-IMP-2 一项）", b["total"] == 3, str(b.get("total")))
got = [(i["archive_no"], i["cert_no"], i["delivery_position"], i["chain_position"])
       for i in b["items"]]
expect = [
    ("DA-IMP-1", "IM-C", 1, 2),   # 链 IM-R1,IM-A,IM-B,IM-C；IM-A 序位 2
    ("DA-IMP-1", "IM-A", 2, 2),   # 链 IM-R1,IM-A；IM-A 序位 2
    ("DA-IMP-2", "IM-B", 1, 2),   # 链 IM-R1,IM-A,IM-B；IM-A 序位 2
]
check("行内容（归档包/目标/交付序位/链序位）精确匹配", got == expect,
      json.dumps(got, ensure_ascii=False))
check("按归档包编号及原交付序位稳定排序",
      [row_key(i) for i in b["items"]] == sorted(row_key(i) for i in b["items"]))
check("目标 IM-C 只列一次（链上多证书同批失效不重复计）",
      sum(1 for i in b["items"] if i["cert_no"] == "IM-C") == 1)
check("同包未受影响目标不出现（DA-IMP-2 的 IM-D 不在列）",
      all(i["cert_no"] != "IM-D" for i in b["items"]))
check("未命中归档包不出现（DA-IMP-3 不在列）",
      all(i["archive_no"] != "DA-IMP-3" for i in b["items"]))
check("归档时刻与归档包创建时刻一致",
      all(created[i["archive_no"]] == i["archived_at"] for i in b["items"]),
      json.dumps([i["archived_at"] for i in b["items"]]))
check("两种序位均从 1 开始（无 0 序位）",
      all(i["delivery_position"] >= 1 and i["chain_position"] >= 1 for i in b["items"]))

# ---------------------------------------------------------------------------
print("== B. 分页稳定：limit/offset 切片拼接等于全量，重复查询结果一致 ==")
s, p1 = impact(b1, limit=2, offset=0)
s2, p2 = impact(b1, limit=2, offset=2)
s3, p3 = impact(b1, limit=1, offset=3)
check("分页各页 200", s == 200 and s2 == 200 and s3 == 200)
check("第 1 页 2 条、第 2 页 1 条、越界页空",
      len(p1["items"]) == 2 and len(p2["items"]) == 1 and p3["items"] == [])
check("各页 total 恒为 3", p1["total"] == 3 and p2["total"] == 3 and p3["total"] == 3)
check("分页拼接 == 全量且顺序一致",
      p1["items"] + p2["items"] == b["items"])
check("回显分页参数", p1["limit"] == 2 and p2["offset"] == 2)
s, again = impact(b1)
check("重复查询结果稳定（逐字节一致）", again == b)
s, e = impact(b1, limit=0)
check("limit=0 -> 422", s == 422, f"got {s}")
s, e = impact(b1, offset=-1)
check("offset=-1 -> 422", s == 422, f"got {s}")

# ---------------------------------------------------------------------------
print("== C. 未知批次返回明确的不存在错误 ==")
s, e = impact(999999)
check("未知批次 404 revocation_not_found",
      s == 404 and err_code(e) == "revocation_not_found", f"{s} {err_code(e)}")

# ---------------------------------------------------------------------------
print("== D. 无受影响归档返回空页 ==")
s, rb = revoke("IM-R4")   # IM-G 从未归档
b2 = rb["batch_id"]
s, b = impact(b2)
check("空影响面 200 且 total=0、items=[]",
      s == 200 and b["total"] == 0 and b["items"] == [], json.dumps(b)[:120])

# ---------------------------------------------------------------------------
print("== E. 同一目标出现在多个归档包时分别列出 ==")
s, rb = revoke("IM-R2")   # 级联失效 IM-D
b3 = rb["batch_id"]
s, b = impact(b3)
got = [(i["archive_no"], i["cert_no"], i["delivery_position"], i["chain_position"])
       for i in b["items"]]
expect = [
    ("DA-IMP-2", "IM-D", 2, 1),   # 包内第 2 位；链 IM-R2,IM-D，IM-R2 序位 1
    ("DA-IMP-3", "IM-D", 1, 1),   # 包内第 1 位
]
check("IM-D 在两个归档包中分别列出", got == expect, json.dumps(got, ensure_ascii=False))
check("total=2", b["total"] == 2)

# ---------------------------------------------------------------------------
print("== F. 再撤销链1基准 IM-R1：冻结链含本批被撤销证书即入清单 ==")
s, rb = revoke("IM-R1")   # 下游已全部失效，affected_count=0，但冻结链仍含 IM-R1
b4 = rb["batch_id"]
check("撤销 IM-R1 200 且无新增级联", s == 200 and rb["affected_count"] == 0,
      f"{s} affected={rb.get('affected_count')}")
s, b = impact(b4)
got = [(i["archive_no"], i["cert_no"], i["delivery_position"], i["chain_position"])
       for i in b["items"]]
expect = [
    ("DA-IMP-1", "IM-C", 1, 1),   # 基准在冻结链链首，序位 1
    ("DA-IMP-1", "IM-A", 2, 1),
    ("DA-IMP-2", "IM-B", 1, 1),
]
check("按冻结链口径命中（不受此前批次失效影响）", got == expect,
      json.dumps(got, ensure_ascii=False))

# ---------------------------------------------------------------------------
print("== G. 既有契约不变：归档快照 / 归档查询 / 撤销响应 ==")
s, snap = call("GET", "/api/v1/delivery-archives/DA-IMP-1")
check("归档快照查询 200 且与创建时一致", s == 200 and snap == da1, f"got {s}")
check("快照链节点仍冻结为 ACTIVE",
      all(n["certificate"]["status"] == "ACTIVE"
          for it in snap["items"] for n in it["chain"]["nodes"]))
s, replay = archive("DA-IMP-1", ["IM-C", "IM-A"])
check("归档同编号同列表重放 200 且快照不变", s == 200 and replay == da1, f"got {s}")
s, rev = call("GET", f"/api/v1/revocations/{b1}")
check("撤销批次详情契约不变",
      s == 200 and rev["batch_id"] == b1
      and rev["revoked"]["cert_no"] == "IM-A"
      and {a["cert_no"] for a in rev["affected"]} == {"IM-B", "IM-C"}
      and all("chain" in a and "depth" in a for a in rev["affected"]),
      json.dumps(rev)[:150])
s, lst = call("GET", "/api/v1/revocations")
check("撤销批次列表可查", s == 200 and lst["total"] >= 4, f"got {s}")

# ---------------------------------------------------------------------------
print("== H. 并发：归档创建 vs 撤销 —— 按实际提交次序判定，被拒包不入清单 ==")
K = 16
for i in range(K):
    assert ref(f"XI-R{i}")[0] == 201
    assert cal(f"XI-M{i}", f"XI-R{i}")[0] == 201
    assert cal(f"XI-L{i}", f"XI-M{i}")[0] == 201

barrier = threading.Barrier(K * 2 + 1)
outcomes = []
olock = threading.Lock()


def archive_worker(i):
    barrier.wait()
    s, b = archive(f"XDA-IMP-{i}", [f"XI-L{i}"])
    with olock:
        outcomes.append(("archive", i, s, b))


def revoke_worker(i):
    barrier.wait()
    s, b = revoke(f"XI-R{i}")
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

arch = {i: (s, b) for kind, i, s, b in outcomes if kind == "archive"}
revk = {i: (s, b) for kind, i, s, b in outcomes if kind == "revoke"}
check("并发撤销全部 200", all(s == 200 for s, _ in revk.values()),
      str({i: s for i, (s, _) in revk.items() if s != 200}))
bad_arch = {i: (s, err_code(b)) for i, (s, b) in arch.items()
            if not (s == 201 or (s == 422 and err_code(b) == "archive_not_deliverable"))}
check("并发归档结果仅 201 / 422 archive_not_deliverable", not bad_arch, str(bad_arch))

mismatch = []
committed = rejected = 0
for i in range(K):
    as_, ab = arch[i]
    rs_, rb = revk[i]
    bid = rb["batch_id"]
    qs, qb = impact(bid)
    rows = [(r["archive_no"], r["cert_no"], r["delivery_position"], r["chain_position"])
            for r in qb["items"]]
    if as_ == 201:
        committed += 1
        # 归档先提交：冻结链含被撤销基准，必须入清单且序位正确
        if rows != [(f"XDA-IMP-{i}", f"XI-L{i}", 1, 1)] or qb["total"] != 1:
            mismatch.append((i, "committed archive missing/wrong", rows))
        if ab["created_at"] != qb["items"][0]["archived_at"]:
            mismatch.append((i, "archived_at mismatch"))
    else:
        rejected += 1
        # 撤销先提交：归档被拒不落库，不得入清单
        if rows != [] or qb["total"] != 0:
            mismatch.append((i, "rejected archive listed", rows))
        gs, _ = call("GET", f"/api/v1/delivery-archives/XDA-IMP-{i}")
        if gs != 404:
            mismatch.append((i, "rejected archive persisted"))
    # 重复查询结果稳定
    qs2, qb2 = impact(bid)
    if qb2 != qb:
        mismatch.append((i, "unstable repeated query"))
check(f"并发结果与提交次序一致（成功 {committed}、被拒 {rejected}，均合法）",
      not mismatch and committed + rejected == K, str(mismatch[:3]))

# ---------------------------------------------------------------------------
print("== I. 被拒归档（校验不通过）不落库、不入任何影响面清单 ==")
s, e = archive("DA-IMP-REJ", ["IM-C"])   # IM-C 已级联失效
check("失效目标整包拒绝 422", s == 422 and err_code(e) == "archive_not_deliverable",
      f"{s} {err_code(e)}")
s, _ = call("GET", "/api/v1/delivery-archives/DA-IMP-REJ")
check("被拒归档未落库（404）", s == 404, f"got {s}")
s, b = impact(b4)  # IM-R1 批次的影响面
check("被拒归档不出现在影响面清单",
      all(i["archive_no"] != "DA-IMP-REJ" for i in b["items"]))

# ---------------------------------------------------------------------------
print("== J. 并发读+写：已提交批次的影响面对并发读稳定、不受新归档影响 ==")
s, ref_body = impact(b4)
check("基准影响面 total=3", s == 200 and ref_body["total"] == 3)
# 撤销提交后，新建归档不可能再包含被撤销证书的冻结链：并发写不影响已提交批次的结果
for i in range(4):
    assert ref(f"XJ-R{i}")[0] == 201
    assert cal(f"XJ-C{i}", f"XJ-R{i}")[0] == 201

barrier5 = threading.Barrier(8 + 4 + 1)
read_results = []
write_results = []


def reader_worker(j):
    barrier5.wait()
    for _ in range(15):
        qs, qb = impact(b4)
        with olock:
            read_results.append((j, qs, qb))


def writer_worker(j):
    barrier5.wait()
    s, b = archive(f"XJ-DA-{j}", [f"XJ-C{j}"])
    with olock:
        write_results.append((j, s))


ths = [threading.Thread(target=reader_worker, args=(j,)) for j in range(8)]
ths += [threading.Thread(target=writer_worker, args=(j,)) for j in range(4)]
for t in ths:
    t.start()
barrier5.wait()
for t in ths:
    t.join()

check("并发写新归档全部 201", all(s == 201 for _, s in write_results),
      str(write_results))
check("120 次并发读全部 200 且与基准逐字节一致",
      len(read_results) == 120
      and all(qs == 200 and qb == ref_body for _, qs, qb in read_results),
      f"reads={len(read_results)}")
s, after = impact(b4)
check("并发写后影响面仍不变", after == ref_body)

# ---------------------------------------------------------------------------
fails = [r for r in results if not r[1]]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)
