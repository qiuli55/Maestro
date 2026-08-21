"""pytest 公共：把 src 加入路径，并注册一个不调子进程的 FakeWorker 供测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
from maestro.workers.base import SubprocessWorker, WorkerResult, register


@register
class FakeWorker(SubprocessWorker):
    """测试用：直接返回固定文本，不启动子进程、不读 API key。"""

    name = "fake"

    def build_command(self, prompt, workdir):
        return ["echo", "fake"]

    def spawn(self, prompt, workdir, timeout, task_id=None, subtask_id=None):
        return WorkerResult(f"FAKE-OUT: {prompt[:40]}", "", False, 0)


@register
class FlakyWorker(SubprocessWorker):
    """测试用：永远失败，验证失败隔离 / 重试。"""

    name = "flaky"

    def build_command(self, prompt, workdir):
        return ["false"]

    def spawn(self, prompt, workdir, timeout, task_id=None, subtask_id=None):
        return WorkerResult("", "boom", False, 1)


@register
class BoomWorker(SubprocessWorker):
    """测试用：spawn 直接抛异常（模拟 worker 内部崩溃/未知错误），
    验证编排器把异常隔离为子任务 FAILED 而非拖垮整任务。"""

    name = "boom"

    def build_command(self, prompt, workdir):
        return ["echo", "boom"]

    def spawn(self, prompt, workdir, timeout, task_id=None, subtask_id=None):
        raise RuntimeError("worker 内部崩溃")


# 触发真实 worker 模块注册（opencode/octo/embedded）
from maestro.workers import opencode, octo, embedded  # noqa: F401,E402

import os
import shutil
from maestro.workers import base as wbase_mod


@pytest.fixture(autouse=True)
def _fake_workers_have_bin():
    """给 conftest 里的 FakeWorker/FlakyWorker/BoomWorker 设个真实存在的 bin。

    这些 worker 重写了 spawn() 不调子进程，但 S1.2 引入的 check_health 会验证 bin 存在。
    用 shutil.which("cmd.exe" if os.name == "nt" else "echo") 拿真实命令，避免破坏测试。
    """
    real_bin = shutil.which("cmd.exe" if os.name == "nt" else "echo")
    if real_bin:
        for name in ("fake", "flaky", "boom"):
            cls = wbase_mod._REGISTRY.get(name)
            if cls is not None and hasattr(cls, "bin"):
                cls.bin = real_bin
                cls.health_probe_args = None  # 跳过探测，只验证 bin 存在
                cls.health_probe_timeout = 1.0
                cls.reset_health_cache()
    yield


@pytest.fixture
def tmp_db(tmp_path):
    from maestro import db

    conn = db.init_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _isolate_outputs(monkeypatch, tmp_path):
    # 所有测试把产出写到 tmp，避免污染项目 outputs/
    monkeypatch.setattr("maestro.orchestrator.OUTPUTS_ROOT", tmp_path / "outputs")
