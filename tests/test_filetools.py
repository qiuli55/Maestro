"""内嵌 worker 文件读取工具测试：路径逃逸 / 敏感文件 / Windows 设备名 / 大小上限。"""
import sys

sys.path.insert(0, "src")

from pathlib import Path

import pytest

from maestro.workers.filetools import safe_read, _is_sensitive, MAX_READ_CHARS


def _mk(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_read_normal_file(tmp_path):
    _mk(tmp_path, "a.txt", "hello 内容")
    assert safe_read("a.txt", tmp_path) == "hello 内容"


def test_path_traversal_blocked(tmp_path):
    """../../ 逃逸到 root 之外必须被拒绝。"""
    secret = tmp_path.parent / "outside.txt"
    secret.write_text("绝密", encoding="utf-8")
    out = safe_read("../outside.txt", tmp_path)
    assert "拒绝" in out and "绝密" not in out


def test_dotenv_blocked(tmp_path):
    _mk(tmp_path, ".env", "API_KEY=abc123")
    assert "拒绝" in safe_read(".env", tmp_path)


def test_git_dir_blocked(tmp_path):
    _mk(tmp_path, ".git/config", "secret config")
    assert "拒绝" in safe_read(".git/config", tmp_path)


def test_oversize_truncated(tmp_path):
    _mk(tmp_path, "big.txt", "x" * (MAX_READ_CHARS + 5000))
    out = safe_read("big.txt", tmp_path)
    assert len(out) <= MAX_READ_CHARS + 30
    assert "截断" in out


def test_missing_file_reports(tmp_path):
    assert "不存在" in safe_read("nope.txt", tmp_path)


def test_is_sensitive_names():
    assert _is_sensitive(Path("x/.env"))
    assert _is_sensitive(Path("x/id_rsa"))
    assert _is_sensitive(Path("x/node_modules/foo.js"))
    assert not _is_sensitive(Path("x/normal.txt"))


# ============================================================================
# Windows 设备文件名黑名单（v2 改进）
# ============================================================================

@pytest.mark.parametrize("reserved_name", [
    "CON", "PRN", "AUX", "NUL",                 # 设备
    "COM1", "COM9",                              # 串口
    "LPT1", "LPT9",                              # 并口
    "con", "nul",                                # 大小写不敏感
])
def test_windows_reserved_names_blocked(reserved_name):
    """Windows 设备文件名（CON/NUL/COM1 等）必须被拒绝。

    这些名字在 Windows 下不是普通文件——访问会触发设备 I/O。
    即使跨平台，LLM 也不能指定它们。
    """
    assert _is_sensitive(Path(f"x/{reserved_name}"))


def test_windows_reserved_in_path_blocked():
    """路径中任意一段命中设备名也要拒。"""
    assert _is_sensitive(Path("x/CON/foo.txt"))
    assert _is_sensitive(Path("y/COM1/bar.py"))


def test_safe_read_rejects_reserved_name(tmp_path):
    """_is_sensitive 应能识别 Windows 设备名（CON/NUL/COM1 等）。"""
    # 直接测试 _is_sensitive（避免 tmp_path 不让创建 CON/NUL 文件）
    assert _is_sensitive(Path(str(tmp_path / "CON")))
    assert _is_sensitive(Path(str(tmp_path / "NUL")))
    assert _is_sensitive(Path(str(tmp_path / "sub/COM1/x")))  # 路径段命中
    # CONNECT / CON.txt 不是设备名（CON 是 3 字符，CONNECT 是 7 字符）
    assert not _is_sensitive(Path(str(tmp_path / "CONNECT")))
    assert not _is_sensitive(Path(str(tmp_path / "CON.txt")))
