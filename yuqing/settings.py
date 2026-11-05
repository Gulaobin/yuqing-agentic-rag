"""运行配置：先读仓库根目录的 .env，再用同名环境变量覆盖。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 行；忽略空行和 # 注释，去掉值两侧的引号。文件不存在时返回空字典。"""
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip("'\"")
    return env


@dataclass(frozen=True)
class Settings:
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_db: str
    ollama_url: str
    embed_model: str
    embed_dim: int
    docs_dir: Path
    model_base_url: str
    model_name: str
    model_api_key: str  # 可以为空；创建模型客户端时才要求必填
    model_timeout: float

    def __repr__(self) -> str:  # 不在日志和报错里打出密钥与口令
        return f"Settings(postgres={self.postgres_host}:{self.postgres_port}/{self.postgres_db}, model={self.model_name})"

    @property
    def postgres_dsn(self) -> str:
        return (
            f"host={self.postgres_host} port={self.postgres_port} dbname={self.postgres_db} "
            f"user={self.postgres_user} password={self.postgres_password}"
        )


def load_settings(env_file: Path | None = None) -> Settings:
    values = {**load_env_file(env_file or ROOT / ".env"), **os.environ}

    def get(key: str, default: str | None = None) -> str:
        value = values.get(key, default)
        if value is None or value == "":
            raise RuntimeError(f"缺少配置 {key}：在 .env 中设置，或设置同名环境变量")
        return value

    return Settings(
        postgres_host=get("POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(get("POSTGRES_PORT", "5432")),
        postgres_user=get("POSTGRES_USER"),
        postgres_password=get("POSTGRES_PASSWORD"),
        postgres_db=get("POSTGRES_DB"),
        ollama_url=get("OLLAMA_URL", f"http://127.0.0.1:{get('OLLAMA_PORT', '11434')}"),
        embed_model=get("EMBED_MODEL", "qwen3-embedding:0.6b"),
        embed_dim=int(get("EMBED_DIM", "1024")),
        docs_dir=Path(get("DOCS_DIR", str(ROOT / "data" / "docs"))),
        model_base_url=get("MODEL_BASE_URL", "https://api.deepseek.com"),
        model_name=get("MODEL_NAME", "deepseek-flash"),
        model_api_key=values.get("MODEL_API_KEY", ""),
        model_timeout=float(get("MODEL_TIMEOUT", "120")),
    )
