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
    2. PyInstaller frozen：sys._MEIPASS 是 _internal/，含 maestro/ 子包
       但 _internal/ 的父级 dist/<appname>/ 才是用户感知的"项目根"
       （configs/web/wallpaper 在 _internal 的同级目录下）。但生产镜像惯例
       是把 configs 放在 _internal 之外——这里保持 onedir 默认布局兼容。
    3. 源码运行：从 src/maestro/runtime.py 回溯两级到 repo 根
    """
    env = os.environ.get("MAESTRO_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        # PyInstaller onefile / onedir：sys.executable 的父目录即
        # 部署根（dist/<appname>/ 含 Maestro.exe 与 _internal/）。
        return Path(sys.executable).resolve().parent
    # 源码运行：src/maestro/runtime.py → parents[2] 是 repo 根
    return Path(__file__).resolve().parents[2]


def src_dir() -> Path:
    """src 目录（源码运行时）或 _MEIPASS（frozen）；用于 PYTHONPATH 注入。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # frozen：_MEIPASS 直接是包含 maestro/ 子包 + configs/ + web/ 等的目录
        # （我们 spec 把整个 src/ + configs/ + web/ + wallpaper/ 都打进 _MEIPASS）
        return Path(sys._MEIPASS)
    return project_root() / "src"


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
