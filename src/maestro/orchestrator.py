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

from . import runtime as _runtime_mod

# 任务产物目录：MAESTRO_OUTPUTS 优先；未设则项目根/outputs（自动创建）
OUTPUTS_ROOT = Path(os.environ.get("MAESTRO_OUTPUTS") or _runtime_mod.outputs_dir())


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
    # 工作流卡片级模型优先于任务级模型
    model = subtask.get("model") or model
    try:
        if _mark_cancelled_if_requested(tconn, task_id, sid):
            return
        db.set_subtask_status(tconn, sid, db.RUNNING)
        db.log_event(
            tconn,
            task_id,
            f"subtask {sid} running",
            sid,
            data={"worker_type": subtask["worker_type"], "desc": subtask["desc"]},
        )

        worker = _pick_worker(tconn, task_id, subtask)
        if worker is None:
            return  # 健康预检/沙箱拦截的失败原因已由 _pick_worker 落库

        desc = _build_prompt(tconn, task_id, subtask)
        res = worker.spawn(desc, str(workdir), timeout, task_id=task_id, subtask_id=sid)
        res = _maybe_retry(worker, res, tconn, task_id, sid, subtask, str(workdir), timeout)

        if res.timed_out or res.returncode != 0 or res.error:
            db.set_subtask_output(tconn, sid, db.FAILED, error=_err_msg(res))
            db.log_event(tconn, task_id, f"subtask {sid} FAILED", sid, data={"error": _err_msg(res)})
            return

        _finish_success(tconn, task_id, sid, res, workdir)
    except Exception as e:  # noqa: BLE001 — 隔离一切异常：不拖垮整任务（并行时尤为关键）
        _mark_failed(tconn, task_id, sid, e)
    finally:
        tconn.close()


def _mark_cancelled_if_requested(tconn, task_id: str, sid: str) -> bool:
    """协作式取消闸门：spawn 前最后一道检查，已取消则标记 CANCELLED 返回 True。

    线程池并行派发后，这里能立即标记而不调 worker（避免浪费 token/时间）。
    """
    if not _is_cancelled(tconn, task_id):
        return False
    db.set_subtask_output(tconn, sid, db.CANCELLED, error="[cancelled before dispatch]")
    return True


def _pick_worker(tconn, task_id: str, subtask: dict):
    """选 worker 并做健康预检 + 沙箱防线 1 扫描；失败已落库，返回 None 表示放弃。"""
    sid = subtask["id"]
    worker = get_worker(subtask["worker_type"])

    # Worker 健康预检：避免子进程启动失败后才报错（用户感知是卡死很久）
    if isinstance(worker, wbase.SubprocessWorker):
        ok, reason = worker.check_health()
        if not ok:
            db.set_subtask_output(tconn, sid, db.FAILED,
                                  error=f"[worker 不可用] {reason}")
            db.log_event(tconn, task_id, f"subtask {sid} WORKER_UNHEALTHY", sid,
                         data={"reason": reason})
            return None

    # 沙箱防线 1：外部 CLI（黑盒 agent）派发前对 prompt 做危险指令前置检测。
    # embedded 有工具白名单+审批流兜底，不重复扫描（避免误伤正常代码需求）。
    if isinstance(worker, wbase.SubprocessWorker):
        level, reason = guard.scan(subtask["desc"])
        if level == "block":
            db.set_subtask_output(tconn, sid, db.FAILED, error=f"[沙箱拦截] {reason}")
            db.log_event(tconn, task_id, f"subtask {sid} BLOCKED by guard", sid, data={"reason": reason})
            return None
        if level == "warn":
            db.log_event(tconn, task_id, f"subtask {sid} guard warning", sid, data={"reason": reason})
    return worker


def _build_prompt(tconn, task_id: str, subtask: dict) -> str:
    """组装子任务 prompt：[[PREV_OUTPUT]] 占位替换为上一环节产出。

    取同任务 stage 更小且已 DONE 的子任务 output（最近 3 个、总长 4000 字上限，
    防上下文爆炸）。
    """
    desc = subtask["desc"]
    if "[[PREV_OUTPUT]]" not in desc:
        return desc
    stg = subtask.get("stage")
    if stg is None:
        return desc.replace("[[PREV_OUTPUT]]", "（无环节信息）")
    prev_rows = tconn.execute(
        "SELECT output FROM subtasks WHERE task_id=? AND stage<? AND status='done' "
        "AND output IS NOT NULL ORDER BY idx DESC LIMIT 3",
        (task_id, stg),
    ).fetchall()
    prev_text = "\n\n".join(r["output"] for r in reversed(prev_rows))[:4000]
    return desc.replace(
        "[[PREV_OUTPUT]]",
        "（上一环节产出）\n" + (prev_text or "（上一环节无产出）") + "\n",
    )


def _maybe_retry(worker, res, tconn, task_id: str, sid: str, subtask: dict,
                 workdir: str, timeout: int):
    """瞬时故障（超时/网络/限流/5xx）重试 1 次；永久失败不重跑，避免重复计费。

    重试用 subtask["desc"] 原文（不含上一环节注入），与原实现一致。
    """
    if not (res.timed_out or res.returncode != 0 or res.error):
        return res
    if not _is_retryable_worker_error(res):
        db.log_event(tconn, task_id, f"subtask {sid} failed (permanent, no retry)", sid,
                     data={"error": _err_msg(res)})
        return res
    db.log_event(tconn, task_id, f"subtask {sid} failed, retrying", sid, data={"error": _err_msg(res)})
    # task_id/subtask_id 必须带上：embedded 的审批流靠它归属审批单
    return worker.spawn(subtask["desc"], workdir, timeout, task_id=task_id, subtask_id=sid)


def _finish_success(tconn, task_id: str, sid: str, res, workdir) -> None:
    """成功收尾：产出落盘 + DONE + 事件。"""
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


def _mark_failed(tconn, task_id: str, sid: str, e: Exception) -> None:
    """异常收尾：FAILED 落库；DB 也异常时只留日志（并行隔离，不拖垮整任务）。"""
    err = f"执行异常: {type(e).__name__}: {str(e)[:300]}"
    try:
        db.set_subtask_output(tconn, sid, db.FAILED, error=err)
        db.log_event(tconn, task_id, f"subtask {sid} FAILED (异常)", sid, data={"error": err})
    except Exception as db_e:  # noqa: BLE001 — DB 也已异常时不再传播，只留日志
        observability.get_logger(__name__).error(
            "subtask %s 失败且错误落库也失败: 执行错=%s 落库错=%s", sid, err, db_e)


# 可重试的子任务失败关键词：超时/网络/上游限流/5xx 是瞬时故障，值得重跑一次；
# 其余（bin 不存在、参数错误、业务失败）是永久失败，重跑只会再烧一遍钱。
_RETRYABLE_ERR_HINTS = (
    "timeout", "超时", "timed out", "connection", "网络", "rate limit", "429",
    "500", "502", "503", "504", "temporar", "server error", "上游",
)


def _is_retryable_worker_error(res) -> bool:
    if res.timed_out:
        return True
    text = ((res.error or "") + " " + (res.output or "")).lower()
    return any(hint in text for hint in _RETRYABLE_ERR_HINTS)


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


def _clean_workflow_cards(conn, items: list[dict], task_id: str) -> list[dict]:
    """校验并清洗工作流卡片：desc 必填、worker 合法、skills 展开、stage 合法。

    skills 展开为【技能要求】附加到 desc；引用上一环节产出时加 [[PREV_OUTPUT]]
    标记（执行时由 _execute_subtask 替换为上一 stage 已 DONE 子任务的 output）。
    """
    from . import skills as skills_mod

    allowed = wbase.available_workers()
    lib = {s["name"]: s for s in skills_mod.merged_skills(conn)}
    cleaned = []
    for i, st in enumerate(items):
        desc = (st.get("desc") or "").strip()
        if not desc:
            raise ValueError(f"卡片 #{i + 1} 缺任务描述")
        wt = st.get("worker_type") or "embedded"
        if wt not in allowed:
            raise ValueError(f"卡片 #{i + 1} 智能体非法: {wt}")
        skills = [str(s)[:30] for s in (st.get("skills") or [])][:8]
        desc_full = skills_mod.expand_desc(desc[:800], skills, lib)
        if st.get("use_prev"):
            desc_full = "[[PREV_OUTPUT]]\n" + desc_full
        stage = st.get("stage")
        if stage is not None and (not isinstance(stage, int) or stage < 0):
            raise ValueError(f"卡片 #{i + 1} stage 非法: {stage}")
        cleaned.append({
            "id": f"{task_id}_st_{i + 1}",
            "desc": desc_full,
            "worker_type": wt,
            "model": st.get("model") or None,
            "stage": stage,
        })
        # skills 白名单之外的信息不落库
    return cleaned


def prepare_custom(
    conn,
    prompt: str,
    task_id: str | None = None,
    subtasks: list[dict] | None = None,
    parallel: bool = True,
    no_merge: bool = False,
    conv_id: str | None = None,
) -> str:
    """自定义子任务链路（工作流编排器）：跳过拆分，直接把启用卡片作为子任务入库。

    subtasks: [{desc, worker_type, model?, skills?}]。
    入库后停 READY（confirm=False 由调用方接着 execute）。
    """
    task_id = task_id or f"task_{uuid.uuid4().hex[:8]}"
    items = subtasks or []
    if not items:
        raise ValueError("工作流没有启用的卡片")
    cleaned = _clean_workflow_cards(conn, items, task_id)
    db.create_task(conn, task_id, prompt, conv_id=conv_id)
    db.set_task_params(
        conn, task_id,
        scenario="custom",
        worker_type=cleaned[0]["worker_type"],
        parallel=parallel,
        no_merge=no_merge,
    )
    db.add_subtasks(conn, task_id, cleaned)
    db.set_task_status(conn, task_id, db.READY)
    db.log_event(
        conn, task_id,
        f"custom workflow -> {len(cleaned)} cards (ready)",
        data=[{"id": s["id"], "worker_type": s["worker_type"], "model": s["model"]} for s in cleaned],
    )
    observability.get_logger(__name__).info(
        "custom workflow prepared",
        extra={"task_id": task_id, "n_cards": len(cleaned), "parallel": parallel},
    )
    return task_id


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
    """检查任务是否被取消（协作式）。

    两种触发路径都算：
    - status == CANCELLED（终态）
    - cancel_requested_at 非空（用户已点取消，DB 已记录，但 status 可能还在 PENDING/RUNNING）

    子任务 dispatch 前/完成后调；为 True 时停止后续调度、保持 CANCELLED 终态。
    """
    t = db.get_task(conn, task_id)
    if not t:
        return False
    if t["status"] == db.CANCELLED:
        return True
    return bool(t.get("cancel_requested_at"))


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


def _finalize_from_subs(conn, task_id: str, prefix: str, extra: dict | None = None) -> str:
    """按子任务终态聚合任务终态：仍有 FAILED 则 FAILED，否则 DONE 并推送会话。

    聚合口径与 resume_incomplete 一致：不把"全失败"标成 DONE。
    日志消息格式固定为 "{prefix} -> {终态}"（custom / 场景 A 共用）。
    """
    final_subs = db.get_subtasks(conn, task_id)
    n_failed = sum(1 for s in final_subs if s["status"] == db.FAILED)
    final_status = db.FAILED if n_failed else db.DONE
    db.set_task_status(conn, task_id, final_status)
    db.log_event(conn, task_id, f"{prefix} -> {final_status}",
                 data={"n_failed": n_failed, **(extra or {})})
    if final_status == db.DONE:
        _push_result_to_conv(conn, task_id)
    return final_status


def _run_stage_parallel(db_path: str, task_id: str, group: list[dict],
                        timeout: int, model: str | None) -> None:
    """工作流单环节内并行执行（上限 8 线程）；f.result() 重抛子线程异常。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(group), 8)) as ex:
        futures = [ex.submit(_execute_subtask, db_path, task_id, st, timeout, model)
                   for st in group]
        for f in concurrent.futures.as_completed(futures):
            f.result()


def _execute_workflow(conn, task_id: str, subtasks: list[dict], db_path: str,
                      timeout: int, model: str | None, parallel: bool) -> str:
    """custom 工作流执行：按 stage 分组——环节内并行、环节间串行（前一环节
    产出经 [[PREV_OUTPUT]] 注入下一环节）。"""
    stage_groups: dict[int, list[dict]] = {}
    for st in subtasks:
        stage_groups.setdefault(st.get("stage") or 0, []).append(st)
    for stage_no in sorted(stage_groups):
        if _is_cancelled(conn, task_id):
            db.log_event(conn, task_id, "workflow cancelled mid-stage, stop dispatch",
                         data={"stage": stage_no})
            break
        group = stage_groups[stage_no]
        db.log_event(conn, task_id, f"workflow stage {stage_no} start",
                     data={"cards": [st["id"] for st in group]})
        if parallel and len(group) > 1:
            _run_stage_parallel(db_path, task_id, group, timeout, model)
        else:
            for st in group:
                _execute_subtask(db_path, task_id, st, timeout, model)
    # 取消是终态：直接返回，不能用 DONE 覆盖（与 resume_incomplete 口径一致）
    if _is_cancelled(conn, task_id):
        return task_id
    _finalize_from_subs(conn, task_id, "workflow complete")
    return task_id


def _execute_scenario_a(conn, task_id: str, subtasks: list[dict], db_path: str,
                        timeout: int, model: str | None) -> str:
    """场景 A：串行逐个投喂，互不干扰；中途取消即停。"""
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
    _finalize_from_subs(conn, task_id, "scenario A serial complete",
                        extra={"subtasks": [st["id"] for st in subtasks]})
    return task_id


def _dispatch_subtasks(conn, task_id: str, subtasks: list[dict], db_path: str,
                       timeout: int, model: str | None, parallel: bool) -> None:
    """场景 B/C 派发：并行（上限 8）或串行；派发期间检查取消标志及时收手。"""
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


def _finalize_no_merge(conn, task_id: str, subs: list[dict]) -> str:
    """关闭汇总收尾：各子任务独立产出，不调用 merge；仍有 FAILED 则任务置 FAILED。"""
    n_failed = sum(1 for s in subs if s["status"] == db.FAILED)
    if n_failed:
        db.set_task_status(conn, task_id, db.FAILED)
        db.log_event(conn, task_id, "no-merge complete -> FAILED", data={"n_failed": n_failed})
        return task_id
    db.set_task_result(conn, task_id, "（已关闭汇总：各子任务独立产出，见上方子任务列表）", None)
    db.set_task_status(conn, task_id, db.DONE)
    db.log_event(conn, task_id, "no-merge complete")
    _push_result_to_conv(conn, task_id)
    return task_id


def _finalize_merge(conn, task_id: str, subs: list[dict], model: str | None):
    """汇总收尾：merge 结果落盘 + DONE；汇总失败（如熔断/网络）落 FAILED 并抛出。"""
    try:
        result = merge.merge(subs, model=model)
    except Exception as e:  # noqa: BLE001 — 与 _prepare_job 对齐：失败要落库可见
        db.set_task_status(conn, task_id, db.FAILED)
        db.log_event(conn, task_id, "merge FAILED", data={"error": str(e)[:500]})
        raise
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
    return result


def _begin_execution(conn, task_id: str, subtasks: list[dict]) -> dict:
    """执行前置：校验状态 + 置 RUNNING + 起始事件/日志。校验不过直接抛。"""
    task = db.get_task(conn, task_id)
    if not task:
        raise KeyError(f"任务不存在: {task_id}")
    if task["status"] != db.READY:
        raise ValueError(f"任务状态 {task['status']} 不可执行（需 ready）")
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
    return task


def execute_task(
    conn,
    task_id: str,
    timeout: int = 600,
    model: str | None = None,
) -> str:
    """人工闸门第二段：按库内（可能已编辑）子任务执行完整链路。

    仅接受 READY 状态任务；执行开始后置 RUNNING，结束置 DONE；
    子任务仍有 FAILED（串行/无汇总路径）或汇总失败时置 FAILED。
    执行中可取消（db.cancel_task 置 CANCELLED）：串行在派发下一个前检查，
    并行在全部收尾后检查——取消则不汇总、保持 CANCELLED。
    """
    subtasks = db.get_subtasks(conn, task_id)
    task = _begin_execution(conn, task_id, subtasks)

    parallel = bool(task["parallel"])
    no_merge = bool(task["no_merge"])
    db_path = _db_path(conn)  # 主线程算路径，跨线程只传字符串

    if task["scenario"] == "custom":
        return _execute_workflow(conn, task_id, subtasks, db_path, timeout, model, parallel)
    if task["scenario"] == "a":
        return _execute_scenario_a(conn, task_id, subtasks, db_path, timeout, model)

    _dispatch_subtasks(conn, task_id, subtasks, db_path, timeout, model, parallel)

    # 执行期间被取消：不汇总，保持 CANCELLED（取消是终态，不能被 DONE 覆盖）
    if _is_cancelled(conn, task_id):
        db.log_event(conn, task_id, "task cancelled, skip merge")
        return task_id

    subs = db.get_subtasks(conn, task_id)
    if no_merge:
        return _finalize_no_merge(conn, task_id, subs)
    result = _finalize_merge(conn, task_id, subs, model)
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
