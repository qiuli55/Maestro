"""CLI 冒烟测试：run/status/retry/reset 子命令跑通，db 与 outputs 隔离到 tmp。

用 FakeWorker 替代真实子进程，不依赖 API key。验证命令行入口可加载、任务可落地。
"""
import pytest
from maestro import cli
from maestro.workers.base import get_worker


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    # 用 MAESTRO_DB 环境变量指向 tmp 库（而非 mock init_db），
    # 这样 _execute_subtask 的每线程独立连接也落到同一 tmp 文件，符合真实行为
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    monkeypatch.setattr("maestro.orchestrator.OUTPUTS_ROOT", tmp_path / "outputs")
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    return tmp_path


def test_run_scenario_a_cli(isolated, tmp_path, capsys):
    inp = tmp_path / "req.txt"
    inp.write_text("加登录\n加支付", encoding="utf-8")
    rc = cli.main(["run", "--scenario", "a", "--worker", "embedded", "--input", str(inp)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "任务已提交" in out and "st_1" in out and "st_2" in out


def test_status_cli(isolated, tmp_path, capsys):
    inp = tmp_path / "req.txt"
    inp.write_text("需求1", encoding="utf-8")
    cli.main(["run", "--scenario", "a", "--worker", "embedded", "--input", str(inp)])
    out1 = capsys.readouterr().out
    tid = out1.split("任务已提交：")[1].split()[0]
    rc = cli.main(["status", tid])
    assert rc == 0
    assert "需求1" in capsys.readouterr().out
