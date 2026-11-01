"""Published replies read like a person; unsafe content is held, not posted."""
from app.assistant.reply_humanize import humanize_reply
from app.assistant.choice_intent import choice_prompt


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
