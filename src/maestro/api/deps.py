"""共享基础设施：连接池/线程池、任务快照、WS 广播与订阅、审批推送钩子。"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

from fastapi import WebSocket

from .. import db  # noqa: F401  （保持 db 导入语义）
from .. import sandbox


def _compare_keys(provided: str, expected: str) -> bool:
    """常量时间比较，防时序攻击。"""
    import hmac

    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


# ====== 后台线程池 ======
# 快/慢池分离：prepare（拆分，毫秒级）走快池；execute/retry/resume（长任务，
# 最长 600s）走慢池。共用一个池时，4 个长任务会把新任务的拆分也饿死。
from concurrent.futures import ThreadPoolExecutor

_executor_fast = ThreadPoolExecutor(max_workers=4, thread_name_prefix="maestro-fast")
_executor_slow = ThreadPoolExecutor(max_workers=2, thread_name_prefix="maestro-slow")

_POLL_INTERVAL = 1.2  # WS 轮询间隔（秒）


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


# ====== WebSocket 实时推送（活跃连接 / 订阅表 / 广播 / 线程安全推送） ======
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


async def capture_main_loop() -> None:
    global _main_loop
    _main_loop = asyncio.get_running_loop()


async def prune_old_events_startup() -> None:
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
