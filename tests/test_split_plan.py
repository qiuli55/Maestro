"""split_plan（场景 C · AI 智能拆分）单元测试：mock LLM，验证解析与清洗。

不依赖网络/真实 DeepSeek；只测拆分器把 LLM 的 JSON 落成子任务 dict 的行为。
"""
from unittest.mock import patch

from maestro.split import split_plan, validate
from maestro.workers.base import available_workers


def test_split_plan_parses_tasks():
    fake = {"tasks": [
        {"id": "t1", "desc": "生成诉讼文书", "goal": "完整起诉状"},
        {"id": "t2", "desc": "生成传唤文书", "goal": "传票"},
        {"id": "t3", "desc": "生成问题合同", "goal": "带风险点的合同范本"},
    ]}
    with patch("maestro.split.llm.complete_json", return_value=fake):
        subs = split_plan("帮我生成三份文书")
    assert len(subs) == 3
    assert subs[0]["desc"] == "生成诉讼文书"
    assert subs[0]["goal"] == "完整起诉状"
    assert subs[0]["worker_type"] == "embedded"  # 占位，由编排器覆盖
    assert subs[0]["id"]  # 规范化后的 id 非空


def test_split_plan_skips_empty_and_caps_at_8():
    fake = {"tasks": [
        {"id": "t1", "desc": "a"},
        {"id": "t2", "desc": ""},          # 空 desc -> 跳过
        {"id": "t3", "desc": "b"},
        {"id": "t4", "desc": "c"},
        {"id": "t5", "desc": "d"},
        {"id": "t6", "desc": "e"},
        {"id": "t7", "desc": "f"},
        {"id": "t8", "desc": "g"},
        {"id": "t9", "desc": "h"},         # 第 9 个 -> 截断
    ]}
    with patch("maestro.split.llm.complete_json", return_value=fake):
        subs = split_plan("x")
    assert len(subs) == 8
    assert all(s["desc"] for s in subs)  # 没有空 desc 漏进来


def test_split_plan_validate_ok():
    fake = {"tasks": [{"id": "t1", "desc": "生成诉讼文书"}]}
    with patch("maestro.split.llm.complete_json", return_value=fake):
        subs = split_plan("x")
    cleaned = validate(subs, available_workers())
    assert cleaned[0]["worker_type"] == "embedded"
    assert cleaned[0]["id"] == "t1"


import sqlite3
import tempfile
import os
from unittest.mock import MagicMock

from maestro import db, orchestrator


def _fake_exec(db_path, task_id, subtask, timeout, model):
    """贴近真实的桩：把每个子任务产出写入 result.md（run_task 默认分支依赖目录存在）。"""
    wd = orchestrator.OUTPUTS_ROOT / task_id / subtask["id"]
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "result.md").write_text("ok", encoding="utf-8")


def test_no_merge_skips_merge():
    """no_merge=True 时不应调用 merge；False 时应调用一次。"""
    fake_subs = [{"id": "st_1", "desc": "x", "worker_type": "embedded", "source_segments": None}]
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["MAESTRO_DB"] = path
    try:
        conn = db.init_db(path)
        with patch.object(orchestrator.split, "split_plan", return_value=fake_subs), \
             patch.object(orchestrator, "_execute_subtask", _fake_exec), \
             patch.object(orchestrator.merge, "merge") as m_merge:
            m_merge.return_value = MagicMock(summary="X", conflicts=[], duplicates=[], coverage_gaps=[])
            orchestrator.run_task(conn, "需求", scenario="c", worker_type="embedded",
                                  no_merge=True, task_id="t_nm")
            assert m_merge.call_count == 0, "no_merge 时不应调用 merge"
            assert db.get_task(conn, "t_nm")["status"] == "done"
        conn.close()
        conn2 = db.init_db(path)
        with patch.object(orchestrator.split, "split_plan", return_value=fake_subs), \
             patch.object(orchestrator, "_execute_subtask", _fake_exec), \
             patch.object(orchestrator.merge, "merge") as m_merge2:
            m_merge2.return_value = MagicMock(summary="X", conflicts=[], duplicates=[], coverage_gaps=[])
            orchestrator.run_task(conn2, "需求", scenario="c", worker_type="embedded",
                                  task_id="t_m")
            assert m_merge2.call_count == 1, "默认应调用 merge 汇总"
        conn2.close()
    finally:
        os.environ.pop("MAESTRO_DB", None)
        import shutil as _sh
        for tid in ("t_nm", "t_m"):
            _sh.rmtree(orchestrator.OUTPUTS_ROOT / tid, ignore_errors=True)
        try:
            os.remove(path)
        except OSError:
            pass
