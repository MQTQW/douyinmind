"""Deterministic source-command resolution; never calls a model or a tool.

Positive anchored save grammar is intentionally narrower than arbitrary Chinese.
Everything else remains query/clarification, so explaining, quoting, negating or
conditionally discussing a save cannot itself write to a knowledge base.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
import unicodedata

from app.mention_agent.contracts import Binding
from .registry import Registry, RegistryError, normalize_alias


_MARKER = re.compile(
    r"(?:(?:[，。；;\s]*测试编号\s*[:：]?\s*[A-Za-z0-9_-]{1,80})"
    r"|(?:[。；;]\s*DM-[A-Za-z0-9][A-Za-z0-9_-]{0,76}))[。.!！\s]*$"
)
_NEGATIVE = re.compile(r"不要|先别|暂不|不必|无需|不用|禁止|取消|不准|别再|别把|别存|别保存|别导入|不想|不希望|不需要|不能|莫要")
_QUESTION = re.compile(r"[?？]|(?:怎么|如何|是否|能否|可否|要不要|可以吗|行吗|好吗|好不好|请问|教我|教程|方法|步骤|能不能|可不可以|吗(?:[，。！!\s]|$))")
_CONDITIONAL = re.compile(r"如果|假如|要是|倘若|只要|若是|假设|等.+(?:后|再)|当.+(?:时|后)|(?:以后|之后|之前)再")
_MULTIPLE = re.compile(r"并且|然后|同时|以及|或者|还有|再(?:把|将|存|收|保|导)|[、,，;；]|(?:和|及|或)")
_PREFIX = r"(?:(?:请(?:你)?|麻烦(?:你)?|帮我|替我|劳驾|帮忙)\s*)*"
_VIDEO = r"(?:这个|当前|这条|该|本条|此|本)\s*视频"
_VERB = r"(?:保存|存|收录|收进|放入|加入|导入|上传)"
_DIRECTION = r"(?:到|至|进|入)?"
_SAVE = re.compile(
    r"^" + _PREFIX + r"(?:"
    r"(?:把|将)\s*" + _VIDEO + r"\s*" + _VERB + r"\s*" + _DIRECTION + r"\s*(?P<first>.*)"
    r"|" + _VERB + r"\s*" + _VIDEO + r"\s*" + _DIRECTION + r"\s*(?P<second>.*)"
    r"|" + _VERB + r"\s*(?:到|至|进|入)\s*(?P<third>.+)"
    r"|(?:收进|放入|加入|导入(?:到)?|上传到)\s*(?P<fourth>.+)"
    r")$"
)
_QUOTE_PAIRS = {"“": "”", "‘": "’", '"': '"', "'": "'", "「": "」", "『": "』"}
_DEFAULT_NAMES = {"默认", "默认库", "默认知识库", "我的默认库", "当前默认库"}
_UNSPECIFIED_SAVE_NAMES = {"知识库","我的知识库","本人的知识库","本人知识库","某个知识库","这个知识库"}
_ALL_SOURCE_NAMES = {"全部资料","所有资料","全部入库资料","所有入库资料","全部已入库资料","本机全部资料","本人本机全部资料","本机所有资料","全部知识库","所有知识库","本机全部知识库",
    # Ordinary aggregate query ranges, not literal unknown KB names. The
    # resolver still requires the separate all-local grant; quoted names and
    # actual registered name collisions retain the existing literal policy.
    "已入库资料","我的已入库资料","本人已入库资料","本人的已入库资料"}
_QUERY_PREFIX = re.compile(
    r"^(?:请问|我想问|想问|帮我看看|请你|麻烦你|帮我|请|麻烦|我想知道|查询|检索|使用|查看|看看|查一下|解释|概括|总结|根据|回答|说明|分析|描述|介绍|以及|还有|和|及|与|从|在|用)"
)
_FOLLOWUP = re.compile(
    r"^(?:(?:那|那么)?\s*第?[一二三四五六七八九十百0-9]+(?:点|条|步|项|部分|个)"
    r"(?:呢|是什么|是什么意思|怎么理解|怎么做|再说一下|再解释一下)?"
    r"|(?:请)?(?:继续|接着说|展开说|再详细一点|再讲详细一点|举个例子|再举个例子)"
    r"|(?:这个|那个|它|这点|那点)(?:呢|是什么意思|怎么理解|再解释一下))\s*[。？?！!]*$"
)


@dataclass(frozen=True)
class ParsedIntent:
    action: str
    grant_id: str | None = None
    reason_code: str | None = None
    clarification: str | None = None
    explicit_library: bool = False
    needs_followup_context: bool = False


@dataclass(frozen=True)
class Resolution:
    action: str
    binding: Binding | None = None
    library: dict | None = None
    user_id: str | None = None
    device_id: str | None = None
    route_state: str = "blocked"
    clarification: str | None = None
    reason_code: str | None = None
    authorization: dict | None = None
    query_scope: str = "library"
    choice_intent: dict | None = None
    choice_options: list[dict] = field(default_factory=list)
    catalog_count: int | None = None
    catalog_scope: str | None = None

    def to_dict(self):
        return {"action": self.action, "binding": self.binding.to_dict() if self.binding else None,
                "library": self.library, "user_id": self.user_id, "device_id": self.device_id,
                "route_state": self.route_state, "clarification": self.clarification,
                "reason_code": self.reason_code, "authorization": self.authorization,
                "query_scope":self.query_scope,"choice_intent":self.choice_intent,
                "choice_options":self.choice_options,"catalog_count":self.catalog_count,"catalog_scope":self.catalog_scope}


def _clean(text):
    value = re.sub(r"^\s*@\S+\s+", "", text, count=1).strip()
    return _MARKER.sub("", value).strip().rstrip("。！!").strip()


def _target_name(value):
    value = value.strip().rstrip("。！!").strip()
    # A target can be quoted with a trailing 知识库, or be a plain alias.
    if value.endswith("知识库") and value[:1] in _QUOTE_PAIRS:
        value = value[:-3].strip()
    if value[:1] in _QUOTE_PAIRS:
        if len(value) < 2 or value[-1] != _QUOTE_PAIRS[value[0]]:
            return None
        value = value[1:-1].strip()
    if any(char in value for char in "“”‘’\"'「」『』"):
        return None
    try:
        return normalize_alias(value)
    except RegistryError:
        return None


def _forms(grant):
    values = [grant["name"], *grant.get("aliases", [])]
    result = set()
    for value in values:
        if not value:
            continue
        try:
            normalized = normalize_alias(value)
        except RegistryError:
            # A catalog display name can predate alias validation. It is not
            # a matchable target, and must not poison unrelated intake.
            continue
        result.add(normalized)
        if not normalized.endswith(("库", "资料")):
            result.add(normalized + "库")
            result.add(normalized + "知识库")
    return result


def library_forms(grant):
    """Exact library names the deterministic parser accepts for this one grant.

    These are for a trusted caller's selected grant only. They are not model
    authority to discover, grant, or change a knowledge-base target.
    """
    return sorted(_forms(grant))


def _has_explicit_library(text, grants):
    quoted,remaining=_query_quoted_targets(text)
    if quoted or _query_named_targets(remaining,grants):
        return True
    return bool(re.search(r"(?:我的|当前)?默认(?:知识库|库)",text))


def _normalized_text(text):
    # Comment bodies are longer than a library alias; do not apply alias limits.
    return " ".join(unicodedata.normalize("NFKC", text).strip().split()).casefold()


def _query_named_targets(text, grants):
    """Recognize explicit unquoted locative names before substring matching."""
    values = re.findall(
        r"(?:^|[\s，。！？?；;]|从|在|用)([^\s，。！？?；;]{1,132}?(?:知识库|库|资料))"
        r"(?=里|中|内|的|查|找|检索|查询|解释|有|包含|主要|讲|[\s，。！？?；;]|$)", text)
    values += re.findall(r"(?:根据|使用|用|从|在|查询|检索)\s*([^\s，。！？?；;“”\"‘’]{1,132}?(?:知识库|库|资料))"
                         r"(?=回答|说明|解释|分析|搜索|检索|查询|里|中|内|的|[\s，。！？?；;]|$)",text)
    generic = _DEFAULT_NAMES | _ALL_SOURCE_NAMES | {"知识库", "库", "资料", "我的知识库", "这个知识库", "当前知识库", "本知识库","这个库","当前库","本库","该库","我的库"}
    forms = {form for grant in grants for form in _forms(grant)}
    result = []
    for value in values:
        candidate = normalize_alias(value)
        while candidate not in forms and candidate not in generic:
            shorter = _QUERY_PREFIX.sub("", candidate, count=1).strip()
            if not shorter or shorter == candidate:
                break
            candidate = shorter
        if candidate not in generic or candidate in forms:
            result.append(candidate)
    for request in re.finditer(r"(?:根据|使用|用|从|在|查询|检索)\s*([^，。！？?；;]{1,300})",text):
        phrase=_normalized_text(request.group(1))
        leading=re.match(r"(.{1,132}?)(?=回答|说明|解释|分析|查询|检索|里|中|内)",phrase)
        if leading:
            candidate=leading.group(1).strip()
            if candidate not in generic and candidate not in forms and any(form in candidate for form in forms):
                result.append(candidate)
        for form in forms:
            if re.search(r"(?:^|和|与|及|或|\s)"+re.escape(form)+r"(?=知识库|里|中|内|回答|说明|解释|分析|和|与|及|或|[\s，。！？?；;]|$)",phrase):
                result.append(form)
    # A short configured alias (e.g. 学习) is explicit only in a locative
    # phrase such as 学习里. Its mere occurrence in prose cannot choose a KB.
    for segment in re.split(r"[，。！？?；;、]", _normalized_text(text)):
        candidate = segment.strip()
        while candidate:
            matches = [form for form in forms if candidate.startswith(form)
                       and re.match(r"\s*(?:里|中|内)", candidate[len(form):])]
            if matches:
                result.extend(matches)
                break
            shorter = _QUERY_PREFIX.sub("", candidate, count=1).strip()
            if shorter == candidate:
                break
            candidate = shorter
    return list(dict.fromkeys(result))


def _query_quoted_targets(text):
    names, remaining = [], text
    for opening, closing in _QUOTE_PAIRS.items():
        quoted = re.escape(opening) + r"([^" + re.escape(closing) + r"]*)" + re.escape(closing)
        for pattern in (quoted + r"\s*(?:知识库|库|里|中|内)", r"(?:知识库|库)\s*" + quoted):
            names.extend(match.group(1) for match in re.finditer(pattern, remaining))
            remaining = re.sub(pattern, " ", remaining)
    return names, remaining


def _choice(grants, default_grant_id):
    default = [item for item in grants if item["grant_id"] == default_grant_id]
    if len(default) == 1:
        return default[0]
    return None


def _query_choice(text, grants, default_grant_id):
    normalized = _normalized_text(text)
    quoted, remaining = _query_quoted_targets(text)
    # Quoting one name cannot hide a second unquoted target in the same query.
    named = quoted + _query_named_targets(remaining, grants)
    if named:
        selected = []
        for name in named:
            matches = [grant for grant in grants if normalize_alias(name) in _forms(grant)]
            if len(matches) != 1:
                return ParsedIntent("clarify", reason_code="unknown_or_ambiguous_library",
                                    clarification="请明确选择一个获准使用的库，不会猜测相似库名。")
            selected.extend(matches)
        unique = {item["grant_id"] for item in selected}
        if len(unique) != 1:
            return ParsedIntent("clarify", reason_code="multiple_libraries",
                                clarification="这条指令涉及多个知识库，请先明确选择一个。")
        return ParsedIntent("query", selected[0]["grant_id"])
    mentioned = []
    for grant in grants:
        for form in _forms(grant):
            if len(form) >= 2 and form.endswith(("库", "资料")) and form in normalized:
                mentioned.append(grant)
                break
    if len(mentioned) > 1:
        return ParsedIntent("clarify", reason_code="multiple_libraries",
                            clarification="这条指令涉及多个知识库，请先明确选择一个。")
    if mentioned:
        return ParsedIntent("query", mentioned[0]["grant_id"])
    # Explicit library names are not silently interpreted as the default.
    unknown_save_question = re.search(r"(?:存到|保存到|收进|导入到|加入)\s*([^，。?？]+)", text)
    if quoted or unknown_save_question:
        return ParsedIntent("clarify", reason_code="unknown_library",
                            clarification="未找到你获准使用的目标库，请从允许的知识库中明确选择。")
    selected = _choice(grants, default_grant_id)
    if selected is None:
        return ParsedIntent("clarify", reason_code="default_library_required",
                            clarification="请指定一个允许的知识库，或先设置默认库。")
    return ParsedIntent("query", selected["grant_id"])


def _parse_intent(text, grants, default_grant_id=None):
    """Resolve only the caller's supplied allowed options; unknown names stay private."""
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        return ParsedIntent("clarify", reason_code="invalid_comment",
                            clarification="请提供完整、简短的提问或保存指令。")
    text = _clean(text)
    if not text:
        return ParsedIntent("clarify", reason_code="empty_comment", clarification="请说明要查询什么或保存哪个当前视频。")
    if _NEGATIVE.search(text) or _QUESTION.search(text) or _CONDITIONAL.search(text):
        return _query_choice(text, grants, default_grant_id)
    match = _SAVE.fullmatch(text)
    if match is None:
        return _query_choice(text, grants, default_grant_id)
    target = next(value for value in match.groups() if value is not None).strip()
    if re.search(r"https?://|v\.douyin\.com|www\.|file:|[A-Za-z]:[\\/]", text, re.I):
        return ParsedIntent("clarify", reason_code="external_target_forbidden",
                            clarification="保存仅限这条评论所在的视频和你获准使用的库。")
    normalized = _target_name(target) if target else None
    literal_named = bool(target[:1] in _QUOTE_PAIRS and any(normalized in _forms(grant) for grant in grants))
    if not target or (normalized in _UNSPECIFIED_SAVE_NAMES and not literal_named):
        return ParsedIntent("clarify",reason_code="save_library_required",
            clarification="请先选择要保存到哪个知识库，选择前不会保存。")
    if normalized in _DEFAULT_NAMES:
        literal = [grant for grant in grants if normalized in _forms(grant)]
        if any(grant["grant_id"] != default_grant_id for grant in literal):
            return ParsedIntent("clarify", reason_code="ambiguous_default_library",
                clarification="“默认库”同时是某个库的名称或别名，请改用明确、不冲突的库名。")
        if target[:1] in _QUOTE_PAIRS and not literal:
            return ParsedIntent("clarify", reason_code="unknown_library",
                clarification="未找到这个带引号的库名，请写明获准使用的库，或不加引号使用默认库。")
    if normalized in _DEFAULT_NAMES:
        selected = _choice(grants, default_grant_id)
        if selected is None:
            return ParsedIntent("clarify", reason_code="default_library_required",
                                clarification="请先选择本人的默认知识库，或写明目标库。")
    else:
        matches = [grant for grant in grants if normalized is not None and normalized in _forms(grant)]
        if len(matches) > 1:
            return ParsedIntent("clarify", reason_code="ambiguous_library_alias",
                                clarification="这个库名或别名对应多个允许的库，请选择具体知识库。")
        if not matches:
            return ParsedIntent("clarify", reason_code="multiple_targets" if _MULTIPLE.search(target) else "unknown_library",
                                clarification="请明确指定一个允许的知识库；不会猜测、同时保存到多个库或自动建库。")
        selected = matches[0]
    if not selected.get("can_import"):
        return ParsedIntent("clarify", selected["grant_id"], reason_code="import_not_allowed",
                            clarification="当前账号尚未获准向这个库保存视频，可以继续查询资料。")
    return ParsedIntent("save", selected["grant_id"])


def parse_intent(text, grants, default_grant_id=None):
    """Parse original text; annotate target/context without accepting model hints."""
    try:
        parsed = _parse_intent(text, grants, default_grant_id)
    except RegistryError:
        # User-written names have the same restrictions as configured aliases.
        # A bad reference is a reviewable command, not a receiver-loop failure.
        return ParsedIntent("clarify", reason_code="invalid_library_reference", explicit_library=True,
            clarification="库名含有无法识别的字符或过长，请重新写明一个允许的知识库。")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        return parsed
    cleaned = _clean(text)
    try:
        explicit = parsed.action=="save" or _has_explicit_library(cleaned, grants)
    except RegistryError:
        return ParsedIntent("clarify",reason_code="invalid_library_reference",explicit_library=True,
            clarification="库名含有无法识别的字符或过长，请重新写明一个允许的知识库。")
    return replace(parsed, explicit_library=explicit,
                   needs_followup_context=bool(_FOLLOWUP.fullmatch(cleaned)))


def parse_library_selection(text, options):
    """Compatibility wrapper around the one shared deterministic selector."""
    from .choice_intent import parse_library_selection as shared_selector
    try:
        selected=shared_selector(text,options)
    except (ValueError,TypeError,KeyError):
        return None
    if not isinstance(selected,dict):
        return None
    value=selected.get("knowledge_base_id",selected.get("kb_id"))
    return value if type(value) is int and value>0 else None


class CommandResolver:
    def __init__(self, registry: Registry):
        self.registry = registry

    def resolve_verified_intent(self,envelope,recorded_decision,*,preferred_binding_id=None,needs_followup_context=None):
        """A private completed semantic receipt selects meaning, never grants."""
        from .semantic_plan_store import RecordedSemanticDecision
        if type(recorded_decision) is not RecordedSemanticDecision:
            return Resolution('ignore',reason_code='semantic_recorded_decision_required')
        return self.resolve(envelope,preferred_binding_id=preferred_binding_id,
            needs_followup_context=needs_followup_context,_semantic_decision=recorded_decision)

    def resolve_semantic_failure(self,envelope,code):
        messages={
            'model_budget_exceeded':'这次理解指令的额度不足，尚未保存或执行其他操作，请在项目中核对额度设置。',
            'semantic_scope_unavailable':'账号或资料范围发生了变化，这次尚未执行，请重新确认。',
            'semantic_response_invalid':'这次没能确定你的意思，尚未执行，可以再具体说一下吗？',
        }
        message=messages.get(code,'这次理解指令的服务暂时没有返回明确结果，尚未执行，也不会自动重复调用。')
        return self.resolve(envelope,_program_clarification=(
            code if code in messages else 'semantic_understanding_unavailable',message))

    def resolve(self, envelope, *, preferred_binding_id=None, needs_followup_context=None,
                _semantic_decision=None,_program_clarification=None):
        """Resolve registry scope, optionally using trusted same-thread context.

        Only the caller may supply preferred_binding_id after verifying the
        original thread. An identically named envelope/model field is ignored.
        A stale/foreign preferred binding never causes fallback to another KB.
        """
        if not isinstance(envelope, dict):
            return Resolution("ignore", reason_code="invalid_envelope")
        if needs_followup_context is not None and type(needs_followup_context) is not bool:
            return Resolution("ignore", reason_code="invalid_followup_context")
        try:
            from app.mention_agent.contracts import public_profile
            sender = public_profile(envelope.get("sender_profile_url"))
            receiver = public_profile(envelope.get("receiver_profile_url"))
            video = envelope.get("video_id")
            if not isinstance(video, str) or not re.fullmatch(r"[0-9]{5,30}", video):
                return Resolution("ignore", reason_code="invalid_video_id")
            snapshot = self.registry.snapshot()
        except (RegistryError, ValueError, TypeError, AttributeError):
            return Resolution("ignore", reason_code="invalid_or_unavailable_scope")
        users = [item for item in snapshot["users"] if item["sender_profile_url"] == sender
                 and item["receiver_profile_url"] == receiver]
        if len(users) != 1:
            return Resolution("ignore", reason_code="sender_not_registered")
        user = users[0]
        if not user["active"] or user["verification_status"] != "verified":
            return Resolution("ignore", user_id=user["user_id"], reason_code="user_not_verified")
        configured_device = next((item for item in user["devices"]
                                  if item["device_id"] == user["default_device_id"]), None)
        if configured_device and not configured_device["is_local"]:
            return Resolution("clarify", user_id=user["user_id"], device_id=configured_device["device_id"],
                route_state="offline", reason_code="remote_device_not_connected",
                clarification="这台设备尚未接通受认证的跨电脑通道，不会在本机同号知识库代办。")
        devices = [item for item in user["devices"] if item["enabled"] and not item["revoked"]
                   and item["verification_status"] == "verified" and item["is_local"]]
        default = next((item for item in devices if item["device_id"] == user["default_device_id"]), None)
        if default:
            devices = [default]
        elif user["default_device_id"]:
            return Resolution("ignore", user_id=user["user_id"], reason_code="default_device_not_verified")
        elif len(devices) != 1:
            return Resolution("clarify", user_id=user["user_id"], reason_code="device_required",
                              clarification="请在本机选择并验证要使用的设备。")
        if not devices:
            return Resolution("ignore", user_id=user["user_id"], reason_code="no_verified_local_device")
        device = devices[0]
        grants = [item for item in device["libraries"] if item["active"] and item["library_exists"]]
        default_grant = next((item["grant_id"] for item in grants if item["is_default"]), None)
        if _program_clarification is not None:
            parsed=ParsedIntent('clarify',reason_code=_program_clarification[0],clarification=_program_clarification[1])
        elif _semantic_decision is None:
            parsed = parse_intent(envelope.get("comment_text"), grants, default_grant)
        else:
            from .semantic_resolution import parsed_intent_from_receipt
            try:
                parsed=parsed_intent_from_receipt(envelope,user,device,grants,_semantic_decision)
            except Exception:
                parsed=ParsedIntent('clarify',reason_code='semantic_scope_changed',
                    clarification='这条指令的账号或资料范围发生了变化，本次尚未执行，请重新确认。')
        defaultless_query = (parsed.reason_code == "default_library_required"
                             and _SAVE.fullmatch(_clean(envelope.get("comment_text", ""))) is None)
        if parsed.reason_code == "save_library_required":
            choice=self.registry.library_choices(user["user_id"],device["device_id"])
            binding=self.registry.authorize_all_local(user["user_id"],device["device_id"])
            authorization=self.registry.all_authorization_snapshot(user["user_id"],device["device_id"]) if binding else None
            if binding is None:
                candidates=sorted(grants,key=lambda item:not item["is_default"])
                for grant in candidates:
                    for carrier_action in ("query","import"):
                        carrier=self.registry.authorize_binding(grant["binding_id"],device["instance_id"],grant["knowledge_base_id"],carrier_action)
                        if carrier is not None:
                            binding=carrier
                            authorization=self.registry.authorization_snapshot(grant["binding_id"],device["instance_id"],grant["knowledge_base_id"],carrier_action)
                            break
                    if binding is not None:break
            prefix="你本人本机共有" if choice["catalog_scope"]=="all_local" else "本账号当前可见的授权知识库共有"
            names="；".join(str(index+1)+"."+item["name"]+("（当前不能保存）" if not item["can_import"] else "")
                           for index,item in enumerate(choice["choice_options"]))
            clarification=prefix+str(choice["catalog_count"])+"个："+names+"。请在原线程回复库名或序号；选择前不会保存。"
            if not choice["catalog_public_reply_allowed"]:
                binding=authorization=None
            return Resolution("clarify",binding=binding,authorization=authorization,user_id=user["user_id"],device_id=device["device_id"],
                library={"name":"尚未选择保存目标","knowledge_base_id":None,"accepted_names":[]},
                query_scope=getattr(binding,"query_scope","library") if binding else "library",
                route_state="ready" if binding and device["online"] else "offline" if binding else "blocked",
                reason_code="save_library_required",clarification=clarification,
                choice_options=choice["choice_options"],catalog_count=choice["catalog_count"],catalog_scope=choice["catalog_scope"],
                choice_intent={"kind":"save_library_choice","catalog_scope":choice["catalog_scope"],
                    "catalog_count":choice["catalog_count"],"options_sha256":choice["options_sha256"],
                    "eligible_count":sum(item["can_import"] for item in choice["choice_options"]),
                    "catalog_public_reply_allowed":choice["catalog_public_reply_allowed"]})
        # No library name means all-local only with a separate owner grant.
        # Same-thread history can resolve pronouns, never silently narrow scope.
        if (parsed.action=="query" or defaultless_query) and not parsed.explicit_library:
            binding=self.registry.authorize_all_local(user["user_id"],device["device_id"])
            authorization=self.registry.all_authorization_snapshot(user["user_id"],device["device_id"]) if binding else None
            if binding and authorization:
                return Resolution("query",binding=binding,authorization=authorization,user_id=user["user_id"],device_id=device["device_id"],
                    library={"name":"全部入库资料","knowledge_base_id":None,"query_scope":"all_local","accepted_names":[]},
                    query_scope="all_local",route_state="ready" if device["online"] else "offline",reason_code="all_local_query")
            parsed=ParsedIntent("clarify",reason_code="all_local_not_allowed",
                clarification="当前账号未获准查询本人本机全部资料，请明确指定一个已授权知识库，或在本机管理中确认全资料权限。")
        selected = next((item for item in grants if item["grant_id"] == parsed.grant_id), None)
        if parsed.action == "clarify" and selected is None:
            selected = _choice([grant for grant in grants if grant.get("can_query")], default_grant)
        action = "import" if parsed.action == "save" else "query"
        binding = self.registry.authorize_binding(selected["binding_id"], device["instance_id"],
            selected["knowledge_base_id"], action) if selected else None
        authorization = self.registry.authorization_snapshot(selected["binding_id"], device["instance_id"],
            selected["knowledge_base_id"], action) if binding else None
        if parsed.action in {"save", "query"} and binding is None:
            return Resolution("clarify", user_id=user["user_id"], device_id=device["device_id"],
                reason_code="library_action_not_allowed", clarification="当前库的授权不允许这项操作，请核对本机设置。")
        library = ({"grant_id": selected["grant_id"], "knowledge_base_id": selected["knowledge_base_id"],
                    "name": selected["name"], "aliases": selected["aliases"], "version": selected["version"],
                    "accepted_names": library_forms(selected)}
                   if selected else None)
        return Resolution(parsed.action, binding=binding, library=library, user_id=user["user_id"],
            device_id=device["device_id"], route_state=("ready" if device["online"] else "offline") if binding else "blocked",
            clarification=parsed.clarification, reason_code=parsed.reason_code, authorization=authorization)


Resolver = CommandResolver
ResolveResult = Resolution
