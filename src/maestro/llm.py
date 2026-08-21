"""LLM 封装：编排器大脑（拆分 / 汇总 / 内嵌 worker）统一调用入口。

安全：API Key 仅从环境变量读取，绝不写入代码或日志。

多 provider：model 参数支持 "provider:model" 语法（如 "kimi:kimi-k2.6"），
不带前缀走默认 DeepSeek。provider 定义见 configs/providers.json。
"""

import json
import os
import time

from openai import OpenAI

from . import config

_DEFAULT_PROVIDER = "deepseek"


def _resolve(model: str | None) -> tuple[str, str]:
    """把 model 解析为 (provider, model_name)。支持 "provider:model"。

    - "kimi:kimi-k2.6" -> ("kimi", "kimi-k2.6")
    - "deepseek-chat" / None -> ("deepseek", "deepseek-chat" 或空由调用方补默认)
    """
    if model and ":" in model:
        provider, _, m = model.partition(":")
        if m:
            return provider, m
    return _DEFAULT_PROVIDER, (model or "")


def get_client(provider: str = _DEFAULT_PROVIDER) -> tuple[OpenAI, str]:
    """按 provider 返回 (client, 默认 model)。

    provider 配置来自 configs/providers.json；key 一律走环境变量（api_key_env 指定的变量名）。
    DeepSeek 缺配置时回退到环境变量 + 内置默认（向后兼容）。
    """
    providers = config.load_providers()
    info = providers.get(provider)

    if info:
        base_url = info.get("base_url")
        key_env = info.get("api_key_env") or f"{provider.upper()}_API_KEY"
        default_model = info.get("model") or os.environ.get("MAESTRO_MODEL", "deepseek-chat")
    elif provider == _DEFAULT_PROVIDER:
        # 无配置文件时向后兼容：环境变量优先，其次内置默认
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        key_env = "DEEPSEEK_API_KEY"
        default_model = os.environ.get("MAESTRO_MODEL", "deepseek-chat")
    else:
        raise RuntimeError(f"未配置 provider '{provider}'（见 configs/providers.json）")

    if not base_url:
        raise RuntimeError(f"provider '{provider}' 缺少 base_url（见 configs/providers.json）")

    key = os.environ.get(key_env)
    if not key:
        raise RuntimeError(f"缺少 {key_env} 环境变量（provider={provider}）")
    return OpenAI(api_key=key, base_url=base_url), default_model


def complete(
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = 0.2,
    max_retries: int = 1,
) -> str:
    """一次 LLM 调用，失败自动重试 max_retries 次（默认 1 次）。"""
    provider, model_name = _resolve(model)
    client, default_model = get_client(provider)
    model_name = model_name or default_model
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001 — 网络/限流统一重试
            last_err = e
            if attempt < max_retries:
                # 指数退避 + jitter：1s, 2s, 4s, 8s...（封顶 30s），
                # + 0~1s 随机抖动避免雪崩。
                import random

                backoff = min(30, 2**attempt) + random.random()
                time.sleep(backoff)
    raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）: {last_err}")


def complete_json(system: str, user: str, model: str | None = None) -> dict | list:
    """要求模型返回 JSON，解析并容错（剥离 ```json 围栏）。"""
    raw = complete(system, user, model=model, temperature=0.1)
    return _extract_json(raw)


def complete_with_tools(
    system: str,
    user: str,
    tools: list[dict],
    tool_executor,
    model: str | None = None,
    max_rounds: int = 3,
) -> str:
    """带工具的多轮往返（有限轮，非完整 agent 循环）。

    tools: OpenAI 函数格式 [{type:"function", function:{name, description, parameters}}]
    tool_executor: callable(name, args: dict) -> str，执行工具并返回结果文本。
    流程：调 LLM -> 若返回 tool_calls 则执行 -> 把结果作为 tool 消息喂回 -> 再调 LLM。
    最多 max_rounds 轮工具调用（默认 3，有限轮防失控；技术方案 §133 不内置完整 agent 循环）。
    """
    provider, model_name = _resolve(model)
    client, default_model = get_client(provider)
    model_name = model_name or default_model
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    for _ in range(max_rounds):
        resp = client.chat.completions.create(model=model_name, messages=msgs, tools=tools, temperature=0.2)
        msg = resp.choices[0].message
        if not getattr(msg, "tool_calls", None):
            return msg.content or ""

        # 执行工具并把结果喂回
        msgs.append(msg)  # 模型带 tool_calls 的 assistant 消息
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = tool_executor(tc.function.name, args)
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )
    # 达到轮数上限仍未给出最终文本：日志警告 + 返回空串。
    # 旧实现是返回最后一次 tool result（语义错误：tool 输出不是 assistant 回答），
    # 现在改返回空串，让调用方走错误"处理而非"得到半成品答案"路径。
    import warnings

    warnings.warn(
        f"LLM 在 {max_rounds} 轮工具调用后仍没给最终文本（msgs={len(msgs)}）",
        RuntimeWarning,
        stacklevel=2,
    )
    return ""


def _extract_json(raw: str) -> dict | list:
    raw = raw.strip()
    # 剥离可能的 ```json ... ``` 围栏
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 退化：截取第一个 [ 或 { 到最后一个 ] 或 }
        start = min([i for i in (raw.find("{"), raw.find("[")) if i >= 0], default=-1)
        end = max(raw.rfind("}"), raw.rfind("]"))
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise
