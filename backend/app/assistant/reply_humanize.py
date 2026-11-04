"""Turn published assistant replies into short, human-looking comment text.

This is presentation only: it never grants, resends or changes a task state. It
strips bot/formal tells, markdown and lists, caps comment length, and blocks
content a real person would not post (links, contact handles, bot self-claims).
"""
from __future__ import annotations

import re

from .reply_style import normalize_reply_prefix, style_reply_text

MAX_CHARS = {"answer": 150, "import_status": 90, "clarification": 150}
# A library menu has a separately frozen, structured body. Its existing budget
# includes the salutation and must not be shortened after its hash is recorded.
CHOICE_MAX_CHARS = 280
_LINKS = re.compile(r"https?://\S+|www\.\S+|\bv\.douyin\.com\S*", re.I)
_CONTACT = re.compile(r"微信|vx|v信|qq|QQ|公众号|加我|私聊|电话|手机号|contact|wechat", re.I)
_BOT_CLAIM = re.compile(r"我是\s*(?:AI|人工智能|机器人|语言模型)|作为\s*(?:AI|人工智能|助手)|自动回复|系统提示|DouyinMind", re.I)
_MD_HEADER = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+")
_MD_BULLET = re.compile(r"(?m)^[ \t]*(?:[-*+•]|\d{1,3}[.、)])[ \t]+")
_MD_QUOTE = re.compile(r"(?m)^[ \t]{0,3}>[ \t]+")
_MD_EMPHASIS = re.compile(r"(?<!\w)(\*\*|__|\*|_)(?=\S)([^\n]*?\S)\1(?!\w)")
_PROTECTED = re.compile(
    r'(?P<ticks>`+)[\s\S]*?(?P=ticks)|“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"\n]*"|\'[^\'\n]*\'')
_FORMAL_LEAD = re.compile(
    r"^根据(?:你提供的|已入库的)?资料(?:显示|来看|可以知道|可知)?(?:[，,:：][ \t]*|[ \t]*\n[ \t]*|$)")
_FORMAL_TRANSITION = re.compile(
    r"(?m)(^|[。！？!?\n])[ \t]*(?:综上所述|总而言之|总之|总的来说|首先|其次|再次|最后|另外|此外)[，,:：][ \t]*")
_FORMAL_TAIL = re.compile(r"(?:希望(?:对你|能)有(?:所)?帮助|希望能帮到你)[。！! \t]*$")
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


def validate_frozen_reply(text, kind="answer", *, maximum=None):
    """Check an immutable public body without rewriting names or ordinals."""
    if not isinstance(text, str) or not text.strip():
        return {"allowed": False, "text": "", "reason": "reply_content_empty"}
    blocked = _blocks(text)
    if blocked:
        return {"allowed": False, "text": "", "reason": blocked}
    limit = MAX_CHARS.get(kind, 120) if maximum is None else maximum
    if type(limit) is not int or limit < 20:
        return {"allowed": False, "text": "", "reason": "reply_content_invalid"}
    if len(text) > limit:
        return {"allowed": False, "text": "", "reason": "reply_content_too_long"}
    return {"allowed": True, "text": text, "reason": None}


def _tidy(text):
    text = _MD_HEADER.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_QUOTE.sub("", text)
    # Only paired formatting delimiters at word boundaries are presentation.
    # Identifiers such as Git_notes, C# and decimal/version numbers are data.
    text = _MD_EMPHASIS.sub(r"\2", text)
    text = _FILLER.sub(" ", text).strip()
    return text


def _answer_body(text, *, legacy_rewrites=False):
    if legacy_rewrites:
        for pattern, replacement in _REWRITES:
            if pattern.match(text):
                text = pattern.sub(replacement, text)
                break
    # Protect the entire paired quote/code span, including its whitespace and
    # punctuation. A marker absent from the input cannot collide with user text.
    marker = "\ue000"
    while marker in text:
        marker += "\ue000"
    protected = []

    def protect(match):
        token = marker + str(len(protected)) + "\ue001"
        protected.append((token, match[0]))
        return token

    value = _PROTECTED.sub(protect, text)
    # Reach a fixed point before restoration: owner/server revalidation must
    # never reveal another formatting layer and change a candidate's body/hash.
    previous = None
    while value != previous:
        previous = value
        value = _tidy(value)
        value = _FORMAL_LEAD.sub("", value)
        value = _FORMAL_TRANSITION.sub(r"\1", value)
        value = _FORMAL_TAIL.sub("", value)
        value = re.sub(r"\s*([，,。！？!?])\s*", r"\1", value)
        value = value.strip(" \t\r\n，,；;：:")
    for token, original in protected:
        value = value.replace(token, original)
    return value


def prepare_public_body(text, kind="answer", *, prefix="", maximum=320):
    """Prepare one new body before evidence validation and immutable storage.

    ``maximum`` includes the normalized, frozen prefix. Answers may have safe
    presentation formatting removed; program-generated statuses/clarifications
    retain their text. Nothing is truncated. The return value excludes prefix.
    Callers must validate quotes, source references and claims on this final
    body before storing it; publication must only validate the frozen text.
    """
    try:
        if (not isinstance(text, str) or not text.strip()
                or kind not in {"answer", "clarification", "import_status"}
                or type(maximum) is not int or maximum < 20 or _blocks(text)):
            raise ValueError()
        prefix = normalize_reply_prefix(prefix)
        def without_prefix(value):
            if not prefix:
                return value
            address = re.escape(prefix[:-1])
            return re.sub(r"^(?:" + address + r"[，,:：]\s*)+", "", value.lstrip())

        body = without_prefix(text)
        if kind == "answer":
            previous = None
            while body != previous:
                previous = body
                body = without_prefix(_answer_body(body))
        styled = style_reply_text(body, prefix, maximum=maximum)
        if validate_frozen_reply(styled, kind, maximum=maximum)["allowed"] is not True:
            raise ValueError()
        # style_reply_text also removes a salutation exposed by formatting
        # cleanup, so its exact suffix is the body that Store must freeze.
        return styled[len(prefix):] if prefix else styled
    except (ValueError, TypeError, KeyError):
        raise ValueError("invalid_public_reply") from None


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
        value = _answer_body(text, legacy_rewrites=True)
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
