"""Trusted application scope, independent from model-produced arguments."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit


def public_profile(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("公开账号链接无效")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in {"www.douyin.com", "douyin.com"}
            or parsed.username or parsed.password or parsed.port
            or not re.fullmatch(r"/user/[^/?#]+/?", parsed.path)
            or parsed.path.rstrip("/").endswith("/self")):
        raise ValueError("需要固定的抖音公开账号链接")
    return urlunsplit(("https", "www.douyin.com", parsed.path.rstrip("/"), "", ""))


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Binding:
    binding_id: str
    instance_id: str
    sender_profile_url: str
    receiver_profile_url: str
    knowledge_base_id: int | None
    public_reply_allowed: bool
    active: bool = True
    query_scope: str = "library"

    def __post_init__(self):
        if self.query_scope == 'all_local':
            if self.knowledge_base_id is not None:
                raise ValueError('全部资料范围不能借用知识库编号')
        elif self.query_scope != 'library' or type(self.knowledge_base_id) is not int or self.knowledge_base_id <= 0:
            raise ValueError('绑定查询范围无效')

    @property
    def is_all_local(self):
        return self.query_scope == 'all_local'

    @classmethod
    def from_dict(cls, value: dict) -> "Binding":
        legacy = set(cls.__dataclass_fields__) - {'query_scope'}
        if (not isinstance(value, dict) or set(value) not in (legacy, legacy | {'query_scope'})
                or ('query_scope' in value and value['query_scope'] != 'all_local')):
            raise ValueError("本机绑定字段不完整或含未知字段")
        for name in ("binding_id", "instance_id"):
            if not isinstance(value[name], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value[name]):
                raise ValueError("本机绑定编号无效")
        library = value["knowledge_base_id"]
        if ((value.get('query_scope') == 'all_local' and library is not None)
                or ('query_scope' not in value and (type(library) is not int or library <= 0))):
            raise ValueError("绑定知识库编号无效")
        if type(value["active"]) is not bool or type(value["public_reply_allowed"]) is not bool:
            raise ValueError("绑定授权必须是明确的布尔值")
        sender, receiver = public_profile(value["sender_profile_url"]), public_profile(value["receiver_profile_url"])
        if sender == receiver:
            raise ValueError("当前单人原型需要分开的提问账号A和助手B")
        return cls(**{**value, "sender_profile_url": sender, "receiver_profile_url": receiver})

    def to_dict(self) -> dict:
        result = asdict(self)
        if not self.is_all_local:
            result.pop('query_scope')
        return result


def validate_envelope(value: dict, binding: Binding) -> dict:
    required = {"command_id", "reference_id", "reference_quality", "native_comment_id",
                "sender_profile_url", "receiver_profile_url", "video_id", "comment_text",
                "source_mode", "evidence"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("收件信封字段不完整或含未知字段")
    if not binding.active:
        raise ValueError("本机绑定已停用")
    for name in ("command_id", "reference_id"):
        if not isinstance(value[name], str) or not value[name] or len(value[name]) > 256:
            raise ValueError("收件引用编号无效")
    if value["reference_quality"] not in {"native", "verified_ui"}:
        raise ValueError("未知的原评论引用质量")
    native = value["native_comment_id"]
    if native is not None and (not isinstance(native, str) or not native.strip() or len(native) > 256):
        raise ValueError("原生评论号必须保留为字符串")
    if value["reference_quality"] == "native" and native is None:
        raise ValueError("缺少声称存在的原生评论号")
    if not isinstance(value["video_id"], str) or not re.fullmatch(r"\d{5,30}", value["video_id"]):
        raise ValueError("视频编号必须为完整字符串")
    text = value["comment_text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError("原评论缺失或过长")
    if value["source_mode"] not in {"live_ui", "replay", "isolated"}:
        raise ValueError("未知收件来源")
    if public_profile(value["sender_profile_url"]) != binding.sender_profile_url:
        raise ValueError("评论发送者不属于当前绑定")
    if public_profile(value["receiver_profile_url"]) != binding.receiver_profile_url:
        raise ValueError("评论没有发给当前助手")
    evidence = value["evidence"]
    if isinstance(evidence, dict) and evidence.get('source_kind') == 'conversation_followup':
        from .followup_evidence import validate_followup_envelope
        return validate_followup_envelope(value, binding)
    if isinstance(evidence, dict) and evidence.get('source_kind') == 'library_choice_reply':
        from .choice_evidence import validate_choice_envelope
        return validate_choice_envelope(value, binding)
    if isinstance(evidence, dict) and 'source_kind' in evidence:
        raise ValueError('未知收件证据类型')
    if (not isinstance(evidence, dict)
            or any(evidence.get(key) is not True for key in ("real_mention", "sender_verified", "video_verified"))
            or type(evidence.get("unique_count")) is not int or evidence["unique_count"] != 1):
        raise ValueError("缺少真实@、发送者、视频或唯一原评论证据")
    try:
        observed = datetime.fromisoformat(evidence.get("observed_at_utc", ""))
    except (TypeError, ValueError):
        raise ValueError("观测时间无效") from None
    if observed.tzinfo is None:
        raise ValueError("观测时间缺少时区")
    canonical_json(value)
    return {**value, "sender_profile_url": binding.sender_profile_url,
            "receiver_profile_url": binding.receiver_profile_url}


def parse_tool_arguments(raw: str, allowed: set[str], required: set[str]) -> dict:
    def reject_constant(_):
        raise ValueError("非有限JSON数值")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("工具参数出现重复字段")
            result[key] = value
        return result

    if not isinstance(raw, str) or len(raw) > 12000:
        raise ValueError("工具参数无效或过长")
    args = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    if not isinstance(args, dict) or set(args) - allowed or not required.issubset(args):
        raise ValueError("工具参数字段无效")
    return args
