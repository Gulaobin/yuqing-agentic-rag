"""HTTP 接口（FastAPI）。

POST /api/runs            提交问题，立即返回 202 和 run_id
GET  /api/runs/{run_id}   ?after=<seq> 读取新事件；done 为真时 result 是最终结果
GET  /api/configs         可用配置
GET  /                    聊天页
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .service import MAX_QUESTION_CHARS, RunService

STATIC = Path(__file__).with_name("static")
RUN_ID = re.compile(r"^[0-9a-f]{12}$")


class RunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    config: str = "v0.1"


def create_app(service: RunService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.service is None:  # 正常启动时才构建真实 Agent；测试直接传入 service
            from ..agent.__main__ import build_agent

            app.state.service = RunService(build_agent())
        yield
        app.state.service.shutdown()

    app = FastAPI(title="舆情研判 Agentic RAG", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    @app.post("/api/runs", status_code=202)
    def create_run(request: RunRequest) -> dict:
        try:
            run_id = app.state.service.submit(request.question, request.config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, after: int = Query(0, ge=0)) -> dict:
        status = app.state.service.status(run_id, after) if RUN_ID.match(run_id) else None
        if status is None:
            raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
        return status

    @app.get("/api/configs")
    def configs() -> list[dict]:
        return [c.to_dict() for c in app.state.service.configs.values()]

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    return app


app = create_app()
