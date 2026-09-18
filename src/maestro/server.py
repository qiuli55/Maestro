"""Maestro Web 服务：FastAPI 组装器（中间件 / 路由注册 / 静态挂载）。

路由按域拆分在 maestro/api/ 包（tasks / workflows / meta / chat / conversations / ws），
共享基础设施（线程池 / 快照 / WS 广播）在 maestro/api/deps.py。

启动：python -m maestro.server   或    maestro serve
"""
from __future__ import annotations

import json
import os
import sys
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
# frozen 下 web/wallpaper 被 spec 打进 _MEIPASS（_internal/），不在 exe 同级
if _runtime_mod.is_frozen() and hasattr(sys, "_MEIPASS"):
    WEB_DIR = Path(sys._MEIPASS) / "web"
    WALLPAPER_DIR = Path(sys._MEIPASS) / "wallpaper"
else:
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

# ====== API 防护中间件 ======
# [2026-09-08] 需求变更：面试演示场景不再强制 X-API-Key（原策略：设 key 后 /api/*
# 全部要鉴权）。改为「AI 消耗端点每 IP 3 次免费试用 + 访问令牌解锁」，令牌与 NOVA
# 的防线一致（317132ll）；解锁状态持久化，输对一次该 IP 永久放行。
# MAESTRO_API_KEY 降级为管理凭证：带正确 key（头或 ?key=）的调用直接放行且不消耗
# 免费次数，脚本/巡检/旧演示链接全部兼容。CSRF 的 Content-Type 检查保留。
from starlette.middleware.base import BaseHTTPMiddleware

# 免费试用配置：dispatch 内每次从 env 读，monkeypatch 即时生效（测试隔离）
_FREE_TRIAL_LIMIT_DEFAULT = 3
_UNLOCK_TOKEN_DEFAULT = "317132ll"
_TRIAL_STATE_FILE_DEFAULT = str(ROOT / "data" / "ai_call_state.json")


def _trial_cfg() -> tuple[int, str, str]:
    """返回 (免费次数上限, 访问令牌, 状态文件路径)，均允许环境变量覆盖。"""
    return (
        int(os.environ.get("MAESTRO_FREE_TRIAL_LIMIT", _FREE_TRIAL_LIMIT_DEFAULT)),
        os.environ.get("MAESTRO_GUARD_TOKEN", _UNLOCK_TOKEN_DEFAULT),
        os.environ.get("MAESTRO_TRIAL_STATE", _TRIAL_STATE_FILE_DEFAULT),
    )

# AI 消耗型端点：会触发 LLM/worker 调用的 POST（读取类端点不计数，页面打开即用）
_AI_COST_EXACT = frozenset({"/api/chat", "/api/chat/stream", "/api/tasks"})
_AI_COST_SUFFIX = ("/execute", "/retry")


def _load_trial_state() -> dict:
    """读取免费试用/解锁状态；文件缺失或损坏时返回空结构。"""
    _, _, state_file = _trial_cfg()
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        return {"ips": {}, "unlocked": []}


def _save_trial_state(st: dict) -> None:
    """持久化试用状态（解锁名单 + 各 IP 已用次数）。"""
    _, _, state_file = _trial_cfg()
    Path(state_file).parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(st, f)


class _APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """API 防护：AI 消耗端点按 IP 免费试用，超次需访问令牌；key 为管理凭证。"""

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

        # 白名单直接放行
        if path in self._WHITELIST_PATHS or any(
            path.startswith(p) for p in self._WHITELIST_PREFIXES
        ):
            return await call_next(request)

        # 管理凭证：正确 X-API-Key（头或 ?key= 查询参数）放行且不消耗免费次数
        # —— 兼容巡检脚本、旧演示链接与服务间调用
        provided = (
            request.headers.get("x-api-key") or request.query_params.get("key") or ""
        ).strip()
        if api_key and provided and _compare_keys(provided, api_key):
            return await call_next(request)

        # CSRF 防线：恶意网页可用 text/plain 发"简单请求"绕过 CORS preflight 打
        # 写端点；要求 application/json 迫使其 preflight，从而被 CORS 拦截。
        # 只看 Origin 头（浏览器跨源请求必带）：curl/服务间调用/测试不带 Origin，
        # 不受影响；自家页面所有 fetch 都声明 application/json，天然通过。
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

        # AI 消耗端点：每 IP 前 N 次免费，超过需访问令牌（输对一次永久解锁）
        if self._is_ai_cost(request.method, path):
            limit, unlock_token, _ = _trial_cfg()
            st = _load_trial_state()
            ip = (
                request.headers.get("x-real-ip")
                or (request.client.host if request.client else "unknown")
            )
            if ip not in st.get("unlocked", []):
                token = (
                    request.headers.get("x-access-token")
                    or request.query_params.get("token")
                    or ""
                ).strip()
                if token and _compare_keys(token, unlock_token):
                    st.setdefault("unlocked", []).append(ip)
                    _save_trial_state(st)
                else:
                    used = st.get("ips", {}).get(ip, 0)
                    if used >= limit:
                        return JSONResponse(
                            {
                                "error": "免费体验已超过 "
                                + str(limit)
                                + " 次，请输入访问令牌继续使用",
                                "need_token": True,
                            },
                            status_code=401,
                        )
                    st.setdefault("ips", {})[ip] = used + 1
                    _save_trial_state(st)

        return await call_next(request)

    @staticmethod
    def _is_ai_cost(method: str, path: str) -> bool:
        """判断是否为受管端点（触发 LLM/worker 调用或审批决策的 POST；读取类不计数）。"""
        if method != "POST" or not path.startswith("/api/"):
            return False
        # [2026-09-16] 审批端点纳入计数：原实现只数 chat/execute/retry，访客可
        # 无鉴权反复审批自己触发的命令（自批漏洞）。计入后超免费次数的 IP
        # 必须持访问令牌才能审批，与"3 次机会"策略对齐。
        if "/approvals/" in path:
            return True
        return path in _AI_COST_EXACT or path.endswith(_AI_COST_SUFFIX)


app.add_middleware(_APIKeyAuthMiddleware)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """统一注入安全响应头（XSS / clickjacking / MIME sniffing / referrer 防御）。

    CSP 故意保持宽松（允许 'unsafe-inline' 样式 + 同源 img/connect/frame）——
    前端是单文件 + 内联 inline style + 使用 fetch/EventSource，前端重构到 ES
    modules + 外部样式后可逐步收紧。/metrics 文本格式不走 HTML，CSP 不影响。
    """

    _PATH_PASSTHROUGH = frozenset({"/metrics"})  # 文本响应不受 CSP 影响

    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        # nosniff / clickjacking / referrer：直接赋值（BaseHTTPMiddleware 的
        # MutableHeaders 用 setdefault 在某些路径不生效）
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        # Permissions-Policy：关闭不需用到的强大 API（浏览器原生权限管理，比
        # 依赖每个脚本自觉更稳）。Geolocation/camera/mic/USB/etc. 全关
        # （桌面本地服务用不到；自托管场景如有需要再开）。
        resp.headers["Permissions-Policy"] = (
            "accelerometer=(), ambient-light-sensor=(), autoplay=(), battery=(), "
            "camera=(), display-capture=(), document-domain=(), encrypted-media=(), "
            "execution-while-not-rendered=(), execution-while-out-of-viewport=(), "
            "fullscreen=(self), geolocation=(), gyroscope=(), magnetometer=(), "
            "microphone=(), midi=(), payment=(), picture-in-picture=(), "
            "publickey-credentials-get=(), screen-wake-lock=(), sync-xhr=(), "
            "usb=(), web-share=(), xr-spatial-tracking=()"
        )
        # CSP：仅对 HTML 响应注入（静态/JSON/流式跳过，避免污染 text/event-stream）
        ctype = resp.headers.get("Content-Type", "")
        if (resp.headers.get("Content-Type", "").startswith("text/html") or
                (request.url.path == "/" and not resp.headers.get("Content-Type"))):
            # 内联样式不可避免（pet_q 视差 + 一些 UI 微样式）；script 全部走 app.js
            # 'unsafe-inline' 仅在 style 允许；object-src 'none' 防插件注入；
            # frame-ancestors 'none' 替代 X-Frame-Options（DUAL 防御）。
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "img-src 'self' data: blob:; "
                "style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; "
                "connect-src 'self' ws: wss:; "
                "font-src 'self' data:; "
                "object-src 'none'; "
                "base-uri 'self'; "
                "form-action 'self'; "
                "frame-ancestors 'none'; "
                "upgrade-insecure-requests"
            )
        return resp


app.add_middleware(_SecurityHeadersMiddleware)


# ====== 路由注册（按域） ======
app.include_router(api_meta.router)
app.include_router(api_tasks.router)
app.include_router(api_workflows.router)
app.include_router(api_chat.router)
app.include_router(api_conversations.router)
app.include_router(api_ws.router)


async def _backup_loop() -> None:
    """每 24h 在线备份一次（保留 7 份）。启动后立即跑第一轮。"""
    import asyncio as _asyncio

    from . import db as _db

    while True:
        try:
            _db.backup_database(keep=7)
        except Exception as e:  # noqa: BLE001 — 备份失败不拖垮服务
            log.warning("在线备份失败（24h 后重试）: %s", e)
        await _asyncio.sleep(24 * 3600)


# ====== lifespan（替代弃用的 on_event；后台任务持引用防 GC） ======
from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # ---- startup ----
    import asyncio

    from .api.deps import capture_main_loop as _capture, prune_old_events_startup as _prune

    await _capture()
    await _prune()
    _backup_task = asyncio.get_running_loop().create_task(_backup_loop())
    app.state.backup_task = _backup_task  # 持引用防 GC
    # 审批实时推送：注册 sandbox.on_request 钩子（WS 审批卡片依赖）
    from .api.deps import _approval_push_hook as _approval_hook
    from . import sandbox as _sandbox
    _sandbox.on_request(_approval_hook)
    yield
    # ---- shutdown ----
    _backup_task.cancel()


app.router.lifespan_context = _lifespan


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
