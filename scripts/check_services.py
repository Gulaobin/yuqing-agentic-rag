"""检查本地依赖服务是否就绪：容器健康、vector 扩展已启用、嵌入维度正确。

用法（在仓库根目录）：python scripts/check_services.py
全部通过返回 0，任一项失败返回 1。只用标准库。
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_SERVICES = ("postgres", "ollama")


def load_env(path: Path) -> dict[str, str]:
    """解析 .env：KEY=VALUE，忽略空行和 # 注释，去掉值两侧的引号。"""
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip("'\"")
    return env


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def parse_ps(stdout: str) -> list[dict]:
    """docker compose ps --format json：新版每行一个对象，旧版是一个数组。"""
    text = stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def check_health() -> bool:
    result = compose("ps", "--format", "json")
    if result.returncode != 0:
        print(f"[FAIL] docker compose ps: {result.stderr.strip()}")
        return False
    health = {item.get("Service"): item.get("Health") or item.get("State") for item in parse_ps(result.stdout)}
    ok = True
    for name in REQUIRED_SERVICES:
        status = health.get(name, "not running")
        passed = status == "healthy"
        ok &= passed
        print(f"[{'OK' if passed else 'FAIL'}] service {name}: {status}")
    return ok


def check_vector_extension() -> bool:
    sql = "select extversion from pg_extension where extname = 'vector'"
    result = compose(
        "exec", "-T", "postgres", "sh", "-c",
        f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "{sql}"',
    )
    version = result.stdout.strip()
    if result.returncode != 0 or not version:
        print(f"[FAIL] pgvector extension not found: {result.stderr.strip() or 'empty result'}")
        return False
    print(f"[OK] pgvector extension version {version}")
    return True


def check_embedding(env: dict[str, str]) -> bool:
    port = env.get("OLLAMA_PORT", "11434")
    model = env.get("EMBED_MODEL", "qwen3-embedding:0.6b")
    expected = int(env.get("EMBED_DIM", "1024"))
    body = json.dumps({"model": model, "input": "测试"}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/embed",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"[FAIL] embed {model}: HTTP {exc.code} {detail} (model pulled? see ollama-pull logs)")
        return False
    except urllib.error.URLError as exc:
        print(f"[FAIL] embed {model}: cannot reach 127.0.0.1:{port} ({exc.reason})")
        return False
    vectors = payload.get("embeddings") or []
    dim = len(vectors[0]) if vectors else 0
    passed = dim == expected
    print(f"[{'OK' if passed else 'FAIL'}] embed {model}: dim {dim}, expected {expected}")
    return passed


def main() -> int:
    env_path = ROOT / ".env"
    if not env_path.exists():
        print("[FAIL] .env not found: copy .env.example to .env first")
        return 1
    env = load_env(env_path)
    results = [check_health(), check_vector_extension(), check_embedding(env)]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
