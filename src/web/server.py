"""FastAPI 应用：托管 src/web/static 静态资源 + /api 端点（WEB_PLAN.md §4-②）。

API 契约（§5）：
```
GET    /api/sessions                      会话列表 [{thread_id, title, tech, created_at, updated_at, preview, qa_count, note_count}]
POST   /api/sessions                      新建会话 → {thread_id}
GET    /api/sessions/{id}                 会话详情（conversation 消息流 + 状态摘要）
POST   /api/sessions/{id}/run             {command, tech?, focus?, args?} → 执行图
POST   /api/sessions/{id}/resume          恢复 interrupt（note 合并决策 {answer}）
GET    /api/sessions/{id}/stream          SSE：进度事件流
GET    /api/docs                          列出文档 {materials, reports, knowledge}
GET    /api/docs/content?path=...         单个 markdown 文件内容（路径白名单）
DELETE /api/sessions/{id}                 删除会话
```
统一卡片契约与 domain/card_input.parse_card_input 一致：collect → {command, tech, focus?}；
read/ask → {command, args: [...]}；前端卡片命令名 ask 经 card_input 映射为图命令 qa。

I1：本模块顶层不 import langgraph / chromadb（见各子模块 lazy import），
`import src.web` 满足 WEB_PLAN.md §9 约束。
"""

import asyncio
import json
import queue
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import config
from ..domain.card_input import parse_card_input
from . import docs as docs_mod
from . import runner as runner_mod
from . import sessions as sessions_mod

# 静态资源目录（原生模块化 SPA：index.html + css/ + js/）
_STATIC_DIR = Path(__file__).parent / "static"


class RunRequest(BaseModel):
    command: str
    tech: str | None = None
    focus: str | None = None
    args: list[str] | None = None


class ResumeRequest(BaseModel):
    answer: str


def _build_payload(req: RunRequest) -> dict:
    """把前端卡片输入转成图执行契约（复用 parse_card_input 校验，错误文案一致）。"""
    cmd = (req.command or "").strip().lower()
    if cmd == "collect":
        tokens = [(req.tech or "").strip()]
        if req.focus and req.focus.strip():
            tokens.append(req.focus.strip())
        return parse_card_input("collect", tokens)
    if cmd == "note":
        return {"command": "note"}
    return parse_card_input(cmd, req.args or [])


def create_app() -> FastAPI:
    app = FastAPI(title="Tech Learner Agent", docs_url="/api/docs-ui", openapi_url="/api/openapi.json")

    # 个人工具绑 127.0.0.1（§3.1），宽松 CORS 仅为支持 file:// 直开 index.html 的开发调试
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- 会话 ----
    @app.get("/api/sessions")
    def list_sessions():
        return sessions_mod.list_sessions()

    @app.post("/api/sessions")
    def create_session():
        thread_id = f"learn-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        return {"thread_id": thread_id}

    @app.get("/api/sessions/{thread_id}")
    def get_session(thread_id: str):
        s = sessions_mod.get_session(thread_id)
        if s is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return s

    @app.delete("/api/sessions/{thread_id}")
    def delete_session(thread_id: str):
        if not sessions_mod.delete_session(thread_id):
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"ok": True}

    # ---- 图执行：run / resume / SSE ----
    @app.post("/api/sessions/{thread_id}/run")
    def run_session(thread_id: str, req: RunRequest):
        payload = _build_payload(req)
        if payload.get("error"):
            raise HTTPException(status_code=422, detail=payload["error"])
        err = runner_mod.start_run(thread_id, payload)
        if err:
            raise HTTPException(status_code=409, detail=err)
        return {"status": "started"}

    @app.post("/api/sessions/{thread_id}/resume")
    def resume_session(thread_id: str, req: ResumeRequest):
        err = runner_mod.resume_run(thread_id, req.answer)
        if err:
            raise HTTPException(status_code=409, detail=err)
        return {"status": "resumed"}

    @app.get("/api/sessions/{thread_id}/stream")
    async def stream_session(thread_id: str):
        job = runner_mod.get_job(thread_id)

        async def gen():
            while True:
                try:
                    evt = job.queue.get_nowait()
                except queue.Empty:
                    # 无事件：worker 还活着 → 心跳保活；worker 结束且队列空 → 关闭
                    if not job.active and job.queue.empty():
                        break
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(1)
                    continue
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                if evt.get("type") == "done":
                    break

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- 文件浏览 ----
    @app.get("/api/docs")
    def list_docs():
        return docs_mod.list_docs()

    @app.get("/api/docs/content")
    def read_doc(path: str):
        doc = docs_mod.read_doc(path)
        if doc is None:
            raise HTTPException(status_code=400, detail="路径不在白名单内或文件不存在")
        return doc

    # ---- 静态资源（最后挂载，API 路由优先匹配）----
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


app = create_app()


def main() -> None:
    """`python -m src.web.server` 启动入口。"""
    import uvicorn
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT)


if __name__ == "__main__":
    main()
