"""把事件格式化成一行行运行轨迹，命令行实时输出和事后重放共用。"""

from __future__ import annotations

from typing import Any


def short(text: str | None, limit: int = 120) -> str:
    flat = (text or "").replace("\n", " ")
    return flat if len(flat) <= limit else flat[:limit] + "…"


def format_event(type: str, payload: dict[str, Any]) -> str | None:
    if type == "run.started":
        return f"问题：{payload['question']}（配置 {payload['config']['name']}）"
    if type == "model.request":
        tools = "、".join(payload["tools"]) or "无工具"
        return f"→ 调用模型（可用：{tools}）"
    if type == "model.response":
        calls = payload["tool_calls"]
        what = f"{len(calls)} 个工具调用" if calls else f"直接回复：{short(payload['content'], 60)}"
        return (f"← 模型 {payload['latency_s']}s，输入 {payload['prompt_tokens']} token，{what}")
    if type == "model.error":
        return f"✗ 模型调用失败：{payload['error']}"
    if type == "tool.started":
        return f"  · {payload['name']}({short(payload['arguments'], 100)})"
    if type == "tool.result":
        mark = "✓" if payload["ok"] else "✗"
        return f"    {mark} {short(payload['content'])}"
    if type == "retrieval.result":
        return "  · 固定检索：" + "、".join(h["chunk_id"] for h in payload["hits"])
    if type == "answer.submitted":
        return "  · submit_answer：" + ("已接受" if payload["accepted"] else short(payload["error"]))
    if type == "run.finished":
        return (f"结束：{payload['status']}，模型 {payload['model_calls']} 次，工具 {payload['tool_calls']} 次，"
                f"输入 {payload['prompt_tokens']} / 输出 {payload['completion_tokens']} token")
    if type == "run.failed":
        return f"失败：{payload['error']}"
    return None
