"""Exact frozen-menu phrases only; no accounts, storage, model or browser."""
from copy import deepcopy

import pytest

from app.assistant.choice_intent import parse_library_selection, selection_forms


OPTIONS = [
    {'knowledge_base_id': 1, 'name': '111', 'ordinal': 1},
    {'knowledge_base_id': 5, 'name': '测试长视频', 'ordinal': 5},
]


@pytest.mark.parametrize('text', [
    '收藏到测试长视频吧',
    '收藏到测试长视频',
    '加到测试长视频',
    '加入到测试长视频',
    '加入到测试长视频吧。',
    '加到“测试长视频”吧！',
    '收藏到测试长视频知识库吧',
    '测试长视频吧',
    '存到测试长视频吧',
    '保存到测试长视频吧。',
    '第5个吧',
    '5吧',
])
def test_affirmative_exact_frozen_choice_with_one_particle_preserves_original(text):
    options = deepcopy(OPTIONS)
    before = deepcopy(options)
    original = text
    selected = parse_library_selection(text, options)
    assert selected is options[1]
    assert options == before and text == original


@pytest.mark.parametrize('text', [
    '不要收藏到测试长视频吧',
    '别加到测试长视频',
    '不用加入到测试长视频',
    '取消收藏到测试长视频',
    '收藏到测试长视频吗',
    '收藏到测试长视频吧？',
    '怎么收藏到测试长视频',
    '如果有用就收藏到测试长视频吧',
    '有空再加入到测试长视频',
    '收藏到测试长视频和111吧',
    '收藏到测试长视频，或者111',
    '收藏到测试长视频；再加入111',
    '收藏到测试长视频吧吧',
    '收藏到测试长视频好吗',
    '收藏到测试长视频，另一个要求',
    '前缀收藏到测试长视频吧',
    '收藏到未知测试长视频吧',
    '收藏到测试长视频未知库吧',
    '收藏到https://example.invalid/测试长视频吧',
])
def test_nonaffirmative_unknown_multiple_or_partial_text_never_selects(text):
    assert parse_library_selection(text, OPTIONS) is None


def test_two_approved_same_names_remain_ambiguous():
    options = OPTIONS + [{'knowledge_base_id': 8, 'name': '测试长视频', 'ordinal': 8}]
    assert parse_library_selection('收藏到测试长视频吧', options) is None


def test_particle_must_not_hide_a_real_library_name_collision():
    options = OPTIONS + [{'knowledge_base_id': 8, 'name': '测试长视频吧', 'ordinal': 8}]
    assert parse_library_selection('测试长视频吧', options) is None
    assert parse_library_selection('收藏到测试长视频吧', options) is None
    assert parse_library_selection('收藏到“测试长视频吧”', options) is options[2]


def test_library_named_like_full_command_is_not_silently_disambiguated():
    options = OPTIONS + [{'knowledge_base_id': 8, 'name': '收藏到测试长视频吧', 'ordinal': 8}]
    assert parse_library_selection('收藏到测试长视频吧', options) is None


def test_numeric_library_and_original_catalog_ordinal_collision_stays_ambiguous():
    options = [
        {'knowledge_base_id': 5, 'name': '测试长视频', 'ordinal': 5},
        {'knowledge_base_id': 8, 'name': '5', 'ordinal': 8},
    ]
    assert parse_library_selection('5吧', options) is None
    assert parse_library_selection('收藏到5吧', options) is options[1]


def test_browser_and_store_share_the_same_exact_forms_not_model_target_parameters():
    forms = selection_forms(OPTIONS)
    assert forms['收藏到测试长视频吧'] == {5}
    assert '收藏到未知测试长视频吧' not in forms
    assert '收藏到测试长视频吧？' not in forms
