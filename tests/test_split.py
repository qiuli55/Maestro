"""拆分测试：场景 A 多需求逐行 -> 独立子任务；场景 B 长文 LLM 拆段。

验证子任务数、worker_type 分配、source_segments 来源段标注、结构校验。
用 FakeWorker，不依赖 API key。
"""
from maestro import split
from maestro.workers.base import available_workers


def test_split_requirements_one_per_line():
    subs = split.split_requirements("加登录\n加支付\n加报表", "opencode")
    assert len(subs) == 3
    assert subs[0]["id"] == "st_1" and subs[0]["worker_type"] == "opencode"
    assert subs[0]["source_segments"] is None


def test_validate_rejects_bad_worker():
    try:
        split.validate([{"desc": "x", "worker_type": "nope"}], available_workers())
        assert False
    except ValueError:
        pass


def test_validate_normalizes_id():
    out = split.validate([{"id": "st 1!", "desc": "x", "worker_type": "fake"}], available_workers())
    assert out[0]["id"] == "st_1"


def test_split_document_calls_llm(monkeypatch):
    import json

    fake_segs = {"segments": [
        {"id": "s1", "title": "第一段", "text": "内容A"},
        {"id": "s2", "title": "第二段", "text": "内容B"},
    ]}

    def fake_json(system, user, model=None):
        return fake_segs

    monkeypatch.setattr("maestro.llm.complete_json", fake_json)
    subs = split.split_document("很长很长的文本", max_segments=5)
    assert len(subs) == 2
    assert subs[0]["source_segments"] == "s1"
    assert "内容A" in subs[0]["desc"]
