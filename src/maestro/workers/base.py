"""Worker 基类：subprocess 驱动通用逻辑 + 超时回收。

所有 worker 统一接口：spawn(prompt, workdir, timeout) -> WorkerResult
- 独立工作目录 workdir，避免并发写冲突
- 超时返回 timed_out=True，由编排器标记失败并回收进程
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass
class WorkerResult:
    output: str
    error: str
    timed_out: bool = False
    returncode: int = 0


class SubprocessWorker:
    """通过子进程 CLI 驱动外部 agent。子类只需实现 build_command。"""

    name: str = "subprocess"
    bin: str = ""
    # 子进程 stdout/stderr 编码。Windows 下 CLI 经管道输出多为 UTF-8，
    # 但个别（如旧版 codebuddy 控制台）会是 GBK。spawn 用 _decode 依次尝试
    # utf-8 → gbk → 子类 encoding → 强制 replace，确保中文产出不崩溃、尽量不乱码。
    # 子类可覆盖 encoding 作为兜底候选（默认 utf-8）。
    encoding: str = "utf-8"

    def build_command(self, prompt: str, workdir: str) -> list[str]:
        raise NotImplementedError

    def extra_env(self) -> dict:
        return {}

    def _decode(self, data: bytes) -> str:
        """把子进程原始字节按 utf-8 → gbk → 子类 encoding → replace 顺序解码。"""
        if not data:
            return ""
        for enc in ("utf-8", "gbk", self.encoding):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    def spawn(self, prompt: str, workdir: str, timeout: int,
              task_id: str | None = None, subtask_id: str | None = None) -> WorkerResult:
        """启动子进程。task_id/subtask_id 供需要审批的 worker（embedded）归属审批请求。"""
        cmd = self.build_command(prompt, workdir)
        env = {**os.environ, **self.extra_env()}
        try:
            proc = subprocess.run(
                cmd, cwd=workdir, env=env,
                capture_output=True, timeout=timeout,
            )
            return WorkerResult(self._decode(proc.stdout), self._decode(proc.stderr), False, proc.returncode)
        except subprocess.TimeoutExpired as e:
            return WorkerResult(
                self._decode(e.stdout or b""), self._decode(e.stderr or b""), True, -1
            )
        except OSError as e:
            # 启动失败（bin 不存在/不可执行）：标记失败而非拖垮整个任务
            return WorkerResult("", f"worker 启动失败: {e}", False, -1)


# worker 注册表（worker_type -> 类）。编排器按 subtask.worker_type 取。
_REGISTRY: dict[str, type] = {}


def register(cls):
    _REGISTRY[cls.name] = cls
    return cls


def get_worker(name: str) -> SubprocessWorker | object:
    if name not in _REGISTRY:
        raise KeyError(f"未注册的 worker: {name}（可选: {list(_REGISTRY)}）")
    return _REGISTRY[name]()


def available_workers() -> list[str]:
    return list(_REGISTRY)


# 导入即把 configs/workers.json 的 bin 路径灌入环境变量（不覆盖已设 env）
from .. import config as _cfg  # noqa: E402

_cfg.apply_worker_bins()
