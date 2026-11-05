"""评测命令：python -m yuqing.evaluation.run [--configs v0.1,fixed] [--only K1,S2] [--label v0.1]

逐题、逐配置运行 Agent，规则判分，结果写入 eval/results/<label>.json，并打印按题型分开的对照表。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime

from ..agent.config import PRESETS
from ..settings import ROOT, load_settings
from .grading import TYPES, grade, load_questions

RESULTS_DIR = ROOT / "eval" / "results"


def summarize(records: list[dict], configs: list[str]) -> dict:
    summary: dict[str, dict] = {}
    for config in configs:
        rows = [r for r in records if r["config"] == config]
        by_type = defaultdict(lambda: [0, 0])
        for r in rows:
            by_type[r["type"]][1] += 1
            by_type[r["type"]][0] += r["grade"]["passed"]
        n = len(rows) or 1
        summary[config] = {
            "passed": sum(r["grade"]["passed"] for r in rows),
            "total": len(rows),
            "by_type": {t: {"passed": by_type[t][0], "total": by_type[t][1]} for t in TYPES if by_type[t][1]},
            "avg_model_calls": round(sum(r["model_calls"] for r in rows) / n, 2),
            "avg_tool_calls": round(sum(r["tool_calls"] for r in rows) / n, 2),
            "avg_prompt_tokens": round(sum(r["prompt_tokens"] for r in rows) / n),
            "avg_completion_tokens": round(sum(r["completion_tokens"] for r in rows) / n),
            "avg_latency_s": round(sum(r["latency_s"] for r in rows) / n, 1),
        }
    return summary


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def cell(text: object, size: int, left: bool = False) -> str:
    text = str(text)
    pad = " " * max(0, size - width(text))
    return text + pad if left else pad + text


def print_table(summary: dict, configs: list[str]) -> None:
    rows = [["题型"] + configs]
    for t in TYPES:
        row = [t]
        for c in configs:
            item = summary[c]["by_type"].get(t)
            row.append(f"{item['passed']}/{item['total']}" if item else "-")
        rows.append(row)
    rows.append(["合计"] + [f"{summary[c]['passed']}/{summary[c]['total']}" for c in configs])
    for key, label in (("avg_model_calls", "平均模型调用"), ("avg_tool_calls", "平均工具调用"),
                       ("avg_prompt_tokens", "平均输入 token"), ("avg_latency_s", "平均耗时 s")):
        rows.append([label] + [summary[c][key] for c in configs])
    for row in rows:
        print(cell(row[0], 16, left=True) + "".join(cell(x, 10) for x in row[1:]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuqing.evaluation.run")
    parser.add_argument("--configs", default="v0.1,fixed")
    parser.add_argument("--only", default="", help="只跑这些题目编号，逗号分隔")
    parser.add_argument("--label", default="v0.1", help="结果文件名 eval/results/<label>.json")
    args = parser.parse_args(argv)

    configs = [c for c in args.configs.split(",") if c]
    unknown = [c for c in configs if c not in PRESETS]
    if unknown:
        print(f"[FAIL] 未知配置：{unknown}", file=sys.stderr)
        return 1
    questions = load_questions()
    if args.only:
        wanted = set(args.only.split(","))
        questions = [q for q in questions if q.id in wanted]

    from ..agent.__main__ import build_agent
    from ..core.llm import ModelError
    from ..knowledge.store import KnowledgeBaseError

    try:
        agent = build_agent()
    except (ModelError, KnowledgeBaseError, FileNotFoundError, RuntimeError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    records = []
    started = time.monotonic()
    for question in questions:
        for config in configs:
            result = agent.run(question.question, PRESETS[config])
            graded = grade(question, result.answer.text, result.answer.cited)
            mark = "✓" if graded.passed else "✗"
            detail = ""
            if not graded.passed:
                parts = []
                if graded.missing:
                    parts.append(f"缺 {graded.missing}")
                if graded.forbidden:
                    parts.append(f"违禁 {graded.forbidden}")
                if not graded.evidence_ok:
                    parts.append("证据不符")
                if question.refuse and not graded.refused:
                    parts.append("未拒答")
                detail = "  " + "；".join(parts)
            print(f"{mark} {question.id} [{config}] {result.status} 模型 {result.model_calls} 工具 {result.tool_calls}{detail}",
                  flush=True)
            records.append({
                "id": question.id, "type": question.type, "config": config, "question": question.question,
                "run_id": result.run_id, "status": result.status, "answer": result.answer.text,
                "cited": result.answer.cited, "model_calls": result.model_calls, "tool_calls": result.tool_calls,
                "prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
                "latency_s": result.latency_s, "grade": graded.to_dict(),
            })

    summary = summarize(records, configs)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{args.label}.json"
    out.write_text(json.dumps({
        "label": args.label,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "model": load_settings().model_name,
        "configs": {c: PRESETS[c].to_dict() for c in configs},
        "summary": summary,
        "records": records,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print()
    print_table(summary, configs)
    print(f"\n用时 {time.monotonic() - started:.0f} 秒，结果写入 {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
