"""Maestro Web 服务：FastAPI 组装器（中间件 / 路由注册 / 静态挂载）。

路由按域拆分在 maestro/api/ 包（tasks / workflows / meta / chat / conversations / ws），
共享基础设施（线程池 / 快照 / WS 广播）在 maestro/api/deps.py。

启动：python -m maestro.server   或    maestro serve
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import maestro.workers as _wmod  # 触发 worker 注册

from . import config, observability
from .api import chat as api_chat
from .api import conversations as api_conversations
from .api import meta as api_meta
from .api import tasks as api_tasks
from .api import workflows as api_workflows
from .api import ws as api_ws
from .api.deps import (
    _active_ws_connections,
    _ws_lock,
    _ws_subscriptions,
    _ws_wants,
    _compare_keys,
    capture_main_loop,
    prune_old_events_startup,
)

load_dotenv()
config.apply_worker_bins()  # 把 workers.json 的 bin 路径灌进环境变量（不覆盖已设的）
observability.setup_logging()  # 结构化日志（受 LOG_LEVEL / LOG_FORMAT 控制）

log = observability.get_logger(__name__)
log.info("maestro starting", extra={"version": "0.2", "workers": _wmod.available_workers()})

from . import runtime as _runtime_mod

# 项目根 + 静态目录：打包后 ROOT 来自 MAESTRO_HOME 或 exe 同级（runtime 统一解析）
ROOT = _runtime_mod.project_root()
WEB_DIR = ROOT / "web"
WALLPAPER_DIR = ROOT / "wallpaper"

app = FastAPI(title="Maestro 编排器", version="0.2")

# CORS 限白名单：本机内网工具，只允许 127.0.0.1/localhost 跨域。
# 之前 allow_origins=["*"] 任意脚本（包括外部网站）可调用本服务 API——
# 本服务虽只监听 127.0.0.1，但浏览器跨域请求仍能从任意源发起。
# 浏览器内部预览/WorkBuddy 面板均通过 127.0.0.1:8787 访问，无影响。

_ALLOWED_ORIGINS = [
    "http://127.0.0.1:8787",
    "http://localhost:8787",
    # WorkBuddy 内部预览可能通过 file:// 或 http://localhost 访问
    "null",  # file:// 协议在浏览器中 Origin 头为 "null"
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
    allow_credentials=False,  # 无 cookie 鉴权，避免 CSRF
)

# ====== API 鉴权中间件 ======
# 启用条件：MAESTRO_API_KEY 环境变量已设（生产部署场景）。
# 未设时中间件直接放行（本地开发场景，向后兼容）。
# 白名单（无需鉴权）：/api/healthz, /api/ready, /api/workers/health, /ws/*, 静态资源。
from starlette.middleware.base import BaseHTTPMiddleware


class _APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """应用层 API 鉴权：白名单外的 /api/* 必须带 X-API-Key。"""

    _WHITELIST_PATHS = frozenset({
        "/api/healthz",         # liveness（K8s/容器探针）
        "/api/ready",           # readiness
        "/api/workers/health",  # worker 健康
        "/",                    # 静态首页
        "/wallpaper",           # 壁纸页
        "/metrics",             # Prometheus 指标（监控探针；内含计数不含敏感值）
    })
    _WHITELIST_PREFIXES = (
        "/ws/",                 # WebSocket（前端连 WS 不带 key，WS 自身鉴权）
        "/assets/",             # 静态资源
        "/static/",             # FastAPI mount 静态
    )

    def __init__(self, app):
        super().__init__(app)
        # 故意不在 __init__ 缓存 key —— dispatch 内每次从 env 读，
        # 让 monkeypatch.delenv/setenv 能即时生效（测试隔离）。

    async def dispatch(self, request, call_next):
        path = request.url.path
        api_key = os.environ.get("MAESTRO_API_KEY", "").strip()

        # 没设 key -> 放行（开发模式），但浏览器跨源写请求要求 JSON Content-Type：
        # 恶意网页可用 text/plain 发"简单请求"绕过 CORS preflight 打我们的写端点
        # （CSRF）；要求 application/json 迫使其 preflight，从而被 CORS 拦截。
        # 只看 Origin 头（浏览器跨源请求必带）：curl/服务间调用/测试不带 Origin，
        # 不受影响；自家页面所有 fetch 都声明 application/json，天然通过。
        if not api_key:
            if (
                request.headers.get("origin")
                and request.method in ("POST", "PUT", "DELETE", "PATCH")
                and path.startswith("/api/")
            ):
                ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    return JSONResponse(
                        {"error": "Content-Type must be application/json"},
                        status_code=415,
                    )
            return await call_next(request)

        # 白名单直接放行
        if path in self._WHITELIST_PATHS or any(
            path.startswith(p) for p in self._WHITELIST_PREFIXES
        ):
            return await call_next(request)

        # /api/* 业务端点要求 X-API-Key 头
        if path.startswith("/api/"):
            provided = request.headers.get("x-api-key", "").strip()
            if not provided:
                return JSONResponse(
                    {"error": "missing X-API-Key header"},
                    status_code=401,
                )
            if not _compare_keys(provided, api_key):
                return JSONResponse(
                    {"error": "invalid X-API-Key"},
                    status_code=403,
                )

        return await call_next(request)


app.add_middleware(_APIKeyAuthMiddleware)

# ====== 路由注册（按域） ======
app.include_router(api_meta.router)
app.include_router(api_tasks.router)
app.include_router(api_workflows.router)
app.include_router(api_chat.router)
app.include_router(api_conversations.router)
app.include_router(api_ws.router)

# ====== 启动钩子 ======
app.add_event_handler("startup", capture_main_loop)
app.add_event_handler("startup", prune_old_events_startup)


async def _backup_loop() -> None:
    """启动时立即备份一次，之后每 24h 备份（在线备份，保留 7 份）。"""
    import asyncio as _asyncio

    from . import db as _db

    while True:
        try:
            _db.backup_database(keep=7)
        except Exception:  # noqa: BLE001 — 备份失败不拖垮服务
            pass
        await _asyncio.sleep(24 * 3600)


@app.on_event("startup")
async def _start_backup_loop():
    import asyncio as _asyncio

    _asyncio.get_running_loop().create_task(_backup_loop())


# ---------- 壁纸预览 ----------


@app.get("/metrics")
def metrics(format: str = "prometheus"):
    """Prometheus 指标（?format=json 可切 JSON）。监控探针用，免鉴权。"""
    from . import metrics as metrics_mod

    m = metrics_mod.collect_metrics()
    if format == "json":
        return m
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        metrics_mod.format_prometheus(m),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/wallpaper")
def wallpaper():
    # 数字人页面（wallpaper/index.html）已下线。访问 /wallpaper 重定向到 /
    # （芙莉莲壁纸 1:1 复刻）。/wallpaper/models 与 /wallpaper/vendor 仍由下方
    # 静态挂载提供，供 web/index.html 的芙莉莲素材与桌宠 sprite 使用。
    return RedirectResponse(url="/", status_code=301)


# 壁纸目录整体静态挂载：/wallpaper/models/...、/wallpaper/vendor/...
# 都能通过 8787 访问，供 web/index.html（芙莉莲壁纸 + 桌宠 sprite）使用。
# /wallpaper 精确路由在前做重定向；/wallpaper/xxx 走这个挂载。
if WALLPAPER_DIR.exists():
    app.mount("/wallpaper", StaticFiles(directory=str(WALLPAPER_DIR), html=True), name="wallpaper_static")


# ---------- 静态前端（最后挂载，避免拦截 /api 与 /ws）----------

if WEB_DIR.exists():
    # 开发/自托管场景禁用前端缓存：更新 app.js/style.css 后普通刷新即生效
    # （生产 CDN 场景可再包一层带版本号的缓存策略）
    class _NoCacheStatic(StaticFiles):
        def file_response(self, *args, **kwargs):
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp

    app.mount("/", _NoCacheStatic(directory=str(WEB_DIR), html=True), name="web")


# ====== 兼容导出（既有测试/调用方依赖这些名字） ======
# 路由函数
create_task = api_tasks.create_task
# 工作流助手
apply_workflow_defaults = api_tasks.apply_workflow_defaults
_workflow_to_subtasks = api_tasks._workflow_to_subtasks
# WS 基础设施（测试直接引用模块级状态）
_ws_authenticated = api_ws._ws_authenticated


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "maestro.server:app",
        host=os.environ.get("MAESTRO_HOST", "127.0.0.1"),
        port=int(os.environ.get("MAESTRO_PORT", "8787")),
        reload=False,
    )
