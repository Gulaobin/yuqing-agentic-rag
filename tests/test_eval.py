"""评测集测试：题目格式、实例题标准答案与生成数据一致、判分规则。"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from datagen import config as C
from datagen.generate import generate
from yuqing.evaluation.grading import TYPES, Question, grade, load_questions
from yuqing.tools.data import BusinessDB


def matches_all(question: Question, text: str) -> bool:
    return grade(question, text, question.evidence_any[:1]).passed


class QuestionSetTest(unittest.TestCase):
    def test_five_types_three_each(self) -> None:
        questions = load_questions()
        self.assertEqual(len(questions), 15)
        for t in TYPES:
            self.assertEqual(sum(q.type == t for q in questions), 3, t)

    def test_patterns_compile_and_refusals_have_no_evidence(self) -> None:
        for q in load_questions():
            for group in q.must:
                for pattern in group:
                    re.compile(pattern)
            for pattern in q.must_not:
                re.compile(pattern)
            if q.refuse:
                self.assertEqual(q.evidence_any, [], q.id)
            else:
                self.assertTrue(q.must and q.evidence_any, q.id)


class InstanceAnswersMatchDataTest(unittest.TestCase):
    """实例类题的标准答案必须与 datagen 默认种子生成的数据一致；改了生成器这里会报出来。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        result = generate(C.DEFAULT_SEED, Path(cls.tmp.name))
        cls.db = BusinessDB(result.db_path)
        cls.facts = {p["plant_id"]: p["facts"] for p in result.manifest["plants"]}
        cls.q = {q.id: q for q in load_questions()}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_s1_peak(self) -> None:
        summary = self.db.get_metrics({"event_id": "EVT-0012", "hour_from": 0, "hour_to": 71})["summary"]
        text = f"EVT-0012 峰值在第 {summary['peak_hour']} 小时，有 {summary['peak_posts']} 条帖子"
        self.assertTrue(matches_all(self.q["S1"], text), text)

    def test_s2_rising(self) -> None:
        rising = self.facts["P-004"]["rising_events"]
        self.assertEqual(rising, ["EVT-0021"])
        self.assertTrue(matches_all(self.q["S2"], "处于发酵中的事件只有 EVT-0021"))

    def test_s3_and_m1_first_poster_and_amplifier(self) -> None:
        f = self.facts["P-003"]
        self.assertTrue(matches_all(self.q["S3"], f"首帖由 {f['first_account_id']} 发布"))
        self.assertTrue(matches_all(self.q["M1"], f"{f['first_account_id']} 首发，{f['amplifier_account_id']} 转发放大"))

    def test_m2_sentiment_shares(self) -> None:
        f = self.facts["P-005"]
        text = (f"前 24 小时负面占比 {f['negative_share_before_24h'] * 100:.1f}%，"
                f"第 36 小时以后 {f['negative_share_from_36h'] * 100:.1f}%")
        self.assertTrue(matches_all(self.q["M2"], text), text)

    def test_m3_coordinated(self) -> None:
        f = self.facts["P-001"]
        text = f"有，{f['new_account_count']} 个新注册账号在 5 分钟内转发了 {f['target_post_id']}"
        self.assertTrue(matches_all(self.q["M3"], text), text)


class GradingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.q = {q.id: q for q in load_questions()}

    def test_spaces_and_alternatives(self) -> None:
        self.assertTrue(grade(self.q["K2"], "发帖时注册不足 7 天", ["MTH-003#01"]).passed)
        self.assertTrue(grade(self.q["K2"], "不足七天", ["MTH-003"]).passed)
        self.assertFalse(grade(self.q["K2"], "不足 17 天", ["MTH-003"]).passed)  # 17 天不能算 7 天

    def test_evidence_required(self) -> None:
        g = grade(self.q["K1"], "二级事件当天上报", [])
        self.assertFalse(g.passed)
        self.assertFalse(g.evidence_ok)
        self.assertTrue(grade(self.q["K1"], "二级事件当天上报", ["STD-002#01"]).passed)

    def test_m2_does_not_confuse_78_with_8(self) -> None:
        self.assertFalse(grade(self.q["M2"], "前 24 小时负面 78%", ["EVT-0015"]).passed)
        self.assertTrue(grade(self.q["M2"], "前 24 小时负面 78.2%，36 小时后 8.4%", ["EVT-0015"]).passed)

    def test_refusals(self) -> None:
        self.assertTrue(grade(self.q["R1"], "现有资料无法确定责任单位", []).passed)
        self.assertFalse(grade(self.q["R1"], "责任单位是禾沐科技", []).passed)
        self.assertFalse(grade(self.q["R3"], "裁了 300 名员工，具体无法确定", []).passed)  # 编造数字

    def test_answer_text_alone_cannot_pass_without_must(self) -> None:
        self.assertFalse(grade(self.q["M1"], "首发账号是 A01104", ["A00166"]).passed)


if __name__ == "__main__":
    unittest.main()
