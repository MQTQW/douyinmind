"""Synthetic retrieval checks; deliberately independent of stored eval exams."""
from __future__ import annotations

from copy import deepcopy

import pytest

from app.services.retrieval_fusion import rerank_chunks


def doc(chunk: str, text: str, *, video: str = "v1", title: str = "普通课程", score: float = 0.4) -> dict:
    return {"chunk_id": chunk, "platform_item_id": video, "text": text, "title": title, "score": score}


def test_lexical_can_recover_a_relevant_chunk_outside_dense_pool():
    dense = [doc("wrong", "今天谈论森林生态与动物迁徙。", score=0.9)]
    needed = doc("right", "设置定时任务需要填写执行周期，并保存时区选项。")
    ranked = rerank_chunks("定时任务的执行周期和时区怎么设置？", dense, dense + [needed], 1)
    assert ranked[0]["chunk_id"] == "right"
    assert ranked[0]["retrieval_scores"]["lexical_rank"] == 1
    assert ranked[0]["retrieval_scores"]["dense_rank"] is None


def test_subquestions_do_not_disappear_under_repeated_main_topic():
    primary = [doc(f"main{i}", f"光伏电池把太阳光变为电能，通过半导体产生电流。详细过程如下{i}。") for i in range(8)]
    secondary = doc("extra", "废弃电池的回收流程分为收集、拆解和材料分离。")
    ranked = rerank_chunks("光伏电池如何产生电流，以及废弃电池的回收流程是什么？", primary, primary + [secondary], 3)
    assert "extra" in [hit["chunk_id"] for hit in ranked]


def test_dense_hit_original_fields_and_inputs_are_unchanged():
    lexical = doc("a", "数据库连接池可以复用现有连接。", score=0.0)
    dense = {**lexical, "score": 0.7654321, "extra": {"source": "dense"}}
    snapshot = deepcopy(([dense], [lexical]))
    result = rerank_chunks("连接池的作用", [dense], [lexical], 1)
    assert {k: v for k, v in result[0].items() if k != "retrieval_scores"} == dense
    assert ([dense], [lexical]) == snapshot


@pytest.mark.parametrize("metadata", [
    doc("meta", "视频标题：缓存教程\n作者：甲", title="缓存教程"),
    doc("meta", "视频标题：缓存教程\n多行标题 #缓存\n作者：甲", title="缓存教程\n多行标题 #缓存"),
    doc("meta", "缓存教程", title="缓存教程"),
    doc("meta", "视频标题：" + "题" * 510 + "\n作者：甲", title="题" * 500),
    doc("meta", "   "),
])
def test_title_author_and_empty_chunks_never_become_content(metadata):
    assert rerank_chunks("缓存教程", [metadata], [metadata], 3) == []


def test_title_author_followed_by_real_body_remains_evidence():
    hit = doc("real", "视频标题：缓存教程\n作者：甲\n缓存通过重复使用结果减少计算。", title="缓存教程")
    assert rerank_chunks("缓存减少计算", [hit], [hit], 1)[0]["chunk_id"] == "real"


def test_scope_documents_are_authoritative_for_dense_candidates():
    scoped = doc("inside", "水流推动涡轮。", video="allowed")
    outside = doc("outside", "水流推动涡轮并产生电流。", video="forbidden")
    result = rerank_chunks("水流推动涡轮", [outside, scoped], [scoped], 8)
    assert [hit["platform_item_id"] for hit in result] == ["allowed"]
    assert rerank_chunks("水流推动涡轮", [outside], [], 8) == []


def test_explicit_unique_title_has_soft_bias_without_discarding_other_evidence():
    target = doc("target", "齿轮通过啮合传递动力。", video="gear", title="齿轮原理讲解 #机械")
    other = doc("other", "齿轮通过啮合传递动力，并改变转速。", video="other", title="机械入门")
    result = rerank_chunks("《齿轮原理讲解》视频中，齿轮如何传递动力？", [other, target], [other, target], 2)
    assert result[0]["platform_item_id"] == "gear"
    assert result[0]["retrieval_scores"]["explicit_title_match"]
    assert len(result) == 2


def test_ambiguous_short_title_does_not_select_a_video():
    a = doc("a", "齿轮啮合传递动力。", video="a", title="齿轮原理甲")
    b = doc("b", "齿轮啮合传递动力。", video="b", title="齿轮原理乙")
    result = rerank_chunks("《齿轮原理》里如何传递动力", [a, b], [a, b], 2)
    assert not any(hit["retrieval_scores"]["explicit_title_match"] for hit in result)


def test_unquoted_named_video_can_be_identified():
    target = doc("target", "水泵通过叶轮旋转输送水。", video="pump", title="离心水泵原理")
    other = doc("other", "叶轮旋转可以输送水。", video="other", title="输水管网")
    result = rerank_chunks("离心水泵原理视频中叶轮有什么作用？", [target, other], [target, other], 2)
    assert result[0]["retrieval_scores"]["explicit_title_match"]


def test_no_lexical_match_keeps_dense_evidence_instead_of_inventing_it():
    hit = doc("semantic", "风经过叶片时产生升力。", score=0.8)
    result = rerank_chunks("空气动力机制", [hit], [hit], 1)
    assert result[0]["text"] == hit["text"]
    assert result[0]["retrieval_scores"]["bm25"] == 0
    assert rerank_chunks("空气动力机制", [], [hit], 1) == []


def test_english_identifiers_and_numbers_are_retrievable():
    a = doc("a", "The timeout option is configured through HTTP_TIMEOUT=45.")
    b = doc("b", "The option configures an unrelated display theme.")
    result = rerank_chunks("What is HTTP_TIMEOUT set to?", [b], [a, b], 1)
    assert result[0]["chunk_id"] == "a"


def test_time_labels_are_not_numeric_content_evidence():
    a = doc("time", "[00:45] 什么是电路？")
    b = doc("body", "设备功率为45瓦。")
    assert rerank_chunks("功率45", [a], [a, b], 1)[0]["chunk_id"] == "body"


def test_same_id_duplicates_cannot_use_multiple_slots():
    hit = doc("one", "电池使用化学反应储存和释放能量。")
    result = rerank_chunks("电池如何储存能量", [hit, hit], [hit, hit], 5)
    assert len(result) == 1


def test_output_is_deterministic_and_count_bounded():
    docs = [doc(str(i), f"太阳能转换过程第{i}步。") for i in range(180)]
    assert rerank_chunks("太阳能转换", docs[:32], docs, 5) == rerank_chunks("太阳能转换", docs[:32], docs, 5)
    assert len(rerank_chunks("太阳能转换", docs[:32], docs, 10000)) <= 96
    assert rerank_chunks("太阳能转换", docs[:32], docs, 0) == []


def test_sentence_repetition_does_not_outweigh_other_required_evidence():
    repeated = doc("repeat", "蒸汽推动活塞。" * 20)
    second = doc("second", "冷凝器通过冷却蒸汽形成低压环境。")
    result = rerank_chunks("蒸汽如何推动活塞，冷凝器又起什么作用？", [repeated], [repeated, second], 2)
    assert {hit["chunk_id"] for hit in result} == {"repeat", "second"}


def test_dense_candidate_after_32_is_not_silently_ignored_without_lexical_overlap():
    duplicates = [doc(f"generic{i}", "这是课程的开场白，欢迎各位观众。") for i in range(32)]
    necessary = doc("semantic33", "催化剂降低活化能，让单体连接成长链。")
    result = rerank_chunks("聚合过程为什么能启动？", duplicates + [necessary], duplicates + [necessary], 5)
    recovered = next(hit for hit in result if hit["chunk_id"] == "semantic33")
    assert recovered["retrieval_scores"]["dense_rank"] == 33
    assert recovered["retrieval_scores"]["bm25"] == 0


def test_maximum_dense_pool_does_not_evict_lexical_only_evidence():
    dense = [doc(f"generic{i}", "森林生态研究讨论动物的迁徙。") for i in range(128)]
    needed = doc("lexical129", "定时任务必须保存执行周期和时区设置。")
    result = rerank_chunks("定时任务的执行周期和时区如何设置？", dense, dense + [needed], 5)
    assert "lexical129" in [hit["chunk_id"] for hit in result]


def test_dense_pool_accepts_128_candidates_but_remains_bounded():
    # No lexical match: every returned source must have entered through dense
    # ranking, including the final configured slot, rather than a lexical path.
    docs = [doc(str(i), f"欢迎来到课堂开场。{i}") for i in range(140)]
    result = rerank_chunks("催化剂", docs, docs, 10000)
    assert len(result) == 128
    assert {hit["retrieval_scores"]["dense_rank"] for hit in result} == set(range(1, 129))
    assert all(hit["retrieval_scores"]["bm25"] == 0 for hit in result)
