"""知识库语料的一致性：篇数、交叉引用，以及规范里的数字与数据生成规则一致。"""

from __future__ import annotations

import re
import unittest

from datagen import config as C
from yuqing.knowledge.documents import load_documents
from yuqing.settings import ROOT


class CorpusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = {d.doc_id: d for d in load_documents(ROOT / "data" / "docs")}

    def body(self, doc_id: str) -> str:
        return self.docs[doc_id].body

    def test_counts_by_type(self) -> None:
        counts: dict[str, int] = {}
        for doc in self.docs.values():
            counts[doc.doc_type] = counts.get(doc.doc_type, 0) + 1
        self.assertEqual(counts, {"规范": 10, "方法": 10, "复盘": 20})

    def test_cross_references_exist(self) -> None:
        for doc in self.docs.values():
            for ref in re.findall(r"\b(?:STD|MTH|CASE)-\d{3}\b", doc.body):
                self.assertIn(ref, self.docs, f"{doc.doc_id} 引用了不存在的 {ref}")

    def test_level_thresholds_match_generator(self) -> None:
        text = self.body("STD-001")
        for threshold, _ in C.LEVEL_THRESHOLDS:
            self.assertIn(f"不少于 {threshold} 条", text)
        self.assertIn("72 小时", text)

    def test_status_rule_matches_generator(self) -> None:
        text = self.body("STD-004")
        self.assertIn(f"不早于第 {C.RISING_PEAK_HOUR} 小时", text)
        start, end = C.SUBSIDED_TAIL_HOURS
        self.assertIn(f"第 {start}–{end - 1} 小时", text)
        self.assertIn(f"不超过 {int(C.SUBSIDED_TAIL_SHARE * 100)}%", text)
        for value in ("rising", "subsided", "declining"):
            self.assertIn(value, text)

    def test_new_account_definition_matches_generator(self) -> None:
        self.assertIn(f"不足 {C.NEW_ACCOUNT_DAYS} 天", self.body("MTH-003"))

    def test_heat_definition_matches_generator(self) -> None:
        text = self.body("MTH-001")
        self.assertIn("包含原帖和转发", text)
        self.assertIn("取最早的一个", text)  # 与 config.status_for 的并列规则一致

    def test_case_numbers_satisfy_the_methods_they_cite(self) -> None:
        """复盘里的数字要和它引用的判定标准对得上。"""
        self.assertIn("23 个账号", self.body("CASE-002"))  # >= 10，判定为协同
        self.assertIn("共 9 个", self.body("CASE-003"))  # < 10，不判定
        self.assertIn("共 8 条", self.body("CASE-016"))  # < 10，不判定
        self.assertEqual(C.level_for(680), 1)  # CASE-008
        self.assertEqual(C.level_for(120), 4)  # CASE-011
        self.assertEqual(C.level_for(430), 2)  # CASE-019
        self.assertLessEqual(12, 64 * 0.2)  # CASE-006：第 14 小时起不超过 12 条，峰值 64
        self.assertLessEqual(6 / 410, C.SUBSIDED_TAIL_SHARE)  # CASE-013：已平息
        self.assertGreater(16 / 260, C.SUBSIDED_TAIL_SHARE)  # CASE-014：回落中


if __name__ == "__main__":
    unittest.main()
