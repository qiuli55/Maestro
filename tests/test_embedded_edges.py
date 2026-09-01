"""Embedded worker 边界测试。

覆盖 write_file 路径校验 + image generate 失败路径：
- write_file 拒绝 ../ / / \\ : 字符
- write_file 不存在时的异常处理
- image generate 缺 OPENAI_API_KEY 的友好错误
- list_files 空目录返回空字符串
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def _ensure(path):
    """_resolve_output_dir 桩助手：返回目录路径并按需建。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


from maestro.workers import embedded


def _executor_for_test(workdir):
    """直接调 embedded._executor 获取内部工具函数。"""
    return embedded._executor(workdir)


def test_write_file_rejects_path_traversal(tmp_path):
    """write_file 必须拒绝 ../ / / \\ : 字符。"""
    workdir = tmp_path / "work"
    workdir.mkdir()
    fn = _executor_for_test(str(workdir))

    for bad in ["../etc/passwd", "..\\windows\\system32", "/etc/passwd",
                "C:\\Windows\\evil.txt", "subdir/file.txt"]:
        with patch.object(embedded, "_resolve_output_dir", lambda: _ensure(tmp_path / "outputs")):
            result = fn("write_file", {"filename": bad, "content": "x"})
        assert "[write_file] 错误" in result, f"应拒绝 {bad!r}, 实际: {result!r}"


def test_write_file_accepts_simple_filename(tmp_path):
    """write_file 接受纯文件名（无路径分隔符），写入 OUTPUT_DIR。"""
    workdir = tmp_path / "work"
    workdir.mkdir()
    fn = _executor_for_test(str(workdir))

    out_dir = tmp_path / "outputs"
    with patch.object(embedded, "_resolve_output_dir", lambda: _ensure(out_dir)):
        result = fn("write_file", {"filename": "hello.py", "content": "print('hi')"})

    assert "[write_file] 成功" in result
    assert (out_dir / "hello.py").exists()
    assert (out_dir / "hello.py").read_text(encoding="utf-8") == "print('hi')"


def test_write_file_missing_filename_returns_error(tmp_path):
    """write_file 缺 filename 参数：返回错误而非抛异常。"""
    fn = _executor_for_test(str(tmp_path))
    result = fn("write_file", {"content": "x"})
    assert "filename" in result.lower()
    assert "错误" in result


def test_image_generate_missing_api_key(tmp_path, monkeypatch):
    """image generate 缺 OPENAI_API_KEY：返回友好错误而非抛异常。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fn = _executor_for_test(str(tmp_path))

    result = fn("generate_image", {"prompt": "a cat"})
    assert "[generate_image] 错误" in result
    assert "OPENAI_API_KEY" in result


def test_image_generate_missing_prompt(tmp_path, monkeypatch):
    """image generate 缺 prompt：返回错误而非抛异常。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    fn = _executor_for_test(str(tmp_path))

    result = fn("generate_image", {})
    assert "需要 prompt" in result


def test_list_files_empty_directory(tmp_path):
    """list_files 输出目录为空时：返回友好提示。"""
    empty = tmp_path / "empty_out"
    empty.mkdir()
    fn = _executor_for_test(str(tmp_path))

    with patch.object(embedded, "_resolve_output_dir", lambda: _ensure(empty)):
        result = fn("list_files", {})

    assert "空" in result


def test_read_file_unknown_tool(tmp_path):
    """未知工具名：返回错误标记，不抛异常。"""
    fn = _executor_for_test(str(tmp_path))
    result = fn("nonexistent_tool", {"foo": "bar"})
    assert "未知工具" in result or "未知" in result