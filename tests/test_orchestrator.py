"""编排器生命周期测试：parse->split->dispatch(串行/并行)->collect->merge->deliver。

覆盖：串行链路、并行+汇总、失败重试 1 次、失败隔离、重启恢复（resume_incomplete）。
用 FakeWorker + mock LLM，不依赖 API key；outputs 隔离到 tmp。
"""
from maestro import db, merge, orchestrator
from maestro.workers.base import get_worker


def test_scenario_a_serial(tmp_db, monkeypatch):
    # 用 fake worker 跑串行链路
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    tid = orchestrator.run_task(tmp_db, "加登录\n加支付", scenario="a", worker_type="fake")
    task = db.get_task(tmp_db, tid)
    assert task["status"] == db.DONE
    subs = db.get_subtasks(tmp_db, tid)
    assert len(subs) == 2
    assert all(s["status"] == db.DONE for s in subs)
    # 产出落盘
    assert all(s["result_path"] for s in subs)


def test_scenario_b_parallel_merge(tmp_db, monkeypatch):
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    monkeypatch.setattr("maestro.llm.complete_json", lambda *a, **k: {
        "segments": [{"id": "s1", "title": "一", "text": "甲"}, {"id": "s2", "title": "二", "text": "乙"}]
    })
    monkeypatch.setattr("maestro.llm.complete", lambda *a, **k: "汇总报告")
    tid = orchestrator.run_task(tmp_db, "长文本", scenario="b", worker_type="fake")
    task = db.get_task(tmp_db, tid)
    assert task["status"] == db.DONE
    subs = db.get_subtasks(tmp_db, tid)
    assert all(s["status"] == db.DONE for s in subs), "并行派发后子任务应全部 DONE"
    assert "汇总报告" in (task["result"] or "")
    # 汇总落盘
    assert task["result_path"]


def test_failure_isolation_retry_once(tmp_db, monkeypatch):
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("flaky"))
    tid = orchestrator.run_task(tmp_db, "需求X", scenario="a", worker_type="flaky")
    subs = db.get_subtasks(tmp_db, tid)
    assert subs[0]["status"] == db.FAILED  # 重试仍失败 -> 标红


def test_worker_exception_isolated_not_crash_task(tmp_db, monkeypatch):
    """worker.spawn 抛异常：子任务标 FAILED，任务本身不崩（并行场景关键）。"""
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("boom"))
    tid = orchestrator.run_task(tmp_db, "A\nB", scenario="a", worker_type="boom")
    subs = db.get_subtasks(tmp_db, tid)
    assert len(subs) == 2
    assert all(s["status"] == db.FAILED for s in subs)
    task = db.get_task(tmp_db, tid)
    assert task["status"] == db.DONE  # 任务仍完成（子任务失败被记录，不传播）
    # 错误信息落库（"执行异常"前缀）
    assert "执行异常" in (subs[0]["error"] or "")


def test_unknown_worker_type_isolated(tmp_db, monkeypatch):
    """库内子任务 worker_type 被外部改坏（绕过 split.validate）：execute 不崩，
    子任务标 FAILED——编排器异常隔离是 API 校验之外的防御纵深。"""
    def _bad_get(name):
        raise KeyError(f"未注册的 worker: {name}")
    monkeypatch.setattr("maestro.orchestrator.get_worker", _bad_get)
    tid = orchestrator.run_task(tmp_db, "需求X", scenario="a", worker_type="fake")
    # 直接改库内 worker_type 为非法值（模拟外部写坏 / 历史脏数据）
    subs = db.get_subtasks(tmp_db, tid)
    conn = tmp_db
    conn.execute("UPDATE subtasks SET worker_type='nope' WHERE id=?", (subs[0]["id"],))
    conn.commit()
    db.set_task_status(tmp_db, tid, db.READY)
    orchestrator.execute_task(tmp_db, tid)
    subs = db.get_subtasks(tmp_db, tid)
    assert subs[0]["status"] == db.FAILED
    assert "未注册的 worker" in (subs[0]["error"] or "")
    assert db.get_task(tmp_db, tid)["status"] == db.DONE


def test_resume_incomplete(tmp_db, monkeypatch):
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    tid = orchestrator.run_task(tmp_db, "A\nB", scenario="a", worker_type="fake")
    # 模拟中断：任务回到 RUNNING，第二个子任务改回 pending，再 resume
    db.set_task_status(tmp_db, tid, db.RUNNING)
    subs = db.get_subtasks(tmp_db, tid)
    db.set_subtask_status(tmp_db, subs[1]["id"], db.PENDING)
    orchestrator.resume_incomplete(tmp_db, tid)
    subs = db.get_subtasks(tmp_db, tid)
    assert all(s["status"] == db.DONE for s in subs)


# ---------- 人工闸门：prepare(拆分停) -> 编辑 -> execute(确认执行) ----------

def test_prepare_stops_at_ready_not_executed(tmp_db, monkeypatch):
    """prepare 只拆分入库，停在 READY，不执行任何子任务。"""
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    tid = orchestrator.prepare_task(tmp_db, "加登录\n加支付", scenario="a", worker_type="fake")
    task = db.get_task(tmp_db, tid)
    assert task["status"] == db.READY
    subs = db.get_subtasks(tmp_db, tid)
    assert len(subs) == 2
    assert all(s["status"] == db.PENDING for s in subs), "prepare 后子任务应全部 pending（未执行）"
    assert all(s["result_path"] is None for s in subs), "prepare 阶段不应有产出"


def test_execute_runs_edited_subtasks(tmp_db, monkeypatch):
    """编辑子任务（增删改调序）后 execute 按新列表执行。"""
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    tid = orchestrator.prepare_task(tmp_db, "需求1\n需求2\n需求3", scenario="a", worker_type="fake")
    # 编辑：删第 2 条、改第 1 条 desc、新增一条
    db.replace_subtasks(tmp_db, tid, [
        {"id": f"{tid}_st_1", "desc": "改过的需求1", "worker_type": "fake"},
        {"id": f"{tid}_st_new", "desc": "新增的需求", "worker_type": "fake"},
    ])
    orchestrator.execute_task(tmp_db, tid)
    task = db.get_task(tmp_db, tid)
    assert task["status"] == db.DONE
    subs = db.get_subtasks(tmp_db, tid)
    assert len(subs) == 2
    assert all(s["status"] == db.DONE for s in subs)
    assert subs[0]["desc"] == "改过的需求1"
    assert subs[1]["desc"] == "新增的需求"


def test_execute_rejects_non_ready(tmp_db, monkeypatch):
    """非 READY 状态任务不可 execute。"""
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    tid = orchestrator.run_task(tmp_db, "A", scenario="a", worker_type="fake")  # 已 DONE
    try:
        orchestrator.execute_task(tmp_db, tid)
        assert False, "DONE 任务再 execute 应抛错"
    except ValueError:
        pass


def test_prepare_then_run_task_equivalent(tmp_db, monkeypatch):
    """run_task = prepare + execute 等价（回归：CLI 全自动路径不变）。"""
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    monkeypatch.setattr("maestro.llm.complete_json", lambda *a, **k: {
        "segments": [{"id": "s1", "title": "一", "text": "甲"}]
    })
    monkeypatch.setattr("maestro.llm.complete", lambda *a, **k: "汇总报告")
    tid = orchestrator.run_task(tmp_db, "长文本", scenario="b", worker_type="fake")
    task = db.get_task(tmp_db, tid)
    assert task["status"] == db.DONE
    assert "汇总报告" in (task["result"] or "")


def test_resume_skips_ready_task(tmp_db, monkeypatch):
    """重启恢复不碰 READY（待确认）任务——人工闸门重启后仍等确认。"""
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    tid = orchestrator.prepare_task(tmp_db, "A\nB", scenario="a", worker_type="fake")
    orchestrator.resume_incomplete(tmp_db, tid)
    task = db.get_task(tmp_db, tid)
    assert task["status"] == db.READY, "READY 任务不应被 resume 跑完"
    subs = db.get_subtasks(tmp_db, tid)
    assert all(s["status"] == db.PENDING for s in subs), "READY 任务的子任务不应被 resume 执行"


# ---------- 取消 / 删除 / 恢复 ----------

def test_cancel_mid_serial_stops_dispatch(tmp_db, monkeypatch):
    """串行执行中取消：后续子任务不再派发，任务保持 CANCELLED。"""
    import threading

    calls = {"n": 0}
    orig = orchestrator._execute_subtask
    def slow_execute(db_path, task_id, st, timeout, model):
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一个子任务执行期间触发取消（模拟用户点取消）
            db.cancel_task(tmp_db, task_id)
        return orig(db_path, task_id, st, timeout, model)
    monkeypatch.setattr("maestro.orchestrator._execute_subtask", slow_execute)
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))

    tid = orchestrator.run_task(tmp_db, "A\nB\nC", scenario="a", worker_type="fake")
    task = db.get_task(tmp_db, tid)
    assert task["status"] == db.CANCELLED, "取消后任务应保持 CANCELLED"
    subs = db.get_subtasks(tmp_db, tid)
    assert calls["n"] == 1, "取消后不应再派发后续子任务"
    assert subs[0]["status"] == db.DONE
    assert all(s["status"] == db.PENDING for s in subs[1:]), "未派发的子任务保持 pending"


def test_cancel_parallel_skips_merge(tmp_db, monkeypatch):
    """并行执行中取消：收尾后不 merge，保持 CANCELLED。"""
    import threading

    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    monkeypatch.setattr("maestro.llm.complete_json", lambda *a, **k: {
        "segments": [{"id": "s1", "title": "一", "text": "甲"}, {"id": "s2", "title": "二", "text": "乙"}]
    })
    # merge 不应被调用（取消后跳过）
    def boom(*a, **k):
        raise AssertionError("取消后不应调 merge")
    monkeypatch.setattr("maestro.llm.complete", boom)

    # 第一个子任务开始执行时，用独立连接取消任务（子线程不能复用主连接）
    from pathlib import Path
    db_path = tmp_db.execute("PRAGMA database_list").fetchone()[2]
    orig = orchestrator._execute_subtask
    cancelled = threading.Event()
    def cancel_first(db_path_, task_id, st, timeout, model):
        if not cancelled.is_set():
            c2 = db.init_db(db_path_)
            db.cancel_task(c2, task_id)
            c2.close()
            cancelled.set()
        return orig(db_path_, task_id, st, timeout, model)
    monkeypatch.setattr("maestro.orchestrator._execute_subtask", cancel_first)

    tid = orchestrator.prepare_task(tmp_db, "长文本", scenario="b", worker_type="fake")
    orchestrator.execute_task(tmp_db, tid)
    task = db.get_task(tmp_db, tid)
    assert task["status"] == db.CANCELLED, "并行场景取消后不 merge 不置 DONE"


def test_execute_rejects_cancelled(tmp_db, monkeypatch):
    """已取消任务不可 execute（保护）。"""
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    tid = orchestrator.prepare_task(tmp_db, "A", scenario="a", worker_type="fake")
    db.cancel_task(tmp_db, tid)
    try:
        orchestrator.execute_task(tmp_db, tid)
        assert False, "CANCELLED 任务再 execute 应抛错"
    except ValueError:
        pass


def test_resume_skips_cancelled(tmp_db, monkeypatch):
    """resume 不碰 CANCELLED 终态任务。"""
    monkeypatch.setattr("maestro.orchestrator.get_worker", lambda name: get_worker("fake"))
    tid = orchestrator.prepare_task(tmp_db, "A\nB", scenario="a", worker_type="fake")
    db.cancel_task(tmp_db, tid)
    orchestrator.resume_incomplete(tmp_db, tid)
    task = db.get_task(tmp_db, tid)
    assert task["status"] == db.CANCELLED
    subs = db.get_subtasks(tmp_db, tid)
    assert all(s["status"] == db.PENDING for s in subs)
