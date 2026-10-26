"""Closed Chinese menu ordinals; pure frozen-option selection only."""
from copy import deepcopy

import pytest

from app.assistant.choice_intent import parse_library_selection


REAL_MENU = [
    {'ordinal':1,'knowledge_base_id':1,'name':'111'},
    {'ordinal':2,'knowledge_base_id':2,'name':'1'},
    {'ordinal':3,'knowledge_base_id':3,'name':'多久时间'},
    {'ordinal':4,'knowledge_base_id':4,'name':'全部收藏'},
    {'ordinal':5,'knowledge_base_id':5,'name':'测试长视频'},
    {'ordinal':6,'knowledge_base_id':6,'name':'验收测试 · 演示资料'},
]


@pytest.mark.parametrize('text', ['第四个','选第四个','选择第四个','第四个吧','保存到第四个','放到第四个','放到第4个'])
def test_real_frozen_fourth_option_selects_kb4_without_reordering(text):
    assert parse_library_selection(text, REAL_MENU)['knowledge_base_id'] == 4


@pytest.mark.parametrize('ordinal,number', [(1,'一'),(10,'十'),(11,'十一'),(20,'二十'),
    (21,'二十一'),(99,'九十九'),(100,'一百'),(101,'一百零一'),(110,'一百一十'),(128,'一百二十八')])
def test_standard_chinese_ordinals_use_the_stored_ordinal_not_id_or_filtered_position(ordinal, number):
    options = [{'ordinal':ordinal,'knowledge_base_id':900,'name':'合成目标资料'}]
    assert parse_library_selection('第'+number+'个', options)['knowledge_base_id'] == 900
    assert parse_library_selection('选第'+number+'个', options)['knowledge_base_id'] == 900


@pytest.mark.parametrize('name,text', [('第四个','第四个'),('选第四个','选第四个'),('第4个','第4个')])
def test_name_and_ordinal_collisions_keep_every_target_and_refuse_ambiguity(name,text):
    options = deepcopy(REAL_MENU)+[{'ordinal':9,'knowledge_base_id':99,'name':name}]
    assert parse_library_selection(text, options) is None


def test_filtered_menu_does_not_renumber_its_frozen_ordinals():
    options = [REAL_MENU[1], REAL_MENU[3]]
    assert parse_library_selection('第四个', options)['knowledge_base_id'] == 4
    assert parse_library_selection('第二个', options)['knowledge_base_id'] == 2
    assert parse_library_selection('第一个', options) is None


@pytest.mark.parametrize('text', ['不要第四个','别放到第四个','不选第四个','第四个吗','放到第四个？',
    '如果合适放到第四个','第四个和第五个','第四个或者第五个','第四个吧吧','第四个谢谢','第一百二十九个'])
def test_negative_question_condition_multiple_target_and_out_of_range_forms_are_not_consent(text):
    assert parse_library_selection(text, REAL_MENU) is None


def test_literal_number_names_keep_prior_collision_and_prefix_semantics():
    assert parse_library_selection('1', REAL_MENU) is None
    assert parse_library_selection('111', REAL_MENU)['knowledge_base_id'] == 1
    assert parse_library_selection('放到1', REAL_MENU)['knowledge_base_id'] == 2
    assert parse_library_selection('放到111', REAL_MENU)['knowledge_base_id'] == 1
    assert parse_library_selection('放到第一个', REAL_MENU)['knowledge_base_id'] == 1


def test_parser_never_rewrites_original_reply_or_options():
    text = ' 第四个吧 '; original = deepcopy(REAL_MENU)
    assert parse_library_selection(text, REAL_MENU)['knowledge_base_id'] == 4
    assert text == ' 第四个吧 ' and REAL_MENU == original
