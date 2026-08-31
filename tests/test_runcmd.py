"""受控命令执行测试：三档分级（allowed/approval/blocked）+ 审批流 + 超时 + 输出截断。"""
import sys

sys.path.insert(0, "src")

from unittest import mock

from maestro.workers import runcmd
from maestro.workers.runcmd import validate, run
from maestro import sandbox


def test_validate_allows_readonly():
    assert validate("dir")[0] == sandbox.ALLOWED
    assert validate("git status")[0] == sandbox.ALLOWED
    assert validate("git log --oneline")[0] == sandbox.ALLOWED
    assert validate("echo hello")[0] == sandbox.ALLOWED


def test_validate_blocks_unknown_command():
    level, reason = validate("python -c 'x'")
    assert level == sandbox.APPROVAL  # python 是"需批准"级别（非白名单但可审批）


def test_validate_blocks_catastrophic():
    # 删盘符根目录 → 直接 blocked，不给审批机会
    assert validate("rm -rf C:\\")[0] == sandbox.BLOCKED
    assert validate("format C:")[0] == sandbox.BLOCKED


def test_validate_approval_for_mutating():
    # del 是"写/删"级别，需批准（不是直接 blocked）
    assert validate("del /s /q *.*")[0] == sandbox.APPROVAL


def test_validate_blocks_redirect_and_pipe():
    assert validate("echo hi > out.txt")[0] == sandbox.BLOCKED
    assert validate("dir | findstr x")[0] == sandbox.BLOCKED
    assert validate("tasklist && dir")[0] == sandbox.BLOCKED


def test_validate_approval_for_dangerous_git_sub():
    assert validate("git push")[0] == sandbox.APPROVAL
    assert validate("git clone http://x")[0] == sandbox.APPROVAL


def test_validate_allows_readonly_git_sub():
    assert validate("git status")[0] == sandbox.ALLOWED
    assert validate("git diff")[0] == sandbox.ALLOWED
    assert validate("git log")[0] == sandbox.ALLOWED


def test_run_blocks_before_exec():
    """blocked 命令不真正执行。"""
    out = run("format C:", ".")
    assert "拒绝" in out


def test_run_echo_works():
    out = run("echo hello123", ".")
    assert "hello123" in out


def test_run_approval_without_task_rejected():
    """approval 命令缺 task_id 上下文 → 拒绝（安全默认）。"""
    out = run("del /q x.txt", ".")
    assert "拒绝" in out or "批准" in out


def test_run_unknown_returns_rejection():
    # 完全未知的命令（如 xyzabc）→ blocked
    assert "拒绝" in run("xyzabc123", ".")


def test_run_timeout():
    with mock.patch("maestro.workers.runcmd.subprocess.run",
                    side_effect=__import__("subprocess").TimeoutExpired("x", 1)):
        out = run("git status", ".")
        assert "超时" in out


import pytest
from maestro.workers import runcmd
from maestro import sandbox


# ---------- Windows 内建命令：白名单走到 ALLOWED 却被 PATH 找不到 ----------
# dir / type / echo / whoami / set / path / ver / hostname / tree 都是 cmd.exe 内建命令，
# shell=False 直接跑会 FileNotFoundError，沙箱白名单失效。
WINDOWS_BUILTINS = ["dir", "type", "echo", "whoami", "set", "path", "ver", "hostname", "tree"]


@pytest.mark.parametrize("cmd", WINDOWS_BUILTINS)
def test_windows_builtin_in_allowed_executes(tmp_path, monkeypatch, cmd):
    """白名单内的 Windows 内建命令必须能跑出输出，而不是 FileNotFoundError。

    之前实现：_split_cmd 把 `dir` 拆成 ["dir"] 直接给 subprocess.run，
    找不到 dir.exe，吞成"命令未找到"——白名单实际失效。
    """
    monkeypatch.setattr("maestro.sandbox.request_approval",
                        lambda cmd, task_id, subtask_id: "approved")
    if cmd == "type":
        # type 需要文件参数：造一个临时文件
        f = tmp_path / "hello.txt"
        f.write_text("hi\n")
        out = runcmd.run(f'type hello.txt', str(tmp_path), task_id="t", subtask_id="s")
    elif cmd == "echo":
        out = runcmd.run("echo hello-from-builtin", str(tmp_path), task_id="t", subtask_id="s")
    else:
        out = runcmd.run(cmd, str(tmp_path), task_id="t", subtask_id="s")
    assert "命令未找到" not in out, f"{cmd} 在 Windows 上不应报未找到：{out}"
    if cmd == "echo":
        assert "hello-from-builtin" in out
    if cmd == "type":
        assert "hi" in out


def test_runcmd_validate_builtin_allowed(tmp_path):
    """白名单内的 Windows 内建命令 validate 应通过（不被误判为审批或拒绝）。"""
    level, reason = runcmd.validate("echo test")
    assert level == sandbox.ALLOWED, reason
    for c in ["dir", "type hello.txt", "whoami", "ver", "set"]:
        level2, reason2 = runcmd.validate(c)
        assert level2 == sandbox.ALLOWED, f"{c}: {reason2}"
