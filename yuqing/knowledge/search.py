"""检索命令：python -m yuqing.knowledge.search "问题" [-k 5]"""

from __future__ import annotations

import argparse
import sys

from ..settings import load_settings
from . import store
from .embedding import EmbeddingError, OllamaEmbedder, query_input


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuqing.knowledge.search", description="检索知识库")
    parser.add_argument("question")
    parser.add_argument("-k", type=int, default=5, help="返回片段数，默认 5")
    args = parser.parse_args(argv)

    settings = load_settings()
    embedder = OllamaEmbedder(settings.ollama_url, settings.embed_model, settings.embed_dim)
    try:
        conn = store.connect(settings.postgres_dsn)
        with conn:
            store.check_compatible(store.read_meta(conn), settings.embed_model, settings.embed_dim)
            vector = embedder.embed([query_input(args.question)])[0]
            hits = store.search(conn, vector, args.k)
    except (store.KnowledgeBaseError, EmbeddingError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    for hit in hits:
        preview = hit.content.replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:60] + "…"
        print(f"{hit.rank}. {hit.similarity:.3f}  {hit.chunk_id}  {hit.section}")
        print(f"   {preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
