"""端到端集成冒烟（需真 LLM key，无 key 自动跳过）。

触发条件：
  pytest tests/test_integration_smoke.py -q
  环境变量 DEEPSEEK_API_KEY 已设且格式合法（>=20 字符 + sk- 前缀）
  → 真跑（用 xfail(strict=False) 容忍余额/网络故障）

覆盖：
  - 真实 chat 一次性对话（chat.chat）+ 流式（chat.chat_stream）
  - 真实任务链路：单子任务 embedded worker 跑 LLM（orchestrator.run_task scenario=a）
  - 用户档案抽取（name/like/dislike 正则）

注意：
  - 单测约 5-15s（依赖网络/DeepSeek）
  - 数据库隔离：每个测试用临时 DB 不污染项目库
  - xfail：余额/网络/限流等真环境问题不会让 CI 红，本地想强制 assert 请用 --runxfail
"""
import os

import pytest

# 格式校验：防止 .env 里的过期/无效 key 误触发真调用
def _has_usable_key() -> bool:
    k = os.environ.get("DEEPSEEK_API_KEY", "")
    return bool(k) and len(k) >= 20 and k.startswith("sk-")


# 标记：skip 与 xfail 不能同时对一个函数生效，用 list 形式给每个用例
# 单独打标：第一个标记决定执行/跳过，第二个标记对真调用时的失败宽容。
# 模块级 pytestmark 是 list 形式，按函数顺序应用。
_skip = pytest.mark.skipif(
    not _has_usable_key(),
    reason="需要有效的 DEEPSEEK_API_KEY（>=20 字符 + sk- 前缀）才跑",
)
_xfail = pytest.mark.xfail(
    reason="集成 smoke：依赖真 LLM key 余额/网络，CI 暂不强制（strict=False）",
    strict=False,
    raises=Exception,
)
# 全局：每个用例两个标记都打上
pytestmark = [_skip, _xfail]


from maestro import chat, db, orchestrator  # noqa: E402


@pytest.fixture
def tmp_db_real(tmp_path):
    """真集成测试的独立 DB（init_db 默认行为，不连到项目库）。"""
    db_path = tmp_path / f"smoke_{__import__('uuid').uuid4().hex[:8]}.db"
    os.environ["MAESTRO_DB"] = str(db_path)
    if hasattr(db, "_migrated"):
        db._migrated.discard(str(db_path.resolve()))
    conn = db.init_db(db_path)
    yield conn
    conn.close()


def test_real_chat_one_shot(tmp_db_real):
    """真 chat.chat：发一句话，验证 1-30s 内收到非空回复。"""
    import time
    t0 = time.monotonic()
    reply = chat.chat(tmp_db_real, "用一句话说你是谁")
    elapsed = time.monotonic() - t0
    assert isinstance(reply, str) and reply.strip(), f"空回复: {reply!r}"
    assert elapsed < 30, f"回复过慢 ({elapsed:.1f}s)"
    h = db.get_chat_history(tmp_db_real)
    assert [m["role"] for m in h] == ["user", "assistant"]


def test_real_chat_stream(tmp_db_real):
    """真 chat.chat_stream：delta 累积 + 完整回复落库。"""
    parts = []
    err = None
    for kind, data in chat.chat_stream(tmp_db_real, '用两个字说你好'):
        if kind == "delta":
            parts.append(data)
        if kind == "error":
            err = data
            break
    if err is not None:
        raise RuntimeError(f"流错误: {err}")
    reply = "".join(parts).strip()
    assert len(reply) > 0
    h = db.get_chat_history(tmp_db_real)
    assert h[-1]["role"] == "assistant" and h[-1]["content"].strip()


def test_real_task_run_scenario_a(tmp_db_real):
    """真任务链路：embedded worker 跑 LLM 完成单子任务。"""
    import time
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
    assert elapsed < 50, f"任务过慢 ({elapsed:.1f}s)"


def test_real_profile_extraction(tmp_db_real):
    """档案抽取正则：名字/喜欢/讨厌。"""
    chat.chat(tmp_db_real, "我叫小明，我最喜欢吃西瓜，讨厌香菜")
    prof = db.get_user_profile(tmp_db_real)
    assert prof.get("name") in ("小明",), prof
    assert "喜欢" in prof or "讨厌" in prof, prof
