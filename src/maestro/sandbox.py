"""安全沙箱审批中心（WorkBuddy 模型：默认受限，越权必须用户批准）。

命令三档分级（配合 runcmd.validate）：
- allowed：白名单只读命令，直接执行
- approval：写/改/删/安装类命令 -> 进审批队列 -> 用户批准才执行（默认 120s 超时）
- blocked：灾难性命令（格式化/关机/注册表/命令链逃逸），直接拒绝，不进审批

线程模型：
- worker 线程（embedded 的 tool_executor 线程）调 request_approval()：
  注册请求（SQLite 持久化 + 内存 Event）后阻塞等待用户决定
- server 的审批 API 调 decide() -> set Event 唤醒
- 超时未决 -> timedout，放弃执行（审批请求会过期，不留僵尸等待）

SQLite 只做持久化展示（快照/API 可见），内存 Event 做跨线程唤醒。
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import db

# 审批默认超时（秒）。环境变量可调，测试用短超时。
DEFAULT_APPROVAL_TIMEOUT = int(os.environ.get("MAESTRO_APPROVAL_TIMEOUT", "120"))

# 分级结果
ALLOWED = "allowed"
APPROVAL = "approval"
BLOCKED = "blocked"


@dataclass
class ApprovalRequest:
    id: str
    task_id: str
    cmd: str
    subtask_id: str | None
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    decision: str = "pending"  # pending / approved / rejected（超时仍为 pending，调用方判 timedout）
    event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)


# 进程内注册表：approval_id -> ApprovalRequest。SQLite 持久化展示，Event 做唤醒。
_REGISTRY: dict[str, ApprovalRequest] = {}
_LOCK = threading.Lock()

# 审批请求产生时的通知钩子（server 可注册：往 WS 推一条事件）
_notify_hooks: list = []


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 线程级 SQLite 连接（worker 线程频繁调 _persist/list_pending，
# 每次开关连接开销大且触发 database is locked）。
_TLS = threading.local()


def _get_conn():
    """取当前线程的 SQLite 连接（懒初始化，跨线程独立）。

    响应 MAESTRO_DB 切换：env 变时丢弃旧连接重开（测试隔离 + 多租户未来需要）。
    """
    import os as _os

    cur_path = _os.environ.get("MAESTRO_DB", "")
    cache_path = getattr(_TLS, "db_path", None)
    if cache_path != cur_path:
        # env 变了（测试隔离 / 多库切换）→ 关闭旧连接重开
        old = getattr(_TLS, "conn", None)
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        _TLS.conn = None
        _TLS.db_path = cur_path
    conn = getattr(_TLS, "conn", None)
    if conn is None:
        conn = db.init_db()
        _TLS.conn = conn
        _TLS.db_path = cur_path
    return conn


def _persist(req: ApprovalRequest, status: str) -> None:
    """审批记录 upsert 到 SQLite。失败不阻断主流程（内存 Event 才是审批主干）。

    用 thread-local 连接复用：worker 线程高频调 _persist 时不反复开关连接。
    SQLite 连接生命周期与线程一致——worker 线程关闭时连接随之释放，无需显式 close。
    """
    try:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO approvals (id, task_id, subtask_id, cmd, status, created_at, decided_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   status=excluded.status, decided_at=excluded.decided_at""",
            (
                req.id,
                req.task_id,
                req.subtask_id,
                req.cmd,
                status,
                req.created_at,
                _now() if status != "pending" else None,
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — 持久化失败可降级为纯内存审批
        # 连接异常时丢弃，下次 _get_conn 重建
        try:
            if getattr(_TLS, "conn", None) is not None:
                _TLS.conn.close()
        except Exception:
            pass
        _TLS.conn = None


def on_request(hook) -> None:
    """注册钩子：新审批请求产生时调用 hook(req)（如 server 向 WS 推送）。"""
    _notify_hooks.append(hook)


def _notify(req: ApprovalRequest) -> None:
    for hook in list(_notify_hooks):
        try:
            hook(req)
        except Exception:  # noqa: BLE001 — 钩子异常不影响审批主流程
            pass


def list_pending(task_id: str | None = None) -> list[dict]:
    """取待审批请求列表（供任务快照 / API 展示）。task_id 缺省返回全部。"""
    conn = db.init_db()
    try:
        if task_id:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE task_id=? AND status='pending' ORDER BY created_at",
                (task_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def request_approval(
    cmd: str,
    task_id: str,
    subtask_id: str | None = None,
    timeout: int = DEFAULT_APPROVAL_TIMEOUT,
) -> str:
    """注册审批请求并阻塞等待用户决定。返回 decision：approved / rejected / timedout。

    阻塞期间 worker 线程挂起，用户通过 server API 批准/拒绝（decide 唤醒）。
    超时返回 timedout（不执行），审批记录标记 timedout。
    """
    req = ApprovalRequest(
        id=f"ap_{uuid.uuid4().hex[:8]}",
        task_id=task_id,
        cmd=cmd,
        subtask_id=subtask_id,
        created_at=_now(),
    )
    with _LOCK:
        _REGISTRY[req.id] = req
    _persist(req, "pending")
    _notify(req)

    req.event.wait(timeout)
    with _LOCK:
        _REGISTRY.pop(req.id, None)
        decision = req.decision
    if decision == "pending":
        _persist(req, "timedout")
        return "timedout"
    _persist(req, decision)
    return decision


def decide(approval_id: str, approve: bool) -> bool:
    """用户决定：approve=True 放行，False 拒绝。返回是否找到并生效。

    找到后从注册表摘除并持久化结果，二次 decide 同一 id 返回 False（防重复处理）。
    """
    with _LOCK:
        req = _REGISTRY.pop(approval_id, None)
        if not req:
            return False
        req.decision = "approved" if approve else "rejected"
        req.event.set()
    _persist(req, req.decision)
    return True
