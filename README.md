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
