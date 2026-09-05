"""任务 REST API：创建/列表/编辑/执行/取消/恢复/重试/审批。"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException

from .. import db
from .deps import _executor_fast, _executor_slow, _snapshot
from .. import orchestrator, sandbox, split
import maestro.workers as _wmod
from ..workers import base as wbase

logger = logging.getLogger(__name__)

router = APIRouter()


def _db_path(conn):
    return conn.execute("PRAGMA database_list").fetchone()[2]

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
        except Exception as db_e:  # noqa: BLE001 — 错误落库再失败，只留日志
            logger.error("重派失败且错误落库也失败 subtask=%s: 重派错=%s 落库错=%s",
                         subtask_id, e, db_e)
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


@router.get("/api/healthz", include_in_schema=False)
def healthz():
    """轻量 liveness 探针：进程是否在响应（不查依赖）。

    用于 K8s livenessProbe / Docker HEALTHCHECK / 负载均衡器探活。
    """
    return {"status": "ok"}


@router.get("/api/ready")
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


@router.get("/api/workers/health")
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


@router.get("/api/tasks")
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
    """用户工作流定义 → 有序启用卡片（跳过禁用卡片），带 stage 序号与 use_prev。"""
    subs = []
    for stage_idx, st in enumerate((definition or {}).get("stages") or []):
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
                "stage": stage_idx,
                "use_prev": bool(c.get("use_prev")),
            })
    return subs

def apply_workflow_defaults(payload: dict) -> dict:
    """工作流预设填充：configs/workflows.json 提供 scenario/parallel/agents/model 等默认值，
    payload 里显式传入的字段优先（前端可对工作流做逐项覆盖）。

    未知工作流抛 ValueError（由调用方转 400）。
    """
    wf_id = payload.get("workflow")
    if not wf_id:
        return payload
    from .. import config as _cfg

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

@router.get("/api/tasks")
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

def _resolve_custom_subtasks(payload: dict, wf_id) -> list | None:
    """工作流编排：解析自定义子任务卡片，客户端直提优先（跳过 LLM 拆分）。

    payload.subtasks 优先；否则按 workflow id 展开用户自建工作流的启用卡片。
    """
    custom = payload.get("subtasks")
    if custom is not None and not isinstance(custom, list):
        raise HTTPException(400, "subtasks 必须是数组")
    if custom is None and wf_id:
        # 用户自建工作流：按 id 展开启用卡片
        conn = db.init_db()
        try:
            uw = db.get_user_workflow(conn, str(wf_id))
        finally:
            conn.close()
        if uw is not None:
            custom = _workflow_to_subtasks(uw["definition"])
            if not custom:
                raise HTTPException(400, "该工作流没有启用的卡片")
    return custom


def _normalize_task_args(payload: dict, custom_subtasks, valid_workers: list[str]) -> dict:
    """校验并归一任务参数（scenario/worker/parallel 等），返回整理后的字典。"""
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    raw_scenario = payload.get("scenario", "a")
    scenario = "custom" if custom_subtasks is not None else raw_scenario
    if custom_subtasks is None and scenario not in ("a", "b", "c", "auto"):
        raise HTTPException(400, "scenario 必须是 a / b / c / auto")
    # 多 agent 选择（支持同时派给多个 agent）：至少选一个；不合法/不可用的过滤掉
    selected = [w for w in (payload.get("selected_workers") or []) if w in valid_workers]
    if not selected:  # 没传或全不合法，退回 embedded
        selected = ["embedded"]
    worker_type = payload.get("worker_type", "embedded")
    if worker_type not in valid_workers:
        worker_type = selected[0]  # 用第一个 agent 拆分任务，后续 round-robin 分配
    return {
        "prompt": prompt,
        "scenario": scenario,
        "worker_type": worker_type,
        # parallel 默认按用户传入的原始 scenario 判断（custom 覆盖不影响默认值）
        "parallel": bool(payload.get("parallel", raw_scenario != "a")),
        "no_merge": bool(payload.get("no_merge", False)),
        "model": payload.get("model") or None,
        "confirm": bool(payload.get("confirm", True)),
        "conv_id": payload.get("conv_id") or None,
        "selected_workers": selected,
    }


@router.post("/api/tasks")
async def create_task(payload: dict):
    """创建任务：预生成 task_id 立即返回，拆分/执行交后台线程（人工闸门可选）。"""
    try:
        payload = apply_workflow_defaults(payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    wf_id = payload.get("workflow")
    custom_subtasks = _resolve_custom_subtasks(payload, wf_id)
    # key 不在这里注入 env：llm.get_client 每次按 provider 从 providers.json
    # 的 api_key_env 现查，并发任务互不影响（进程级 env 会让不同模型任务互相覆盖 key）。
    valid_workers = _wmod.available_workers()
    args = _normalize_task_args(payload, custom_subtasks, valid_workers)

    detect_reason = None
    if args["scenario"] == "auto":
        # 决策：LLM 识别输入类型 → 路由到 a/b/c
        try:
            args["scenario"], detect_reason = split.detect_scenario(args["prompt"], model=args["model"])
        except Exception as e:  # noqa: BLE001 — 识别失败退回场景 A，不让任务失败
            args["scenario"], detect_reason = "a", f"auto 识别失败回退 a: {type(e).__name__}"

    # 预先生成 task_id 立即返回；拆分交给后台线程
    task_id = f"task_{os.urandom(4).hex()}"
    _executor_fast.submit(
        _prepare_job,
        task_id,
        args["prompt"],
        args["scenario"],
        args["worker_type"],
        args["parallel"],
        args["model"],
        args["no_merge"],
        args["confirm"],
        args["conv_id"],
        args["selected_workers"],
        custom_subtasks,
    )
    return {
        "task_id": task_id,
        "confirm": args["confirm"],
        "scenario": args["scenario"],
        "detect_reason": detect_reason,
        "selected_workers": args["selected_workers"],
        "model": args["model"],
        "workflow": wf_id,
    }


@router.put("/api/tasks/{task_id}/subtasks")
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


@router.post("/api/tasks/{task_id}/execute")
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


@router.delete("/api/tasks/{task_id}")
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


@router.post("/api/tasks/{task_id}/cancel")
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


@router.post("/api/tasks/{task_id}/resume")
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


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    snap = _snapshot(task_id)
    if not snap:
        raise HTTPException(404, "任务不存在")
    return snap


@router.post("/api/tasks/{task_id}/retry/{subtask_id}")
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


@router.post("/api/tasks/{task_id}/approvals/{approval_id}")
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

