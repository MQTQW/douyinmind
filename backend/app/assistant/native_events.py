"""Pure native notice projections; no root/parent inference or execution rights."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse

from .native_transport import decimal_id, NativeTransportError

_SEC = re.compile(r"[A-Za-z0-9_.=-]{1,256}", re.ASCII)
_ROUTE = re.compile(r"/detail/([1-9][0-9]{0,29})/?", re.ASCII)


class NativeEventError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise NativeEventError(code) from None


_IDENTIFIER_FACT_BRAND = object()
_IDENTIFIER_FIELD_ROLES = frozenset({
    'unspecified', 'receiver_uid', 'notice_id', 'recipient_uid', 'author_uid',
    'notice_create_time', 'schema_cid', 'at_outer_video_id', 'at_inner_video_id',
    'reply_video_id', 'reply_wrapper_video_id', 'reply_outer_video_id',
    'reply_comment_id', 'reply_parent_hint', 'probe_video_id', 'probe_comment_id',
})
_IDENTIFIER_VALUE_KINDS = frozenset({'null_or_missing', 'bool', 'int', 'float', 'str', 'list', 'dict', 'other'})
_IDENTIFIER_PROBLEMS = frozenset({
    'wrong_type', 'negative', 'zero_not_allowed', 'out_of_range', 'empty',
    'non_ascii', 'padded', 'signed', 'leading_zero', 'non_decimal', 'unknown',
})


def _identifier_kind(value):
    if value is None:
        return 'null_or_missing'
    for kind, label in ((bool, 'bool'), (int, 'int'), (float, 'float'),
                        (str, 'str'), (list, 'list'), (dict, 'dict')):
        if type(value) is kind:
            return label
    return 'other'


def _identifier_problem(value, *, zero):
    if type(value) is int:
        if value < 0:
            return 'negative'
        if value == 0 and not zero:
            return 'zero_not_allowed'
        return 'out_of_range' if value >= 10 ** 30 else 'unknown'
    if type(value) is not str:
        return 'wrong_type'
    if not value:
        return 'empty'
    if not value.isascii():
        return 'non_ascii'
    if value != value.strip():
        return 'padded'
    if value[0] in '+-':
        return 'signed'
    if value.isdigit():
        if value == '0' and not zero:
            return 'zero_not_allowed'
        if len(value) > 1 and value[0] == '0':
            return 'leading_zero'
        return 'out_of_range' if len(value) > 30 else 'unknown'
    return 'non_decimal'


def validate_notice_identifier_diagnostic(value):
    """Three fixed enums only; unknown extra values are never inspected."""
    if type(value) is not dict or set(value) != {'field_role', 'value_kind', 'problem'}:
        return None
    fields = {'field_role': _IDENTIFIER_FIELD_ROLES,
              'value_kind': _IDENTIFIER_VALUE_KINDS, 'problem': _IDENTIFIER_PROBLEMS}
    result = {}
    for key, allowed in fields.items():
        item = value[key]
        if type(item) is not str or item not in allowed:
            return None
        result[key] = item
    return result


def notice_identifier_diagnostic(error):
    """Only an actual _id failure has a trusted issuer tuple, never raw data."""
    if type(error) is not NativeEventError:
        return None
    stored = object.__getattribute__(error, '__dict__')
    if type(stored) is not dict:
        return None
    fact = stored.get('_identifier_fact')
    if type(fact) is not tuple or len(fact) != 4 or fact[0] is not _IDENTIFIER_FACT_BRAND:
        return None
    return validate_notice_identifier_diagnostic({
        'field_role': fact[1], 'value_kind': fact[2], 'problem': fact[3]})


def _id(value, *, zero=False, field_role='unspecified'):
    try:
        return decimal_id(value, zero=zero)
    except NativeTransportError:
        # This tuple contains enums, not the rejected value, and grants no
        # permission. The same business error continues through the old gate.
        role = field_role if type(field_role) is str and field_role in _IDENTIFIER_FIELD_ROLES else 'unspecified'
        error = NativeEventError('invalid_notice_identifier')
        error._identifier_fact = (_IDENTIFIER_FACT_BRAND, role, _identifier_kind(value),
                                  _identifier_problem(value, zero=zero))
        raise error from None


def canonical_profile(sec_uid):
    if type(sec_uid) is not str or not _SEC.fullmatch(sec_uid):
        _fail("invalid_native_profile")
    return "https://www.douyin.com/user/" + sec_uid


def _object(value):
    if type(value) is not dict:
        _fail("invalid_notice_shape")
    return value


def _text(value):
    if (type(value) is not str or not 1 <= len(value) <= 4000 or not value.strip()
            or any(ord(c) < 32 and c not in "\r\n\t" for c in value)):
        _fail("invalid_notice_content")
    return value


def _cid_from_schema(value, video):
    if (type(value) is not str or not 1 <= len(value) <= 8192
            or any(c.isspace() or ord(c) < 32 for c in value)):
        _fail("invalid_notice_schema")
    try:
        parsed = urllib.parse.urlsplit(value)
        route = _ROUTE.fullmatch(parsed.path)
        if (parsed.scheme != "aweme" or parsed.netloc != "aweme" or not route
                or route.group(1) != video or parsed.fragment):
            _fail("invalid_notice_schema")
        # Encoded/duplicate/case-variant CID keys are intentionally not accepted.
        raw_keys = [part.split("=", 1)[0] for part in parsed.query.split("&")]
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True,
                                      max_num_fields=100, errors="strict")
        hits = [(key, val) for key, val in pairs if key.casefold() == "cid"]
        if len(hits) != 1 or hits[0][0] != "cid" or raw_keys.count("cid") != 1:
            _fail("invalid_notice_schema")
        return _id(hits[0][1], field_role='schema_cid')
    except NativeEventError:
        raise
    except (ValueError, UnicodeError):
        _fail("invalid_notice_schema")


def _timestamp(value):
    if value is None:
        return None
    number = _id(value, zero=True, field_role='notice_create_time')
    if int(number) >= 10 ** 12:
        _fail("invalid_notice_time")
    return number


def _visibility_probe(value):
    """An optional local diagnostic cannot change parsing or grant visibility."""
    if type(value) is not dict or not {'video_id', 'current_text', 'previous_text'} <= value.keys():
        return None
    if set(value) - {'video_id', 'current_text', 'previous_text', 'current_comment_id', 'previous_comment_id'}:
        return None
    try:
        result = {'video_id': _id(value['video_id'], field_role='probe_video_id'), 'current_text': _text(value['current_text'])}
        result['previous_text'] = None if value['previous_text'] is None else _text(value['previous_text'])
        for key in ('current_comment_id', 'previous_comment_id'):
            result[key] = None if value.get(key) is None else _id(value[key], field_role='probe_comment_id')
        return result
    except (NativeEventError, KeyError, TypeError, ValueError):
        return None


def _visibility_category(wrapper, key):
    if key not in wrapper:
        return 'missing'
    value = wrapper[key]
    if value is None:
        return 'null'
    if type(value) is bool:
        return 'bool'
    if type(value) is int:
        return 'int0' if value == 0 else 'int1' if value == 1 else 'other_int'
    return 'string' if type(value) is str else 'other'


def _observe_unknown_at_visibility(raw, wrapper, nid, probe, observer):
    # Called only after exact recipient and registered profile checks. Every
    # output value is an independent enum/hash/bool/null, never raw page data.
    if probe is None or not callable(observer):
        return
    video = cid = None
    try:
        video = _id(raw.get('aweme_id'), field_role='at_outer_video_id')
        if _id(_object(wrapper.get('aweme')).get('aweme_id'), field_role='at_inner_video_id') != video:
            video = None
    except NativeEventError:
        video = None
    if video is not None:
        try:
            cid = _cid_from_schema(wrapper.get('schema_url'), video)
        except NativeEventError:
            pass
    content = wrapper.get('content')
    normal = lambda text: re.sub(r'\s+', '', text)
    safe = {
        'channel': 9, 'notice_type': 45, 'at_sub_type': 2,
        'notice_id_sha256': hashlib.sha256(nid.encode('ascii')).hexdigest(),
        'visible_category': _visibility_category(wrapper, 'comment_status'),
        'hidden_category': _visibility_category(wrapper, 'comment_unvisible'),
        'video_id_valid': video is not None,
        'video_matches_probe': video == probe['video_id'],
        'comment_id_valid': cid is not None,
        'current_comment_id_matches': None if probe['current_comment_id'] is None else cid == probe['current_comment_id'],
        'previous_comment_id_matches': None if probe['previous_comment_id'] is None else cid == probe['previous_comment_id'],
        'content_is_string': type(content) is str,
        'current_text_matches': type(content) is str and normal(content) == normal(probe['current_text']),
        'previous_text_matches': None if probe['previous_text'] is None else
            type(content) is str and normal(content) == normal(probe['previous_text']),
    }
    try:
        observer(safe)
    except Exception:
        pass  # Diagnostics cannot affect unknown-visibility quarantine.


def project_page(page, channel, *, receiver_uid, receiver_sec_uid, profile_allowed,
                 diagnostic_probe=None, diagnostic_observer=None):
    """Drop unrelated user content before projecting a registered user's notice.

    `profile_allowed` checks the current local Registry profile identity only;
    actions still require a fresh Registry authorization and actual UI proof.
    Native author UID/sec are pinned together by the durable source store.
    """
    probe = _visibility_probe(diagnostic_probe)
    receiver_uid = _id(receiver_uid, field_role='receiver_uid')
    receiver_profile = canonical_profile(receiver_sec_uid)
    if type(channel) is not int or channel not in (9, 7) or not callable(profile_allowed):
        _fail("invalid_native_channel")
    page = _object(page)
    if type(page.get("status_code")) is not int or page["status_code"] != 0:
        _fail("invalid_notice_page")
    rows = page.get("notice_list_v2")
    if type(rows) is not list or len(rows) > 100:
        _fail("invalid_notice_page")
    notices, candidates, seen, skipped = [], [], set(), 0
    for raw in rows:
        raw = _object(raw)
        nid, recipient = _id(raw.get("nid_str"), field_role='notice_id'), _id(raw.get("user_id"), field_role='recipient_uid')
        if recipient != receiver_uid:
            _fail("notice_recipient_mismatch")
        if nid in seen:
            _fail("duplicate_native_notice_id")
        seen.add(nid)
        kind = raw.get("type")
        if type(kind) is not int or not 0 <= kind <= 100000:
            _fail("invalid_notice_type")
        # Page identities have no other users' body, media, profile or body hash.
        notices.append({"native_notice_id": nid, "notice_type": kind})
        if (channel == 9 and kind != 45) or (channel == 7 and kind != 31):
            skipped += 1
            continue
        if channel == 9:
            wrapper = _object(raw.get("at"))
            subtype = wrapper.get("sub_type")
            if type(subtype) is not int:
                _fail("invalid_notice_type")
            if subtype != 2:
                skipped += 1
                continue
            author = _object(wrapper.get("user_info"))
        else:
            wrapper = _object(raw.get("comment"))
            subtype = wrapper.get("comment_type")
            if type(subtype) is not int or subtype not in (0, 1, 2, 11, 12):
                _fail("unknown_comment_type")
            if subtype != 2:
                skipped += 1
                continue
            comment = _object(wrapper.get("comment"))
            author = _object(comment.get("user"))
        uid, sec = _id(author.get("uid"), field_role='author_uid'), author.get("sec_uid")
        sender_profile = canonical_profile(sec)
        try:
            allowed = profile_allowed(sender_profile)
        except Exception:
            _fail("native_profile_check_failed")
        if allowed is not True:
            skipped += 1
            continue
        if channel == 9:
            visible, hidden = wrapper.get("comment_status"), wrapper.get("comment_unvisible")
        else:
            visible, hidden = comment.get("status"), wrapper.get("comment_unvisible")
        if type(visible) is not int or visible not in (0, 1) or type(hidden) is not int or hidden not in (0, 1):
            # Unknown is not visible. Keep only the already-validated page
            # identity so this one row cannot block later valid notices.
            notices[-1]["skip_reason"] = "native_visibility_unknown"
            skipped += 1
            if channel == 9:
                _observe_unknown_at_visibility(raw, wrapper, nid, probe, diagnostic_observer)
            continue
        created = _timestamp(raw.get("create_time"))
        if visible != 1 or hidden != 0:
            # Explicit unavailable markers are not malformed JSON or an empty
            # successful response.  Keep a minimal notice identity/reason so a
            # stale unavailable item cannot block later valid notifications.
            notices[-1]["skip_reason"] = "native_comment_unavailable"
            skipped += 1
            continue
        if channel == 9:
            video = _id(raw.get("aweme_id"), field_role='at_outer_video_id')
            if _id(_object(wrapper.get("aweme")).get("aweme_id"), field_role='at_inner_video_id') != video:
                _fail("notice_video_mismatch")
            content = _text(wrapper.get("content"))
            cid, hint = _cid_from_schema(wrapper.get("schema_url"), video), None
            source_kind = "native_notice_candidate"
        else:
            video = _id(comment.get("aweme_id"), field_role='reply_video_id')
            if _id(_object(wrapper.get("aweme")).get("aweme_id"), field_role='reply_wrapper_video_id') != video:
                _fail("notice_video_mismatch")
            if "aweme_id" in raw and _id(raw["aweme_id"], field_role='reply_outer_video_id') != video:
                _fail("notice_video_mismatch")
            cid, content = _id(comment.get("cid"), field_role='reply_comment_id'), _text(comment.get("text"))
            hint = wrapper.get("parent_id")
            hint = None if hint is None else _id(hint, zero=True, field_role='reply_parent_hint')
            source_kind = "native_reply_notice_candidate"
        candidates.append({
            "namespace": "douyin_web_v1", "source_kind": source_kind,
            "native_notice_id": nid, "native_comment_id": cid, "notice_type": kind,
            "recipient_uid": receiver_uid, "sender_uid": uid, "sender_sec_uid": sec,
            "sender_profile_url": sender_profile, "receiver_profile_url": receiver_profile,
            "video_id": video, "content": content, "created_time": created,
            "parent_id_hint": hint, "relation_status": "unknown",
            "root_comment_id": None, "parent_comment_id": None, "dispatchable": False,
            **({"at_sub_type": 2} if channel == 9 else {"comment_type": 2}),
        })
    fingerprint = hashlib.sha256(json.dumps(notices, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"candidates": candidates, "notices": notices,
            "fingerprint": fingerprint, "skipped": skipped}


def project_semantic_messages(page, channel, *, receiver_uid, receiver_sec_uid, profile_allowed):
    """Public AT text for meaning only, independent of visibility enum values.

    This projection is never returned by project_page and grants no UI claim.
    The existing candidate/review paths still prove whether execution is possible.
    """
    receiver_uid=_id(receiver_uid, field_role='receiver_uid');receiver_profile=canonical_profile(receiver_sec_uid)
    if type(channel) is not int or channel not in (9,7) or not callable(profile_allowed):
        _fail('invalid_native_channel')
    if channel!=9:return []
    page=_object(page)
    if type(page.get('status_code')) is not int or page['status_code']!=0:_fail('invalid_notice_page')
    rows=page.get('notice_list_v2')
    if type(rows) is not list or len(rows)>100:_fail('invalid_notice_page')
    messages=[];seen=set()
    for raw in rows:
        raw=_object(raw);nid=_id(raw.get('nid_str'), field_role='notice_id')
        if _id(raw.get('user_id'), field_role='recipient_uid')!=receiver_uid:_fail('notice_recipient_mismatch')
        if nid in seen:_fail('duplicate_native_notice_id')
        seen.add(nid);kind=raw.get('type')
        if type(kind) is not int or not 0<=kind<=100000:_fail('invalid_notice_type')
        if kind!=45:continue
        wrapper=_object(raw.get('at'));subtype=wrapper.get('sub_type')
        if type(subtype) is not int:_fail('invalid_notice_type')
        if subtype!=2:continue
        author=_object(wrapper.get('user_info'));uid=_id(author.get('uid'), field_role='author_uid');sec=author.get('sec_uid')
        sender=canonical_profile(sec)
        try:allowed=profile_allowed(sender)
        except Exception:_fail('native_profile_check_failed')
        if allowed is not True:continue
        try:
            video=_id(raw.get('aweme_id'), field_role='at_outer_video_id')
            if _id(_object(wrapper.get('aweme')).get('aweme_id'), field_role='at_inner_video_id')!=video:continue
            content=_text(wrapper.get('content'));cid=_cid_from_schema(wrapper.get('schema_url'),video)
            created=_timestamp(raw.get('create_time'))
        except NativeEventError:continue
        messages.append({'namespace':'douyin_web_v1','source_kind':'native_notice_candidate',
            'native_notice_id':nid,'native_comment_id':cid,'notice_type':45,
            'recipient_uid':receiver_uid,'sender_uid':uid,'sender_sec_uid':sec,
            'sender_profile_url':sender,'receiver_profile_url':receiver_profile,'video_id':video,
            'content':content,'created_time':created,'parent_id_hint':None,'relation_status':'unknown',
            'root_comment_id':None,'parent_comment_id':None,'dispatchable':False,'at_sub_type':2})
    return messages


def project_ui_review_hints(page, channel, *, receiver_uid, receiver_sec_uid, profile_allowed):
    """At most sixteen inert facts for a separate, explicit local UI review.

    This is not the normal candidate projection. Unknown integer visibility
    remains unknown; malformed individual hint content stays quarantined. Page
    identity/type corruption still fails closed. No probe comparison is used.
    """
    receiver_uid = _id(receiver_uid, field_role='receiver_uid')
    receiver_profile = canonical_profile(receiver_sec_uid)
    if type(channel) is not int or channel not in (9, 7) or not callable(profile_allowed):
        _fail('invalid_native_channel')
    if channel != 9:
        return []
    page = _object(page)
    if type(page.get('status_code')) is not int or page['status_code'] != 0:
        _fail('invalid_notice_page')
    rows = page.get('notice_list_v2')
    if type(rows) is not list or len(rows) > 100:
        _fail('invalid_notice_page')
    hints, seen = [], set()
    for raw in rows:
        raw = _object(raw)
        nid, recipient = _id(raw.get('nid_str'), field_role='notice_id'), _id(raw.get('user_id'), field_role='recipient_uid')
        if recipient != receiver_uid:
            _fail('notice_recipient_mismatch')
        if nid in seen:
            _fail('duplicate_native_notice_id')
        seen.add(nid)
        kind = raw.get('type')
        if type(kind) is not int or not 0 <= kind <= 100000:
            _fail('invalid_notice_type')
        if kind != 45:
            continue
        wrapper = _object(raw.get('at'))
        subtype = wrapper.get('sub_type')
        if type(subtype) is not int:
            _fail('invalid_notice_type')
        if subtype != 2:
            continue
        author = _object(wrapper.get('user_info'))
        uid, sec = _id(author.get('uid'), field_role='author_uid'), author.get('sec_uid')
        sender_profile = canonical_profile(sec)
        try:
            allowed = profile_allowed(sender_profile)
        except Exception:
            _fail('native_profile_check_failed')
        if allowed is not True:
            continue  # Do not inspect an unregistered person's body/media.
        visible, hidden = wrapper.get('comment_status'), wrapper.get('comment_unvisible')
        if type(visible) is not int or visible in (0, 1) or type(hidden) is not int or hidden != 0:
            continue
        try:
            video = _id(raw.get('aweme_id'), field_role='at_outer_video_id')
            if _id(_object(wrapper.get('aweme')).get('aweme_id'), field_role='at_inner_video_id') != video:
                continue
            content = _text(wrapper.get('content'))
            cid = _cid_from_schema(wrapper.get('schema_url'), video)
            created = _timestamp(raw.get('create_time'))
            if created is None:
                continue
        except NativeEventError:
            continue  # Not a complete hint; normal unknown skip is unchanged.
        if len(hints) < 16:
            hints.append({
                'namespace': 'douyin_web_v1', 'source_kind': 'native_visibility_review_hint',
                'native_notice_id': nid, 'native_comment_id': cid, 'notice_type': 45,
                'recipient_uid': receiver_uid, 'sender_uid': uid, 'sender_sec_uid': sec,
                'sender_profile_url': sender_profile, 'receiver_profile_url': receiver_profile,
                'video_id': video, 'content': content, 'created_time': created,
                'parent_id_hint': None, 'relation_status': 'unknown',
                'root_comment_id': None, 'parent_comment_id': None, 'dispatchable': False,
                'at_sub_type': 2, 'native_visibility': 'unknown',
                'visible_category': 'other_int', 'hidden_category': 'int0',
            })
    return hints
