"""pgvector 存储：全量重建知识库，按余弦距离检索。

重建在一个事务里完成（PostgreSQL 的 DDL 也是事务性的）：中途失败时旧数据保持不变。
psycopg 在函数内导入，只做解析和分块的测试不需要安装它。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .documents import Chunk, Document, corpus_sha256


DEFAULT_SCHEMA = "public"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class KnowledgeBaseError(RuntimeError):
    pass


def _q(schema: str) -> str:
    """校验并返回 schema 名。表名一律写成 schema.表，不依赖 search_path，避免删错 schema 里的表。"""
    if not _IDENT.match(schema):
        raise KnowledgeBaseError(f"schema 名 {schema!r} 不合法")
    return schema


@dataclass(frozen=True)
class Hit:
    rank: int
    chunk_id: str
    doc_id: str
    title: str
    doc_type: str
    section: str
    content: str
    similarity: float


def connect(dsn: str) -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise KnowledgeBaseError("缺少依赖 psycopg：pip install -r requirements.txt") from exc
    try:
        return psycopg.connect(dsn, connect_timeout=10)
    except psycopg.OperationalError as exc:
        raise KnowledgeBaseError(f"连不上 PostgreSQL：{exc}") from exc


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def schema_statements(dim: int, schema: str = DEFAULT_SCHEMA) -> list[str]:
    s = _q(schema)
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        f"DROP TABLE IF EXISTS {s}.kb_chunks",
        f"DROP TABLE IF EXISTS {s}.kb_documents",
        f"DROP TABLE IF EXISTS {s}.kb_meta",
        f"CREATE TABLE {s}.kb_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        f"""CREATE TABLE {s}.kb_documents (
            doc_id   TEXT PRIMARY KEY,
            title    TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            path     TEXT NOT NULL,
            sha256   TEXT NOT NULL
        )""",
        f"""CREATE TABLE {s}.kb_chunks (
            chunk_id   TEXT PRIMARY KEY,
            doc_id     TEXT NOT NULL REFERENCES {s}.kb_documents (doc_id),
            seq        INTEGER NOT NULL,
            section    TEXT NOT NULL,
            content    TEXT NOT NULL,
            char_start INTEGER NOT NULL,
            char_end   INTEGER NOT NULL,
            embedding  vector({int(dim)}) NOT NULL
        )""",
    ]


def rebuild(
    conn: Any,
    docs: list[Document],
    chunks: list[Chunk],
    vectors: list[list[float]],
    model: str,
    dim: int,
    schema: str = DEFAULT_SCHEMA,
) -> None:
    s = _q(schema)
    if len(chunks) != len(vectors):
        raise KnowledgeBaseError(f"分块 {len(chunks)} 个，向量 {len(vectors)} 条，数量不一致")
    with conn.transaction():
        with conn.cursor() as cur:
            for statement in schema_statements(dim, s):
                cur.execute(statement)
            cur.executemany(
                f"INSERT INTO {s}.kb_documents (doc_id, title, doc_type, path, sha256) VALUES (%s, %s, %s, %s, %s)",
                [(d.doc_id, d.title, d.doc_type, d.path, d.sha256) for d in docs],
            )
            cur.executemany(
                f"INSERT INTO {s}.kb_chunks (chunk_id, doc_id, seq, section, content, char_start, char_end, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                [
                    (c.chunk_id, c.doc_id, c.seq, c.section, c.text, c.start, c.end, vector_literal(v))
                    for c, v in zip(chunks, vectors)
                ],
            )
            cur.execute(f"CREATE INDEX kb_chunks_embedding_idx ON {s}.kb_chunks USING hnsw (embedding vector_cosine_ops)")
            cur.executemany(
                f"INSERT INTO {s}.kb_meta (key, value) VALUES (%s, %s)",
                [
                    ("embed_model", model),
                    ("embed_dim", str(dim)),
                    ("corpus_sha256", corpus_sha256(docs)),
                    ("documents", str(len(docs))),
                    ("chunks", str(len(chunks))),
                ],
            )


def read_meta(conn: Any, schema: str = DEFAULT_SCHEMA) -> dict[str, str]:
    s = _q(schema)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"{s}.kb_meta",))
        if cur.fetchone()[0] is None:
            raise KnowledgeBaseError("知识库还没建：先运行 python -m yuqing.knowledge.ingest")
        cur.execute(f"SELECT key, value FROM {s}.kb_meta")
        return dict(cur.fetchall())


def check_compatible(meta: dict[str, str], model: str, dim: int) -> None:
    """查询向量必须和入库时用同一个模型、同一维度，否则相似度没有意义。"""
    if meta.get("embed_model") != model or meta.get("embed_dim") != str(dim):
        raise KnowledgeBaseError(
            f"知识库用 {meta.get('embed_model')}（{meta.get('embed_dim')} 维）建立，"
            f"当前配置是 {model}（{dim} 维）：请重新运行入库"
        )


def search(conn: Any, query_vector: list[float], k: int = 5, schema: str = DEFAULT_SCHEMA) -> list[Hit]:
    s = _q(schema)
    literal = vector_literal(query_vector)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.chunk_id, c.doc_id, d.title, d.doc_type, c.section, c.content,
                   1 - (c.embedding <=> %(q)s::vector) AS similarity
            FROM {s}.kb_chunks c JOIN {s}.kb_documents d ON d.doc_id = c.doc_id
            ORDER BY c.embedding <=> %(q)s::vector, c.chunk_id
            LIMIT %(k)s
            """,
            {"q": literal, "k": k},
        )
        rows = cur.fetchall()
    return [Hit(rank, *row[:6], float(row[6])) for rank, row in enumerate(rows, start=1)]
