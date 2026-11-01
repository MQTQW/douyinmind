"""Turn published assistant replies into short, human-looking comment text.

This is presentation only: it never grants, resends or changes a task state. It
strips bot/formal tells, markdown and lists, caps comment length, and blocks
content a real person would not post (links, contact handles, bot self-claims).
"""
from __future__ import annotations

import re

MAX_CHARS = {"answer": 150, "import_status": 90, "clarification": 150}
_LINKS = re.compile(r"https?://\S+|www\.\S+|\bv\.douyin\.com\S*", re.I)
_CONTACT = re.compile(r"微信|vx|v信|qq|QQ|公众号|加我|私聊|电话|手机号|contact|wechat", re.I)
_BOT_CLAIM = re.compile(r"我是\s*(?:AI|人工智能|机器人|语言模型)|作为\s*(?:AI|人工智能|助手)|自动回复|系统提示|DouyinMind", re.I)
_MD_HEADER = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_MD_BULLET = re.compile(r"(?m)^\s*(?:[-*+•]|\d{1,2}[.、)])\s+")
_MD_INLINE = re.compile(r"[`*_>#]+")
_ENUM = re.compile(r"\b\d{1,2}[.、)]\s*")
_FORMAL_LEAD = re.compile(r"^(?:根据(?:你提供的|已入库的)?资料[，,]?(?:显示|来看|可以知道|可知)?[，,:：]?|"
                          r"(?:首先|其次|再次|最后|另外|此外)[，,]?|综上(?:所述)?[，,]?|总之[，,]?|总的来说[，,]?)+")
_FORMAL_TAIL = re.compile(r"[。！!]?\s*(?:希望(?:对你|能)有(?:所)?帮助|如需(?:要)?更多(?:信息|内容)?[，,]?[^。]*|"
                          r"如果你(?:还)?想[^。]*|欢迎[^。]*)[。！!]?\s*$")
_FILLER = re.compile(r"[ \t\u3000]+")
_SENTENCE = re.compile(r"[^。！？!?]+[。！？!?]?")

_REWRITES = (
    (re.compile(r"^这个视频已加入[“\"「]?(.+?)[”\"」]?知识库，?文字稿和检索资料已就绪，?现在可以根据它提问。?$"),
     r"存好啦，在「\1」里，想问啥直接问我就行。"),
    (re.compile(r"^这个视频的入库任务仍在处理中，?尚未确认完成。?.*$"),
     "还在入库，等会儿好了我再说一声。"),
    (re.compile(r"^这次视频入库没有完成，?失败记录已保留.*$"),
     "这次没存成，记录我留着了，晚点再试一次吧。"),
    (re.compile(r"^这次入库提交结果暂未确认，?记录已保留.*$"),
     "刚才没确认成功，我先不重复提交，晚点再看看。"),
    (re.compile(r"^还没有确认提交这个视频的入库任务.*$"),
     "还没定存到哪个库，跟我说下库名就行。"),
)


def _blocks(text):
    if _LINKS.search(text):
        return "reply_content_link"
    if _CONTACT.search(text):
        return "reply_content_contact"
    if _BOT_CLAIM.search(text):
        return "reply_content_bot_claim"
    return None


def _tidy(text):
    text = _MD_HEADER.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_INLINE.sub("", text)
    text = _ENUM.sub("", text)
    text = _FILLER.sub(" ", text).strip()
    return text


def _cap(text, maximum):
    """Keep whole sentences up to the comment-sized budget."""
    if len(text) <= maximum:
        return text
    kept = ""
    for sentence in _SENTENCE.findall(text):
        candidate = (kept + sentence).strip()
        if len(candidate) > maximum:
            break
        kept = candidate
    if kept:
        return kept.rstrip("，,；;：: ") + ("…" if not kept.endswith(("。", "！", "？", "!", "?")) else "")
    return text[: maximum - 1].rstrip("，,；;：: ") + "…"


def humanize_reply(text, kind="answer", *, maximum=None):
    """Return {'allowed', 'text', 'reason'}; blocking never silently posts."""
    if not isinstance(text, str) or not text.strip():
        return {"allowed": False, "text": "", "reason": "reply_content_empty"}
    try:
        blocked = _blocks(text)
        if blocked:
            return {"allowed": False, "text": "", "reason": blocked}
        value = _tidy(text)
        for pattern, replacement in _REWRITES:
            if pattern.match(value):
                value = pattern.sub(replacement, value)
                break
        value = _FORMAL_LEAD.sub("", value)
        # Formal transitions anywhere in the body make it read like a report.
        value = re.sub(r"(?:综上所述|总而言之|总之|总的来说|首先|其次|再次|最后)[，,]?", "", value)
        value = _FORMAL_TAIL.sub("", value)
        value = re.sub(r"\s*([，,。！？!?])\s*", r"\1", value)
        value = value.strip(" ，,；;：:")
        if not value:
            return {"allowed": False, "text": "", "reason": "reply_content_empty"}
        limit = maximum or MAX_CHARS.get(kind, 120)
        if type(limit) is not int or limit < 20:
            raise ValueError("invalid_reply_budget")
        value = _cap(value, limit)
        if not value:
            return {"allowed": False, "text": "", "reason": "reply_content_empty"}
        return {"allowed": True, "text": value, "reason": None}
    except Exception:
        return {"allowed": False, "text": "", "reason": "reply_content_invalid"}
