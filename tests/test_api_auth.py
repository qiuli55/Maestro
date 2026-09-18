"""API 防护中间件测试：免费试用 + 访问令牌 + 管理凭证 key + 白名单。

[2026-09-08] 防护策略变更：不再强制 X-API-Key，改为「AI 消耗端点每 IP 3 次免费
试用 + 访问令牌解锁」；正确 MAESTRO_API_KEY（头或 ?key=）作为管理凭证放行且不计数。
中间件总是挂载，dispatch 内每次从 env 读配置 —— monkeypatch 即时切换，不需 reimport。
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "src")

import pytest
from fastapi.testclient import TestClient

from maestro.server import app

API_KEY = "test-secret-key-12345"
UNLOCK_TOKEN = "317132ll"


@pytest.fixture
def client():
    """默认无 key（管理凭证关闭），各测试自行 setenv 启用。"""
    return TestClient(app)


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """独立状态文件路径，测试内可读取断言。"""
    p = tmp_path / "trial_state.json"
    monkeypatch.setenv("MAESTRO_TRIAL_STATE", str(p))
    return p


# ====== 白名单与读取端点：永远放行 ======

def test_healthz_always_allowed(client):
    """/api/healthz 探针不需要任何凭证。"""
    assert client.get("/api/healthz").status_code == 200


def test_tasks_list_never_counts(client, state_file):
    """GET /api/tasks 是读取端点：无凭证也放行，不消耗免费次数。"""
    for _ in range(5):
        assert client.get("/api/tasks").status_code == 200
    assert not state_file.exists()


def test_websocket_prefix_allowed(client):
    """/ws/* 免防护（前端连 WS 不带 key）。"""
    resp = client.get("/ws/something")
    assert resp.status_code not in (401, 403, 415)


def test_static_pages_allowed(client):
    """首页与壁纸页不需要任何凭证。"""
    assert client.get("/").status_code not in (401, 403)
    assert client.get("/wallpaper").status_code not in (401, 403)


# ====== AI 消耗端点：免费试用 ======

def test_ai_endpoint_free_trial_counts(client, state_file):
    """POST /api/tasks 前 3 次免费，每次计数落盘。"""
    for i in range(3):
        resp = client.post("/api/tasks", json={"prompt": f"t{i}"})
        assert resp.status_code != 401
    st = json.loads(state_file.read_text())
    assert st["ips"]["testclient"] == 3


def test_ai_endpoint_fourth_call_needs_token(client, state_file):
    """第 4 次调用 AI 端点 → 401 need_token。"""
    for i in range(3):
        client.post("/api/tasks", json={"prompt": f"t{i}"})
    resp = client.post("/api/tasks", json={"prompt": "t4"})
    assert resp.status_code == 401
    body = resp.json()
    assert body["need_token"] is True
    assert "访问令牌" in body["error"]


def test_free_limit_configurable(client, state_file, monkeypatch):
    """免费次数可被 MAESTRO_FREE_TRIAL_LIMIT 覆盖（如设 1：第 2 次即要令牌）。"""
    monkeypatch.setenv("MAESTRO_FREE_TRIAL_LIMIT", "1")
    assert client.post("/api/tasks", json={"prompt": "a"}).status_code != 401
    assert client.post("/api/tasks", json={"prompt": "b"}).status_code == 401


def test_trial_state_isolated_per_ip(client, state_file):
    """不同 X-Real-IP 各自计数，互不挤占。"""
    for i in range(3):
        client.post("/api/tasks", json={"prompt": "t"}, headers={"X-Real-IP": "1.1.1.1"})
    resp = client.post(
        "/api/tasks", json={"prompt": "t"}, headers={"X-Real-IP": "2.2.2.2"}
    )
    assert resp.status_code != 401


# ====== 访问令牌解锁 ======

def test_unlock_token_grants_permanent_access(client, state_file):
    """输对令牌一次 → 该 IP 永久解锁，后续调用不再要求令牌。"""
    for i in range(3):
        client.post("/api/tasks", json={"prompt": f"t{i}"})
    resp = client.post(
        "/api/tasks", json={"prompt": "t4"}, headers={"X-Access-Token": UNLOCK_TOKEN}
    )
    assert resp.status_code != 401
    # 解锁状态落盘，之后不带令牌也放行
    resp = client.post("/api/tasks", json={"prompt": "t5"})
    assert resp.status_code != 401


def test_unlock_token_via_query_param(client, state_file):
    """?token= 查询参数同样可解锁（低频脚本场景）。"""
    for i in range(3):
        client.post("/api/tasks", json={"prompt": f"t{i}"})
    resp = client.post(f"/api/tasks?token={UNLOCK_TOKEN}", json={"prompt": "t4"})
    assert resp.status_code != 401


def test_wrong_token_consumes_trial(client, state_file):
    """令牌错误不报 403（按免费次数走），错令牌调用也计数。"""
    for i in range(4):
        resp = client.post(
            "/api/tasks", json={"prompt": "t"}, headers={"X-Access-Token": "wrong"}
        )
    st = json.loads(state_file.read_text())
    assert st["ips"]["testclient"] == 3  # 第 4 次被拒，未计入
    assert resp.status_code == 401


# ====== 管理凭证 key（兼容旧集成）======

def test_correct_api_key_bypasses_trial(client, state_file, monkeypatch):
    """带正确 X-API-Key 的调用放行且不消耗免费次数。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    for i in range(5):
        resp = client.post(
            "/api/tasks", json={"prompt": "t"}, headers={"X-API-Key": API_KEY}
        )
        assert resp.status_code != 401
    assert not state_file.exists()


def test_key_via_query_param_bypasses_trial(client, state_file, monkeypatch):
    """?key= 查询参数（旧演示链接形式）等价于头凭证。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    for i in range(5):
        resp = client.post(f"/api/tasks?key={API_KEY}", json={"prompt": "t"})
        assert resp.status_code != 401
    assert not state_file.exists()


def test_wrong_api_key_falls_back_to_trial(client, state_file, monkeypatch):
    """错误的 key 不再报 403：忽略之，按免费试用逻辑走。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    for i in range(4):
        resp = client.post(
            "/api/tasks", json={"prompt": "t"}, headers={"X-API-Key": "bad-key"}
        )
    assert resp.status_code == 401
    assert resp.json()["need_token"] is True


# ====== CSRF Content-Type 防线（保留）======

def test_cross_origin_write_requires_json(client, monkeypatch):
    """跨源写请求非 JSON Content-Type → 415（防 text/plain 简单请求绕过 CORS）。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)  # 管理凭证开着也一样拦
    resp = client.post(
        "/api/chat",
        content="message=hi",
        headers={"Origin": "http://evil.example", "Content-Type": "text/plain"},
    )
    assert resp.status_code == 415


def test_same_origin_json_write_passes(client, state_file):
    """同源 JSON 写请求正常进入试用计数流程（不被 CSRF 误伤）。"""
    resp = client.post("/api/tasks", json={"prompt": "t"})
    assert resp.status_code != 415


# ====== 审批端点纳入防护（2026-09-16 自批漏洞修复）======

def test_approval_endpoint_counts_trial(client, state_file):
    """审批 POST 计入免费次数（原实现不计，访客可无鉴权反复自批命令）。"""
    resp = client.post("/api/tasks/demo-task/approvals/ap-1", json={"action": "approve"})
    assert resp.status_code != 401  # 免费期内放行（路由层 404 不影响中间件计数）
    st = json.loads(state_file.read_text())
    assert st["ips"]["testclient"] == 1


def test_approval_endpoint_blocked_after_trial(client, state_file):
    """免费次数用完后审批请求被拦：访客无法再自批自己触发的命令。"""
    for i in range(3):
        client.post("/api/tasks", json={"prompt": f"t{i}"})
    resp = client.post("/api/tasks/demo-task/approvals/ap-1", json={"action": "approve"})
    assert resp.status_code == 401
    assert resp.json()["need_token"] is True


def test_approval_with_token_still_works(client, state_file):
    """带正确令牌的审批放行（解锁用户 / 管理凭证不受影响）。"""
    for i in range(3):
        client.post("/api/tasks", json={"prompt": f"t{i}"})
    resp = client.post(
        "/api/tasks/demo-task/approvals/ap-1",
        json={"action": "approve"},
        headers={"X-Access-Token": UNLOCK_TOKEN},
    )
    assert resp.status_code != 401

