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
