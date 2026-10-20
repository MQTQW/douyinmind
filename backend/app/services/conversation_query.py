"""Add bounded user-question context to explicitly dependent retrieval queries.

This copies existing questions as search clues; it does not rewrite a question,
infer an entity, or make an answer. The caller must provide user messages only,
ordered oldest to newest, and must still enforce the current library scope.
"""
from __future__ import annotations

import re

from app.services.query_intent import is_greeting, strip_greeting_prefix


_NEW_TOPIC = re.compile(
    r"换(?:个|一个)?话题|换个问题|另(?:一个|个)话题|新问题|重新问(?:一个)?|"
    r"不(?:要|用|需|必)参考(?:历史|前文)|无需(?:参考|结合)(?:历史|前文)|独立问题|单独回答|"
    r"\b(?:new topic|new question|change (?:the )?subject|ignore (?:the )?history)\b",
    re.I,
)
_REFERENCE = re.compile(
    r"它(?:们)?|(?:这|那)\s*(?:(?:[几两一二三四五六七八九十百千万\d]+\s*)?"
    r"(?:个|项|种|条|步|点|句)|些|样)|刚才|刚刚|(?:前|后)者|"
    r"第[一二三四五六七八九十百两\d]+(?:条|点|步|个|种)|"
    r"你(?:刚才)?(?:说|提到|讲)的|上述|上面(?:的|提到)|"
    r"\b(?:it|its|them|their|this|that|these|those|former|latter)\b",
    re.I,
)
_SHORT_FOLLOWUP = re.compile(
    r"(?:请|麻烦|能不能|可以|能)?(?:"
    r"继续(?:解释|说|讲|讲讲)?|接着(?:说|讲|解释)|"
    r"再(?:具体|详细)(?:说说|讲讲|解释(?:一下)?)|再展开(?:说说|讲讲)|"
    r"(?:详细|具体)(?:说说|讲讲|解释一下)|展开讲讲|"
    r"还有(?:呢|吗|其他原因吗|什么原因|哪些原因|哪些方法)?|"
    r"然后呢|下一步呢|为什么会这样|具体是什么意思|再说说原因|"
    r"再举(?:一个|一|个)?例子"
    r")(?:吗|呢|吧)?|continue|go on|tell me more|explain further|more details(?: please)?",
    re.I,
)
_TRAILING_PUNCTUATION = re.compile(r"[\s，,。.!！?？；;：:～~]+$")
_HISTORY_HEADER = "\n\n前文提问（仅用于理解指代，不作为事实依据）：\n"
_MAX_PRIOR_QUESTIONS = 3


def _content_query(query: str) -> str:
    return _TRAILING_PUNCTUATION.sub("", strip_greeting_prefix(query)).strip()


def _is_short_followup(query: str) -> bool:
    return bool(_SHORT_FOLLOWUP.fullmatch(_content_query(query)))


def needs_followup_context(query: str) -> bool:
    """Recognize explicit references, not every question beginning with 那.

    Complete independent questions and explicit topic changes stay unchanged.
    Ambiguous references are not resolved here; this only requests old user
    questions for retrieval. A caller need not read history for other queries.
    """
    text = _content_query(query)
    if not text or is_greeting(query) or _NEW_TOPIC.search(text):
        return False
    return bool(_REFERENCE.search(text) or _SHORT_FOLLOWUP.fullmatch(text))


def build_retrieval_query(
    query: str, prior_user_queries: list[str], max_history_chars: int = 1000,
) -> str:
    """Append up to three whole prior questions within the history budget.

    ``max_history_chars`` bounds the entire appended block, including its label
    and separators; the original current query remains byte-for-byte unchanged
    at the front. Oversize records are skipped whole instead of slicing an
    entity or inventing a shorter query. Empty social/continuation messages do
    not displace a useful topic question. An explicit old topic switch is a
    boundary: older topics are never recovered across it, even if it is large.

    The scheme cannot determine which of several entities a pronoun means, or
    recover a subject first introduced only by an assistant. It adds no model
    call and must not be treated as factual evidence in the answer prompt.
    """
    if not needs_followup_context(query) or max_history_chars <= len(_HISTORY_HEADER):
        return query

    selected: list[str] = []
    seen: set[str] = set()
    used_chars = len(_HISTORY_HEADER)
    for original in reversed(prior_user_queries):
        previous = original.strip()
        topic_boundary = bool(_NEW_TOPIC.search(previous))
        if (previous and previous not in seen and not is_greeting(previous)
                and not _is_short_followup(previous)):
            seen.add(previous)
            added_chars = len(previous) + (2 if selected else 0)
            if used_chars + added_chars <= max_history_chars:
                selected.append(previous)
                used_chars += added_chars
                if len(selected) == _MAX_PRIOR_QUESTIONS:
                    break
        if topic_boundary:
            break

    if not selected:
        return query
    return query + _HISTORY_HEADER + "\n\n".join(reversed(selected))
