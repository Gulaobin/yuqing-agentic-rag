"""文档工具：search_docs 语义检索片段，read_doc 读整篇文档。"""

from __future__ import annotations

from typing import Any, Protocol

from ..core.tools import Tool
from ..knowledge import store
from ..knowledge.embedding import Embedder, query_input

READ_LIMIT = 4000  # read_doc 单次返回的最大字符数


class DocsBackend(Protocol):
    def search(self, query: str, k: int, doc_type: str | None) -> list[dict[str, Any]]: ...

    def read(self, doc_id: str) -> dict[str, Any] | None: ...


class PgDocsBackend:
    """pgvector 知识库。构造时核对嵌入模型与入库时一致。"""

    def __init__(self, dsn: str, embedder: Embedder, schema: str = store.DEFAULT_SCHEMA):
        self.dsn = dsn
        self.embedder = embedder
        self.schema = schema
        with store.connect(dsn) as conn:
            store.check_compatible(store.read_meta(conn, schema), embedder.model, embedder.dim)

    def search(self, query: str, k: int, doc_type: str | None) -> list[dict[str, Any]]:
        vector = self.embedder.embed([query_input(query)])[0]
        with store.connect(self.dsn) as conn:
            hits = store.search(conn, vector, k, self.schema, doc_type)
        return [
            {"chunk_id": h.chunk_id, "doc_id": h.doc_id, "title": h.title, "doc_type": h.doc_type,
             "section": h.section, "similarity": round(h.similarity, 3), "content": h.content}
            for h in hits
        ]

    def read(self, doc_id: str) -> dict[str, Any] | None:
        with store.connect(self.dsn) as conn:
            found = store.read_document(conn, doc_id, self.schema)
        if found is None:
            return None
        title, doc_type, sections = found
        return {"doc_id": doc_id, "title": title, "doc_type": doc_type,
                "sections": [{"section": s, "text": t} for s, t in sections]}


def docs_tools(backend: DocsBackend) -> list[Tool]:
    def search_docs(args: dict[str, Any]) -> dict[str, Any]:
        hits = backend.search(args["query"], args["k"], args.get("doc_type"))
        return {"query": args["query"], "hits": hits}

    def read_doc(args: dict[str, Any]) -> dict[str, Any]:
        doc = backend.read(args["doc_id"])
        if doc is None:
            raise ValueError(f"文档 {args['doc_id']} 不存在")
        text = "\n\n".join(f"## {s['section']}\n{s['text']}" for s in doc["sections"])
        truncated = len(text) > READ_LIMIT
        return {"doc_id": doc["doc_id"], "title": doc["title"], "doc_type": doc["doc_type"],
                "text": text[:READ_LIMIT], "truncated": truncated}

    return [
        Tool(
            name="search_docs",
            description="在研判规范、方法说明、历史复盘文档中做语义检索，返回最相关的若干片段及其编号。"
                        "适合查询定义、标准、流程、时限、判定方法和历史案例。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 100, "description": "检索语句，用自然语言描述要找的内容"},
                    "k": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5, "description": "返回片段数"},
                    "doc_type": {"type": "string", "enum": ["规范", "方法", "复盘"], "description": "只在某类文档中检索"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=search_docs,
        ),
        Tool(
            name="read_doc",
            description="按文档编号读取整篇文档，例如 STD-001、MTH-002、CASE-003。片段信息不完整时使用。",
            parameters={
                "type": "object",
                "properties": {"doc_id": {"type": "string", "pattern": "^(STD|MTH|CASE)-\\d{3}$"}},
                "required": ["doc_id"],
                "additionalProperties": False,
            },
            handler=read_doc,
        ),
    ]
