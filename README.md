# 联合目录补采采集服务

联合目录管理员对单馆书目进行补采的 OAI-PMH 采集服务。核心保证：**翻页失败绝不让半批记录入库**。

## 需求与实现对照

| 需求 | 实现 |
| --- | --- |
| FastAPI 接收截止日，首次另填起始日 | `POST /api/v1/harvest`，请求体 `until` 必填、`from` 选填 |
| 之后从上次成功采集的截止日接着采，起止日都包含 | `from` 留空时取 `harvest_progress.last_until` 作为起始日（含当天） |
| OAI-PMH ListRecords 按日期拉取 oai_dc | 首请求 `verb=ListRecords&metadataPrefix=oai_dc&from=...&until=...` |
| 续页用 resumptionToken 替代日期和格式参数 | 续页请求只带 `verb` + `resumptionToken`；样例源对混用参数返回 `badArgument` |
| 令牌失效丢弃已取页，按原区间重抓一次 | 捕获 `badResumptionToken` 后清空已取页，用原始 from/until 重新拉取；只重抓一次 |
| 按标识更新 SQLite 书目 | `bib_records.identifier` 主键 upsert，重复采集幂等 |
| 遇删除标记就撤下 | header `status=deleted` 的记录置为 `withdrawn`（库中不存在则写墓碑） |
| 整批采完才提交书目变更和截止日 | 单事务 `BEGIN IMMEDIATE`，全部页取完才写库并提交；异常整体回滚 |
| 无匹配也推进截止日 | `noRecordsMatch` 不写书目，同事务推进 `last_until` |
| 换系统登记新旧标识对照 | `POST /api/v1/identifier-mappings` 整批写入 `identifier_mappings` |
| 登记旧号在架、新号未入库、标识不重复参与 | 单事务校验；任一冲突返回 `409` 并指出标识，整批不登记 |
| 登记不改书目/状态/截止日 | 只写对照表，不碰 `bib_records` 与 `harvest_progress` |
| 补采按已登记对照写库 | 写库事务内读取对照，即使翻页早于登记开始也按最新对照处理 |
| 旧号记录及删除标记忽略 | 旧号停用不会撤下书目；新号的更新/撤下落到原书目 |
| 目录仍列旧号、详情任一标识可查 | 一个逻辑书目只有旧号一行；`/catalog/{old或new}` 均可查 |
| Docker Compose 启动服务和本地分页样例源 | `docker compose up --build` 启动 `harvester-api` + `oai-source` |
| Swagger 发起采集并查询目录和进度 | http://localhost:8000/docs |

## 目录结构

```
app/                  FastAPI 采集服务
  main.py             路由（采集/标识对照/目录/进度/运行历史/健康检查）
  harvester.py        采集编排：翻页、令牌失效重抓、按对照整批落库
  mappings.py         新旧标识对照的整批校验与登记
  oai_client.py       OAI-PMH 2.0 客户端（httpx + xml.etree）
  db.py               SQLite 表结构与事务操作
  schemas.py          Pydantic 请求/响应模型
  Dockerfile
sample-oai/           本地分页样例 OAI-PMH 源（纯标准库）
  server.py           27 条 10 月样例记录 + 换系统轨迹（11-01/02/15），PAGE_SIZE=10
  Dockerfile
tests/                pytest 端到端测试（19 个用例）
scripts/              辅助脚本（故障回滚演示）
docker-compose.yml
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 采集服务 API / Swagger：http://localhost:8000/docs
- 样例 OAI 源：http://localhost:8080/oai?verb=Identify
- SQLite 数据保存在命名卷 `harvester-data`（容器内 `/data/harvester.db`）

## 操作流程（Swagger 或 curl）

### 1. 首次采集：另填起始日，起止日都包含

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-15"}'
```

响应：

```json
{
  "status": "success",
  "from_date": "2026-10-01",
  "until_date": "2026-10-15",
  "pages": 2, "fetched": 15,
  "inserted": 14, "updated": 0, "withdrawn": 1,
  "ignored": 0, "retries": 0, "run_id": 1
}
```

### 2. 后续采集：只填截止日，自动从上次截止日（含）续采

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"until": "2026-10-27"}'
# 实际区间：2026-10-15 ~ 2026-10-27（last_until 含当天，避免边界漏采）
```

### 3. 查询目录与进度

```bash
curl 'http://localhost:8000/api/v1/catalog?status=active'   # 在架（默认）
curl 'http://localhost:8000/api/v1/catalog?status=all'      # 含已撤下
curl 'http://localhost:8000/api/v1/catalog/oai:sample.lib:rec-013'
curl 'http://localhost:8000/api/v1/progress'                # 上次成功截止日
curl 'http://localhost:8000/api/v1/runs'                    # 采集运行历史
```

### 4. 无匹配也推进截止日

对没有任何记录的区间发起采集，返回 `fetched=0` 且 `last_until` 照常推进。

## 成员馆换系统：新旧标识对照

成员馆换系统后，同一书目会改用新的 OAI 标识。读者收藏的旧链接必须继续可用，
因此联合目录仍以旧号为主键，补采时把新号上的更新/撤下映射回旧书目。

登记规则（整批单事务，冲突则整批不登记）：

- 旧号必须已入库且 `status=active`（在架）；
- 新号不得已入库（`active` 或 `withdrawn` 都算已入库）；
- 任一标识不能在本批内重复出现（无论作为旧号还是新号），也不能与已登记对照
  中的任一旧号或新号重复；
- 登记只写 `identifier_mappings`，**不改**书目内容、书目状态和续采截止日。

```bash
# 整批登记（也接受裸数组，以及 old/new 字段别名）
curl -X POST http://localhost:8000/api/v1/identifier-mappings \
  -H 'Content-Type: application/json' \
  -d '{
        "mappings": [
          {"old_identifier": "oai:sample.lib:rec-001",
           "new_identifier": "oai:sample2.lib:rec-001"},
          {"old_identifier": "oai:sample.lib:rec-002",
           "new_identifier": "oai:sample2.lib:rec-002"}
        ]
      }'

# 查询已登记对照
curl http://localhost:8000/api/v1/identifier-mappings
```

有冲突时返回 `409`，`detail.conflicts` 逐条指出标识与原因（如
`old_not_found`、`old_not_active`、`new_already_in_catalog`、
`duplicate_in_batch`、`already_registered`），且本批一行都不写入：

```json
{
  "detail": {
    "message": "存在冲突，整批未登记",
    "conflicts": [
      {"identifier": "oai:sample.lib:rec-013",
       "reason": "old_not_active",
       "message": "旧号当前状态为 withdrawn，不在架"}
    ]
  }
}
```

### 样例源中的换系统轨迹

`sample-oai` 为 rec-001、rec-002 预置了 11 月的成员馆换系统记录：

| datestamp | 标识 | 含义 |
| --- | --- | --- |
| 2026-11-01 | `oai:sample.lib:rec-001/002` | 旧系统停用，旧号发删除标记 |
| 2026-11-02 | `oai:sample2.lib:rec-001/002` | 新系统上线，新号带更新后的书目 |
| 2026-11-15 | `oai:sample2.lib:rec-001/002` | 新号最终删除 |

在 Swagger（http://localhost:8000/docs）按以下顺序演示：

1. 先采入旧号（10 月）：

   ```bash
   curl -X POST http://localhost:8000/api/v1/harvest \
     -H 'Content-Type: application/json' \
     -d '{"from": "2026-10-01", "until": "2026-10-31"}'
   ```

2. 登记 rec-001/rec-002 的新旧对照（使用上面的登记请求）。

3. 分批补采 11-01~11-10，验证**旧号停用后书目仍在架**：

   ```bash
   curl -X POST http://localhost:8000/api/v1/harvest \
     -H 'Content-Type: application/json' \
     -d '{"from": "2026-10-31", "until": "2026-11-10"}'
   # fetched=4, ignored=2（两条旧号删除标记被忽略）,
   # updated=2（两条新号更新落到旧书目）, withdrawn=0
   curl 'http://localhost:8000/api/v1/catalog/oai:sample.lib:rec-001'   # active
   curl 'http://localhost:8000/api/v1/catalog/oai:sample2.lib:rec-001'  # 同样查到旧号那条
   ```

4. 继续补采 11-10~11-30，验证**新号被删除时才撤下原书目**：

   ```bash
   curl -X POST http://localhost:8000/api/v1/harvest \
     -H 'Content-Type: application/json' \
     -d '{"from": "2026-11-10", "until": "2026-11-30"}'
   # fetched=2, withdrawn=2
   curl 'http://localhost:8000/api/v1/catalog/oai:sample.lib:rec-001'   # withdrawn
   curl 'http://localhost:8000/api/v1/catalog/oai:sample2.lib:rec-001'  # withdrawn，仍是旧号那条
   ```

目录列表始终使用旧号且每个逻辑书目只有一行；`status=all` 中不会出现
`oai:sample2.lib:*` 这样的新号记录行。补采写库事务内读取对照，因此即使
翻页在登记开始之前已经启动，只要登记先于写库提交，本批仍按已登记对照处理。

## 令牌失效重抓的验证方法

样例源提供故障注入端点：

```bash
# 令下一次 resumptionToken 请求返回 badResumptionToken
curl -X POST http://localhost:8080/admin/fail-next
curl http://localhost:8080/admin/state

# 全区间 27 条共 3 页：第 1 页取到后续页令牌失效 -> 丢弃该页 -> 原区间重抓成功
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-27"}'
# 响应中 retries=1；运行历史与服务日志可查
curl -X POST http://localhost:8080/admin/reset   # 清除故障开关
```

若重抓过程中令牌**再次**失效（不可恢复），接口返回 `502`，本批所有书目变更回滚、
截止日保持不变，`harvest_runs` 中记录一条 `failed` 历史——任何半批数据都不会进库。

## 事务与一致性说明

- 采集过程先在内存中收集全部页面，**不向 SQLite 写任何数据**；
- 全部页成功后，在单个 `BEGIN IMMEDIATE` 事务内完成 upsert、撤下、截止日推进、运行历史更新；
- 标识对照在同一个写库事务内读取：即使翻页早于登记开始，只要登记已先提交，本批就按最新对照处理；
- 事务中任何一步失败都会 rollback，库状态回到采集前；
- 进程内互斥锁保证同一时刻只有一个采集任务，并发请求返回 `409`；
- 对照登记同样使用 `BEGIN IMMEDIATE`，校验冲突时不写入任何一行。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 终端 1：样例 OAI 源
python3 sample-oai/server.py

# 终端 2：API
APP_DB_PATH=./data/harvester.db OAI_ENDPOINT=http://127.0.0.1:8080/oai \
  uvicorn app.main:app --reload

# 测试
pytest
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OAI_ENDPOINT` | `http://oai-source:8080/oai` | OAI-PMH 端点 |
| `APP_DB_PATH` | `/data/harvester.db` | SQLite 数据库路径 |
| `PAGE_SIZE`（样例源） | `10` | 样例源每页记录数 |
| `TOKEN_TTL`（样例源） | `3600` | 样例源 resumptionToken 有效期秒数 |
