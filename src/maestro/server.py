"""Maestro Web 服务（P2 任务窗口）：FastAPI + WebSocket 实时推送。

前端内页面在 web/ 目录（仪表盘 / 任务详情 / 壁纸预览）。
编排器 run_task 是同步阻塞的（场景 B 还含 LLM 汇总），故用后台线程执行，
WebSocket 每 ~1.2s 轮询 SQLite 把最新状态推给前端。

启动：python -m maestro.server   或   maestro serve
"""

from __future__ import annotations

import asyncio
import json
import hmac
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import maestro.workers as _wmod  # 触发 worker 注册

from . import chat, config, db, observability, orchestrator, sandbox, split

load_dotenv()
config.apply_worker_bins()  # 把 workers.json 的 bin 路径灌进环境变量（不覆盖已设的）
observability.setup_logging()  # 结构化日志（受 LOG_LEVEL / LOG_FORMAT 控制）

log = observability.get_logger(__name__)
log.info("maestro starting", extra={"version": "0.2", "workers": _wmod.available_workers()})

ROOT = Path(__file__).resolve().parents[2]
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
from starlette.responses import JSONResponse


class _APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """应用层 API 鉴权：白名单外的 /api/* 必须带 X-API-Key。"""

    _WHITELIST_PATHS = frozenset({
        "/api/healthz",         # liveness（K8s/容器探针）
        "/api/ready",           # readiness
        "/api/workers/health",  # worker 健康
        "/",                    # 静态首页
        "/wallpaper",           # Live2D 壁纸页
    })
    _WHITELIST_PREFIXES = (
        "/ws/",                 # WebSocket（前端连 WS 不带 key）
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


def _compare_keys(provided: str, expected: str) -> bool:
    """常量时间比较，防时序攻击。"""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


# 中间件总是挂载——dispatch 内读 env（每次请求都判断当前 env）。
# 这样测试不需要 reimport server，monkeypatch.setenv/delenv 即可切换鉴权。
app.add_middleware(_APIKeyAuthMiddleware)

# 快/慢池分离：prepare（拆分，毫秒级）走快池；execute/retry/resume（长任务，
# 最长 600s）走慢池。共用一个池时，4 个长任务会把新任务的拆分也饿死。
_executor_fast = ThreadPoolExecutor(max_workers=4, thread_name_prefix="maestro-fast")
_executor_slow = ThreadPoolExecutor(max_workers=2, thread_name_prefix="maestro-slow")

_POLL_INTERVAL = 1.2  # WS 轮询间隔（秒）


# ---------- 后台任务执行 ----------


def _prepare_job(
    task_id: str,
    prompt: str,
    scenario: str,
    worker_type: str,
    parallel: bool,
    model: str | None,
    no_merge: bool = False,
    confirm: bool = True,
    conv_id: str | None = None,
    selected_workers: list[str] | None = None,
    custom_subtasks: list[dict] | None = None,
):
    """后台线程：拆分+入库，停在 READY 等用户确认；confirm=False 则立即执行。

    custom_subtasks 非空时走工作流编排链路（卡片即子任务，跳过拆分）。
    """
    conn = db.init_db()
    selected_workers = selected_workers or ["embedded"]
    try:
        if custom_subtasks is not None:
            task_id_out = orchestrator.prepare_custom(
                conn,
                prompt,
                task_id=task_id,
                subtasks=custom_subtasks,
                parallel=parallel,
                no_merge=no_merge,
                conv_id=conv_id,
            )
        else:
            task_id_out = orchestrator.prepare_task(
                conn,
                prompt,
                scenario=scenario,
                worker_type=worker_type,
                parallel=parallel,
                model=model,
                task_id=task_id,
                no_merge=no_merge,
                conv_id=conv_id,
            )
            # 多 agent round-robin 分配
            if len(selected_workers) > 1:
                subtasks = db.get_subtasks(conn, task_id_out)
                for i, st in enumerate(subtasks):
                    wt = selected_workers[i % len(selected_workers)]
                    conn.execute("UPDATE subtasks SET worker_type=? WHERE id=?", (wt, st["id"]))
                conn.commit()
        if not confirm:
            # 跳过人工闸门：拆分完立即执行（等价旧行为）
            orchestrator.execute_task(conn, task_id, model=model)
    except Exception as e:  # noqa: BLE001 — 拆分/执行失败也要落库可见
        db.set_task_status(conn, task_id, db.FAILED)
        db.log_event(conn, task_id, "prepare FAILED", data={"error": str(e)[:500]})
    finally:
        conn.close()


def _execute_job(task_id: str, timeout: int = 600):
    """后台线程：确认后执行完整链路。异常落 FAILED，避免任务永远停在 RUNNING
    （线程池的 future 无人取结果，不落库就静默丢失）。"""
    conn = db.init_db()
    try:
        orchestrator.execute_task(conn, task_id, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — 与 _prepare_job 对齐：执行失败也要落库可见
        db.set_task_status(conn, task_id, db.FAILED)
        db.log_event(conn, task_id, "execute FAILED", data={"error": str(e)[:500]})
    finally:
        conn.close()


def _retry_job(subtask_id: str, timeout: int = 600):
    conn = db.init_db()
    try:
        orchestrator.retry_subtask(conn, subtask_id, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — 重派失败也要落库可见
        try:
            db.set_subtask_output(conn, subtask_id, db.FAILED,
                                  error=f"[重派失败] {type(e).__name__}: {str(e)[:200]}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()


def _resume_job(task_id: str, timeout: int = 600):
    conn = db.init_db()
    try:
        orchestrator.resume_incomplete(conn, task_id, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — 续跑失败也要落库可见
        db.set_task_status(conn, task_id, db.FAILED)
        db.log_event(conn, task_id, "resume FAILED", data={"error": str(e)[:500]})
    finally:
        conn.close()


# ---------- 快照（DB -> dict）----------


def _snapshot(task_id: str) -> dict | None:
    conn = db.init_db()
    try:
        task = db.get_task(conn, task_id)
        if not task:
            return None
        subtasks = db.get_subtasks(conn, task_id)
        events = db.get_events(conn, task_id)
        summary_md = None
        if task.get("result_path") and os.path.exists(task["result_path"]):
            try:
                summary_md = Path(task["result_path"]).read_text(encoding="utf-8")
            except OSError:
                summary_md = None
        return {
            "task": dict(task),
            "subtasks": [dict(s) for s in subtasks],
            "events": [dict(e) for e in events],
            "summary_md": summary_md,
            "approvals": sandbox.list_pending(task_id),
        }
    finally:
        conn.close()


def _task_row(task: dict, n_subtasks: int, n_failed: int, updated_at: str) -> dict:
    return {
        "id": task["id"],
        "user_prompt": (task["user_prompt"] or "")[:120],
        "status": task["status"],
        "created_at": task["created_at"],
        "updated_at": updated_at,
        "n_subtasks": n_subtasks,
        "n_failed": n_failed,
    }


# ---------- 健康检查（Kubernetes liveness / readiness 探针用）----------


@app.get("/api/healthz", include_in_schema=False)
def healthz():
    """轻量 liveness 探针：进程是否在响应（不查依赖）。

    用于 K8s livenessProbe / Docker HEALTHCHECK / 负载均衡器探活。
    """
    return {"status": "ok"}


@app.get("/api/ready")
def ready():
    """完整 readiness 探针：检查 SQLite 可用 + LLM provider 配置。

    Returns:
        200 + {"status": "ok", "checks": {...}} — 全通过
        503 + {"status": "degraded", "checks": {...}} — 关键依赖异常
    """
    checks: dict = {}
    db_ok = True
    try:
        conn = db.init_db()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        checks["db_error"] = str(e)[:200]
        db_ok = False
    checks["db"] = "ok" if db_ok else "fail"

    # LLM provider 配置检查（不真发请求，只看 key 是否存在）
    providers = config.load_providers()
    key_present = bool(os.environ.get("DEEPSEEK_API_KEY")) or bool(providers)
    checks["llm"] = "ok" if key_present else "no_key"

    # Worker 注册检查
    workers = _wmod.available_workers()
    checks["workers"] = {"count": len(workers), "names": workers}

    all_ok = db_ok and key_present
    return {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
        "version": "0.2",
    }


@app.get("/api/workers/health")
def workers_health():
    """所有已注册 worker 的健康检查状态。

    每个 worker 检查：
    - bin 路径存在 + 可执行
    -（可选）--version 探测 5s 超时

    结果缓存 30s，避免每次 dispatch 都探测。

    返回：
        200 + {"workers": {name: {"ok": bool, "reason": str}}, "all_ok": bool}
        503 + {"workers": {...}, "all_ok": false, "degraded": [names]} — 有 worker 不健康
    """
    results: dict = {}
    degraded: list[str] = []
    for name in _wmod.available_workers():
        try:
            worker = _wmod.get_worker(name)
            # 只对 SubprocessWorker 调用 check_health（embedded/minimax 等非子进程 worker
            # 内部 Python 模块，无 bin/超时等概念，标 ok=True 即可）
            from maestro.workers.base import SubprocessWorker
            if isinstance(worker, SubprocessWorker):
                ok, reason = worker.check_health()
            else:
                ok, reason = True, "in-process worker"
        except KeyError:
            ok, reason = False, "worker not registered"
        except Exception as e:  # noqa: BLE001
            ok, reason = False, f"check_health raised: {type(e).__name__}"
        results[name] = {"ok": ok, "reason": reason}
        if not ok:
            degraded.append(name)

    all_ok = not degraded
    payload = {"workers": results, "all_ok": all_ok}
    if not all_ok:
        payload["degraded"] = degraded
    if all_ok:
        return payload
    # 至少一个 worker 不健康，返回 503 让监控告警
    from fastapi.responses import JSONResponse
    return JSONResponse(content=payload, status_code=503)


# ---------- REST API ----------


@app.get("/api/tasks")
def list_tasks(status: str | None = None, limit: int = 200, offset: int = 0):
    """任务列表：可选按状态过滤 + 分页（默认最近 200 条，与旧行为一致）。"""
    if limit < 1 or limit > 1000:
        raise HTTPException(400, "limit 须在 1-1000")
    if offset < 0:
        raise HTTPException(400, "offset 不能为负")
    if status is not None and status not in db.VALID_STATUS:
        raise HTTPException(400, f"status 非法（允许: {sorted(db.VALID_STATUS)}）")

    conn = db.init_db()
    try:
        # LEFT JOIN 保证没子任务的 task 也在结果里（COUNT 为 0）。
        base = (
            "SELECT t.id, t.user_prompt, t.status, t.created_at, t.updated_at, "
            "       COUNT(s.id) AS n_subtasks, "
            "       SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END) AS n_failed "
            "FROM tasks t LEFT JOIN subtasks s ON s.task_id = t.id"
        )
        params: list = []
        if status:
            base += " WHERE t.status=?"
            params.append(status)
        base += " GROUP BY t.id ORDER BY t.created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = conn.execute(base, params).fetchall()
        out = []
        for r in rows:
            r = dict(r)
            n = int(r.pop("n_subtasks") or 0)
            nf = int(r.pop("n_failed") or 0)
            out.append(_task_row(r, n, nf, r["updated_at"]))
        return out
    finally:
        conn.close()


# Agent/Worker 列表（前端多选 agent 面板用）
_WORKER_META = {
    "embedded": {"label": "内置编排器", "desc": "本地 LLM，内置文件工具，适合轻量任务"},
    "opencode": {"label": "OpenCode", "desc": "深度编码 agent，适合代码生成/重构"},
    "octo": {"label": "Octo", "desc": "Headless 编码 agent，走 DeepSeek"},
    "workbuddy": {"label": "WorkBuddy", "desc": "全能工具型 agent，适合复杂工作流"},
    "minimax": {"label": "MiniMax", "desc": "图片/视频/语音生成，适合创意任务"},
    "codebuddy": {"label": "CodeBuddy", "desc": "腾讯云编码 agent（需实名认证）"},
}


def _workflow_to_subtasks(definition: dict) -> list[dict]:
    """用户工作流定义 → 有序启用卡片（跳过禁用卡片）。"""
    subs = []
    for st in (definition or {}).get("stages") or []:
        stage_name = str(st.get("name") or "环节").strip()[:30]
        for c in st.get("cards") or []:
            if not c.get("enabled", True):
                continue
            desc = (c.get("desc") or "").strip()
            if not desc:
                continue
            subs.append({
                "desc": f"【{stage_name}】{desc}"[:900],
                "worker_type": c.get("worker") or "embedded",
                "model": c.get("model") or None,
                "skills": [str(s)[:30] for s in (c.get("skills") or [])][:8],
            })
    return subs


def _validate_workflow_definition(definition) -> list[dict]:
    """校验并清洗工作流定义，返回规范化的 stages。"""
    if not isinstance(definition, dict) or not isinstance(definition.get("stages"), list) \
            or not definition["stages"]:
        raise HTTPException(400, "definition.stages 不能为空")
    available = _wmod.available_workers()
    cleaned = []
    for i, st in enumerate(definition["stages"]):
        if not isinstance(st, dict):
            raise HTTPException(400, f"环节 #{i + 1} 格式错误")
        name = str(st.get("name") or f"环节{i + 1}").strip()[:30]
        raw_cards = st.get("cards")
        if not isinstance(raw_cards, list) or not raw_cards:
            raise HTTPException(400, f"环节「{name}」至少需要一张卡片")
        cards = []
        for c in raw_cards:
            if not isinstance(c, dict):
                raise HTTPException(400, f"环节「{name}」存在格式错误的卡片")
            desc = str(c.get("desc") or "").strip()
            if not desc:
                raise HTTPException(400, f"环节「{name}」有卡片缺任务描述")
            wt = c.get("worker") or "embedded"
            if wt not in available:
                raise HTTPException(400, f"环节「{name}」卡片智能体非法: {wt}")
            cards.append({
                "id": str(c.get("id") or f"card_{os.urandom(4).hex()}"),
                "enabled": bool(c.get("enabled", True)),
                "desc": desc[:800],
                "worker": wt,
                "model": c.get("model") or None,
                "skills": [str(s)[:30] for s in (c.get("skills") or [])][:8],
            })
        cleaned.append({
            "id": str(st.get("id") or f"stage_{os.urandom(4).hex()}"),
            "name": name,
            "cards": cards,
        })
    return cleaned


@app.get("/api/workflows")
def list_workflows():
    """工作流库（任务处理模板）：前端任务窗口的选择器数据源。"""
    from . import config as _cfg

    available = _wmod.available_workers()
    out = []
    for wf in _cfg.load_workflows():
        workers = [w for w in wf["workers"] if w in available]
        out.append({
            "id": wf["id"],
            "name": wf["name"],
            "icon": wf["icon"],
            "desc": wf["desc"],
            "scenario": wf["scenario"],
            "parallel": wf["parallel"],
            "workers": workers or wf["workers"],
            "workers_available": bool(workers),
            "model": wf["model"],
            "confirm": wf["confirm"],
            "no_merge": wf["no_merge"],
            "source": "preset",
        })
    conn = db.init_db()
    try:
        for uw in db.list_user_workflows(conn):
            out.append({
                "id": uw["id"],
                "name": uw["name"],
                "icon": "🧩",
                "desc": "自定义工作流",
                "source": "user",
            })
    finally:
        conn.close()
    return out


@app.get("/api/workflows/{wf_id}")
def get_workflow_detail(wf_id: str):
    """单个工作流详情（编排器载入用）：preset 返回预设字段，user 返回完整 definition。"""
    from . import config as _cfg

    preset = _cfg.resolve_workflow(wf_id)
    if preset:
        return {"id": preset["id"], "name": preset["name"], "source": "preset",
                "icon": preset["icon"], "desc": preset["desc"],
                "definition": {"stages": [{"id": "stage_" + preset["id"], "name": preset["name"],
                                            "cards": [{"id": "card_" + preset["id"], "enabled": True,
                                                       "desc": preset["desc"] or ("按预设执行：" + preset["name"]),
                                                       "worker": (preset["workers"] or ["embedded"])[0],
                                                       "model": preset["model"], "skills": []}]}]}}
    conn = db.init_db()
    try:
        uw = db.get_user_workflow(conn, wf_id)
    finally:
        conn.close()
    if not uw:
        raise HTTPException(404, "工作流不存在")
    return {"id": uw["id"], "name": uw["name"], "source": "user", "definition": uw["definition"]}


@app.post("/api/workflows")
def save_user_workflow(payload: dict):
    """保存（新建/更新）用户自建工作流。definition.stages 经白名单校验。"""
    name = str(payload.get("name") or "").strip()[:40]
    if not name:
        raise HTTPException(400, "工作流名称不能为空")
    stages = _validate_workflow_definition(payload.get("definition"))
    wf_id = str(payload.get("id") or f"wf_{os.urandom(4).hex()}")
    conn = db.init_db()
    try:
        db.upsert_user_workflow(conn, wf_id, name, {"stages": stages})
    finally:
        conn.close()
    return {"ok": True, "id": wf_id, "name": name, "stages": stages}


@app.delete("/api/workflows/{wf_id}")
def delete_user_workflow(wf_id: str):
    conn = db.init_db()
    try:
        if not db.delete_user_workflow(conn, wf_id):
            raise HTTPException(404, "工作流不存在（内置预设不可删除）")
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/skills")
def list_skills():
    """技能库：内置 + 用户自建（同名覆盖），带分类。"""
    from . import skills as skills_mod

    conn = db.init_db()
    try:
        return skills_mod.merged_skills(conn)
    finally:
        conn.close()


@app.post("/api/skills")
def add_skill(payload: dict):
    """新增用户技能；未指定分类时按名称关键词自动分类。"""
    from . import skills as skills_mod

    name = str(payload.get("name") or "").strip()[:30]
    if not name:
        raise HTTPException(400, "技能名称不能为空")
    category = str(payload.get("category") or "").strip()[:10]
    if not category:
        category = skills_mod.classify_skill(name)
    if category not in skills_mod.CATEGORIES:
        raise HTTPException(400, f"分类非法：{category}（允许: {skills_mod.CATEGORIES}）")
    snippet = str(payload.get("snippet") or "").strip()[:300]
    conn = db.init_db()
    try:
        # 同名用户技能已存在 → 更新
        existing = next((s for s in db.list_user_skills(conn) if s["name"] == name), None)
        s_id = existing["id"] if existing else f"sk_{os.urandom(4).hex()}"
        db.upsert_user_skill(conn, s_id, name, category, snippet)
        user_names = {s["name"] for s in db.list_user_skills(conn)}
    finally:
        conn.close()
    # 内置同名技能被用户技能覆盖
    effective_source = "user" if name in user_names else "builtin"
    return {"ok": True, "id": s_id, "name": name, "category": category,
            "snippet": snippet, "source": effective_source}


@app.delete("/api/skills/{skill_id}")
def delete_skill(skill_id: str):
    conn = db.init_db()
    try:
        if not db.delete_user_skill(conn, skill_id):
            raise HTTPException(404, "技能不存在（内置技能不可删除）")
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/agents")
def list_agents():
    """返回所有已注册的 agent（含名称/描述/是否可用）。"""
    from . import workers as _wmod

    available = _wmod.available_workers()
    return [
        {
            "name": name,
            "label": _WORKER_META.get(name, {}).get("label", name),
            "desc": _WORKER_META.get(name, {}).get("desc", ""),
            "available": name in available,
        }
        for name in _WORKER_META
    ]


@app.get("/api/keys")
def list_keys():
    """Key 库列表（不含实际 key 值，只返回元数据）。"""
    from . import config as _cfg

    keys_data = _cfg.load_keys()
    out = []
    for k in keys_data.get("keys") or []:
        env_var = k.get("env_var", "")
        configured = bool(env_var and os.environ.get(env_var))
        out.append(
            {
                "id": k.get("id"),
                "label": k.get("label", env_var),
                "provider": k.get("provider", ""),
                "models": k.get("models", []),
                "configured": configured,
            }
        )
    return out


@app.get("/api/models")
def list_models():
    """返回所有已配置的模型（环境变量存在才返回）。"""
    from . import config as _cfg

    return _cfg.get_available_models()


def apply_workflow_defaults(payload: dict) -> dict:
    """工作流预设填充：configs/workflows.json 提供 scenario/parallel/agents/model 等默认值，
    payload 里显式传入的字段优先（前端可对工作流做逐项覆盖）。

    未知工作流抛 ValueError（由调用方转 400）。
    """
    wf_id = payload.get("workflow")
    if not wf_id:
        return payload
    from . import config as _cfg

    wf = _cfg.resolve_workflow(str(wf_id))
    if wf is None:
        raise ValueError(f"未知工作流: {wf_id}")
    out = dict(payload)
    for key in ("scenario", "parallel", "no_merge", "confirm"):
        if key not in out:
            out[key] = wf[key]
    if not out.get("selected_workers") and wf["workers"]:
        out["selected_workers"] = list(wf["workers"])
    if out.get("model") in (None, "") and wf["model"]:
        out["model"] = wf["model"]
    return out


@app.post("/api/tasks")
async def create_task(payload: dict):
    try:
        payload = apply_workflow_defaults(payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    wf_id = payload.get("workflow")
    prompt = (payload.get("prompt") or "").strip()
    # 工作流编排：客户端直接提交启用卡片作为子任务（跳过 LLM 拆分）
    custom_subtasks = payload.get("subtasks")
    if custom_subtasks is not None and not isinstance(custom_subtasks, list):
        raise HTTPException(400, "subtasks 必须是数组")
    if custom_subtasks is None and wf_id:
        # 用户自建工作流：按 id 展开启用卡片
        conn = db.init_db()
        try:
            uw = db.get_user_workflow(conn, str(wf_id))
        finally:
            conn.close()
        if uw is not None:
            custom_subtasks = _workflow_to_subtasks(uw["definition"])
            if not custom_subtasks:
                raise HTTPException(400, "该工作流没有启用的卡片")
    scenario = payload.get("scenario", "a")
    worker_type = payload.get("worker_type", "embedded")
    parallel = bool(payload.get("parallel", scenario != "a"))
    no_merge = bool(payload.get("no_merge", False))
    model = payload.get("model") or None
    # key 不在这里注入 env：llm.get_client 每次按 provider 从 providers.json
    # 的 api_key_env 现查，并发任务互不影响（进程级 env 会让不同模型任务互相覆盖 key）。
    # confirm=False 时跳过人工闸门，拆分后立即执行（等价旧行为）
    confirm = bool(payload.get("confirm", True))
    # 任务关联会话：完成后结果写入该会话（对话隔离/任务对话可见结果）
    conv_id = payload.get("conv_id") or None
    # 多 agent 选择（支持同时派给多个 agent）
    selected_workers: list[str] = payload.get("selected_workers") or []
    # 至少选一个；不合法/不可用的 agent 过滤掉
    valid_workers = _wmod.available_workers()
    selected_workers = [w for w in selected_workers if w in valid_workers]
    # 如果没传或全不合法，退回 embedded
    if not selected_workers:
        selected_workers = ["embedded"]

    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    if custom_subtasks is not None:
        scenario = "custom"  # 自定义卡片链路：不做场景校验与 auto 识别
    elif scenario not in ("a", "b", "c", "auto"):
        raise HTTPException(400, "scenario 必须是 a / b / c / auto")
    # 用第一个 agent 拆分任务，后续 round-robin 分配
    if worker_type not in valid_workers:
        worker_type = selected_workers[0]

    detect_reason = None
    if scenario == "auto" and custom_subtasks is None:
        # 决策：LLM 识别输入类型 → 路由到 a/b/c
        try:
            scenario, detect_reason = split.detect_scenario(prompt, model=model)
        except Exception as e:  # noqa: BLE001 — 识别失败退回场景 A，不让任务失败
            scenario, detect_reason = "a", f"auto 识别失败回退 a: {type(e).__name__}"

    # 预先生成 task_id 立即返回；拆分交给后台线程
    task_id = f"task_{os.urandom(4).hex()}"
    _executor_fast.submit(
        _prepare_job,
        task_id,
        prompt,
        scenario,
        worker_type,
        parallel,
        model,
        no_merge,
        confirm,
        conv_id,
        selected_workers,
        custom_subtasks,
    )
    return {
        "task_id": task_id,
        "confirm": confirm,
        "scenario": scenario,
        "detect_reason": detect_reason,
        "selected_workers": selected_workers,
        "model": model,
        "workflow": wf_id,
    }


@app.put("/api/tasks/{task_id}/subtasks")
def update_subtasks(task_id: str, payload: dict):
    """人工闸门编辑：整体替换子任务列表（增删改 / 调序）。仅 ready 态允许。"""
    items = payload.get("subtasks")
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "subtasks 必须是非空数组")
    conn = db.init_db()
    try:
        task = db.get_task(conn, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task["status"] != db.READY:
            raise HTTPException(409, f"任务状态 {task['status']} 不可编辑（仅 ready 待确认状态可编辑）")
        allowed = _wmod.available_workers()
        cleaned = []
        for i, it in enumerate(items):
            desc = (it.get("desc") or "").strip()
            if not desc:
                raise HTTPException(400, f"子任务 #{i + 1} 缺 desc")
            wt = it.get("worker_type") or "embedded"
            if wt not in allowed:
                raise HTTPException(400, f"子任务 #{i + 1} worker_type 非法: {wt}")
            cleaned.append({"id": it.get("id"), "desc": desc, "worker_type": wt})
        subs = db.replace_subtasks(conn, task_id, cleaned)
        db.log_event(conn, task_id, f"subtasks edited -> {len(subs)}", data=[s["id"] for s in subs])
        return {"ok": True, "n_subtasks": len(subs)}
    finally:
        conn.close()


@app.post("/api/tasks/{task_id}/execute")
def execute_task(task_id: str):
    """确认执行：ready 态任务按（已编辑）子任务列表开始派发。"""
    conn = db.init_db()
    try:
        task = db.get_task(conn, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task["status"] != db.READY:
            raise HTTPException(409, f"任务状态 {task['status']} 不可执行（仅 ready 待确认状态可执行）")
    finally:
        conn.close()
    _executor_slow.submit(_execute_job, task_id)
    return {"ok": True, "task_id": task_id}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    """删除任务（级联删子任务/事件）。running 中任务先取消再删。"""
    conn = db.init_db()
    try:
        task = db.get_task(conn, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task["status"] == db.RUNNING:
            raise HTTPException(409, "任务执行中，请先取消再删除")
        if not db.delete_task(conn, task_id):
            raise HTTPException(404, "任务不存在")
        return {"ok": True, "task_id": task_id}
    finally:
        conn.close()


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    """取消任务（ready 未执行 / running 执行中 / pending 均可）。终态不可取消。"""
    conn = db.init_db()
    try:
        if not db.cancel_task(conn, task_id):
            task = db.get_task(conn, task_id)
            if not task:
                raise HTTPException(404, "任务不存在")
            raise HTTPException(409, f"任务状态 {task['status']} 不可取消（仅未完成状态可取消）")
        return {"ok": True, "task_id": task_id}
    finally:
        conn.close()


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str):
    """重启恢复入口：把中断（running 态）任务的未完成子任务续跑完。"""
    conn = db.init_db()
    try:
        task = db.get_task(conn, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task["status"] != db.RUNNING:
            raise HTTPException(409, f"任务状态 {task['status']} 不可恢复（仅 running 中断态可恢复）")
    finally:
        conn.close()
    _executor_slow.submit(_resume_job, task_id)
    return {"ok": True, "task_id": task_id}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    snap = _snapshot(task_id)
    if not snap:
        raise HTTPException(404, "任务不存在")
    return snap


@app.post("/api/tasks/{task_id}/retry/{subtask_id}")
def retry_subtask(task_id: str, subtask_id: str):
    conn = db.init_db()
    try:
        st = db.get_subtask(conn, subtask_id)
    finally:
        conn.close()
    if not st or st["task_id"] != task_id:
        raise HTTPException(404, "子任务不存在")
    _executor_slow.submit(_retry_job, subtask_id)
    return {"ok": True, "subtask_id": subtask_id}


# ---------- 沙箱审批 ----------


@app.post("/api/tasks/{task_id}/approvals/{approval_id}")
def approval_decision(task_id: str, approval_id: str, payload: dict):
    """审批命令：POST {"action": "approve" | "reject"}。
    对应 WorkBuddy 沙箱的"越权需用户批准"——agent 请求执行修改类命令，
    用户在这里点头放行或否决。批准后命令立即在等待线程中执行。
    """
    action = payload.get("action")
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action 必须是 approve / reject")

    # 归属校验：审批请求必须属于该任务（防跨任务操作）
    found = False
    for ap in sandbox.list_pending(task_id):
        if ap["id"] == approval_id:
            found = True
            break
    if not found:
        raise HTTPException(404, f"审批请求不存在或不属于该任务: {approval_id}")

    ok = sandbox.decide(approval_id, approve=(action == "approve"))
    if not ok:
        raise HTTPException(409, f"审批请求已过期或已被处理: {approval_id}")
    return {"ok": True, "approval_id": approval_id, "decision": "approved" if action == "approve" else "rejected"}


# ---------- 数字人闲聊 ----------


@app.get("/api/conversations")
def conversation_list(kind: str | None = None):
    """会话列表（最近活跃倒序，带消息数/最后预览）；?kind=chat|task 只取某类。"""
    conn = db.init_db()
    try:
        return {"conversations": db.list_conversations(conn, kind=kind)}
    finally:
        conn.close()


@app.post("/api/conversations")
def conversation_create(payload: dict | None = None):
    """创建新会话：POST {"title": "...", "kind": "chat|task"} → {"conv_id", "title", "kind"}。"""
    payload = payload or {}
    title = payload.get("title") or ""
    kind = payload.get("kind") or "chat"
    if kind not in ("chat", "task"):
        raise HTTPException(400, "kind 必须是 chat / task")
    conn = db.init_db()
    try:
        conv_id = db.create_conversation(conn, title, kind=kind)
        return {"conv_id": conv_id, "title": (title or "").strip() or "新对话", "kind": kind}
    finally:
        conn.close()


@app.put("/api/conversations/{conv_id}")
def conversation_rename(conv_id: str, payload: dict):
    """重命名会话：PUT {"title": "..."}。"""
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title 不能为空")
    conn = db.init_db()
    try:
        if not db.rename_conversation(conn, conv_id, title):
            raise HTTPException(404, "会话不存在")
        return {"ok": True, "title": title}
    finally:
        conn.close()


@app.delete("/api/conversations/{conv_id}")
def conversation_delete(conv_id: str):
    """删除会话（级联删消息）。"""
    conn = db.init_db()
    try:
        if not db.delete_conversation(conn, conv_id):
            raise HTTPException(404, "会话不存在")
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/conversations/{conv_id}/export")
def conversation_export(conv_id: str):
    """导出会话为 Markdown 文本（下载文件）。"""
    conn = db.init_db()
    try:
        conv = db.get_conversation(conn, conv_id)
        if not conv:
            raise HTTPException(404, "会话不存在")
        msgs = db.get_chat_history(conn, limit=10000, conv_id=conv_id)
        lines = [f"# 对话：{conv['title']}", ""]
        for m in msgs:
            who = "👤 用户" if m["role"] == "user" else "🌿 芙莉莲"
            ts = (m["created_at"] or "")[:16].replace("T", " ")
            lines.append(f"### {who}（{ts}）")
            lines.append(m["content"])
            lines.append("")
        text = "\n".join(lines).strip() + "\n"
        filename = f"对话_{conv['title'][:16]}.md"
        return Response(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )
    finally:
        conn.close()


@app.post("/api/chat")
def chat_message(payload: dict):
    """数字人闲聊：POST {"message": "...", "conv_id": "..."} → {"reply": "..."}。"""
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "message 不能为空")
    conv_id = payload.get("conv_id") or db.DEFAULT_CONV_ID
    conn = db.init_db()
    try:
        if not db.get_conversation(conn, conv_id):
            raise HTTPException(404, "会话不存在")
        reply = chat.chat(conn, message, conv_id=conv_id)
        return {"reply": reply, "conv_id": conv_id}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — LLM 失败不拖垮服务
        raise HTTPException(500, f"闲聊失败：{e}") from e
    finally:
        conn.close()


@app.post("/api/chat/stream")
def chat_message_stream(payload: dict, background: BackgroundTasks):
    """数字人闲聊（流式/打字机）：POST {"message", "conv_id"} → SSE。

    事件：data: {"delta": "文本增量"} … data: {"done": true, "reply": "..."}
          / data: {"error": "..."}

    流结束后用 FastAPI BackgroundTasks 异步广播到 WebSocket，
    避免在同步生成器内调 asyncio.get_event_loop()（uvicorn 已在 loop 中，
    会 RuntimeError）。
    """
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "message 不能为空")
    conv_id = payload.get("conv_id") or db.DEFAULT_CONV_ID
    # 跨对话上下文关联：客户端勾选的其他会话 id（过滤自身 + 上限 4 个）
    raw_links = payload.get("link_conv_ids") or []
    if not isinstance(raw_links, list) or not all(isinstance(x, str) for x in raw_links):
        raise HTTPException(400, "link_conv_ids 必须是字符串数组")
    link_conv_ids = [x for x in raw_links if x != conv_id][:4]
    conn = db.init_db()
    try:
        if not db.get_conversation(conn, conv_id):
            raise HTTPException(404, "会话不存在")
    finally:
        conn.close()

    full_reply: list[str | None] = [None]

    def gen():
        conn2 = db.init_db()
        try:
            for kind, data in chat.chat_stream(conn2, message, conv_id=conv_id, link_conv_ids=link_conv_ids):
                if kind == "delta":
                    yield f"data: {json.dumps({'delta': data}, ensure_ascii=False)}\n\n"
                elif kind == "error":
                    yield f"data: {json.dumps({'error': data}, ensure_ascii=False)}\n\n"
                    return
                elif kind == "done":
                    full_reply[0] = data
                    yield f"data: {json.dumps({'done': True, 'reply': data}, ensure_ascii=False)}\n\n"
        finally:
            conn2.close()

    # 流结束后广播到 WS：注册到 BackgroundTasks，由 uvicorn 的 event loop
    # 负责调度（绝对不要在同步生成器内 get_event_loop）。
    reply_at_end = full_reply  # 闭包捕获
    conv_at_end = conv_id

    async def _broadcast_after_stream():
        reply = reply_at_end[0]
        if reply:
            try:
                await broadcast_to_ws(conv_at_end, "assistant", reply)
            except Exception:  # noqa: BLE001 — WS 失败不阻塞其他流程
                pass

    background.add_task(_broadcast_after_stream)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/chat/history")
def chat_history(limit: int = 20, conv_id: str | None = None):
    """取某会话最近闲聊记录（时间正序）；conv_id 缺省返回全部。"""
    conn = db.init_db()
    try:
        return {"messages": db.get_chat_history(conn, limit=limit, conv_id=conv_id)}
    finally:
        conn.close()


@app.get("/api/chat/profile")
def chat_profile():
    """查看数字人记住的用户档案（长期记忆）。"""
    conn = db.init_db()
    try:
        return {"profile": db.get_user_profile(conn)}
    finally:
        conn.close()


@app.delete("/api/chat/profile")
def chat_profile_clear():
    """清空数字人的用户档案（长期记忆）。"""
    conn = db.init_db()
    try:
        db.clear_user_profile(conn)
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/chat/history")
def chat_clear():
    """清空闲聊记录。"""
    conn = db.init_db()
    try:
        db.clear_chat_history(conn)
        return {"ok": True}
    finally:
        conn.close()


# ---------- WebSocket 实时推送 ----------

# 活跃的 WebSocket 连接（用于消息推送）
# asyncio.Lock 保护并发 append/remove/broadcast：同一 loop 内协程并发时
# 仍有 list iteration 修改的潜在风险（RuntimeError: list changed during iteration）。
_active_ws_connections: list[WebSocket] = []
_ws_lock = threading.Lock()
_WS_MAX = 100  # 单机最大连接数（防资源耗尽）
# 订阅表：websocket -> set(conv_id)；"__all__" 表示全部（兼容老客户端不订阅的行为）
_ws_subscriptions: dict[WebSocket, set[str]] = {}


def _ws_authenticated(websocket: WebSocket) -> bool:
    """WS 鉴权：MAESTRO_API_KEY 未设放行（开发）；已设则要求 ?key= 或首消息 {"key": ...}。"""
    api_key = os.environ.get("MAESTRO_API_KEY", "").strip()
    if not api_key:
        return True
    provided = websocket.query_params.get("key", "").strip()
    return bool(provided) and _compare_keys(provided, api_key)


@app.websocket("/ws")
async def ws_messages(websocket: WebSocket):
    """通用 WebSocket 端点：聊天/任务/审批事件推送。

    鉴权：设了 MAESTRO_API_KEY 时必须带 ?key=<key>。
    订阅协议：客户端发 {"action":"subscribe","conv_id":"..."} 订阅，
    {"action":"unsubscribe","conv_id":"..."} 退订；未订阅时默认收全部
    （"__all__"），首个 subscribe 后只收订阅的会话 + 广播类事件。
    """
    if not _ws_authenticated(websocket):
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
    with _ws_lock:
        if len(_active_ws_connections) >= _WS_MAX:
            await websocket.close(code=1013, reason="too many connections")
            return
        _active_ws_connections.append(websocket)
        _ws_subscriptions[websocket] = {"__all__"}
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            action = msg.get("action")
            if action not in ("subscribe", "unsubscribe"):
                continue
            cid = str(msg.get("conv_id") or "")[:64]
            if not cid:
                continue
            with _ws_lock:
                subs = _ws_subscriptions.get(websocket)
                if subs is None:
                    continue
                if action == "subscribe":
                    subs.discard("__all__")  # 显式订阅后不再全收
                    subs.add(cid)
                else:
                    subs.discard(cid)
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_lock:
            if websocket in _active_ws_connections:
                _active_ws_connections.remove(websocket)
            _ws_subscriptions.pop(websocket, None)


def _ws_wants(ws: WebSocket, conv_id: str) -> bool:
    """该连接是否应收到 conv_id 的事件（订阅表过滤）。"""
    subs = _ws_subscriptions.get(ws)
    if subs is None:
        return False
    return "__all__" in subs or conv_id in subs


async def _send_ws_safe(ws: WebSocket, msg: dict) -> bool:
    """单连接发送；失败返回 False（调用方负责摘除）。给慢客户端 3s 超时，
    避免一个卡死的连接阻塞整批推送。"""
    try:
        await asyncio.wait_for(ws.send_json(msg), timeout=3.0)
        return True
    except Exception:  # noqa: BLE001 — 超时/断开都按死连接处理
        return False


async def broadcast_to_ws(conv_id: str, role: str, content: str, *, kind: str = "chat") -> None:
    """向订阅了 conv_id 的连接推送事件；并发发送 + 慢客户端超时。

    kind: chat（聊天消息）/ task（任务状态）/ approval（审批请求）。
    广播类事件（kind=approval 未带 conv_id）走 "__all__"。
    """
    with _ws_lock:
        if not _active_ws_connections:
            return
        targets = [
            ws for ws in list(_active_ws_connections)
            if not conv_id or _ws_wants(ws, conv_id)
        ]
    if not targets:
        return
    msg = {"kind": kind, "conv_id": conv_id, "role": role, "content": content}
    results = await asyncio.gather(*(_send_ws_safe(ws, msg) for ws in targets))
    dead = [ws for ws, ok in zip(targets, results) if not ok]
    if dead:
        with _ws_lock:
            for ws in dead:
                if ws in _active_ws_connections:
                    _active_ws_connections.remove(ws)
                _ws_subscriptions.pop(ws, None)


def push_event_threadsafe(conv_id: str, role: str, content: str, *, kind: str = "task") -> None:
    """工作线程（编排器/沙箱）安全推送：把推送调度回事件循环。

    在 running loop 外直接 create_task 会 RuntimeError；用
    loop.call_soon_threadsafe 保证线程安全。无连接时是廉价 no-op。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(
            lambda: loop.create_task(broadcast_to_ws(conv_id, role, content, kind=kind))
        )
        return
    # 无运行中的 loop（如 TestClient 同步调用栈）：放到全局 loop（若有）
    global _main_loop
    if _main_loop is not None and _main_loop.is_running():
        _main_loop.call_soon_threadsafe(
            lambda: _main_loop.create_task(broadcast_to_ws(conv_id, role, content, kind=kind))
        )


_main_loop: asyncio.AbstractEventLoop | None = None


@app.on_event("startup")
async def _capture_main_loop():
    global _main_loop
    _main_loop = asyncio.get_running_loop()


@app.on_event("startup")
async def _prune_old_events():
    """启动时清理 30 天前的任务事件（task_events 无限增长的保留策略）。"""
    try:
        conn = db.init_db()
        try:
            db.prune_old_events(conn, days=30)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — 清理失败不影响启动
        pass


# ---- 审批实时推送：注册 sandbox 钩子（用户不用再等 2s 轮询，命令 120s 就超时）----
def _approval_push_hook(req) -> None:
    """sandbox.on_request 钩子：新审批请求 → WS 推送（工作线程里调，走 threadsafe）。"""
    push_event_threadsafe(
        getattr(req, "task_id", None) or "",
        "assistant",
        json.dumps({
            "approval_id": getattr(req, "id", ""),
            "task_id": getattr(req, "task_id", ""),
            "subtask_id": getattr(req, "subtask_id", ""),
            "cmd": getattr(req, "cmd", ""),
        }, ensure_ascii=False),
        kind="approval",
    )


sandbox.on_request(_approval_push_hook)


@app.websocket("/ws/task/{task_id}")
async def ws_task(websocket: WebSocket, task_id: str):
    if not _ws_authenticated(websocket):
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
    try:
        while True:
            # 同步 DB/文件 IO 放线程池：否则每个 WS tick 都阻塞整个事件循环
            snap = await asyncio.to_thread(_snapshot, task_id)
            if snap is None:
                await websocket.send_json({"error": "task_not_found"})
                break
            await websocket.send_json(snap)
            status = snap["task"]["status"]
            if status in (db.DONE, db.FAILED):
                # 终态再推一次后保持连接短暂存活，前端自行关闭
                await asyncio.sleep(_POLL_INTERVAL)
                await websocket.send_json(_snapshot(task_id))
                break
            await asyncio.sleep(_POLL_INTERVAL)
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001 — WS 异常不拖垮服务
        return


# ---------- 壁纸预览 ----------


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
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("maestro.server:app", host="127.0.0.1", port=8787, reload=False)
