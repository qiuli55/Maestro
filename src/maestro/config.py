"""配置加载：prompts.yaml（拆分/汇总 prompt）+ workers.json（worker 命令路径）。

不强制存在——缺失时各模块用内联默认值。配置文件优先级高于代码内联。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_PROMPTS_PATH = _ROOT / "configs" / "prompts.yaml"
_WORKERS_PATH = _ROOT / "configs" / "workers.json"
_PROVIDERS_PATH = _ROOT / "configs" / "providers.json"


def load_prompts() -> dict:
    try:
        return yaml.safe_load(_PROMPTS_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def load_providers() -> dict:
    """加载 LLM provider 配置（name -> {base_url, api_key_env, model}）。

    缺失时返回空 dict，由 llm.py 回退到内置 DeepSeek 默认。
    """
    try:
        cfg = json.loads(_PROVIDERS_PATH.read_text(encoding="utf-8"))
        return cfg.get("providers") or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def apply_worker_bins():
    """把 workers.json 里的 bin 路径灌进对应环境变量（不覆盖已设的 env）。"""
    try:
        cfg = json.loads(_WORKERS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    for name, info in (cfg.get("workers") or {}).items():
        envk = f"{name.upper()}_BIN"
        if info.get("bin") and envk not in os.environ:
            os.environ[envk] = info["bin"]


PROMPTS = load_prompts()


_KEYS_PATH = _ROOT / "configs" / "keys.json"


def load_keys() -> dict:
    """加载 Key 库（不含实际 key 值，只返回元数据）。"""
    try:
        return json.loads(_KEYS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"keys": []}


def get_available_models() -> list[dict]:
    """返回所有已配置（环境变量存在）的模型列表。"""
    keys_data = load_keys()
    available = []
    for key_entry in keys_data.get("keys") or []:
        env_var = key_entry.get("env_var", "")
        # 检查环境变量是否存在（key 已配置）
        if env_var and os.environ.get(env_var):
            for model in key_entry.get("models") or []:
                available.append(
                    {
                        "id": model["id"],
                        "label": model["label"],
                        "desc": model.get("desc", ""),
                        "provider": key_entry.get("provider", ""),
                        "key_label": key_entry.get("label", env_var),
                    }
                )
    return available


def resolve_model_to_key(model_id: str) -> str | None:
    """根据模型 ID 找到对应的环境变量名。"""
    keys_data = load_keys()
    for key_entry in keys_data.get("keys") or []:
        for model in key_entry.get("models") or []:
            if model["id"] == model_id:
                return key_entry.get("env_var")
    return None
