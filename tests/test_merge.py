"""汇总四规则测试：来源引用解析、冲突裁决结构、去重、覆盖扫描。

用 mock LLM 注入已知冲突/重复/遗漏样例，验证 merge 输出结构与审计段。
不依赖 API key。
"""
from maestro import merge


def _subs():
    return [
        {"id": "st_1", "desc": "读段A", "worker_type": "fake", "source_segments": "s1",
         "output": "A说项目3月启动"},
        {"id": "st_2", "desc": "读段B", "worker_type": "fake", "source_segments": "s2",
         "output": "B说项目5月启动"},
    ]


def test_merge_parses_four_rules(monkeypatch):
    monkeypatch.setattr("maestro.llm.complete", lambda *a, **k: "汇总：[来源:st_1] A称3月")
    monkeypatch.setattr("maestro.llm.complete_json", lambda *a, **k: {
        "conflicts": [{"point": "启动月份", "side_a": "3月", "side_b": "5月",
                        "evidence_a": "st_1原文", "evidence_b": "st_2原文", "verdict": "存疑"}],
        "duplicates": ["两处都提到同一负责人"],
        "coverage_gaps": ["年度总结无人覆盖"],
    })
    r = merge.merge(_subs())
    assert "来源" in r.summary
    assert len(r.conflicts) == 1
    assert r.conflicts[0].verdict == "存疑"
    assert r.duplicates == ["两处都提到同一负责人"]
    assert r.coverage_gaps == ["年度总结无人覆盖"]


def test_merge_empty_audit_ok(monkeypatch):
    monkeypatch.setattr("maestro.llm.complete", lambda *a, **k: "无冲突汇总")
    monkeypatch.setattr("maestro.llm.complete_json", lambda *a, **k: {})
    r = merge.merge(_subs())
    assert r.conflicts == [] and r.duplicates == [] and r.coverage_gaps == []
