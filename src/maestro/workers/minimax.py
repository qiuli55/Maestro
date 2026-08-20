"""MiniMax CLI worker：调用 mmx 生成文本/图片/视频/语音/音乐。

使用 MiniMax API Key，需要在环境变量或配置中设置。
"""
import os
import subprocess
import json
import tempfile
import shutil
from pathlib import Path
from ..workers.base import WorkerResult, register

# MiniMax CLI 路径（需要用 node 运行）
MMX_BIN = "F:/npm_global/bin/mmx"
# 使用 node 运行
MMX_CMD = ["node", "F:/npm_global/node_modules/mmx-cli/dist/mmx.mjs"]

# API Key 必须从环境变量提供；缺失直接报错，绝不写死。
# 历史背景：之前 DEFAULT_API_KEY 落地源码后已通过 git filter-branch / BFG 清理；
# 此处不再保留任何 fallback 字符串。
MMX_API_KEY = os.environ.get("MMX_API_KEY") or ""
MMX_REGION = os.environ.get("MMX_REGION", "cn")

if not MMX_API_KEY:
    import warnings
    warnings.warn(
        "MMX_API_KEY 未设置；minimax worker 调用会失败。"
        "请在 .env 或系统环境变量中设置 MMX_API_KEY 后重启服务。",
        stacklevel=2,
    )


def _run_mmx(args: list) -> tuple[str, str, int]:
    """运行 mmx 命令，返回 (stdout, stderr, returncode)"""
    cmd = MMX_CMD + ["--region", MMX_REGION] + args
    # mmx CLI 已通过 `mmx auth login` 持久化 key（~/.mmx/config.json），
    # 不必每次显式传 --api-key。如果强制传了反而会在某些 mmx 版本里
    # 覆盖已持久化的 key 并要求重新 login。
    # 兜底：如果未来 mmx 改了协议 / 没持久化，再传 MMX_API_KEY。
    if MMX_API_KEY:
        cmd = cmd + ["--api-key", MMX_API_KEY]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except Exception as e:
        return "", str(e), -1


@register
class MiniMaxWorker:
    name = "minimax"

    def spawn(self, prompt: str, workdir: str, timeout: int,
              task_id: str | None = None, subtask_id: str | None = None) -> WorkerResult:
        # task_id/subtask_id：审批归属用。minimax 无审批型工具，忽略。
        if not MMX_API_KEY:
            return WorkerResult("", "未设置 MMX_API_KEY 环境变量", False, 1)

        # 解析 prompt 判断任务类型
        prompt_lower = prompt.lower()
        
        if "图片" in prompt or "image" in prompt_lower or "生成图" in prompt:
            return self._generate_image(prompt)
        elif "视频" in prompt or "video" in prompt_lower or "生成视频" in prompt:
            return self._generate_video(prompt)
        elif "语音" in prompt or "speech" in prompt_lower or "配音" in prompt:
            return self._generate_speech(prompt)
        elif "音乐" in prompt or "music" in prompt_lower or "生成音乐" in prompt:
            return self._generate_music(prompt)
        else:
            return self._text_chat(prompt)

    def _text_chat(self, prompt: str) -> WorkerResult:
        stdout, stderr, rc = _run_mmx(["text", "chat", "--message", prompt])
        if rc != 0:
            return WorkerResult("", stderr or "调用失败", False, rc)
        return WorkerResult(stdout, "", False, 0)

    def _generate_image(self, prompt: str) -> WorkerResult:
        # 提取图片描述
        desc = prompt.replace("生成图片", "").replace("生成图", "").replace("image", "").replace("图片", "").strip()
        if not desc:
            desc = prompt
        
        stdout, stderr, rc = _run_mmx([
            "image", "generate",
            "--prompt", desc,
            "--aspect-ratio", "16:9"
        ])
        
        if rc != 0:
            return WorkerResult("", stderr or "图片生成失败", False, rc)
        
        # mmx 会把图片保存到当前目录的 minimax-output 文件夹
        return WorkerResult(f"图片生成完成：{stdout}", "", False, 0)

    def _generate_video(self, prompt: str) -> WorkerResult:
        desc = prompt.replace("生成视频", "").replace("video", "").strip()
        stdout, stderr, rc = _run_mmx([
            "video", "generate",
            "--prompt", desc
        ])
        
        if rc != 0:
            return WorkerResult("", stderr or "视频生成失败", False, rc)
        
        return WorkerResult(f"视频生成任务已提交：{stdout}", "", False, 0)

    def _generate_speech(self, prompt: str) -> WorkerResult:
        # 提取要朗读的文本
        text = prompt.replace("生成语音", "").replace("配音", "").replace("speech", "").strip()
        if not text:
            text = prompt
        
        stdout, stderr, rc = _run_mmx([
            "speech", "synthesize",
            "--text", text
        ])
        
        if rc != 0:
            return WorkerResult("", stderr or "语音合成失败", False, rc)
        
        return WorkerResult(f"语音生成完成：{stdout}", "", False, 0)

    def _generate_music(self, prompt: str) -> WorkerResult:
        desc = prompt.replace("生成音乐", "").replace("music", "").strip()
        stdout, stderr, rc = _run_mmx([
            "music", "generate",
            "--prompt", desc
        ])
        
        if rc != 0:
            return WorkerResult("", stderr or "音乐生成失败", False, rc)
        
        return WorkerResult(f"音乐生成完成：{stdout}", "", False, 0)
