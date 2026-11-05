"""入库命令：python -m yuqing.knowledge.ingest [--dry-run] [--max-chars N]

读取 data/docs 下的全部文档 → 分块 → 嵌入 → 在一个事务里重建 kb_* 表。
--dry-run 只解析和分块，不连接任何服务。
"""

from __future__ import annotations

import argparse
import sys
import time

from ..settings import load_settings
from . import store
from .documents import DEFAULT_MAX_CHARS, DocumentError, chunk_document, load_documents
from .embedding import EmbeddingError, OllamaEmbedder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuqing.knowledge.ingest", description="重建知识库")
    parser.add_argument("--dry-run", action="store_true", help="只解析和分块，不嵌入、不写库")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="每块最多字符数")
    args = parser.parse_args(argv)

    settings = load_settings()
    try:
        docs = load_documents(settings.docs_dir)
    except DocumentError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    chunks = [c for doc in docs for c in chunk_document(doc, args.max_chars)]
    lengths = [len(c.text) for c in chunks]
    print(f"{len(docs)} 篇文档，{len(chunks)} 个分块，块长 {min(lengths)}–{max(lengths)} 字符")
    if args.dry_run:
        return 0

    embedder = OllamaEmbedder(settings.ollama_url, settings.embed_model, settings.embed_dim)
    started = time.monotonic()
    try:
        vectors = embedder.embed([c.embedding_input for c in chunks])
    except EmbeddingError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print(f"嵌入完成：{settings.embed_model}，{len(vectors)} 条，用时 {time.monotonic() - started:.1f} 秒")

    try:
        conn = store.connect(settings.postgres_dsn)
        with conn:
            store.rebuild(conn, docs, chunks, vectors, settings.embed_model, settings.embed_dim)
    except store.KnowledgeBaseError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print("[OK] 知识库已重建：kb_documents、kb_chunks、kb_meta")
    return 0


if __name__ == "__main__":
    sys.exit(main())
