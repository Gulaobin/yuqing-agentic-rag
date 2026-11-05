"""运行配置：同一个 Agent loop，用配置区分可用工具、预算和检索模式。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


@dataclass(frozen=True)
class RunConfig:
    name: str
    tools: tuple[str, ...]  # 本次允许的工具（submit_answer 总是可用，不计入）
    max_model_calls: int
    max_tool_calls: int
    retrieval: Literal["agentic", "fixed"]  # fixed：代码检索一次、模型生成一次，只给评测当对照
    fixed_k: int = 5

    def to_dict(self) -> dict:
        return asdict(self)


PRESETS: dict[str, RunConfig] = {
    "v0.1": RunConfig(
        name="v0.1",
        tools=("search_docs", "read_doc", "query_events", "query_posts"),
        max_model_calls=10,
        max_tool_calls=8,
        retrieval="agentic",
    ),
    "fixed": RunConfig(name="fixed", tools=(), max_model_calls=1, max_tool_calls=0, retrieval="fixed"),
}
