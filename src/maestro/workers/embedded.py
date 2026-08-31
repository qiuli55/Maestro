"""内嵌 worker：直接调 DeepSeek，支持读文件和写文件（生成代码/文档/图片）。

不启动子进程——编排器进程内完成。
- 轻代码/文档生成：LLM 产出 + 读/写文件
- 图像生成：调用 DALL-E API（需 OPENAI_API_KEY）
"""

import os
import re
import uuid as _uuid_for_image
from pathlib import Path

from .. import llm
from .base import WorkerResult, register
from .filetools import safe_read
from .runcmd import run as run_command

from .. import runtime as _runtime_mod

# 输出目录：MAESTRO_OUTPUTS 优先；未设走 runtime.outputs_dir()（PyInstaller frozen
# 兼容——MAESTRO_HOME 或 exe 同级自动建 outputs/）。
def _resolve_output_dir() -> Path:
    p = Path(os.environ.get("MAESTRO_OUTPUTS") or _runtime_mod.outputs_dir())
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ensure_output_dir() -> Path:
    """懒加载：首次写入时建目录。"""
    return _resolve_output_dir()


_SYSTEM = (
    "你是 Maestro 的内嵌执行 worker，处理各种生成任务。\n"
    "你可以用以下工具：\n"
    "1. read_file - 读取工作目录内的文本文件\n"
    "2. write_file - 写内容到文件（代码、文档、文本等）\n"
    "3. generate_image - 用 DALL-E 生成图片（需描述图片内容）\n"
    "4. list_files - 列出输出目录的文件\n"
    "5. run_command - 执行查看类命令（git status、dir、tasklist 等）直接执行；"
    "写/改/删/安装类命令（git commit、del、copy、pip install 等）会请求用户批准，"
    "需等待用户决定，批准后才执行；格式化/关机/注册表等破坏性命令被禁止\n"
    "注意：命令已在当前工作目录执行，直接写 git status 即可，不要用 git -C 或 cd 指定路径。\n"
    "根据用户需求选择合适的工具完成文件生成任务。\n"
    "生成文件后，返回文件的完整路径给用户。"
)

_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取工作目录内的一个文本文件，返回其内容",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对工作目录的文件路径"}},
            "required": ["path"],
        },
    },
}

_WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "写内容到文件（可以是代码、Markdown、文本等）。文件会保存到任务的 outputs 目录（可跨任务访问）",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "文件名（不含路径），如 demo.py、readme.md"},
                "content": {"type": "string", "description": "文件内容"},
            },
            "required": ["filename", "content"],
        },
    },
}

_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": "用 DALL-E 生成图片（需描述图片内容）",
        "parameters": {
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "图片描述（英文效果更好）"}},
            "required": ["prompt"],
        },
    },
}

_LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": "列出输出目录的文件",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

_RUNCMD_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "执行命令。查看类命令（git status/log/diff、dir、ls、type、tasklist、systeminfo 等）直接执行；写/改/删/安装类命令（git commit、del、copy、pip install 等）会请求用户批准，需等待用户决定（可能超时），批准后才执行；格式化/关机/注册表/重定向/管道等破坏性命令被禁止。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令（查看类直接执行；修改类需用户批准）"}
            },
            "required": ["command"],
        },
    },
}


def _executor(workdir: str, task_id: str | None = None, subtask_id: str | None = None):
    def run(name: str, args: dict) -> str:
        if name == "read_file":
            return safe_read(args.get("path", ""), workdir)

        elif name == "write_file":
            filename = args.get("filename", "")
            content = args.get("content", "")
            if not filename:
                return "[write_file] 错误：需要 filename 参数"
            # 安全检查：禁止写入敏感路径（绝对路径/盘符/路径穿越字符）
            if ".." in filename or "/" in filename or "\\" in filename or ":" in filename:
                return "[write_file] 错误：文件名不能包含路径字符"
            out_dir = _ensure_output_dir()
            out_path = out_dir / filename
            try:
                out_path.write_text(content, encoding="utf-8")
                return f"[write_file] 成功：{out_path}"
            except Exception as e:
                return f"[write_file] 失败：{e}"

        elif name == "generate_image":
            prompt = args.get("prompt", "")
            if not prompt:
                return "[generate_image] 错误：需要 prompt 参数"

            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                return "[generate_image] 错误：未设置 OPENAI_API_KEY 环境变量（需要 DALL-E 生成图片）"

            try:
                from openai import OpenAI  # noqa: F401 - 本地延迟导入

                client = OpenAI(api_key=api_key)
                response = client.images.generate(
                    model="dall-e-3",
                    prompt=prompt,
                    size="1024x1024",
                    quality="standard",
                    n=1,
                )
                image_url = response.data[0].url
                # 下载图片并保存（加 30s 超时，避免 DALL-E CDN 挂起时 worker 卡死）
                import urllib.request

                img_data = urllib.request.urlopen(image_url, timeout=30).read()
                # 文件名：UUID 兜底，避免不同 prompt 前 20 字相同导致覆盖
                safe_hint = re.sub(r"[^\w]", "_", prompt[:20])[:20]
                img_name = f"image_{safe_hint}_{_uuid_for_image.uuid4().hex[:6]}.png"
                img_path = _ensure_output_dir() / img_name
                img_path.write_bytes(img_data)
                return f"[generate_image] 成功：{img_path}\n图片 URL: {image_url}"
            except Exception as e:
                return f"[generate_image] 失败：{e}"

        elif name == "list_files":
            try:
                files = list(_resolve_output_dir().glob("*"))
                if not files:
                    return "[list_files] 输出目录为空"
                return "[list_files] " + "\n".join(f"- {f.name}" for f in files)
            except Exception as e:
                return f"[list_files] 失败：{e}"

        elif name == "run_command":
            cmd = args.get("command", "")
            if not cmd:
                return "[run_command] 错误：需要 command 参数"
            return run_command(cmd, workdir, task_id=task_id, subtask_id=subtask_id)

        return f"[未知工具] {name}"

    return run


@register
class EmbeddedWorker:
    name = "embedded"

    def spawn(
        self, prompt: str, workdir: str, timeout: int, task_id: str | None = None, subtask_id: str | None = None
    ) -> WorkerResult:
        try:
            out = llm.complete_with_tools(
                system=_SYSTEM,
                user=prompt,
                tools=[_READ_TOOL, _WRITE_TOOL, _IMAGE_TOOL, _LIST_TOOL, _RUNCMD_TOOL],
                tool_executor=_executor(workdir, task_id, subtask_id),
            )
            return WorkerResult(out, "", False, 0)
        except Exception as e:  # noqa: BLE001
            return WorkerResult("", str(e), False, 1)
