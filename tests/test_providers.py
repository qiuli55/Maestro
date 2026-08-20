"""多 provider 测试：model 解析、provider 配置加载、默认回退。

不依赖真实 API key，用 monkeypatch 控制环境变量。
"""
import sys

sys.path.insert(0, "src")

import pytest

from maestro import llm
from maestro.llm import _resolve, get_client, _DEFAULT_PROVIDER


def test_resolve_plain_model_goes_default():
    assert _resolve("deepseek-chat") == (_DEFAULT_PROVIDER, "deepseek-chat")


def test_resolve_none_goes_default_empty():
    assert _resolve(None) == (_DEFAULT_PROVIDER, "")


def test_resolve_provider_model():
    assert _resolve("kimi:kimi-k2.6") == ("kimi", "kimi-k2.6")


def test_resolve_provider_only_falls_back_default():
    """只有 provider 没有模型（如 "kimi:"）当默认处理。"""
    assert _resolve("kimi:") == (_DEFAULT_PROVIDER, "kimi:")


def test_get_client_kimi_uses_config(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "test-key")
    client, default_model = get_client("kimi")
    assert default_model == "kimi-k2.6"
    assert str(client.base_url).rstrip("/") == "https://api.moonshot.cn/v1"


def test_get_client_unknown_provider_raises(monkeypatch):
    with pytest.raises(RuntimeError, match="未配置 provider"):
        get_client("nonexistent")


def test_get_client_default_without_config(monkeypatch):
    """无配置文件时回退到环境变量 + 内置默认。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setattr("maestro.llm.config", __import__("types").SimpleNamespace(
        load_providers=lambda: {}
    ))
    client, default_model = get_client("deepseek")
    assert default_model == "deepseek-chat"


def test_get_client_missing_key_raises(monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="缺少 KIMI_API_KEY"):
        get_client("kimi")
