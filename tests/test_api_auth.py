"""API 鉴权中间件测试：MAESTRO_API_KEY 启用 + 白名单 + 缺 key/错 key。

中间件总是挂载，dispatch 内每次从 env 读 key —— 所以 monkeypatch.setenv/delenv
就能切换鉴权，不需要 reimport server。
"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

import pytest
from fastapi.testclient import TestClient

from maestro.server import app


@pytest.fixture
def client(monkeypatch):
    """单一 client，monkeypatch 控制 MAESTRO_API_KEY。
    默认无 key（dev 模式放行），各测试 setenv 启用鉴权。
    """
    monkeypatch.delenv("MAESTRO_API_KEY", raising=False)
    return TestClient(app)


# ====== 基础：dev 模式 ======

def test_healthz_works_without_key(client):
    """没设 env 时 /api/healthz 也能访问。"""
    resp = client.get("/api/healthz")
    assert resp.status_code == 200


def test_workers_health_works_without_key(client):
    """没设 env 时 /api/workers/health 也能访问。"""
    resp = client.get("/api/workers/health")
    assert resp.status_code in (200, 503)


def test_business_endpoint_works_without_key(client):
    """没设 env 时 /api/tasks 直接放行（dev 模式）。"""
    resp = client.get("/api/tasks")
    assert resp.status_code == 200


# ====== 启用鉴权 ======

API_KEY = "test-secret-key-12345"


def test_healthz_allowed_when_key_set(client, monkeypatch):
    """白名单内（healthz）无需鉴权。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.get("/api/healthz")
    assert resp.status_code == 200


def test_ready_allowed_when_key_set(client, monkeypatch):
    """/api/ready 也免鉴权。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.get("/api/ready")
    assert resp.status_code == 200


def test_websocket_allowed_when_key_set(client, monkeypatch):
    """/ws/* 免鉴权（前端连 WS 不带 key）。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.get("/ws/something")
    assert resp.status_code != 401
    assert resp.status_code != 403


# ====== 业务端点强制鉴权 ======

def test_tasks_list_requires_api_key(client, monkeypatch):
    """GET /api/tasks 必须带 X-API-Key。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.get("/api/tasks")
    assert resp.status_code == 401
    assert "missing X-API-Key" in resp.json()["error"]


def test_tasks_list_wrong_key_rejected(client, monkeypatch):
    """key 错 → 403。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.get("/api/tasks", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 403
    assert "invalid X-API-Key" in resp.json()["error"]


def test_tasks_list_correct_key_works(client, monkeypatch):
    """key 对 → 200。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.get("/api/tasks", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200


def test_post_task_requires_api_key(client, monkeypatch):
    """POST /api/tasks 也必须鉴权。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.post("/api/tasks", json={"prompt": "test"})
    assert resp.status_code == 401


def test_chat_endpoint_requires_api_key(client, monkeypatch):
    """/api/chat 同样鉴权。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 401


# ====== 边界 ======

def test_empty_key_header_rejected(client, monkeypatch):
    """X-API-Key 头为空 → 401。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.get("/api/tasks", headers={"X-API-Key": ""})
    assert resp.status_code == 401


def test_whitespace_only_key_rejected(client, monkeypatch):
    """X-API-Key 仅空白 → 401（中间件 strip 后变空字符串）。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.get("/api/tasks", headers={"X-API-Key": "   "})
    assert resp.status_code == 401


def test_partial_match_rejected(client, monkeypatch):
    """key 前缀匹配不算（hmac.compare_digest 防前缀攻击）。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    prefix = API_KEY[:5]
    resp = client.get("/api/tasks", headers={"X-API-Key": prefix})
    assert resp.status_code == 403


def test_static_index_allowed_without_key(client, monkeypatch):
    """首页 / 不需要 key。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.get("/")
    assert resp.status_code not in (401, 403)


def test_wallpaper_allowed_without_key(client, monkeypatch):
    """/wallpaper 不需要 key。"""
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.get("/wallpaper")
    assert resp.status_code not in (401, 403)


def test_env_key_whitespace_stripped(client, monkeypatch):
    """MAESTRO_API_KEY 含空白被 strip（避免误配）。"""
    monkeypatch.setenv("MAESTRO_API_KEY", "  " + API_KEY + "  ")
    # 客户端用 strip 后的 key
    resp = client.get("/api/tasks", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    # 客户端用带空格的 key —— dispatch 内也会 strip
    resp = client.get("/api/tasks", headers={"X-API-Key": "  " + API_KEY + "  "})
    assert resp.status_code == 200


# ====== env 切换即时 ======

def test_env_unset_after_set_restores_open_access(client, monkeypatch):
    """设了 env → 鉴权生效；删除 env → 立即恢复 dev 模式（不需重启）。"""
    # 启用
    monkeypatch.setenv("MAESTRO_API_KEY", API_KEY)
    resp = client.get("/api/tasks")
    assert resp.status_code == 401
    # 关闭
    monkeypatch.delenv("MAESTRO_API_KEY")
    resp = client.get("/api/tasks")
    assert resp.status_code == 200