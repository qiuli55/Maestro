"""编排器：Orchestrator-Workers 生命周期状态机。

parse -> split -> dispatch(串行/并行) -> collect -> merge -> deliver
可靠性：单 worker 超时上限、失败重试 1 次、失败隔离、重启恢复（读 SQLite 续跑）。
"""

from __future__ import annotations

import concurrent.futures
import os
import uuid
from pathlib import Path

from . import db, guard, merge, observability, split
from .workers import base as wbase
from .workers.base import get_worker

OUTPUTS_ROOT = Path(os.environ.get("MAESTRO_OUTPUTS", Path(__file__).resolve().parents[2] / "outputs"))


def _workdir(task_id: str, subtask_id: str) -> Path:
    d = OUTPUTS_ROOT / task_id / subtask_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_path(conn) -> str:
    """从连接取数据库文件路径（供子线程开独立连接用）。"""
    return conn.execute("PRAGMA database_list").fetchone()[2]


def _execute_subtask(db_path: str, task_id: str, subtask: dict, timeout: int, model: str | None):
    """执行单个子任务：运行 -> 落盘 -> 状态。失败重试 1 次。

    每个调用开独立 SQLite 连接——SQLite 连接非线程安全，并行派发时子线程不能复用主连接。
    db_path 必须在主线程预先算出后传入（连接本身不可跨线程）。
    """
    sid = subtask["id"]
    workdir = _workdir(task_id, sid)
    tconn = db.init_db(db_path)
    try:
        db.set_subtask_status(tconn, sid, db.RUNNING)
        db.log_event(
            tconn,
            task_id,
            f"subtask {sid} running",
            sid,
            data={"worker_type": subtask["worker_type"], "desc": subtask["desc"]},
        )

        worker = get_worker(subtask["worker_type"])

        # 沙箱防线 1：外部 CLI（黑盒 agent）派发前对 prompt 做危险指令前置检测。
        # embedded 有工具白名单+审批流兜底，不重复扫描（避免误伤正常代码需求）。
        if isinstance(worker, wbase.SubprocessWorker):
            level, reason = guard.scan(subtask["desc"], subtask["worker_type"])
            if level == "block":
                db.set_subtask_output(tconn, sid, db.FAILED, error=f"[沙箱拦截] {reason}")
                db.log_event(tconn, task_id, f"subtask {sid} BLOCKED by guard", sid, data={"reason": reason})
                return
            if level == "warn":
                db.log_event(tconn, task_id, f"subtask {sid} guard warning", sid, data={"reason": reason})

        # 沙箱防线 2：worker 内部（embedded 的 run_command）走审批流，任务上下文用于审批归属
        res = worker.spawn(subtask["desc"], str(workdir), timeout, task_id=task_id, subtask_id=sid)

        if res.timed_out or res.returncode != 0 or res.error:
            # 重试 1 次
            db.log_event(tconn, task_id, f"subtask {sid} failed, retrying", sid, data={"error": _err_msg(res)})
            res = worker.spawn(subtask["desc"], str(workdir), timeout)

        if res.timed_out or res.returncode != 0 or res.error:
            db.set_subtask_output(tconn, sid, db.FAILED, error=_err_msg(res))
            db.log_event(tconn, task_id, f"subtask {sid} FAILED", sid, data={"error": _err_msg(res)})
            return

        # 成功：产出落盘 + 登记 result_path
        result_file = workdir / "result.md"
        result_file.write_text(res.output, encoding="utf-8")
        db.set_subtask_output(tconn, sid, db.DONE, output=res.output, result_path=str(result_file))
        db.log_event(
            tconn,
            task_id,
            f"subtask {sid} done",
            sid,
            data={"result_path": str(result_file), "bytes": len(res.output or "")},
        )
    except Exception as e:  # noqa: BLE001 — 隔离一切异常：不拖垮整任务（并行时尤为关键）
        err = f"执行异常: {type(e).__name__}: {str(e)[:300]}"
        try:
            db.set_subtask_output(tconn, sid, db.FAILED, error=err)
            db.log_event(tconn, task_id, f"subtask {sid} FAILED (异常)", sid, data={"error": err})
        except Exception:  # noqa: BLE001 — DB 也已异常时不再传播
            pass
    finally:
        tconn.close()


def _err_msg(res) -> str:
    if res.timed_out:
        return f"超时（{res.returncode}）"
    return (res.error or "非零退出").strip()[:500]


def _prepare_subtasks(
    conn, task_id: str, task_prompt: str, scenario: str, worker_type: str, model: str | None
) -> list[dict]:
    """拆分并入库子任务（公共步骤），返回带全局 id 的子任务列表。"""
    if scenario == "a":
        subtasks = split.split_requirements(task_prompt, worker_type)
    elif scenario == "c":
        subtasks = split.split_plan(task_prompt, model=model)
        for st in subtasks:
            st["worker_type"] = worker_type  # 子任务统一用用户选择的 worker
    else:
        subtasks = split.split_document(task_prompt)
    subtasks = split.validate(subtasks, wbase.available_workers())
    # subtask id 全局唯一化：加 task_id 前缀（subtasks 表主键，避免跨任务碰撞）
    for st in subtasks:
        st["id"] = f"{task_id}_{st['id']}"
    db.add_subtasks(conn, task_id, subtasks)
    return subtasks


def prepare_task(
    conn,
    task_prompt: str,
    scenario: str = "a",
    worker_type: str = "embedded",
    model: str | None = None,
    parallel: bool = True,
    task_id: str | None = None,
    no_merge: bool = False,
    conv_id: str | None = None,
) -> str:
    """人工闸门第一段：创建任务 + 拆分 + 入库，停在 READY（待用户确认/编辑）。

    不执行任何子任务；调用方确认后调 execute_task 才真正派发。
    """
    task_id = task_id or f"task_{uuid.uuid4().hex[:8]}"
    db.create_task(conn, task_id, task_prompt, conv_id=conv_id)
    db.set_task_params(conn, task_id, scenario=scenario, worker_type=worker_type, parallel=parallel, no_merge=no_merge)
    subtasks = _prepare_subtasks(conn, task_id, task_prompt, scenario, worker_type, model)
    db.set_task_status(conn, task_id, db.READY)
    db.log_event(
        conn,
        task_id,
        f"split -> {len(subtasks)} subtasks (ready, awaiting confirm)",
        data=[{"id": st["id"], "desc": st["desc"], "worker_type": st["worker_type"]} for st in subtasks],
    )
    observability.get_logger(__name__).info(
        "task prepared (HITL gate)",
        extra={
            "task_id": task_id,
            "scenario": scenario,
            "n_subtasks": len(subtasks),
            "worker_type": worker_type,
            "parallel": parallel,
        },
    )
    return task_id


def _is_cancelled(conn, task_id: str) -> bool:
    """轮询取消标志：执行中用户可取消（POST /api/tasks/{id}/cancel）。"""
    t = db.get_task(conn, task_id)
    return bool(t and t["status"] == db.CANCELLED)


def _push_result_to_conv(conn, task_id: str) -> None:
    """任务完成后把结果写入关联会话（任务创建时带了 conv_id 才推）。

    有汇总结果（merge/no_merge）推报告全文；否则（场景 A）拼各子任务产出。
    """
    task = db.get_task(conn, task_id)
    conv_id = task and task.get("conv_id")
    if not conv_id:
        return
    text = (task.get("result") or "").strip()
    if not text:
        parts = []
        for st in db.get_subtasks(conn, task_id):
            content = (st.get("output") or "").strip()
            if content:
                parts.append(f"【{st.get('desc', st.get('id', ''))}】\n{content}")
        text = "\n\n".join(parts) or "任务已完成（无产出）。"
    db.add_chat_message(conn, "assistant", text, conv_id=conv_id)
    db.touch_conversation(conn, conv_id)


def execute_task(
    conn,
    task_id: str,
    timeout: int = 600,
    model: str | None = None,
) -> str:
    """人工闸门第二段：按库内（可能已编辑）子任务执行完整链路。

    仅接受 READY 状态任务；执行开始后置 RUNNING，结束置 DONE。
    执行中可取消（db.cancel_task 置 CANCELLED）：串行在派发下一个前检查，
    并行在全部收尾后检查——取消则不汇总、保持 CANCELLED。
    """
    task = db.get_task(conn, task_id)
    if not task:
        raise KeyError(f"任务不存在: {task_id}")
    if task["status"] != db.READY:
        raise ValueError(f"任务状态 {task['status']} 不可执行（需 ready）")

    subtasks = db.get_subtasks(conn, task_id)
    if not subtasks:
        raise ValueError("子任务列表为空，无法执行")

    db.set_task_status(conn, task_id, db.RUNNING)
    db.log_event(
        conn, task_id, f"task confirmed, dispatch {len(subtasks)} subtasks", data=[st["id"] for st in subtasks]
    )
    observability.get_logger(__name__).info(
        "task dispatching",
        extra={
            "task_id": task_id,
            "scenario": task["scenario"],
            "n_subtasks": len(subtasks),
            "parallel": task["parallel"],
            "no_merge": task["no_merge"],
        },
    )

    parallel = bool(task["parallel"])
    no_merge = bool(task["no_merge"])
    db_path = _db_path(conn)  # 主线程算路径，跨线程只传字符串

    # 场景 A：串行（逐个投喂，互不干扰）；B/C：并行 -> 汇总
    if task["scenario"] == "a":
        for st in subtasks:
            if _is_cancelled(conn, task_id):
                db.log_event(
                    conn,
                    task_id,
                    "task cancelled mid-serial, stop dispatch",
                    data={"dispatched": st["id"], "rest": len(subtasks) - subtasks.index(st)},
                )
                return task_id
            _execute_subtask(db_path, task_id, st, timeout, model)
        db.set_task_status(conn, task_id, db.DONE)
        db.log_event(conn, task_id, "scenario A serial complete", data={"subtasks": [st["id"] for st in subtasks]})
        _push_result_to_conv(conn, task_id)
        return task_id

    if parallel:
        # 上限 8：LLM 拆分可能产生很多子任务，无界线程会瞬时打满 CPU / 文件描述符。
        max_workers = min(len(subtasks), 8) if subtasks else 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(_execute_subtask, db_path, task_id, st, timeout, model) for st in subtasks]
            # 重新抛出子线程内的异常，避免静默失败；
            # as_completed 期间检查取消标志，及时放弃未派发的子任务。
            for f in concurrent.futures.as_completed(futures):
                f.result()
                if _is_cancelled(conn, task_id):
                    # 取消已派发的子任务最终结果会被状态检查过滤；
                    # 此处不再等剩余 future，由 with 退出时自动 cancel_pending。
                    break
    else:
        for st in subtasks:
            if _is_cancelled(conn, task_id):
                break
            _execute_subtask(db_path, task_id, st, timeout, model)

    # 执行期间被取消：不汇总，保持 CANCELLED（取消是终态，不能被 DONE 覆盖）
    if _is_cancelled(conn, task_id):
        db.log_event(conn, task_id, "task cancelled, skip merge")
        return task_id

    subs = db.get_subtasks(conn, task_id)
    if no_merge:
        # 关闭汇总：各子任务独立产出，不调用 merge/audit
        db.set_task_result(conn, task_id, "（已关闭汇总：各子任务独立产出，见上方子任务列表）", None)
        db.set_task_status(conn, task_id, db.DONE)
        db.log_event(conn, task_id, "no-merge complete")
        _push_result_to_conv(conn, task_id)
        return task_id
    result = merge.merge(subs, model=model)
    # 汇总结果落盘
    summary_file = OUTPUTS_ROOT / task_id / "summary.md"
    summary_file.write_text(_render_summary(result), encoding="utf-8")
    db.set_task_result(conn, task_id, result.summary, str(summary_file))
    db.set_task_status(conn, task_id, db.DONE)
    db.log_event(
        conn,
        task_id,
        "task merged",
        data={
            "summary_len": len(result.summary),
            "conflicts": len(result.conflicts),
            "duplicates": len(result.duplicates),
            "coverage_gaps": len(result.coverage_gaps),
        },
    )
    _push_result_to_conv(conn, task_id)
    observability.get_logger(__name__).info(
        "task done",
        extra={"task_id": task_id, "summary_len": len(result.summary), "n_subtasks": len(subs), "no_merge": no_merge},
    )
    return task_id


def run_task(
    conn,
    task_prompt: str,
    scenario: str = "a",
    worker_type: str = "embedded",
    timeout: int = 600,
    model: str | None = None,
    parallel: bool = True,
    task_id: str | None = None,
    no_merge: bool = False,
) -> str:
    """全自动路径（CLI 用）：prepare + execute 一步到位，无需人工确认。"""
    task_id = prepare_task(
        conn,
        task_prompt,
        scenario=scenario,
        worker_type=worker_type,
        model=model,
        parallel=parallel,
        task_id=task_id,
        no_merge=no_merge,
    )
    return execute_task(conn, task_id, timeout=timeout, model=model)


def _render_summary(result: merge.MergeResult) -> str:
    out = [result.summary, "", "## 审计", ""]
    if result.conflicts:
        out.append("### 冲突裁决")
        for c in result.conflicts:
            out.append(f"- **{c.point}**：A={c.side_a} / B={c.side_b} → 结论：{c.verdict}")
    if result.duplicates:
        out.append("### 去重")
        for d in result.duplicates:
            out.append(f"- {d}")
    if result.coverage_gaps:
        out.append("### 覆盖遗漏（需人工确认）")
        for g in result.coverage_gaps:
            out.append(f"- {g}")
    if not (result.conflicts or result.duplicates or result.coverage_gaps):
        out.append("（无冲突 / 重复 / 遗漏）")
    return "\n".join(out)


def retry_subtask(conn, subtask_id: str, timeout: int = 600, model: str | None = None):
    """手动重派单个失败子任务（任务窗口失败重派入口）。

    历史背景：之前设 RETRY 立刻被 _execute_subtask 覆盖 RUNNING，事件流
    看不到"曾经重试过"。现在 RETRY 显式写状态（短瞬保留）+ 失败时记录
    attempts 计数（存在 error 字段前缀），前端可识别。
    """
    st = db.get_subtask(conn, subtask_id)
    if not st:
        raise KeyError(f"子任务不存在: {subtask_id}")
    task_id = st["task_id"]
    prev_status = st["status"]
    prev_error = (st.get("error") or "").strip()
    # 提取已重试次数：error 形如 "[attempt 2] 错误..." → 第 3 次重试
    attempts = 0
    if prev_error.startswith("[attempt "):
        try:
            attempts = int(prev_error.split("]")[0].split()[-1])
        except (ValueError, IndexError):
            attempts = 0
    db.set_subtask_status(conn, subtask_id, db.RETRY)
    db.log_event(
        conn,
        task_id,
        f"retry subtask {subtask_id}",
        subtask_id,
        data={"prev_status": prev_status, "prev_attempts": attempts},
    )
    _execute_subtask(_db_path(conn), task_id, st, timeout, model)
    # 把 attempt 数写回 error 字段（便于后续事件追溯）。如果这次仍失败，
    # _execute_subtask 会用新错误覆盖，这里再做一次增量：
    cur = db.get_subtask(conn, subtask_id)
    if cur and cur["status"] == db.FAILED:
        new_err = (cur.get("error") or "").strip()
        attempts += 1
        if not new_err.startswith(f"[attempt {attempts}]"):
            cur_err_prefixed = f"[attempt {attempts}] {new_err}" if new_err else f"[attempt {attempts}]"
            db.set_subtask_output(conn, subtask_id, db.FAILED, output=cur.get("output") or "", error=cur_err_prefixed)
    return db.get_subtask(conn, subtask_id)


def resume_incomplete(conn, task_id: str, timeout: int = 600, model: str | None = None):
    """重启恢复：只续跑执行中被中断的任务（RUNNING）。

    READY（待确认）任务不自动跑——那是人工闸门，重启后仍等用户确认；
    pending 属于拆分前，也不碰；CANCELLED 是终态，跳过。
    running=上次被中断，重跑其 pending/running 子任务。
    """
    task = db.get_task(conn, task_id)
    if not task or task["status"] != db.RUNNING:
        return
    db_path = _db_path(conn)
    subs = db.get_subtasks(conn, task_id)
    for st in subs:
        if st["status"] in (db.PENDING, db.RUNNING):
            _execute_subtask(db_path, task_id, st, timeout, model)
    # 续跑后子任务仍有 FAILED 则父任务置 FAILED（避免把"全失败"误标为 DONE）。
    final_subs = db.get_subtasks(conn, task_id)
    has_failed = any(s["status"] == db.FAILED for s in final_subs)
    final_status = db.FAILED if has_failed else db.DONE
    db.set_task_status(conn, task_id, final_status)
    db.log_event(
        conn,
        task_id,
        f"resume complete -> {final_status}",
        data={"n_failed": sum(1 for s in final_subs if s["status"] == db.FAILED)},
    )
