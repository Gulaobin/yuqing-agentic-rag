# 舆情研判 Agentic RAG

面向舆情事件研判的 Agentic RAG：单 agent 在研判文档与事件、帖子、账号、指标等业务数据之间多源调查，每条结论都能追溯到证据，按版本迭代并逐版评测。

> 开发中，当前目标版本 v0.1。

## 本地启动依赖服务

前置：Docker Desktop、Python 3.10+。

```bash
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
# 编辑 .env：至少改掉 POSTGRES_PASSWORD；本机 5432 被占用就改 POSTGRES_PORT
docker compose up -d        # 首次会拉镜像，并由 ollama-pull 下载嵌入模型（约 600 MB）
docker compose logs -f ollama-pull   # 看到 success 后 Ctrl+C
python scripts/check_services.py     # 三项都是 [OK] 即就绪
```

| 服务 | 地址 | 用途 |
|---|---|---|
| postgres（pgvector/pgvector:pg16） | 127.0.0.1:5432 | 知识库向量存储，首次启动自动启用 `vector` 扩展 |
| ollama（ollama/ollama:0.12.3） | 127.0.0.1:11434 | 嵌入模型 qwen3-embedding:0.6b，输出 1024 维 |

- 停止：`docker compose down`（数据卷保留）。
- 修改 `infra/postgres/init/` 下的脚本后需要重建数据卷才会生效：`docker compose down -v`（会删除库中全部数据）。

## 生成合成数据

业务数据全部由脚本生成，同一种子每次输出完全相同；事件、地名、机构名均为虚构。

```bash
python -m datagen                 # 默认种子 20261103，输出到 data/generated/
python -m datagen --seed 42 --out /tmp/yuqing
python -m unittest                # 确定性、完整性、埋入规律反查
```

输出两个文件：

- `yuqing.sqlite`：只读业务库，表结构见 [`datagen/schema.sql`](datagen/schema.sql)
  - `events` 30 个事件；`level`、`status` 按 manifest 中的规则由帖子数据派生
  - `accounts` 约 1550 个账号，编号随注册时间递增
  - `posts` 约 6500 条帖子；`parent_post_id` 非空为转发，被转发帖同事件、同平台、时间更早
  - `metrics_hourly` 每个事件 72 小时的帖数、转发数、去重账号数，由 `posts` 聚合
- `manifest.json`：种子、派生规则、库内容哈希，以及 8 条埋入规律（评测的标准答案）

| 编号 | 事件 | 规律 |
|---|---|---|
| P-001 | EVT-0007 | 40 个注册不足 7 天的账号在 5 分钟内转发同一条帖子，正文相同 |
| P-002 | EVT-0012 | 峰值在第 30 小时前，之后每小时帖数不超过峰值的 20% |
| P-003 | EVT-0003 | 小号首发，2 小时内被百万粉媒体号转发，其转发树占全部转发一半以上 |
| P-004 | EVT-0021 | 峰值在第 60 小时后，唯一处于 rising 的事件 |
| P-005 | EVT-0015 | 前 24 小时负面 >= 70%，第 36 小时起负面 <= 30% |
| P-006 | EVT-0009 | 前 12 小时以小红书为主，第 24 小时起以微博为主 |
| P-007 | EVT-0018 | 12 个新注册账号分散转发，不构成集中协同（防误判） |
| P-008 | EVT-0005 / 0011 / 0024 | 同一账号是这三个事件且仅这三个事件的首发账号 |

生成时会把每条规律写库后再用 SQL 反查一遍，任何一条不成立就报错退出；反查得到的具体编号和数值写在 manifest 的 `facts` 里。

## 知识库

`data/docs/` 下 40 篇 Markdown 文档（研判规范 10、方法说明 10、历史复盘 20），由模型辅助起草、人工审定。规范里的分级阈值、状态规则、新注册账号定义等数字与数据生成器的派生规则一致，`tests/test_corpus.py` 会检查。

```bash
pip install -r requirements.txt               # psycopg
python -m yuqing.knowledge.ingest --dry-run   # 只解析和分块：40 篇、144 块
python -m yuqing.knowledge.ingest             # 嵌入并在一个事务里重建 kb_documents / kb_chunks / kb_meta
python -m yuqing.knowledge.search "哪一级舆情需要当天上报？"
```

- 分块：按 Markdown 标题分节，节内按空行分段，超过 400 字依次按行、按句末标点、按字数切开；每块记录在原文中的起止位置，`chunk.text == body[start:end]`。
- 嵌入：Ollama `/api/embed`，文档侧输入「标题路径 + 正文」，查询侧按 Qwen3-Embedding 的建议加任务说明前缀。
- 存储：pgvector `vector(EMBED_DIM)` + HNSW 余弦索引；`kb_meta` 记录嵌入模型和维度，检索时不一致会拒绝执行。

### 测试

```bash
python -m unittest                            # 不需要服务的测试（需要服务的自动跳过）
$env:YUQING_INTEGRATION="1"; python -m unittest; Remove-Item Env:YUQING_INTEGRATION   # PowerShell，需先 docker compose up
```

集成测试会重建正式知识库，并检查 6 个语义问题能否在前 5 个片段里检索到对应文档；存储测试用独立的 `kb_test` schema，不影响正式数据。

## core 层

`yuqing/core/` 是 Agent loop 的基座，只用标准库：

| 模块 | 作用 |
|---|---|
| `llm.py` | OpenAI 兼容聊天客户端（非流式）：工具调用解析；429/5xx/网络错误按退避重试，4xx 不重试；报错不含 API Key |
| `messages.py` | 对话容器：每个 tool_call 必须有且只有一条对应的工具结果，否则追加下一条消息时就报错 |
| `tools.py` | 工具箱：JSON Schema 子集校验参数、填默认值；未知工具、非法 JSON、参数错误、工具异常都作为失败结果回填给模型 |
| `budget.py` | 模型与工具调用预算；`must_answer` 为真时下一次调用不再提供工具 |

在 `.env` 里填好 `MODEL_API_KEY`（以及需要时的 `MODEL_BASE_URL`、`MODEL_NAME`）后，用真实模型走一遍工具调用：

```bash
python -m yuqing.core.smoke
```

## Agent

同一个 Agent loop，用 `RunConfig` 区分配置（`yuqing/agent/config.py`）：

| 配置 | 工具 | 预算（模型 / 工具） | 说明 |
|---|---|---|---|
| `v0.1` | search_docs、read_doc、query_events、query_posts、get_metrics | 10 / 8 | 自主检索，模型以 `submit_answer` 提交结构化回答 |
| `fixed` | 无 | 1 / 0 | 代码按问题检索 5 个片段、模型生成一次，只用作评测对照 |

```bash
python -m datagen                                   # 业务库（只读 SQLite）
python -m yuqing.knowledge.ingest                   # 知识库
python -m yuqing.agent "EVT-0007 的热度现在怎么样？"   # 实时打印轨迹，最后输出回答
python -m yuqing.agent "哪一级舆情需要当天上报？" --config fixed
python -m yuqing.agent.replay                       # 从事件日志重放最近一次运行
```

- **结构化回答**：每条结论附证据编号，编号必须出现在本轮工具返回的结果里，否则把原因回填给模型重交；资料不足的写进 `unknowns`。
- **预算**：模型一次要的工具超过余额时只执行余额内的；只剩最后一次模型调用或工具用完时只提供 `submit_answer`，并提示立即提交。
- **业务数据工具**：只读连接；表名、列名、过滤条件写死在代码里，参数一律绑定。
- **事件日志**：`var/events.sqlite`，每次模型和工具调用前后各一条，工具调用通过 `parent_call_id` 指回发起它的模型调用。

## 接口与聊天页

```bash
pip install -r requirements.txt
python -m yuqing.api            # 浏览器打开 http://127.0.0.1:8000
```

| 接口 | 说明 |
|---|---|
| `POST /api/runs` | `{"question": "...", "config": "v0.1"}`，立即返回 202 和 `run_id`，Agent 在后台线程运行 |
| `GET /api/runs/{run_id}?after=<seq>` | 返回 seq 之后的新事件；`done` 为真时 `result` 是最终结果 |
| `GET /api/configs` | 可用配置 |

运行状态不放内存：是否结束看最后一条事件是不是 `run.finished` / `run.failed`，服务重启后已结束的运行照样能查。聊天页是单个 HTML 文件，每 0.6 秒轮询一次新事件。

## 评测

`eval/questions.json`：5 类各 3 题（知识单点、知识跨文档、实例单跳、实例多跳、应拒答）。规则判分：必需内容按正则匹配、引用的证据编号要包含指定文档或记录、应拒答题要明确表示无法确定。实例题的标准答案来自数据生成器埋入的规律，`tests/test_eval.py` 会与生成数据核对。

```bash
python -m yuqing.evaluation.run                  # v0.1 与 fixed 各跑 15 题，约 5 分钟
python -m yuqing.evaluation.run --only S1,M3     # 只跑部分题
```

结果写入 `eval/results/<label>.json`。按题型分开报告、不算混合总分：一个永远回答「无法确定」的模型也能拿满应拒答的 3 分。
