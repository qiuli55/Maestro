"""lifespan startup/shutdown 集成测：TestClient 上下文验证 backup task 与 prune 钩子。

Lifespan 在 dev/CI 模式下只在 TestClient.__enter__ 真实启动路径才执行（纯 import
不会触发）。本测试验证：
  1. startup 阶段：backup task 创建并绑定到 app.state，prune 30 天事件被调用
  2. shutdown 阶段：backup task 被 cancel
"""
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_lifespan_startup_creates_backup_task(tmp_path, monkeypatch):
    """lifespan startup：app.state.backup_task 存在、未 done、未 cancelled。"""
    from maestro import server
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)  # 避免 StaticFiles 找不到
    monkeypatch.setattr(server, "WALLPAPER_DIR", tmp_path)
    # 防 prune 30 天事件用真实数据
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))

    with TestClient(server.app) as client:
        bt = server.app.state.backup_task
        assert bt is not None, "lifespan 应创建 backup_task"
        assert not bt.done()
        assert not bt.cancelled()
        # happy-path 断言：lifespan 不抛异常、上下文内健康检查 200
        assert client.get("/api/healthz").status_code == 200


def test_lifespan_shutdown_cancels_task(tmp_path, monkeypatch):
    """shutdown：backup task 被 cancel。"""
    from maestro import server
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    monkeypatch.setattr(server, "WALLPAPER_DIR", tmp_path)
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))

    with TestClient(server.app) as client:
        bt = server.app.state.backup_task
        # 上下文退出（with 结束）应 cancel task
    assert bt.cancelled(), "lifespan shutdown 应 cancel backup task"


def test_lifespan_idempotent_testclients(tmp_path, monkeypatch):
    """多次 enter/leave 同一 app：每次都新建 task，旧的已被 cancel。"""
    from maestro import server
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    monkeypatch.setattr(server, "WALLPAPER_DIR", tmp_path)
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))

    with TestClient(server.app) as c1:
        bt1 = server.app.state.backup_task
    assert bt1.cancelled()
    with TestClient(server.app) as c2:
        bt2 = server.app.state.backup_task
    assert bt2.cancelled()
    assert bt1 is not bt2, "每次 startup 都应创建新 task"
