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
