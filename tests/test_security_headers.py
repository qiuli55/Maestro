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


def test_permissions_policy_on_html(client):
    """/（HTML）含 Permissions-Policy 关闭强大 API。"""
    r = client.get("/")
    pp = r.headers.get("Permissions-Policy", "")
    assert "camera=()" in pp, "camera 应被禁用"
    assert "microphone=()" in pp, "microphone 应被禁用"
    assert "geolocation=()" in pp, "geolocation 应被禁用"
    assert "fullscreen=(self)" in pp, "fullscreen 仅同源允许"


def test_permissions_policy_on_metrics(client):
    """/metrics（文本）也注入 Permissions-Policy（统一响应头，不按路由分支）。"""
    r = client.get("/metrics")
    assert "Permissions-Policy" in r.headers


def test_csp_stricter_on_html(client):
    """/（HTML）CSP 含收紧项：object-src 'none'、font-src、upgrade-insecure-requests。"""
    r = client.get("/")
    csp = r.headers.get("Content-Security-Policy", "")
    assert "object-src 'none'" in csp, "应封禁插件/Flash/旧 ActiveX"
    assert "font-src 'self' data:" in csp, "应限制字体来源"
    assert "upgrade-insecure-requests" in csp, "应升级 http→https"
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp, "应替代 X-Frame-Options 禁止 iframe 嵌入"


def test_csp_not_on_text_event_stream(client):
    """/api/chat/feed（text/event-stream）不注入 CSP（流式响应污染 CSP 会断浏览器解析）。"""
    with client.stream("GET", "/api/chat/feed?conv_id=conv_x&since_id=0&poll=0.1") as r:
        try:
            assert "Content-Security-Policy" not in r.headers
        finally:
            r.close()
