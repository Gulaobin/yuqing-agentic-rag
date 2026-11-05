"""结构化回答：submit_answer 的参数定义、证据编号核对与渲染。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from ..core.tools import validate

ID_REGEX = r"(?:STD|MTH|CASE)-\d{3}(?:#\d{2})?|EVT-\d{4}|P\d{4}-\d{4}|A\d{5}"
ID_PATTERN = re.compile(ID_REGEX)

SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "conclusions": {
            "type": "array",
            "maxItems": 5,
            "description": "结论，每条一句话并附证据编号",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 300},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"type": "string", "pattern": f"^(?:{ID_REGEX})$"},
                    },
                },
                "required": ["text", "evidence"],
                "additionalProperties": False,
            },
        },
        "unknowns": {
            "type": "array",
            "maxItems": 5,
            "description": "资料不足、无法确定的问题",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
        },
    },
    "required": ["conclusions"],
    "additionalProperties": False,
}

SUBMIT_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "submit_answer",
        "description": "提交最终回答。结论必须附证据编号，编号只能来自本轮工具返回的结果。调用后本轮结束。",
        "parameters": SUBMIT_SCHEMA,
    },
}


@dataclass(frozen=True)
class Conclusion:
    text: str
    evidence: tuple[str, ...]


@dataclass
class Answer:
    text: str  # 给人看的文本
    structured: bool  # 是否通过 submit_answer 提交
    conclusions: list[Conclusion] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)  # 回答引用、且本轮确实见过的编号

    def to_dict(self) -> dict:
        return asdict(self)


def extract_ids(text: str) -> set[str]:
    """从工具结果里收集编号；片段编号 STD-001#02 同时记下文档编号 STD-001。"""
    found = set()
    for match in ID_PATTERN.findall(text):
        found.add(match)
        if "#" in match:
            found.add(match.split("#")[0])
    return found


def parse_submission(arguments: str, seen: set[str]) -> tuple[Answer | None, str | None]:
    """返回 (回答, None) 或 (None, 回填给模型的错误说明)。"""
    try:
        data = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError as exc:
        return None, f"错误：submit_answer 的参数不是合法的 JSON（{exc.msg}）"
    errors = validate(SUBMIT_SCHEMA, data)
    if errors:
        return None, "错误：submit_answer 参数不合法。" + "；".join(errors)
    conclusions = [Conclusion(c["text"], tuple(c["evidence"])) for c in data["conclusions"]]
    unknowns = list(data.get("unknowns", []))
    if not conclusions and not unknowns:
        return None, "错误：conclusions 和 unknowns 不能同时为空"
    unseen = sorted({e for c in conclusions for e in c.evidence} - seen)
    if unseen:
        return None, (
            f"错误：以下证据编号没有出现在本轮工具返回的结果中：{', '.join(unseen)}。"
            "请只引用查询结果里的编号；没有证据支撑的判断写进 unknowns。"
        )
    cited = sorted({e for c in conclusions for e in c.evidence})
    return Answer(render(conclusions, unknowns), True, conclusions, unknowns, cited), None


def render(conclusions: list[Conclusion], unknowns: list[str]) -> str:
    lines = [f"{i}. {c.text} [{', '.join(c.evidence)}]" for i, c in enumerate(conclusions, start=1)]
    if unknowns:
        lines.append("无法确定：")
        lines.extend(f"- {u}" for u in unknowns)
    return "\n".join(lines)
