"""可观测性指标采集：任务/子任务/事件/审批/WS/连接池/进程。

GET /metrics 输出 Prometheus 文本格式（?format=json 可切 JSON）。
"""
from __future__ import annotations

import os
import time

from . import db
from .api import deps

_started_at = time.monotonic()


def collect_metrics() -> dict:
    """采集一次指标快照（只读，不落库）。"""
    conn = db.init_db()
    try:
        task_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
        task_total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        subtask_total = conn.execute("SELECT COUNT(*) FROM subtasks").fetchone()[0]
        event_total = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        approval_pending = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending'"
        ).fetchone()[0]
    finally:
        conn.close()

    with deps._ws_lock:
        ws_conns = len(deps._active_ws_connections)

    return {
        "version": "0.2",
        "uptime_seconds": int(time.monotonic() - _started_at),
        "tasks": {r["status"]: int(r["n"]) for r in task_rows},
        "tasks_total": int(task_total),
        "subtasks_total": int(subtask_total),
        "events_total": int(event_total),
        "approvals_pending": int(approval_pending),
        "ws_connections": ws_conns,
        "pool": db._pool_stats_snapshot(),
    }


def format_prometheus(m: dict) -> str:
    """转 Prometheus 文本格式（text/plain; version=0.0.4）。"""
    lines = [
        f"# HELP maestro_tasks_total 任务总数",
        f"# TYPE maestro_tasks_total gauge",
        f"maestro_tasks_total {m['tasks_total']}",
    ]
    for status, n in sorted(m["tasks"].items()):
        lines.append(f'maestro_tasks{{status="{status}"}} {n}')
    lines += [
        f"# TYPE maestro_subtasks_total gauge",
        f"maestro_subtasks_total {m['subtasks_total']}",
        f"# TYPE maestro_events_total gauge",
        f"maestro_events_total {m['events_total']}",
        f"# TYPE maestro_approvals_pending gauge",
        f"maestro_approvals_pending {m['approvals_pending']}",
        f"# TYPE maestro_ws_connections gauge",
        f"maestro_ws_connections {m['ws_connections']}",
        f"# TYPE maestro_pool_open gauge",
        f"maestro_pool_open {m['pool'].get('open', 0)}",
        f"# TYPE maestro_pool_reuse gauge",
        f"maestro_pool_reuse {m['pool'].get('reuse', 0)}",
        f"# TYPE maestro_uptime_seconds gauge",
        f"maestro_uptime_seconds {m['uptime_seconds']}",
    ]
    return "\n".join(lines) + "\n"
