from datetime import datetime, UTC
"""数字人闲聊测试：历史存取、chat 调用、API 路由。"""
from fastapi.testclient import TestClient

from maestro import chat, db
from maestro.server import app


def test_add_and_get_history(tmp_db):
    db.add_chat_message(tmp_db, "user", "你好")
    db.add_chat_message(tmp_db, "assistant", "嗨！")
    h = db.get_chat_history(tmp_db)
    assert [(m["role"], m["content"]) for m in h] == [("user", "你好"), ("assistant", "嗨！")]


def test_history_limit(tmp_db):
    for i in range(5):
        db.add_chat_message(tmp_db, "user", f"m{i}")
    h = db.get_chat_history(tmp_db, limit=3)
    assert [m["content"] for m in h] == ["m2", "m3", "m4"]


def test_clear_history(tmp_db):
    db.add_chat_message(tmp_db, "user", "x")
    db.clear_chat_history(tmp_db)
    assert db.get_chat_history(tmp_db) == []


def test_chat_rejects_empty(tmp_db):
    try:
        chat.chat(tmp_db, "   ")
        assert False, "空消息应抛错"
    except ValueError:
        pass


def test_chat_roundtrip(tmp_db, monkeypatch):
    """chat 应调 LLM 并把 user+assistant 存库。"""
    calls = {}

    class FakeResp:
        class _C:
            message = type("m", (), {"content": "我是 Maestro！"})
        choices = [_C()]

    def fake_create(model, messages, temperature=0.2, **kw):
        calls["messages"] = messages
        calls["temperature"] = temperature
        return [type("ch",(),{"choices":[type("c",(),{"delta":type("d",(),{"content":'我是 Maestro！'})})]})()]

    monkeypatch.setattr(
        "maestro.chat.llm.get_client",
        lambda: type("c", (), {"chat": type("ch", (), {"completions": type("cp", (), {"create": staticmethod(fake_create)})})})(),
    )
    reply = chat.chat(tmp_db, "在吗")
    assert reply == "我是 Maestro！"
    assert calls["temperature"] == 0.8
    h = db.get_chat_history(tmp_db)
    assert [m["role"] for m in h] == ["user", "assistant"]
    assert h[0]["content"] == "在吗"


def test_chat_api_route(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    # 空消息 400
    assert c.post("/api/chat", json={"message": "  "}).status_code == 400
    # history 空列表
    r = c.get("/api/chat/history")
    assert r.status_code == 200 and r.json() == {"messages": []}


def test_chat_api_roundtrip(tmp_path, monkeypatch):
    """真实 API 走 chat.chat，需 mock LLM 避免依赖 API key。"""
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    import maestro.chat as chatmod

    class FakeResp:
        class _C:
            message = type("m", (), {"content": "嗯，我在。"})
        choices = [_C()]

    monkeypatch.setattr(
        "maestro.chat.llm.get_client",
        lambda: type("c", (), {"chat": type("ch", (), {"completions": type("cp", (), {"create": staticmethod(lambda **k: [type("ch",(),{"choices":[type("c",(),{"delta":type("d",(),{"content":"嗯，我在。"})})]})()])})})})(),
    )
    c = TestClient(app)
    r = c.post("/api/chat", json={"message": "你好"})
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "嗯，我在。"
    h = c.get("/api/chat/history").json()["messages"]
    assert [m["role"] for m in h] == ["user", "assistant"]


# ---------- 长期记忆（user_profile） ----------

def test_profile_set_get(tmp_db):
    assert db.get_user_profile(tmp_db) == {}
    db.set_user_profile(tmp_db, "name", "秋离")
    db.set_user_profile(tmp_db, "like", "写代码")
    prof = db.get_user_profile(tmp_db)
    assert prof["name"] == "秋离" and prof["like"] == "写代码"
    # 覆盖式更新
    db.set_user_profile(tmp_db, "name", "新名字")
    assert db.get_user_profile(tmp_db)["name"] == "新名字"
    assert len(db.get_user_profile(tmp_db)) == 2


def test_profile_clear(tmp_db):
    db.set_user_profile(tmp_db, "name", "x")
    db.clear_user_profile(tmp_db)
    assert db.get_user_profile(tmp_db) == {}


def test_extract_profile_from_message(tmp_db):
    """从用户消息提取档案（名字/身份/喜欢/讨厌）。"""
    chat._extract_profile(tmp_db, "我叫秋离，我是计算机专业的学生，喜欢写代码和二次元，讨厌被说教")
    prof = db.get_user_profile(tmp_db)
    assert prof["name"] == "秋离"
    assert prof["identity"] == "计算机专业的学生"
    assert prof["like"] == "写代码"
    assert prof["dislike"] == "被说教"


def test_extract_profile_no_false_positive(tmp_db):
    """没提到自我信息时不应写入。"""
    chat._extract_profile(tmp_db, "今天天气不错，你觉得呢")
    assert db.get_user_profile(tmp_db) == {}


def test_profile_block_in_system(tmp_db, monkeypatch):
    """记住的信息应注入 system prompt。"""
    calls = {}

    class FakeResp:
        class _C:
            message = type("m", (), {"content": "嗯。"})
        choices = [_C()]

    def fake_create(model, messages, temperature=0.2, **kw):
        calls["messages"] = messages
        return [type("ch",(),{"choices":[type("c",(),{"delta":type("d",(),{"content":'嗯。'})})]})()]

    monkeypatch.setattr(
        "maestro.chat.llm.get_client",
        lambda: type("c", (), {"chat": type("ch", (), {"completions": type("cp", (), {"create": staticmethod(fake_create)})})})(),
    )
    chat.chat(tmp_db, "我叫秋离")
    chat.chat(tmp_db, "你好")
    sys_content = calls["messages"][0]["content"]
    assert "名字：秋离" in sys_content
    assert "芙莉莲记得的" in sys_content


def test_profile_api_route(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    assert c.get("/api/chat/profile").json() == {"profile": {}}
    db2 = db.init_db()
    try:
        db.set_user_profile(db2, "name", "秋离")
    finally:
        db2.close()
    # 不同连接读同一 DB
    assert c.get("/api/chat/profile").json()["profile"]["name"] == "秋离"
    assert c.delete("/api/chat/profile").json() == {"ok": True}
    assert c.get("/api/chat/profile").json() == {"profile": {}}


# ---------- 会话隔离（conversations） ----------

def test_create_and_list_conversations(tmp_db):
    c1 = db.create_conversation(tmp_db, "任务：写脚本")
    c2 = db.create_conversation(tmp_db, "闲聊")
    lst = db.list_conversations(tmp_db)
    ids = {c["id"] for c in lst}
    assert c1 in ids and c2 in ids
    titles = {c["title"]: c for c in lst}
    assert titles["任务：写脚本"]["msg_count"] == 0


def test_messages_isolated_by_conversation(tmp_db):
    c1 = db.create_conversation(tmp_db, "A")
    c2 = db.create_conversation(tmp_db, "B")
    db.add_chat_message(tmp_db, "user", "A的问题", conv_id=c1)
    db.add_chat_message(tmp_db, "assistant", "A的回答", conv_id=c1)
    db.add_chat_message(tmp_db, "user", "B的问题", conv_id=c2)
    # 各会话只看到自己的消息
    h1 = db.get_chat_history(tmp_db, conv_id=c1)
    h2 = db.get_chat_history(tmp_db, conv_id=c2)
    assert [m["content"] for m in h1] == ["A的问题", "A的回答"]
    assert [m["content"] for m in h2] == ["B的问题"]
    # 列表带消息数与预览
    lst = {c["title"]: c for c in db.list_conversations(tmp_db)}
    assert lst["A"]["msg_count"] == 2 and lst["A"]["last_content"] == "A的回答"
    assert lst["B"]["msg_count"] == 1 and lst["B"]["last_content"] == "B的问题"


def test_touch_conversation_updates_order(tmp_db):
    c1 = db.create_conversation(tmp_db, "A")
    c2 = db.create_conversation(tmp_db, "B")
    db.add_chat_message(tmp_db, "user", "x", conv_id=c2)
    # c2 刚活跃应排最前
    lst = db.list_conversations(tmp_db)
    assert lst[0]["id"] == c2


def test_chat_isolated_context(tmp_db, monkeypatch):
    """chat() 带 conv_id：上下文只含该会话历史。"""
    calls = {}

    class FakeResp:
        class _C:
            message = type("m", (), {"content": "嗯。"})
        choices = [_C()]

    def fake_create(model, messages, temperature=0.2, **kw):
        calls["messages"] = messages
        return [type("ch",(),{"choices":[type("c",(),{"delta":type("d",(),{"content":'嗯。'})})]})()]

    monkeypatch.setattr(
        "maestro.chat.llm.get_client",
        lambda: type("c", (), {"chat": type("ch", (), {"completions": type("cp", (), {"create": staticmethod(fake_create)})})})(),
    )
    c1 = db.create_conversation(tmp_db, "A")
    c2 = db.create_conversation(tmp_db, "B")
    chat.chat(tmp_db, "A的第一条", conv_id=c1)
    chat.chat(tmp_db, "B的第一条", conv_id=c2)
    chat.chat(tmp_db, "B的第二条", conv_id=c2)
    # 最后一次调用上下文：system + B 的历史（user+assistant）+ 当前消息，不含 A
    contents = [m["content"] for m in calls["messages"] if m["role"] != "system"]
    assert contents == ["B的第一条", "嗯。", "B的第二条"]


def test_conversation_api_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    # 创建会话
    r = c.post("/api/conversations", json={"title": "任务：写代码"})
    assert r.status_code == 200
    conv_id = r.json()["conv_id"]
    assert r.json()["title"] == "任务：写代码"
    # 列表
    lst = c.get("/api/conversations").json()["conversations"]
    assert any(x["id"] == conv_id for x in lst)
    # chat 到不存在会话 404
    assert c.post("/api/chat", json={"message": "hi", "conv_id": "conv_nonexistent"}).status_code == 404
    # history 按会话过滤
    h = c.get("/api/chat/history", params={"conv_id": conv_id}).json()["messages"]
    assert h == []


# ---------- 会话类型（kind：聊天/任务历史分开） ----------

def test_conversation_kind_filter(tmp_db):
    c1 = db.create_conversation(tmp_db, "闲聊话题", kind="chat")
    c2 = db.create_conversation(tmp_db, "任务：写脚本", kind="task")
    c3 = db.create_conversation(tmp_db, "任务：查资料", kind="task")
    chats = db.list_conversations(tmp_db, kind="chat")
    tasks = db.list_conversations(tmp_db, kind="task")
    assert {c["id"] for c in chats} == {c1, db.DEFAULT_CONV_ID}
    assert {c["id"] for c in tasks} == {c2, c3}
    assert {c["kind"] for c in db.list_conversations(tmp_db)} == {"chat", "task"}


def test_conversation_kind_api(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    r = c.post("/api/conversations", json={"title": "任务：X", "kind": "task"})
    assert r.status_code == 200 and r.json()["kind"] == "task"
    # 非法 kind 400
    assert c.post("/api/conversations", json={"kind": "foo"}).status_code == 400
    lst = c.get("/api/conversations", params={"kind": "task"}).json()["conversations"]
    assert any(x["kind"] == "task" for x in lst)
    # chat 列表不应含 task
    assert all(x["kind"] == "chat" for x in c.get("/api/conversations", params={"kind": "chat"}).json()["conversations"])


# ---------- 流式闲聊（chat_stream / SSE） ----------

def _mk_stream_client(chunks, monkeypatch):
    """mock llm client：create(stream=True) 返回 chunks 列表。"""
    calls = {}

    def fake_create(model, messages, temperature=0.2, **kw):
        calls["messages"] = messages
        calls["stream"] = kw.get("stream")
        return chunks

    monkeypatch.setattr(
        "maestro.chat.llm.get_client",
        lambda: type("c", (), {"chat": type("ch", (), {"completions": type("cp", (), {"create": staticmethod(fake_create)})})})(),
    )
    return calls


def _chunk(text):
    return type("ch", (), {"choices": [type("c", (), {"delta": type("d", (), {"content": text})})]})()


def test_chat_stream_deltas(tmp_db, monkeypatch):
    """chat_stream 应逐块 yield delta，最后 done 并完整存库。"""
    calls = _mk_stream_client([_chunk("你"), _chunk("好"), _chunk("啊")], monkeypatch)
    events = list(chat.chat_stream(tmp_db, "在吗"))
    assert calls["stream"] is True
    deltas = [d for k, d in events if k == "delta"]
    assert "".join(deltas) == "你好啊"
    done = [d for k, d in events if k == "done"]
    assert done == ["你好啊"]
    # 存库：user + assistant 完整回复
    h = db.get_chat_history(tmp_db)
    assert [m["role"] for m in h] == ["user", "assistant"]
    assert h[1]["content"] == "你好啊"


def test_chat_stream_error(tmp_db, monkeypatch):
    """LLM 抛异常 → yield error，不存 assistant。"""
    def boom(**kw):
        raise RuntimeError("llm down")
    monkeypatch.setattr(
        "maestro.chat.llm.get_client",
        lambda: type("c", (), {"chat": type("ch", (), {"completions": type("cp", (), {"create": staticmethod(boom)})})})(),
    )
    events = list(chat.chat_stream(tmp_db, "在吗"))
    assert events[0][0] == "error"
    assert "llm down" in events[0][1]
    h = db.get_chat_history(tmp_db)
    assert [m["role"] for m in h] == ["user"]  # 只有 user，无 assistant


def test_chat_stream_api(tmp_path, monkeypatch):
    """POST /api/chat/stream 返回 SSE：delta 块 + done。"""
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    _mk_stream_client([_chunk("嗯"), _chunk("，"), _chunk("在。")], monkeypatch)
    c = TestClient(app)
    r = c.post("/api/chat/stream", json={"message": "你好"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert 'data: {"delta": "嗯"}' in body
    assert 'data: {"delta": "，"}' in body
    assert '"done": true' in body


def test_chat_stream_api_404(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    r = c.post("/api/chat/stream", json={"message": "hi", "conv_id": "conv_nonexistent"})
    assert r.status_code == 404


# ---------- 会话管理（删除/重命名/导出） ----------

def test_rename_delete_conversation(tmp_db):
    c1 = db.create_conversation(tmp_db, "旧名字", kind="chat")
    assert db.rename_conversation(tmp_db, c1, "新名字") is True
    assert db.get_conversation(tmp_db, c1)["title"] == "新名字"
    db.add_chat_message(tmp_db, "user", "x", conv_id=c1)
    assert db.delete_conversation(tmp_db, c1) is True
    assert db.get_conversation(tmp_db, c1) is None
    assert db.get_chat_history(tmp_db, conv_id=c1) == []
    # 删除不存在的会话返回 False
    assert db.delete_conversation(tmp_db, "conv_none") is False


def test_conversation_manage_api(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    conv_id = c.post("/api/conversations", json={"title": "旧", "kind": "chat"}).json()["conv_id"]
    # 重命名
    r = c.put(f"/api/conversations/{conv_id}", json={"title": "新标题"})
    assert r.status_code == 200 and r.json()["title"] == "新标题"
    # 空标题 400
    assert c.put(f"/api/conversations/{conv_id}", json={"title": "  "}).status_code == 400
    # 导出（空会话）
    r = c.get(f"/api/conversations/{conv_id}/export")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert "新标题" in r.text
    # 删除
    assert c.delete(f"/api/conversations/{conv_id}").json() == {"ok": True}
    assert c.delete(f"/api/conversations/{conv_id}").status_code == 404
    assert c.get(f"/api/conversations/{conv_id}/export").status_code == 404


def test_conversation_export_content(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    conv_id = c.post("/api/conversations", json={"title": "测试导出", "kind": "chat"}).json()["conv_id"]
    # 通过 chat 接口产生对话（mock LLM）
    import maestro.chat as chatmod

    class FakeResp:
        class _C:
            message = type("m", (), {"content": "你好呀"})
        choices = [_C()]

    monkeypatch.setattr(
        "maestro.chat.llm.get_client",
        lambda: type("c", (), {"chat": type("ch", (), {"completions": type("cp", (), {"create": staticmethod(lambda **k: [type("ch2",(),{"choices":[type("c",(),{"delta":type("d",(),{"content":"你好呀"})})]})()])})})})(),
    )
    c.post("/api/chat", json={"message": "在吗", "conv_id": conv_id})
    r = c.get(f"/api/conversations/{conv_id}/export")
    assert "在吗" in r.text and "你好呀" in r.text
    assert "测试导出" in r.text


# ---------- 会话管理（删除/重命名/导出） ----------

def test_rename_delete_conversation(tmp_db):
    c1 = db.create_conversation(tmp_db, "旧名字", kind="chat")
    assert db.rename_conversation(tmp_db, c1, "新名字") is True
    assert db.get_conversation(tmp_db, c1)["title"] == "新名字"
    db.add_chat_message(tmp_db, "user", "x", conv_id=c1)
    assert db.delete_conversation(tmp_db, c1) is True
    assert db.get_conversation(tmp_db, c1) is None
    assert db.get_chat_history(tmp_db, conv_id=c1) == []
    # 删除不存在的会话返回 False
    assert db.delete_conversation(tmp_db, "conv_none") is False


def test_conversation_manage_api(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    conv_id = c.post("/api/conversations", json={"title": "旧", "kind": "chat"}).json()["conv_id"]
    # 重命名
    r = c.put(f"/api/conversations/{conv_id}", json={"title": "新标题"})
    assert r.status_code == 200 and r.json()["title"] == "新标题"
    # 空标题 400
    assert c.put(f"/api/conversations/{conv_id}", json={"title": "  "}).status_code == 400
    # 导出（空会话）
    r = c.get(f"/api/conversations/{conv_id}/export")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert "新标题" in r.text
    # 删除
    assert c.delete(f"/api/conversations/{conv_id}").json() == {"ok": True}
    assert c.delete(f"/api/conversations/{conv_id}").status_code == 404
    assert c.get(f"/api/conversations/{conv_id}/export").status_code == 404


def test_conversation_export_content(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    conv_id = c.post("/api/conversations", json={"title": "测试导出", "kind": "chat"}).json()["conv_id"]
    # 通过 chat 接口产生对话（mock LLM）
    import maestro.chat as chatmod

    class FakeResp:
        class _C:
            message = type("m", (), {"content": "你好呀"})
        choices = [_C()]

    monkeypatch.setattr(
        "maestro.chat.llm.get_client",
        lambda: type("c", (), {"chat": type("ch", (), {"completions": type("cp", (), {"create": staticmethod(lambda **k: [type("ch2",(),{"choices":[type("c",(),{"delta":type("d",(),{"content":"你好呀"})})]})()])})})})(),
    )
    c.post("/api/chat", json={"message": "在吗", "conv_id": conv_id})
    r = c.get(f"/api/conversations/{conv_id}/export")
    assert "在吗" in r.text and "你好呀" in r.text
    assert "测试导出" in r.text


def _stub_stream_llm(monkeypatch, captured):
    """把 LLM 桩成流式返回"好"，并捕获 messages 供断言。"""

    def fake_create(model, messages, temperature=0.2, **kw):
        captured["messages"] = messages
        return [type("ch", (), {"choices": [type("c", (), {"delta": type("d", (), {"content": "好"})})]})()]

    monkeypatch.setattr(
        "maestro.chat.llm.get_client",
        lambda: type("c", (), {"chat": type("ch", (), {"completions": type("cp", (), {"create": staticmethod(fake_create)})})})(),
    )


def test_chat_linked_context_injected(tmp_db, monkeypatch):
    """关联会话的最近内容应注入 system 消息（含对方会话标题）。"""
    conv_a = db.create_conversation(tmp_db, title="窗口A")
    conv_b = db.create_conversation(tmp_db, title="窗口B")
    db.add_chat_message(tmp_db, "user", "我最喜欢草莓蛋糕", conv_id=conv_b)
    db.add_chat_message(tmp_db, "assistant", "记住了", conv_id=conv_b)

    captured = {}
    _stub_stream_llm(monkeypatch, captured)
    chat.chat(tmp_db, "我们聊到哪了", conv_id=conv_a, link_conv_ids=[conv_b])
    system = captured["messages"][0]["content"]
    assert "草莓蛋糕" in system
    assert "窗口B" in system


def test_chat_without_links_no_inject(tmp_db, monkeypatch):
    """不关联时不注入对方内容。"""
    conv_a = db.create_conversation(tmp_db, title="窗口A")
    conv_b = db.create_conversation(tmp_db, title="窗口B")
    db.add_chat_message(tmp_db, "user", "我最喜欢草莓蛋糕", conv_id=conv_b)

    captured = {}
    _stub_stream_llm(monkeypatch, captured)
    chat.chat(tmp_db, "嗨", conv_id=conv_a)
    system = captured["messages"][0]["content"]
    assert "草莓蛋糕" not in system
    assert "关联对话" not in system


def test_stream_api_rejects_bad_links(tmp_db, tmp_path, monkeypatch):
    """/api/chat/stream 的 link_conv_ids 必须是字符串数组。"""
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    conv_id = c.post("/api/conversations", json={"title": "t", "kind": "chat"}).json()["conv_id"]
    r = c.post("/api/chat/stream", json={"message": "hi", "conv_id": conv_id, "link_conv_ids": "convB"})
    assert r.status_code == 400


def test_stream_api_filters_self_link(tmp_db, tmp_path, monkeypatch):
    """关联自己应被过滤掉，不报错。"""
    captured = {}
    _stub_stream_llm(monkeypatch, captured)
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    conv_id = c.post("/api/conversations", json={"title": "t", "kind": "chat"}).json()["conv_id"]
    r = c.post("/api/chat/stream", json={"message": "hi", "conv_id": conv_id, "link_conv_ids": [conv_id]})
    assert r.status_code == 200


def test_conversation_search(tmp_db):
    """?q= 搜标题或消息内容；LIKE 通配符转义；大小写不敏感。"""
    c1 = db.create_conversation(tmp_db, title="芙莉莲的魔法笔记")
    c2 = db.create_conversation(tmp_db, title="项目周报")
    db.add_chat_message(tmp_db, "user", "帮我总结一下 SQLite 索引优化", conv_id=c2)
    db.add_chat_message(tmp_db, "assistant", "好的", conv_id=c2)

    # 按标题搜
    r = db.list_conversations(tmp_db, q="魔法")
    assert [x["id"] for x in r] == [c1]
    # 按消息内容搜
    r = db.list_conversations(tmp_db, q="索引优化")
    assert [x["id"] for x in r] == [c2]
    # LIKE 通配符按字面匹配（不当作通配符）
    r = db.list_conversations(tmp_db, q="100%")
    assert r == []
    # 无匹配
    assert db.list_conversations(tmp_db, q="不存在的关键词xyz") == []
    # 组合 kind + q
    c3 = db.create_conversation(tmp_db, title="魔法任务", kind="task")
    r = db.list_conversations(tmp_db, kind="task", q="魔法")
    assert [x["id"] for x in r] == [c3]


def test_history_incremental_since_id(tmp_db):
    """?since_id=N 增量同步：只返回 id > N 的消息。"""
    c1 = db.create_conversation(tmp_db, title="增量同步")
    m1 = db.add_chat_message(tmp_db, "user", "1", conv_id=c1)
    m2 = db.add_chat_message(tmp_db, "assistant", "2", conv_id=c1)
    m3 = db.add_chat_message(tmp_db, "user", "3", conv_id=c1)

    # since_id=0：全量
    msgs = db.get_chat_history(tmp_db, conv_id=c1, since_id=0)
    assert [m["id"] for m in msgs] == [m1, m2, m3]
    # since_id=m1：只剩 m2, m3
    msgs = db.get_chat_history(tmp_db, conv_id=c1, since_id=m1)
    assert [m["id"] for m in msgs] == [m2, m3]
    # since_id=m3：空
    assert db.get_chat_history(tmp_db, conv_id=c1, since_id=m3) == []


def test_history_feed_sse_streams_new_messages(tmp_path, monkeypatch):
    """/api/chat/feed SSE：先推 1 条历史补漏，再即时推新加的消息。"""
    import time
    from fastapi.testclient import TestClient
    from maestro import server
    from maestro import db

    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    monkeypatch.setattr(server, "WALLPAPER_DIR", tmp_path)
    db_path = tmp_path / "m.db"
    monkeypatch.setenv("MAESTRO_DB", str(db_path))
    monkeypatch.setenv("MAESTRO_DB_POOL", "0")
    # 预写历史（用独立连接，与 TestClient 启的实例隔离）
    seed = db.init_db(db_path, use_cache=False)
    c1 = "conv_sse_test"
    try:
        db.add_chat_message(seed, "user", "hist-1", conv_id=c1)
        db.add_chat_message(seed, "assistant", "hist-2", conv_id=c1)
    finally:
        seed.close()
    # 第二连接用于即时推新消息
    writer = db.init_db(db_path, use_cache=False)

    # SSE 长轮询走原始 socket：TestClient.stream 走 ASGI 传输，长轮询
    # generator 即使客户端断开也未必立即感知（要等下次 time.sleep 后），会卡。
    # 直连 socket + select 限时最稳。
    def read_sse_events(conv_id, since_id, predicate, timeout=4.0):
        # TestClient.stream 走 ASGI 传输：服务端 generator 阻塞在 time.sleep
        # 时 r.close() 不会立即停 generator。简单粗暴：read_bytes 是生成器，
        # 不用 with，由 except 兜底；超时由调用方 timeout 控制。
        import httpx
        with c.stream("GET", f"/api/chat/feed?conv_id={conv_id}&since_id={since_id}&poll=0.05") as r:
            assert r.status_code == 200
            buf = b""
            t0 = time.monotonic()
            try:
                for chunk in r.iter_bytes(chunk_size=64):
                    buf += chunk
                    if predicate(buf) or time.monotonic() - t0 > timeout:
                        break
            except httpx.RemoteProtocolError:
                # 客户端 close 触发服务端 generator GeneratorExit 抛上来
                pass
            finally:
                try: r.close()
                except Exception: pass
        return buf.decode("utf-8", errors="replace")

        # 第一次连接 since_id=0：应立即收到 2 条补漏
        text = read_sse_events(
            c1, since_id=0, predicate=lambda b: b.count(b"event: msg") >= 2
        )
        assert text.count("event: msg") == 2, f"未收到 2 条补漏，got: {text[:200]}"

        # 第二次连接 since_id=2：等待即时推送 new-3
        db.add_chat_message(writer, "user", "new-3", conv_id=c1)
        text2 = read_sse_events(
            c1, since_id=2, predicate=lambda b: b'"new-3"' in b
        )
        assert "new-3" in text2, f"未收到新消息推送，got: {text2[:200]}"
    writer.close()