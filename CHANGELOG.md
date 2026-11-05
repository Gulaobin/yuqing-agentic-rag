# 更新记录

## v0.1.0（未发布）

第一个端到端可用版本：提问 → 自主查询文档与业务数据 → 带证据编号的结构化回答 → 事件日志可回放。

- 环境：Docker Compose 起 PostgreSQL + pgvector 与 Ollama（qwen3-embedding:0.6b，1024 维）
- 数据：固定种子生成 30 个事件、约 6500 条帖子，埋入 8 条已知规律作为评测标准答案
- 知识库：40 篇研判文档，按标题分节分块，pgvector + HNSW 余弦检索
- core：OpenAI 兼容模型客户端、tool_call 配对检查、工具参数校验、调用预算
- Agent：5 个工具（search_docs、read_doc、query_events、query_posts、get_metrics），submit_answer 证据编号核对，fixed 对照模式
- 接口与聊天页：FastAPI 后台运行 + 轮询事件日志
- 评测：15 题规则判分，v0.1 与 fixed 对照

改动依据：真实模型回答「EVT-0021 的热度还在上升吗」时，加入 get_metrics 前工具调用 8 次（额度用完、未拿到峰值小时）、输入 19,928 token；加入后工具调用 6 次、输入 16,884 token，并完整给出 STD-004 的三条判定依据。
