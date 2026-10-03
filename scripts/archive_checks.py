"""客户交付归档包测试：冻结快照/幂等重放/冲突/拒绝点名/并发一致性。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/archive_checks.py [base_url]
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
t0 = datetime(2026, 6, 1, 0, 0, tzinfo=TZ)
_seq = [0]


def ts(offset_days=0):
    return (t0 + timedelta(days=offset_days)).isoformat()


def ref(no, name=None, operator=None, remark=None):
    return call("POST", "/api/v1/reference-certificates", {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": name or ("基准" + no),
        "valid_from": ts(0), "valid_to": ts(400), "calibrated_at": ts(0),
        "operator": operator, "remark": remark})


def cal(no, parent, conclusion="PASS", name=None, operator=None, remark=None):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": name or ("仪表" + no),
        "valid_from": ts(2), "valid_to": ts(300), "calibrated_at": ts(3),
        "conclusion": conclusion, "parent_cert_no": parent,
        "operator": operator, "remark": remark})


def chain_nos(snapshot_item):
    return [n["certificate"]["cert_no"] for n in snapshot_item["chain"]["nodes"]]


# ---------------------------------------------------------------------------
print("== 0. 前置数据 ==")
s, _ = ref("AC-REF-1", name="归档基准器", operator="张工", remark="r0")
check("基准 201", s == 201, f"got {s}")
s, _ = cal("AC-C1", "AC-REF-1", name="仪表C1", operator="李工")
check("二级 201", s == 201, f"got {s}")
s, _ = cal("AC-C2", "AC-C1", name="仪表C2", operator="王工", remark="客户交付件")
check("三级 201", s == 201, f"got {s}")
s, _ = call("POST", "/api/v1/correction-declarations", {
    "declaration_no": "AC-CORR-1", "cert_no": "AC-C2",
    "corrections": {"device_name": "仪表C2（更名）"}, "reason": "名称笔误"})
check("归档前更正 201", s == 201, f"got {s}")

# ---------------------------------------------------------------------------
print("== A. 创建归档包：原始签发内容 + 从基准到目标的链 + 合并展示，按提交顺序 ==")
s, b = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DA-1", "cert_nos": ["AC-C1", "AC-C2"]})
check("创建 201", s == 201, f"got {s} {json.dumps(b, ensure_ascii=False)[:200]}")
check("count 与顺序", [i["cert_no"] for i in b["items"]] == ["AC-C1", "AC-C2"])
check("含创建时刻", bool(b.get("created_at")))
i1, i2 = b["items"]
check("原始签发内容不受更正影响",
      i2["certificate"]["device_name"] == "仪表C2"
      and i2["certificate"]["operator"] == "王工"
      and i2["certificate"]["status"] == "ACTIVE"
      and i2["certificate"]["conclusion"] == "PASS")
check("展示字段按已登记更正合并",
      i2["display"] == {"device_name": "仪表C2（更名）", "operator": "王工", "remark": "客户交付件"},
      json.dumps(i2["display"], ensure_ascii=False))
check("未更正证书展示取原始值",
      i1["display"] == {"device_name": "仪表C1", "operator": "李工", "remark": None})
check("C1 链：基准 -> C1", chain_nos(i1) == ["AC-REF-1", "AC-C1"], str(chain_nos(i1)))
check("C2 链：基准 -> C1 -> C2", chain_nos(i2) == ["AC-REF-1", "AC-C1", "AC-C2"])
check("链节点为完整原始证书内容",
      all(set(["cert_no", "kind", "status", "conclusion", "valid_from"]) <=
          set(n["certificate"]) for n in i2["chain"]["nodes"]))
created_at_1 = b["created_at"]

# ---------------------------------------------------------------------------
print("== B. 同编号同有序列表重试：200 返回首次快照 ==")
s, b_re = call("POST", "/api/v1/delivery-archives",
               {"archive_no": "DA-1", "cert_nos": ["AC-C1", "AC-C2"]})
check("重试 200", s == 200, f"got {s}")
check("与首次快照逐字节同构", b_re == b)

# ---------------------------------------------------------------------------
print("== C. 归档后更正/撤销，重试与查询均返回首次快照 ==")
s, _ = call("POST", "/api/v1/correction-declarations", {
    "declaration_no": "AC-CORR-2", "cert_no": "AC-C2",
    "corrections": {"remark": "归档后新增备注", "operator": None}, "reason": "后续更正"})
check("归档后再更正 201", s == 201, f"got {s}")
s, _ = call("POST", "/api/v1/certificates/AC-REF-1/revoke",
            {"reason": "基准超差", "operator": "qa"})
check("撤销基准 200", s == 200, f"got {s}")
s, cur = call("GET", "/api/v1/certificates/AC-C2")
check("目标已级联失效", s == 200 and cur["status"] == "INVALID", str(cur.get("status")))
s, b_re = call("POST", "/api/v1/delivery-archives",
               {"archive_no": "DA-1", "cert_nos": ["AC-C1", "AC-C2"]})
check("撤销后重试仍 200", s == 200, f"got {s}")
check("重试快照不随撤销/更正变化",
      b_re == b
      and b_re["created_at"] == created_at_1
      and b_re["items"][1]["display"]["remark"] == "客户交付件"
      and b_re["items"][1]["display"]["operator"] == "王工")
for n in b_re["items"][1]["chain"]["nodes"]:
    check(f"快照链 {n['certificate']['cert_no']} 冻结为 ACTIVE",
          n["certificate"]["status"] == "ACTIVE")
s, b_get = call("GET", "/api/v1/delivery-archives/DA-1")
check("按编号查询返回同一快照", s == 200 and b_get == b, f"got {s}")

# 原证书详情接口仍返回当前（已失效）状态，原始签发字段不变 —— 既有契约
s, cur1 = call("GET", "/api/v1/certificates/AC-C2")
check("证书详情状态为当前 INVALID、原始字段不变（既有契约）",
      cur1["status"] == "INVALID" and cur1["device_name"] == "仪表C2")

# 已失效状态不能再建新包
s, e = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DA-REJ-INVALID", "cert_nos": ["AC-C2"]})
check("失效目标整包拒绝", s == 422 and err_code(e) == "archive_not_deliverable",
      f"{s} {err_code(e)}")
check("失效原因点名（目标+链）",
      "AC-C2" in e["error"]["details"].get("target_not_active", [])
      and "AC-C2" in e["error"]["details"].get("chain_not_eligible", []))

# ---------------------------------------------------------------------------
print("== D. 同编号不同列表报冲突（顺序不同也算不同） ==")
for nos in (["AC-C2", "AC-C1"], ["AC-C1"], ["AC-C1", "AC-C2", "AC-REF-1"]):
    s, e = call("POST", "/api/v1/delivery-archives",
                {"archive_no": "DA-1", "cert_nos": nos})
    check(f"列表 {nos} -> 409 archive_conflict",
          s == 409 and err_code(e) == "archive_conflict", f"{s} {err_code(e)}")

# ---------------------------------------------------------------------------
print("== E. 未知编号查询 404 ==")
s, e = call("GET", "/api/v1/delivery-archives/DA-UNKNOWN")
check("404 archive_not_found", s == 404 and err_code(e) == "archive_not_found", f"got {s}")

# ---------------------------------------------------------------------------
print("== F. 拒绝路径：不存在/非 CALIBRATION/FAIL/重复，整包不落库并点名 ==")
s, _ = ref("AC-REF-2")
s, _ = cal("AC-FAIL", "AC-REF-2", conclusion="FAIL")
s, e = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DA-E1", "cert_nos": ["AC-GHOST"]})
check("不存在 422", s == 422 and err_code(e) == "archive_not_deliverable"
      and e["error"]["details"]["missing"] == ["AC-GHOST"], json.dumps(e, ensure_ascii=False))
s, e = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DA-E2", "cert_nos": ["AC-REF-2"]})
check("目标为基准证书 422",
      s == 422 and e["error"]["details"].get("not_calibration") == ["AC-REF-2"],
      json.dumps(e, ensure_ascii=False))
s, e = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DA-E3", "cert_nos": ["AC-FAIL"]})
check("目标 FAIL 422",
      s == 422 and e["error"]["details"].get("target_not_qualified") == ["AC-FAIL"])
s, e = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DA-E4", "cert_nos": ["AC-FAIL", "AC-GHOST", "AC-REF-2"]})
d = e["error"]["details"]
check("多问题一次性分类点名",
      s == 422 and d["missing"] == ["AC-GHOST"]
      and d["not_calibration"] == ["AC-REF-2"]
      and d["target_not_qualified"] == ["AC-FAIL"]
      and d["cert_nos"] == ["AC-FAIL", "AC-GHOST", "AC-REF-2"],
      json.dumps(d, ensure_ascii=False))
s, e = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DA-E5", "cert_nos": ["AC-FAIL", "AC-FAIL"]})
check("包内编号重复（独立错误码）",
      s == 422 and err_code(e) == "archive_cert_duplicate"
      and e["error"]["details"]["cert_nos"] == ["AC-FAIL"], f"got {s}")
for no in ["DA-E1", "DA-E2", "DA-E3", "DA-E4", "DA-E5"]:
    s, _ = call("GET", f"/api/v1/delivery-archives/{no}")
    check(f"被拒整包 {no} 未落库", s == 404, f"got {s}")

# FAIL 证书不能因与合格证书同包而混入；合格证书单独建另一包则成功
s, _ = cal("AC-OK", "AC-REF-2")
s, b = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DA-2", "cert_nos": ["AC-OK"]})
check("合格组合另建包 201", s == 201 and b["items"][0]["chain"]["root_cert_no"] == "AC-REF-2",
      f"got {s}")

# 上游 FAIL：普通证书引用 FAIL 证书在签发时就不可能成功；改为上游被撤销后
s, _ = call("POST", "/api/v1/certificates/AC-REF-2/revoke", {"reason": "r", "operator": "o"})
s, e = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DA-E6", "cert_nos": ["AC-OK"]})
check("上游链撤销后目标随链失效 -> 422 chain_not_eligible",
      s == 422 and "AC-OK" in e["error"]["details"].get("chain_not_eligible", [])
      and "AC-OK" in e["error"]["details"].get("target_not_active", []),
      json.dumps(e["error"]["details"], ensure_ascii=False))

# 撤销后重签新链可归档（编号不受影响）
s, _ = ref("AC-REF-3")
s, _ = cal("AC-NEW", "AC-REF-3")
s, b = call("POST", "/api/v1/delivery-archives",
            {"archive_no": "DA-3", "cert_nos": ["AC-NEW"]})
check("新合格链 201", s == 201, f"got {s}")

# ---------------------------------------------------------------------------
print("== G. 并发：归档 vs 撤销 —— 快照必须对应同一个一致业务时点 ==")
K = 24
for i in range(K):
    s, _ = ref(f"XR-{i}")
    assert s == 201, s
    s, _ = cal(f"XM-{i}", f"XR-{i}")
    assert s == 201, s
    s, _ = cal(f"XL-{i}", f"XM-{i}")
    assert s == 201, s

barrier = threading.Barrier(K * 2 + 1)
outcomes = []
olock = threading.Lock()


def archive_worker(i):
    barrier.wait()
    s, b = call("POST", "/api/v1/delivery-archives",
                {"archive_no": f"XDA-{i}", "cert_nos": [f"XL-{i}"]})
    with olock:
        outcomes.append(("archive", i, s, b))


def revoke_worker(i):
    barrier.wait()
    s, b = call("POST", f"/api/v1/certificates/XR-{i}/revoke",
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

ok_archives, bad_codes = [], set()
for kind, i, s, b in outcomes:
    if kind == "archive":
        if s == 201:
            ok_archives.append((i, b))
        elif s == 422 and err_code(b) == "archive_not_deliverable":
            pass
        else:
            bad_codes.add((s, err_code(b)))
    else:
        if s != 200:
            bad_codes.add((s, err_code(b)))
check("无 5xx/死锁失败/序列化失败等意外码", not bad_codes, f"bad={bad_codes}")

mismatch = []
for i, b in ok_archives:
    item = b["items"][0]
    statuses = [(n["certificate"]["cert_no"], n["certificate"]["status"])
                for n in item["chain"]["nodes"]]
    # 关键不变量：成功的快照里链上不得混合状态——要么整链冻结在 ACTIVE（撤销前），
    # 要么整包被拒（撤销后）；不允许“根 INVALID、叶 ACTIVE”的混合时点
    if any(st != "ACTIVE" for _, st in statuses):
        mismatch.append((i, statuses))
    stored = call("GET", f"/api/v1/delivery-archives/XDA-{i}")[1]
    if stored != b:
        mismatch.append((i, "stored snapshot differs"))
check(f"成功归档 {len(ok_archives)}/{K} 且全部整链一致（无新旧状态混合）",
      not mismatch, f"{len(ok_archives)} ok; bad={mismatch[:3]}")
check("撤销与归档互斥结果齐全（部分成功、部分被拒均合法）", 0 < len(ok_archives) < K,
      f"ok={len(ok_archives)}")

# ---------------------------------------------------------------------------
print("== H. 并发：归档 vs 更正 —— 展示值只取某个一致时点的顺序合并结果 ==")
s, _ = ref("HC-R")
assert s == 201
s, _ = cal("HC-C", "HC-R", name="原名")
assert s == 201
N_CORR = 8
barrier2 = threading.Barrier(N_CORR * 2 + 1)
out2 = []


def corr_worker(j):
    barrier2.wait()
    s, b = call("POST", "/api/v1/correction-declarations", {
        "declaration_no": f"HC-D{j}", "cert_no": "HC-C",
        "corrections": {"device_name": f"名称-{j}"}, "reason": f"并发更正{j}"})
    with olock:
        out2.append(("corr", j, s, b))


def arch_worker(j):
    barrier2.wait()
    s, b = call("POST", "/api/v1/delivery-archives",
                {"archive_no": f"HC-DA-{j}", "cert_nos": ["HC-C"]})
    with olock:
        out2.append(("arch", j, s, b))


ths = []
for j in range(N_CORR):
    ths.append(threading.Thread(target=corr_worker, args=(j,)))
    ths.append(threading.Thread(target=arch_worker, args=(j,)))
for t in ths:
    t.start()
barrier2.wait()
for t in ths:
    t.join()

unexpected = {(kind, s, err_code(b)) for kind, _, s, b in out2
              if not ((kind == "corr" and s == 201) or (kind == "arch" and s == 201))}
check("并发更正/归档全部 201（无死锁/冲突/5xx）", not unexpected, str(unexpected))

# 任一成功快照必须等于“按 seq 序的某个前缀”合并结果（单一一致时点），
# 且原证书字段始终为原始值
s, view = call("GET", "/api/v1/certificates/HC-C/corrections")
decls = sorted(view["declarations"], key=lambda d: d["seq"])
prefix_values = ["原名"]
for d in decls:
    prefix_values.append(d["corrections"]["device_name"])
bad = []
for kind, j, s, b in out2:
    if kind != "arch":
        continue
    disp = b["items"][0]["display"]["device_name"]
    if disp not in prefix_values:
        bad.append((j, disp))
    if b["items"][0]["certificate"]["device_name"] != "原名":
        bad.append((j, "original changed"))
check("每个快照展示值都对应某个已提交的一致合并时点", not bad, str(bad[:3]))

# ---------------------------------------------------------------------------
print("== I. 并发同编号同列表：1 次首次创建，其余重放同一快照 ==")
s, _ = ref("ID-R")
s, _ = cal("ID-C", "ID-R")
barrier3 = threading.Barrier(6 + 1)
out3 = []


def same_worker(j):
    barrier3.wait()
    s, b = call("POST", "/api/v1/delivery-archives",
                {"archive_no": "ID-DA", "cert_nos": ["ID-C"]})
    with olock:
        out3.append((j, s, b))


ths = [threading.Thread(target=same_worker, args=(j,)) for j in range(6)]
for t in ths:
    t.start()
barrier3.wait()
for t in ths:
    t.join()
codes = sorted(s for _, s, _ in out3)
bodies = {json.dumps(b, sort_keys=True, ensure_ascii=False) for _, _, b in out3}
check("6 并发：恰好 1 个 201 + 5 个 200", codes == [200] * 5 + [201], str(codes))
check("全部返回同一个首次快照", len(bodies) == 1, str(len(bodies)))
s, _ = call("GET", "/api/v1/delivery-archives/ID-DA")
check("快照可查询", s == 200)

# ---------------------------------------------------------------------------
print("== J. 并发同编号不同列表：1 次创建成功，异列表全部冲突，同列表重放 ==")
s, _ = ref("JD-R")
s, _ = cal("JD-A", "JD-R")
s, _ = cal("JD-B", "JD-R")
barrier4 = threading.Barrier(8 + 1)
out4 = []


def diff_worker(j, nos):
    barrier4.wait()
    s, b = call("POST", "/api/v1/delivery-archives",
                {"archive_no": "JD-DA", "cert_nos": nos})
    with olock:
        out4.append((j, nos, s, b))


ths = []
for j in range(4):
    ths.append(threading.Thread(target=diff_worker, args=(j, ["JD-A"])))
for j in range(4, 8):
    ths.append(threading.Thread(target=diff_worker, args=(j, ["JD-B"])))
for t in ths:
    t.start()
barrier4.wait()
for t in ths:
    t.join()
created = [o for o in out4 if o[2] == 201]
replayed = [o for o in out4 if o[2] == 200]
conflicts = [o for o in out4 if o[2] == 409]
check("8 并发：恰好 1 个 201", len(created) == 1,
      f"{len(created)=} {len(replayed)=} {len(conflicts)=}")
winner_list = created[0][1]
check("同列表 200 重放、异列表 409 archive_conflict",
      len(conflicts) == 4
      and all(err_code(o[3]) == "archive_conflict" for o in conflicts)
      and all(o[1] == winner_list for o in replayed)
      and len(replayed) == 3,
      f"{len(replayed)=} {len(conflicts)=} winner={winner_list}")
check("200 快照与首次一致",
      all(o[3] == created[0][3] for o in replayed))

# ---------------------------------------------------------------------------
fails = [r for r in results if not r[1]]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)
