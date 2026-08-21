"""Maestro CLI 入口（P1：纯命令行，无 Web UI）。

maestro run   --scenario {a|b|c} [--input <file>] [--worker <opencode|octo|embedded>] [--serial]
              a=需求逐个喂（串行） b=多段文本并行汇总 c=AI 智能拆分
maestro status <task_id>
maestro retry  <subtask_id>
maestro reset  <task_id>
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import db, orchestrator


def _read_input(args) -> str:
    if args.input:
        return Path(args.input).read_text(encoding="utf-8")
    return sys.stdin.read()


def _print_status(conn, task_id: str):
    task = db.get_task(conn, task_id)
    if not task:
        print(f"任务不存在: {task_id}")
        return
    print(f"# 任务 {task_id}  状态={task['status']}")
    for st in db.get_subtasks(conn, task_id):
        mark = "✓" if st["status"] == db.DONE else ("✗" if st["status"] == db.FAILED else "…")
        print(f"  {mark} {st['id']} [{st['worker_type']}] {st['status']}: {st['desc'][:60]}")
    if task["result"]:
        print("\n--- 汇总 ---")
        print(task["result"])
    # 审计段（冲突裁决 / 去重 / 覆盖遗漏）在 summary.md 里，CLI 一并输出
    if task.get("result_path") and Path(task["result_path"]).exists():
        md = Path(task["result_path"]).read_text(encoding="utf-8")
        marker = "## 审计"
        if marker in md:
            print(md[md.index(marker) :])


def main(argv: list[str] | None = None):
    load_dotenv()  # 载入 DEEPSEEK_API_KEY 等
    parser = argparse.ArgumentParser(prog="maestro", description="壁纸多 Agent 编排器 (P1)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="执行一个任务")
    run_p.add_argument("--scenario", choices=["a", "b", "c", "auto"], required=True)
    run_p.add_argument("--input", help="输入文本文件；省略读 stdin")
    run_p.add_argument("--worker", choices=["opencode", "octo", "embedded"], default="embedded")
    run_p.add_argument("--serial", action="store_true", help="串行派发（场景 B 默认并行）")
    run_p.add_argument("--timeout", type=int, default=int(__import__("os").environ.get("WORKER_TIMEOUT", "600")))

    status_p = sub.add_parser("status", help="查看任务状态")
    status_p.add_argument("task_id")

    retry_p = sub.add_parser("retry", help="重派失败子任务")
    retry_p.add_argument("subtask_id")
    retry_p.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("WORKER_TIMEOUT", "600")),
        help="超时（秒，默认从 WORKER_TIMEOUT 环境变量读）",
    )

    reset_p = sub.add_parser("reset", help="清空任务（开发期）")
    reset_p.add_argument("task_id")

    serve_p = sub.add_parser("serve", help="启动 Web 服务（P2 任务窗口）")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8787)

    args = parser.parse_args(argv)
    conn = db.init_db()

    if args.cmd == "serve":
        import uvicorn

        print(f"Maestro Web 启动于 http://{args.host}:{args.port}")
        uvicorn.run("maestro.server:app", host=args.host, port=args.port, reload=False)
        return 0

    if args.cmd == "run":
        prompt = _read_input(args)
        if not prompt.strip():
            print("错误：输入为空")
            return 1
        task_id = orchestrator.run_task(
            conn,
            prompt,
            scenario=args.scenario,
            worker_type=args.worker,
            timeout=args.timeout,
            parallel=not args.serial,
        )
        print(f"任务已提交：{task_id}")
        _print_status(conn, task_id)
        return 0

    if args.cmd == "status":
        _print_status(conn, args.task_id)
        return 0

    if args.cmd == "retry":
        st = orchestrator.retry_subtask(conn, args.subtask_id, timeout=args.timeout)
        print(f"重派完成：{st['id']} -> {st['status']}")
        return 0

    if args.cmd == "reset":
        task = db.get_task(conn, args.task_id)
        if task:
            for st in db.get_subtasks(conn, args.task_id):
                conn.execute("DELETE FROM subtasks WHERE id=?", (st["id"],))
            conn.execute("DELETE FROM tasks WHERE id=?", (args.task_id,))
            conn.execute("DELETE FROM task_events WHERE task_id=?", (args.task_id,))
            conn.commit()
        shutil.rmtree(orchestrator.OUTPUTS_ROOT / args.task_id, ignore_errors=True)
        print(f"已重置：{args.task_id}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
