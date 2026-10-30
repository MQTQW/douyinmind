"""Deterministic future-draft presentation; never authorization or source text."""
from __future__ import annotations

import re


def normalize_reply_prefix(value: str) -> str:
    if type(value) is not str or len(value) > 24:
        raise ValueError('invalid_reply_prefix')
    if not value:
        return ''
    # A short form of address only: no handles, links, instructions or newlines.
    match = re.fullmatch(r'([\u3400-\u9fffA-Za-z0-9· ]{1,16})[，,:：]?', value)
    if match is None or not match[1].strip():
        raise ValueError('invalid_reply_prefix')
    return match[1].strip() + '，'


def reply_body_budget(maximum: int, prefix: str) -> int:
    prefix = normalize_reply_prefix(prefix)
    if type(maximum) is not int or maximum <= len(prefix):
        raise ValueError('invalid_reply_style_budget')
    return maximum - len(prefix)


def style_reply_text(text: str, prefix: str, *, maximum: int) -> str:
    prefix = normalize_reply_prefix(prefix)
    if not prefix:
        result = text
    else:
        if type(text) is not str:
            raise ValueError('invalid_reply_text')
        address = re.escape(prefix[:-1])
        # Model-supplied duplicate/variant salutations never accumulate.
        body = re.sub(r'^(?:' + address + r'[，,:：]\s*)+', '', text.lstrip())
        if not body.strip():
            raise ValueError('invalid_reply_text')
        result = prefix + body
    if type(result) is not str or not result.strip() or len(result) > maximum:
        raise ValueError('reply_too_long')
    return result
