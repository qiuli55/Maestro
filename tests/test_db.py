"""db 状态层测试：schema 建表、CRUD、状态机流转、重启恢复。

用 tmp_db fixture 指向临时库，不污染项目根；不依赖 API key。
"""
import json

from maestro import db


def test_schema_and_crud(tmp_db):
    t = db.create_task(tmp_db, "task_x", "do things")
    assert t["status"] == db.PENDING
    db.set_task_status(tmp_db, "task_x", db.RUNNING)
    assert db.get_task(tmp_db, "task_x")["status"] == db.RUNNING


def test_subtask_lifecycle(tmp_db):
    db.create_task(tmp_db, "t1", "p")
    db.add_subtasks(tmp_db, "t1", [
        {"id": "st_1", "desc": "a", "worker_type": "fake"},
        {"id": "st_2", "desc": "b", "worker_type": "fake"},
    ])
    subs = db.get_subtasks(tmp_db, "t1")
    assert len(subs) == 2
    assert subs[0]["idx"] == 0 and subs[1]["idx"] == 1
    db.set_subtask_output(tmp_db, "st_1", db.DONE, output="ok", result_path="/x")
    row = db.get_subtask(tmp_db, "st_1")
    assert row["status"] == db.DONE and row["output"] == "ok"


def test_illegal_status_rejected(tmp_db):
    db.create_task(tmp_db, "t2", "p")
    try:
        db.set_task_status(tmp_db, "t2", "bogus")
        assert False, "应拒绝非法状态"
    except ValueError:
        pass


def test_events_logged(tmp_db):
    db.create_task(tmp_db, "t3", "p")
    db.log_event(tmp_db, "t3", "hello")
    evs = db.get_events(tmp_db, "t3")
    assert len(evs) == 1
    assert evs[0]["data"] is None  # 不带 data 时列为 NULL


def test_log_event_stores_data(tmp_db):
    db.create_task(tmp_db, "t4", "p")
    payload = {"worker_type": "embedded", "desc": "做X", "n": 3}
    db.log_event(tmp_db, "t4", "subtask st_1 running", "st_1", data=payload)
    ev = db.get_events(tmp_db, "t4")[0]
    # data 入库为 JSON 字符串，取回应可原数还原
    assert json.loads(ev["data"]) == payload


def test_event_data_migration(tmp_db, tmp_path):
    """存量库（无 data 列）再次 init_db 应幂等补列，且能存 data。"""
    import sqlite3

    legacy = tmp_path / "legacy.db"
    c = sqlite3.connect(legacy)
    c.execute(
        """CREATE TABLE task_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               task_id TEXT NOT NULL, subtask_id TEXT,
               ts TEXT NOT NULL, event TEXT NOT NULL)"""
    )
    c.commit()
    c.close()

    conn = db.init_db(legacy)  # 触发 ALTER 补列
    db.log_event(conn, "t5", "x", data={"k": 1})
    ev = db.get_events(conn, "t5")[0]
    assert json.loads(ev["data"]) == {"k": 1}
    conn.close()


# ---------- 取消 / 删除（任务管理）----------

def test_cancel_task(tmp_db):
    db.create_task(tmp_db, "tc", "p")
    db.set_task_status(tmp_db, "tc", db.RUNNING)
    assert db.cancel_task(tmp_db, "tc") is True
    assert db.get_task(tmp_db, "tc")["status"] == db.CANCELLED
    # 终态不可再取消
    assert db.cancel_task(tmp_db, "tc") is False
    # 不存在任务
    assert db.cancel_task(tmp_db, "missing") is False


def test_cancel_allows_ready_and_pending(tmp_db):
    db.create_task(tmp_db, "tready", "p")
    db.set_task_status(tmp_db, "tready", db.READY)
    assert db.cancel_task(tmp_db, "tready") is True
    assert db.get_task(tmp_db, "tready")["status"] == db.CANCELLED


def test_delete_task_cascades(tmp_db):
    db.create_task(tmp_db, "tdel", "p")
    db.add_subtasks(tmp_db, "tdel", [
        {"id": "tdel_st_1", "desc": "a", "worker_type": "fake"},
    ])
    db.log_event(tmp_db, "tdel", "hello")
    assert db.delete_task(tmp_db, "tdel") is True
    assert db.get_task(tmp_db, "tdel") is None
    assert db.get_subtasks(tmp_db, "tdel") == []
    assert db.get_events(tmp_db, "tdel") == []
    assert db.delete_task(tmp_db, "tdel") is False  # 已删再删返回 False


def test_cancelled_is_valid_status(tmp_db):
    db.create_task(tmp_db, "tcv", "p")
    db.set_task_status(tmp_db, "tcv", db.CANCELLED)  # 不抛 ValueError
    assert db.get_task(tmp_db, "tcv")["status"] == db.CANCELLED


def test_prune_old_events(tmp_db):
    """30 天前的事件被清理，近期保留。"""
    from datetime import UTC, datetime, timedelta

    # 直接插一条 40 天前的 + 一条新的
    import json

    old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    tmp_db.execute(
        "INSERT INTO task_events (task_id, subtask_id, ts, event, data) VALUES (?,?,?,?,?)",
        ("t_old", None, old_ts, "old", None),
    )
    tmp_db.commit()
    db.log_event(tmp_db, "t_new", "fresh event")
    n = db.prune_old_events(tmp_db, days=30)
    assert n >= 1
    rows = tmp_db.execute("SELECT task_id FROM task_events").fetchall()
    ids = {r[0] for r in rows}
    assert "t_old" not in ids
    assert "t_new" in ids
