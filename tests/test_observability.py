"""Observability 模块测试：结构化日志 + 健康检查端点。"""
from __future__ import annotations

import json
import logging
import sys

sys.path.insert(0, "src")

import pytest
from fastapi.testclient import TestClient

from maestro import observability


@pytest.fixture
def reset_logging():
    """每个测试前重置全局 logging 状态。"""
    observability._CONFIGURED = False
    yield
    observability._CONFIGURED = False


def test_setup_logging_json_format(capsys, reset_logging):
    """JSON 模式应输出可解析 JSON，含 ts/level/logger/msg/extras。"""
    observability.setup_logging(level="INFO", fmt="json")
    log = observability.get_logger("maestro.test")
    log.info("hello", extra={"task_id": "t1", "count": 3})

    captured = capsys.readouterr()
    line = captured.out.strip()
    data = json.loads(line)  # 必须能解析为 JSON
    assert data["level"] == "INFO"
    assert data["logger"] == "maestro.test"
    assert data["msg"] == "hello"
    assert data["task_id"] == "t1"
    assert data["count"] == 3
    # ISO8601 时间戳
    assert "T" in data["ts"]


def test_setup_logging_text_format(capsys, reset_logging):
    """text 模式应输出人类可读格式（含 level + 时间）。"""
    observability.setup_logging(level="DEBUG", fmt="text")
    log = observability.get_logger("maestro.test")
    log.warning("warning msg", extra={"key": "v"})

    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "warning msg" in captured.out
    assert "key='v'" in captured.out


def test_setup_logging_idempotent(capsys, reset_logging):
    """重复 setup_logging 不应重复添加 handler。"""
    observability.setup_logging(level="INFO", fmt="json")
    observability.setup_logging(level="INFO", fmt="json")  # 第二次幂等
    log = observability.get_logger("maestro.test")
    log.info("once")

    captured = capsys.readouterr()
    # 应只一行输出（不是两行）
    lines = [l for l in captured.out.strip().split("\n") if l.strip()]
    assert len(lines) == 1


def test_setup_logging_level_filter(capsys, reset_logging):
    """高于设定 level 的日志被丢弃。"""
    observability.setup_logging(level="WARNING", fmt="text")
    log = observability.get_logger("maestro.test")
    log.info("should be filtered")
    log.warning("should appear")

    captured = capsys.readouterr()
    assert "should be filtered" not in captured.out
    assert "should appear" in captured.out


def test_setup_logging_env_vars(monkeypatch, capsys, reset_logging):
    """LOG_LEVEL / LOG_FORMAT 环境变量应生效。"""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_FORMAT", "json")
    observability.setup_logging()
    log = observability.get_logger("maestro.test")
    log.debug("debug should appear")

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["level"] == "DEBUG"


def test_get_logger_returns_named_logger(reset_logging):
    """get_logger 应返回标准 logging.Logger，名字正确。"""
    log = observability.get_logger("maestro.foo.bar")
    assert isinstance(log, logging.Logger)
    assert log.name == "maestro.foo.bar"


# ============================================================================
# /api/healthz + /api/ready 测试
# ============================================================================

@pytest.fixture
def client(monkeypatch):
    """FastAPI 测试客户端（用 in-memory db + 临时 MAESTRO_DB）。"""
    import os, tempfile
    from maestro import server
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        monkeypatch.setenv("MAESTRO_DB", db_path)
        # 清空 server 模块的 worker/executor 缓存
        client = TestClient(server.app)
        yield client


def test_healthz_returns_200(client):
    """/api/healthz 永远返回 200（liveness 探针不查依赖）。"""
    r = client.get("/api/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready_checks_db_and_llm(client):
    """/api/ready 应返回 db + llm + workers 检查结果。"""
    r = client.get("/api/ready")
    assert r.status_code in (200, 503)  # 200 全过 / 503 依赖缺失
    body = r.json()
    assert "status" in body
    assert "checks" in body
    assert "db" in body["checks"]
    assert "llm" in body["checks"]
    assert "workers" in body["checks"]
    assert "version" in body


def test_ready_returns_db_ok(client):
    """/api/ready 应能查 SQLite（SELECT 1）。"""
    r = client.get("/api/ready")
    body = r.json()
    # db 应正常（test fixture 创建了 db）
    assert body["checks"]["db"] == "ok"


def test_ready_lists_registered_workers(client):
    """/api/ready 应列出已注册 worker 名字。"""
    r = client.get("/api/ready")
    body = r.json()
    names = body["checks"]["workers"]["names"]
    # 至少要有 fake（conftest 注册）
    assert "fake" in names
    # 也应有标准 worker（opencode/octo/embedded）
    assert "embedded" in names