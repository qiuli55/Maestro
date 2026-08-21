"""结构化日志配置 + 健康检查支持。

用法：
    from maestro.observability import setup_logging, get_logger
    setup_logging()            # 在 server 启动入口调一次
    log = get_logger(__name__)
    log.info("task dispatched", extra={"task_id": tid, "subtask_count": n})

环境变量：
- LOG_LEVEL=DEBUG|INFO|WARNING|ERROR（默认 INFO）
- LOG_FORMAT=text|json（默认 text；json 用于 ELK/Loki）

设计要点：
- JSON 模式输出 {"ts": ISO8601, "level": "INFO", "logger": "...", "msg": "...", ...extras}
- text 模式维持人类可读（开发/CI 默认）
- 不传 `propagate=False` 让子 logger 也输出（FastAPI/Uvicorn 自动接）
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

_CONFIGURED = False


class JSONFormatter(logging.Formatter):
    """结构化 JSON 输出（便于 ELK / Loki 收集）。"""

    # 标准 LogRecord 属性（保留 + 排除冲突）
    _STD_ATTRS = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime",
        # Python 3.13+ 新增的属性
        "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # extra 字段（用户自定义）直接拍平到顶层
        for key, value in record.__dict__.items():
            if key not in self._STD_ATTRS and not key.startswith("_"):
                # 序列化为 JSON 友好类型
                try:
                    json.dumps(value)
                    out[key] = value
                except (TypeError, ValueError):
                    out[key] = repr(value)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


class ColoredTextFormatter(logging.Formatter):
    """人类可读 + 颜色（开发/CI 用）。"""

    COLORS = {
        "DEBUG": "\x1b[36m",    # cyan
        "INFO": "\x1b[32m",     # green
        "WARNING": "\x1b[33m",  # yellow
        "ERROR": "\x1b[31m",    # red
        "CRITICAL": "\x1b[35m", # magenta
    }
    RESET = "\x1b[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        msg = record.getMessage()
        # extra 字段作为 key=value 追加
        extras = ""
        std_attrs = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message", "asctime",
            "taskName",  # Python 3.13+
        }
        for key, value in record.__dict__.items():
            if key not in std_attrs and not key.startswith("_"):
                extras += f" {key}={value!r}"
        line = f"{color}{record.levelname:<7}{self.RESET} {ts} {record.name}: {msg}{extras}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(level: str | None = None, fmt: str | None = None) -> None:
    """全局 logging 配置。重复调用幂等。

    Args:
        level: DEBUG/INFO/WARNING/ERROR（默认从 LOG_LEVEL env 或 INFO）
        fmt: text/json（默认从 LOG_FORMAT env 或 text）
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    fmt = (fmt or os.environ.get("LOG_FORMAT", "text")).lower()

    if fmt not in ("text", "json"):
        fmt = "text"

    formatter = JSONFormatter() if fmt == "json" else ColoredTextFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # 避免重复 handler（reload 场景）
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    # 抑制最吵的库
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """取模块 logger（统一入口，便于将来替换实现）。"""
    return logging.getLogger(name)
