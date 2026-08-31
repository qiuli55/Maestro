"""运行时路径解析：源码运行 vs PyInstaller 打包后的统一入口。

打包成 exe 后 `Path(__file__).resolve().parents[N]` 解析到 `sys._MEIPASS` 只读目录，
写 SQLite / outputs 都会失败。本模块提供唯一权威的项目根解析逻辑：

  - MAESTRO_HOME 环境变量（推荐：exe 同级目录的绝对路径）
  - sys.executable 解析（PyInstaller frozen 场景）
  - 源码运行时从 src/maestro/runtime.py 回溯两级

调用方：config/db/orchestrator/metrics 统一改走 `runtime.project_root()`。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def project_root() -> Path:
    """解析项目根目录（配置文件、数据库、产物均挂其下）。

    优先级：
    1. MAESTRO_HOME 环境变量（运维可显式指定）
    2. PyInstaller frozen：用 sys.executable 的父目录（onedir 模式即 _internal/ 的同级）
    3. 源码运行：从 src/maestro/runtime.py 回溯两级到 repo 根
    """
    env = os.environ.get("MAESTRO_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        # PyInstaller onefile / onedir：sys.executable 指向打包后的 exe
        return Path(sys.executable).resolve().parent
    # 源码运行：src/maestro/runtime.py → parents[2] 是 repo 根
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """数据库与运行时数据目录（项目根/data）。首次调用自动创建。"""
    p = project_root() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def outputs_dir() -> Path:
    """任务产物目录（项目根/outputs）。首次调用自动创建。"""
    p = project_root() / "outputs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_data_dir() -> Path:
    """跨平台的"用户级数据"目录（用于备份、port.txt 等非打包资源）。

    Windows: %APPDATA%/Maestro；macOS: ~/Library/Application Support/Maestro；
    Linux: ${XDG_DATA_HOME:-~/.local/share}/Maestro。
    """
    import platform

    sys_name = platform.system()
    if sys_name == "Windows":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        p = base / "Maestro"
    elif sys_name == "Darwin":
        p = Path.home() / "Library" / "Application Support" / "Maestro"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
        p = base / "Maestro"
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_frozen() -> bool:
    """是否处于 PyInstaller 打包后的 frozen 环境（用于日志、行为分支）。"""
    return bool(getattr(sys, "frozen", False))
