"""Published replies read like a person; unsafe content is held, not posted."""
import pytest

from app.assistant.reply_humanize import (
    CHOICE_MAX_CHARS, humanize_reply, prepare_public_body, validate_frozen_reply,
)
from app.assistant.choice_intent import choice_prompt
from app.assistant.reply_style import style_reply_text


def test_import_status_reply_becomes_short_human_text():
    decision = humanize_reply("这个视频已加入“测试”知识库，文字稿和检索资料已就绪，现在可以根据它提问。", "import_status")
    assert decision["allowed"] is True
    assert decision["text"] == "存好啦，在「测试」里，想问啥直接问我就行。"
    assert "检索资料" not in decision["text"] and "知识库" not in decision["text"]


def test_markdown_lists_and_formal_tells_are_removed_and_capped():
    text = ("## 根据资料显示\n首先，视频讲了三点：\n1. 第一点是合成事实。\n2. 第二点是作者观点。\n"
            "3. 第三点是总结。\n综上所述，希望对你有帮助！")
    decision = humanize_reply(text, "answer")
    assert decision["allowed"] is True
    assert len(decision["text"]) <= 150
    for bad in ("#", "首先", "综上所述", "希望对你有帮助", "1.", "2."):
        assert bad not in decision["text"]


def test_links_contacts_and_bot_claims_are_blocked():
    for text, code in (
            ("详情看 https://example.invalid/x", "reply_content_link"),
            ("想聊的加我微信 12345", "reply_content_contact"),
            ("我是AI助手，正在自动回复", "reply_content_bot_claim"),
            ("   ", "reply_content_empty")):
        decision = humanize_reply(text, "answer")
        assert decision["allowed"] is False and decision["reason"] == code


def test_long_answer_keeps_whole_sentences_within_budget():
    text = "这个视频主要讲了固态硬盘价格下跌。" * 20
    decision = humanize_reply(text, "answer", maximum=60)
    assert decision["allowed"] is True and len(decision["text"]) <= 60
    assert decision["text"].endswith("。")


def test_library_question_is_short_natural_and_keeps_real_names():
    one = choice_prompt([{"knowledge_base_id": 1, "name": "测试", "ordinal": 1, "can_import": True}], catalog_count=1)
    assert "测试" in one and len(one) <= 160
    assert "请回复完整库名" not in one and "请回复" not in one
    two = choice_prompt([{"knowledge_base_id": 1, "name": "甲库", "ordinal": 1, "can_import": True},
                         {"knowledge_base_id": 2, "name": "乙库", "ordinal": 2, "can_import": True}], catalog_count=2)
    assert "甲库" in two and "乙库" in two and len(two) <= 160
    empty = choice_prompt([{"knowledge_base_id": 1, "name": "只读", "ordinal": 1, "can_import": False}],
                          catalog_count=1)
    assert "授权" in empty and "只读" not in empty


def test_frozen_choice_menu_keeps_names_ordinals_and_salutation_exactly():
    options = [{"name": "首先_Git#笔记", "ordinal": 3, "can_import": True},
               {"name": "1. 最后复习", "ordinal": 8, "can_import": True}]
    body = "少爷，" + choice_prompt(options, catalog_count=8)
    result = validate_frozen_reply(body, "clarification", maximum=CHOICE_MAX_CHARS)
    assert result == {"allowed": True, "text": body, "reason": None}
    assert "3. 首先_Git#笔记" in body and "8. 1. 最后复习" in body


def test_frozen_menu_uses_whole_structured_budget_instead_of_answer_truncation():
    body = "少爷，1. 「" + "长库名" * 60 + "」，请回复库名或序号。"
    assert 150 < len(body) <= CHOICE_MAX_CHARS
    assert validate_frozen_reply(body, "clarification", maximum=CHOICE_MAX_CHARS)["text"] == body
    decision = validate_frozen_reply("库" * (CHOICE_MAX_CHARS + 1), "clarification", maximum=CHOICE_MAX_CHARS)
    assert decision["allowed"] is False and decision["reason"] == "reply_content_too_long"


@pytest.mark.parametrize("text,reason", [
    ("1. https://example.invalid/资料", "reply_content_link"),
    ("1. 微信联系方式", "reply_content_contact"),
    ("我是AI，回复库名", "reply_content_bot_claim"),
    ("  ", "reply_content_empty"),
])
def test_frozen_menu_still_blocks_unsafe_public_content(text, reason):
    result = validate_frozen_reply(text, "clarification", maximum=CHOICE_MAX_CHARS)
    assert result["allowed"] is False and result["reason"] == reason


@pytest.mark.parametrize("prepare", [prepare_public_body, lambda text: humanize_reply(text)["text"]])
def test_answer_formatting_preserves_versions_decimals_and_literal_names(prepare):
    text = "Python 3.12 与 Python3.12 使用阈值 1.25；Git_笔记、C# 和最后一课都保留原名。"
    assert prepare(text) == text


@pytest.mark.parametrize("text", [
    '原文：“首先，Python 3.12 写入 Git_笔记，最后，保留 1.25。”',
    '原话：「最后，按 1.25 配比。\n1. 第一点原样保留。」',
    '原话：『首先，保留 Git#笔记。』及‘最后，保留原话。’',
    '原文："首先，阈值 1.25。"；术语：\'最后一课\'。',
    '运行 `git status --short && echo "最后，Git_笔记#1"` 后继续。',
    '运行 ``python -c "print(`marker`)"``，版本为 3.12。',
])
def test_new_answer_keeps_entire_quoted_and_code_spans_exact(text):
    assert prepare_public_body(text) == text


def test_new_answer_only_removes_real_presentation_markers():
    text = "## 根据资料显示\n首先，建议如下：\n1. 使用 Python 3.12。\n2. 打开 **Git_笔记** 和最后一课。"
    assert prepare_public_body(text) == "建议如下：\n使用 Python 3.12。打开 Git_笔记 和最后一课。"


def test_according_to_a_topic_is_content_and_is_not_removed_or_capped():
    text = "根据主题整理文件。" * 33
    assert 150 < len(text) <= 320
    assert prepare_public_body(text) == text


@pytest.mark.parametrize("kind", ["clarification", "import_status"])
def test_program_bodies_preserve_library_names_and_presentation_characters(kind):
    text = "存好啦，在「首先_Git#笔记_最后一课 1.25」里；1. 请按原名查看。"
    assert prepare_public_body(text, kind) == text


def test_new_body_reserves_one_normalized_prefix_and_does_not_freeze_it_twice():
    prefix = "少爷，"
    body = prepare_public_body("少爷: 少爷，**Python 3.12** 与 Git_笔记。", prefix="少爷:")
    assert body == "Python 3.12 与 Git_笔记。"
    assert style_reply_text(body, prefix, maximum=320) == prefix + body
    assert prepare_public_body("## 少爷，Python3.12。", prefix=prefix) == "Python3.12。"


def test_new_body_is_idempotent_after_nested_formatting_and_salutations():
    body = prepare_public_body("## 少爷，首先，最后，**Git_笔记**。", prefix="少爷，")
    assert body == "Git_笔记。"
    assert prepare_public_body(body, prefix="少爷，") == body
    assert prepare_public_body("少爷，" + body, prefix="少爷，") == body


def test_new_body_checks_the_styled_total_and_never_truncates():
    prefix = "同学，"
    body = "原" * (280 - len(prefix))
    assert prepare_public_body(body, prefix=prefix, maximum=280) == body
    with pytest.raises(ValueError, match="^invalid_public_reply$"):
        prepare_public_body(body + "文", prefix=prefix, maximum=280)
    with pytest.raises(ValueError, match="^invalid_public_reply$"):
        prepare_public_body("原" * 321)


@pytest.mark.parametrize("kind", ["answer", "clarification", "import_status"])
@pytest.mark.parametrize("text", [
    "", "  ", "请看 https://example.invalid/private", "请看 `https://example.invalid/private`",
    "原文：‘加我微信’。", "我是AI助手，正在自动回复。",
])
def test_new_body_blocks_unsafe_text_even_inside_protected_spans(kind, text):
    with pytest.raises(ValueError, match="^invalid_public_reply$"):
        prepare_public_body(text, kind)


@pytest.mark.parametrize("prefix", ["@同学", "同学\n", "DouyinMind，"])
def test_new_body_validates_the_prefix_as_part_of_the_public_text(prefix):
    with pytest.raises(ValueError, match="^invalid_public_reply$"):
        prepare_public_body("这段正文安全。", prefix=prefix)


def test_protected_span_tokens_cannot_collide_with_literal_private_use_text():
    text = '\ue0000\ue001 “首先，Git_笔记。” 与 `Python3.12`。'
    assert prepare_public_body(text) == text
