"""命令行：python -m yuqing.agent "问题" [--config v0.1|fixed] [--json]

实时打印运行轨迹，最后输出回答。事件写入 var/events.sqlite，可用 python -m yuqing.agent.replay 重放。
"""

from __future__ import annotations

import argparse
import json
import sys

from ..core.events import EventLog
from ..core.llm import ChatClient, ModelError
from ..core.tools import Toolbox
from ..knowledge.embedding import OllamaEmbedder
from ..knowledge.store import KnowledgeBaseError
from ..settings import ROOT, load_settings
from ..tools.data import BusinessDB, data_tools
from ..tools.docs import PgDocsBackend, docs_tools
from .config import PRESETS
from .loop import Agent
from .trace import format_event

EVENTS_PATH = ROOT / "var" / "events.sqlite"
BUSINESS_DB = ROOT / "data" / "generated" / "yuqing.sqlite"


def build_agent(observer=None) -> Agent:
    settings = load_settings()
    client = ChatClient(settings.model_base_url, settings.model_api_key, settings.model_name,
                        timeout=settings.model_timeout)
    embedder = OllamaEmbedder(settings.ollama_url, settings.embed_model, settings.embed_dim)
    docs = PgDocsBackend(settings.postgres_dsn, embedder)
    toolbox = Toolbox(docs_tools(docs) + data_tools(BusinessDB(BUSINESS_DB)))
    return Agent(client, toolbox, EventLog(EVENTS_PATH), docs=docs, observer=observer)


def print_event(type: str, payload: dict) -> None:
    line = format_event(type, payload)
    if line:
        print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuqing.agent", description="舆情研判 Agent")
    parser.add_argument("question")
    parser.add_argument("--config", choices=sorted(PRESETS), default="v0.1")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    args = parser.parse_args(argv)

    try:
        agent = build_agent(observer=None if args.json else print_event)
    except (ModelError, KnowledgeBaseError, FileNotFoundError, RuntimeError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    result = agent.run(args.question, PRESETS[args.config])
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"\n回答（run_id {result.run_id}）：\n{result.answer.text}")
    return 0 if result.status != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
