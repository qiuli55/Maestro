"""octo worker：headless 单发，DeepSeek 经 OPENAI_* 环境变量接入。"""

import os

from .base import SubprocessWorker, register


@register
class OctoWorker(SubprocessWorker):
    name = "octo"
    bin = os.environ.get("OCTO_BIN", r"E:\tools\octo\octo.exe")

    def build_command(self, prompt: str, workdir: str) -> list[str]:
        model = os.environ.get("MAESTRO_MODEL", "deepseek-chat")
        # --no-tools 纯文本产出（内嵌式执行，不做文件改动）
        # --no-save 不残留 session；--quiet 少状态噪音
        return [
            self.bin,
            "--provider",
            "openai",
            "--model",
            model,
            "--no-tools",
            "--no-save",
            "--quiet",
            prompt,
        ]

    def extra_env(self) -> dict:
        # octo 用 OPENAI_* 指向 DeepSeek 兼容端点
        return {
            "OPENAI_BASE_URL": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            "OPENAI_API_KEY": os.environ.get("DEEPSEEK_API_KEY", ""),
        }
