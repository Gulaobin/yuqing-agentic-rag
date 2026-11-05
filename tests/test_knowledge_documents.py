"""文档解析与分块的单元测试，不需要任何服务。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yuqing.knowledge.documents import (
    DocumentError,
    chunk_document,
    load_documents,
    parse_document,
    split_sections,
)
from yuqing.settings import ROOT

DOCS_DIR = ROOT / "data" / "docs"


def make(body: str, doc_id: str = "STD-099", doc_type: str = "规范") -> str:
    return f"---\ndoc_id: {doc_id}\ntitle: 测试\ntype: {doc_type}\n---\n\n{body}"


class ParseTest(unittest.TestCase):
    def test_parses_front_matter_and_body(self) -> None:
        doc = parse_document(make("# 标题\n\n正文。\n"), "x.md")
        self.assertEqual((doc.doc_id, doc.title, doc.doc_type), ("STD-099", "测试", "规范"))
        self.assertEqual(doc.body, "# 标题\n\n正文。\n")

    def test_crlf_and_lf_give_same_document(self) -> None:
        text = make("# 标题\n\n正文。\n")
        self.assertEqual(parse_document(text, "x.md"), parse_document(text.replace("\n", "\r\n"), "x.md"))

    def test_rejects_bad_front_matter(self) -> None:
        cases = {
            "缺少 front matter": "# 标题\n",
            "没有结束标记": "---\ndoc_id: STD-001\n",
            "缺少 title": "---\ndoc_id: STD-001\ntype: 规范\n---\n",
            "编号格式": make("x", doc_id="STD-1"),
            "类型不合法": make("x", doc_type="通知"),
            "类型与前缀不符": make("x", doc_id="MTH-001", doc_type="规范"),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(DocumentError):
                parse_document(text, "x.md")

    def test_load_rejects_mismatched_file_name_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "STD-001.md").write_text(make("x", doc_id="STD-002"), encoding="utf-8")
            with self.assertRaises(DocumentError):
                load_documents(root)
            (root / "STD-001.md").write_text(make("x", doc_id="STD-001"), encoding="utf-8")
            (root / "sub").mkdir()
            (root / "sub" / "STD-001.md").write_text(make("y", doc_id="STD-001"), encoding="utf-8")
            with self.assertRaises(DocumentError):
                load_documents(root)


class ChunkTest(unittest.TestCase):
    def test_sections_follow_heading_path_and_skip_code_fences(self) -> None:
        body = "# 文档\n\n## 甲\n\n内容一\n\n```\n# 不是标题\n```\n\n### 甲一\n\n内容二\n\n## 乙\n\n内容三\n"
        sections = split_sections(body)
        self.assertEqual([s.path for s in sections], ["文档 > 甲", "文档 > 甲 > 甲一", "文档 > 乙"])
        self.assertIn("# 不是标题", body[sections[0].start:sections[0].end])

    def test_long_paragraph_is_split_within_limit(self) -> None:
        sentence = "这是一个用于测试分块的句子，长度适中。"
        doc = parse_document(make("# 标题\n\n" + sentence * 60 + "\n"), "x.md")
        chunks = chunk_document(doc, max_chars=100)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 100)
            self.assertEqual(chunk.text, doc.body[chunk.start:chunk.end])

    def test_text_without_any_break_is_hard_cut(self) -> None:
        doc = parse_document(make("# 标题\n\n" + "字" * 250 + "\n"), "x.md")
        self.assertEqual([len(c.text) for c in chunk_document(doc, max_chars=100)], [100, 100, 50])


class CorpusChunkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = load_documents(DOCS_DIR)
        cls.chunks = {d.doc_id: chunk_document(d) for d in cls.docs}

    def test_chunks_are_slices_of_body_within_limit(self) -> None:
        for doc in self.docs:
            for chunk in self.chunks[doc.doc_id]:
                self.assertEqual(chunk.text, doc.body[chunk.start:chunk.end], chunk.chunk_id)
                self.assertLessEqual(len(chunk.text), 400, chunk.chunk_id)
                self.assertEqual(chunk.text, chunk.text.strip(), chunk.chunk_id)

    def test_every_body_line_is_covered(self) -> None:
        """除标题行外，正文每一行非空文字都落在某个分块里。"""
        for doc in self.docs:
            covered = "".join(c.text for c in self.chunks[doc.doc_id])
            for line in doc.body.splitlines():
                if line.strip() and not line.startswith("#"):
                    self.assertIn(line.strip(), covered, f"{doc.doc_id}: {line[:30]}")

    def test_chunk_ids_unique_and_deterministic(self) -> None:
        ids = [c.chunk_id for chunks in self.chunks.values() for c in chunks]
        self.assertEqual(len(ids), len(set(ids)))
        again = [c.chunk_id for d in load_documents(DOCS_DIR) for c in chunk_document(d)]
        self.assertEqual(ids, again)


if __name__ == "__main__":
    unittest.main()
