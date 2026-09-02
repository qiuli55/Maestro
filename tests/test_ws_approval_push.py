"""WS 审批推送端到端：sandbox.on_request 钩子 → push_event_threadsafe → /ws 订阅者收到。

这正是前端审批卡片依赖的关键链路（kind=approval），覆盖集成而非单点。

实现说明：钩子与 WS 推送都活在 server 的事件循环进程内（push_event_threadsafe
靠进程内 loop 调度，没有跨进程通路），所以 e2e 用 TestClient 跑真实 app：
真实 lifespan（capture_main_loop + 注册审批钩子）+ 真实 /ws 端点，审批从
独立线程触发（对齐真实"worker 线程里产生审批"的调用形态）。
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient


def test_approval_push_e2e(tmp_path, monkeypatch):
    """worker 线程触发审批钩子，/ws 订阅者收到 kind=approval 消息。"""
    from maestro import sandbox, server
    from maestro.api import deps
    from maestro.sandbox import ApprovalRequest

    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("MAESTRO_API_KEY", raising=False)  # WS 鉴权放行

    # 钩子列表是模块级状态，测试前后快照还原，防污染其它用例
    hooks_before = list(sandbox._notify_hooks)
    try:
        with TestClient(server.app) as client:  # lifespan：capture_main_loop + 注册钩子
            with client.websocket_connect("/ws") as ws:
                received = []

                def reader():
                    try:
                        received.append(ws.receive_json())
                    except Exception:  # noqa: BLE001 — 超时/断开都算没收到
                        pass

                rt = threading.Thread(target=reader, daemon=True)
                rt.start()
                time.sleep(0.3)  # 等 onopen（/ws 连接即自动订阅 __all__）

                # 模拟 worker 线程产生审批（真实调用形态：沙箱线程里 _notify）
                req = ApprovalRequest(id="ap_e2e_test", task_id="t_e2e",
                                      subtask_id="st_x", cmd="git status")
                nt = threading.Thread(target=sandbox._notify, args=(req,), daemon=True)
                nt.start()
                nt.join(timeout=5)
                rt.join(timeout=5)
    finally:
        deps._ws_subscriptions.clear()
        deps._active_ws_connections.clear()
        sandbox._notify_hooks[:] = hooks_before

    assert received, "WS 应收到至少一条消息"
    msg = received[0]
    assert msg["kind"] == "approval"
    assert msg["conv_id"] == "t_e2e"
    payload = __import__("json").loads(msg["content"])
    assert payload["approval_id"] == "ap_e2e_test"
    assert payload["cmd"] == "git status"
