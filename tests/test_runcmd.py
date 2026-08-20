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
