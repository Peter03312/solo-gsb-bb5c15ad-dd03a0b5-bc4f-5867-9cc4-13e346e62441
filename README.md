# 计量校准证书追溯服务

可离线运行的计量校准证书签发与**量值溯源链**管理服务。以基准（标准）设备的合格证书为
追溯起点，普通校准证书必须引用一张在**校准时刻有效且合格**的标准设备证书；撤销上游
证书时，其全部下游证书级联失效。离线采集的校准记录可作为**交接批次**整批签发：
批次键幂等、整批原子落库、批内证书可相互引用。已签发证书的设备名称、操作员、备注
录入有误时，可登记**更正声明**：声明按登记顺序生效、只增不改，原证书内容不受影响。
证书撤销后，客服可按**撤销批次追查已交付凭据的影响面**：哪些交付归档包中的
哪些目标证书，其归档时刻冻结的追溯链包含本批被撤销（含级联失效）的证书。

- 技术栈：Python 3.11 · FastAPI · PostgreSQL 16 · psycopg3
- 关键业务不变量由**数据库约束 + 触发器 + 单事务**保证，不依赖应用层“先查后写”，
  因此在并发下依然成立。

---

## 目录结构

```
.
├── app/                 # FastAPI 应用
│   ├── main.py          # 入口、异常处理、健康检查
│   ├── routers.py       # HTTP 路由
│   ├── repository.py    # 核心业务规则（签发校验/成环检测/级联失效）
│   ├── schemas.py       # 请求/响应模型
│   ├── db.py            # 连接池
│   ├── config.py        # 环境变量配置
│   └── errors.py        # 统一业务错误
├── db/init.sql          # 数据库初始化（枚举、约束、触发器、视图、批次/更正/交付归档表）
├── scripts/             # 冒烟测试脚本（可直接对运行中的服务执行）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

首次启动时：

1. 启动 PostgreSQL 16，并在**空数据卷**上自动执行 `db/init.sql` 完成建表；
2. 等待数据库健康检查通过后启动 API。

服务地址与端口：

| 服务 | 地址 |
| --- | --- |
| API 基址 | <http://localhost:8080> |
| 健康检查 | <http://localhost:8080/health> |
| 交互式文档（Swagger） | <http://localhost:8080/docs> |
| OpenAPI 描述 | <http://localhost:8080/openapi.json> |
| PostgreSQL（仅绑定宿主机回环地址） | `127.0.0.1:5432` |

停止与清理：

```bash
docker compose down          # 停止，保留数据卷（证书记录保留）
docker compose down -v       # 停止并删除数据卷（下次启动重新初始化空库）
```

> 完全离线部署：在有网络的机器上先 `docker compose build`（或 `docker save` 导出
> `python:3.11-slim` 与 `postgres:16-alpine` 镜像），拷贝到内网环境 `docker compose up -d`
> 即可，运行期不访问任何外部地址。

---

## 配置

所有配置通过环境变量注入，可在项目根目录放置 `.env`（参考 `.env.example`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_PORT` | `8080` | API 在宿主机暴露的端口 |
| `POSTGRES_USER` | `caltrace` | 数据库用户 |
| `POSTGRES_PASSWORD` | `caltrace` | 数据库密码（生产请修改） |
| `POSTGRES_DB` | `caltrace` | 数据库名 |
| `POSTGRES_PORT` | `127.0.0.1:5432` | 数据库在宿主机的绑定地址:端口 |
| `DB_HOST` / `DB_PORT` | `db` / `5432` | API 容器内连接数据库用（compose 已固定） |

### 不用 Compose、直连已有 PostgreSQL

```bash
pip install -r requirements.txt
psql "$DB_URL" -f db/init.sql          # 需 superuser 创建 btree_gist 扩展
DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=caltrace DB_USER=caltrace DB_PASSWORD=secret \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
```

要求 PostgreSQL 14+，当前库内可执行 `CREATE EXTENSION btree_gist`（contrib 自带）。

---

## 业务规则

1. **追溯起点**：基准设备证书（`kind=REFERENCE`）无上游引用，结论恒为合格。
2. **引用有效性**：普通证书（`kind=CALIBRATION`）必须引用一张标准设备证书，
   且该校准证书在本证书的**校准时刻**满足：状态生效、结论合格、校准时刻落在其
   有效期 `[valid_from, valid_to)` 内。已撤销（REVOKED）或已失效（INVALID）的
   证书不能再作为校准依据。
3. **引用链不可成环**：树状溯源结构 + 签发时防御性成环检测 + 递归 CTE `CYCLE` 保护。
4. **有效期不重叠**：同一设备处于 ACTIVE 的证书，有效期区间不得重叠
   （`EXCLUDE USING gist` 排他约束，半开区间 `[from, to)`，首尾相接允许）。
   证书被撤销/失效后区间释放，可重新签发。
5. **不可改写**：证书签发后业务字段禁止 UPDATE、禁止 DELETE（数据库触发器强制，
   直连数据库修改也会被拒绝）；状态只允许 `ACTIVE → REVOKED/INVALID`。
6. **级联失效**：撤销一张证书 → 其所有直接/间接下游 ACTIVE 证书在**同一事务**中
   置为 INVALID，受影响证书及完整引用链快照写入 `invalidations` 表，历史签发内容
   永久可查。
7. **并发安全**：
   - 相同证书编号并发签发 → 唯一约束保证仅一张成功，其余 `409 cert_duplicate`；
   - 同设备重叠区间并发签发 → 应用预检 `422 validity_overlap`，竞态穿透时由 GiST
     排他约束兜底返回 `409 validity_overlap_concurrent`；
   - 撤销与新签发并发 → 撤销事务持有被撤销证书行锁与全部下游行锁，
     新引用插入的外键检查会阻塞到撤销提交，随后按“引用已失效”被拒绝，
     不会出现“失效证书还能被引用”的窗口。
8. **批次签发（离线交接）**：`POST /api/v1/certificate-batches` 携带唯一批次键与
   非空证书列表，每项沿用单张签发的全部字段与规则；普通证书可引用库内证书或
   本批证书（**列表顺序不代表引用顺序**，服务按引用拓扑排序后逐张校验落库）。
   批内编号唯一、引用无环、上游当前生效且合格且校准时刻落在其有效期内、同设备
   生效有效期不重叠——**任一记录不合规则整批不落库**，错误响应以
   `details.cert_no` 定位出错编号。批次键为幂等键：同键同请求重放首次签发结果
   快照（即使证书事后被撤销），同键不同请求返回 `409 batch_conflict`；
   并发重复提交由批次键主键兜底，仅一个事务真正落库。
9. **更正声明（展示字段纠错）**：已签发证书的 `device_name` / `operator` / `remark`
   录入有误时，可登记更正声明（唯一声明编号 + 证书编号 + 非空修正字段集合 +
   非空原因）。仅这三个展示字段可更正；修正集合中字段值为 `null` 表示**显式置空**，
   与**未提交**的字段（保持原值）严格区分。每张证书的声明按成功登记顺序编号
   （`seq`，从 1 开始）并依序生效——同一字段后登记的声明覆盖先登记的。
   声明写入只增不改的 `correction_declarations` 表，**不改写原证书**：证书详情、
   追溯链、批次首次签发快照保持既有内容与响应契约；已撤销（REVOKED）或级联
   失效（INVALID）的证书同样可更正展示资料，其状态、有效期与签发、撤销判定
   不受影响。声明编号为幂等键：同编号同内容重试返回首次声明（HTTP 200），
   同编号不同内容返回 `409 correction_conflict`；同一证书的并发更正由证书行锁
   串行化，声明顺序不丢失、不乱序。
10. **客户交付归档包（可交付凭据冻结）**：向客户交付一组校准证书时，以归档包
    编号 + 非空证书编号列表（**顺序即交付顺序**）冻结当时可交付的追溯凭据。
    仅接受：包内编号互不重复；目标均为 `CALIBRATION`；目标**及各自完整上游链**
    全部处于 `ACTIVE` 且结论 `PASS`。任一证书不存在或不合格，**整包不保存**，
    错误响应分类列出全部相关编号（`details.cert_nos` 及各分类）。成功后每张
    目标证书按提交顺序返回：**原始签发内容**、**从基准到目标的完整链**、按
    归档时刻**已登记更正声明合并后的展示字段**；连同创建时刻一起作为不可改写
    的快照存入 `delivery_archives` 表。归档包编号为幂等键：编号与有序列表均
    相同的重试返回首次快照（HTTP 200，即使证书事后被更正或撤销）；同编号
    不同列表（含仅顺序不同）返回 `409 archive_conflict`；并发同编号由主键
    兜底。快照对应**同一个一致的业务时点**：创建事务对目标及全部上游行按
    “基准→下游”顺序加共享行锁（`FOR SHARE`），与并发撤销（`FOR UPDATE`）、
    并发更正（目标行 `FOR UPDATE`）的加锁顺序一致，二者不会混合新旧状态——
    并发撤销/更正要么先于归档提交（归档按新状态校验或合并），要么被阻塞到
    归档提交之后（归档整体按旧状态快照）；理论等待环由死锁检测终止并重试。
11. **撤销影响面追查（已交付凭据）**：`GET /api/v1/revocations/{batch_id}/archive-impact`
    按撤销批次列出受影响的已交付归档凭据——凡目标证书在**归档时刻冻结的追溯链**
    （归档快照，非当前状态）中包含本批被撤销证书集合（被主动撤销的证书 +
    全部级联失效证书）中任一证书者，即入清单。每行输出：归档包编号、包内目标
    证书编号、原交付序位、被撤销证书在冻结链中的序位、归档时刻；两种序位均
    从 1 开始。同一目标在同一包内只列一次（链上命中多张本批证书也去重为一行），
    同一目标出现在多个归档包时分别列出，同包未受影响项不出现。清单直接读取
    归档快照，覆盖此前已存在的全部归档包；归档创建与撤销并发时按两者实际
    提交次序判定（撤销先提交则含本批证书的归档被拒、从不落库，归档先提交则
    其快照入清单），被拒创建的包不入清单。按（归档包编号, 原交付序位）稳定
    排序分页，相关表均只增不改，重复查询结果稳定；未知批次返回
    `404 revocation_not_found`，无受影响归档返回空页（`total=0`、`items=[]`）。

证书状态：`ACTIVE` 生效中 / `REVOKED` 被主动撤销 / `INVALID` 因上游撤销级联失效。

---

## HTTP API

所有时间字段为带时区 ISO 8601，例如 `2026-03-01T09:00:00+08:00`。

### 1. 登记基准设备合格证书（追溯起点）

```bash
curl -sS -X POST http://localhost:8080/api/v1/reference-certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "REF-MASTER-001",
    "device_id": "DEV-MASTER",
    "device_name": "实验室最高标准器",
    "valid_from": "2026-01-01T00:00:00+08:00",
    "valid_to":   "2027-01-01T00:00:00+08:00",
    "calibrated_at": "2026-01-01T09:00:00+08:00",
    "operator": "张工",
    "remark": "年度基准登记"
  }'
```

`calibrated_at` 可省略，缺省取 `valid_from`。响应 `201`，含证书与其追溯链。

### 2. 签发普通校准证书

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "CAL-WS-001",
    "device_id": "DEV-WS-A",
    "device_name": "车间万用表A",
    "valid_from": "2026-03-01T00:00:00+08:00",
    "valid_to":   "2027-03-01T00:00:00+08:00",
    "calibrated_at": "2026-03-05T10:00:00+08:00",
    "conclusion": "PASS",
    "parent_cert_no": "REF-MASTER-001",
    "operator": "李工"
  }'
```

再向下签发一层（引用普通证书）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "CAL-CUS-001",
    "device_id": "DEV-CUS-B",
    "device_name": "客户仪表B",
    "valid_from": "2026-04-01T00:00:00+08:00",
    "valid_to":   "2026-10-01T00:00:00+08:00",
    "calibrated_at": "2026-04-02T10:00:00+08:00",
    "conclusion": "PASS",
    "parent_cert_no": "CAL-WS-001"
  }'
```

### 3. 查询追溯引用链

```bash
curl -sS http://localhost:8080/api/v1/certificates/CAL-CUS-001/chain | python3 -m json.tool
```

返回从基准到目标证书的有序节点链（`nodes[0]` 即追溯起点）。

### 4. 查询某证书的全部下游（撤销前影响面预览）

```bash
curl -sS http://localhost:8080/api/v1/certificates/REF-MASTER-001/downstream
```

### 5. 撤销证书（级联失效下游，返回受影响证书与引用链）

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates/REF-MASTER-001/revoke \
  -H 'Content-Type: application/json' \
  -d '{"reason": "年度比对发现超差", "operator": "质量负责人"}'
```

响应示例（节选）：

```json
{
  "batch_id": 1,
  "revoked": {"cert_no": "REF-MASTER-001", "status": "REVOKED", ...},
  "affected_count": 2,
  "affected": [
    {"cert_no": "CAL-WS-001",  "depth": 1, "chain": [{"cert_no": "REF-MASTER-001", ...}, {"cert_no": "CAL-WS-001", ...}]},
    {"cert_no": "CAL-CUS-001", "depth": 2, "chain": [{"cert_no": "REF-MASTER-001", ...}, {"cert_no": "CAL-WS-001", ...}, {"cert_no": "CAL-CUS-001", ...}]}
  ]
}
```

### 6. 查询证书 / 列表 / 设备历史 / 撤销批次

```bash
# 单张证书（无论 ACTIVE / REVOKED / INVALID，历史内容始终可查）
curl -sS http://localhost:8080/api/v1/certificates/CAL-CUS-001

# 列表，支持 device_id / status / kind 过滤与分页
curl -sS 'http://localhost:8080/api/v1/certificates?status=INVALID&limit=50&offset=0'

# 某设备的全部证书（含已撤销/已失效的历史记录）
curl -sS http://localhost:8080/api/v1/devices/DEV-CUS-B/certificates

# 撤销批次列表 / 详情
curl -sS http://localhost:8080/api/v1/revocations
curl -sS http://localhost:8080/api/v1/revocations/1
```

### 7. 批量签发交接批次（整批原子、批次键幂等）

离线采集的一组校准记录作为一个交接批次一次签发。每项通过 `kind` 区分基准
（`REFERENCE`）或普通（`CALIBRATION`）证书，字段与单张签发一致；普通证书的
`parent_cert_no` 可引用库内证书或本批证书，**列表顺序不代表引用顺序**：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificate-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_key": "HANDOFF-2026-10-03-001",
    "certificates": [
      {"kind": "CALIBRATION", "cert_no": "CAL-B-002", "device_id": "DEV-B-2",
       "valid_from": "2026-05-01T00:00:00+08:00", "valid_to": "2027-05-01T00:00:00+08:00",
       "calibrated_at": "2026-05-02T09:00:00+08:00", "conclusion": "PASS",
       "parent_cert_no": "CAL-B-001"},
      {"kind": "REFERENCE", "cert_no": "REF-B-000", "device_id": "DEV-B-0",
       "valid_from": "2026-01-01T00:00:00+08:00", "valid_to": "2027-01-01T00:00:00+08:00",
       "calibrated_at": "2026-01-01T09:00:00+08:00"},
      {"kind": "CALIBRATION", "cert_no": "CAL-B-001", "device_id": "DEV-B-1",
       "valid_from": "2026-03-01T00:00:00+08:00", "valid_to": "2027-03-01T00:00:00+08:00",
       "calibrated_at": "2026-03-02T09:00:00+08:00", "conclusion": "PASS",
       "parent_cert_no": "REF-B-000"}
    ]
  }'
```

首次成功响应 `201`（`items` 按请求列表顺序返回，各含从基准到自身的追溯链）：

```json
{
  "batch_key": "HANDOFF-2026-10-03-001",
  "count": 3,
  "issued_at": "2026-10-03T10:20:30.123456+00:00",
  "items": [
    {"certificate": {"cert_no": "CAL-B-002", ...},
     "chain": {"cert_no": "CAL-B-002", "root_cert_no": "REF-B-000", "length": 3, "nodes": [...]}},
    {"certificate": {"cert_no": "REF-B-000", ...}, "chain": {...}},
    {"certificate": {"cert_no": "CAL-B-001", ...}, "chain": {...}}
  ]
}
```

幂等与冲突约定：

- **同键同请求重试** → `200`，返回首次签发结果快照（即使证书事后被撤销）；
- **同键不同请求** → `409 batch_conflict`；
- **任一记录不合规** → 整批不落库，错误响应 `details.cert_no` 定位出错编号，
  错误码与单张签发一致（如 `reference_not_active`、`validity_overlap`），
  批内特有问题使用 `batch_cert_duplicate`（批内编号重复）与
  `reference_cycle`（批内引用成环）。

### 8. 查询签发批次（首次签发结果快照）

```bash
curl -sS http://localhost:8080/api/v1/certificate-batches/HANDOFF-2026-10-03-001 | python3 -m json.tool
```

返回与签发响应同构的快照，不受事后撤销影响；批次不存在返回 `404 batch_not_found`。

### 9. 登记更正声明（纠正展示字段录入错误）

证书签发后业务字段不可改写；若 `device_name` / `operator` / `remark` 录入有误，
通过更正声明纠错（仅这三个展示字段可更正）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/correction-declarations \
  -H 'Content-Type: application/json' \
  -d '{
    "declaration_no": "CORR-2026-0001",
    "cert_no": "CAL-WS-001",
    "corrections": {
      "device_name": "车间万用表A（FLUKE-17B）",
      "operator": null
    },
    "reason": "设备名称少录型号，操作员误填需置空"
  }'
```

- `corrections` 为**非空**修正字段集合：只接受 `device_name` / `operator` / `remark`；
  字段值为 `null` 表示**显式置空**，**未提交**的字段保持原值，两者严格区分；
- 首次登记响应 `201`；同编号同内容重试响应 `200`，返回首次声明（不重复登记）；
  同编号不同内容响应 `409 correction_conflict`；
- 已撤销 / 已失效证书同样可更正，其状态、有效期与签发、撤销判定不受影响。

响应（`200`/`201`）包含声明顺序、原证书、声明历史与合并后的当前展示：

```json
{
  "declaration": {
    "declaration_no": "CORR-2026-0001",
    "cert_no": "CAL-WS-001",
    "seq": 1,
    "corrections": {"device_name": "车间万用表A（FLUKE-17B）", "operator": null},
    "reason": "设备名称少录型号，操作员误填需置空",
    "created_at": "2026-10-03T10:25:30.123456+00:00"
  },
  "certificate": {"cert_no": "CAL-WS-001", "device_name": "车间万用表A", "operator": "李工", ...},
  "declarations": [
    {"declaration_no": "CORR-2026-0001", "seq": 1, ...}
  ],
  "display": {"device_name": "车间万用表A（FLUKE-17B）", "operator": null, "remark": null}
}
```

其中 `certificate` 始终是**签发时的原始内容**（不受更正影响）；`declarations` 为该证书
全部声明按登记顺序（`seq`）升序排列；`display` 为按序合并后的当前展示——同一字段
后登记的声明覆盖先登记的。

### 10. 查询证书的更正视图（声明历史 + 合并展示）

```bash
curl -sS http://localhost:8080/api/v1/certificates/CAL-WS-001/corrections | python3 -m json.tool
```

返回 `cert_no`、`certificate`（原证书）、`declarations`（声明历史，按 `seq` 升序）与
`display`（合并后的当前展示）；证书不存在返回 `404 cert_not_found`。

### 11. 按声明编号查询

```bash
curl -sS http://localhost:8080/api/v1/correction-declarations/CORR-2026-0001 | python3 -m json.tool
```

返回首次登记的声明及所属证书的当前更正视图（与登记响应同构）；
声明不存在返回 `404 declaration_not_found`。

> 注意：`GET /api/v1/certificates/{cert_no}`、追溯链与批次快照始终返回签发时的
> 原始字段值；合并后的展示值只通过上述更正相关接口与交付归档包快照获取。

### 12. 创建客户交付归档包（冻结可交付追溯凭据）

向客户交付一组校准证书时，以归档包编号和**非空、有序、编号互不重复**的证书
编号列表冻结当时可交付的凭据。服务校验每张目标证书均为 `CALIBRATION`，且
目标及其**完整上游链**全部 `ACTIVE`、结论 `PASS`；任一不达标则整包不保存，
错误体一次性分类点名全部相关编号：

```bash
curl -sS -X POST http://localhost:8080/api/v1/delivery-archives \
  -H 'Content-Type: application/json' \
  -d '{
    "archive_no": "DELIVERY-2026-10-03-001",
    "cert_nos": ["CAL-WS-001", "CAL-CUS-001"]
  }'
```

首次成功响应 `201`，`items` **按提交顺序**返回每张目标证书的：
`certificate`（原始签发内容）、`chain`（从基准到目标的完整引用链）、
`display`（按归档时刻已登记更正声明合并后的展示字段）：

```json
{
  "archive_no": "DELIVERY-2026-10-03-001",
  "count": 2,
  "created_at": "2026-10-03T10:40:30.123456+00:00",
  "items": [
    {
      "cert_no": "CAL-WS-001",
      "certificate": {"cert_no": "CAL-WS-001", "device_name": "车间万用表A", "status": "ACTIVE", ...},
      "chain": {"cert_no": "CAL-WS-001", "root_cert_no": "REF-MASTER-001", "length": 2, "nodes": [...]},
      "display": {"device_name": "车间万用表A（FLUKE-17B）", "operator": null, "remark": null}
    },
    {
      "cert_no": "CAL-CUS-001",
      "certificate": {...},
      "chain": {"length": 3, ...},
      "display": {"device_name": "客户仪表B", "operator": "王工", "remark": null}
    }
  ]
}
```

校验不通过示例（包中混入不存在/失效/不合格证书时，整包拒绝且不落库）：

```json
{
  "error": {
    "code": "archive_not_deliverable",
    "message": "存在不存在或不满足交付条件的目标证书……整包未保存。相关编号: CAL-X, CAL-Y",
    "details": {
      "archive_no": "DELIVERY-2026-10-03-001",
      "cert_nos": ["CAL-X", "CAL-Y"],
      "missing": ["CAL-X"],
      "not_calibration": [],
      "target_not_active": ["CAL-Y"],
      "target_not_qualified": [],
      "chain_not_eligible": ["CAL-Y"]
    }
  }
}
```

幂等、冲突与并发约定：

- **归档包编号 + 有序列表均相同的重试** → `200`，返回**首次创建时刻的快照**；
  即使此后证书被更正或撤销，快照内容（含链节点状态与当时的合并展示）不变；
- **同编号不同列表**（含仅元素顺序不同、子集/超集）→ `409 archive_conflict`；
- 列表内编号重复 → `422 archive_cert_duplicate`；
- 与并发撤销/更正相遇时，快照对应同一个一致的业务时点，不会混合新旧状态：
  撤销/更正先提交则本包按新状态判定（成功则整体新状态快照，否则整包拒绝），
  本包先提交则整体冻结旧状态；并发同编号重试由主键保证仅一个快照。

### 13. 查询交付归档包（创建时刻快照）

```bash
curl -sS http://localhost:8080/api/v1/delivery-archives/DELIVERY-2026-10-03-001 | python3 -m json.tool
```

返回与创建响应同构的快照，不受事后更正/撤销影响；归档包编号未知返回
`404 archive_not_found`。

### 14. 按撤销批次追查已交付凭据影响面（分页）

证书撤销后，客服凭撤销响应中的 `batch_id` 追查该批撤销波及了哪些已交付的
归档凭据。以第 5 节撤销 `REF-MASTER-001` 得到的 `batch_id=1` 为例：

```bash
curl -sS 'http://localhost:8080/api/v1/revocations/1/archive-impact?limit=50&offset=0' | python3 -m json.tool
```

响应示例（假设第 12 节的归档包 `DELIVERY-2026-10-03-001` 已交付）：

```json
{
  "batch_id": 1,
  "revoked_cert_no": "REF-MASTER-001",
  "revoked_at": "2026-10-03T11:02:03.123456+00:00",
  "total": 2,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "archive_no": "DELIVERY-2026-10-03-001",
      "cert_no": "CAL-WS-001",
      "delivery_position": 1,
      "revoked_chain_position": 1,
      "archived_at": "2026-10-03T10:40:30.123456+00:00"
    },
    {
      "archive_no": "DELIVERY-2026-10-03-001",
      "cert_no": "CAL-CUS-001",
      "delivery_position": 2,
      "revoked_chain_position": 1,
      "archived_at": "2026-10-03T10:40:30.123456+00:00"
    }
  ]
}
```

每行 = 一个归档包内的一张受影响目标证书：

| 字段 | 含义 |
| --- | --- |
| `archive_no` | 归档包编号 |
| `cert_no` | 包内目标证书编号 |
| `delivery_position` | 原交付序位（提交列表中的位置，从 1 开始） |
| `revoked_chain_position` | 本批被撤销证书在该目标**冻结追溯链**中的序位（链首基准为 1） |
| `archived_at` | 归档时刻（归档包创建时间） |

入列与分页约定：

- 目标的**冻结链**（归档快照中的 `chain`，非当前状态）包含本批被撤销证书
  集合（被主动撤销的证书 + 全部级联失效证书）中任一证书即入列；
- 同一目标在同一包内只列一次；同一目标出现在多个归档包时分别列出；
  同包未受影响项不出现；
- 覆盖此前已存在的全部归档包；与归档创建并发时按实际提交次序判定，
  被拒创建的包不入清单；
- 按 `(archive_no, delivery_position)` 稳定排序，`limit`（1–200，默认 50）
  与 `offset`（默认 0）分页；`total` 为全部命中数（空页/末页之后也如实返回）；
- 批次不存在返回 `404 revocation_not_found`；无受影响归档返回
  `200` 空页（`total=0`、`items=[]`）。

---

## 错误响应

统一格式：`{"error": {"code": "...", "message": "...", "details": {...}}}`

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `cert_not_found` / `device_not_found` / `revocation_not_found` / `batch_not_found` / `declaration_not_found` / `archive_not_found` | 目标不存在 |
| 409 | `cert_duplicate` | 证书编号已存在（含并发重复签发、批次与库内冲突） |
| 409 | `batch_conflict` | 同一批次键提交了内容不同的签发请求 |
| 409 | `correction_conflict` | 同一声明编号提交了内容不同的更正请求 |
| 409 | `archive_conflict` | 同一归档包编号提交了证书列表不同（含仅顺序不同）的交付请求 |
| 409 | `archive_busy` | 归档与并发撤销/更正竞争且死锁重试耗尽（防御性兜底，调用方可重试） |
| 422 | `archive_cert_duplicate` | 归档包内证书编号重复 |
| 422 | `archive_not_deliverable` | 归档目标存在不达标项：不存在/非 CALIBRATION/目标或上游链非 ACTIVE/非 PASS（`details` 分类点名，整包未保存） |
| 422 | `batch_cert_duplicate` | 批次内证书编号重复 |
| 422 | `correction_empty` | 更正声明的修正字段集合为空 |
| 422 | `correction_field_not_allowed` | 更正了 device_name/operator/remark 之外的字段（`details.fields` 点名） |
| 422 | `correction_field_invalid` | 更正字段值越界（如长度超限） |
| 422 | `correction_reason_required` | 更正原因为空或纯空白 |
| 422 | `invalid_reference` | 引用的标准证书不存在（库内与本批均找不到） |
| 422 | `reference_not_active` | 引用了已撤销/已失效证书（失效证书不得作依据） |
| 422 | `reference_not_qualified` | 引用的标准证书结论为不合格 |
| 422 | `reference_out_of_validity` | 校准时刻不在标准证书有效期内 |
| 422 | `reference_cycle` | 引用关系将形成环路（含批内成环） |
| 422 | `validity_overlap` | 同设备已生效证书有效期重叠（含批内重叠） |
| 409 | `validity_overlap_concurrent` | 并发签发重叠，排他约束兜底拒绝 |
| 409 | `cert_not_active` | 撤销一张非 ACTIVE 证书 |

> 批次签发中由单条记录触发的校验错误与单张签发同码，并在 `details.cert_no`
> 中给出出错证书编号（必要时附 `details.parent_cert_no` / `details.cycle` 等）。

---

## 自测

服务启动后执行（仅依赖 bash + curl / python3 标准库）：

```bash
scripts/smoke-test.sh                 # 17 项主流程与错误路径
python3 scripts/extra_checks.py       # 21 项并发/边界/中段撤销补充检查
python3 scripts/batch_checks.py       # 批次签发：原子性/乱序引用/幂等重放/键冲突/并发
python3 scripts/correction_checks.py  # 更正声明：顺序生效/显式置空/幂等/冲突/并发
python3 scripts/archive_checks.py     # 交付归档包：冻结快照/整包拒绝点名/重试/冲突/并发一致性
python3 scripts/impact_checks.py      # 撤销影响面：命中判定/序位/稳定分页/空页/并发提交次序
```

覆盖：基准登记、多级签发、无效引用、越界校准时刻、区间重叠（含首尾相接）、
同编号并发与重叠区间并发（3 线程仅 1 成功）、中段撤销、级联失效、失效证书
禁止引用、撤销后重签、历史可查、撤销批次回放，数据库层 UPDATE/DELETE 封印，
以及批次签发的整批原子性、批内乱序引用与拓扑校验、同键重放（含事后撤销）、
同键冲突、并发重复提交（5 线程仅 1 次首次签发）与批次查询；
更正声明的登记与顺序生效、显式置空与未提交的区分、原证书/追溯链/批次快照
不受更正影响、已撤销/已失效证书可更正且状态判定不变、同编号同内容重放
（含键序无关）、同编号异内容冲突、同一证书并发更正（8 线程 seq 1..8 不丢失
不乱序）、并发同编号（同内容仅 1 次登记、异内容仅 1 次成功）与全部错误路径；
交付归档包的创建快照（原始内容/完整链/合并展示/提交顺序）、同编号同有序列表
重放（首次 201、重试 200，事后更正或撤销快照不变）、同编号异列表（含仅顺序
不同）冲突、未知编号 404、整包拒绝且不落库（不存在/基准证书/FAIL/上游链撤销/
重复编号，多问题分类点名）、与并发撤销（24 组链各自归档×撤销，成功快照整链
ACTIVE，无新旧状态混合）、与并发更正（展示值只取某个已提交的一致合并时点）、
并发同编号（同列表 1 次创建其余重放、异列表仅 1 次创建其余 409）以及
150 线程撤销+更正+归档高强度混合压测（零死锁、零混合时点）；
撤销影响面追查的命中判定（被撤销证书与级联失效证书在冻结链中）、
两种 1 起始序位、按（归档包编号, 原交付序位）稳定分页与末页后空页、
同一目标多包分别列出与同包去重、未受影响项不出现、撤销后被拒/新建
归档不入清单、根撤销覆盖既有归档包、无影响批次空页、未知批次 404、
归档与撤销并发按实际提交次序判定（16 组链各自归档×撤销逐批核对），
以及既有归档快照/归档查询/撤销响应契约不变。
