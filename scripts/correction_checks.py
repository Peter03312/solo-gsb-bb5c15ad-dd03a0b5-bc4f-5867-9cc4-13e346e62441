"""更正声明场景测试：登记/顺序生效/显式置空/幂等重放/冲突/并发/错误路径。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/correction_checks.py [base_url]
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
t0 = datetime(2026, 2, 1, 0, 0, tzinfo=TZ)


def d(days):
    return (t0 + timedelta(days=days)).isoformat()


def declare(decl_no, cert_no, corrections, reason="录入错误更正"):
    return call("POST", "/api/v1/correction-declarations", {
        "declaration_no": decl_no, "cert_no": cert_no,
        "corrections": corrections, "reason": reason,
    })


print("== 0. 前置：基准 + 普通证书（展示字段有初始值） ==")
s, _ = call("POST", "/api/v1/reference-certificates", {
    "cert_no": "CORR-ROOT", "device_id": "DEV-CORR-ROOT", "device_name": "更正用基准器",
    "valid_from": d(0), "valid_to": d(400), "calibrated_at": d(0),
    "operator": "张工", "remark": "基准初始备注"})
check("基准证书 201", s == 201, f"got {s}")
s, _ = call("POST", "/api/v1/certificates", {
    "cert_no": "CORR-C1", "device_id": "DEV-CORR-1", "device_name": "车间万用表X",
    "valid_from": d(10), "valid_to": d(300), "calibrated_at": d(11),
    "conclusion": "PASS", "parent_cert_no": "CORR-ROOT",
    "operator": "李工", "remark": "初始备注"})
check("普通证书 201", s == 201, f"got {s}")

print("== A. 登记更正声明：返回声明顺序/原证书/声明历史/合并展示 ==")
s, body = declare("CORR-001", "CORR-C1", {"device_name": "车间万用表X-改"}, "设备名称笔误")
check("登记 201", s == 201, f"got {s} {json.dumps(body, ensure_ascii=False)[:200]}")
decl = body.get("declaration", {})
check("声明顺序 seq=1", decl.get("seq") == 1, str(decl.get("seq")))
check("声明回显修正集合与原因",
      decl.get("corrections") == {"device_name": "车间万用表X-改"}
      and decl.get("reason") == "设备名称笔误",
      json.dumps(decl, ensure_ascii=False)[:200])
check("原证书保持签发内容（不受更正影响）",
      body.get("certificate", {}).get("device_name") == "车间万用表X"
      and body.get("certificate", {}).get("operator") == "李工",
      json.dumps(body.get("certificate", {}), ensure_ascii=False)[:200])
disp = body.get("display", {})
check("合并展示：已更正字段生效、未提交字段保持原值",
      disp.get("device_name") == "车间万用表X-改"
      and disp.get("operator") == "李工" and disp.get("remark") == "初始备注",
      json.dumps(disp, ensure_ascii=False))
check("声明历史含 1 条且按序返回",
      [x["seq"] for x in body.get("declarations", [])] == [1],
      json.dumps(body.get("declarations", []), ensure_ascii=False)[:200])

print("== B. 显式置空与未提交严格区分 ==")
s, body = declare("CORR-002", "CORR-C1", {"operator": None}, "操作员误填，置空")
check("显式置空登记 201 且 seq=2", s == 201 and body["declaration"]["seq"] == 2, f"got {s}")
disp = body.get("display", {})
check("置空字段变为 null，其余字段不受波及",
      disp.get("operator") is None
      and disp.get("device_name") == "车间万用表X-改"
      and disp.get("remark") == "初始备注",
      json.dumps(disp, ensure_ascii=False))
check("声明历史保留置空语义（corrections 中 operator 为 null）",
      body["declarations"][1]["corrections"] == {"operator": None},
      json.dumps(body["declarations"][1], ensure_ascii=False)[:200])

print("== C. 同一字段按登记顺序生效：后登记覆盖先登记 ==")
s, body = declare("CORR-003", "CORR-C1",
                  {"device_name": "车间万用表X-再改", "remark": "补充说明"}, "二次更正")
check("第三条声明 201 且 seq=3", s == 201 and body["declaration"]["seq"] == 3, f"got {s}")
disp = body.get("display", {})
check("合并展示取最新登记值",
      disp.get("device_name") == "车间万用表X-再改" and disp.get("remark") == "补充说明"
      and disp.get("operator") is None,
      json.dumps(disp, ensure_ascii=False))

print("== D. 证书更正视图查询 ==")
s, body = call("GET", "/api/v1/certificates/CORR-C1/corrections")
check("查询 200", s == 200, f"got {s}")
check("声明历史 3 条且 seq 升序",
      [x["seq"] for x in body.get("declarations", [])] == [1, 2, 3],
      json.dumps([x["seq"] for x in body.get("declarations", [])]))
check("视图含原证书与合并展示",
      body.get("certificate", {}).get("device_name") == "车间万用表X"
      and body.get("display", {}).get("device_name") == "车间万用表X-再改",
      json.dumps(body, ensure_ascii=False)[:200])

print("== E. 原证书详情/追溯链保持既有契约（不随更正改变） ==")
s, body = call("GET", "/api/v1/certificates/CORR-C1")
check("证书详情仍为原始展示字段",
      s == 200 and body.get("device_name") == "车间万用表X"
      and body.get("operator") == "李工" and body.get("remark") == "初始备注",
      json.dumps(body, ensure_ascii=False)[:200])
s, body = call("GET", "/api/v1/certificates/CORR-C1/chain")
names = [n["certificate"]["device_name"] for n in body.get("nodes", [])]
check("追溯链节点仍为原始内容", names == ["更正用基准器", "车间万用表X"], str(names))

print("== F. 幂等：同编号同内容重试返回首次声明；键序无关 ==")
s, body = declare("CORR-001", "CORR-C1", {"device_name": "车间万用表X-改"}, "设备名称笔误")
check("重试返回 200", s == 200, f"got {s}")
check("返回首次声明（seq 不变、不重复登记）",
      body.get("declaration", {}).get("seq") == 1
      and len(body.get("declarations", [])) == 3,
      json.dumps(body.get("declaration", {}))[:150])
s, body = declare("CORR-004", "CORR-C1", {"remark": "r1", "device_name": "n1"}, "键序测试")
check("多字段声明 201", s == 201, f"got {s}")
s, body2 = declare("CORR-004", "CORR-C1", {"device_name": "n1", "remark": "r1"}, "键序测试")
check("同内容不同键序重放 200 且为同一条声明",
      s == 200 and body2.get("declaration", {}).get("seq") == body["declaration"]["seq"],
      f"got {s}")
s, body = call("GET", "/api/v1/certificates/CORR-C1/corrections")
check("重放未产生新声明（共 4 条）", len(body.get("declarations", [])) == 4,
      str(len(body.get("declarations", []))))

print("== G. 同编号不同内容：报冲突 ==")
s, body = declare("CORR-001", "CORR-C1", {"device_name": "别的名字"}, "设备名称笔误")
check("异内容（字段值不同）409 correction_conflict",
      s == 409 and err_code(body) == "correction_conflict",
      f"{s} {json.dumps(body, ensure_ascii=False)[:200]}")
s, body = declare("CORR-001", "CORR-C1", {"device_name": "车间万用表X-改"}, "换了个原因")
check("异内容（原因不同）409", s == 409 and err_code(body) == "correction_conflict", f"got {s}")
s, body = declare("CORR-001", "CORR-C1", {"device_name": "车间万用表X-改", "remark": "x"}, "设备名称笔误")
check("异内容（字段集合不同）409", s == 409 and err_code(body) == "correction_conflict", f"got {s}")

print("== H. 错误路径：证书不存在/字段越界/空修正/空原因 ==")
s, body = declare("CORR-E1", "CORR-NOPE", {"remark": "x"})
check("证书不存在 404 cert_not_found", s == 404 and err_code(body) == "cert_not_found",
      f"{s} {json.dumps(body, ensure_ascii=False)[:150]}")
s, body = declare("CORR-E2", "CORR-C1", {})
check("空修正集合 422 correction_empty", s == 422 and err_code(body) == "correction_empty",
      f"{s} {json.dumps(body, ensure_ascii=False)[:150]}")
s, body = declare("CORR-E3", "CORR-C1", {"status": "ACTIVE"})
check("越界字段 422 correction_field_not_allowed",
      s == 422 and err_code(body) == "correction_field_not_allowed"
      and body["error"]["details"]["fields"] == ["status"],
      f"{s} {json.dumps(body, ensure_ascii=False)[:200]}")
s, body = declare("CORR-E4", "CORR-C1", {"device_name": "ok", "valid_from": "2027-01-01"})
check("混合越界字段被点名", s == 422 and err_code(body) == "correction_field_not_allowed"
      and body["error"]["details"]["fields"] == ["valid_from"],
      f"{s} {json.dumps(body, ensure_ascii=False)[:200]}")
s, body = declare("CORR-E5", "CORR-C1", {"remark": "x"}, reason="")
check("空原因 422 correction_reason_required",
      s == 422 and err_code(body) == "correction_reason_required",
      f"{s} {json.dumps(body, ensure_ascii=False)[:150]}")
s, body = declare("CORR-E6", "CORR-C1", {"remark": "x"}, reason="   ")
check("纯空白原因 422 correction_reason_required",
      s == 422 and err_code(body) == "correction_reason_required", f"got {s}")
s, body = declare("CORR-E7", "CORR-C1", {"device_name": "x" * 257})
check("字段值超长 422 correction_field_invalid",
      s == 422 and err_code(body) == "correction_field_invalid",
      f"{s} {json.dumps(body, ensure_ascii=False)[:150]}")
s, body = call("GET", "/api/v1/correction-declarations/CORR-NOPE")
check("查询不存在声明 404 declaration_not_found",
      s == 404 and err_code(body) == "declaration_not_found", f"got {s}")
s, body = call("GET", "/api/v1/certificates/CORR-NOPE/corrections")
check("查询不存在证书的更正视图 404 cert_not_found",
      s == 404 and err_code(body) == "cert_not_found", f"got {s}")
s, body = call("GET", "/api/v1/certificates/CORR-C1/corrections")
check("错误请求均未落库（仍 4 条声明）", len(body.get("declarations", [])) == 4,
      str(len(body.get("declarations", []))))

print("== I. 按声明编号查询 ==")
s, body = call("GET", "/api/v1/correction-declarations/CORR-003")
check("查询 200 且定位到首次声明",
      s == 200 and body["declaration"]["seq"] == 3
      and body["declaration"]["cert_no"] == "CORR-C1",
      f"{s} {json.dumps(body, ensure_ascii=False)[:200]}")
check("含该证书当前声明历史与合并展示",
      len(body.get("declarations", [])) == 4
      and body.get("display", {}).get("device_name") == "n1",
      json.dumps(body.get("display", {}), ensure_ascii=False))

print("== J. 已撤销/级联失效证书可更正，状态与判定不受影响 ==")
s, _ = call("POST", "/api/v1/certificates/CORR-ROOT/revoke", {"reason": "基准超差", "operator": "qa"})
check("撤销基准 200", s == 200, f"got {s}")
s, body = call("GET", "/api/v1/certificates/CORR-C1")
check("下游已级联失效", s == 200 and body.get("status") == "INVALID", body.get("status"))
s, body = declare("CORR-005", "CORR-C1", {"remark": "失效后补充说明"}, "失效证书资料更正")
check("失效证书可更正 201", s == 201, f"got {s} {json.dumps(body, ensure_ascii=False)[:150]}")
check("合并展示更新", body.get("display", {}).get("remark") == "失效后补充说明",
      json.dumps(body.get("display", {}), ensure_ascii=False))
s, body = declare("CORR-006", "CORR-ROOT", {"operator": "张工（复核）"}, "撤销证书资料更正")
check("已撤销证书可更正 201", s == 201, f"got {s}")
s, body = call("GET", "/api/v1/certificates/CORR-C1")
check("更正后状态/有效期/原始字段不变",
      s == 200 and body.get("status") == "INVALID"
      and body.get("device_name") == "车间万用表X"
      and body.get("valid_from") == "2026-02-10T16:00:00+00:00",
      json.dumps(body, ensure_ascii=False)[:200])
s, body = call("GET", "/api/v1/certificates/CORR-ROOT")
check("被撤销证书状态不变", s == 200 and body.get("status") == "REVOKED", body.get("status"))
s, body = call("POST", "/api/v1/certificates", {
    "cert_no": "CORR-C1-USE", "device_id": "DEV-CORR-9",
    "valid_from": d(20), "valid_to": d(100), "calibrated_at": d(21),
    "conclusion": "PASS", "parent_cert_no": "CORR-C1"})
check("失效证书仍不得作为校准依据（签发判定不受影响）",
      s == 422 and err_code(body) == "reference_not_active",
      f"{s} {json.dumps(body, ensure_ascii=False)[:150]}")

print("== K. 并发更正同一证书：不丢失、不乱序 ==")
s, _ = call("POST", "/api/v1/reference-certificates", {
    "cert_no": "CORR-ROOT2", "device_id": "DEV-CORR-ROOT2",
    "valid_from": d(0), "valid_to": d(400), "calibrated_at": d(0)})
s, _ = call("POST", "/api/v1/certificates", {
    "cert_no": "CORR-C2", "device_id": "DEV-CORR-2",
    "valid_from": d(10), "valid_to": d(300), "calibrated_at": d(11),
    "conclusion": "PASS", "parent_cert_no": "CORR-ROOT2"})
check("并发用前置证书 201", s == 201, f"got {s}")

N = 8
outcomes = []
lock = threading.Lock()


def worker(i):
    s, body = declare(f"CORR-C-{i}", "CORR-C2", {"remark": f"并发更正{i}"}, "并发测试")
    with lock:
        outcomes.append((i, s, body))


threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
[t.start() for t in threads]
[t.join() for t in threads]
check(f"{N} 并发全部登记成功", all(s == 201 for _, s, _ in outcomes),
      f"codes={sorted(s for _, s, _ in outcomes)}")
s, body = call("GET", "/api/v1/certificates/CORR-C2/corrections")
seqs = sorted(x["seq"] for x in body.get("declarations", []))
check("声明顺序为 1..N 无重复无缺失", seqs == list(range(1, N + 1)), str(seqs))
last = next(x for x in body["declarations"] if x["seq"] == N)
check("合并展示为最后登记序号的值",
      body["display"]["remark"] == last["corrections"]["remark"],
      f"display={body['display'].get('remark')} last={last['corrections']['remark']}")

print("== L. 并发同编号同内容：一次登记，其余重放同一首次声明 ==")
s, _ = call("POST", "/api/v1/certificates", {
    "cert_no": "CORR-C3", "device_id": "DEV-CORR-3",
    "valid_from": d(10), "valid_to": d(300), "calibrated_at": d(11),
    "conclusion": "PASS", "parent_cert_no": "CORR-ROOT2"})
check("前置证书 201", s == 201, f"got {s}")
outcomes2 = []


def worker2(i):
    s, body = declare("CORR-SAME", "CORR-C3", {"operator": "王工"}, "并发同号同内容")
    with lock:
        outcomes2.append((i, s, body))


threads = [threading.Thread(target=worker2, args=(i,)) for i in range(5)]
[t.start() for t in threads]
[t.join() for t in threads]
codes2 = sorted(s for _, s, _ in outcomes2)
check("5 并发：1 个 201 + 4 个 200", codes2 == [200, 200, 200, 200, 201], f"codes={codes2}")
seqs2 = {o[2].get("declaration", {}).get("seq") for o in outcomes2}
check("全部返回同一条首次声明（seq 一致）", seqs2 == {1}, str(seqs2))
s, body = call("GET", "/api/v1/certificates/CORR-C3/corrections")
check("仅登记 1 条声明", len(body.get("declarations", [])) == 1,
      str(len(body.get("declarations", []))))

print("== M. 并发同编号不同内容：一次成功，其余冲突 ==")
outcomes3 = []


def worker3(i):
    s, body = declare("CORR-DIFF", "CORR-C3", {"remark": f"内容{i}"}, "并发同号异内容")
    with lock:
        outcomes3.append((i, s, body))


threads = [threading.Thread(target=worker3, args=(i,)) for i in range(4)]
[t.start() for t in threads]
[t.join() for t in threads]
winners = [o for o in outcomes3 if o[1] == 201]
losers = [o for o in outcomes3 if o[1] != 201]
check("4 并发恰好 1 个 201", len(winners) == 1,
      f"codes={sorted(o[1] for o in outcomes3)}")
check("其余均为 409 correction_conflict",
      len(losers) == 3
      and all(o[1] == 409 and err_code(o[2]) == "correction_conflict" for o in losers),
      json.dumps([(o[0], o[1], err_code(o[2])) for o in outcomes3], ensure_ascii=False))
s, body = call("GET", "/api/v1/certificates/CORR-C3/corrections")
check("冲突声明未落库（共 2 条）", len(body.get("declarations", [])) == 2,
      str(len(body.get("declarations", []))))

print("== N. 批次首次签发快照不受更正影响 ==")
s, body = call("POST", "/api/v1/certificate-batches", {
    "batch_key": "BAT-CORR",
    "certificates": [
        {"kind": "REFERENCE", "cert_no": "BAT-CORR-ROOT", "device_id": "DEV-BAT-CORR",
         "device_name": "批内基准器", "valid_from": d(0), "valid_to": d(400),
         "calibrated_at": d(0), "operator": "批内操作员"},
    ]})
check("批次签发 201", s == 201, f"got {s}")
s, _ = declare("CORR-007", "BAT-CORR-ROOT", {"device_name": "批内基准器-改", "operator": None},
               "批次证书资料更正")
check("批次证书可更正 201", s == 201, f"got {s}")
s, body = call("GET", "/api/v1/certificate-batches/BAT-CORR")
snap = body["items"][0]["certificate"]
check("批次快照保持首次签发内容",
      s == 200 and snap.get("device_name") == "批内基准器"
      and snap.get("operator") == "批内操作员",
      json.dumps(snap, ensure_ascii=False)[:200])
s, body = call("GET", "/api/v1/certificates/BAT-CORR-ROOT/corrections")
check("更正视图反映合并展示",
      body.get("display", {}).get("device_name") == "批内基准器-改"
      and body.get("display", {}).get("operator") is None,
      json.dumps(body.get("display", {}), ensure_ascii=False))

fails = [r for r in results if not r[1]]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)
