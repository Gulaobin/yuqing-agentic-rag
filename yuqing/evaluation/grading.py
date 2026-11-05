"""评测判分：规则判分，只看回答文本和引用的编号，不调用模型。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..settings import ROOT

QUESTIONS_PATH = ROOT / "eval" / "questions.json"
TYPES = ("知识单点", "知识跨文档", "实例单跳", "实例多跳", "应拒答")
REFUSAL = re.compile(r"无法确定|无法判断|无法得知|无法回答|无法提供|不能确定|没有(相关)?(记录|数据|信息|资料)|"
                     r"未(记录|提供|包含)|不(包含|记录)|资料(中)?没有|超出.{0,8}(范围|窗口)")


@dataclass(frozen=True)
class Question:
    id: str
    type: str
    question: str
    must: list[list[str]] = field(default_factory=list)
    must_not: list[str] = field(default_factory=list)
    evidence_any: list[str] = field(default_factory=list)
    refuse: bool = False


@dataclass
class Grade:
    passed: bool
    missing: list[str]  # 没命中的 must 组（取每组第一个写法）
    forbidden: list[str]  # 命中的 must_not
    evidence_ok: bool
    refused: bool

    def to_dict(self) -> dict:
        return asdict(self)


def load_questions(path: Path = QUESTIONS_PATH) -> list[Question]:
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = [Question(**q) for q in data["questions"]]
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ValueError(f"题目编号重复：{ids}")
    unknown = {q.type for q in questions} - set(TYPES)
    if unknown:
        raise ValueError(f"未知题型：{unknown}")
    return questions


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def cited_docs(cited: list[str]) -> set[str]:
    """引用编号集合；片段编号同时算作它的文档编号。"""
    result = set()
    for item in cited:
        result.add(item)
        result.add(item.split("#")[0])
    return result


def grade(question: Question, answer_text: str, cited: list[str]) -> Grade:
    text = normalize(answer_text)
    missing = [group[0] for group in question.must if not any(re.search(p, text) for p in group)]
    forbidden = [p for p in question.must_not if re.search(p, text)]
    evidence_ok = not question.evidence_any or bool(cited_docs(cited) & set(question.evidence_any))
    refused = bool(REFUSAL.search(text))
    passed = not missing and not forbidden and evidence_ok and (refused if question.refuse else True)
    return Grade(passed, missing, forbidden, evidence_ok, refused)
