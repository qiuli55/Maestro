"""内嵌 worker 的受控文件读取：安全边界 + 大小上限。

技术方案 §133：内嵌 worker 只做「LLM 产出 + 简单文件操作（读文件）」，
不执行命令、不改文件、不跑测试。
"""
from __future__ import annotations

import os
from pathlib import Path

# 单文件读取上限（字符），防读入超大文件撑爆上下文
MAX_READ_CHARS = 20000

# 拒绝读取的敏感路径/文件（防泄露密钥、配置）
_DENY_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
               "credentials", "secrets", "secret"}
_DENY_PARTS = {".git", "__pycache__", "node_modules"}


def _is_sensitive(path: Path) -> bool:
    """命中敏感文件名 / 敏感目录段则拒绝。"""
    if path.name in _DENY_NAMES:
        return True
    # 任意一层路径段命中敏感目录（含大小写不敏感，Windows）
    for part in path.parts:
        if part.lower() in _DENY_PARTS:
            return True
    return False


def safe_read(rel_path: str, root: str | Path) -> str:
    """安全读取 root 下的相对文件。

    - realpath 校验：解析符号链接/.. 后必须仍在 root 内（防路径逃逸）
    - 拒绝敏感文件（.env/密钥/.git）
    - 大小上限截断
    返回文件内容；违规/不存在/非文件则返回带标记的错误文本（不抛异常，
    由 LLM 决定如何处理），绝不泄露绝对路径细节。
    """
    root = Path(root).resolve()
    candidate = (root / rel_path).resolve()

    if not str(candidate).startswith(str(root) + os.sep) and candidate != root:
        return "[read_file 拒绝] 路径越界，只允许读取工作目录内的文件。"

    if not candidate.exists():
        return "[read_file 拒绝] 文件不存在。"

    if not candidate.is_file():
        return "[read_file 拒绝] 目标不是文件。"

    if _is_sensitive(candidate):
        return "[read_file 拒绝] 该文件是敏感文件（如密钥/配置），禁止读取。"

    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"[read_file 拒绝] 读取失败: {type(e).__name__}"

    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + "\n…[已截断，文件过长]"
    return text
