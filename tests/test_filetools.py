"""内嵌 worker 文件读取工具测试：路径逃逸 / 敏感文件 / 大小上限。"""
import sys

sys.path.insert(0, "src")

from pathlib import Path

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
