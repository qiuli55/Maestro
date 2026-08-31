"""SecurityHeadersMiddleware + /metrics 白名单集成测。"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from maestro import server
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    monkeypatch.setattr(server, "WALLPAPER_DIR", tmp_path)
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m.db"))
    with TestClient(server.app) as c:
        yield c


def test_security_headers_on_html(client):
    """/（HTML 响应）含全套安全头：nosniff / X-Frame-Options / Referrer-Policy / CSP。"""
    r = client.get("/")
    assert r.status_code == 200
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer"
    csp = h["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp  # 没允许 unsafe-inline → 阻止旧内联脚本
    assert "style-src 'self' 'unsafe-inline'" in csp  # 内联样式仍允许


def test_security_headers_on_metrics(client):
    """/metrics（文本响应）注入 nosniff/X-Frame/Referrer；不注入 CSP。"""
    r = client.get("/metrics")
    assert r.status_code == 200
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer"
    assert "Content-Security-Policy" not in h  # 文本响应不污染 CSP


def test_security_headers_on_api_json(client):
    """/api/... JSON 响应：nosniff + X-Frame + Referrer，无 CSP。"""
    r = client.get("/api/healthz")
    assert r.status_code == 200
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer"
    assert "Content-Security-Policy" not in h


def test_metrics_whitelisted_with_api_key(tmp_path, monkeypatch):
    """/metrics 在设了 MAESTRO_API_KEY 时仍免鉴权——监控探针不能被锁。"""
    from maestro import server
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    monkeypatch.setattr(server, "WALLPAPER_DIR", tmp_path)
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("MAESTRO_API_KEY", "secret-key-for-prometheus")
    with TestClient(server.app) as c:
        # 无 key 头：应 200
        assert c.get("/metrics").status_code == 200
        assert c.get("/api/healthz").status_code == 200  # 同级白名单
        # 带错误 key：业务端点应 403
        assert c.get("/api/tasks", headers={"X-API-Key": "wrong"}).status_code == 403
