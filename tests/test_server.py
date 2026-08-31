"""server.py 冒烟测试：不触发真实 LLM / worker，仅验证路由与装配正确。"""
from fastapi.testclient import TestClient

from maestro.server import app


def test_root_serves_dashboard():
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert "Maestro" in r.text


def test_tasks_list_empty_ok():
    c = TestClient(app)
    r = c.get("/api/tasks")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_create_task_rejects_empty_prompt():
    c = TestClient(app)
    r = c.post("/api/tasks", json={"prompt": "   "})
    assert r.status_code == 400


def test_get_missing_task_404():
    c = TestClient(app)
    r = c.get("/api/tasks/task_does_not_exist")
    assert r.status_code == 404


def test_wallpaper_route_exists():
    c = TestClient(app)
    # 壁纸文件存在时返回 html；不存在时 404 也算路由已注册
    r = c.get("/wallpaper")
    assert r.status_code in (200, 404)


def test_task_list_includes_time_and_failed(tmp_path, monkeypatch):
    """任务列表应返回 updated_at 与失败子任务数（P2 打磨 #1）。"""
    import os
    from maestro import db as dbmod

    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    conn = dbmod.init_db()
    tid = "task_polish_demo"
    dbmod.create_task(conn, tid, "示例需求")
    dbmod.add_subtasks(conn, tid, [
        {"id": tid + "_s1", "desc": "正常", "worker_type": "embedded"},
        {"id": tid + "_s2", "desc": "会失败", "worker_type": "embedded"},
    ])
    dbmod.set_subtask_status(conn, tid + "_s2", dbmod.FAILED)
    conn.close()

    c = TestClient(app)
    r = c.get("/api/tasks")
    assert r.status_code == 200
    items = [t for t in r.json() if t["id"] == tid]
    assert items, "种子任务应出现在列表"
    item = items[0]
    assert item["updated_at"], "应返回 updated_at"
    assert item["n_subtasks"] == 2, "应有 2 个子任务"
    assert item["n_failed"] == 1, "应统计到 1 个失败子任务"


# ---------- 人工闸门：编辑 / 确认执行 API ----------

def _seed_ready_task(tmp_path, monkeypatch):
    """建一个 READY 状态任务（未执行），返回 (TestClient, task_id)。"""
    import os
    from maestro import db as dbmod

    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    conn = dbmod.init_db()
    tid = "task_edit_demo"
    dbmod.create_task(conn, tid, "示例")
    dbmod.set_task_status(conn, tid, dbmod.READY)
    dbmod.set_task_params(conn, tid, scenario="a", worker_type="embedded", parallel=False, no_merge=False)
    dbmod.add_subtasks(conn, tid, [
        {"id": tid + "_st_1", "desc": "原任务1", "worker_type": "embedded"},
        {"id": tid + "_st_2", "desc": "原任务2", "worker_type": "embedded"},
    ])
    conn.close()
    return TestClient(app), tid


def test_edit_subtasks_ready_ok(tmp_path, monkeypatch):
    """READY 任务可整体替换子任务（增删改调序）。"""
    c, tid = _seed_ready_task(tmp_path, monkeypatch)
    r = c.put(f"/api/tasks/{tid}/subtasks", json={"subtasks": [
        {"desc": "改过的1", "worker_type": "opencode"},
        {"desc": "新加的", "worker_type": "embedded"},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["n_subtasks"] == 2
    # 入库结果核对
    import os
    from maestro import db as dbmod
    conn = dbmod.init_db()
    subs = dbmod.get_subtasks(conn, tid)
    conn.close()
    assert [s["desc"] for s in subs] == ["改过的1", "新加的"]
    assert subs[0]["worker_type"] == "opencode"


def test_edit_subtasks_rejects_bad_worker(tmp_path, monkeypatch):
    c, tid = _seed_ready_task(tmp_path, monkeypatch)
    r = c.put(f"/api/tasks/{tid}/subtasks", json={"subtasks": [
        {"desc": "x", "worker_type": "no_such_worker"},
    ]})
    assert r.status_code == 400, r.text


def test_edit_subtasks_rejects_empty(tmp_path, monkeypatch):
    c, tid = _seed_ready_task(tmp_path, monkeypatch)
    r = c.put(f"/api/tasks/{tid}/subtasks", json={"subtasks": []})
    assert r.status_code == 400, r.text


def test_execute_only_ready(tmp_path, monkeypatch):
    """execute 仅接受 READY；非 ready（如不存在/pending）拒绝。"""
    c, tid = _seed_ready_task(tmp_path, monkeypatch)
    r = c.post(f"/api/tasks/{tid}/execute")
    assert r.status_code == 200, r.text
    # 再次 execute（已提交执行，状态被后台改 RUNNING 前可能仍是 ready，但这里只验证路由存在）
    # 不存在任务
    r = c.post("/api/tasks/task_missing/execute")
    assert r.status_code == 404


def test_edit_subtasks_rejects_not_ready(tmp_path, monkeypatch):
    """非 READY 任务（如 DONE）不可编辑。"""
    from maestro import db as dbmod
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    conn = dbmod.init_db()
    tid = "task_done_demo"
    dbmod.create_task(conn, tid, "示例")
    dbmod.set_task_status(conn, tid, dbmod.DONE)
    conn.close()
    c = TestClient(app)
    r = c.put(f"/api/tasks/{tid}/subtasks", json={"subtasks": [{"desc": "x", "worker_type": "embedded"}]})
    assert r.status_code == 409, r.text


def test_create_task_returns_confirm_flag(tmp_path, monkeypatch):
    """创建任务返回 confirm 参数（默认 True=先确认）。"""
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    # 用场景 A（无需 LLM），避免真实拆分依赖 API key
    r = c.post("/api/tasks", json={"prompt": "需求1\n需求2", "scenario": "a", "confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"], "应返回 task_id"
    assert body["confirm"] is True
    # 等待后台 prepare 完成（异步），随后任务应停在 READY 而非直接执行
    import time
    for _ in range(30):
        r = c.get(f"/api/tasks/{body['task_id']}")
        if r.status_code != 200:  # 后台尚未建任务
            time.sleep(0.1)
            continue
        snap = r.json()
        if snap["task"]["status"] in ("ready", "failed"):
            break
        time.sleep(0.1)
    assert snap["task"]["status"] == "ready", f"confirm=True 应停在 ready，实际 {snap['task']['status']}"
    assert all(s["status"] == "pending" for s in snap["subtasks"]), "prepare 后子任务未执行"


# ---------- 任务管理：删除 / 取消 / 恢复 ----------

def _seed_task(tmp_path, monkeypatch, status, tid="task_mgmt"):
    """建一个指定状态的任务，返回 task_id。"""
    import os
    from maestro import db as dbmod

    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    conn = dbmod.init_db()
    dbmod.create_task(conn, tid, "示例")
    dbmod.set_task_status(conn, tid, status)
    conn.close()
    return tid


def test_create_task_confirm_false_auto_runs(tmp_path, monkeypatch):
    """confirm=False 跳过闸门：拆分后立即执行（旧行为等价）。"""
    import time
    from maestro import db as dbmod
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    # monkeypatch 后台执行用的 worker —— 直接 patch orchestrator.execute_task 避免真调 LLM
    import maestro.orchestrator as orch
    calls = []
    def fake_execute(conn, task_id, timeout=600, model=None):
        calls.append(task_id)
        dbmod.set_task_status(conn, task_id, dbmod.DONE)
        return task_id
    monkeypatch.setattr(orch, "execute_task", fake_execute)

    r = c.post("/api/tasks", json={"prompt": "需求1\n需求2", "scenario": "a", "confirm": False})
    assert r.status_code == 200
    tid = r.json()["task_id"]
    assert r.json()["confirm"] is False
    for _ in range(30):
        if calls: break
        time.sleep(0.1)
    assert calls == [tid], "confirm=False 应自动执行"


def test_delete_task_ok(tmp_path, monkeypatch):
    tid = _seed_task(tmp_path, monkeypatch, "done")
    c = TestClient(app)
    r = c.delete(f"/api/tasks/{tid}")
    assert r.status_code == 200, r.text
    # 再删 404
    assert c.delete(f"/api/tasks/{tid}").status_code == 404
    # 列表里已无
    assert all(t["id"] != tid for t in c.get("/api/tasks").json())


def test_delete_running_rejected(tmp_path, monkeypatch):
    tid = _seed_task(tmp_path, monkeypatch, "running")
    c = TestClient(app)
    r = c.delete(f"/api/tasks/{tid}")
    assert r.status_code == 409, r.text


def test_cancel_ready_task(tmp_path, monkeypatch):
    tid = _seed_task(tmp_path, monkeypatch, "ready")
    c = TestClient(app)
    r = c.post(f"/api/tasks/{tid}/cancel")
    assert r.status_code == 200, r.text
    snap = c.get(f"/api/tasks/{tid}").json()
    assert snap["task"]["status"] == "cancelled"


def test_cancel_terminal_rejected(tmp_path, monkeypatch):
    tid = _seed_task(tmp_path, monkeypatch, "done")
    c = TestClient(app)
    r = c.post(f"/api/tasks/{tid}/cancel")
    assert r.status_code == 409, r.text


def test_resume_only_running(tmp_path, monkeypatch):
    # running 可恢复（提交后台任务，路由返回 ok）
    tid = _seed_task(tmp_path, monkeypatch, "running")
    c = TestClient(app)
    r = c.post(f"/api/tasks/{tid}/resume")
    assert r.status_code == 200, r.text
    # done 不可恢复
    tid2 = _seed_task(tmp_path, monkeypatch, "done", tid="task_mgmt2")
    assert c.post(f"/api/tasks/{tid2}/resume").status_code == 409
    # 不存在 404
    assert c.post("/api/tasks/task_missing/resume").status_code == 404


# ---------- 壁纸静态挂载 / 任务列表分页过滤 ----------

def test_wallpaper_static_mount():
    """wallpaper/ 目录应整体挂载：frieren.html 与素材可通过 /wallpaper/ 访问。

    mount 在模块导入时绑定真实 WALLPAPER_DIR，故直接断言真实目录（只读）。
    """
    from pathlib import Path
    import maestro.server as srv

    c = TestClient(srv.app)
    # 路由存在即可（文件存在与否由目录内容决定）
    assert c.get("/wallpaper").status_code == 200  # index.html
    if (Path(srv.WALLPAPER_DIR) / "frieren.html").exists():
        assert c.get("/wallpaper/frieren.html").status_code == 200
    assert c.get("/wallpaper/nonexistent_xyz.png").status_code == 404


def test_task_list_filter_and_pagination(tmp_path, monkeypatch):
    """/api/tasks 支持 status 过滤 + limit/offset 分页。"""
    from maestro import db as dbmod

    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    conn = dbmod.init_db()
    for i in range(5):
        tid = f"task_pg_{i}"
        dbmod.create_task(conn, tid, f"需求{i}")
        dbmod.set_task_status(conn, tid, dbmod.DONE if i % 2 == 0 else dbmod.READY)
    conn.close()

    c = TestClient(app)
    # 全量
    assert len(c.get("/api/tasks").json()) == 5
    # 状态过滤
    done = c.get("/api/tasks", params={"status": "done"}).json()
    assert len(done) == 3 and all(t["status"] == "done" for t in done)
    ready = c.get("/api/tasks", params={"status": "ready"}).json()
    assert len(ready) == 2
    # 分页
    page1 = c.get("/api/tasks", params={"limit": 2, "offset": 0}).json()
    page2 = c.get("/api/tasks", params={"limit": 2, "offset": 2}).json()
    assert len(page1) == 2 and len(page2) == 2
    assert page1[0]["id"] != page2[0]["id"], "两页不应重复"
    # 非法参数
    assert c.get("/api/tasks", params={"status": "bogus"}).status_code == 400
    assert c.get("/api/tasks", params={"limit": 0}).status_code == 400
    assert c.get("/api/tasks", params={"offset": -1}).status_code == 400


def test_create_task_auto_detects_scenario(tmp_path, monkeypatch):
    """scenario=auto 时调用 detect_scenario 并按其结果路由（决策自主性）。"""
    from maestro.server import app
    from maestro import split as splitmod

    calls = {}

    def _fake_detect(prompt, model=None):
        calls["prompt"] = prompt
        return "b", "多段文本需汇总"

    monkeypatch.setattr(splitmod, "detect_scenario", _fake_detect)
    # 隔离 DB，避免写真实库
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m.db"))
    from maestro.server import create_task
    # 用 TestClient 但避免后台线程写库：直接验证 create_task 返回结构
    c = TestClient(app)
    r = c.post("/api/tasks", json={"prompt": "第一段\n\n第二段", "scenario": "auto", "confirm": False})
    assert r.status_code == 200
    data = r.json()
    assert data["scenario"] == "b"
    assert data["detect_reason"] == "多段文本需汇总"
    assert calls["prompt"] == "第一段\n\n第二段"


def test_create_task_auto_fallback_on_error(tmp_path, monkeypatch):
    """auto 识别抛异常时回退场景 a，不失败。"""
    from maestro.server import app
    from maestro import split as splitmod

    def _boom(prompt, model=None):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(splitmod, "detect_scenario", _boom)
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m2.db"))
    c = TestClient(app)
    r = c.post("/api/tasks", json={"prompt": "加登录\n加支付", "scenario": "auto"})
    assert r.status_code == 200
    assert r.json()["scenario"] == "a"
    assert "回退" in (r.json().get("detect_reason") or "")


def test_create_task_auto_detects_scenario(tmp_path, monkeypatch):
    """scenario=auto 时调用 detect_scenario 并按其结果路由（决策自主性）。"""
    from maestro.server import app
    from maestro import split as splitmod

    calls = {}

    def _fake_detect(prompt, model=None):
        calls["prompt"] = prompt
        return "b", "多段文本需汇总"

    monkeypatch.setattr(splitmod, "detect_scenario", _fake_detect)
    # 隔离 DB，避免写真实库
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m.db"))
    from maestro.server import create_task
    # 用 TestClient 但避免后台线程写库：直接验证 create_task 返回结构
    c = TestClient(app)
    r = c.post("/api/tasks", json={"prompt": "第一段\n\n第二段", "scenario": "auto", "confirm": False})
    assert r.status_code == 200
    data = r.json()
    assert data["scenario"] == "b"
    assert data["detect_reason"] == "多段文本需汇总"
    assert calls["prompt"] == "第一段\n\n第二段"


def test_create_task_auto_fallback_on_error(tmp_path, monkeypatch):
    """auto 识别抛异常时回退场景 a，不失败。"""
    from maestro.server import app
    from maestro import split as splitmod

    def _boom(prompt, model=None):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(splitmod, "detect_scenario", _boom)
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m2.db"))
    c = TestClient(app)
    r = c.post("/api/tasks", json={"prompt": "加登录\n加支付", "scenario": "auto"})
    assert r.status_code == 200
    assert r.json()["scenario"] == "a"
    assert "回退" in (r.json().get("detect_reason") or "")


# ---------- P0：CSRF Origin 守卫 / WS 鉴权与订阅 ----------


def test_csrf_origin_without_json_rejected(tmp_path, monkeypatch):
    """跨源写请求（带 Origin）且非 application/json → 415（浏览器简单请求 CSRF 防线）。"""
    monkeypatch.delenv("MAESTRO_API_KEY", raising=False)
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m.db"))
    c = TestClient(app)
    r = c.post("/api/chat", content="hi",
               headers={"Origin": "https://evil.example", "Content-Type": "text/plain"})
    assert r.status_code == 415


def test_csrf_no_origin_allowed(tmp_path, monkeypatch):
    """无 Origin（curl/服务间调用）不受 CSRF 守卫影响。"""
    monkeypatch.delenv("MAESTRO_API_KEY", raising=False)
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m.db"))
    c = TestClient(app)
    r = c.post("/api/chat", content="hi", headers={"Content-Type": "text/plain"})
    assert r.status_code != 415


def test_ws_requires_key_when_set(tmp_path, monkeypatch):
    """设了 MAESTRO_API_KEY：无 key 的 WS 连接被拒（4401），带 key 放行。"""
    monkeypatch.setenv("MAESTRO_API_KEY", "secret123")
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m.db"))
    c = TestClient(app)
    # 无 key：服务端在 accept 前 close(4401) -> 客户端侧抛 WebSocketDisconnect
    rejected = False
    try:
        with c.websocket_connect("/ws") as ws:
            ws.receive_text()
    except Exception:
        rejected = True  # close/断开都算拒绝
    assert rejected, "无 key 的 WS 连接应被拒绝"
    # 带 key：正常建立
    with c.websocket_connect("/ws?key=secret123") as ws:
        ws.send_text("ping")
    # 错误 key：同样拒绝
    rejected2 = False
    try:
        with c.websocket_connect("/ws?key=wrong") as ws:
            ws.receive_text()
    except Exception:
        rejected2 = True
    assert rejected2, "错误 key 的 WS 连接应被拒绝"
    monkeypatch.delenv("MAESTRO_API_KEY")


def test_ws_broadcast_respects_subscription(tmp_path, monkeypatch):
    """订阅 A 会话的连接只收 A 的消息；未订阅连接收全部（兼容）。"""
    import asyncio

    import maestro.server as srv

    async def scenario():
        # 直接操作订阅表验证过滤逻辑（不做真实握手，聚焦路由语义）
        class FakeWS:
            def __init__(self):
                self.sent = []

        ws = FakeWS()
        with srv._ws_lock:
            srv._active_ws_connections.append(ws)
            srv._ws_subscriptions[ws] = {"conv_A"}
        assert srv._ws_wants(ws, "conv_A") is True
        assert srv._ws_wants(ws, "conv_B") is False
        # 全收模式
        srv._ws_subscriptions[ws] = {"__all__"}
        assert srv._ws_wants(ws, "conv_B") is True
        with srv._ws_lock:
            srv._active_ws_connections.remove(ws)
            srv._ws_subscriptions.pop(ws, None)

    asyncio.run(scenario())


def test_approval_push_hook_registered():
    """sandbox.on_request 钩子已注册（审批实时推送不再依赖 2s 轮询）。"""
    from maestro import sandbox

    assert len(sandbox._notify_hooks) >= 1


def test_metrics_endpoint(tmp_path, monkeypatch):
    """/metrics 返回 Prometheus 文本与 JSON 两种格式，含核心指标。"""
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m.db"))
    c = TestClient(app)
    r = c.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "maestro_tasks_total" in r.text
    assert "maestro_ws_connections" in r.text
    rj = c.get("/metrics?format=json")
    assert rj.status_code == 200
    data = rj.json()
    assert "tasks_total" in data and "uptime_seconds" in data and "pool" in data


def test_health_dashboard_endpoint(tmp_path, monkeypatch):
    """/api/health/dashboard：状态 + 任务分布 + 待审批 + WS + 错误列表。"""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m.db"))
    # 造点数据：有任务 + 失败子任务 + 事件错误 + 待审批
    from maestro import db, sandbox
    conn = db.init_db(tmp_path / "m.db")
    db.create_task(conn, "t_dash", "prompt", conv_id=None)
    db.set_task_status(conn, "t_dash", db.FAILED)
    db.log_event(conn, "t_dash", "subtask FAIL")
    db.upsert_user_skill(conn, "sk_t", "测试技能", "通用", "")
    sandbox.on_request(lambda r: None)  # 不重要
    conn.execute("INSERT INTO approvals (id, task_id, cmd, status, created_at) VALUES (?,?,?,?,?)",
                 ("ap1", "t_dash", "rm -rf /", "pending", db._now()))
    conn.commit()
    conn.close()

    c = TestClient(app)
    r = c.get("/api/health/dashboard")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] in ("ok", "warn", "degraded")
    assert "failed" in d["tasks"] and d["tasks"]["failed"] >= 1
    assert d["approvals_pending"] >= 1
    assert "ws_connections" in d
    # 有失败任务 + 待审批超阈值可能 warn
    if d["status"] == "warn":
        assert any(e["level"] == "warn" for e in d["errors"])
