"""Worker 适配器包。导入即注册全部 worker（@register 触发）。"""

from . import codebuddy, embedded, minimax, octo, opencode, workbuddy  # noqa: F401 触发注册
from .base import SubprocessWorker, WorkerResult, available_workers, get_worker

__all__ = ["get_worker", "available_workers", "SubprocessWorker", "WorkerResult"]
