"""接口测试：RunService 用替身 Agent；HTTP 层直接按 ASGI 协议调用，不需要 httpx。未安装 FastAPI 时跳过 HTTP 部分。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest

from yuqing.agent.config import RunConfig
from yuqing.api.service import RunService
from yuqing.core.events import EventLog

try:
    from yuqing.api.app import create_app

    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


class FakeAgent:
    """写几条事件后结束；question 以「炸」开头时抛异常，以「慢」开头时等待放行。"""

    def __init__(self) -> None:
        self.events = EventLog(":memory:")
        self.release = threading.Event()

    def run(self, question: str, config: RunConfig, run_id: str | None = None) -> None:
        self.events.append(run_id, "run.started", {"question": question, "config": config.to_dict()})
        if question.startswith("炸"):
            raise RuntimeError("意外错误")
        if question.startswith("慢"):
            self.release.wait(5)
        self.events.append(run_id, "model.request", {"messages": 2, "tools": []}, call_id="model_1")
        self.events.append(run_id, "run.finished", {"status": "answered", "answer": {"text": "答", "structured": False}})


def wait_done(service: RunService, run_id: str, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.status(run_id)
        if status and status["done"]:
            return status
        time.sleep(0.02)
    raise AssertionError(f"{run_id} 没有在 {timeout} 秒内结束")


class RunServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = FakeAgent()
        self.service = RunService(self.agent)
        self.addCleanup(self.service.shutdown)
        self.addCleanup(self.agent.events.close)

    def test_submit_returns_immediately_and_status_follows_events(self) -> None:
        run_id = self.service.submit("慢慢来")
        first = self.service.status(run_id)
        self.assertIsNotNone(first)
        self.assertFalse(first["done"])  # 尚未结束
        self.agent.release.set()
        status = wait_done(self.service, run_id)
        self.assertEqual([e["type"] for e in status["events"]], ["run.started", "model.request", "run.finished"])
        self.assertEqual(status["result"]["status"], "answered")
        later = self.service.status(run_id, after=status["last_seq"])
        self.assertEqual((later["events"], later["done"], later["last_seq"]), ([], True, status["last_seq"]))

    def test_incremental_reads(self) -> None:
        run_id = self.service.submit("问题")
        status = wait_done(self.service, run_id)
        second_seq = status["events"][1]["seq"]
        rest = self.service.status(run_id, after=second_seq)
        self.assertEqual([e["type"] for e in rest["events"]], ["run.finished"])

    def test_unexpected_exception_becomes_run_failed(self) -> None:
        run_id = self.service.submit("炸了")
        status = wait_done(self.service, run_id)
        self.assertEqual(status["result"]["type"], "run.failed")
        self.assertIn("RuntimeError: 意外错误", status["result"]["error"])

    def test_validation_and_unknown_run(self) -> None:
        for question, config in (("   ", "v0.1"), ("x" * 501, "v0.1"), ("问题", "nope")):
            with self.subTest(question=question[:5], config=config), self.assertRaises(ValueError):
                self.service.submit(question, config)
        self.assertIsNone(self.service.status("0123456789ab"))


def asgi_request(app, method: str, path: str, body: dict | None = None, query: str = "") -> tuple[int, object]:
    async def call():
        payload = json.dumps(body).encode() if body is not None else b""
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
            "headers": [(b"host", b"test"), (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode())],
            "client": ("127.0.0.1", 50000), "server": ("test", 80),
        }
        messages, sent_body = [], False

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw.decode("utf-8")

    return asyncio.run(call())


@unittest.skipUnless(HAS_FASTAPI, "未安装 FastAPI")
class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = FakeAgent()
        self.service = RunService(self.agent)
        self.addCleanup(self.service.shutdown)
        self.addCleanup(self.agent.events.close)
        self.app = create_app(self.service)

    def test_create_and_poll_run(self) -> None:
        status, data = asgi_request(self.app, "POST", "/api/runs", {"question": "EVT-0021 怎么样", "config": "fixed"})
        self.assertEqual(status, 202)
        run_id = data["run_id"]
        wait_done(self.service, run_id)
        status, data = asgi_request(self.app, "GET", f"/api/runs/{run_id}", query="after=0")
        self.assertEqual(status, 200)
        self.assertTrue(data["done"])
        self.assertEqual(data["events"][0]["payload"]["config"]["name"], "fixed")

    def test_errors(self) -> None:
        self.assertEqual(asgi_request(self.app, "POST", "/api/runs", {"question": ""})[0], 422)
        self.assertEqual(asgi_request(self.app, "POST", "/api/runs", {"question": "x", "config": "nope"})[0], 400)
        self.assertEqual(asgi_request(self.app, "GET", "/api/runs/0123456789ab")[0], 404)
        self.assertEqual(asgi_request(self.app, "GET", "/api/runs/../etc")[0], 404)
        self.assertEqual(asgi_request(self.app, "GET", "/api/runs/0123456789ab", query="after=-1")[0], 422)

    def test_configs_and_index(self) -> None:
        status, data = asgi_request(self.app, "GET", "/api/configs")
        self.assertEqual(status, 200)
        self.assertEqual({c["name"] for c in data}, {"v0.1", "fixed"})
        status, page = asgi_request(self.app, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("舆情研判", page)
        self.assertIn("/api/runs", page)


if __name__ == "__main__":
    unittest.main()
