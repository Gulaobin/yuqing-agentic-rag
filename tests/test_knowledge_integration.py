"""端到端：真实嵌入模型 + docker compose 的 PostgreSQL。设置 YUQING_INTEGRATION=1 才运行。

会重建正式知识库（public schema 的 kb_* 表），然后用语义问题检查检索结果。
"""

from __future__ import annotations

import os
import unittest

from yuqing.knowledge import ingest, store
from yuqing.knowledge.embedding import OllamaEmbedder, query_input
from yuqing.settings import load_settings

ENABLED = os.environ.get("YUQING_INTEGRATION") == "1"

# 问题 → 必须出现在前 5 个片段中的文档。问题刻意不照抄文档标题。
EXPECTED = {
    "哪一级舆情需要在当天上报？": "STD-002",
    "怎么判断是不是水军集中转发？": "MTH-002",
    "注册几天以内的账号算新号？": "MTH-003",
    "怎么判断一个事件的热度还在上升？": "STD-004",
    "舆论从负面转为正面，有什么判定标准？": "MTH-004",
    "首发账号粉丝很少，被大号转发后才火起来，这叫什么？": "MTH-005",
}


@unittest.skipUnless(ENABLED, "未设置 YUQING_INTEGRATION=1")
class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert ingest.main([]) == 0, "入库失败"
        cls.settings = load_settings()
        cls.embedder = OllamaEmbedder(cls.settings.ollama_url, cls.settings.embed_model, cls.settings.embed_dim)
        cls.conn = store.connect(cls.settings.postgres_dsn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def test_meta_matches_settings(self) -> None:
        meta = store.read_meta(self.conn)
        store.check_compatible(meta, self.settings.embed_model, self.settings.embed_dim)
        self.assertEqual(meta["documents"], "40")

    def test_semantic_questions_hit_expected_documents(self) -> None:
        misses = {}
        for question, doc_id in EXPECTED.items():
            vector = self.embedder.embed([query_input(question)])[0]
            top = [h.doc_id for h in store.search(self.conn, vector, k=5)]
            if doc_id not in top:
                misses[question] = (doc_id, top)
        self.assertEqual(misses, {}, "未命中：问题 → (期望文档, 实际前 5)")


if __name__ == "__main__":
    unittest.main()
