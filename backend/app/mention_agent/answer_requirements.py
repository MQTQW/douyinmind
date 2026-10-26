"""Bounded explicit quotation requirements, validated against current tool text.

This is a format/provenance check, not a semantic answer-quality or transcript
accuracy judge. One source chunk may supply several different excerpts.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
import unicodedata

MAX_QUOTES = 3
MIN_EXCERPT_CHARS = 6
QUOTE_CODES = frozenset({
    "evidence_quotes_required", "evidence_quote_count", "invalid_evidence_quote",
    "evidence_quote_not_provided", "duplicate_evidence_quote", "evidence_quote_not_visible",
    "query_required_before_quote_clarification", "public_evidence_quote_count", "unaccounted_public_quote",
})
_NUMBER = r"(?:[0-9]+|[零一二两三四五六七八九十]{1,3})"
_UNIT = r"(?:条|段|句|处|项|个)"
_QUALIFIERS = r"(?:(?:不同的?|(?:可|可以|能)核对的?|(?:可|可以|能)对照的?|独立的?|直接的?|完整的?|简短的?|相关的?|关键的?|明确的?|视频中[的]?|原视频中[的]?|文中[的]?)\s*){0,3}"
_TARGET = r"(?:原文(?:依据|证据|引用|摘录|片段)?|原话|原句|引文|摘录|引用(?:依据|证据|片段)?)"
_PATTERNS = [
    re.compile(rf"(?P<number>{_NUMBER})\s*{_UNIT}\s*{_QUALIFIERS}{_TARGET}"),
    re.compile(rf"(?:引用|摘录)\s*(?P<number>{_NUMBER})\s*{_UNIT}"),
    re.compile(rf"{_TARGET}\s*(?:共|给我|给出|给|列出|提供|附上|需要|要|至少|最少|不少于|不要少于|最多|至多|不超过|不要超过|不多于|[:：])?\s*(?P<number>{_NUMBER})\s*{_UNIT}"),
]
_REQUEST = re.compile(r"请|帮我|给我|给出|(?:^|并|再|另|同时|只|都)(?:给|用|拿|以)|列出|列举|提供|附上|摘录|引用|贴出|需要|想要|我要|要|而是|至少|最少|不少于|最多|至多|不超过|不要超过")
_NEGATION = re.compile(r"不要|不需要|不用|别|无需|不必|不想|不要求|不是|并非")
_UPPER = re.compile(r"(?:最多|至多|不超过|不要超过|不多于).{0,8}$")
_LOWER = re.compile(r"(?:至少|最少|不少于|不要少于).{0,8}$")
_PUBLIC_QUOTES = re.compile(r'“(.*?)”|「(.*?)」|『(.*?)』|‘(.*?)’|"(.*?)"', re.S)


class AnswerRequirementError(ValueError):
    def __init__(self, code):
        self.code = code if code in QUOTE_CODES else "invalid_evidence_quote"
        super().__init__(self.code)


@dataclass(frozen=True)
class QuoteRequirement:
    minimum: int
    maximum: int
    issue: str | None = None

    @property
    def description(self):
        return f"{self.minimum}条" if self.minimum == self.maximum else f"{self.minimum}至{self.maximum}条"

    def clarification(self):
        if self.issue == "unsupported_quote_count":
            return "当前一次最多能给出3条简短原文引用，请把条数调整到1至3条。"
        if self.issue:
            return "请明确需要几条原文引用；当前一次支持1至3条。"
        return f"本次还未生成满足“{self.description}原文引用”要求的回答。可以减少引用条数或补充资料后再问。"


def _number(text):
    if text.isascii() and text.isdecimal():
        return int(text)
    digits = dict(zip("零一二两三四五六七八九", (0,1,2,2,3,4,5,6,7,8,9)))
    if text in digits:
        return digits[text]
    if text.count("十") == 1:
        left, right = text.split("十")
        if (not left or left in digits) and (not right or right in digits):
            return (digits[left] if left else 1)*10+(digits[right] if right else 0)
    return None


def parse_answer_requirements(comment):
    if not isinstance(comment, str):
        return None
    text = unicodedata.normalize("NFKC", comment)
    text = re.sub(r"^\s*@\S+\s+", "", text, count=1)
    constraints, seen, issue = [], set(), None
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            identity = match.span("number")
            if identity in seen:
                continue
            seen.add(identity)
            start = max((text.rfind(mark,0,identity[0]) for mark in "，,。！？!?；;\n"), default=-1)+1
            prefix = text[start:identity[0]].strip()
            # A bounded explicit correction ends the scope of its earlier negation.
            if "而是" in prefix:
                prefix = prefix.rsplit("而是",1)[1].strip()
            upper, lower = bool(_UPPER.search(prefix)), bool(_LOWER.search(prefix))
            if _NEGATION.search(prefix.replace("不要超过","").replace("不要少于","")):
                continue
            requested = bool(_REQUEST.search(prefix))
            if re.search(r"是否|要不要|需不需要|需要不需要",prefix):
                continue
            if prefix and not requested:
                continue
            if not requested and re.match(r"\s*(?:够吗|够不够|是什么|在哪里|有吗|吗)", text[match.end():]):
                continue
            if re.search(r"视频|作者|他说|文中|资料里",prefix) and not re.search(r"请|帮我|给我|我要|需要",prefix):
                continue
            count = _number(match.group("number"))
            if count is None:
                issue = "ambiguous_quote_count"
                continue
            if count == 0:
                continue
            if not upper and count > MAX_QUOTES:
                issue = "unsupported_quote_count"
                continue
            constraints.append((1,min(count,MAX_QUOTES)) if upper else
                               (count,MAX_QUOTES) if lower else (count,count))
    if issue:
        return QuoteRequirement(1,MAX_QUOTES,issue)
    if not constraints:
        return None
    minimum, maximum = max(item[0] for item in constraints), min(item[1] for item in constraints)
    return QuoteRequirement(minimum,maximum,"ambiguous_quote_count" if minimum>maximum else None)


def tools_for_requirements(tools, requirement):
    if requirement is None or requirement.issue:
        return tools
    selected = deepcopy(tools)
    for item in selected:
        function = item["function"]
        if function["name"] == "propose_reply":
            function["parameters"]["properties"]["evidence_quotes"] = {
                "type":"array", "minItems":requirement.minimum, "maxItems":requirement.maximum,
                "items":{"type":"object","properties":{
                    "source_id":{"type":"string","minLength":1,"maxLength":128},
                    "excerpt":{"type":"string","minLength":MIN_EXCERPT_CHARS,"maxLength":280}},
                    "required":["source_id","excerpt"],"additionalProperties":False}}
            function["parameters"]["required"].append("evidence_quotes")
            function["description"] += " 用户要求原文引用；正文必须用引号分别写出这些真实摘录，evidence_quotes只作核对，不能代替正文。"
        elif function["name"] == "request_clarification":
            function["parameters"]["properties"]["reason_code"]["enum"].append("quote_requirement_unmet")
    return selected


def requirement_instruction(requirement):
    return (f"用户明确要求{requirement.description}原文引用。回答时单独propose_reply，提供evidence_quotes"
            "数组，每项只有source_id和excerpt。摘录只能来自本轮工具实际返回给你的原文，不能借用历史或未显示内容。"
            f"每段至少{MIN_EXCERPT_CHARS}个非空白字符，选择足够完整的短句；不同摘录不能只是重复或互相包含。"
            "同一资料块可以提供多段不同原文，不要求不同来源。最终text必须用中文引号或「」逐条展示同样的摘录，"
            "不能只在evidence_quotes里提交而正文仍是概述。字词和标点须与原文一致，允许换行空白差异。"
            "正文中其他较长引文也必须声明并验证，不能额外插入第三段未核对的话。短术语和已绑定库名不算原文条数。"
            "正文仍最多280字，不能放宽长度、轮数或工具预算；无法满足时单独request_clarification"
            "(reason_code=quote_requirement_unmet)，不要编造引用或声称已经满足。")


def quote_error_hint(requirement):
    return (f"请修正为{requirement.description}不同原文摘录：evidence_quotes每项提供本轮实际资料的source_id、excerpt；"
            "公开text要用引号逐条写出完全对应的摘录，所用资料编号都列在source_ids里。同一资料块可以给两条。"
            "只摘词、重复/包含摘录、改写或仅hidden字段均不合格。保持原长度和轮数预算；"
            "确实无法满足时单独request_clarification(reason_code=quote_requirement_unmet)。")


def _normalized(text):
    return " ".join(text.split())


def validate_quotes(requirement, quoted, public_text, source_ids, provided_texts, *, non_evidence_labels=()):
    if not isinstance(quoted,list):
        raise AnswerRequirementError("evidence_quotes_required")
    if not requirement.minimum <= len(quoted) <= requirement.maximum:
        raise AnswerRequirementError("evidence_quote_count")
    normalized_seen, result = [], []
    for item in quoted:
        if not isinstance(item,dict) or set(item)!={"source_id","excerpt"}:
            raise AnswerRequirementError("invalid_evidence_quote")
        source, excerpt = item["source_id"], item["excerpt"]
        if (not isinstance(source,str) or source not in source_ids or not isinstance(excerpt,str)
                or len(excerpt)>280 or len("".join(excerpt.split())) < MIN_EXCERPT_CHARS
                or sum(character.isalnum() for character in excerpt)<4):
            raise AnswerRequirementError("invalid_evidence_quote")
        normalized = _normalized(excerpt)
        if any(normalized in previous or previous in normalized for previous in normalized_seen):
            raise AnswerRequirementError("duplicate_evidence_quote")
        contexts = provided_texts.get(source,[])
        if not any(normalized in _normalized(context) for context in contexts):
            raise AnswerRequirementError("evidence_quote_not_provided")
        normalized_seen.append(normalized)
        result.append({"source_id":source,"excerpt":excerpt.strip()})
    # Count what a reader sees as quotation, not merely the hidden declaration.
    # Short terminology is not automatically an extra basis. Explicit numbered
    # evidence markers still make a short quotation accountable. Name exemptions
    # come only from the trusted binding, never a model-supplied exception field.
    labels={_normalized(label) for label in non_evidence_labels if isinstance(label,str)}
    visible=[]
    for match in _PUBLIC_QUOTES.finditer(public_text):
        value=next(value for value in match.groups() if value is not None)
        normalized=_normalized(value)
        prefix=public_text[max(0,match.start()-32):match.start()]
        explicit=bool(re.search(r'(?:原文(?:依据)?|依据|引文|原话|引用)\s*[1-9一二三①②③]?\s*(?:是|为|写道|说|提到)?\s*[:：]?\s*$',prefix)
                      or re.search(r'(?:^|[\n；;。])\s*(?:[1-9一二三][.、)）]|[①②③]|第[1-9一二三]'+_UNIT+r')\s*[:：]?\s*$',prefix))
        if normalized in labels and normalized not in normalized_seen and not explicit:
            continue
        substantive=len(''.join(value.split()))>=MIN_EXCERPT_CHARS and sum(char.isalnum() for char in value)>=4
        if substantive or explicit or normalized in normalized_seen:
            visible.append(normalized)
    if any(normalized not in visible for normalized in normalized_seen):
        raise AnswerRequirementError('evidence_quote_not_visible')
    if not requirement.minimum<=len(visible)<=requirement.maximum:
        raise AnswerRequirementError('public_evidence_quote_count')
    if len(visible)!=len(normalized_seen) or any(normalized not in normalized_seen for normalized in visible):
        raise AnswerRequirementError('unaccounted_public_quote')
    return result
