"""知识库存储测试：需要一个装了 pgvector 的 PostgreSQL 和 psycopg。

用确定性的字符二元组哈希向量代替真实嵌入，检验建表、写入、元数据、检索和事务回滚。
运行条件（二选一）：
- YUQING_INTEGRATION=1：连接 .env 配置的数据库（即 docker compose 起的 PostgreSQL）；
- YUQING_TEST_DSN="host=... port=... dbname=... user=... password=..."：连接指定数据库。
表建在独立的 kb_test schema 里，不影响正式知识库（public schema 中的 kb_* 表）。
"""

from __future__ import annotations

import hashlib
import math
import os
import unittest

from yuqing.knowledge import store
from yuqing.knowledge.documents import chunk_document, load_documents
from yuqing.settings import ROOT, load_settings


def resolve_dsn() -> str | None:
    if os.environ.get("YUQING_TEST_DSN"):
        return os.environ["YUQING_TEST_DSN"]
    if os.environ.get("YUQING_INTEGRATION") == "1":
        return load_settings().postgres_dsn
    return None


DSN = resolve_dsn()
SCHEMA = "kb_test"
DIM = 256


def hash_embed(text: str, dim: int = DIM) -> list[float]:
    """字符二元组哈希到固定维度并归一化：同样的词得到相近的向量。"""
    vector = [0.0] * dim
    for a, b in zip(text, text[1:]):
        vector[int(hashlib.md5((a + b).encode("utf-8")).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


@unittest.skipUnless(DSN, "未设置 YUQING_INTEGRATION=1 或 YUQING_TEST_DSN")
class StoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = load_documents(ROOT / "data" / "docs")
        cls.chunks = [c for d in cls.docs for c in chunk_document(d)]
        cls.vectors = [hash_embed(c.embedding_input) for c in cls.chunks]
        cls.conn = store.connect(DSN)
        store.rebuild(cls.conn, cls.docs, cls.chunks, cls.vectors, "hash-bigram", DIM, SCHEMA)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def test_meta_and_counts(self) -> None:
        meta = store.read_meta(self.conn, SCHEMA)
        self.assertEqual(meta["embed_model"], "hash-bigram")
        self.assertEqual(meta["chunks"], str(len(self.chunks)))
        store.check_compatible(meta, "hash-bigram", DIM)
        with self.assertRaises(store.KnowledgeBaseError):
            store.check_compatible(meta, "qwen3-embedding:0.6b", 1024)
        count = self.conn.execute(f"SELECT COUNT(*) FROM {SCHEMA}.kb_chunks").fetchone()[0]
        self.assertEqual(count, len(self.chunks))

    def test_search_finds_lexically_matching_chunk(self) -> None:
        cases = {
            "新注册账号的界定": "MTH-003",
            "疑似协同转发的判定": "MTH-002",
            "舆情上报流程 上报时限": "STD-002",
        }
        for question, doc_id in cases.items():
            with self.subTest(question):
                hits = store.search(self.conn, hash_embed(question), k=5, schema=SCHEMA)
                self.assertEqual(len(hits), 5)
                self.assertIn(doc_id, [h.doc_id for h in hits])
                self.assertEqual([h.rank for h in hits], [1, 2, 3, 4, 5])
                sims = [h.similarity for h in hits]
                self.assertEqual(sims, sorted(sims, reverse=True))

    def test_failed_rebuild_keeps_previous_data(self) -> None:
        bad = [v[:-1] for v in self.vectors]  # 维度不对，插入时失败
        with self.assertRaises(Exception):
            store.rebuild(self.conn, self.docs, self.chunks, bad, "broken", DIM, SCHEMA)
        self.conn.rollback()
        self.assertEqual(store.read_meta(self.conn, SCHEMA)["embed_model"], "hash-bigram")

    def test_mismatched_lengths_rejected_before_writing(self) -> None:
        with self.assertRaises(store.KnowledgeBaseError):
            store.rebuild(self.conn, self.docs, self.chunks, self.vectors[:-1], "x", DIM, SCHEMA)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(DSN, "未设置 YUQING_INTEGRATION=1 或 YUQING_TEST_DSN")
class SchemaIsolationTest(unittest.TestCase):
    def test_test_schema_rebuild_leaves_public_untouched(self) -> None:
        """在 kb_test 里重建，不能删掉或改动 public 里的同名表。"""
        conn = store.connect(DSN)
        try:
            docs = load_documents(ROOT / "data" / "docs")[:2]
            chunks = [c for d in docs for c in chunk_document(d)]
            vectors = [hash_embed(c.embedding_input) for c in chunks]
            store.rebuild(conn, docs, chunks, vectors, "public-marker", DIM, "kb_iso_a")
            store.rebuild(conn, docs, chunks, vectors, "other", DIM, "kb_iso_b")
            conn.commit()
            self.assertEqual(store.read_meta(conn, "kb_iso_a")["embed_model"], "public-marker")
            self.assertEqual(store.read_meta(conn, "kb_iso_b")["embed_model"], "other")
        finally:
            conn.execute("DROP SCHEMA IF EXISTS kb_iso_a CASCADE")
            conn.execute("DROP SCHEMA IF EXISTS kb_iso_b CASCADE")
            conn.commit()
            conn.close()

    def test_invalid_schema_name_rejected(self) -> None:
        with self.assertRaises(store.KnowledgeBaseError):
            store.schema_statements(8, "public; DROP TABLE x")
