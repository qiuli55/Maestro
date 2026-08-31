"""数字人闲聊：人格 System Prompt + DeepSeek + 多轮历史上下文 + 长期记忆。

闲聊记录存 SQLite（chat_messages 表），每次对话取最近 N 轮拼接进上下文。
长期记忆：从用户消息中提取关键信息（名字/身份/喜欢/讨厌）存入 user_profile 表，
跨会话记住——"芙莉莲真的记得你"。
安全：用户输入直接作为 user 消息，不做任何指令注入；人格 prompt 从 configs 读。
"""
from __future__ import annotations

import os
import re
import sqlite3

from . import config, db, llm

HISTORY_TURNS = 8  # 带入上下文的最近轮数（user+assistant 各 1 条为 1 轮）

# 用户档案提取规则：(档案 key, 正则)。值按标点/连接词截断，只记第一段。
PROFILE_PATTERNS: list[tuple[str, str]] = [
    ("name", r"我的名字(?:是|叫)([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_·]{0,11})"),
    ("name", r"我叫([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_·]{0,11})"),
    ("identity", r"我是(?:一名|一个|个|做)?([\u4e00-\u9fa5A-Za-z0-9]{1,14}?(?:学生|工程师|程序员|开发|设计师|运营|测试|实习|老师|医生|律师|会计|产品))"),
    ("like", r"(?:我)?(?:很|最)?喜欢([^，。！？!?、,;；\s]{2,20})"),
    ("like", r"(?:我)?最爱([^，。！？!?、,;；\s]{2,20})"),
    ("dislike", r"(?:我)?(?:很)?讨厌([^，。！？!?、,;；\s]{2,20})"),
    ("dislike", r"(?:我)?不喜欢([^，。！？!?、,;；\s]{2,20})"),
]

_PROFILE_LABELS = {"name": "名字", "identity": "身份", "like": "喜欢", "dislike": "讨厌"}


def _clean(value: str) -> str:
    """按标点/连接词截断，取第一段（"写代码和二次元"→"写代码"）。"""
    return re.split(r"[，。！？!?、,;；\s]|和|与|以及|跟|或", value.strip())[0][:20].strip()


def _extract_profile(conn: sqlite3.Connection, message: str) -> None:
    """从用户消息里提取关键信息存入 user_profile（覆盖式）。"""
    for key, pattern in PROFILE_PATTERNS:
        m = re.search(pattern, message)
        if not m:
            continue
        value = _clean(m.group(1))
        if len(value) >= 2:
            db.set_user_profile(conn, key, value)


def _profile_block(conn: sqlite3.Connection) -> str:
    """把记住的档案拼成一段 system 附加内容；没记住就不注入。"""
    prof = db.get_user_profile(conn)
    if not prof:
        return ""
    lines = []
    for key, label in _PROFILE_LABELS.items():
        if key in prof:
            lines.append(f"- {label}：{prof[key]}")
    if not lines:
        return ""
    return (
        "【关于你的主人】（芙莉莲记得的，跨会话有效；已经知道的事不要反问）\n"
        + "\n".join(lines)
        + "\n"
    )


def _system_prompt() -> str:
    return (config.PROMPTS.get("chat_system") or "").strip() or (
        "你是 Maestro，住在用户桌面壁纸里的二次元数字人助手。语气轻松亲切，中文为主，回答简洁。"
    )


def _linked_context_block(conn: sqlite3.Connection, link_conv_ids) -> str:
    """把关联会话的最近几轮拼成参考上下文注入 system。

    防失控上限：最多 4 个会话 x 6 条 x 每条 300 字。会话不存在或无历史则跳过。
    """
    if not link_conv_ids:
        return ""
    blocks: list[str] = []
    for cid in list(link_conv_ids)[:4]:
        conv = db.get_conversation(conn, cid)
        if not conv:
            continue
        hist = db.get_chat_history(conn, limit=6, conv_id=cid)
        lines = []
        for h2 in hist:
            if h2["role"] not in ("user", "assistant"):
                continue
            who = "用户" if h2["role"] == "user" else "你"
            lines.append(f"{who}: {str(h2['content'])[:300]}")
        if not lines:
            continue
        title = str(conv.get("title") or cid).strip()[:20]
        blocks.append(f"【对话「{title}」最近内容】\n" + "\n".join(lines))
    if not blocks:
        return ""
    return (
        "\n\n【其他关联对话的上下文】（以下是你同时参与的其他对话的近期内容，"
        "仅在与当前话题相关时自然引用，不要主动罗列）\n" + "\n\n".join(blocks) + "\n"
    )


def chat_stream(
    conn: sqlite3.Connection,
    message: str,
    conv_id: str = db.DEFAULT_CONV_ID,
    model: str | None = None,
    link_conv_ids: list[str] | None = None,
):
    """流式闲聊（打字机效果）：yield ("delta", text) 增量 / ("error", msg) / ("done", reply)。

    与 chat() 相同的前置（档案/历史/存 user 消息），LLM stream=True 逐块 yield；
    assistant 完整回复在流结束时存库（中断则丢弃本次回复）。
    """
    message = (message or "").strip()
    if not message:
        raise ValueError("消息不能为空")

    _extract_profile(conn, message)

    system = _system_prompt()
    profile = _profile_block(conn)
    if profile:
        system = system + "\n\n" + profile
    # 跨对话关联：把用户勾选的其他会话最近几轮注入 system 作为参考上下文
    linked = _linked_context_block(conn, link_conv_ids)
    if linked:
        system = system + linked

    history = db.get_chat_history(conn, limit=HISTORY_TURNS * 2, conv_id=conv_id)
    messages = [{"role": "system", "content": system}]
    for h in history:
        if h["role"] in ("user", "assistant"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    db.add_chat_message(conn, "user", message, conv_id=conv_id)
    db.touch_conversation(conn, conv_id)

    model = model or os.environ.get("MAESTRO_MODEL", "deepseek-chat")
    # 把 model 解析为 (provider, model_name)，按 provider 选 base_url/api_key。
    # 之前 llm.get_client() 不传 provider → 默认 DeepSeek，kimi/anthropic 等走错端点。
    # 测试桩可能传 0-arg lambda，所以 get_client 需兼容无参调用（默认 provider）。
    provider, model_name = llm._resolve(model)
    try:
        client, default_model = llm.get_client(provider)
    except TypeError:
        # 测试桩：lambda 无参 → 兼容旧调用
        client, default_model = llm.get_client(), model_name
    model_name = model_name or default_model
    parts: list[str] = []
    try:
        stream = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.8,
            stream=True,
        )
        for chunk in stream:
            delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            if delta:
                parts.append(delta)
                yield ("delta", delta)
    except Exception as e:  # noqa: BLE001 — 流中断不拖垮服务
        yield ("error", f"流式回复失败：{e}")
        return

    reply = "".join(parts).strip()
    if not reply:
        reply = "（我卡了一下，再说一遍嘛）"
    db.add_chat_message(conn, "assistant", reply, conv_id=conv_id)
    yield ("done", reply)


def chat(
    conn: sqlite3.Connection,
    message: str,
    conv_id: str = db.DEFAULT_CONV_ID,
    model: str | None = None,
    link_conv_ids: list[str] | None = None,
) -> str:
    """非流式闲聊（兼容/测试用）：内部走 chat_stream，累积完整回复。"""
    reply = ""
    for kind, data in chat_stream(conn, message, conv_id=conv_id, model=model, link_conv_ids=link_conv_ids):
        if kind == "error":
            raise RuntimeError(data)
        if kind == "done":
            reply = data
    return reply
