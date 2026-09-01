"""多模型路由器：按任务类型自动选最合适的 LLM。

设计要点：
- 零额外依赖（不用 LLM 来选 LLM 避免鸡生蛋；用关键词 + 长度启发）
- 配置项优先（环境变量 > 默认值），用户可关掉
- 单函数 `route(prompt, explicit=None)` 返回模型 id（deepseek:deepseek-chat 等），
  明确 None 表示"用全局默认"（向后兼容）

分类规则（保守：只把高置信度的明确信号分到专用模型）：
  - 长文本（>600 字）或含"分析/推理/对比/为什么/证明/推导"等 → reasoner
  - 含"代码/写函数/写脚本/python/实现/重构/debug/修 bug"且短文 → coder
  - 其他 → chat（默认）
  - 规则无匹配时用 prompt 长度（>200 字符用 chat 默认、否则也 chat）
"""
from __future__ import annotations

import os
import re

# 默认值可被 MAESTRO_MODEL_CHAT / MAESTRO_MODEL_CODE / MAESTRO_MODEL_REASON 覆盖
_DEFAULTS = {
    "chat": "deepseek:deepseek-chat",
    "code": "deepseek:deepseek-coder",
    "reason": "deepseek:deepseek-reasoner",
}

_CODE_HINTS = re.compile(
    r"\b(代码|写函数|写脚本|python|javascript|typescript|java\W|golang|rust|"
    r"实现|重构|debug|修\s*bug|fix\s*bug|debug|bug|class|def\s+\w+|"
    r"function\s+\w+|implement|rewrite|refactor|unit\s+test)\b",
    re.IGNORECASE,
)
_REASON_HINTS = re.compile(
    r"(分析|推理|对比|为什么|证明|推导|深入|原理|调研|research|analyze|"
    r"compare|why|prove|derive|investigate|explain\s+why)",
    re.IGNORECASE,
)

# 显式前缀强制路由（最高优先级），与 README/Markdown 工具前缀一致
_FORCE_PREFIX = re.compile(r"^\s*(reason|code|chat)[:：]\s*", re.IGNORECASE)


def _pick_explicit(prompt: str) -> str | None:
    """检查 prompt 开头的 reason:/code:/chat: 显式前缀。返回**桶名**。"""
    m = _FORCE_PREFIX.match(prompt)
    if not m:
        return None
    return m.group(1).lower()  # "reason" | "code" | "chat"


def _pick_heuristic(prompt: str) -> str:
    """关键词 + 长度启发式分类。"""
    text = prompt.strip()
    if not text:
        return "chat"
    # 1) 关键词优先
    if _REASON_HINTS.search(text):
        return "reason"
    if _CODE_HINTS.search(text):
        # 代码类里若带深度分析词（"为什么这么写 / 解释这段代码"）转 reason
        if _REASON_HINTS.search(text):
            return "reason"
        return "code"
    # 2) 长度启发：长文本默认 chat（成本低），除非有 reason 信号
    if len(text) > 1500:
        return "reason"
    return "chat"


def route(prompt: str, explicit: str | None = None) -> str:
    """选择模型。explicit 非空时直接返回（用户/工作流已指定）；否则按 prompt 路由。"""
    if explicit:
        return explicit
    chosen = _pick_explicit(prompt) or _pick_heuristic(prompt)
    return os.environ.get(f"MAESTRO_MODEL_{chosen.upper()}") or _DEFAULTS[chosen]


def is_model_auto_routed() -> bool:
    """前端用：当前是否开启自动路由（未传 model 参数时）。"""
    return not os.environ.get("MAESTRO_MODEL", "").strip()


def available_models() -> list[str]:
    """返回当前可路由到的模型清单（前置：环境变量 > 默认）。"""
    return [
        os.environ.get("MAESTRO_MODEL_REASON") or _DEFAULTS["reason"],
        os.environ.get("MAESTRO_MODEL_CODE") or _DEFAULTS["code"],
        os.environ.get("MAESTRO_MODEL_CHAT") or _DEFAULTS["chat"],
    ]
