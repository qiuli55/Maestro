"""launcher._spawn_server 传参 + 环境变量校验。"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def test_spawn_server_uses_src_dir(tmp_path, monkeypatch):
    """_spawn_server 必须 cwd=src/（让 -m maestro.server 解析模块）。"""
    from maestro import launcher

    monkeypatch.setattr("maestro.launcher.runtime.project_root", lambda: tmp_path)
    monkeypatch.setattr("maestro.launcher._find_free_port", lambda: 8888)
    with patch.object(subprocess, "Popen") as mp:
        mp.return_value = MagicMock()
        launcher._spawn_server(8888)
        args, kwargs = mp.call_args
        # 第一个位置参数是 argv 列表
        cmd = args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:] == ["-m", "maestro.server"]
        # cwd 指向 src 子目录
        assert Path(kwargs["cwd"]).name == "maestro"  # launcher 在 src/maestro/launcher.py
        assert Path(kwargs["cwd"]).is_dir()


def test_spawn_server_env_includes_port_and_home(tmp_path, monkeypatch):
    """环境变量必须含 MAESTRO_PORT / MAESTRO_HOST / MAESTRO_HOME。"""
    from maestro import launcher

    monkeypatch.setattr("maestro.launcher.runtime.project_root", lambda: tmp_path)
    monkeypatch.setattr("maestro.launcher._find_free_port", lambda: 8889)
    with patch.object(subprocess, "Popen") as mp:
        mp.return_value = MagicMock()
        launcher._spawn_server(8889)
        env = mp.call_args.kwargs["env"]
        assert env["MAESTRO_PORT"] == "8889"
        assert env["MAESTRO_HOST"] == "127.0.0.1"
        # MAESTRO_HOME 来自 project_root（不应是 MAESTRO_HOME 已设的 env）
        assert env.get("MAESTRO_HOME") == str(tmp_path)


def test_spawn_server_no_window_flag_on_windows(monkeypatch, tmp_path):
    """Windows 下传递 CREATE_NO_WINDOW（避免弹黑色 cmd 框）。"""
    from maestro import launcher

    monkeypatch.setattr("maestro.launcher.runtime.project_root", lambda: tmp_path)
    monkeypatch.setattr("maestro.launcher._find_free_port", lambda: 8890)
    with patch.object(subprocess, "Popen") as mp:
        mp.return_value = MagicMock()
        monkeypatch.setattr(sys, "platform", "win32")
        launcher._spawn_server(8890)
        flags = mp.call_args.kwargs.get("creationflags", 0)
        assert flags & subprocess.CREATE_NO_WINDOW, "Windows 必须传 CREATE_NO_WINDOW"
