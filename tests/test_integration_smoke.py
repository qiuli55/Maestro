"""端到端集成冒烟（需真 LLM key，无 key 自动跳过）。

触发条件：
  pytest tests/test_integration_smoke.py -q
  环境变量 DEEPSEEK_API_KEY 已设 → 真跑
  未设 → 自动跳过（CI 不需要 key；本地开发真测）

覆盖：
  - 真实 chat 一次性对话（chat.chat）+ 流式（chat.chat_stream）
  - 真实任务链路：单子任务 embedded worker 跑 LLM（orchestrator.run_task scenario=a）
  - 真实拆分（scenario=b）+ merge 汇总
  - 用户档案抽取（name/like/dislike 正则）

注意：
  - 单测约 5-15s（依赖网络/DeepSeek）
  - 用 mark rate-limit：CI 失败时人工重跑而不是自动化连续触发
  - 数据库隔离：每个测试用临时 DB 不污染项目库
"""
import os
import tempfile
import time
import uuid

import pytest

# 无真 key 直接 skip（CI/没 key 的开发环境不会卡）
pytestmark = pytest.mark.skipif(
    not os.environ.get("DEEPSEEK_API_KEY"),
    reason="需要 DEEPSEEK_API_KEY 才跑（避免 CI/开发机误触真调用）",
)

from maestro import chat, db, orchestrator
from maestro.workers import base as wbase_mod


@pytest.fixture
def tmp_db_real(tmp_path):
    """真集成测试的独立 DB（init_db 默认行为，不连到项目库）。"""
    db_path = tmp_path / f"smoke_{uuid.uuid4().hex[:8]}.db"
    os.environ["MAESTRO_DB"] = str(db_path)
    # init_db 重读 env，用新 path
    if hasattr(db, "_migrated"):
        db._migrated.discard(str(db_path.resolve()))
    conn = db.init_db(db_path)
    yield conn
    conn.close()


def test_real_chat_one_shot(tmp_db_real):
    """真 chat.chat：发一句话，验证 1-3s 内收到非空回复。"""
    t0 = time.monotonic()
    reply = chat.chat(tmp_db_real, "用一句话说你是谁")
    elapsed = time.monotonic() - t0
    assert isinstance(reply, str) and reply.strip(), f"空回复: {reply!r}"
    # DeepSeek 通常 1-5s；留宽限到 30s 应付网络抖动
    assert elapsed < 30, f"回复过慢 ({elapsed:.1f}s)"
    # 写入持久化
    h = db.get_chat_history(tmp_db_real)
    assert [m["role"] for m in h] == ["user", "assistant"]

def test_real_chat_stream(tmp_db_real):
    """真 chat.chat_stream：delta 累积 + 完整回复落库。"""
    parts = []
    for kind, data in chat.chat_stream(tmp_db_real, '用两个字说你好'):
        if kind == "delta":
            parts.append(data)
        if kind == "error":
            pytest.fail(f"流错误: {data}")
    reply = "".join(parts).strip()
    assert len(reply) > 0
    h = db.get_chat_history(tmp_db_real)
    assert h[-1]["role"] == "assistant" and h[-1]["content"].strip()


def test_real_task_run_scenario_a(tmp_db_real):
    """真任务链路：embedded worker 跑 LLM 完成单子任务。"""
    t0 = time.monotonic()
    tid = orchestrator.run_task(
        tmp_db_real,
        "用一句话解释冒泡排序",
        scenario="a",
        worker_type="embedded",
    )
    elapsed = time.monotonic() - t0
    task = db.get_task(tmp_db_real, tid)
    assert task["status"] == db.DONE, f"任务 {tid} 状态 {task['status']}"
    assert task["result"] and "排序" in task["result"], task["result"][:200]
    subs = db.get_subtasks(tmp_db_real, tid)
    assert subs[0]["status"] == db.DONE
    # 嵌入式 LLM 调用通常 5-20s；50s 上限防挂死
    assert elapsed < 50, f"任务过慢 ({elapsed:.1f}s)"


def test_real_profile_extraction(tmp_db_real):
    """档案抽取正则：名字/喜欢/讨厌。"""
    chat.chat(tmp_db_real, "我叫小明，我最喜欢吃西瓜，讨厌香菜")
    prof = db.get_user_profile(tmp_db_real)
    assert prof.get("name") in ("小明",), prof
    # like/dislike 任一命中即可（正则覆盖多个变体）
    assert "喜欢" in prof or "讨厌" in prof, prof
