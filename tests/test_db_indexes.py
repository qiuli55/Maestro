"""db.py 索引测试。

确保高频查询字段（subtasks.task_id / task_events.task_id / 等）有索引。
"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

import pytest

from maestro import db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "t.db"))
    return db.init_db()


def _table_indexes(conn, table_name: str) -> set[str]:
    """返回表的索引名集合（含主键自动索引）。"""
    rows = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
    return {r["name"] for r in rows}


def test_subtasks_has_task_id_index(fresh_db):
    """subtasks.task_id 索引：每次执行任务都按 task_id 查子任务。"""
    indexes = _table_indexes(fresh_db, "subtasks")
    assert "subtasks_task_id_idx" in indexes, \
        f"缺少 subtasks.task_id 索引，已有：{indexes}"


def test_task_events_has_task_id_index(fresh_db):
    """task_events.task_id 索引：每个任务详情页都按 task_id 查事件。"""
    indexes = _table_indexes(fresh_db, "task_events")
    assert "task_events_task_id_idx" in indexes


def test_task_events_has_subtask_id_index(fresh_db):
    """task_events.subtask_id 索引：retry_subtask 等场景可能按 subtask_id 反查。"""
    indexes = _table_indexes(fresh_db, "task_events")
    assert "task_events_subtask_id_idx" in indexes


def test_approvals_has_task_id_index(fresh_db):
    """approvals.task_id 索引：sandbox.list_pending(task_id) 高频查询。"""
    indexes = _table_indexes(fresh_db, "approvals")
    assert "approvals_task_id_idx" in indexes


def test_chat_messages_has_conv_id_index(fresh_db):
    """chat_messages.conv_id 索引：chat history 按 conv_id 查询。"""
    indexes = _table_indexes(fresh_db, "chat_messages")
    assert "chat_messages_conv_id_idx" in indexes


def test_existing_data_can_be_queried_with_index(fresh_db):
    """创建索引后查询性能 OK（验证索引不破坏功能）。"""
    # 插入数据
    db.create_task(fresh_db, "t1", "prompt")
    db.add_subtasks(fresh_db, "t1", [
        {"id": f"t1_s{i}", "desc": f"d{i}", "worker_type": "embedded"} for i in range(5)
    ])
    db.log_event(fresh_db, "t1", "test", data={"key": "value"})
    fresh_db.commit()

    # 按 task_id 查询——索引生效
    subs = db.get_subtasks(fresh_db, "t1")
    assert len(subs) == 5
    events = db.get_events(fresh_db, "t1")
    assert len(events) == 1


def test_indexes_actually_used_by_query_planner(fresh_db):
    """EXPLAIN QUERY PLAN 必须用索引扫（不是全表扫）。"""
    db.create_task(fresh_db, "t1", "prompt")
    db.add_subtasks(fresh_db, "t1", [
        {"id": f"t1_s{i}", "desc": f"d{i}", "worker_type": "embedded"} for i in range(5)
    ])
    fresh_db.commit()

    # 查 EXPLAIN QUERY PLAN
    plan = fresh_db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM subtasks WHERE task_id=?", ("t1",)
    ).fetchall()
    plan_text = " ".join(row[3] for row in plan)
    # 必须用索引（"USING INDEX" 或 "USING COVERING INDEX"）
    assert "USING INDEX" in plan_text or "subtasks_task_id_idx" in plan_text, \
        f"任务查询未命中索引！计划：{plan_text}"


def test_index_creation_is_idempotent(tmp_path, monkeypatch):
    """多次 init_db 不报"索引已存在"错误。"""
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "t.db"))
    db.init_db()
    db.init_db()  # 第二次
    db.init_db()  # 第三次
    # 都应成功
    conn = db.init_db()
    try:
        # 索引仍唯一存在
        rows = conn.execute("PRAGMA index_list(subtasks)").fetchall()
        names = [r["name"] for r in rows]
        # subtasks_task_id_idx 只能出现一次
        assert names.count("subtasks_task_id_idx") == 1
    finally:
        conn.close()