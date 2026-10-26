"""Ordinary source ranges versus literal library targets, isolated Registry."""
from copy import deepcopy

import pytest

from app.assistant.commands import CommandResolver, parse_intent
from app.assistant.registry import Registry
from app.mention_agent.contracts import Binding


EXACT = '@6 根据我的已入库资料，解释 Git 暂存区的作用，并给出两条依据。DM-1023-MIX-QA-01'
CATALOG = [{'id': 1, 'name': '111'}, {'id': 2, 'name': '1'}, {'id': 3, 'name': '多久时间'},
           {'id': 4, 'name': '全部收藏'}, {'id': 5, 'name': '测试长视频'},
           {'id': 6, 'name': '验收测试 · 演示资料'}]


def scope(tmp_path):
    catalog = deepcopy(CATALOG)
    registry = Registry(tmp_path/'registry.sqlite3', catalog_reader=lambda: deepcopy(catalog))
    original = Binding('generic-scope-old', 'generic-scope-instance',
        'https://www.douyin.com/user/generic-scope-A', 'https://www.douyin.com/user/generic-scope-B', 6, True, True)
    migration = registry.import_legacy(original, verified_by='synthetic-owner', allow_import=True)
    registry.set_local_owner_access(migration['user_id'], migration['device_id'], query_all_local=True,
        catalog_read_allowed=True, public_reply_allowed=True, grant_current_libraries=True, verified_by='synthetic-owner')
    envelope = {'sender_profile_url': original.sender_profile_url, 'receiver_profile_url': original.receiver_profile_url,
                'video_id': '8999999000000000001', 'comment_text': ''}
    return registry, CommandResolver(registry), original, migration, catalog, envelope


@pytest.mark.parametrize('text', [EXACT, '根据已入库资料，解释Git暂存区。',
    '从本人已入库资料里解释Git。', '根据本人的已入库资料，解释Git。'])
def test_ordinary_ingested_source_range_is_not_an_unknown_library(tmp_path, text):
    registry, resolver, old, migration, catalog, envelope = scope(tmp_path)
    original = {**envelope, 'comment_text': text}; before = deepcopy(original)
    device = registry.get_device(migration['device_id'])
    parsed = parse_intent(text, device['libraries'], migration['grant_id'])
    assert parsed.action == 'query' and parsed.explicit_library is False
    result = resolver.resolve(original)
    assert result.action == 'query' and result.reason_code == 'all_local_query'
    assert result.query_scope == 'all_local' and result.binding.knowledge_base_id is None
    assert registry.authorize_snapshot(result.authorization, action='query') == result.binding
    assert original == before


@pytest.mark.parametrize('text', ['根据我的投资资料解释Git。', '从“未授权库”知识库查询Git。',
    '根据“我的已入库资料”知识库解释Git。', '根据我的未知编程库解释Git。'])
def test_unknown_explicit_library_is_not_erased_by_aggregate_range_vocabulary(tmp_path, text):
    registry, resolver, old, migration, catalog, envelope = scope(tmp_path)
    result = resolver.resolve({**envelope, 'comment_text': text})
    assert result.action == 'clarify'
    assert result.reason_code in {'unknown_or_ambiguous_library', 'unknown_library'}
    assert result.query_scope != 'all_local'


def test_aggregate_phrase_still_requires_the_separate_owner_all_source_permission(tmp_path):
    registry, resolver, old, migration, catalog, envelope = scope(tmp_path)
    registry.set_local_owner_access(migration['user_id'], migration['device_id'], query_all_local=False,
        catalog_read_allowed=True, public_reply_allowed=True, verified_by='synthetic-owner')
    result = resolver.resolve({**envelope, 'comment_text': EXACT})
    assert result.action == 'clarify' and result.reason_code == 'all_local_not_allowed'
    assert result.query_scope != 'all_local'


def test_known_explicit_library_and_save_rules_are_not_changed(tmp_path):
    registry, resolver, old, migration, catalog, envelope = scope(tmp_path)
    explicit = resolver.resolve({**envelope, 'comment_text': '根据“验收测试 · 演示资料”知识库解释Git暂存区。'})
    assert explicit.action == 'query' and explicit.binding.knowledge_base_id == 6
    assert explicit.query_scope == 'library'
    unnamed_save = resolver.resolve({**envelope, 'comment_text': '保存当前视频'})
    assert unnamed_save.action == 'clarify' and unnamed_save.reason_code == 'save_library_required'
    generic_save = resolver.resolve({**envelope, 'comment_text': '保存当前视频到我的已入库资料。'})
    assert generic_save.action == 'clarify' and generic_save.reason_code == 'unknown_library'


def test_plain_question_without_range_or_marker_uses_owner_all_sources(tmp_path):
    registry, resolver, old, migration, catalog, envelope = scope(tmp_path)
    result = resolver.resolve({**envelope, 'comment_text': 'Git暂存区有什么用'})
    assert result.action == 'query' and result.query_scope == 'all_local'
    assert result.binding.knowledge_base_id is None
    assert result.reason_code == 'all_local_query'


@pytest.mark.parametrize('name,kbid', [('111', 1), ('1', 2)])
def test_explicit_numeric_library_is_literal_not_an_ordinal_or_all_scope(tmp_path, name, kbid):
    registry, resolver, old, migration, catalog, envelope = scope(tmp_path)
    result = resolver.resolve({**envelope, 'comment_text': '根据“'+name+'”知识库解释Git。'})
    assert result.action == 'query' and result.query_scope == 'library'
    assert result.binding.knowledge_base_id == kbid


def test_real_quoted_generic_named_library_is_selected_and_alias_collision_clarifies(tmp_path):
    registry, resolver, old, migration, catalog, envelope = scope(tmp_path)
    catalog.append({'id': 7, 'name': '我的已入库资料'})
    registry.grant_library(migration['user_id'], migration['device_id'], 7, can_query=True, can_import=False)
    body = '根据“我的已入库资料”知识库解释Git。'
    selected = resolver.resolve({**envelope, 'comment_text': body})
    assert selected.action == 'query' and selected.query_scope == 'library'
    assert selected.binding.knowledge_base_id == 7
    catalog.append({'id': 8, 'name': '另一份资料'})
    registry.grant_library(migration['user_id'], migration['device_id'], 8,
        aliases=['我的已入库资料'], can_query=True, can_import=False)
    collision = resolver.resolve({**envelope, 'comment_text': body})
    assert collision.action == 'clarify' and collision.query_scope != 'all_local'
    assert collision.reason_code == 'unknown_or_ambiguous_library'


def test_unknown_explicit_knowledge_library_does_not_fall_back_to_all(tmp_path):
    registry, resolver, old, migration, catalog, envelope = scope(tmp_path)
    result = resolver.resolve({**envelope, 'comment_text': '根据不存在知识库解释Git。'})
    assert result.action == 'clarify' and result.reason_code == 'unknown_or_ambiguous_library'
    assert result.query_scope != 'all_local'
