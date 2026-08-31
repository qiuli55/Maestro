"""工作流 / 技能 REST API（预设 + 用户自建 + 技能库）。"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from .. import db
from .. import config as _cfg
from ..workers import base as wbase
import maestro.workers as _wmod

router = APIRouter()


def _validate_workflow_definition(definition) -> list[dict]:
    """校验并清洗工作流定义，返回规范化的 stages。"""
    if not isinstance(definition, dict) or not isinstance(definition.get("stages"), list) \
            or not definition["stages"]:
        raise HTTPException(400, "definition.stages 不能为空")
    available = wbase.available_workers()
    cleaned = []
    for i, st in enumerate(definition["stages"]):
        if not isinstance(st, dict):
            raise HTTPException(400, f"环节 #{i + 1} 格式错误")
        name = str(st.get("name") or f"环节{i + 1}").strip()[:30]
        raw_cards = st.get("cards")
        if not isinstance(raw_cards, list) or not raw_cards:
            raise HTTPException(400, f"环节「{name}」至少需要一张卡片")
        cards = []
        for c in raw_cards:
            if not isinstance(c, dict):
                raise HTTPException(400, f"环节「{name}」存在格式错误的卡片")
            desc = str(c.get("desc") or "").strip()
            if not desc:
                raise HTTPException(400, f"环节「{name}」有卡片缺任务描述")
            wt = c.get("worker") or "embedded"
            if wt not in available:
                raise HTTPException(400, f"环节「{name}」卡片智能体非法: {wt}")
            cards.append({
                "id": str(c.get("id") or f"card_{os.urandom(4).hex()}"),
                "enabled": bool(c.get("enabled", True)),
                "desc": desc[:800],
                "worker": wt,
                "model": c.get("model") or None,
                "skills": [str(s)[:30] for s in (c.get("skills") or [])][:8],
            })
        cleaned.append({
            "id": str(st.get("id") or f"stage_{os.urandom(4).hex()}"),
            "name": name,
            "cards": cards,
        })
    return cleaned


@router.get("/api/workflows")
def list_workflows():
    """工作流库（任务处理模板）：前端任务窗口的选择器数据源。"""
    from .. import config as _cfg

    available = _wmod.available_workers()
    out = []
    for wf in _cfg.load_workflows():
        workers = [w for w in wf["workers"] if w in available]
        out.append({
            "id": wf["id"],
            "name": wf["name"],
            "icon": wf["icon"],
            "desc": wf["desc"],
            "scenario": wf["scenario"],
            "parallel": wf["parallel"],
            "workers": workers or wf["workers"],
            "workers_available": bool(workers),
            "model": wf["model"],
            "confirm": wf["confirm"],
            "no_merge": wf["no_merge"],
            "source": "preset",
        })
    conn = db.init_db()
    try:
        for uw in db.list_user_workflows(conn):
            out.append({
                "id": uw["id"],
                "name": uw["name"],
                "icon": "🧩",
                "desc": "自定义工作流",
                "source": "user",
            })
    finally:
        conn.close()
    return out


@router.get("/api/workflows/{wf_id}")
def get_workflow_detail(wf_id: str):
    """单个工作流详情（编排器载入用）：preset 返回预设字段，user 返回完整 definition。"""
    from .. import config as _cfg

    preset = _cfg.resolve_workflow(wf_id)
    if preset:
        return {"id": preset["id"], "name": preset["name"], "source": "preset",
                "icon": preset["icon"], "desc": preset["desc"],
                "definition": {"stages": [{"id": "stage_" + preset["id"], "name": preset["name"],
                                            "cards": [{"id": "card_" + preset["id"], "enabled": True,
                                                       "desc": preset["desc"] or ("按预设执行：" + preset["name"]),
                                                       "worker": (preset["workers"] or ["embedded"])[0],
                                                       "model": preset["model"], "skills": []}]}]}}
    conn = db.init_db()
    try:
        uw = db.get_user_workflow(conn, wf_id)
    finally:
        conn.close()
    if not uw:
        raise HTTPException(404, "工作流不存在")
    return {"id": uw["id"], "name": uw["name"], "source": "user", "definition": uw["definition"]}


@router.post("/api/workflows")
def save_user_workflow(payload: dict):
    """保存（新建/更新）用户自建工作流。definition.stages 经白名单校验。"""
    name = str(payload.get("name") or "").strip()[:40]
    if not name:
        raise HTTPException(400, "工作流名称不能为空")
    stages = _validate_workflow_definition(payload.get("definition"))
    wf_id = str(payload.get("id") or f"wf_{os.urandom(4).hex()}")
    conn = db.init_db()
    try:
        db.upsert_user_workflow(conn, wf_id, name, {"stages": stages})
    finally:
        conn.close()
    return {"ok": True, "id": wf_id, "name": name, "stages": stages}


@router.delete("/api/workflows/{wf_id}")
def delete_user_workflow(wf_id: str):
    conn = db.init_db()
    try:
        if not db.delete_user_workflow(conn, wf_id):
            raise HTTPException(404, "工作流不存在（内置预设不可删除）")
    finally:
        conn.close()
    return {"ok": True}


@router.get("/api/skills")
def list_skills():
    """技能库：内置 + 用户自建（同名覆盖），带分类。"""
    from .. import skills as skills_mod

    conn = db.init_db()
    try:
        return skills_mod.merged_skills(conn)
    finally:
        conn.close()


@router.post("/api/skills")
def add_skill(payload: dict):
    """新增用户技能；未指定分类时按名称关键词自动分类。"""
    from .. import skills as skills_mod

    name = str(payload.get("name") or "").strip()[:30]
    if not name:
        raise HTTPException(400, "技能名称不能为空")
    category = str(payload.get("category") or "").strip()[:10]
    if not category:
        category = skills_mod.classify_skill(name)
    if category not in skills_mod.CATEGORIES:
        raise HTTPException(400, f"分类非法：{category}（允许: {skills_mod.CATEGORIES}）")
    snippet = str(payload.get("snippet") or "").strip()[:300]
    conn = db.init_db()
    try:
        # 同名用户技能已存在 → 更新
        existing = next((s for s in db.list_user_skills(conn) if s["name"] == name), None)
        s_id = existing["id"] if existing else f"sk_{os.urandom(4).hex()}"
        db.upsert_user_skill(conn, s_id, name, category, snippet)
        user_names = {s["name"] for s in db.list_user_skills(conn)}
    finally:
        conn.close()
    # 内置同名技能被用户技能覆盖
    effective_source = "user" if name in user_names else "builtin"
    return {"ok": True, "id": s_id, "name": name, "category": category,
            "snippet": snippet, "source": effective_source}


@router.delete("/api/skills/{skill_id}")
def delete_skill(skill_id: str):
    conn = db.init_db()
    try:
        if not db.delete_user_skill(conn, skill_id):
            raise HTTPException(404, "技能不存在（内置技能不可删除）")
    finally:
        conn.close()
    return {"ok": True}

