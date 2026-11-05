"""调用预算：限制一轮对话里的模型调用次数和工具调用次数。

用法约定（由 Agent loop 遵守）：
- 每次调用模型前 spend_model()；
- 模型一次要求 n 个工具调用时，grant_tools(n) 返回实际允许执行的个数，其余回填「预算已用尽」；
- must_answer 为真时，下一次模型调用不再提供工具，要求直接回答。
"""

from __future__ import annotations

from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    max_model_calls: int
    max_tool_calls: int
    model_calls: int = 0
    tool_calls: int = 0

    def __post_init__(self) -> None:
        if self.max_model_calls < 1 or self.max_tool_calls < 0:
            raise ValueError("模型调用上限至少为 1，工具调用上限不能为负")

    @property
    def model_remaining(self) -> int:
        return self.max_model_calls - self.model_calls

    @property
    def tool_remaining(self) -> int:
        return self.max_tool_calls - self.tool_calls

    @property
    def must_answer(self) -> bool:
        """只剩最后一次模型调用，或工具额度已用完：下一次必须直接回答。"""
        return self.model_remaining <= 1 or self.tool_remaining <= 0

    def spend_model(self) -> None:
        if self.model_remaining <= 0:
            raise BudgetExceeded(f"模型调用已达上限 {self.max_model_calls} 次")
        self.model_calls += 1

    def grant_tools(self, requested: int) -> int:
        granted = max(0, min(requested, self.tool_remaining))
        self.tool_calls += granted
        return granted
