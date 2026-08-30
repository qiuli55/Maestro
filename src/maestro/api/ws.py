"""WebSocket 端点：通用推送通道 + 任务快照轮询。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import db
from .deps import (
    _POLL_INTERVAL,
    _WS_MAX,
    _active_ws_connections,
    _snapshot,
    _ws_authenticated,
    _ws_lock,
    _ws_subscriptions,
)

router = APIRouter()


@router.websocket("/ws")
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

@router.websocket("/ws/task/{task_id}")
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

