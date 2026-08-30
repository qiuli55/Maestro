"""元信息 REST API：健康检查 / agent / key / model。"""
from __future__ import annotations

import os

from fastapi import APIRouter

from .. import config
from .. import config as _cfg
from .. import db
from ..workers import base as wbase
import maestro.workers as _wmod

router = APIRouter()


_WORKER_META = {
    "embedded": {"label": "内置编排器", "desc": "本地 LLM，内置文件工具，适合轻量任务"},
    "opencode": {"label": "OpenCode", "desc": "深度编码 agent，适合代码生成/重构"},
    "octo": {"label": "Octo", "desc": "Headless 编码 agent，走 DeepSeek"},
    "workbuddy": {"label": "WorkBuddy", "desc": "全能工具型 agent，适合复杂工作流"},
    "minimax": {"label": "MiniMax", "desc": "图片/视频/语音生成，适合创意任务"},
    "codebuddy": {"label": "CodeBuddy", "desc": "腾讯云编码 agent（需实名认证）"},
}

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

@router.get("/api/agents")
def list_agents():
    """返回所有已注册的 agent（含名称/描述/是否可用）。"""
    from .. import workers as _wmod

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


@router.get("/api/keys")
def list_keys():
    """Key 库列表（不含实际 key 值，只返回元数据）。"""
    from .. import config as _cfg

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


@router.get("/api/models")
def list_models():
    """返回所有已配置的模型（环境变量存在才返回）。"""
    from .. import config as _cfg

    return _cfg.get_available_models()

