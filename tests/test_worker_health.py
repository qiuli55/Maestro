"""Worker 健康检查 + /api/workers/health 端点测试。

跨平台：Windows / POSIX 都跑通。
- Windows：写 .bat 批处理，subprocess.run 调 .bat
- POSIX：写 #!/bin/sh 脚本，chmod +x
- sleep 探测超时用 Python 脚本（不依赖系统 sleep）
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

import pytest

from maestro.workers import base as wbase


# ====== helpers ======

def _make_fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    """跨平台创建一个能跑的可执行脚本。

    Windows: .bat 批处理（@echo 输出，@exit /b N 退出码）
    POSIX: .sh 脚本（#!/bin/sh + chmod +x）
    """
    if os.name == "nt":
        bin_path = tmp_path / f"{name}.bat"
        bin_path.write_text(body, encoding="utf-8")
    else:
        bin_path = tmp_path / name
        bin_path.write_text(body, encoding="utf-8")
        bin_path.chmod(0o755)
    return bin_path


def _make_python_script(tmp_path: Path, name: str, body: str) -> Path:
    """跨平台创建 Python 脚本（用于 sleep 等需要可控行为的探测）。"""
    bin_path = tmp_path / f"{name}.py"
    bin_path.write_text(body, encoding="utf-8")
    return bin_path


# ====== 测试用的 worker 类 ======

class _FakeHealthyWorker(wbase.SubprocessWorker):
    name = "fake_healthy"
    bin = ""
    health_probe_args = ["--version"]
    health_probe_timeout = 5.0
    health_cache_ttl = 30.0


# ====== check_health 单元测试 ======

def test_check_health_returns_fail_with_no_bin_configured():
    """bin 为空时返回 (False, 'bin 路径为空') —— 不是 ok。"""
    w = _FakeHealthyWorker()
    w.reset_health_cache()
    ok, reason = w.check_health()
    assert ok is False
    assert "bin 路径为空" in reason


def test_check_health_returns_ok_for_existing_bin(tmp_path):
    """bin 存在且可执行 → ok=True。"""
    body = "@echo ok\n" if os.name == "nt" else "#!/bin/sh\necho ok\n"
    fake_bin = _make_fake_bin(tmp_path, "fake_cli", body)

    w = _FakeHealthyWorker()
    w.bin = str(fake_bin)
    w.health_probe_args = []  # Windows .bat 不接 --version
    w.health_probe_timeout = 5.0
    w.reset_health_cache()
    ok, reason = w.check_health()
    assert ok is True, reason


def test_check_health_returns_fail_for_missing_bin(tmp_path):
    """bin 不存在 → ok=False。"""
    w = _FakeHealthyWorker()
    w.bin = str(tmp_path / "nonexistent_cli")
    w.reset_health_cache()
    ok, reason = w.check_health()
    assert ok is False
    assert "bin 不存在" in reason


def test_check_health_probe_timeout(tmp_path):
    """探测超时（脚本 sleep 60s）→ ok=False '探测超时'。"""
    sleep_bin = _make_python_script(tmp_path, "sleep_cli", "import time\ntime.sleep(60)\n")

    w = _FakeHealthyWorker()
    w.bin = sys.executable  # 用 python 解释器
    w.health_probe_args = [str(sleep_bin)]  # python <script>
    w.health_probe_timeout = 0.3
    w.reset_health_cache()
    start = time.monotonic()
    ok, reason = w.check_health()
    elapsed = time.monotonic() - start
    assert ok is False
    assert "探测超时" in reason
    assert elapsed < 1.0


def test_check_health_probe_skip_when_args_none(tmp_path):
    """health_probe_args=None → 跳过运行时探测，只查 bin 存在。"""
    body = "@echo ok\n" if os.name == "nt" else "#!/bin/sh\necho ok\n"
    fake_bin = _make_fake_bin(tmp_path, "fake_cli", body)

    w = _FakeHealthyWorker()
    w.bin = str(fake_bin)
    w.health_probe_args = None  # 关键：跳过探测
    w.reset_health_cache()
    ok, reason = w.check_health()
    assert ok is True


def test_check_health_probe_fails_with_nonzero_exit(tmp_path):
    """探测 exit != 0 → ok=False。"""
    body = "@exit /b 1\n" if os.name == "nt" else "#!/bin/sh\nexit 1\n"
    fail_bin = _make_fake_bin(tmp_path, "fail_cli", body)

    w = _FakeHealthyWorker()
    w.bin = str(fail_bin)
    w.health_probe_timeout = 2.0
    w.health_probe_args = []
    w.reset_health_cache()
    ok, reason = w.check_health()
    assert ok is False
    assert "探测失败" in reason


def test_check_health_caches_result(tmp_path):
    """第二次调用走缓存（不重新探测）。"""
    body = "@echo ok\n" if os.name == "nt" else "#!/bin/sh\necho ok\n"
    fake_bin = _make_fake_bin(tmp_path, "fake_cli", body)

    w = _FakeHealthyWorker()
    w.bin = str(fake_bin)
    w.health_probe_args = []
    w.health_probe_timeout = 5.0
    w.health_cache_ttl = 60.0
    w.reset_health_cache()

    start = time.monotonic()
    ok1, r1 = w.check_health()
    first_elapsed = time.monotonic() - start

    start = time.monotonic()
    ok2, r2 = w.check_health()
    second_elapsed = time.monotonic() - start

    assert ok1 == ok2 is True
    assert r1 == r2 == "ok"
    # 第二次应明显快（缓存命中，跳过 subprocess）
    assert second_elapsed < 0.05, f"缓存未生效：第二次耗时 {second_elapsed:.3f}s"


def test_check_health_cache_expires_after_ttl(tmp_path):
    """TTL 过期后重新探测。"""
    body = "@echo ok\n" if os.name == "nt" else "#!/bin/sh\necho ok\n"
    fake_bin = _make_fake_bin(tmp_path, "fake_cli", body)

    w = _FakeHealthyWorker()
    w.bin = str(fake_bin)
    w.health_probe_args = []
    w.health_probe_timeout = 5.0
    w.health_cache_ttl = 0.1
    w.reset_health_cache()

    w.check_health()
    time.sleep(0.15)  # 等缓存过期
    ok, reason = w.check_health()
    assert ok is True


def test_check_health_reset_clears_cache(tmp_path):
    """reset_health_cache 后下次 check 重新探测。"""
    body = "@echo ok\n" if os.name == "nt" else "#!/bin/sh\necho ok\n"
    fake_bin = _make_fake_bin(tmp_path, "fake_cli", body)

    w = _FakeHealthyWorker()
    w.bin = str(fake_bin)
    w.health_cache_ttl = 60.0
    w.reset_health_cache()
    w.check_health()
    assert w._health_cache
    w.reset_health_cache()
    assert not w._health_cache


# ====== workers/health API 端点 ======

def test_workers_health_endpoint_all_ok(tmp_path):
    """所有 worker 健康 → 200 + all_ok=True。"""
    from maestro.workers import base as wbase_mod
    
    body = "@echo ok\n" if os.name == "nt" else "#!/bin/sh\necho ok\n"
    fake_bin = _make_fake_bin(tmp_path, "fake_cli_for_health", body)

    registered_classes = list(wbase_mod._REGISTRY.values())
    original_bins = {}
    original_args = {}
    for cls in registered_classes:
        # 只处理 SubprocessWorker 子类且有 bin 属性的（embedded/minimax/休眠 worker 跳过）
        if not issubclass(cls, wbase.SubprocessWorker) or not hasattr(cls, "bin"):
            continue
        original_bins[cls] = cls.bin
        original_args[cls] = cls.health_probe_args
        cls.bin = str(fake_bin)
        cls.health_probe_args = []
        cls.health_probe_timeout = 2.0
        cls.reset_health_cache()

    try:
        from fastapi.testclient import TestClient
        from maestro.server import app
        client = TestClient(app)
        resp = client.get("/api/workers/health")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["all_ok"] is True
        for name, info in body["workers"].items():
            assert info["ok"] is True, f"{name} 不健康：{info['reason']}"
    finally:
        for cls, orig_bin in original_bins.items():
            cls.bin = orig_bin
            cls.health_probe_args = original_args[cls]
            cls.reset_health_cache()


def test_workers_health_endpoint_degraded_returns_503():
    """有 worker 不健康 → 503 + degraded list。"""
    from maestro.workers import base as wbase_mod
    from maestro.workers import octo  # noqa: F401

    if "octo" not in wbase_mod._REGISTRY:
        pytest.skip("octo worker not available")

    cls = wbase_mod._REGISTRY["octo"]
    orig_bin = cls.bin
    cls.bin = "Z:/nonexistent/path/cli.exe"
    cls.health_probe_args = []
    cls.health_probe_timeout = 1.0
    cls.reset_health_cache()
    try:
        from fastapi.testclient import TestClient
        from maestro.server import app
        client = TestClient(app)
        resp = client.get("/api/workers/health")
        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert body["all_ok"] is False
        assert "octo" in body.get("degraded", [])
    finally:
        cls.bin = orig_bin
        cls.reset_health_cache()


# ====== orchestrator dispatch 时 check_health fail-fast ======

def test_orchestrator_dispatch_fails_fast_when_worker_unhealthy(tmp_path, monkeypatch):
    """orchestrator dispatch 时若 worker 不健康，子任务立即标 FAILED（不进入 spawn）。"""
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "t.db"))
    from maestro import db, orchestrator
    from maestro.workers import base as wbase_mod

    cls = wbase_mod._REGISTRY.get("fake")
    if cls is None:
        pytest.skip("fake worker not registered")

    orig_bin = cls.bin
    orig_args = cls.health_probe_args
    cls.bin = "Z:/no/such/path/fake"
    cls.health_probe_args = None  # 跳过探测，只查 bin 存在
    cls.reset_health_cache()

    try:
        conn = db.init_db()
        try:
            db.create_task(conn, "t_unhealthy", "test")
            db.set_task_params(conn, "t_unhealthy", scenario="b", worker_type="fake", parallel=False, no_merge=True)
            db.add_subtasks(conn, "t_unhealthy", [
                {"id": "t_unhealthy_s0", "desc": "x", "worker_type": "fake"},
            ])
            db.set_task_status(conn, "t_unhealthy", db.READY)

            orchestrator.execute_task(conn, "t_unhealthy", timeout=10)
            sub = db.get_subtask(conn, "t_unhealthy_s0")
            assert sub["status"] == db.FAILED
            assert "worker 不可用" in (sub["error"] or "")
            events = conn.execute(
                "SELECT event FROM task_events WHERE subtask_id=? ORDER BY id",
                ("t_unhealthy_s0",),
            ).fetchall()
            event_names = [e["event"] for e in events]
            assert any("WORKER_UNHEALTHY" in e for e in event_names), \
                f"期望 WORKER_UNHEALTHY 事件，实有：{event_names}"
        finally:
            conn.close()
    finally:
        cls.bin = orig_bin
        cls.health_probe_args = orig_args
        cls.reset_health_cache()