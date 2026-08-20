"""内嵌 worker 工具调用测试：读文件链路 + 工具 schema 传递。

不依赖 API key，mock llm.complete_with_tools。
"""
import sys
from pathlib import Path

sys.path.insert(0, "src")

import pytest

from maestro.workers.embedded import EmbeddedWorker, _READ_TOOL, _RUNCMD_TOOL, _executor


def test_read_tool_schema():
    """read_file 工具 schema 完整（OpenAI function 格式）。"""
    assert _READ_TOOL["type"] == "function"
    assert _READ_TOOL["function"]["name"] == "read_file"
    assert "path" in _READ_TOOL["function"]["parameters"]["properties"]
    assert _READ_TOOL["function"]["parameters"]["required"] == ["path"]


def test_runcmd_tool_schema():
    """run_command 工具 schema 完整。"""
    assert _RUNCMD_TOOL["type"] == "function"
    assert _RUNCMD_TOOL["function"]["name"] == "run_command"
    assert "command" in _RUNCMD_TOOL["function"]["parameters"]["properties"]


def test_executor_runs_readonly_command():
    out = _executor(".")("run_command", {"command": "echo hi_runcmd"})
    assert "hi_runcmd" in out


def test_executor_blocks_dangerous_command():
    out = _executor(".")("run_command", {"command": "rm -rf C:\\"})
    assert "拒绝" in out


def test_executor_reads_file(tmp_path):
    (tmp_path / "code.py").write_text("print('hi')", encoding="utf-8")
    out = _executor(str(tmp_path))("read_file", {"path": "code.py"})
    assert "print('hi')" in out


def test_executor_unknown_tool():
    out = _executor(".")("delete_everything", {})
    assert "未知工具" in out


def test_spawn_calls_tools_with_workdir(monkeypatch, tmp_path):
    """spawn 把 workdir 传给读文件，工具返回内容喂回后 LLM 出结论。"""
    (tmp_path / "a.txt").write_text("文件内容ABC", encoding="utf-8")
    captured = {}

    def fake_complete_with_tools(system, user, tools, tool_executor, model=None):
        captured["tools"] = tools
        captured["workdir_result"] = tool_executor("read_file", {"path": "a.txt"})
        return "结论：文件里写的是ABC"

    monkeypatch.setattr("maestro.llm.complete_with_tools", fake_complete_with_tools)
    res = EmbeddedWorker().spawn("检查 a.txt 内容", str(tmp_path), timeout=60)
    assert res.returncode == 0
    assert "ABC" in res.output
    assert captured["tools"][0]["function"]["name"] == "read_file"
    assert "ABC" in captured["workdir_result"]


def test_spawn_returns_error_result_on_exception(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr("maestro.llm.complete_with_tools", boom)
    res = EmbeddedWorker().spawn("x", str(tmp_path), timeout=60)
    assert res.returncode == 1
    assert "LLM 挂了" in res.error
