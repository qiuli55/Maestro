"""任务拆分：防幻觉的上半场。

拓扑（技术方案 §3.3 YAGNI：P1 仅串行/并行，不做 DAG）：
- 场景 A（多需求逐个喂）：输入已是一行一需求，逐行 => 独立子任务（串行）
- 场景 B（长内容分析）：LLM 按语义拆成若干段，各子任务读一段（并行）
- 场景 C（AI 智能拆分）：LLM 理解项目级需求拆成结构化实施步骤
- 场景 auto：LLM 先识别输入类型再路由到 A/B/C
"""
from __future__ import annotations

import re

from . import llm, config

_SPLIT_SYSTEM = config.PROMPTS.get("split_system") or """你是一个拆分器。把用户的长文本按语义/结构切成若干独立片段。
只做拆分，不要总结、不要回答内容。
输出严格 JSON：{"segments":[{"id":"s1","title":"片段标题","text":"该片段原文（可截断过长原文，但保留关键信息）"}]}
片段数 3-5 个，每段自包含、互不重叠。"""

_DETECT_SYSTEM = config.PROMPTS.get("detect_system") or """你是任务路由。判断用户输入属于哪一类，输出严格 JSON：
{"scenario":"a|b|c","reason":"一句话理由"}
- a=多个并列需求（逐条独立执行、无需汇总，如"加登录、加支付、加报表"）
- b=多段/多文档长文本需合并成统一报告（含章节、段落、多个来源，如年度复盘、多份资料）
- c=单个项目级任务需拆成结构化实施步骤（如"给这个项目加三个功能"、"实现一个订单系统"）
规则：输入含多个段落/章节标题 → b；输入是逐行/并列的短需求 → a；其余较复杂的单一任务 → c。"""


def detect_scenario(task_prompt: str, model: str | None = None) -> tuple[str, str]:
    """场景自动识别：LLM 判断输入该走 a/b/c，返回 (scenario, reason)。"""
    data = llm.complete_json(_DETECT_SYSTEM, task_prompt, model=model)
    s = (data.get("scenario") if isinstance(data, dict) else "") or "a"
    if s not in ("a", "b", "c"):
        s = "a"
    reason = (data.get("reason") if isinstance(data, dict) else "") or ""
    return s, reason


def split_requirements(task_prompt: str, worker_type: str) -> list[dict]:
    """场景 A：逐行喂，每行一个独立子任务（串行队列）。"""
    lines = [ln.strip() for ln in task_prompt.splitlines() if ln.strip()]
    return [
        {"id": f"st_{i+1}", "desc": ln, "worker_type": worker_type, "source_segments": None}
        for i, ln in enumerate(lines)
    ]


def split_document(task_prompt: str, max_segments: int = 5) -> list[dict]:
    """场景 B：LLM 按语义拆段，每段一个子任务（并行读取）。"""
    data = llm.complete_json(_SPLIT_SYSTEM, task_prompt)
    segs = data.get("segments", []) if isinstance(data, dict) else []
    subtasks = []
    for i, seg in enumerate(segs[:max_segments]):
        seg_id = seg.get("id") or f"s{i+1}"
        subtasks.append({
            "id": f"st_{i+1}",
            "desc": f'阅读并分析片段【{seg.get("title", seg_id)}】：\n{seg.get("text", "")}',
            "worker_type": "embedded",  # 读段分析用内嵌 LLM 即可
            "source_segments": seg_id,
        })
    return subtasks


_PLAN_SYSTEM = config.PROMPTS.get("plan_system") or """你是一个智能任务编排器。面对用户的任意需求，先理解其意图，再把它拆成若干个可独立执行的子任务。
只做拆分，不执行、不回答内容。
输出严格 JSON：{"tasks":[{"id":"t1","desc":"子任务的清晰行动目标（一句话，含关键约束/预期产出形式）","goal":"该子任务预期的产物"}]}
子任务 2-6 个，粒度适中；每个自包含、彼此尽量独立；若明显有先后依赖仍按逻辑顺序列出。"""


def split_plan(task_prompt: str, model: str | None = None) -> list[dict]:
    """场景 C：AI 智能拆分——用 LLM 理解需求，拆成结构化子任务（非按行）。"""
    data = llm.complete_json(_PLAN_SYSTEM, task_prompt, model=model)
    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    out = []
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            continue
        desc = (t.get("desc") or "").strip()
        if not desc:
            continue
        out.append({
            "id": re.sub(r"\W+", "_", str(t.get("id") or f"st_{len(out)+1}")).strip("_"),
            "desc": desc,
            "worker_type": "embedded",  # 编排器统一按用户选择覆盖
            "source_segments": None,
            "goal": (t.get("goal") or "").strip(),
        })
        if len(out) >= 8:  # 硬性上限：最多 8 个有效子任务
            break
    return out


def validate(subtasks: list[dict], allowed_workers: list[str]) -> list[dict]:
    """强制结构校验：缺字段 / 非法 worker_type 直接抛错（防脏数据进编排器）。"""
    cleaned = []
    for i, st in enumerate(subtasks):
        if not isinstance(st, dict) or not st.get("desc"):
            raise ValueError(f"子任务 #{i} 缺 desc")
        wid = st.get("id") or f"st_{i+1}"
        wt = st.get("worker_type") or "embedded"
        if wt not in allowed_workers:
            raise ValueError(f"子任务 {wid} 的 worker_type 非法: {wt}（允许: {allowed_workers}）")
        cleaned.append({
            "id": re.sub(r"\W+", "_", str(wid)).strip("_"),
            "desc": st["desc"],
            "worker_type": wt,
            "source_segments": st.get("source_segments"),
        })
    if not cleaned:
        raise ValueError("拆分结果为空")
    return cleaned
