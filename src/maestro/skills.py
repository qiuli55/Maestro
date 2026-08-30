"""Skill 库：任务卡片可勾选的"技能"。

- 内置技能目录：configs/skills.json（静态、随仓库分发）
- 用户自建技能：SQLite user_skills 表（同名时覆盖内置）
- 自动分类：新增技能未显式指定分类时，按关键词规则归入
  设计/代码/文档/视频/音频/数据/图像/网络/通用
- 编排执行时，卡片勾选的技能 snippet 附加到该卡片的子任务指令里
"""
from __future__ import annotations

import re
import sqlite3

from . import config, db

# 分类关键词（顺序即优先级，先命中先归档）
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("设计", ("设计", "海报", "ui", "logo", "图标", "插画", "figma", "photoshop", "ps", "排版", "配色", "banner", "视觉")),
    ("代码", ("代码", "编程", "python", "javascript", "typescript", "java", "golang", "rust", "开发", "脚本", "调试",
             "重构", "api", "前端", "后端", "bug", "算法", "正则", "部署", "docker")),
    ("文档", ("word", "excel", "ppt", "文档", "报告", "docx", "xlsx", "pptx", "markdown", "周报", "总结", "合同",
             "简历", "公文", "翻译", "文案", "写作")),
    ("视频", ("视频", "剪辑", "ffmpeg", "字幕", "动画", "短视频", "vlog", "混剪", "片头", "片尾")),
    ("音频", ("音频", "配音", "语音", "tts", "转写", "播客", "音乐", "音效", "降噪")),
    ("数据", ("数据", "分析", "图表", "csv", "统计", "可视化", "bi", "报表", "趋势")),
    ("图像", ("图像", "图片", "生图", "抠图", "修图", "ocr", "压缩图片", "水印")),
    ("网络", ("搜索", "调研", "爬虫", "抓取", "资讯", "竞品", "行业")),
]

CATEGORIES: list[str] = [c for c, _ in CATEGORY_KEYWORDS] + ["通用"]


def classify_skill(name: str) -> str:
    """按名称关键词把技能归入分类；无命中返回"通用"。"""
    n = (name or "").strip().lower()
    if not n:
        return "通用"
    for cat, kws in CATEGORY_KEYWORDS:
        for kw in kws:
            if kw in n:
                return cat
    return "通用"


def builtin_skills() -> list[dict]:
    return config.load_skills()


def merged_skills(conn: sqlite3.Connection) -> list[dict]:
    """内置 + 用户技能合并；同名时用户技能覆盖内置。"""
    out: dict[str, dict] = {}
    for s in builtin_skills():
        out[s["name"]] = {"id": s["id"], "name": s["name"], "category": s["category"],
                          "snippet": s.get("snippet", ""), "source": "builtin"}
    for s in db.list_user_skills(conn):
        out[s["name"]] = {**s, "source": "user"}
    return list(out.values())


def expand_desc(desc: str, skill_names: list[str], lib: dict[str, dict] | None = None) -> str:
    """把卡片勾选的技能 snippet 附加到子任务指令尾部。

    lib 为 None 时自动拉取合并技能库；未知名跳过（不阻断任务）。
    """
    if not skill_names:
        return desc
    if lib is None:
        lib = {s["name"]: s for s in merged_skills(_conn())}
    extra = [lib[n]["snippet"] for n in skill_names if n in lib and lib[n].get("snippet")]
    if not extra:
        return desc
    return desc + "\n【技能要求】" + "；".join(extra)


def _conn():
    # expand_desc 的便捷路径：调用方通常已持有连接，这里兜底自建
    from . import db as _db

    return _db.init_db()
