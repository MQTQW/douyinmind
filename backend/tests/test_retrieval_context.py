"""Offline synthetic tests for safe verbatim expansion and prompt packing."""
from copy import deepcopy

import pytest

from app.services.retrieval_context import expand_hit_context, select_context_hits


SPEECH = "语音文字稿（来自视频声音）：\n"
VISUAL = "画面分析（来自视频画面）：\n"


def document(index, body, *, video="v1", prefix=SPEECH, **extra):
    return {"chunk_id": f"{video}:{index}", "platform_item_id": video,
            "title": "合成测试视频", "text": prefix + body, "score": .75, **extra}


def overlapped_documents(text, size=120, overlap=40):
    return [document(index, text[start:start + size].strip())
            for index, start in enumerate(range(0, len(text), size - overlap))]


def format_blocks(hits):
    return "\n---\n".join(f"【{hit['platform_item_id']}｜{hit['title']}】\n{hit['text']}" for hit in hits)


def has_source(hit):
    return not hit["text"].startswith("视频标题：") and bool(hit["text"].strip())


def select(hits, max_count=8, max_chars=8000, scope_ids=None):
    return select_context_hits(hits, max_count=max_count, max_chars=max_chars,
                               format_blocks=format_blocks, has_source_text=has_source,
                               scope_ids=scope_ids)


def test_completes_both_sentence_edges_without_rewriting_or_mutating():
    text = "".join(f"第{i}句说明条件与结果，只有核对原文才知道这一项的具体含义。" for i in range(12))
    docs = overlapped_documents(text)
    hit = {**docs[2], "score": .918}
    before = deepcopy((docs, hit))
    result = expand_hit_context([hit], docs, max_extra_chars=70)[0]
    body = result["text"][len(SPEECH):]
    assert body in text
    assert hit["text"][len(SPEECH):] in body
    assert len(result["text"]) - len(hit["text"]) <= 70
    assert body.endswith("。")
    assert text[text.index(body) - 1] == "。"
    assert result["score"] == .918
    assert result["chunk_id"] == hit["chunk_id"]
    assert result["expanded_from_chunk_ids"] == ["v1:1", "v1:2", "v1:3"]
    assert (docs, hit) == before


def test_restores_complete_timestamp_before_the_cut_sentence():
    text = "".join(f"[0{i}:00–0{i}:09] 这里是第{i}个带时间的完整语音片段，逐句保留原来的说法。\n" for i in range(8))
    docs = overlapped_documents(text, size=140, overlap=50)
    hit = docs[1]
    result = expand_hit_context([hit], docs)[0]
    body = result["text"][len(SPEECH):]
    assert body.startswith("[")
    assert body in text
    assert result["expanded_from_chunk_ids"][0] == "v1:0"
    assert result["text"].startswith(SPEECH)


def test_chunk_index_field_takes_precedence_over_identifier_tail():
    text = "".join(f"事实{i}需要保持原话完整，并保留句子的条件和结果。" for i in range(20))
    docs = overlapped_documents(text)
    for index, doc in enumerate(docs):
        doc["chunk_id"] = f"special-{index}"
        doc["chunk_index"] = index
    result = expand_hit_context([docs[2]], docs)[0]
    assert len(result["text"]) > len(docs[2]["text"])
    assert "special-2" in result["expanded_from_chunk_ids"]


@pytest.mark.parametrize("change", ["video", "type", "position", "ambiguous", "text", "unknown_source", "duplicate_position"])
def test_unreliable_or_different_source_neighbour_is_not_joined(change):
    overlap = "这是一段用于唯一定位的相同原文重叠内容，不能改变其中的任何文字或者顺序"
    previous = document(0, "之前的一句。完整开头" + overlap)
    hit = document(1, overlap + "尚未结束")
    docs = [previous, deepcopy(hit)]
    if change == "video":
        previous["platform_item_id"] = "other"
    elif change == "type":
        previous["text"] = previous["text"].replace(SPEECH, VISUAL)
    elif change == "position":
        previous["chunk_id"] = "v1:9"
    elif change == "ambiguous":
        previous["text"] = SPEECH + overlap + "。" + overlap
    elif change == "text":
        hit["text"] += "被改动"
    elif change == "unknown_source":
        previous["text"] = previous["text"].replace(SPEECH, "")
    elif change == "duplicate_position":
        docs.append({**previous, "chunk_id": "other:0", "chunk_index": 0})
    assert expand_hit_context([hit], docs) == [hit]


def test_metadata_only_cannot_supply_facts_or_be_expanded():
    previous = document(0, "视频标题：标题内容不是正文\n作者：测试作者", prefix="")
    hit = document(1, "标题内容不是正文\n作者：测试作者还有一些内容")
    assert expand_hit_context([hit, previous], [previous, hit]) == [hit, previous]


def test_explicit_source_type_can_supply_a_label_for_legacy_chunks():
    overlap = "同一条语音的唯一原文重叠内容用于可靠定位，原文不会经过摘要处理或者改写"
    previous = document(0, "这是上一句。应该补全的开头" + overlap, prefix="", source_type="audio")
    hit = document(1, overlap + "。", prefix="", source_type="speech")
    result = expand_hit_context([hit], [previous, hit])[0]
    assert result["text"] == "应该补全的开头" + overlap + "。"
    assert result["expanded_from_chunk_ids"] == ["v1:0", "v1:1"]


def test_conflicting_source_labels_fail_closed():
    overlap = "同一条语音的唯一原文重叠内容用于可靠定位，原文不会经过摘要处理或者改写"
    previous = document(0, "前一句。开头" + overlap, source_type="visual")
    hit = document(1, overlap + "。")
    assert expand_hit_context([hit], [previous, hit]) == [hit]


def test_insufficient_budget_never_cuts_a_new_sentence_to_fill_space():
    overlap = "可靠但不短的相同内容用于定位边界，只有完全相同的原文才能够进行拼接识别"
    previous = document(0, "第一句。" + "必须整体保留的开头" * 5 + overlap)
    hit = document(1, overlap + "结尾没有结束")
    following = document(2, overlap + "结尾没有结束" + "必须整体保留的结尾" * 5 + "。")
    assert expand_hit_context([hit], [previous, hit, following], max_extra_chars=5) == [hit]


def test_two_edge_repairs_share_one_budget():
    overlap1 = "上方用于定位的唯一重叠内容，包含足够多的原始文字才能避免错误连接相邻内容"
    overlap2 = "下方用于定位的另一段重叠内容，保留不同的文字以免重复语句造成错误拼接行为"
    previous = document(0, "前面。" + "开头" * 30 + overlap1)
    hit = document(1, overlap1 + "正文。" + overlap2)
    following = document(2, overlap2 + "结束" * 30 + "。")
    result = expand_hit_context([hit], [previous, hit, following], max_extra_chars=80)[0]
    assert 0 < len(result["text"]) - len(hit["text"]) <= 80
    assert len(result["expanded_from_chunk_ids"]) == 2


def test_nested_chroma_metadata_and_document_field_are_supported():
    text = "".join(f"第{i}段的原文是具有唯一性的完整句子，不能通过概括来替换这些内容。" for i in range(10))
    docs = overlapped_documents(text)
    nested = [{"metadata": {key: doc[key] for key in ("chunk_id", "platform_item_id", "title")},
               "document": doc["text"]} for doc in docs]
    result = expand_hit_context([docs[2]], nested)[0]
    assert len(result["text"]) > len(docs[2]["text"])


def test_missing_anchor_or_disabled_expansion_returns_copies_unchanged():
    hit = document(0, "完整原文。")
    assert expand_hit_context([hit], []) == [hit]
    assert expand_hit_context([hit], [hit], max_extra_chars=0) == [hit]
    assert expand_hit_context([hit], [hit], max_extra_chars=0)[0] is not hit


def test_budget_counts_formatted_headers_and_skips_later_oversize_blocks():
    hits = [document(0, "首个事实。"), document(1, "不应进入的过长内容" * 60), document(2, "后续短事实。")]
    budget = len(format_blocks([hits[0], hits[2]]))
    assert select(hits, max_chars=budget) == [hits[0], hits[2]]
    assert len(format_blocks(select(hits, max_chars=budget))) <= budget


def test_oversized_first_block_keeps_whole_sentences_and_marks_limit():
    hit = document(0, "第一句是完整的原文。第二句也必须完整保留。" + "第三句很长" * 70 + "。")
    budget = len(format_blocks([document(0, "第一句是完整的原文。第二句也必须完整保留。\n[此片段超过长度限制，后文未提供]")]))
    selected = select([hit], max_chars=budget)
    assert len(selected) == 1
    assert selected[0]["text"] == SPEECH + "第一句是完整的原文。第二句也必须完整保留。\n[此片段超过长度限制，后文未提供]"
    assert len(format_blocks(selected)) <= budget


def test_oversized_unpunctuated_first_line_is_skipped_for_a_later_short_hit():
    hit = document(0, "无法完整放入的一整段" * 100)
    short = document(1, "这个片段放得下。")
    assert select([hit, short], max_chars=100) == [short]


def test_scope_metadata_and_duplicate_hits_do_not_consume_slots():
    first = document(0, "条件成立时才能完成这一步。补充说明。")
    duplicate = document(1, "条件成立时才能完成这一步。")
    other = document(2, "库外视频的事实。", video="other")
    metadata = document(3, "视频标题：这不是正文", prefix="")
    final = document(4, "另一个独立事实。")
    selected = select([metadata, other, first, deepcopy(first), duplicate, final], max_count=2, scope_ids={"v1"})
    assert selected == [first, final]


def test_equal_text_from_different_videos_or_source_types_is_not_collapsed():
    first = document(0, "相同的一句话。")
    other = document(0, "相同的一句话。", video="other")
    visual = document(1, "相同的一句话。", prefix=VISUAL)
    assert select([first, other, visual]) == [first, other, visual]


def test_no_cap_preserves_rank_order_without_mutating_inputs():
    hits = [document(index, f"第{index}个事实。") for index in range(5)]
    before = deepcopy(hits)
    assert select(hits, max_count=3, max_chars=0) == hits[:3]
    assert select(hits, max_count=0) == []
    assert hits == before
