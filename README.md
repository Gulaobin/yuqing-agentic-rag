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
