"""llm 模块测试：完整工具调用循环 + max_rounds 兜底语义。"""
from __future__ import annotations

import sys
import warnings

sys.path.insert(0, "src")

from unittest.mock import MagicMock, patch

import pytest

from maestro import llm


# ============================================================================
# complete_with_tools 兜底行为（v2 修正）
# ============================================================================

def test_max_rounds_exhausted_returns_empty_string_with_warning():
    """max_rounds 耗尽：返回空串 + RuntimeWarning（不再返回最后一次 tool 文本）。"""
    # 构造一个 mock client：每轮都返回 tool_call，永远不返回最终文本
    mock_tool_call = MagicMock()
    mock_tool_call.id = "tc1"
    mock_tool_call.function.name = "noop"
    mock_tool_call.function.arguments = "{}"

    mock_msg = MagicMock()
    mock_msg.tool_calls = [mock_tool_call]
    mock_msg.content = None

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message = mock_msg

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("maestro.llm.get_client", return_value=(mock_client, "mock-model")):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = llm.complete_with_tools(
                system="sys", user="user", tools=[{"name": "noop"}],
                tool_executor=lambda name, args: "tool output",
                max_rounds=2,  # 2 轮 = 2 次 LLM 调用
            )

    # 必须返回空串（不再返回 "tool output"）
    assert result == ""
    # 必须 warn
    assert any(issubclass(x.category, RuntimeWarning) for x in w), \
        "max_rounds 耗尽应触发 RuntimeWarning"


def test_max_rounds_default_is_three():
    """max_rounds 默认是 3（不是 4，避免 off-by-one）。"""
    import inspect
    sig = inspect.signature(llm.complete_with_tools)
    assert sig.parameters["max_rounds"].default == 3


def test_no_tool_calls_returns_immediately():
    """LLM 第一轮就返回文本（无 tool_call）：返回内容，不消耗更多轮数。"""
    mock_msg = MagicMock()
    mock_msg.tool_calls = None
    mock_msg.content = "直接回答"

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message = mock_msg

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("maestro.llm.get_client", return_value=(mock_client, "m")):
        result = llm.complete_with_tools(
            system="s", user="u", tools=[],
            tool_executor=lambda name, args: "should not be called",
        )

    assert result == "直接回答"
    assert mock_client.chat.completions.create.call_count == 1  # 一次就够


def test_tool_call_then_text_returns_text():
    """第一轮 tool_call → 第二轮返回文本：返回第二轮文本，不走兜底。"""
    # 第一轮：tool_call
    tool_call = MagicMock()
    tool_call.id = "tc1"
    tool_call.function.name = "noop"
    tool_call.function.arguments = "{}"
    msg1 = MagicMock()
    msg1.tool_calls = [tool_call]
    msg1.content = None

    # 第二轮：纯文本
    msg2 = MagicMock()
    msg2.tool_calls = None
    msg2.content = "最终答案"

    response1 = MagicMock()
    response1.choices = [MagicMock()]
    response1.choices[0].message = msg1

    response2 = MagicMock()
    response2.choices = [MagicMock()]
    response2.choices[0].message = msg2

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [response1, response2]

    tool_calls_made = []

    def fake_executor(name, args):
        tool_calls_made.append(name)
        return "ok"

    with patch("maestro.llm.get_client", return_value=(mock_client, "m")):
        result = llm.complete_with_tools(
            system="s", user="u", tools=[{"name": "noop"}],
            tool_executor=fake_executor, max_rounds=3,
        )

    assert result == "最终答案"
    assert tool_calls_made == ["noop"]
    assert mock_client.chat.completions.create.call_count == 2


def test_tool_call_invalid_json_args_falls_back_to_empty():
    """tool_call 的 arguments 是无效 JSON：args={}（不抛错）。"""
    tool_call = MagicMock()
    tool_call.id = "tc1"
    tool_call.function.name = "noop"
    tool_call.function.arguments = "{ invalid json }"  # 非法 JSON

    msg1 = MagicMock()
    msg1.tool_calls = [tool_call]
    msg1.content = None

    msg2 = MagicMock()
    msg2.tool_calls = None
    msg2.content = "ok"

    response1 = MagicMock()
    response1.choices = [MagicMock()]
    response1.choices[0].message = msg1
    response2 = MagicMock()
    response2.choices = [MagicMock()]
    response2.choices[0].message = msg2

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [response1, response2]

    executor_args = []
    def fake_executor(name, args):
        executor_args.append((name, args))
        return "ok"

    with patch("maestro.llm.get_client", return_value=(mock_client, "m")):
        llm.complete_with_tools(
            system="s", user="u", tools=[{"name": "noop"}],
            tool_executor=fake_executor, max_rounds=3,
        )

    # 非法 JSON → fallback 到空 dict（不抛错）
    assert executor_args == [("noop", {})]