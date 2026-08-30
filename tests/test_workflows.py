# -*- coding: utf-8 -*-
# 工作流测试：加载器 / 解析器 / API
import json

from fastapi.testclient import TestClient

from maestro import config, db
from maestro.server import app, apply_workflow_defaults


def test_load_workflows_from_shipped_config():
    """仓库自带 configs/workflows.json，应至少有标准编排 + 深度研究。"""
    wfs = config.load_workflows()
    ids = {w["id"] for w in wfs}
    assert {"standard", "deep-research"} <= ids
    for w in wfs:
        assert w["name"] and w["scenario"] in ("a", "b", "c", "auto")
        assert isinstance(w["workers"], list) and w["workers"]


def test_load_workflows_missing_file(tmp_path, monkeypatch):
    """文件缺失时返回空列表（不抛错），由调用方回退。"""
    monkeypatch.setattr(config, "_WORKFLOWS_PATH", tmp_path / "nope.json")
    assert config.load_workflows() == []


def test_load_workflows_skips_dirty_entries(tmp_path, monkeypatch):
    """缺 id/name 的脏条目跳过；缺字段用默认值补齐。"""
    f = tmp_path / "workflows.json"
    f.write_text(json.dumps({"workflows": [
        {"id": "ok1", "name": "好的"},
        {"name": "没 id 的应被跳过"},
        {"id": "ok2", "name": "补默认", "scenario": "c", "workers": ["embedded"]},
    ]}), encoding="utf-8")
    monkeypatch.setattr(config, "_WORKFLOWS_PATH", f)
    wfs = config.load_workflows()
    assert [w["id"] for w in wfs] == ["ok1", "ok2"]
    assert wfs[0]["scenario"] == "auto" and wfs[0]["workers"] == ["embedded"]


def test_resolve_workflow():
    assert config.resolve_workflow("standard")["name"]
    assert config.resolve_workflow("__nope__") is None


def test_apply_workflow_defaults_fills_fields():
    payload = {"prompt": "x", "workflow": "quick-single"}
    out = apply_workflow_defaults(payload)
    assert out["scenario"] == "a"
    assert out["no_merge"] is True
    assert out["parallel"] is False
    assert out["selected_workers"] == ["embedded"]


def test_apply_workflow_defaults_explicit_wins():
    """payload 显式字段优先于工作流默认。"""
    payload = {"workflow": "quick-single", "scenario": "c", "no_merge": False,
               "selected_workers": ["embedded"], "model": None}
    out = apply_workflow_defaults(payload)
    assert out["scenario"] == "c"
    assert out["no_merge"] is False


def test_apply_workflow_defaults_model_injected():
    """deep-review 预设带 reasoner 模型；payload 未指定模型时注入。"""
    out = apply_workflow_defaults({"workflow": "deep-review"})
    assert out["model"] == "deepseek:deepseek-reasoner"


def test_apply_workflow_defaults_unknown_raises():
    try:
        apply_workflow_defaults({"workflow": "__nope__"})
        assert False, "未知工作流应抛 ValueError"
    except ValueError:
        pass


def test_workflows_endpoint():
    c = TestClient(app)
    r = c.get("/api/workflows")
    assert r.status_code == 200
    items = r.json()
    ids = {x["id"] for x in items}
    assert "standard" in ids
    for x in items:
        assert {"id", "name", "desc", "workers", "model", "confirm", "no_merge"} <= set(x)


def test_create_task_with_workflow(tmp_path, monkeypatch):
    """POST /api/tasks 带 workflow：响应回带 workflow id，任务走预设场景。"""
    import maestro.orchestrator as orch

    captured = {}

    def fake_prepare(conn, prompt, scenario="a", worker_type="embedded",
                     parallel=True, model=None, task_id=None, no_merge=False, conv_id=None,
                     selected_workers=None):
        captured["scenario"] = scenario
        captured["no_merge"] = no_merge
        return task_id or "task_x"

    monkeypatch.setattr(orch, "prepare_task", fake_prepare)
    # confirm=False 会触发真实执行链路（embedded→LLM），必须一并桩掉
    monkeypatch.setattr(orch, "execute_task", lambda conn, task_id, **k: task_id)
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    r = c.post("/api/tasks", json={"prompt": "随便写点什么", "workflow": "quick-single"})
    assert r.status_code == 200
    body = r.json()
    assert body["workflow"] == "quick-single"
    assert body["scenario"] == "a"


def test_create_task_unknown_workflow_400(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    r = c.post("/api/tasks", json={"prompt": "x", "workflow": "__nope__"})
    assert r.status_code == 400


def test_classify_skill_keywords():
    """新增技能未指定分类时按关键词自动分类。"""
    from maestro import skills as sk

    assert sk.classify_skill("Python 脚本") == "代码"
    assert sk.classify_skill("海报设计") == "设计"
    assert sk.classify_skill("Word 报告") == "文档"
    assert sk.classify_skill("视频剪辑") == "视频"
    assert sk.classify_skill("数据图表") == "数据"
    assert sk.classify_skill("竞品调研") == "网络"
    assert sk.classify_skill("随便什么") == "通用"


def test_workflow_crud_and_skills(tmp_db):
    """用户工作流/技能：保存→读取→删除，技能同名覆盖内置。"""
    from maestro import skills as sk

    db.upsert_user_workflow(tmp_db, "wf_1", "我的流水线", {"stages": [{"id": "s1", "name": "调研", "cards": []}]})
    wfs = db.list_user_workflows(tmp_db)
    assert len(wfs) == 1 and wfs[0]["name"] == "我的流水线"
    assert db.get_user_workflow(tmp_db, "wf_1")["definition"]["stages"][0]["name"] == "调研"
    assert db.delete_user_workflow(tmp_db, "wf_1") is True
    assert db.get_user_workflow(tmp_db, "wf_1") is None

    db.upsert_user_skill(tmp_db, "sk_1", "代码生成", "代码", "用户自定义片段")
    lib = {s["name"]: s for s in sk.merged_skills(tmp_db)}
    assert lib["代码生成"]["source"] == "user"  # 用户技能覆盖内置
    assert len(sk.builtin_skills()) > 10


def test_expand_desc_appends_snippets(tmp_db):
    from maestro import skills as sk

    out = sk.expand_desc("做一份报告", ["代码生成", "不存在的技能"], lib=None)
    assert "做一份报告" in out
    assert "【技能要求】" in out
    assert "可直接运行" in out  # 注入的是 snippet
    # 未知技能跳过不阻断
    assert sk.expand_desc("x", ["不存在的技能"]) == "x"


def test_workflow_crud_api(tmp_path, monkeypatch):
    """POST/GET/DELETE /api/workflows：校验 + 落库 + 详情。"""
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    r = c.post("/api/workflows", json={
        "name": "内容流水线",
        "definition": {"stages": [
            {"name": "调研", "cards": [{"desc": "收集资料", "worker": "embedded", "skills": ["网络调研"]}]},
            {"name": "写作", "cards": [{"desc": "撰写成文", "worker": "embedded"}]},
        ]},
    })
    assert r.status_code == 200
    wf_id = r.json()["id"]
    assert len(r.json()["stages"]) == 2
    # 详情
    d = c.get(f"/api/workflows/{wf_id}").json()
    assert d["source"] == "user"
    assert d["definition"]["stages"][0]["name"] == "调研"
    # 缺卡片 → 400
    bad = c.post("/api/workflows", json={"name": "坏的", "definition": {"stages": [{"name": "x", "cards": []}]}})
    assert bad.status_code == 400
    # 删除
    assert c.delete(f"/api/workflows/{wf_id}").json() == {"ok": True}
    assert c.delete(f"/api/workflows/{wf_id}").status_code == 404


def test_skills_api_auto_classify(tmp_path, monkeypatch):
    """POST /api/skills 未指定分类 → 自动分类；内置技能不可删除。"""
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    r = c.post("/api/skills", json={"name": "视频混剪"})
    assert r.status_code == 200
    assert r.json()["category"] == "视频"
    skills = c.get("/api/skills").json()
    names = {s["name"]: s for s in skills}
    assert names["视频混剪"]["source"] == "user"
    assert "代码生成" in names and names["代码生成"]["source"] == "builtin"
    builtin_id = names["代码生成"]["id"]
    assert c.delete(f"/api/skills/{builtin_id}").status_code == 404


def test_create_task_with_subtasks(tmp_path, monkeypatch):
    """编排器链路：payload 带 subtasks → prepare_custom（跳过拆分）+ 卡片级模型。"""
    import maestro.orchestrator as orch

    captured = {}

    def fake_custom(conn, prompt, task_id=None, subtasks=None, parallel=True,
                    no_merge=False, conv_id=None):
        captured["subtasks"] = subtasks
        captured["parallel"] = parallel
        return task_id or "task_x"

    monkeypatch.setattr(orch, "prepare_custom", fake_custom)
    monkeypatch.setattr(orch, "execute_task", lambda conn, task_id, **k: task_id)
    monkeypatch.setenv("MAESTRO_DB", str(tmp_path / "maestro.db"))
    c = TestClient(app)
    r = c.post("/api/tasks", json={
        "prompt": "做一份产品发布方案",
        "conv_id": "conv_wf",
        "confirm": False,
        "parallel": True,
        "subtasks": [
            {"desc": "【调研】市场与竞品", "worker_type": "embedded", "skills": ["网络调研"]},
            {"desc": "【写作】发布稿", "worker_type": "embedded", "model": "deepseek:deepseek-reasoner"},
        ],
    })
    assert r.status_code == 200
    assert r.json()["scenario"] == "custom"
    # _prepare_job 在后台线程跑，等它把捕获填上
    import time

    deadline = time.time() + 3
    while "subtasks" not in captured and time.time() < deadline:
        time.sleep(0.05)
    subs = captured["subtasks"]
    assert len(subs) == 2
    assert subs[1]["model"] == "deepseek:deepseek-reasoner"
    assert captured["parallel"] is True
