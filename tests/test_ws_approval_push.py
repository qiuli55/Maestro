"""WS 审批推送端到端：sandbox.on_request 钩子 → push_event_threadsafe → /ws 订阅者收到。

这正是前端审批卡片依赖的关键链路（kind=approval），覆盖集成而非单点。
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


def _collect_ws_msgs(host: str, path: str = "/ws", timeout: float = 3.0):
    """打开 WS 连接并起后台线程收一条消息，返回 (ws, list, thread)。"""
    from websocket import create_connection
    ws = create_connection(f"ws://{host}{path}", timeout=timeout)
    received = []

    def reader():
        try:
            ws.settimeout(timeout)
            msg = ws.recv()
            received.append(json.loads(msg))
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    return ws, received, t


def test_approval_push_e2e(tmp_path, monkeypatch):
    """真起服务，触发 approval 钩子，WS 端 5s 内收到 kind=approval 消息。"""
    from maestro import sandbox
    from maestro.sandbox import ApprovalRequest

    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("MAESTRO_DB_POOL", "0")
    monkeypatch.setenv("MAESTRO_PORT", "8766")

    proc = subprocess.Popen(
        [sys.executable, "-m", "maestro.server"],
        cwd="src", env=os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        import socket
        ready = False
        for _ in range(40):
            try:
                s = socket.socket(); s.settimeout(1)
                s.connect(("127.0.0.1", 8766))
                s.send(b"GET /api/healthz HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                if b"200" in s.recv(200): ready = True
                s.close()
                if ready: break
            except Exception:
                time.sleep(0.25)
        assert ready, "服务未启动"

        ws, received, reader_t = _collect_ws_msgs("127.0.0.1:8766")
        time.sleep(0.4)  # WS onopen

        req = ApprovalRequest(id="ap_e2e_test", task_id="t_e2e", subtask_id="st_x",
                              cmd="git status")
        sandbox._notify(req)
        reader_t.join(timeout=5)
        ws.close()

        assert received, "WS 应收到至少一条消息"
        msg = received[0]
        assert msg["kind"] == "approval"
        assert msg["conv_id"] == "t_e2e"
        payload = json.loads(msg["content"])
        assert payload["approval_id"] == "ap_e2e_test"
        assert payload["cmd"] == "git status"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
