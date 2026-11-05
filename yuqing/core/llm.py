"""OpenAI 兼容的聊天模型客户端（非流式），支持工具调用。

- 429 和 5xx、网络错误、超时会按退避重试；400/401/403/404 等请求本身的错误不重试。
- 错误信息里带响应体前 300 字，绝不带 API Key。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .messages import ToolCall

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ModelError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class Completion:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    usage: Usage
    model: str
    latency_s: float
    attempts: int


def parse_completion(payload: dict[str, Any], latency_s: float, attempts: int) -> Completion:
    try:
        choice = payload["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelError(f"响应缺少 choices[0].message：{json.dumps(payload, ensure_ascii=False)[:300]}") from exc
    calls = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") or {}
        if not raw.get("id") or not function.get("name"):
            raise ModelError(f"工具调用缺少 id 或函数名：{json.dumps(raw, ensure_ascii=False)[:300]}")
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):  # 个别兼容实现直接给对象
            arguments = json.dumps(arguments, ensure_ascii=False)
        calls.append(ToolCall(id=raw["id"], name=function["name"], arguments=arguments))
    usage = payload.get("usage") or {}
    return Completion(
        content=message.get("content"),
        tool_calls=tuple(calls),
        finish_reason=choice.get("finish_reason") or "",
        usage=Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
        model=payload.get("model") or "",
        latency_s=latency_s,
        attempts=attempts,
    )


class ChatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 120,
        max_retries: int = 2,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise ModelError("缺少 MODEL_API_KEY：在 .env 中设置")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.sleep = sleep

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
    ) -> Completion:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        started = time.monotonic()
        for attempt in range(1, self.max_retries + 2):
            try:
                payload = self._post(data)
                return parse_completion(payload, time.monotonic() - started, attempt)
            except ModelError as exc:
                if not exc.retryable or attempt > self.max_retries:
                    raise
                self.sleep(self.backoff * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    def _post(self, data: bytes) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ModelError(
                f"模型接口返回 HTTP {exc.code}：{detail}", status=exc.code, retryable=exc.code in RETRYABLE_STATUS
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ModelError(f"连不上模型接口 {self.url}：{reason}", retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise ModelError("模型接口返回的不是 JSON", retryable=True) from exc
