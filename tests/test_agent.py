"""Agent loop、数据工具与事件日志的测试。模型用脚本化的替身 HTTP 服务，业务数据用生成器现生成。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from datagen import config as C
from datagen.generate import generate
from yuqing.agent import prompts
from yuqing.agent.answer import extract_ids, parse_submission
from yuqing.agent.config import PRESETS, RunConfig
from yuqing.agent.loop import Agent
from yuqing.agent.replay import replay
from yuqing.core.events import EventLog
from yuqing.core.llm import ChatClient
from yuqing.core.tools import Toolbox
from yuqing.tools.data import BusinessDB, data_tools
from yuqing.tools.docs import docs_tools

from .test_core import FakeModelServer, ok


class FakeDocs:
    """固定返回两条片段的文档后端。"""

    HITS = [
        {"chunk_id": "STD-004#01", "doc_id": "STD-004", "title": "舆情事件状态定义", "doc_type": "规范",
         "section": "舆情事件状态定义 > 三种状态", "similarity": 0.8, "content": "峰值小时不早于第 48 小时为发酵中"},
        {"chunk_id": "STD-002#01", "doc_id": "STD-002", "title": "舆情上报流程", "doc_type": "规范",
         "section": "舆情上报流程 > 上报时限", "similarity": 0.7, "content": "二级：当天 24 时前提交书面报告"},
    ]

    def search(self, query: str, k: int, doc_type: str | None) -> list[dict]:
        return self.HITS[:k]

    def read(self, doc_id: str) -> dict | None:
        if doc_id != "STD-004":
            return None
        return {"doc_id": doc_id, "title": "舆情事件状态定义", "doc_type": "规范",
                "sections": [{"section": "三种状态", "text": "……"}]}


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def calls(*items: dict) -> tuple[int, dict]:
    return ok({"role": "assistant", "content": None, "tool_calls": list(items)}, "tool_calls")


def submit(call_id: str, conclusions: list[dict], unknowns: list[str] | None = None) -> dict:
    args = {"conclusions": conclusions}
    if unknowns is not None:
        args["unknowns"] = unknowns
    return tool_call(call_id, "submit_answer", args)


class AgentTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = generate(C.DEFAULT_SEED, Path(cls.tmp.name)).db_path
        cls.db = BusinessDB(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def make_agent(self, responses: list) -> tuple[Agent, FakeModelServer, EventLog]:
        server = FakeModelServer(responses)
        self.addCleanup(server.close)
        client = ChatClient(server.url, "sk-test", "fake", max_retries=0)
        docs = FakeDocs()
        events = EventLog(":memory:")
        self.addCleanup(events.close)
        agent = Agent(client, Toolbox(docs_tools(docs) + data_tools(self.db)), events, docs=docs)
        return agent, server, events


class AgentLoopTest(AgentTestBase):
    def test_multi_step_investigation_then_submit(self) -> None:
        agent, server, events = self.make_agent([
            calls(tool_call("c1", "query_events", {"status": "rising"})),
            calls(tool_call("c1", "query_posts", {"event_id": "EVT-0021", "hour_from": 60, "limit": 3}),
                  tool_call("c2", "search_docs", {"query": "发酵中怎么判定"})),
            calls(submit("c1", [{"text": "EVT-0021 处于发酵中", "evidence": ["EVT-0021", "STD-004"]}])),
        ])
        result = agent.run("哪个事件还在发酵？", PRESETS["v0.1"])
        self.assertEqual(result.status, "answered")
        self.assertTrue(result.answer.structured)
        self.assertEqual(result.answer.cited, ["EVT-0021", "STD-004"])
        self.assertEqual((result.model_calls, result.tool_calls), (3, 3))
        self.assertEqual(result.prompt_tokens, 33)
        # 第二次请求带着第一次的工具结果：真实查到了 EVT-0021
        second = server.requests[1]["body"]["messages"]
        self.assertIn("EVT-0021", second[-1]["content"])
        self.assertEqual(second[-1]["role"], "tool")
        types = [e.type for e in events.read(result.run_id)]
        self.assertEqual(types[0], "run.started")
        self.assertEqual(types[-1], "run.finished")
        self.assertEqual(types.count("model.request"), 3)
        self.assertEqual(types.count("tool.result"), 3)
        tool_events = [e for e in events.read(result.run_id) if e.type == "tool.started"]
        self.assertEqual([e.parent_call_id for e in tool_events], ["model_1", "model_2", "model_2"])
        self.assertEqual(len({e.call_id for e in tool_events}), 3)  # 各轮复用 c1 也不会冲突

    def test_unseen_evidence_is_rejected_then_corrected(self) -> None:
        agent, server, events = self.make_agent([
            calls(tool_call("c1", "query_events", {"event_id": "EVT-0007"})),
            calls(submit("c2", [{"text": "首发帖是 P0007-9999", "evidence": ["EVT-0007", "P0007-9999"]}])),
            calls(submit("c3", [{"text": "EVT-0007 为三级事件", "evidence": ["EVT-0007"]}])),
        ])
        result = agent.run("EVT-0007 什么情况", PRESETS["v0.1"])
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.answer.cited, ["EVT-0007"])
        rejection = server.requests[2]["body"]["messages"][-1]
        self.assertEqual(rejection["role"], "tool")
        self.assertIn("P0007-9999", rejection["content"])
        accepted = [e.payload["accepted"] for e in events.read(result.run_id) if e.type == "answer.submitted"]
        self.assertEqual(accepted, [False, True])

    def test_tool_budget_partial_grant_then_must_answer(self) -> None:
        config = RunConfig("tiny", ("query_events",), max_model_calls=3, max_tool_calls=1, retrieval="agentic")
        agent, server, _ = self.make_agent([
            calls(tool_call("a", "query_events", {"event_id": "EVT-0001"}),
                  tool_call("b", "query_events", {"event_id": "EVT-0002"})),
            calls(submit("c", [], ["工具额度不足，未查 EVT-0002"])),
        ])
        result = agent.run("比较两个事件", config)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.tool_calls, 1)
        messages = server.requests[1]["body"]["messages"]
        tool_messages = [m for m in messages if m["role"] == "tool"]
        self.assertIn("EVT-0001", tool_messages[0]["content"])
        self.assertIn("已用完", tool_messages[1]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": prompts.MUST_ANSWER})
        offered = [t["function"]["name"] for t in server.requests[1]["body"]["tools"]]
        self.assertEqual(offered, ["submit_answer"])

    def test_tool_not_offered_when_must_answer(self) -> None:
        config = RunConfig("one", ("query_events",), max_model_calls=3, max_tool_calls=0, retrieval="agentic")
        agent, server, _ = self.make_agent([
            calls(tool_call("a", "query_posts", {"event_id": "EVT-0001"})),
            calls(submit("b", [], ["没有可用工具"])),
        ])
        result = agent.run("x", config)
        self.assertEqual(result.status, "answered")
        self.assertIn("本轮不能调用 query_posts", server.requests[1]["body"]["messages"][-1]["content"])
        self.assertEqual(result.tool_calls, 0)

    def test_plain_text_reply_is_unstructured(self) -> None:
        agent, _, _ = self.make_agent([ok({"role": "assistant", "content": "我觉得是 EVT-0007"})])
        result = agent.run("x", PRESETS["v0.1"])
        self.assertEqual(result.status, "unstructured")
        self.assertFalse(result.answer.structured)
        self.assertEqual(result.answer.cited, [])  # 没查过，不算引用

    def test_budget_exhausted_without_submit(self) -> None:
        config = RunConfig("short", ("query_events",), max_model_calls=2, max_tool_calls=5, retrieval="agentic")
        bad = submit("s", [{"text": "x", "evidence": ["EVT-0009"]}])
        agent, _, _ = self.make_agent([calls(bad), calls(bad)])
        self.assertEqual(agent.run("x", config).status, "budget_exhausted")

    def test_model_error_marks_run_failed(self) -> None:
        agent, _, events = self.make_agent([(401, {"error": "bad key"})])
        result = agent.run("x", PRESETS["v0.1"])
        self.assertEqual(result.status, "error")
        self.assertEqual(events.read(result.run_id)[-1].type, "run.failed")

    def test_fixed_mode_single_call_without_tools(self) -> None:
        agent, server, _ = self.make_agent([ok({"role": "assistant", "content": "二级事件当天上报 [STD-002#01]"})])
        result = agent.run("哪一级当天上报？", PRESETS["fixed"])
        self.assertEqual((result.status, result.model_calls, result.tool_calls), ("unstructured", 1, 0))
        body = server.requests[0]["body"]
        self.assertNotIn("tools", body)
        self.assertEqual(body["messages"][0]["content"], prompts.FIXED_SYSTEM)
        self.assertIn("[STD-004#01]", body["messages"][1]["content"])
        self.assertEqual(result.answer.cited, ["STD-002", "STD-002#01"])

    def test_replay_reads_back_the_run(self) -> None:
        agent, _, events = self.make_agent([
            calls(tool_call("c1", "query_events", {"event_id": "EVT-0003"})),
            calls(submit("c2", [{"text": "EVT-0003 存在", "evidence": ["EVT-0003"]}])),
        ])
        result = agent.run("EVT-0003", PRESETS["v0.1"])
        lines = replay(events, result.run_id)
        self.assertTrue(lines[0].strip().endswith("问题：EVT-0003（配置 v0.1）"))
        self.assertTrue(any("query_events" in line for line in lines))
        self.assertIn("结束：answered", lines[-1])


class SubmissionTest(unittest.TestCase):
    def test_extract_ids_includes_doc_of_chunk(self) -> None:
        self.assertEqual(extract_ids("见 STD-002#01 与 EVT-0007、P0007-0024、A00166"),
                         {"STD-002#01", "STD-002", "EVT-0007", "P0007-0024", "A00166"})

    def test_submission_validation(self) -> None:
        seen = {"EVT-0007"}
        bad = {
            "非 JSON": "{",
            "编号格式": json.dumps({"conclusions": [{"text": "x", "evidence": ["事件七"]}]}),
            "缺证据": json.dumps({"conclusions": [{"text": "x", "evidence": []}]}),
            "都为空": json.dumps({"conclusions": [], "unknowns": []}),
            "多余字段": json.dumps({"conclusions": [], "unknowns": ["x"], "level": 3}),
        }
        for name, arguments in bad.items():
            with self.subTest(name):
                answer, error = parse_submission(arguments, seen)
                self.assertIsNone(answer)
                self.assertTrue(error.startswith("错误："))
        answer, error = parse_submission(json.dumps({"conclusions": [], "unknowns": ["资料不足"]}), seen)
        self.assertIsNone(error)
        self.assertIn("无法确定", answer.text)


class DataToolsTest(AgentTestBase):
    def run_tool(self, name: str, args: dict) -> dict:
        result = Toolbox(data_tools(self.db)).run(name, json.dumps(args))
        self.assertTrue(result.ok, result.content)
        return json.loads(result.content)

    def test_hour_range_matches_hourly_metrics(self) -> None:
        with self.db.connect() as conn:
            expected = conn.execute(
                "SELECT SUM(post_count) FROM metrics_hourly WHERE event_id = 'EVT-0012' AND hour BETWEEN 30 AND 71"
            ).fetchone()[0]
        data = self.run_tool("query_posts", {"event_id": "EVT-0012", "hour_from": 30, "hour_to": 71, "limit": 50})
        self.assertEqual(data["total"], expected)
        self.assertTrue(all(30 <= p["hour"] <= 71 for p in data["posts"]))

    def test_filters_and_ordering(self) -> None:
        first = self.run_tool("query_posts", {"event_id": "EVT-0003", "limit": 1})["posts"][0]
        self.assertEqual((first["post_id"], first["hour"], first["parent_post_id"]), ("P0003-0001", 0, None))
        children = self.run_tool("query_posts", {"event_id": "EVT-0003", "reposts_of": "P0003-0001", "limit": 50})
        self.assertTrue(all(p["parent_post_id"] == "P0003-0001" for p in children["posts"]))
        latest = self.run_tool("query_posts", {"event_id": "EVT-0003", "order": "time_desc", "limit": 2})["posts"]
        self.assertGreaterEqual(latest[0]["created_at"], latest[1]["created_at"])
        originals = self.run_tool("query_posts", {"event_id": "EVT-0003", "is_repost": False, "limit": 50})
        self.assertTrue(all(p["parent_post_id"] is None for p in originals["posts"]))

    def test_keyword_wildcards_are_literal(self) -> None:
        self.assertEqual(self.run_tool("query_posts", {"event_id": "EVT-0007", "keyword": "%"})["total"], 0)
        self.assertGreater(self.run_tool("query_posts", {"event_id": "EVT-0007", "keyword": "裁员"})["total"], 0)

    def test_query_events_filters(self) -> None:
        self.assertEqual(self.run_tool("query_events", {})["total"], 30)
        rising = self.run_tool("query_events", {"status": "rising"})["events"]
        self.assertEqual([e["event_id"] for e in rising], ["EVT-0021"])

    def test_connection_is_read_only(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            with self.db.connect() as conn:
                conn.execute("DELETE FROM posts")

    def test_get_metrics_summary_matches_generator_rules(self) -> None:
        """汇总按完整 72 小时计算，峰值与状态规则和数据生成器一致。"""
        for event_id in ("EVT-0012", "EVT-0021", "EVT-0001"):
            with self.subTest(event_id):
                data = self.run_tool("get_metrics", {"event_id": event_id})
                counts = [row[1] for row in data["hourly"]]
                self.assertEqual(len(counts), 72)
                summary = data["summary"]
                self.assertEqual(summary["total_posts_72h"], sum(counts))
                self.assertEqual(C.status_for(counts), data["status"])
                self.assertEqual(C.level_for(sum(counts)), data["level"])
                self.assertEqual(counts[summary["peak_hour"]], max(counts))
                self.assertEqual(summary["peak_hour"], counts.index(max(counts)))  # 并列取最早
        rising = self.run_tool("get_metrics", {"event_id": "EVT-0021"})["summary"]
        self.assertGreaterEqual(rising["peak_hour"], C.RISING_PEAK_HOUR)

    def test_get_metrics_range_filters_rows_not_summary(self) -> None:
        full = self.run_tool("get_metrics", {"event_id": "EVT-0007"})
        part = self.run_tool("get_metrics", {"event_id": "EVT-0007", "hour_from": 8, "hour_to": 14})
        self.assertEqual([row[0] for row in part["hourly"]], list(range(8, 15)))
        self.assertEqual(part["summary"], full["summary"])
        result = Toolbox(data_tools(self.db)).run("get_metrics", '{"event_id": "EVT-0007", "hour_from": 9, "hour_to": 3}')
        self.assertEqual(result.error, "handler_error")

    def test_query_posts_truncates_long_text(self) -> None:
        posts = self.run_tool("query_posts", {"event_id": "EVT-0003", "limit": 50})["posts"]
        self.assertTrue(all(len(p["text"]) <= 81 for p in posts))

    def test_query_accounts(self) -> None:
        data = self.run_tool("query_accounts", {"account_ids": ["A01104", "A00166", "A99999", "A01104"]})
        self.assertEqual([a["account_id"] for a in data["accounts"]], ["A00166", "A01104"])
        self.assertEqual(data["not_found"], ["A99999"])
        media = data["accounts"][0]
        self.assertEqual((media["verified"], media["followers"]), (1, 2380000))
        bad = Toolbox(data_tools(self.db)).run("query_accounts", json.dumps({"account_ids": ["A1"] * 51}))
        self.assertEqual(bad.error, "invalid_arguments")

    def test_coordinated_group_is_findable_with_tools(self) -> None:
        """M3 的调查路径：被转发帖下的转发 → 账号注册时间 → 判定新号数量。"""
        target = "P0007-0022"
        posts = self.run_tool("query_posts", {"event_id": "EVT-0007", "reposts_of": target, "limit": 50})["posts"]
        accounts = self.run_tool("query_accounts", {"account_ids": [p["account_id"] for p in posts]})["accounts"]
        created = {a["account_id"]: a["created_at"] for a in accounts}
        from datetime import datetime, timedelta
        fmt = "%Y-%m-%d %H:%M:%S"
        new = {p["account_id"] for p in posts
               if datetime.strptime(p["created_at"], fmt) - datetime.strptime(created[p["account_id"]], fmt) < timedelta(days=7)}
        self.assertEqual(len(new), 40)

    def test_unknown_event_is_reported(self) -> None:
        result = Toolbox(data_tools(self.db)).run("query_posts", '{"event_id": "EVT-0999"}')
        self.assertEqual(result.error, "handler_error")
        self.assertIn("EVT-0999 不存在", result.content)


if __name__ == "__main__":
    unittest.main()
