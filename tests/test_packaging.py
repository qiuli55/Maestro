"""打包层冒烟：runtime 路径解析 + prune 数量 + spec 文件完整性。

不需要真的打包 PyInstaller（产物体积大、慢），仅验证构建配置正确。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_project_root_dev_mode():
    """源码运行：从 src/maestro/runtime.py 回溯两级到 repo 根。"""
    from maestro import runtime

    root = runtime.project_root()
    assert root.name == "Maestro", f"unexpected project root: {root}"
    assert (root / "pyproject.toml").exists()
    assert (root / "web").exists()
    assert (root / "wallpaper").exists()


def test_runtime_project_root_frozen(monkeypatch):
    """PyInstaller frozen 模式：sys.executable 父目录作为根（MAESTRO_HOME 优先）。"""
    from maestro import runtime

    fake_exe = ROOT / "fake_dist" / "Maestro.exe"
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    try:
        monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
        monkeypatch.setattr(runtime.sys, "executable", str(fake_exe))
        # MAESTRO_HOME 优先
        monkeypatch.setenv("MAESTRO_HOME", str(ROOT))
        assert runtime.project_root() == ROOT
        # 未设时：frozen → sys.executable 父目录
        monkeypatch.delenv("MAESTRO_HOME")
        assert runtime.project_root() == fake_exe.parent.resolve()
    finally:
        fake_exe.parent.rmdir() if fake_exe.parent.exists() else None
        if (fake_exe.parent / fake_exe.name).exists():
            (fake_exe.parent / fake_exe.name).unlink()


def test_data_and_outputs_dirs():
    """data/ outputs/ 首次调用自动创建。"""
    from maestro import runtime

    # 真实路径（不会被破坏）
    assert runtime.data_dir().is_dir()
    assert runtime.outputs_dir().is_dir()


def test_user_data_dir_windows_aware():
    """user_data_dir 在所有平台返回路径对象。"""
    from maestro import runtime

    p = runtime.user_data_dir()
    assert isinstance(p, Path)
    assert "Maestro" in p.name or "maestro" in p.name.lower()


def test_prune_assets_lists_targets(monkeypatch):
    """prune_assets 默认 dry-run：iter_targets 至少包含 .bak / _test / PSD。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "prune_assets", str(ROOT / "packaging" / "prune_assets.py")
    )
    prune_assets = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prune_assets)

    targets = list(prune_assets.iter_targets())
    paths = [str(t) for t in targets]
    # 必含：5.6MB 的 PSD 源 + 多个 _test 调试脚本 + .bak
    assert any("user_char_ref.psd" in p for p in paths), "应包含 user_char_ref.psd"
    assert any(".bak" in p for p in paths), "应包含至少一个 .bak 备份"
    assert any("_test" in p for p in paths), "应包含 _test 调试脚本"
    # 预估至少 30 个目标文件
    assert len(targets) >= 10, f"裁剪目标太少: {len(targets)}"
    # 总体积预计 > 5 MB（PSD 一个就 5.7 MB）
    total = sum(t.stat().st_size for t in targets)
    assert total > 1024 * 1024, f"裁剪目标总大小仅 {total} bytes，预期 > 1MB"


def test_maestro_spec_exists_and_basic_shape():
    """maestro.spec 文件存在且包含必要字段。"""
    spec = ROOT / "packaging" / "maestro.spec"
    assert spec.exists()
    content = spec.read_text(encoding="utf-8")
    assert "Analysis(" in content
    assert "launcher.py" in content
    assert "COLLECT" in content
    assert "Maestro" in content
    assert "web" in content
    assert "wallpaper" in content
    assert "configs" in content


def test_build_script_exists():
    assert (ROOT / "packaging" / "build.py").exists()


def test_launcher_help_runs():
    """launcher --help 不依赖 PyWebView，能独立跑通。"""
    import subprocess

    r = subprocess.run(
        [sys.executable, "-m", "maestro.launcher", "--help"],
        cwd=str(ROOT / "src"),
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0
    assert "--no-gui" in r.stdout or "--no-gui" in r.stderr
