"""jail 沙箱包装测试：开关条件 / bwrap 参数构造 / workdir 边界。

场景覆盖：未配置不启用、文件夹缺失不启用、bwrap 缺失不启用、
启用后参数完备（清 env / 专属文件夹可写挂载 / 起始目录正确）、
workdir 越界退回文件夹根。
"""
from __future__ import annotations

from maestro import jail


def test_disabled_without_env(monkeypatch):
    """未设 MAESTRO_JAIL_ROOT → 原样返回（开发机 / Windows 零影响）。"""
    monkeypatch.delenv("MAESTRO_JAIL_ROOT", raising=False)
    argv = ["ls", "-la"]
    assert jail.wrap(argv, "/tmp") == argv


def test_disabled_when_dir_missing(monkeypatch, tmp_path):
    """专属文件夹不存在 → 原样返回（不抛异常）。"""
    monkeypatch.setenv("MAESTRO_JAIL_ROOT", str(tmp_path / "none"))
    argv = ["ls"]
    assert jail.wrap(argv, "/tmp") == argv


def test_disabled_when_bwrap_missing(monkeypatch, tmp_path):
    """bwrap 未安装 → 原样返回（服务照常，不因沙箱工具缺失而挂）。"""
    d = tmp_path / "ws"
    d.mkdir()
    monkeypatch.setenv("MAESTRO_JAIL_ROOT", str(d))
    monkeypatch.setattr(jail.shutil, "which", lambda _name: None)
    argv = ["ls"]
    assert jail.wrap(argv, str(d)) == argv


def test_wrap_builds_sandbox(monkeypatch, tmp_path):
    """启用：命令包进 bwrap，清空环境变量、专属文件夹可写挂载、起始目录正确。"""
    d = tmp_path / "ws"
    work = d / "task_1"
    work.mkdir(parents=True)
    monkeypatch.setenv("MAESTRO_JAIL_ROOT", str(d))
    monkeypatch.setattr(jail.shutil, "which", lambda _name: "/usr/bin/bwrap")
    out = jail.wrap(["ls", "/"], str(work))
    assert out[0] == "/usr/bin/bwrap"
    assert out[-3:] == ["--", "ls", "/"]
    assert "--clearenv" in out and "--die-with-parent" in out
    pair = dict(zip(out, out[1:]))
    assert pair.get("--bind") == str(d)
    assert pair.get("--chdir") == str(work)


def test_workdir_outside_falls_back(monkeypatch, tmp_path):
    """workdir 在专属文件夹外 → 起始目录退回文件夹根（防御式兜底）。"""
    d = tmp_path / "ws"
    d.mkdir()
    monkeypatch.setenv("MAESTRO_JAIL_ROOT", str(d))
    monkeypatch.setattr(jail.shutil, "which", lambda _name: "/usr/bin/bwrap")
    out = jail.wrap(["ls"], "/etc")
    pair = dict(zip(out, out[1:]))
    assert pair.get("--chdir") == str(d)


def test_no_workdir_uses_root(monkeypatch, tmp_path):
    """无 workdir（mmx 场景）→ 起始目录为文件夹根。"""
    d = tmp_path / "ws"
    d.mkdir()
    monkeypatch.setenv("MAESTRO_JAIL_ROOT", str(d))
    monkeypatch.setattr(jail.shutil, "which", lambda _name: "/usr/bin/bwrap")
    out = jail.wrap(["ls"])
    pair = dict(zip(out, out[1:]))
    assert pair.get("--chdir") == str(d)
