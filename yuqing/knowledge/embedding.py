"""嵌入：调用 Ollama 的 /api/embed，按批发送，校验条数和维度。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol

# Qwen3-Embedding 对查询侧建议加任务说明，文档侧不加。
QUERY_INSTRUCTION = "Instruct: 给定一个舆情研判问题，检索能够回答它的规范、方法或复盘文档片段\nQuery: "


def query_input(question: str) -> str:
    return QUERY_INSTRUCTION + question


class EmbeddingError(RuntimeError):
    pass


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, dim: int, batch_size: int = 16, timeout: float = 300):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[i:i + self.batch_size]))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        request = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=json.dumps({"model": self.model, "input": batch}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise EmbeddingError(f"嵌入请求失败：HTTP {exc.code} {detail}（模型 {self.model} 是否已拉取？）") from exc
        except urllib.error.URLError as exc:
            raise EmbeddingError(f"连不上嵌入服务 {self.base_url}：{exc.reason}") from exc
        vectors = payload.get("embeddings") or []
        if len(vectors) != len(batch):
            raise EmbeddingError(f"嵌入服务返回 {len(vectors)} 条向量，请求了 {len(batch)} 条")
        for vector in vectors:
            if len(vector) != self.dim:
                raise EmbeddingError(f"向量维度 {len(vector)} 与配置 EMBED_DIM={self.dim} 不一致")
        return vectors
