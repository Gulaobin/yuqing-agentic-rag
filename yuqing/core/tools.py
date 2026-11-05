"""工具箱：登记工具、生成接口所需的工具定义、校验参数并执行。

参数错误、未知工具、工具内部异常都不抛给 Agent loop，而是作为一条失败的工具结果回填给模型，
让模型有机会改正参数再试。校验器只实现工具参数用得到的 JSON Schema 子集，不引入额外依赖。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class ToolDefinitionError(ValueError):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema，顶层必须是 object
    handler: Callable[[dict[str, Any]], Any]

    def to_api(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


@dataclass(frozen=True)
class ToolResult:
    name: str
    ok: bool
    content: str  # 回填给模型的文本
    error: str | None = None  # unknown_tool / bad_json / invalid_arguments / handler_error / budget


def validate(schema: dict[str, Any], value: Any, path: str = "参数") -> list[str]:
    """返回错误描述列表；空列表表示通过。"""
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(TYPE_CHECKS[t](value) for t in types):
            return [f"{path} 应为 {'/'.join(types)} 类型，实际为 {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} 只能取 {schema['enum']} 之一，实际为 {value!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path} 长度不能少于 {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path} 长度不能超过 {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path} 格式不符合 {schema['pattern']}")
    if TYPE_CHECKS["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} 不能小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} 不能大于 {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path} 至少要有 {schema['minItems']} 项")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path} 最多 {schema['maxItems']} 项")
        if "items" in schema:
            for i, item in enumerate(value):
                errors.extend(validate(schema["items"], item, f"{path}[{i}]"))
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"缺少必填参数 {key}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"不支持的参数 {', '.join(extra)}，可用参数：{', '.join(properties) or '无'}")
        for key, sub in properties.items():
            if key in value:
                errors.extend(validate(sub, value[key], key if path == "参数" else f"{path}.{key}"))
    return errors


def apply_defaults(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    filled = dict(arguments)
    for key, sub in schema.get("properties", {}).items():
        if key not in filled and "default" in sub:
            filled[key] = sub["default"]
    return filled


def to_text(result: Any) -> str:
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


class Toolbox:
    def __init__(self, tools: list[Tool]):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if not NAME_PATTERN.match(tool.name):
                raise ToolDefinitionError(f"工具名 {tool.name!r} 只能包含字母、数字、下划线和连字符，最长 64")
            if tool.name in self._tools:
                raise ToolDefinitionError(f"工具名 {tool.name} 重复")
            if tool.parameters.get("type") != "object":
                raise ToolDefinitionError(f"工具 {tool.name} 的参数 schema 顶层必须是 object")
            self._tools[tool.name] = tool

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        selected = self.names if names is None else names
        unknown = [n for n in selected if n not in self._tools]
        if unknown:
            raise ToolDefinitionError(f"未登记的工具：{unknown}")
        return [self._tools[n].to_api() for n in selected]

    def run(self, name: str, arguments: str) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(name, False, f"错误：没有名为 {name} 的工具。可用工具：{', '.join(self.names)}", "unknown_tool")
        try:
            parsed = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return ToolResult(name, False, f"错误：参数不是合法的 JSON（{exc.msg}，位置 {exc.pos}）", "bad_json")
        errors = validate(tool.parameters, parsed)
        if errors:
            return ToolResult(name, False, "错误：参数不合法。" + "；".join(errors), "invalid_arguments")
        try:
            result = tool.handler(apply_defaults(tool.parameters, parsed))
        except Exception as exc:  # 工具内部异常回填给模型，不中断整轮对话
            return ToolResult(name, False, f"错误：工具执行失败（{type(exc).__name__}: {exc}）", "handler_error")
        return ToolResult(name, True, to_text(result))

    @staticmethod
    def budget_exhausted(name: str) -> ToolResult:
        return ToolResult(name, False, "错误：本轮工具调用次数已用完，未执行。请根据已有信息直接回答。", "budget")
