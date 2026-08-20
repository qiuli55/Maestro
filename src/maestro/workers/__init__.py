"""Worker 适配器包。导入即注册全部 worker（@register 触发）。"""
from . import opencode, octo, embedded, codebuddy, workbuddy, minimax  # noqa: F401 触发注册
from .base import get_worker, available_workers, SubprocessWorker, WorkerResult

__all__ = ["get_worker", "available_workers", "SubprocessWorker", "WorkerResult"]
