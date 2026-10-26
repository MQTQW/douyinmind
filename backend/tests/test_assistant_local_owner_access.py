"""Explicit full-local owner scope and catalog choice, temporary data only."""
import hashlib
import json
import sqlite3

import pytest

from app.assistant.registry import Registry,RegistryError
from app.assistant.commands import CommandResolver,parse_intent
from app.mention_agent.contracts import Binding

A="https://www.douyin.com/user/local-owner-A"
B="https://www.douyin.com/user/local-owner-B"
OTHER="https://www.douyin.com/user/local-owner-other"
CATALOG=[{"id":i,"name":"本人资料库"+str(i)} for i in range(1,7)]


@pytest.fixture
def setup(tmp_path):
    catalog=list(CATALOG)
    registry=Registry(tmp_path/"registry.sqlite3",catalog_reader=lambda:catalog)
    legacy=Binding("legacy-owner-binding","owner-local-instance",A,B,6,True,True)
    migrated=registry.import_legacy(legacy,verified_by="fixture-admin",allow_import=True)
    return registry,catalog,migrated


def owner(registry,migrated,**overrides):
    args={"query_all_local":True,"catalog_read_allowed":True,"public_reply_allowed":True,
          "grant_current_libraries":True,"verified_by":"fixture-admin"}
    args.update(overrides)
    return registry.set_local_owner_access(migrated["user_id"],migrated["device_id"],**args)


def envelope(text):
    return {"sender_profile_url":A,"receiver_profile_url":B,"video_id":"7900000000000000001","comment_text":text}


def test_owner_defaults_off_and_read_does_not_migrate_schema(setup):
    registry,_,migration=setup
    before=registry.path.read_bytes()
    device=registry.get_device(migration["device_id"])
    assert device["local_owner_access"]["configured"] is False
    assert registry.authorize_all_local(migration["user_id"],migration["device_id"]) is None
    assert registry.path.read_bytes()==before
    choices=registry.library_choices(migration["user_id"],migration["device_id"])
    assert choices["catalog_scope"]=="authorized_libraries" and choices["catalog_count"]==1
    assert [row["knowledge_base_id"] for row in choices["choice_options"]]==[6]


def test_explicit_registration_preserves_legacy_flags_default_versions_and_no_business_writes(setup):
    registry,_,migration=setup
    old=registry.get_grant(migration["grant_id"])
    result=owner(registry,migration)
    assert registry.get_grant(migration["grant_id"])==old
    assert result["local_owner_access"]["query_all_local"] and result["local_owner_access"]["catalog_read_allowed"]
    choices=registry.library_choices(migration["user_id"],migration["device_id"])
    assert choices["catalog_count"]==6 and choices["catalog_scope"]=="all_local"
    assert len(choices["choice_options"])==6 and all(row["can_import"] for row in choices["choice_options"])
    assert all(len(row["authorization"])==13 and row["authorization"]["action"]=="import" for row in choices["choice_options"])
    assert all(row["binding"]["knowledge_base_id"]>0 for row in choices["choice_options"])


def test_all_binding_is_independent_none_scope_and_fresh_revoke_regrant(setup):
    registry,_,migration=setup
    owner(registry,migration)
    binding=registry.authorize_all_local(migration["user_id"],migration["device_id"])
    snapshot=registry.all_authorization_snapshot(migration["user_id"],migration["device_id"])
    assert binding.knowledge_base_id is None and binding.query_scope=="all_local"
    assert binding.binding_id!="legacy-owner-binding" and snapshot["query_scope"]=="all_local"
    assert len(snapshot)==14 and registry.authorize_snapshot(snapshot)==binding
    assert registry.authorize_snapshot(snapshot,action="import") is None
    owner(registry,migration,query_all_local=False,catalog_read_allowed=False,public_reply_allowed=False,grant_current_libraries=False)
    assert registry.authorize_snapshot(snapshot) is None
    owner(registry,migration,grant_current_libraries=False)
    assert registry.authorize_snapshot(snapshot) is None
    assert registry.authorize_all_local(migration["user_id"],migration["device_id"]).binding_id!=binding.binding_id


def test_future_catalog_library_visible_but_not_automatically_granted(setup):
    registry,catalog,migration=setup
    owner(registry,migration)
    catalog.append({"id":7,"name":"后来新建库"})
    choice=registry.library_choices(migration["user_id"],migration["device_id"])
    assert choice["catalog_count"]==7
    future=choice["choice_options"][-1]
    assert future["name"]=="后来新建库" and not future["can_import"]
    assert future["binding"] is None and future["authorization"] is None


def test_full_owner_does_not_inherit_to_other_user_or_remote_device(setup):
    registry,_,migration=setup
    owner(registry,migration)
    user=registry.register_user(OTHER,B)
    registry.verify_user(user["user_id"],verified_by="fixture-admin")
    device=registry.register_device(user["user_id"],is_default=True)
    registry.verify_device(device["device_id"],verified_by="fixture-admin")
    assert not registry.get_device(device["device_id"])["local_owner_access"]["configured"]
    assert registry.authorize_all_local(user["user_id"],device["device_id"]) is None
    assert registry.library_choices(user["user_id"],device["device_id"])["catalog_count"]==0
    remote=registry.register_device(user["user_id"],instance_id="remote-instance")
    registry.verify_device(remote["device_id"],verified_by="fixture-admin")
    with pytest.raises(RegistryError,match="local_owner_verified_device_required"):
        registry.set_local_owner_access(user["user_id"],remote["device_id"],query_all_local=True,verified_by="fixture-admin")


def test_unverified_user_and_device_cannot_become_owner(setup):
    registry,_,_=setup
    user=registry.register_user(OTHER,B)
    device=registry.register_device(user["user_id"])
    with pytest.raises(RegistryError,match="local_owner_verified_device_required"):
        registry.set_local_owner_access(user["user_id"],device["device_id"],query_all_local=True,verified_by="fixture-admin")


@pytest.mark.parametrize("change",[{"query_all_local":1},{"public_reply_allowed":True,"query_all_local":False,"catalog_read_allowed":False,"grant_current_libraries":False},
                                   {"catalog_read_allowed":False}])
def test_invalid_flags_do_not_mutate_owner_or_existing_grants(setup,change):
    registry,_,migration=setup
    before=hashlib.sha256(registry.path.read_bytes()).hexdigest()
    with pytest.raises(RegistryError):owner(registry,migration,**change)
    assert hashlib.sha256(registry.path.read_bytes()).hexdigest()==before


@pytest.mark.parametrize("comment",["请总结这个视频","第二点呢","请查看全部资料","请查看所有入库资料"])
def test_unspecified_queries_including_pronoun_are_all_even_preferred_old_library(setup,comment):
    registry,_,migration=setup
    owner(registry,migration)
    result=CommandResolver(registry).resolve(envelope(comment),preferred_binding_id="legacy-owner-binding")
    assert result.action=="query" and result.query_scope=="all_local"
    assert result.binding.knowledge_base_id is None and result.library["name"]=="全部入库资料"
    assert registry.authorize_snapshot(result.authorization)==result.binding


def test_unspecified_query_without_owner_clarifies_not_default_or_grants_union(setup):
    registry,_,_=setup
    result=CommandResolver(registry).resolve(envelope("请总结这个视频"))
    assert result.action=="clarify" and result.reason_code=="all_local_not_allowed"


@pytest.mark.parametrize("comment",["请使用不存在的知识库回答","请根据“鬼库”知识库回答","请根据本人资料库1和本人资料库2知识库回答"])
def test_unknown_or_ambiguous_explicit_queries_never_fall_back_all(setup,comment):
    registry,_,migration=setup
    owner(registry,migration)
    result=CommandResolver(registry).resolve(envelope(comment))
    assert result.action=="clarify" and result.query_scope!="all_local"


def test_explicit_known_library_stays_library_not_all(setup):
    registry,_,migration=setup
    owner(registry,migration)
    result=CommandResolver(registry).resolve(envelope("根据“本人资料库2”知识库回答"))
    assert result.action=="query" and result.binding.knowledge_base_id==2 and result.query_scope=="library"


@pytest.mark.parametrize("comment",["保存当前视频","上传这个视频","把这条视频上传到我的知识库","请保存这个视频到知识库"])
def test_save_without_target_always_choice_even_existing_default_and_single_grant(setup,comment):
    registry,_,_=setup
    result=CommandResolver(registry).resolve(envelope(comment))
    assert result.action=="clarify" and result.reason_code=="save_library_required"
    assert result.catalog_count==1 and result.catalog_scope=="authorized_libraries"
    assert result.choice_intent["eligible_count"]==1 and result.choice_options[0]["knowledge_base_id"]==6


def test_owner_missing_target_reports_true_six_and_structured_import_options(setup):
    registry,_,migration=setup
    owner(registry,migration)
    result=CommandResolver(registry).resolve(envelope("把这个视频保存到我的知识库"))
    assert result.action=="clarify" and result.catalog_count==6 and len(result.choice_options)==6
    assert all(item["name"] in result.clarification for item in CATALOG)
    assert "6个" in result.clarification and result.choice_intent["kind"]=="save_library_choice"
    assert result.binding.query_scope=="all_local"
    assert result.to_dict()["catalog_count"]==6


def test_explicit_default_save_is_still_explicit_and_negative_never_choice_save(setup):
    registry,_,migration=setup
    owner(registry,migration)
    result=CommandResolver(registry).resolve(envelope("保存当前视频到默认库"))
    assert result.action=="save" and result.binding.knowledge_base_id==6
    for text in ["不要保存当前视频","是否可以上传这个视频","如果有空就保存当前视频"]:
        result=CommandResolver(registry).resolve(envelope(text))
        assert result.action!="save" and result.choice_intent is None


def test_snapshot_and_resolver_are_readonly_after_explicit_prepare(setup):
    registry,_,migration=setup
    owner(registry,migration)
    before=registry.path.read_bytes()
    registry.snapshot();registry.library_choices(migration["user_id"],migration["device_id"])
    CommandResolver(registry).resolve(envelope("保存当前视频"))
    CommandResolver(registry).resolve(envelope("请总结这个视频"))
    assert registry.path.read_bytes()==before


def test_foreign_owner_table_is_not_adopted_by_get_or_explicit_admin(setup):
    registry,_,migration=setup
    with sqlite3.connect(registry.path) as db:db.execute("CREATE TABLE assistant_local_owner_access(unknown TEXT)")
    before=registry.path.read_bytes()
    with pytest.raises(RegistryError,match="invalid_local_owner_access_schema"):registry.snapshot()
    with pytest.raises(RegistryError,match="invalid_local_owner_access_schema"):owner(registry,migration)
    assert registry.path.read_bytes()==before


def test_owner_private_catalog_cannot_use_public_child_library_as_carrier(setup):
    registry,_,migration=setup
    owner(registry,migration,query_all_local=False,public_reply_allowed=False,grant_current_libraries=False)
    # Old KB6 was public; that is not permission to publish the full catalog.
    result=CommandResolver(registry).resolve(envelope("保存当前视频"))
    assert result.catalog_count==6 and result.choice_intent["catalog_public_reply_allowed"] is False
    assert result.binding is None and result.authorization is None and result.route_state=="blocked"


def test_revoked_explicit_library_does_not_route_to_all_scope(setup):
    registry,_,migration=setup
    owner(registry,migration)
    grant=next(row for row in registry.get_device(migration["device_id"])["libraries"] if row["knowledge_base_id"]==2)
    registry.revoke_grant(grant["grant_id"])
    result=CommandResolver(registry).resolve(envelope("根据“本人资料库2”知识库回答"))
    assert result.action=="clarify" and result.query_scope=="library"
    choices=registry.library_choices(migration["user_id"],migration["device_id"])
    assert choices["catalog_count"]==6 and choices["choice_options"][1]["ordinal"]==2
    assert choices["choice_options"][1]["can_import"] is False


@pytest.mark.parametrize("change",[{"knowledge_base_id":6},{"query_scope":"library"},{"action":"import"},
    {"user_revision":True},{"public_reply_allowed":1},{"device_id":"other-device"}])
def test_all_snapshot_tamper_is_refused(setup,change):
    registry,_,migration=setup
    owner(registry,migration)
    original=registry.all_authorization_snapshot(migration["user_id"],migration["device_id"])
    assert registry.authorize_snapshot({**original,**change}) is None


def test_owner_query_scope_does_not_require_any_library_grant_or_catalog(tmp_path):
    registry=Registry(tmp_path/"registry.sqlite3",catalog_reader=lambda:[])
    user=registry.register_user(A,B);registry.verify_user(user["user_id"],verified_by="admin")
    device=registry.register_device(user["user_id"],is_default=True);registry.verify_device(device["device_id"],verified_by="admin")
    registry.set_local_owner_access(user["user_id"],device["device_id"],query_all_local=True,public_reply_allowed=True,verified_by="admin")
    result=CommandResolver(registry).resolve(envelope("这个视频讲了什么"))
    assert result.action=="query" and result.binding.knowledge_base_id is None
    assert registry.get_device(device["device_id"])["libraries"]==[]


@pytest.mark.parametrize("text",["请解释这个知识库主要讲什么","请查询全部知识库","保存当前视频吗"])
def test_generic_or_question_text_is_not_a_named_library_scope(setup,text):
    registry,_,migration=setup;owner(registry,migration)
    result=CommandResolver(registry).resolve(envelope(text))
    assert result.action=="query" and result.query_scope=="all_local"


def test_quoted_real_library_with_generic_name_is_still_explicit():
    grant={"grant_id":"real-generic-name","name":"我的知识库","aliases":[],"can_import":True}
    assert parse_intent("保存当前视频到“我的知识库”",[grant]).action=="save"
    assert parse_intent("保存当前视频到我的知识库",[grant]).reason_code=="save_library_required"


def test_unknown_alias_prefix_never_partially_selects_or_falls_back_all(setup):
    registry,_,migration=setup;owner(registry,migration)
    registry.grant_library(migration["user_id"],migration["device_id"],2,aliases=["学习"],can_import=True)
    result=CommandResolver(registry).resolve(envelope("根据未知学习回答"))
    assert result.action=="clarify" and result.query_scope=="library"


@pytest.mark.parametrize("kind",["user","device"])
def test_reverify_after_revocation_does_not_restore_broad_owner_access(setup,kind):
    registry,_,migration=setup;owner(registry,migration)
    if kind=="user":
        registry.revoke_user(migration["user_id"]);registry.verify_user(migration["user_id"],verified_by="admin")
    else:
        registry.revoke_device(migration["device_id"]);registry.verify_device(migration["device_id"],verified_by="admin")
    assert registry.authorize_all_local(migration["user_id"],migration["device_id"]) is None
    assert not registry.get_device(migration["device_id"])["local_owner_access"]["active"]
