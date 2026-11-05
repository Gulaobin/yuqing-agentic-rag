"""Agent loop：检索触发、查询构造、终止判定交给模型，代码负责预算、执行、核对和记录。

一轮的流程：
1. 调模型（预算允许时提供全部工具 + submit_answer；只剩最后一次时只提供 submit_answer）；
2. 模型返回工具调用：在工具预算内执行，结果回填，结果里出现的编号记为「已见」；
3. 模型调用 submit_answer：核对证据编号都已见过，通过则结束，不通过则把原因回填让它改；
4. 模型直接回复文字（没按约定提交）：作为非结构化回答结束。
每次模型调用、工具调用前后都写事件日志。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable

from ..core.budget import Budget
from ..core.events import EventLog
from ..core.llm import ChatClient, Completion, ModelError
from ..core.messages import Conversation, ToolCall
from ..core.tools import Toolbox, ToolResult
from ..tools.docs import DocsBackend
from . import prompts
from .answer import SUBMIT_TOOL_DEFINITION, Answer, extract_ids, parse_submission
from .config import RunConfig

Observer = Callable[[str, dict[str, Any]], None]


@dataclass
class TurnResult:
    run_id: str
    status: str  # answered / unstructured / budget_exhausted / error
    answer: Answer
    model_calls: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    latency_s: float

    def to_dict(self) -> dict:
        data = asdict(self)
        data["answer"] = self.answer.to_dict()
        return data


class Agent:
    def __init__(
        self,
        client: ChatClient,
        toolbox: Toolbox,
        events: EventLog,
        docs: DocsBackend | None = None,
        observer: Observer | None = None,
    ):
        self.client = client
        self.toolbox = toolbox
        self.events = events
        self.docs = docs  # fixed 模式需要
        self.observer = observer

    def run(self, question: str, config: RunConfig, run_id: str | None = None) -> TurnResult:
        run = _Run(self, run_id or uuid.uuid4().hex[:12], config)
        run.emit("run.started", {"question": question, "config": config.to_dict()})
        try:
            if config.retrieval == "fixed":
                status, answer = run.fixed(question)
            else:
                status, answer = run.agentic(question)
        except ModelError as exc:
            run.emit("run.failed", {"error": str(exc), "status": exc.status})
            return run.result("error", Answer(f"模型调用失败：{exc}", False))
        result = run.result(status, answer)
        run.emit("run.finished", {"status": status, "answer": answer.to_dict(),
                                  "model_calls": result.model_calls, "tool_calls": result.tool_calls,
                                  "prompt_tokens": result.prompt_tokens,
                                  "completion_tokens": result.completion_tokens})
        return result


class _Run:
    def __init__(self, agent: Agent, run_id: str, config: RunConfig):
        self.agent = agent
        self.run_id = run_id
        self.config = config
        self.budget = Budget(config.max_model_calls, config.max_tool_calls)
        self.seen: set[str] = set()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.started = time.monotonic()

    def emit(self, type: str, payload: dict[str, Any], call_id: str | None = None, parent: str | None = None) -> None:
        self.agent.events.append(self.run_id, type, payload, call_id=call_id, parent_call_id=parent)
        if self.agent.observer:
            self.agent.observer(type, payload)

    def result(self, status: str, answer: Answer) -> TurnResult:
        return TurnResult(self.run_id, status, answer, self.budget.model_calls, self.budget.tool_calls,
                          self.prompt_tokens, self.completion_tokens, round(time.monotonic() - self.started, 2))

    def call_model(self, messages: list[dict], tools: list[dict] | None) -> tuple[str, Completion]:
        self.budget.spend_model()
        call_id = f"model_{self.budget.model_calls}"
        names = [t["function"]["name"] for t in tools or []]
        self.emit("model.request", {"messages": len(messages), "tools": names}, call_id)
        try:
            completion = self.agent.client.complete(messages, tools)
        except ModelError as exc:
            self.emit("model.error", {"error": str(exc), "status": exc.status}, call_id)
            raise
        self.prompt_tokens += completion.usage.prompt_tokens
        self.completion_tokens += completion.usage.completion_tokens
        self.emit("model.response", {
            "finish_reason": completion.finish_reason,
            "content": completion.content,
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in completion.tool_calls],
            "prompt_tokens": completion.usage.prompt_tokens,
            "completion_tokens": completion.usage.completion_tokens,
            "latency_s": round(completion.latency_s, 2),
            "attempts": completion.attempts,
        }, call_id)
        return call_id, completion

    def run_tool(self, call: ToolCall, model_call: str, allowed: bool, granted: bool) -> ToolResult:
        call_id = f"{model_call}/{call.id}"
        self.emit("tool.started", {"name": call.name, "arguments": call.arguments}, call_id, model_call)
        if not allowed:
            result = ToolResult(call.name, False, f"错误：本轮不能调用 {call.name}。请调用 submit_answer 提交结论。", "not_offered")
        elif not granted:
            result = Toolbox.budget_exhausted(call.name)
        else:
            result = self.agent.toolbox.run(call.name, call.arguments)
        if result.ok:
            self.seen |= extract_ids(result.content)
        self.emit("tool.result", {"name": call.name, "ok": result.ok, "error": result.error,
                                  "content": result.content}, call_id, model_call)
        return result

    # ---------- 自主检索 ----------

    def agentic(self, question: str) -> tuple[str, Answer]:
        conversation = Conversation(prompts.AGENT_SYSTEM)
        conversation.add_user(question)
        work_tools = self.agent.toolbox.definitions(list(self.config.tools))
        warned = False
        while self.budget.model_remaining > 0:
            if self.budget.must_answer and not warned:
                conversation.add_user(prompts.MUST_ANSWER)
                warned = True
            offered = [SUBMIT_TOOL_DEFINITION] if self.budget.must_answer else work_tools + [SUBMIT_TOOL_DEFINITION]
            offered_names = {t["function"]["name"] for t in offered}
            model_call, completion = self.call_model(conversation.to_api(), offered)
            if not completion.tool_calls:
                text = completion.content or ""
                return "unstructured", Answer(text, False, cited=sorted(extract_ids(text) & self.seen))
            conversation.add_assistant(completion.content, completion.tool_calls)

            work = [c for c in completion.tool_calls if c.name != "submit_answer"]
            submits = [c for c in completion.tool_calls if c.name == "submit_answer"]
            granted = self.budget.grant_tools(sum(1 for c in work if c.name in offered_names))
            for call in work:
                allowed = call.name in offered_names
                ok_to_run = allowed and granted > 0
                if ok_to_run:
                    granted -= 1
                result = self.run_tool(call, model_call, allowed, ok_to_run)
                conversation.add_tool_result(call.id, result.content)

            answer = None
            for i, call in enumerate(submits):
                call_id = f"{model_call}/{call.id}"
                if i > 0 or answer is not None:
                    conversation.add_tool_result(call.id, "已忽略：本轮只接受一次提交。")
                    continue
                answer, error = parse_submission(call.arguments, self.seen)
                self.emit("answer.submitted", {"accepted": error is None, "error": error,
                                               "arguments": call.arguments}, call_id, model_call)
                conversation.add_tool_result(call.id, error or "已提交。")
            if answer is not None:
                return "answered", answer
        return "budget_exhausted", Answer("调用次数已用完，未能提交结论。", False, unknowns=["预算内未完成调查"])

    # ---------- 固定检索（对照组） ----------

    def fixed(self, question: str) -> tuple[str, Answer]:
        if self.agent.docs is None:
            raise RuntimeError("fixed 模式需要文档检索后端")
        self.emit("retrieval.started", {"query": question, "k": self.config.fixed_k}, "retrieval")
        hits = self.agent.docs.search(question, self.config.fixed_k, None)
        for hit in hits:
            self.seen |= extract_ids(hit["chunk_id"])
        self.emit("retrieval.result", {"hits": [{k: h[k] for k in ("chunk_id", "similarity")} for h in hits]}, "retrieval")
        messages = [{"role": "system", "content": prompts.FIXED_SYSTEM},
                    {"role": "user", "content": prompts.fixed_user_message(question, hits)}]
        _, completion = self.call_model(messages, None)
        text = completion.content or ""
        return "unstructured", Answer(text, False, cited=sorted(extract_ids(text) & self.seen))
