"""OpenCode worker：opencode run 非交互单发，独立目录执行。

关键点（踩坑记录）：
- 必须带 --format json：默认非交互模式会把 TUI 进度文本吐到 stderr，
  编排器把"有 stderr"误判为失败；json 模式下结果走 stdout、stderr 干净。
- 解析 stdout 的 json-lines：type=text 的 text 字段即回答；type=error 即失败。
- --auto 自动批准权限（独立 workdir 风险可控）；--dir 指定工作目录。
"""
from __future__ import annotations

import json
import os

from .base import SubprocessWorker, WorkerResult, register


@register
class OpenCodeWorker(SubprocessWorker):
    name = "opencode"
    # 必须指向真实 exe，不能指向 .cmd 包装脚本：
    # Python subprocess 在管道重定向下调用 .cmd 会丢失 stdout（opencode 的 JSON 结果走 stdout）。
    bin = os.environ.get("OPENCODE_BIN", r"E:\tools\opencode\node_modules\opencode-ai\bin\opencode.exe")

    def build_command(self, prompt: str, workdir: str) -> list[str]:
        model = os.environ.get("MAESTRO_MODEL", "deepseek-chat")
        return [
            self.bin, "run", prompt,
            "--dir", workdir,
            "--model", f"deepseek/{model}",
            "--auto",
            "--format", "json",
        ]

    def spawn(self, prompt: str, workdir: str, timeout: int,
              task_id: str | None = None, subtask_id: str | None = None) -> WorkerResult:
        res = super().spawn(prompt, workdir, timeout, task_id, subtask_id)
        if res.timed_out:
            return res

        texts: list[str] = []
        error: str | None = None
        for line in (res.output or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # 非 json 的零散输出，兜底保留
                texts.append(line)
                continue
            t = obj.get("type")
            if t == "text":
                # opencode --format json 的 text 在 part.text 嵌套里（顶层无 text 键）
                text = (obj.get("part") or {}).get("text")
                if not text:
                    text = obj.get("text")
                if text:
                    texts.append(text)
            elif t == "error":
                err = obj.get("error", {})
                error = (
                    err.get("data", {}).get("message")
                    or err.get("name")
                    or "opencode error"
                )

        out = "\n".join(texts).strip()
        if error:
            return WorkerResult(out, f"opencode error: {error}", res.timed_out, res.returncode)
        # 非 0 退出且没有可解析文本，也视为失败（保留原始 stderr）
        if res.returncode != 0 and not out:
            return WorkerResult(out, res.error or f"opencode 退出码 {res.returncode}", res.timed_out, res.returncode)
        return WorkerResult(out, res.error, res.timed_out, res.returncode)
