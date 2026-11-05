"""用真实模型走一遍工具调用：python -m yuqing.core.smoke

请模型用 add 工具计算两个数之和：第一次调用应返回 tool_calls，执行后回填结果，第二次调用不带工具、直接回答。
同时用到 Conversation、Toolbox、Budget、ChatClient，用来确认接口地址、密钥和工具调用都可用。
"""

from __future__ import annotations

import sys

from ..settings import load_settings
from .budget import Budget
from .llm import ChatClient, ModelError
from .messages import Conversation
from .tools import Tool, Toolbox

ADD = Tool(
    name="add",
    description="计算两个整数之和",
    parameters={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
    handler=lambda args: {"sum": args["a"] + args["b"]},
)


def main() -> int:
    settings = load_settings()
    try:
        client = ChatClient(settings.model_base_url, settings.model_api_key, settings.model_name,
                            timeout=settings.model_timeout)
    except ModelError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    toolbox = Toolbox([ADD])
    budget = Budget(max_model_calls=2, max_tool_calls=1)
    conversation = Conversation("你是一个计算助手。需要计算时必须调用工具，不要心算。")
    conversation.add_user("请用工具计算 1234 加 4321 等于多少，然后用一句话告诉我结果。")

    try:
        budget.spend_model()
        first = client.complete(conversation.to_api(), toolbox.definitions())
        print(f"第 1 次：finish_reason={first.finish_reason}，工具调用 {len(first.tool_calls)} 个，"
              f"{first.latency_s:.1f} 秒，输入 {first.usage.prompt_tokens} token")
        if not first.tool_calls:
            print(f"[FAIL] 模型没有调用工具，直接回答：{first.content}", file=sys.stderr)
            return 1
        conversation.add_assistant(first.content, first.tool_calls)
        granted = budget.grant_tools(len(first.tool_calls))
        for i, call in enumerate(first.tool_calls):
            result = toolbox.run(call.name, call.arguments) if i < granted else Toolbox.budget_exhausted(call.name)
            print(f"  {call.name}({call.arguments}) -> {result.content}")
            conversation.add_tool_result(call.id, result.content)

        budget.spend_model()
        second = client.complete(conversation.to_api())  # 不再提供工具，要求直接回答
        print(f"第 2 次：finish_reason={second.finish_reason}，{second.latency_s:.1f} 秒")
        print(f"回答：{second.content}")
    except ModelError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    if "5555" not in (second.content or ""):
        print("[FAIL] 回答里没有正确结果 5555", file=sys.stderr)
        return 1
    print(f"[OK] {settings.model_name} 工具调用正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
