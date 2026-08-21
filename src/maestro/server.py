"""Maestro Web 服务（P2 任务窗口）：FastAPI + WebSocket 实时推送。

前端内页面在 web/ 目录（仪表盘 / 任务详情 / 壁纸预览）。
编排器 run_task 是同步阻塞的（场景 B 还含 LLM 汇总），故用后台线程执行，
WebSocket 每 ~1.2s 轮询 SQLite 把最新状态推给前端。

启动：python -m maestro.server   或   maestro serve
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.responses import FileResponse, Response, StreamingResponse
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
from fastapi.middleware.cors import CORSMiddleware

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
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=False,  # 无 cookie 鉴权，避免 CSRF
)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="maestro-task")

_POLL_INTERVAL = 1.2  # WS 轮询间隔（秒）


# ---------- 后台任务执行 ----------

def _prepare_job(task_id: str, prompt: str, scenario: str, worker_type: str,
                 parallel: bool, model: str | None, no_merge: bool = False,
                 confirm: bool = True, conv_id: str | None = None,
                 selected_workers: list[str] | None = None):
    """后台线程：拆分+入库，停在 READY 等用户确认；confirm=False 则立即执行。"""
    conn = db.init_db()
    selected_workers = selected_workers or ["embedded"]
    try:
        task_id_out = orchestrator.prepare_task(
            conn, prompt, scenario=scenario, worker_type=worker_type,
            parallel=parallel, model=model, task_id=task_id, no_merge=no_merge,
            conv_id=conv_id,
        )
        # 多 agent round-robin 分配
        if len(selected_workers) > 1:
            subtasks = db.get_subtasks(conn, task_id_out)
            for i, st in enumerate(subtasks):
                wt = selected_workers[i % len(selected_workers)]
                conn.execute(
                    "UPDATE subtasks SET worker_type=? WHERE id=?",
                    (wt, st["id"])
                )
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
    """后台线程：确认后执行完整链路。"""
    conn = db.init_db()
    try:
        orchestrator.execute_task(conn, task_id, timeout=timeout)
    finally:
        conn.close()


def _retry_job(subtask_id: str, timeout: int = 600):
    conn = db.init_db()
    try:
        orchestrator.retry_subtask(conn, subtask_id, timeout=timeout)
    finally:
        conn.close()


def _resume_job(task_id: str, timeout: int = 600):
    conn = db.init_db()
    try:
        orchestrator.resume_incomplete(conn, task_id, timeout=timeout)
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
    "embedded":  {"label": "内置编排器", "desc": "本地 LLM，内置文件工具，适合轻量任务"},
    "opencode":  {"label": "OpenCode",   "desc": "深度编码 agent，适合代码生成/重构"},
    "octo":      {"label": "Octo",       "desc": "Headless 编码 agent，走 DeepSeek"},
    "workbuddy": {"label": "WorkBuddy",  "desc": "全能工具型 agent，适合复杂工作流"},
    "minimax":   {"label": "MiniMax",    "desc": "图片/视频/语音生成，适合创意任务"},
    "codebuddy": {"label": "CodeBuddy",  "desc": "腾讯云编码 agent（需实名认证）"},
}


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
    for k in (keys_data.get("keys") or []):
        env_var = k.get("env_var", "")
        configured = bool(env_var and os.environ.get(env_var))
        out.append({
            "id": k.get("id"),
            "label": k.get("label", env_var),
            "provider": k.get("provider", ""),
            "models": k.get("models", []),
            "configured": configured,
        })
    return out


@app.get("/api/models")
def list_models():
    """返回所有已配置的模型（环境变量存在才返回）。"""
    from . import config as _cfg
    return _cfg.get_available_models()


@app.post("/api/tasks")
async def create_task(payload: dict):
    prompt = (payload.get("prompt") or "").strip()
    scenario = payload.get("scenario", "a")
    worker_type = payload.get("worker_type", "embedded")
    parallel = bool(payload.get("parallel", scenario != "a"))
    no_merge = bool(payload.get("no_merge", False))
    model = payload.get("model") or None
    # 模型 → 对应 API key 环境变量（供 LLM 调用时读取）
    if model:
        from . import config as _cfg
        key_env = _cfg.resolve_model_to_key(model)
        if key_env:
            os.environ["ACTIVE_API_KEY"] = os.environ.get(key_env, "")
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
    if scenario not in ("a", "b", "c", "auto"):
        raise HTTPException(400, "scenario 必须是 a / b / c / auto")
    # 用第一个 agent 拆分任务，后续 round-robin 分配
    if worker_type not in valid_workers:
        worker_type = selected_workers[0]

    detect_reason = None
    if scenario == "auto":
        # 决策：LLM 识别输入类型 → 路由到 a/b/c
        try:
            scenario, detect_reason = split.detect_scenario(prompt, model=model)
        except Exception as e:  # noqa: BLE001 — 识别失败退回场景 A，不让任务失败
            scenario, detect_reason = "a", f"auto 识别失败回退 a: {type(e).__name__}"

    # 预先生成 task_id 立即返回；拆分交给后台线程
    task_id = f"task_{os.urandom(4).hex()}"
    _executor.submit(_prepare_job, task_id, prompt, scenario, worker_type, parallel, model, no_merge, confirm, conv_id, selected_workers)
    return {"task_id": task_id, "confirm": confirm, "scenario": scenario, "detect_reason": detect_reason, "selected_workers": selected_workers, "model": model}


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
    _executor.submit(_execute_job, task_id)
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
    _executor.submit(_resume_job, task_id)
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
    _executor.submit(_retry_job, subtask_id)
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
            for kind, data in chat.chat_stream(conn2, message, conv_id=conv_id):
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


@app.websocket("/ws")
async def ws_messages(websocket: WebSocket):
    """通用 WebSocket 端点：推送聊天消息和任务结果到前端。根据 conv_id 路由消息到对应窗口。"""
    await websocket.accept()
    with _ws_lock:
        if len(_active_ws_connections) >= _WS_MAX:
            await websocket.close(code=1013, reason="too many connections")
            return
        _active_ws_connections.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # 客户端可以发送 {"action": "subscribe", "conv_id": "xxx"} 来订阅特定会话
            # 目前不需要客户端消息，只接收即可
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_lock:
            if websocket in _active_ws_connections:
                _active_ws_connections.remove(websocket)


async def broadcast_to_ws(conv_id: str, role: str, content: str):
    """向所有 WebSocket 连接广播消息（前端会根据 conv_id 路由）。"""
    # 拷贝副本（在锁内），避免迭代期间其他协程修改列表。
    with _ws_lock:
        if not _active_ws_connections:
            return
        targets = list(_active_ws_connections)
    msg = {"conv_id": conv_id, "role": role, "content": content}
    dead: list[WebSocket] = []
    for ws in targets:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    if dead:
        with _ws_lock:
            for ws in dead:
                if ws in _active_ws_connections:
                    _active_ws_connections.remove(ws)


@app.websocket("/ws/task/{task_id}")
async def ws_task(websocket: WebSocket, task_id: str):
    await websocket.accept()
    try:
        while True:
            snap = _snapshot(task_id)
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
    fp = WALLPAPER_DIR / "index.html"
    if not fp.exists():
        raise HTTPException(404, "壁纸文件不存在")
    return FileResponse(fp)


# 壁纸目录整体静态挂载：/wallpaper/frieren.html、/wallpaper/models/...、/wallpaper/vendor/...
# 都能通过 8787 访问（Wallpaper Engine 长期使用 / 开发预览用）。
# 注意：@app.get("/wallpaper") 精确路由在前，/wallpaper 仍返回 index.html；
# /wallpaper/xxx 走这个挂载。
if WALLPAPER_DIR.exists():
    app.mount("/wallpaper", StaticFiles(directory=str(WALLPAPER_DIR), html=True),
              name="wallpaper_static")


# ---------- 静态前端（最后挂载，避免拦截 /api 与 /ws）----------

if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("maestro.server:app", host="127.0.0.1", port=8787, reload=False)
