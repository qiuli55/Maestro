"""数字人闲聊 REST API（含 SSE 流式 + WS 广播）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse

from .. import db
from .deps import broadcast_to_ws
from .. import chat

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
            except Exception:  # noqa: BLE001 — WS 失败不阻塞其他流程
                pass

    background.add_task(_broadcast_after_stream)
    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/api/chat/history")
def chat_history(limit: int = 20, conv_id: str | None = None):
    """取某会话最近闲聊记录（时间正序）；conv_id 缺省返回全部。"""
    conn = db.init_db()
    try:
        return {"messages": db.get_chat_history(conn, limit=limit, conv_id=conv_id)}
    finally:
        conn.close()


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
