"""对话消息容器：保证每个工具调用都有且只有一条对应的工具结果。

OpenAI 兼容接口要求：assistant 消息里的每个 tool_call，在下一条 assistant 或 user 消息之前，
都要有一条 role=tool、tool_call_id 相同的消息。缺一条或多一条，接口都会报 400。
这里在追加消息时就检查，而不是等发请求时才失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ConversationError(ValueError):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # 模型给出的原始 JSON 字符串，解析和校验交给 Toolbox

    def to_api(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": self.arguments}}


class Conversation:
    def __init__(self, system: str):
        self._messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self._pending: dict[str, str] = {}  # 尚未回填结果的 tool_call_id → 工具名
        self._answered: set[str] = set()  # 上一条助手消息里已回填的 id
        # 接口只要求同一条助手消息内配对；有些兼容实现每轮都从 call_0 编号，所以不同轮次之间允许 id 重复。

    @property
    def pending_tool_calls(self) -> list[str]:
        return list(self._pending)

    def _require_no_pending(self, action: str) -> None:
        if self._pending:
            raise ConversationError(f"{action}之前必须先回填工具结果：{', '.join(self._pending)}")

    def add_user(self, text: str) -> None:
        self._require_no_pending("追加用户消息")
        self._messages.append({"role": "user", "content": text})

    def add_assistant(self, content: str | None, tool_calls: list[ToolCall] | tuple[ToolCall, ...] = ()) -> None:
        self._require_no_pending("追加助手消息")
        if not content and not tool_calls:
            raise ConversationError("助手消息既没有内容也没有工具调用")
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            ids = [c.id for c in tool_calls]
            if len(set(ids)) != len(ids):
                raise ConversationError(f"同一条助手消息里 tool_call_id 重复：{ids}")
            message["tool_calls"] = [c.to_api() for c in tool_calls]
            self._pending = {c.id: c.name for c in tool_calls}
            self._answered = set()
        self._messages.append(message)

    def add_tool_result(self, call_id: str, content: str) -> None:
        if call_id not in self._pending:
            state = "已经回填过" if call_id in self._answered else "不存在于上一条助手消息"
            raise ConversationError(f"工具结果对应的 tool_call_id {call_id} {state}")
        del self._pending[call_id]
        self._answered.add(call_id)
        self._messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

    def to_api(self) -> list[dict[str, Any]]:
        """返回可直接发给接口的消息列表（副本）。有未回填的工具调用时拒绝。"""
        self._require_no_pending("发送请求")
        return [dict(m) for m in self._messages]

    def __len__(self) -> int:
        return len(self._messages)
