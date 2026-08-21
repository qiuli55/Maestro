"""minimax worker 意图识别测试。

之前的实现用 prompt 子串匹配（"图片" in prompt），导致"我喜欢图片"被误判为图像生成。
v2 改成显式前缀（mmx:img:/mmx:vid: 等）+ 短指令前缀（生成图/生成视频 等）。"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

import pytest

from maestro.workers import minimax


def _make_worker():
    """构造一个 minimax worker，monkey patch 所有内部方法避免真调用 mmx CLI。"""
    w = minimax.MiniMaxWorker()
    w._generate_image = MagicMock(return_value="IMG_RESULT")
    w._generate_video = MagicMock(return_value="VID_RESULT")
    w._generate_speech = MagicMock(return_value="SPK_RESULT")
    w._generate_music = MagicMock(return_value="MUS_RESULT")
    w._text_chat = MagicMock(return_value="TXT_RESULT")
    return w


@pytest.fixture(autouse=True)
def _skip_if_no_key(monkeypatch):
    """没有 MMX_API_KEY 时早期返回——monkey patch 让其继续。"""
    monkeypatch.setattr(minimax, "MMX_API_KEY", "sk-fake-for-test")


# ============================================================================
# 显式前缀（推荐用法）
# ============================================================================

@pytest.mark.parametrize("prefix", [
    "mmx:img ", "mmx:image ", "[img] ", "[image] ",
    "MMX:IMG ", "MMX:IMAGE ",
])
def test_explicit_prefix_image(prefix):
    w = _make_worker()
    w.spawn(f"{prefix}a cat", workdir="/tmp", timeout=10)
    w._generate_image.assert_called_once()
    w._generate_video.assert_not_called()
    w._text_chat.assert_not_called()


@pytest.mark.parametrize("prefix", [
    "mmx:vid ", "mmx:video ", "[vid] ", "[video] ",
])
def test_explicit_prefix_video(prefix):
    w = _make_worker()
    w.spawn(f"{prefix}ocean wave", workdir="/tmp", timeout=10)
    w._generate_video.assert_called_once()


@pytest.mark.parametrize("prefix", [
    "mmx:spk ", "mmx:speech ", "[spk] ", "[speech] ",
])
def test_explicit_prefix_speech(prefix):
    w = _make_worker()
    w.spawn(f"{prefix}hello world", workdir="/tmp", timeout=10)
    w._generate_speech.assert_called_once()


@pytest.mark.parametrize("prefix", [
    "mmx:mus ", "mmx:music ", "[mus] ", "[music] ",
])
def test_explicit_prefix_music(prefix):
    w = _make_worker()
    w.spawn(f"{prefix}ambient", workdir="/tmp", timeout=10)
    w._generate_music.assert_called_once()


# ============================================================================
# 短指令前缀（向后兼容）
# ============================================================================

@pytest.mark.parametrize("short", [
    "生成图 a cat",
    "生成图片 一只猫",
    "画一只猫",
    "画个动物",
    "generate image of a cat",
    "draw a dog",
    "create image: sunset",
])
def test_short_prompt_image_intent(short):
    w = _make_worker()
    w.spawn(short, workdir="/tmp", timeout=10)
    w._generate_image.assert_called_once()


@pytest.mark.parametrize("short", [
    "生成视频 海浪",
    "做个视频",
    "generate video: ocean",
    "make video of cat",
])
def test_short_prompt_video_intent(short):
    w = _make_worker()
    w.spawn(short, workdir="/tmp", timeout=10)
    w._generate_video.assert_called_once()


@pytest.mark.parametrize("short", [
    "生成语音 你好",
    "配音 你好",
    "朗读 Hello",
    "generate speech hello",
    "tts 你好",
])
def test_short_prompt_speech_intent(short):
    w = _make_worker()
    w.spawn(short, workdir="/tmp", timeout=10)
    w._generate_speech.assert_called_once()


@pytest.mark.parametrize("short", [
    "生成音乐 ambient",
    "作曲 a happy tune",
    "generate music upbeat",
    "compose ambient",
])
def test_short_prompt_music_intent(short):
    w = _make_worker()
    w.spawn(short, workdir="/tmp", timeout=10)
    w._generate_music.assert_called_once()


# ============================================================================
# 关键反例（不应误判）
# ============================================================================

def test_chatty_sentence_with_image_word_falls_back_to_text():
    """'我喜欢图片' 这种聊天型 prompt（含'图片'但不是生成指令）→ 走 text_chat。"""
    w = _make_worker()
    long_chat = "我很喜欢图片，但今天不想生成新的，只想聊聊图片收藏的话题"
    w.spawn(long_chat, workdir="/tmp", timeout=10)
    w._text_chat.assert_called_once()
    w._generate_image.assert_not_called()


def test_long_prompt_with_image_word_falls_back_to_text():
    """长 prompt（含'图片'但不以生成动词开头）→ 走 text_chat。"""
    w = _make_worker()
    long_prompt = (
        "我想写一个故事，主角是个摄影爱好者，他每天都会给一张图片加上标题。"
        "故事开始于他发现了一张会动的图片，于是开始调查背后的真相。这个故事"
        "需要跨越 80 年代到现代的时间线，主角从胶片时代成长到数字时代"
    )
    assert len(long_prompt) > 60, f"长度不够：{len(long_prompt)}"
    w.spawn(long_prompt, workdir="/tmp", timeout=10)
    w._text_chat.assert_called_once()
    w._generate_image.assert_not_called()


def test_pure_text_prompt_falls_back_to_text():
    """纯文字任务 → text_chat。"""
    w = _make_worker()
    w.spawn("帮我写一个 Python 函数计算斐波那契", workdir="/tmp", timeout=10)
    w._text_chat.assert_called_once()


def test_no_match_short_prompt_falls_back_to_text():
    """短但无明确意图词 → 走 text_chat（保守）。"""
    w = _make_worker()
    w.spawn("你好", workdir="/tmp", timeout=10)
    w._text_chat.assert_called_once()


def test_no_match_long_prompt_falls_back_to_text():
    """无匹配的长 prompt → text_chat。"""
    w = _make_worker()
    w.spawn("用 Rust 写一个简单的 TCP 服务器", workdir="/tmp", timeout=10)
    w._text_chat.assert_called_once()