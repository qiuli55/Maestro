"""Sandbox 审批中心集成测试。

聚焦高价值未覆盖分支：
- request_approval 三种返回（approved/rejected/timedout）
- decide 跨线程唤醒
- list_pending 过滤
- _persist 持久化（pending→approved→rejected 状态机）
- on_request hook 触发
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from maestro import db, sandbox


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """每个测试用独立 db（避免状态污染）。"""
    db_path = tmp_path / "sandbox_test.db"
    monkeypatch.setenv("MAESTRO_DB", str(db_path))
    return db_path


def test_request_approval_approved(fresh_db):
    """worker 线程阻塞 → API 批准 → 返回 'approved'。"""
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(sandbox.request_approval, "pip install foo", task_id="t1", timeout=5)
        time.sleep(0.2)  # 让 worker 线程先注册审批请求

        # 拿到审批 id 并批准
        pending = sandbox.list_pending("t1")
        assert len(pending) == 1
        approval_id = pending[0]["id"]

        assert sandbox.decide(approval_id, approve=True) is True
        result = fut.result(timeout=3)

    assert result == "approved"
    # 持久化：approved
    pending_after = sandbox.list_pending("t1")
    assert len(pending_after) == 0  # 已决定的不在 pending


def test_request_approval_rejected(fresh_db):
    """worker 阻塞 → API 拒绝 → 返回 'rejected'。"""
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(sandbox.request_approval, "del /important.txt", task_id="t2", timeout=5)
        time.sleep(0.2)
        pending = sandbox.list_pending("t2")
        approval_id = pending[0]["id"]
        sandbox.decide(approval_id, approve=False)
        result = fut.result(timeout=3)

    assert result == "rejected"


def test_request_approval_timedout(fresh_db):
    """超时未决定 → 返回 'timedout'。"""
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(sandbox.request_approval, "git push", task_id="t3", timeout=0.5)
        result = fut.result(timeout=3)

    assert result == "timedout"


def test_decide_returns_false_for_unknown_id(fresh_db):
    """decide 对不存在的 approval_id 返回 False（不抛异常）。"""
    assert sandbox.decide("ap_nonexistent", approve=True) is False


def test_list_pending_filter_by_task(fresh_db):
    """list_pending 按 task_id 过滤（不同任务的审批互不干扰）。"""
    with ThreadPoolExecutor(max_workers=2) as ex:
        # 必须后台线程跑，否则主线程会自己阻塞自己，审批永远注册不进来
        fa = ex.submit(sandbox.request_approval, "cmd_a", task_id="t_a", timeout=3)
        fb = ex.submit(sandbox.request_approval, "cmd_b", task_id="t_b", timeout=3)
        time.sleep(0.4)  # 等两条都注册

        pending_a = sandbox.list_pending("t_a")
        pending_b = sandbox.list_pending("t_b")
        pending_all = sandbox.list_pending(None)

        assert len(pending_a) == 1
        assert len(pending_b) == 1
        assert len(pending_all) == 2

        # 清理（不阻塞超时）
        sandbox.decide(pending_a[0]["id"], approve=False)
        sandbox.decide(pending_b[0]["id"], approve=False)
        fa.result(timeout=2)
        fb.result(timeout=2)


def test_on_request_hook_fires(fresh_db):
    """on_request 注册的钩子在每次新审批请求时被调。"""
    calls = []

    def hook(req):
        calls.append(req.id)

    sandbox.on_request(hook)
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(sandbox.request_approval, "ls", task_id="hook_test", timeout=2)
            time.sleep(0.3)
            pending = sandbox.list_pending("hook_test")
            sandbox.decide(pending[0]["id"], approve=True)
            fut.result(timeout=2)

        assert len(calls) == 1
        assert calls[0] == pending[0]["id"]
    finally:
        # 清理钩子（不影响其他测试）
        if hook in sandbox._notify_hooks:  # noqa: SLF001 - 测试白名单访问
            sandbox._notify_hooks.remove(hook)  # noqa: SLF001


def test_concurrent_approval_decisions(fresh_db):
    """多个 worker 线程同时申请审批，互不干扰。"""
    N = 5

    def worker(idx):
        return sandbox.request_approval(f"cmd_{idx}", task_id=f"concurrent_{idx}", timeout=3)

    with ThreadPoolExecutor(max_workers=N) as ex:
        futures = [ex.submit(worker, i) for i in range(N)]
        time.sleep(0.5)

        # 全部批准
        all_pending = sandbox.list_pending(None)
        assert len(all_pending) == N
        for p in all_pending:
            sandbox.decide(p["id"], approve=True)

        results = [f.result(timeout=3) for f in futures]

    assert results == ["approved"] * N


def test_persist_status_progression(fresh_db):
    """审批记录从 pending → approved 持久化轨迹正确。"""
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(sandbox.request_approval, "test_cmd", task_id="persist_test", timeout=3)
        time.sleep(0.3)

        pending = sandbox.list_pending("persist_test")
        approval_id = pending[0]["id"]
        sandbox.decide(approval_id, approve=True)
        fut.result(timeout=2)

        # 直接查 SQLite 看最终 status
        conn = db.init_db(fresh_db)
        try:
            row = conn.execute(
                "SELECT status, decided_at FROM approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
            assert row["status"] == "approved"
            assert row["decided_at"] is not None
        finally:
            conn.close()