"""数字人闲聊 REST API（含 SSE 流式 + WS 广播）。"""
from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse

from .. import db
from .deps import broadcast_to_ws
from .. import chat

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/chat")
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


@router.post("/api/chat/stream")
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
            except Exception as e:  # noqa: BLE001 — WS 失败不阻塞其他流程
                logger.warning("流式结束后 WS 广播失败 conv=%s: %s", conv_at_end, e)

    background.add_task(_broadcast_after_stream)
    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/api/chat/history")
def chat_history(limit: int = 20, conv_id: str | None = None, since_id: int | None = None):
    """取某会话最近闲聊记录（时间正序）。

    ?since_id=N 增量同步：只返回 id > N 的消息（前端轮询/补漏用）。
    conv_id 缺省返回全部。
    """
    conn = db.init_db()
    try:
        return {"messages": db.get_chat_history(conn, limit=limit, conv_id=conv_id, since_id=since_id)}
    finally:
        conn.close()


@router.get("/api/chat/router")
def chat_router_info():
    """前端展示当前可路由模型 + 自动路由开关状态。

    自动路由：
    - MAESTRO_MODEL 未设 → 自动按 prompt 选 chat/code/reason
    - MAESTRO_MODEL 已设 → 固定用该模型（用户全局偏好）
    """
    from .. import model_router

    return {
        "auto_routed": model_router.is_model_auto_routed(),
        "models": model_router.available_models(),
        "default_model": os.environ.get("MAESTRO_MODEL", "").strip() or None,
    }


@router.get("/api/chat/feed")
def chat_feed(conv_id: str, since_id: int = 0, poll: float = 1.0):
    """增量 SSE 长轮询：客户端 EventSource 订阅，服务器发现 id > since_id
    的新消息就推一行，60s 主动 ping 保持连接；客户端断线后用 since_id 重连。

    比前端 1.5s 轮询 /api/chat/history 减少 ~90% 请求；WS 路径已存在但只推
    kind=chat/task/approval，缺"未推前"的历史回填场景——这条专门服务补漏。
    """
    import json
    import time as _time
    from fastapi.responses import StreamingResponse

    if poll < 0.1 or poll > 10:
        poll = 1.0
    if since_id < 0:
        since_id = 0

    def gen():
        last_id = since_id
        last_ping = _time.monotonic()
        # 拉一次"立即"补漏（since_id 之后所有消息），再进入轮询
        conn = db.init_db()
        try:
            rows = db.get_chat_history(conn, limit=200, conv_id=conv_id, since_id=last_id)
            for r in rows:
                payload = json.dumps({"id": r["id"], "role": r["role"], "content": r["content"],
                                      "created_at": r["created_at"]}, ensure_ascii=False)
                yield f"event: msg\ndata: {payload}\n\n"
                last_id = max(last_id, r["id"])
        finally:
            conn.close()
        # 长轮询：每秒查一次，每次最多发新批
        idle_ticks = 0
        while idle_ticks < 60 * 10:  # 10 分钟内无活动则关闭（防僵尸）
            conn = db.init_db()
            try:
                rows = db.get_chat_history(conn, limit=50, conv_id=conv_id, since_id=last_id)
            finally:
                conn.close()
            if rows:
                for r in rows:
                    payload = json.dumps({"id": r["id"], "role": r["role"], "content": r["content"],
                                          "created_at": r["created_at"]}, ensure_ascii=False)
                    yield f"event: msg\ndata: {payload}\n\n"
                    last_id = max(last_id, r["id"])
                idle_ticks = 0
            else:
                idle_ticks += 1
            # 每 30s 主动 ping 一次（防中间代理/反代超时）
            now = _time.monotonic()
            if now - last_ping > 30:
                yield ": ping\n\n"
                last_ping = now
            _time.sleep(poll)

    return StreamingResponse(gen(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/chat/profile")
def chat_profile():
    """查看数字人记住的用户档案（长期记忆）。"""
    conn = db.init_db()
    try:
        return {"profile": db.get_user_profile(conn)}
    finally:
        conn.close()


@router.delete("/api/chat/profile")
def chat_profile_clear():
    """清空数字人的用户档案（长期记忆）。"""
    conn = db.init_db()
    try:
        db.clear_user_profile(conn)
        return {"ok": True}
    finally:
        conn.close()


@router.delete("/api/chat/history")
def chat_clear():
    """清空闲聊记录。"""
    conn = db.init_db()
    try:
        db.clear_chat_history(conn)
        return {"ok": True}
    finally:
        conn.close()
