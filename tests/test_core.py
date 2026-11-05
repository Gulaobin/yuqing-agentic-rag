"""core 层单元测试：消息容器、工具箱、预算、模型客户端（替身 HTTP 服务）。不需要外部服务。"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from yuqing.core.budget import Budget, BudgetExceeded
from yuqing.core.llm import ChatClient, ModelError
from yuqing.core.messages import Conversation, ConversationError, ToolCall
from yuqing.core.tools import Tool, ToolDefinitionError, Toolbox, validate

SEARCH = Tool(
    name="search_docs",
    description="检索文档",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 50},
            "k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            "doc_type": {"type": "string", "enum": ["规范", "方法", "复盘"]},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=lambda args: {"query": args["query"], "k": args["k"]},
)


def boom(_: dict) -> str:
    raise KeyError("missing")


class ConversationTest(unittest.TestCase):
    def test_tool_round_trip_produces_api_messages(self) -> None:
        c = Conversation("系统")
        c.add_user("问题")
        c.add_assistant(None, [ToolCall("call_1", "search_docs", '{"query":"x"}'), ToolCall("call_2", "read_doc", "{}")])
        c.add_tool_result("call_2", "结果二")  # 顺序可以与调用顺序不同
        c.add_tool_result("call_1", "结果一")
        c.add_assistant("回答")
        messages = c.to_api()
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool", "tool", "assistant"])
        self.assertEqual(messages[2]["tool_calls"][0]["function"]["name"], "search_docs")
        self.assertEqual(messages[3]["tool_call_id"], "call_2")

    def test_pending_tool_calls_block_next_message(self) -> None:
        c = Conversation("系统")
        c.add_user("问题")
        c.add_assistant(None, [ToolCall("call_1", "search_docs", "{}")])
        self.assertEqual(c.pending_tool_calls, ["call_1"])
        for action in (lambda: c.add_user("追问"), lambda: c.add_assistant("回答"), c.to_api):
            with self.assertRaises(ConversationError):
                action()

    def test_rejects_unknown_duplicate_and_empty(self) -> None:
        c = Conversation("系统")
        c.add_user("问题")
        with self.assertRaises(ConversationError):
            c.add_tool_result("call_x", "无对应调用")
        with self.assertRaises(ConversationError):
            c.add_assistant(None, [ToolCall("a", "t", "{}"), ToolCall("a", "t", "{}")])
        with self.assertRaises(ConversationError):
            c.add_assistant("")
        c.add_assistant(None, [ToolCall("a", "t", "{}")])
        c.add_tool_result("a", "ok")
        with self.assertRaises(ConversationError):
            c.add_tool_result("a", "重复回填")
        with self.assertRaises(ConversationError):
            c.add_assistant(None, [ToolCall("a", "t", "{}")])  # 复用旧 id

    def test_to_api_returns_copies(self) -> None:
        c = Conversation("系统")
        c.to_api()[0]["content"] = "被改了"
        self.assertEqual(c.to_api()[0]["content"], "系统")


class ToolboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.box = Toolbox([SEARCH, Tool("broken", "总是失败", {"type": "object"}, boom)])

    def test_valid_call_applies_defaults(self) -> None:
        result = self.box.run("search_docs", '{"query": "分级标准"}')
        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.content), {"query": "分级标准", "k": 5})

    def test_errors_become_observations(self) -> None:
        cases = {
            ("nope", "{}"): "unknown_tool",
            ("search_docs", "{query:"): "bad_json",
            ("search_docs", "[]"): "invalid_arguments",
            ("search_docs", "{}"): "invalid_arguments",
            ("search_docs", '{"query": "x", "k": 0}'): "invalid_arguments",
            ("search_docs", '{"query": "x", "k": true}'): "invalid_arguments",
            ("search_docs", '{"query": "x", "doc_type": "通知"}'): "invalid_arguments",
            ("search_docs", '{"query": "x", "limit": 3}'): "invalid_arguments",
            ("broken", "{}"): "handler_error",
        }
        for (name, args), kind in cases.items():
            with self.subTest(name=name, args=args):
                result = self.box.run(name, args)
                self.assertFalse(result.ok)
                self.assertEqual(result.error, kind)
                self.assertTrue(result.content.startswith("错误："))

    def test_error_messages_tell_model_how_to_fix(self) -> None:
        self.assertIn("缺少必填参数 query", self.box.run("search_docs", "{}").content)
        self.assertIn("可用参数：query, k, doc_type", self.box.run("search_docs", '{"query":"x","limit":1}').content)
        self.assertIn("可用工具：search_docs, broken", self.box.run("nope", "{}").content)

    def test_empty_arguments_mean_empty_object(self) -> None:
        self.assertEqual(self.box.run("search_docs", "").error, "invalid_arguments")  # 缺 query

    def test_definitions(self) -> None:
        definitions = self.box.definitions(["search_docs"])
        self.assertEqual(definitions[0]["function"]["name"], "search_docs")
        with self.assertRaises(ToolDefinitionError):
            self.box.definitions(["missing"])

    def test_rejects_bad_tool_definitions(self) -> None:
        for tools in ([SEARCH, SEARCH], [Tool("有中文", "", {"type": "object"}, str)], [Tool("t", "", {"type": "array"}, str)]):
            with self.subTest(tools=[t.name for t in tools]), self.assertRaises(ToolDefinitionError):
                Toolbox(tools)

    def test_nested_validation(self) -> None:
        schema = {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string", "pattern": "^EVT-\\d{4}$"}, "maxItems": 2}}}
        self.assertEqual(validate(schema, {"ids": ["EVT-0001"]}), [])
        errors = validate(schema, {"ids": ["EVT-1", "EVT-0002", "EVT-0003"]})
        self.assertEqual(len(errors), 2)  # 超过 2 项 + 第一项格式不对


class BudgetTest(unittest.TestCase):
    def test_model_and_tool_limits(self) -> None:
        b = Budget(max_model_calls=3, max_tool_calls=2)
        self.assertFalse(b.must_answer)
        b.spend_model()
        self.assertEqual(b.grant_tools(3), 2)  # 只批 2 个
        self.assertEqual(b.grant_tools(1), 0)
        self.assertTrue(b.must_answer)  # 工具用完
        b.spend_model()
        b.spend_model()
        with self.assertRaises(BudgetExceeded):
            b.spend_model()

    def test_last_model_call_must_answer(self) -> None:
        b = Budget(max_model_calls=2, max_tool_calls=5)
        b.spend_model()
        self.assertTrue(b.must_answer)

    def test_rejects_invalid_limits(self) -> None:
        with self.assertRaises(ValueError):
            Budget(max_model_calls=0, max_tool_calls=0)


class FakeModelServer:
    """按顺序返回预设响应的替身接口，并记录收到的请求。"""

    def __init__(self, responses: list[tuple[int, object]]):
        self.responses = list(responses)
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "body": json.loads(body)})
                status, payload = outer.responses.pop(0)
                data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def ok(message: dict, finish: str = "stop") -> tuple[int, dict]:
    return 200, {"model": "fake", "choices": [{"message": message, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 11, "completion_tokens": 3}}


class ChatClientTest(unittest.TestCase):
    def make(self, responses: list[tuple[int, object]], **kwargs) -> tuple[ChatClient, FakeModelServer]:
        server = FakeModelServer(responses)
        self.addCleanup(server.close)
        self.sleeps: list[float] = []
        client = ChatClient(server.url, "sk-secret", "fake-model", sleep=self.sleeps.append, **kwargs)
        return client, server

    def test_parses_tool_calls_and_sends_tools(self) -> None:
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "search_docs", "arguments": '{"query":"x"}'}},
            {"id": "call_2", "type": "function", "function": {"name": "read_doc", "arguments": {"doc_id": "STD-001"}}},
        ]}
        client, server = self.make([ok(message, "tool_calls")])
        result = client.complete([{"role": "user", "content": "hi"}], Toolbox([SEARCH]).definitions())
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual([c.name for c in result.tool_calls], ["search_docs", "read_doc"])
        self.assertEqual(json.loads(result.tool_calls[1].arguments), {"doc_id": "STD-001"})  # 对象被转成字符串
        self.assertEqual(result.usage.prompt_tokens, 11)
        request = server.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["auth"], "Bearer sk-secret")
        self.assertEqual(request["body"]["model"], "fake-model")
        self.assertEqual(request["body"]["tools"][0]["function"]["name"], "search_docs")
        self.assertFalse(request["body"]["stream"])

    def test_omits_tools_when_none(self) -> None:
        client, server = self.make([ok({"role": "assistant", "content": "答"})])
        self.assertEqual(client.complete([{"role": "user", "content": "hi"}]).content, "答")
        self.assertNotIn("tools", server.requests[0]["body"])

    def test_retries_429_and_5xx_with_backoff(self) -> None:
        client, server = self.make([(429, {"error": "rate"}), (503, "busy"), ok({"role": "assistant", "content": "答"})],
                                   max_retries=2, backoff=0.5)
        result = client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual((result.content, result.attempts), ("答", 3))
        self.assertEqual(self.sleeps, [0.5, 1.0])

    def test_gives_up_after_max_retries(self) -> None:
        client, _ = self.make([(500, "x"), (500, "x")], max_retries=1, backoff=0)
        with self.assertRaises(ModelError) as ctx:
            client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.status, 500)

    def test_does_not_retry_client_errors_and_hides_key(self) -> None:
        client, server = self.make([(401, {"error": {"message": "invalid api key"}})])
        with self.assertRaises(ModelError) as ctx:
            client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(len(server.requests), 1)
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertNotIn("sk-secret", str(ctx.exception))

    def test_malformed_responses(self) -> None:
        bad_call = {"role": "assistant", "content": None, "tool_calls": [{"type": "function", "function": {"name": "x"}}]}
        for payload in ({"choices": []}, ok(bad_call)[1]):
            with self.subTest(payload=payload):
                client, _ = self.make([(200, payload)])
                with self.assertRaises(ModelError):
                    client.complete([{"role": "user", "content": "hi"}])

    def test_unreachable_server_is_retryable_error(self) -> None:
        client = ChatClient("http://127.0.0.1:1", "k", "m", max_retries=1, backoff=0, sleep=lambda s: None, timeout=2)
        with self.assertRaises(ModelError) as ctx:
            client.complete([{"role": "user", "content": "hi"}])
        self.assertTrue(ctx.exception.retryable)

    def test_requires_api_key(self) -> None:
        with self.assertRaises(ModelError):
            ChatClient("http://x", "", "m")


if __name__ == "__main__":
    unittest.main()
