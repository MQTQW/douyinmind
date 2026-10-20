"""Conservative social-intent recognition, independent of models and storage.

A greeting followed by a content question remains a content question. Whole
utterance matching also prevents English ``hi`` from matching ``this`` or
``China``. Unknown forms deliberately fall through to the evidence-aware path.
"""
from __future__ import annotations

import re


_CHINESE_SOCIAL = (
    "你好", "您好", "嗨", "哈喽", "在吗", "在不在", "早上好", "上午好",
    "中午好", "下午好", "晚上好", "早安", "晚安", "谢谢", "谢谢你",
    "谢谢您", "多谢", "感谢", "感谢你", "感谢您", "辛苦了",
)
_ENGLISH_SOCIAL = (
    "hi", "hello", "hey", "hi there", "hello there", "thanks", "thank you",
    "thank you very much", "good morning", "good afternoon", "good evening",
    "good night",
)
_IDENTITY = (
    "你是谁", "您是谁", "请问你是谁", "请问您是谁", "你是什么助手",
    "您是什么助手", "who are you", "introduce yourself", "please introduce yourself",
)
_TONE = r"[呀啊呢哦吧哇嘛啦呐哈哟了]{0,2}"
_SEPARATORS = r"[\s，,。.!！?？；;：:～~]+"


def _alternatives(values: tuple[str, ...]) -> str:
    return "(?:" + "|".join(re.escape(value) for value in sorted(values, key=len, reverse=True)) + ")"


_CHINESE = _alternatives(_CHINESE_SOCIAL) + _TONE
_ENGLISH = _alternatives(_ENGLISH_SOCIAL)
_INTRO = (
    r"(?:请|麻烦|帮我)?(?:用(?:一|两|二|三|[123])(?:句话|句|段话|段))?"
    r"(?:简单)?(?:介绍(?:一下|下)?(?:你自己|您自己|自己)|"
    r"(?:做(?:一)?个)?自我介绍(?:一下|下)?)" + _TONE
)
_SOCIAL_UNIT = rf"(?:{_CHINESE}|{_ENGLISH}|{_alternatives(_IDENTITY)}{_TONE}|{_INTRO})"
_WHOLE_SOCIAL = re.compile(rf"{_SOCIAL_UNIT}(?:{_SEPARATORS}{_SOCIAL_UNIT})*(?:{_SEPARATORS})?", re.I)
_LEADING_SOCIAL = re.compile(rf"^(?:{_CHINESE}|{_ENGLISH}){_SEPARATORS}(?P<rest>.+)$", re.I | re.S)


def is_greeting(query: str) -> bool:
    """Return True only for complete greetings, thanks or self-introductions."""
    text = re.sub(r"\s+", " ", query).strip()
    return bool(text and _WHOLE_SOCIAL.fullmatch(text))


def strip_greeting_prefix(query: str) -> str:
    """Remove delimited social prefixes without changing the actual question.

    Pure social utterances are retained. A delimiter is required after the
    prefix, so ``你好奇`` and English words containing ``hi`` are unchanged.
    This helper is for routing/retrieval; the original user message can still
    be preserved for display and history.
    """
    text = query.strip()
    if is_greeting(text):
        return text
    while match := _LEADING_SOCIAL.match(text):
        remainder = match.group("rest").strip()
        if not remainder:
            break
        text = remainder
    return text
