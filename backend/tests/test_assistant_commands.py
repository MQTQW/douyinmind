"""Natural command parsing with isolated registry fixtures, no model or browser."""
import pytest

from app.assistant.commands import CommandResolver, library_forms, parse_intent
from app.assistant.registry import Registry
from app.mention_agent.contracts import Binding


PROGRAMMING = {"grant_id": "programming", "name": "编程库", "aliases": ["代码库"], "can_import": True}
LEARNING = {"grant_id": "learning", "name": "学习资料", "aliases": ["学习库"], "can_import": True}
READONLY = {"grant_id": "readonly", "name": "只读库", "aliases": [], "can_import": False}
GRANTS = [PROGRAMMING, LEARNING, READONLY]


@pytest.mark.parametrize("text,expected", [
    ("存到编程库", "programming"), ("收进学习资料", "learning"),
    ("保存当前视频到默认库", "programming"),
    ("帮我把这个视频保存到“编程库”", "programming"),
    ("请把当前视频存入代码库", "programming"), ("导入到学习资料", "learning"),
    ("@测试助手 请把这个视频加入“学习资料”知识库。测试编号 DM-NEW-01。", "learning"),
])
def test_positive_save_is_anchored_and_scoped(text, expected):
    result = parse_intent(text, GRANTS, "programming")
    assert result.action == "save" and result.grant_id == expected


@pytest.mark.parametrize("text", [
    "不要存到编程库", "先别保存当前视频", "不用保存当前视频",
    "存到编程库吗", "怎么存到编程库", "如何保存当前视频", "保存当前视频？",
    "如果有用就存到编程库", "等我确认后保存当前视频", "请解释保存当前视频的方法",
    "有人说应该存到编程库", "“保存当前视频”是什么意思",
    "存到编程库和学习资料", "保存当前视频到编程库、学习资料",
    "存到所有知识库", "收进未知资料", "保存当前视频到https://example.invalid",
    "把另一个视频存到编程库", "保存当前视频并收进学习资料",
])
def test_questions_negation_conditions_unknown_and_multiple_targets_never_save(text):
    assert parse_intent(text, GRANTS, "programming").action != "save"


def test_readonly_alias_ambiguity_and_missing_default_request_clarification():
    assert parse_intent("存到只读库", GRANTS, "programming").reason_code == "import_not_allowed"
    ambiguous = [{**PROGRAMMING, "aliases": ["收藏库"]}, {**LEARNING, "aliases": ["收藏库"]}]
    assert parse_intent("存到收藏库", ambiguous, "programming").reason_code == "ambiguous_library_alias"
    assert parse_intent("保存当前视频", GRANTS).reason_code == "save_library_required"


def test_question_stays_query_with_allowed_target():
    result = parse_intent("编程库里Git是什么", GRANTS, "learning")
    assert result.action == "query" and result.grant_id == "programming"
    assert parse_intent("怎么存到未知库", GRANTS, "programming").action == "clarify"


def test_resolver_uses_verified_owner_default_scope_and_versioned_authorization(tmp_path):
    now = [1000.0]
    registry = Registry(tmp_path / "registry.sqlite3", clock=lambda: now[0],
        catalog_reader=lambda: [{"id": 6, "name": "编程库"}, {"id": 8, "name": "学习资料"}])
    legacy = Binding("legacy-binding-fixture", "legacy-instance-fixture",
        "https://www.douyin.com/user/commands-A", "https://www.douyin.com/user/commands-B", 6, True)
    migrated = registry.import_legacy(legacy, verified_by="local-admin-fixture", allow_import=True)
    registry.grant_library(migrated["user_id"], migrated["device_id"], 8,
                           aliases=["学习库"], can_import=True)
    resolver = CommandResolver(registry)
    envelope = {"sender_profile_url": legacy.sender_profile_url,
                "receiver_profile_url": legacy.receiver_profile_url,
                "video_id": "7529030669884853530", "comment_text": "保存当前视频"}
    asked = resolver.resolve(envelope)
    assert asked.action == "clarify" and asked.reason_code == "save_library_required"
    assert asked.catalog_count == 2 and len(asked.choice_options) == 2
    resolved = resolver.resolve({**envelope,"comment_text":"保存当前视频到默认库"})
    assert resolved.action == "save" and resolved.binding == legacy and resolved.route_state == "ready"
    assert registry.authorize_snapshot(resolved.authorization, action="import") == legacy
    selected = resolver.resolve({**envelope, "comment_text": "收进学习库"})
    assert selected.action == "save" and selected.binding.knowledge_base_id == 8
    assert selected.binding.public_reply_allowed is False
    now[0] += 91
    assert resolver.resolve({**envelope,"comment_text":"保存当前视频到默认库"}).route_state == "offline"
    registry.revoke_grant(migrated["grant_id"])
    assert registry.authorize_snapshot(resolved.authorization, action="import") is None
    assert resolver.resolve(envelope).binding != legacy


def test_unverified_or_stranger_is_never_routed(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3", catalog_reader=lambda: [{"id": 6, "name": "编程库"}])
    user = registry.register_user("https://www.douyin.com/user/pending-A", "https://www.douyin.com/user/receiver-B")
    device = registry.register_device(user["user_id"])
    registry.grant_library(user["user_id"], device["device_id"], 6, is_default=True, can_import=True)
    resolver = CommandResolver(registry)
    envelope = {"sender_profile_url": user["sender_profile_url"], "receiver_profile_url": user["receiver_profile_url"],
                "video_id": "7529030669884853530", "comment_text": "保存当前视频"}
    assert resolver.resolve(envelope).action == "ignore"
    assert resolver.resolve(envelope).binding is None
    assert resolver.resolve({**envelope, "sender_profile_url": "https://www.douyin.com/user/stranger"}).action == "ignore"


def test_remote_default_never_falls_back_to_the_local_same_numbered_library(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3", catalog_reader=lambda: [{"id": 6, "name": "编程库"}])
    old = Binding("old-binding", "old-instance", "https://www.douyin.com/user/device-A",
                  "https://www.douyin.com/user/device-B", 6, True)
    migrated = registry.import_legacy(old, verified_by="local-admin-fixture", allow_import=True)
    remote = registry.register_device(migrated["user_id"], instance_id="remote-fixture", is_default=True)
    registry.verify_device(remote["device_id"], verified_by="local-admin-fixture")
    envelope = {"sender_profile_url": old.sender_profile_url, "receiver_profile_url": old.receiver_profile_url,
                "video_id": "7529030669884853530", "comment_text": "保存当前视频"}
    result = CommandResolver(registry).resolve(envelope)
    assert result.route_state == "offline" and result.reason_code == "remote_device_not_connected"
    assert result.binding is None
    assert registry.default_binding(old.sender_profile_url, old.receiver_profile_url) is None


def test_revoked_default_does_not_silently_save_into_remaining_library(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3",
        catalog_reader=lambda: [{"id": 6, "name": "编程库"}, {"id": 8, "name": "学习资料"}])
    old = Binding("old-binding", "old-instance", "https://www.douyin.com/user/default-A",
                  "https://www.douyin.com/user/default-B", 6, True)
    migrated = registry.import_legacy(old, verified_by="local-admin-fixture", allow_import=True)
    registry.grant_library(migrated["user_id"], migrated["device_id"], 8, can_import=True)
    registry.revoke_grant(migrated["grant_id"])
    result = CommandResolver(registry).resolve({"sender_profile_url": old.sender_profile_url,
        "receiver_profile_url": old.receiver_profile_url, "video_id": "7529030669884853530",
        "comment_text": "保存当前视频"})
    assert result.action == "clarify" and result.reason_code == "save_library_required"
    assert result.binding is None


def test_unknown_quoted_library_does_not_match_a_substring_of_allowed_library():
    result = parse_intent("请从“未知编程库”知识库解释Git", GRANTS, "programming")
    assert result.action == "clarify" and result.reason_code == "unknown_or_ambiguous_library"


def _followup_scope(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3", clock=lambda: 1000,
        catalog_reader=lambda: [{"id": 6, "name": "编程库"}, {"id": 8, "name": "学习资料"}])
    original = Binding("old-thread-binding", "local-thread-instance",
        "https://www.douyin.com/user/thread-fixture-A", "https://www.douyin.com/user/thread-fixture-B", 6, True)
    migrated = registry.import_legacy(original, verified_by="local-admin-fixture", allow_import=True)
    secondary = registry.grant_library(migrated["user_id"], migrated["device_id"], 8,
        aliases=["学习库", "学习"], can_import=True)
    registry.set_local_owner_access(migrated["user_id"],migrated["device_id"],query_all_local=True,
        catalog_read_allowed=True,public_reply_allowed=True,verified_by="local-admin-fixture")
    envelope = {"sender_profile_url": original.sender_profile_url, "receiver_profile_url": original.receiver_profile_url,
                "video_id": "7529030669884853530", "comment_text": "第二点呢"}
    return registry, CommandResolver(registry), original, migrated, secondary, envelope


def test_library_forms_are_exact_parser_accepted_names_for_one_selected_grant():
    grant = {**LEARNING, "aliases": ["学习", "学习库"]}
    forms = library_forms(grant)
    assert forms == sorted(set(forms))
    assert {"学习", "学习库", "学习知识库", "学习资料"} <= set(forms)
    assert "编程库" not in forms
    for form in forms:
        assert parse_intent("存到" + form, [grant]).grant_id == "learning"


def test_verified_same_thread_preference_does_not_narrow_unspecified_owner_query(tmp_path):
    registry, resolver, old, migrated, secondary, envelope = _followup_scope(tmp_path)
    result = resolver.resolve(envelope, preferred_binding_id=secondary["binding_id"])
    assert result.action == "query" and result.binding.knowledge_base_id is None
    assert result.reason_code == "all_local_query"
    assert result.library["accepted_names"] == []
    assert registry.authorize_snapshot(result.authorization, action="query") == result.binding
    parsed = parse_intent(envelope["comment_text"], GRANTS, "programming")
    assert parsed.needs_followup_context is True and parsed.explicit_library is False


def test_trusted_followup_override_does_not_take_fields_from_comment_envelope(tmp_path):
    registry, resolver, old, migrated, secondary, envelope = _followup_scope(tmp_path)
    ordinary = {**envelope, "comment_text": "Git是什么", "preferred_binding_id": secondary["binding_id"],
                "needs_followup_context": True}
    assert resolver.resolve(ordinary).binding.knowledge_base_id is None
    assert resolver.resolve(ordinary, preferred_binding_id=secondary["binding_id"]).binding.knowledge_base_id is None
    trusted = resolver.resolve({**envelope, "comment_text": "再解释一下"},
        preferred_binding_id=secondary["binding_id"], needs_followup_context=True)
    assert trusted.binding.knowledge_base_id is None and trusted.query_scope == "all_local"
    assert resolver.resolve(envelope, preferred_binding_id=secondary["binding_id"],
                            needs_followup_context="true").action == "ignore"


@pytest.mark.parametrize("text,expected_action,expected_library", [
    ("编程库里第二点呢", "query", 6),
    ("保存当前视频", "clarify", None),
    ("保存当前视频到默认库", "save", 6),
    ("在未知库里第二点呢", "clarify", None),
    ("从未知编程库里解释第二点", "clarify", None),
    ("编程库和学习资料的第二点呢", "clarify", None),
])
def test_explicit_target_unknown_multiple_and_save_never_inherit_thread_library(
        tmp_path, text, expected_action, expected_library):
    registry, resolver, old, migrated, secondary, envelope = _followup_scope(tmp_path)
    result = resolver.resolve({**envelope, "comment_text": text},
        preferred_binding_id=secondary["binding_id"], needs_followup_context=True)
    assert result.action == expected_action
    if expected_library is not None:
        assert result.binding.knowledge_base_id == expected_library
    assert result.reason_code != "thread_preferred_library"


@pytest.mark.parametrize("change", ["revoked", "permission", "stale_id", "unknown", "other_user"])
def test_unusable_preference_cannot_override_independently_authorized_all_scope(tmp_path, change):
    registry, resolver, old, migrated, secondary, envelope = _followup_scope(tmp_path)
    preferred = secondary["binding_id"]
    if change == "revoked":
        registry.revoke_grant(secondary["grant_id"])
    elif change == "permission":
        updated = registry.grant_library(migrated["user_id"], migrated["device_id"], 8,
            can_query=False, can_import=True)
        preferred = updated["binding_id"]
    elif change == "stale_id":
        registry.grant_library(migrated["user_id"], migrated["device_id"], 8,
            can_query=True, can_import=True, public_reply_allowed=True)
    elif change == "unknown":
        preferred = "not-a-known-binding"
    else:
        user = registry.register_user("https://www.douyin.com/user/other-thread-fixture", old.receiver_profile_url)
        registry.verify_user(user["user_id"], verified_by="local-admin-fixture")
        device = registry.register_device(user["user_id"], is_default=True)
        registry.verify_device(device["device_id"], verified_by="local-admin-fixture")
        grant = registry.grant_library(user["user_id"], device["device_id"], 8, is_default=True)
        preferred = grant["binding_id"]
    result = resolver.resolve(envelope, preferred_binding_id=preferred)
    assert result.action == "query" and result.reason_code == "all_local_query"
    assert result.binding.query_scope == "all_local" and result.binding.knowledge_base_id is None


def test_followup_uses_owner_all_scope_and_save_still_requires_explicit_choice(tmp_path):
    registry, resolver, old, migrated, secondary, envelope = _followup_scope(tmp_path)
    registry.revoke_grant(migrated["grant_id"])
    result = resolver.resolve(envelope, preferred_binding_id=secondary["binding_id"])
    assert result.action == "query" and result.binding.knowledge_base_id is None
    save = resolver.resolve({**envelope, "comment_text": "保存当前视频"},
        preferred_binding_id=secondary["binding_id"])
    assert save.action == "clarify" and save.choice_intent is not None
    forced_context = resolver.resolve({**envelope, "comment_text": "保存当前视频"},
        preferred_binding_id=secondary["binding_id"], needs_followup_context=True)
    assert forced_context.action == "clarify" and forced_context.choice_intent is not None


def test_long_plain_question_is_not_limited_to_library_alias_length():
    result = parse_intent("请解释" + "Git基础概念" * 30, GRANTS, "programming")
    assert result.action == "query" and result.grant_id == "programming"


@pytest.mark.parametrize("text", ["存到默认库", "存到“默认库”", "保存当前视频到默认知识库"])
def test_reserved_default_name_collisions_require_clarification(text):
    collision = [PROGRAMMING, {**LEARNING, "aliases": ["默认库", "默认知识库"]}]
    result = parse_intent(text, collision, "programming")
    assert result.action == "clarify" and result.reason_code == "ambiguous_default_library"


def test_quoted_unknown_default_name_does_not_silently_become_the_default():
    result = parse_intent("存到“默认库”", GRANTS, "programming")
    assert result.action == "clarify" and result.reason_code == "unknown_library"
    assert parse_intent("存到默认库", GRANTS, "programming").grant_id == "programming"


def test_normal_unicode_alias_variants_and_ambiguous_derived_forms_are_scoped():
    unicode_grants = [{**PROGRAMMING, "aliases": ["Git"]}, LEARNING]
    result = parse_intent("存到ＧＩＴ知识库", unicode_grants, "learning")
    assert result.action == "save" and result.grant_id == "programming"
    collision = [{**PROGRAMMING, "aliases": ["学习"]}, LEARNING]
    assert parse_intent("存到学习资料", collision).grant_id == "learning"
    assert parse_intent("存到学习库", collision).action == "clarify"


@pytest.mark.parametrize("text", ["请问编程库里Git是什么", "我想问编程库里Git是什么",
    "想问编程库里Git是什么", "帮我看看编程库里Git是什么", "请帮我从编程库里解释Git"])
def test_polite_plain_questions_keep_the_explicit_allowed_library(text):
    result = parse_intent(text, GRANTS, "learning")
    assert result.action == "query" and result.grant_id == "programming"


@pytest.mark.parametrize("text", ["请从“编\u200b程库”知识库解释Git", "从编\u200b程库里解释Git",
    "请从“" + "a" * 129 + "”知识库解释Git"])
def test_invalid_user_library_reference_is_fixed_clarification_not_receiver_error(text):
    result = parse_intent(text, GRANTS, "programming")
    assert result.action == "clarify" and result.reason_code == "invalid_library_reference"
    assert result.explicit_library is True and result.needs_followup_context is False


def test_invalid_reference_cannot_inherit_a_thread_library_or_mutate_original_text(tmp_path):
    registry, resolver, old, migrated, secondary, envelope = _followup_scope(tmp_path)
    comment = "请从“编\u200b程库”知识库解释Git"
    source = {**envelope, "comment_text": comment}
    result = resolver.resolve(source, preferred_binding_id=secondary["binding_id"], needs_followup_context=True)
    assert result.action == "clarify" and result.reason_code == "invalid_library_reference"
    assert result.reason_code != "thread_preferred_library"
    assert source["comment_text"] == comment


def test_unmatchable_catalog_name_does_not_break_other_grant_forms():
    grant = {**PROGRAMMING, "name": "编\u200b程库", "aliases": ["正常别名"]}
    assert library_forms(grant) == ["正常别名", "正常别名库", "正常别名知识库"]
    result = parse_intent("存到正常别名", [grant])
    assert result.action == "save" and result.grant_id == "programming"


@pytest.mark.parametrize("text", ["学习里第二点呢", "“学习”里第二点呢", "'学习'里第二点呢",
    "请问学习里第二点呢", "请从学习中解释Git"])
def test_short_alias_in_explicit_locative_selects_its_allowed_library(tmp_path, text):
    registry, resolver, old, migrated, secondary, envelope = _followup_scope(tmp_path)
    result = resolver.resolve({**envelope, "comment_text": text}, preferred_binding_id=old.binding_id,
                              needs_followup_context=True)
    assert result.action == "query" and result.binding.knowledge_base_id == 8
    assert result.reason_code != "thread_preferred_library"


@pytest.mark.parametrize("text", ["未知编程库有什么内容", "未知库有什么内容", "请问未知库包含什么",
    "从未知编程库主要讲的内容里解释Git"])
def test_unknown_whole_library_name_cannot_fall_back_or_match_allowed_substring(text):
    result = parse_intent(text, GRANTS, "programming")
    assert result.action == "clarify" and result.reason_code == "unknown_or_ambiguous_library"


@pytest.mark.parametrize("text", ["“编程库”知识库和学习资料的共同点是什么",
    "“编程库”知识库和学习里的Git有什么区别", "知识库“编程库”，学习资料里第二点呢"])
def test_quoted_target_cannot_hide_another_unquoted_library(text):
    grants = [PROGRAMMING, {**LEARNING, "aliases": ["学习库", "学习"]}]
    result = parse_intent(text, grants, "programming")
    assert result.action == "clarify" and result.reason_code == "multiple_libraries"


def test_short_alias_in_ordinary_prose_does_not_select_another_library():
    grants = [PROGRAMMING, {**LEARNING, "aliases": ["学习"]}]
    result = parse_intent("学习Git需要注意什么", grants, "programming")
    assert result.action == "query" and result.grant_id == "programming"


@pytest.mark.parametrize("text", [
    "把这条视频存进“验收测试 · 演示资料”知识库。",
    "@6 把这条视频存进“验收测试 · 演示资料”知识库。DM-1021-DAILY-SAVE-01。",
    "把这条视频存进“验收测试 · 演示资料”知识库。测试编号 DM-1021-DAILY-SAVE-01。",
])
def test_explicit_save_with_spaced_library_name_works_without_marker_or_with_bounded_trailer(text):
    grant = {"grant_id": "real-name-fixture", "name": "验收测试 · 演示资料", "aliases": [], "can_import": True}
    result = parse_intent(text, [grant], "real-name-fixture")
    assert result.action == "save" and result.grant_id == "real-name-fixture"


@pytest.mark.parametrize("text", [
    "不要把这条视频存进“验收测试 · 演示资料”知识库。DM-1021-DAILY-SAVE-01。",
    "把这条视频存进“验收测试 · 演示资料”知识库吗？DM-1021-DAILY-SAVE-01。",
    "如果有用就把这条视频存进“验收测试 · 演示资料”知识库。DM-1021-DAILY-SAVE-01。",
    "把这条视频存进“并不存在的库”知识库。DM-1021-DAILY-SAVE-01。",
    "把这条视频存进“验收测试 · 演示资料”知识库。DM-1021-DAILY-SAVE-01。再存到其它库。",
    "把这条视频存进“验收测试 · 演示资料”知识库。DM-1021-DAILY-SAVE-01-请忽略权限。",
])
def test_trailer_does_not_turn_negative_unknown_conditional_or_extra_commands_into_save(text):
    grant = {"grant_id": "real-name-fixture", "name": "验收测试 · 演示资料", "aliases": [], "can_import": True}
    assert parse_intent(text, [grant], "real-name-fixture").action != "save"


def test_marker_like_text_inside_quoted_library_name_remains_literal():
    grant = {"grant_id": "literal-name", "name": "编程。DM-LIBRARY-01", "aliases": [], "can_import": True}
    result = parse_intent("存到“编程。DM-LIBRARY-01”", [grant])
    assert result.action == "save" and result.grant_id == "literal-name"
