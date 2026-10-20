"""Grounding regressions with temporary SQLite data and fake AI clients.

These tests check which original evidence reaches generation and persistence.
They deliberately do not pretend that a mocked model measures factual accuracy.
The separately retained evaluation exam exercises the real model.
"""
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.entities import ChatMessage, KnowledgeBase, KnowledgeBaseVideo, VideoCache


@pytest.fixture
def rag_module(monkeypatch):
    def unexpected_call(**kwargs):
        raise AssertionError("Context preparation must not call a generative model")

    chroma = ModuleType("app.services.chroma_service")
    chroma.get_chroma_service = lambda: SimpleNamespace(count=lambda: 1)
    llm = ModuleType("app.services.llm_service")
    llm.llm_client = SimpleNamespace(chat=unexpected_call, stream_chat=unexpected_call)
    llm.embedding_client = SimpleNamespace(embed_text=lambda text: [1.0, 0.0])
    monkeypatch.setitem(sys.modules, chroma.__name__, chroma)
    monkeypatch.setitem(sys.modules, llm.__name__, llm)
    path = Path(__file__).resolve().parents[1] / "app/services/rag_service.py"
    spec = importlib.util.spec_from_file_location("grounded_rag_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def hit(item_id, text, *, title=None, index=1, score=0.9):
    return dict(platform_item_id=item_id, chunk_id=f"{item_id}:{index}",
                title=title or f"视频{item_id}", text=text, score=score)


def add_scope(db, ids):
    kb = KnowledgeBase(name="原文测试库", name_key="原文测试库")
    db.add(kb)
    db.flush()
    for item in ids:
        db.add(VideoCache(platform_item_id=item, title=f"视频{item}",
                          status="done", transcript_text=f"完整文字稿_{item}"))
        db.add(KnowledgeBaseVideo(knowledge_base_id=kb.id, platform_item_id=item))
    db.commit()
    return kb


def finish(service, db, streaming, query, *, kb_id=None):
    if not streaming:
        return service.answer(db, query, None, knowledge_base_id=kb_id)
    events = list(service.answer_stream(db, query, None, knowledge_base_id=kb_id))
    assert events[-1] == ("done", {"ok": True})
    assert not any(name == "error" for name, _ in events)
    return next(payload for name, payload in events if name == "meta")


def test_more_than_three_chunks_keep_original_six_causes_without_summary(rag_module, monkeypatch):
    summary_calls = []

    def forbidden_summary(**kwargs):
        summary_calls.append(kwargs)
        raise AssertionError("No generated summaries may replace source text")

    monkeypatch.setattr(rag_module.llm_client, "chat", forbidden_summary)
    causes = [f"原因{i}：这是原文中特定的第{i}种原因，不可省略。" for i in range(1, 7)]
    tail = "最后的重要限制：备份周期必须依照实际业务单独确认。"
    original = "【语音转写】\n[00:12-01:35] " + "\n".join(causes) + "补充说明。" * 65 + tail
    hits = [hit("a", original)] + [hit(str(i), f"【语音转写】\n[0{i}:00] 辅助原文{i}。") for i in range(1, 5)]
    context = rag_module.RagService()._compress_chunks("列出六种原因和注意事项", hits)
    assert summary_calls == []
    for sentence in [*causes, tail, "[00:12-01:35]", "【语音转写】"]:
        assert sentence in context
    for item in hits:
        assert item["text"] in context


@pytest.mark.parametrize("route", ["vector", "db_content"])
def test_metadata_does_not_consume_context_slots(rag_module, db, monkeypatch, route):
    monkeypatch.setattr(rag_module.settings, "rag_context_count", 2)
    metadata = [hit(item, f"视频标题：视频{item}\n作者：作者{item}", index=0)
                for item in ["a", "b", "c"]]
    bodies = [hit(item, f"【语音转写】\n[01:00-02:00] 正文证据_{item}。")
              for item in ["a", "b", "c"]]
    context = rag_module.RagService()._build_context(route, metadata + bodies, db, "正文问题")
    assert bodies[0]["text"] in context
    assert bodies[1]["text"] in context
    assert bodies[2]["text"] not in context
    assert all(item["text"] not in context for item in metadata)


@pytest.mark.parametrize("route", ["vector", "db_content"])
def test_metadata_only_chunks_cannot_become_content_evidence(rag_module, db, route):
    metadata = [hit("a", "视频标题：备份三十天的技巧\n作者：测试作者", index=0)]
    assert rag_module.RagService()._build_context(route, metadata, db, "备份期限是多少？") == ""


@pytest.mark.parametrize("streaming", [False, True])
def test_generation_and_saved_sources_use_only_selected_in_scope_originals(
        rag_module, db, monkeypatch, streaming):
    monkeypatch.setattr(rag_module.settings, "rag_context_count", 2)
    kb = add_scope(db, ["a", "b", "c", "d"])
    metadata = [hit(item, f"视频标题：视频{item}\n作者：作者{item}", index=0)
                for item in ["a", "b", "c"]]
    bodies = [hit(item, f"【语音转写】\n[00:0{i}] ORIGINAL_{item} 只讨论第{i}种原因。")
              for i, item in enumerate(["a", "b", "c", "d"], 1)]
    candidates = metadata + bodies + [hit("foreign", "FOREIGN_SECRET 原因是别库内容。")]
    searches, prompts = [], []

    def search(vector, **kwargs):
        searches.append(kwargs)
        return candidates

    monkeypatch.setattr(rag_module, "get_chroma_service", lambda: SimpleNamespace(count=lambda: 8, search=search))

    def generate(**kwargs):
        prompts.append(kwargs)
        return "第一种与第二种原因见原文。[来源: 资料1] [来源: 资料2]"

    monkeypatch.setattr(rag_module.llm_client, "chat", generate)
    monkeypatch.setattr(rag_module.llm_client, "stream_chat", lambda **kwargs: iter([generate(**kwargs)]))
    result = finish(rag_module.RagService(), db, streaming, "原因分别是什么？", kb_id=kb.id)
    assert len(prompts) == 1, "Only final answer generation may use the LLM"
    prompt = prompts[0]["user_prompt"]
    assert bodies[0]["text"] in prompt and bodies[1]["text"] in prompt
    assert "ORIGINAL_c" not in prompt and "ORIGINAL_d" not in prompt
    assert "FOREIGN_SECRET" not in prompt
    assert not any(item["text"] in prompt for item in metadata)
    assert searches and searches[0]["allowed_video_ids"] == {"a", "b", "c", "d"}
    assert {source["platform_item_id"] for source in result["sources"]} == {"a", "b"}
    assert {item["chunk_id"] for item in result["trace"]["chunks"]} == {"a:1", "b:1"}
    assistant = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
    assert set(json.loads(assistant.retrieved_video_ids)) == {"a", "b"}
    assert set(json.loads(assistant.retrieved_chunk_ids)) == {"a:1", "b:1"}


def test_content_with_metadata_header_is_still_original_evidence(rag_module, db):
    original = "视频标题：原文教程\n作者：测试作者\n【语音转写】\n[00:12] 先打开设置，再点击导出。"
    context = rag_module.RagService()._build_context("vector", [hit("a", original)], db, "导出步骤是什么？")
    assert original in context


@pytest.mark.parametrize("title,text,expected", [
    pytest.param(
        "UI工具完整教程\n#ai #界面设计",
        "视频标题：UI工具完整教程\n#ai #界面设计\n作者：示例作者",
        False, id="multiline-title-with-hashtags-and-author-is-metadata"),
    pytest.param(
        "UI工具完整教程\n#ai #界面设计",
        "视频标题：UI工具完整教程\n#ai #界面设计",
        False, id="multiline-title-without-author-is-metadata"),
    pytest.param(
        "UI工具完整教程\n#ai #界面设计",
        "视频标题：UI工具完整教程\n#ai #界面设计\n作者：示例作者\n"
        "【语音转写】\n[00:03] 打开设置后点击导出，这一行是实际正文。",
        True, id="real-body-after-matching-title-and-author-is-evidence"),
    pytest.param(
        ("UI工具教程\n" + "标题" * 300 + "#ai")[:500],
        "视频标题：" + ("UI工具教程\n" + "标题" * 300 + "#ai")[:512] + "\n作者：示例作者",
        False, id="full-multiline-title-with-truncated-500-character-metadata-title"),
])
def test_multiline_metadata_titles_are_distinguished_from_actual_body(rag_module, title, text, expected):
    assert rag_module.RagService()._has_source_text(hit("a", text, title=title, index=0)) is expected


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("answer,expected_ids", [
    ("甲原文中的结论。[来源: 资料1]", {"a"}),
    ("乙原文中的结论。[来源: 视频b]", {"b"}),
    ("我无法从资料确定具体期限。", set()),
    ("错误编号。[来源: 资料99]", set()),
    ("不在实际上下文中的候选。[来源: 视频c]", set()),
])
def test_only_explicit_whitelisted_citations_are_returned_and_restored(
        rag_module, db, monkeypatch, streaming, answer, expected_ids):
    monkeypatch.setattr(rag_module.settings, "rag_context_count", 2)
    kb = add_scope(db, ["a", "b", "c"])
    candidates = [hit(item, f"【语音转写】\n[00:01] 原文证据_{item}。") for item in ["a", "b", "c"]]
    monkeypatch.setattr(rag_module, "get_chroma_service", lambda: SimpleNamespace(
        count=lambda: 3, search=lambda *args, **kwargs: candidates))
    monkeypatch.setattr(rag_module.llm_client, "chat", lambda **kwargs: answer)
    monkeypatch.setattr(rag_module.llm_client, "stream_chat", lambda **kwargs: iter([answer]))
    service = rag_module.RagService()
    if streaming:
        stream = service.answer_stream(db, "备份期限是多少？", None, knowledge_base_id=kb.id)
        assert next(stream) == ("sources", {"sources": []})
        events = list(stream)
        assert events[-1] == ("done", {"ok": True})
        result = next(payload for name, payload in events if name == "meta")
    else:
        result = service.answer(db, "备份期限是多少？", None, knowledge_base_id=kb.id)
    assert {source["platform_item_id"] for source in result["sources"]} == expected_ids
    assert "资料99" not in result["answer"]
    assert "[来源: 视频c]" not in result["answer"]
    assert "参考来源" not in result["answer"]
    assistant = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
    assert set(json.loads(assistant.retrieved_video_ids)) == expected_ids
    assert set(json.loads(assistant.retrieved_chunk_ids)) == {f"{item}:1" for item in expected_ids}
    restored = service.get_messages(db, result["session_id"])
    restored_assistant = next(message for message in restored if message["role"] == "assistant")
    assert {source["platform_item_id"] for source in restored_assistant["sources"]} == expected_ids


@pytest.mark.parametrize("streaming", [False, True])
def test_title_only_match_does_not_supply_a_reference_for_missing_fact(rag_module, db, monkeypatch, streaming):
    kb = add_scope(db, ["a"])
    metadata = hit("a", "视频标题：备份保留三十天\n作者：标题不是事实", index=0)
    monkeypatch.setattr(rag_module, "get_chroma_service", lambda: SimpleNamespace(
        count=lambda: 1, search=lambda *args, **kwargs: [metadata]))
    prompts = []

    def generate(**kwargs):
        prompts.append(kwargs)
        return "当前资料未提供备份保留期限，无法确定。"

    monkeypatch.setattr(rag_module.llm_client, "chat", generate)
    monkeypatch.setattr(rag_module.llm_client, "stream_chat", lambda **kwargs: iter([generate(**kwargs)]))
    result = finish(rag_module.RagService(), db, streaming, "这个视频说备份要留多久？", kb_id=kb.id)
    assert result["sources"] == []
    assert result["trace"]["chunks"] == []
    assert all(metadata["text"] not in call["user_prompt"] for call in prompts)
    assert "参考来源" not in result["answer"]


def test_streaming_and_nonstreaming_send_identical_original_evidence(rag_module, db, monkeypatch):
    kb = add_scope(db, ["a"])
    original = "【语音转写】\n[00:05-00:19] 先创建目录，再安装依赖，最后启动服务。"
    monkeypatch.setattr(rag_module, "get_chroma_service", lambda: SimpleNamespace(
        count=lambda: 1, search=lambda *args, **kwargs: [hit("a", original)]))
    prompts = []

    def generate(**kwargs):
        prompts.append(kwargs)
        return "先创建目录，再安装依赖，最后启动服务。[来源: 视频a]"

    monkeypatch.setattr(rag_module.llm_client, "chat", generate)
    monkeypatch.setattr(rag_module.llm_client, "stream_chat", lambda **kwargs: iter([generate(**kwargs)]))
    service = rag_module.RagService()
    for streaming in [False, True]:
        finish(service, db, streaming, "视频中的操作步骤是什么？", kb_id=kb.id)
    assert len(prompts) == 2
    assert prompts[0] == prompts[1]
    assert original in prompts[0]["user_prompt"]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("truncate_first", [False, True])
def test_character_budget_limits_prompt_trace_and_citations_together(
        rag_module, db, monkeypatch, streaming, truncate_first):
    kb = add_scope(db, ["a", "b"])
    original = "【语音转写】\n[00:02] RETAINED_A " + "原句必须完整地留在预算内。" * 30 + "UNSENT_TAIL_A"
    first = hit("a", original)
    second = hit("b", "【语音转写】\n[00:50] UNSENT_B 第二个视频的正文。")
    service = rag_module.RagService()
    cap = 140 if truncate_first else len(service._format_context_blocks([first])) + 1
    monkeypatch.setattr(rag_module.settings, "rag_context_count", 5)
    monkeypatch.setattr(rag_module.settings, "rag_prompt_max_context_chars", cap)
    selected = service._context_hits([first, second])
    assert [item["chunk_id"] for item in selected] == ["a:1"]
    actual_context = service._format_context_blocks(selected)
    assert len(actual_context) <= cap
    assert "UNSENT_B" not in actual_context
    if truncate_first:
        assert "UNSENT_TAIL_A" not in actual_context
        assert "后文未提供" in actual_context
    else:
        assert original in actual_context

    monkeypatch.setattr(rag_module, "get_chroma_service", lambda: SimpleNamespace(
        count=lambda: 2, search=lambda *args, **kwargs: [first, second]))
    prompts = []

    def generate(**kwargs):
        prompts.append(kwargs)
        # Even if an upstream model invents an unseen source label, the final
        # source cards and persisted references must not advertise that video.
        return "甲资料的原句。[来源: 资料1] 猜测的乙资料。[来源: 资料2]"

    monkeypatch.setattr(rag_module.llm_client, "chat", generate)
    monkeypatch.setattr(rag_module.llm_client, "stream_chat", lambda **kwargs: iter([generate(**kwargs)]))
    result = finish(service, db, streaming, "这个视频具体说了什么？", kb_id=kb.id)
    assert len(prompts) == 1
    assert actual_context in prompts[0]["user_prompt"]
    assert "UNSENT_B" not in prompts[0]["user_prompt"]
    assert [item["chunk_id"] for item in result["trace"]["chunks"]] == ["a:1"]
    assert result["trace"]["chunks"][0]["text"] == selected[0]["text"][:200]
    assert [item["platform_item_id"] for item in result["sources"]] == ["a"]
    assert "[来源: 资料2]" not in result["answer"]
    assistant = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
    assert json.loads(assistant.retrieved_video_ids) == ["a"]
    assert json.loads(assistant.retrieved_chunk_ids) == ["a:1"]


def test_context_too_small_for_a_source_label_has_no_evidence(rag_module, monkeypatch):
    monkeypatch.setattr(rag_module.settings, "rag_prompt_max_context_chars", 5)
    service = rag_module.RagService()
    selected = service._context_hits([hit("a", "【语音转写】\n正文不能只剩来源标题。")])
    assert selected == []
    assert service._cited_sources("错误标注。[来源: 资料1]", selected) == []


@pytest.mark.parametrize("streaming", [False, True])
def test_video_numbers_are_stable_across_chunks_and_same_titles_need_numbered_citations(
        rag_module, db, monkeypatch, streaming):
    kb = add_scope(db, ["a", "b"])
    same_title = "同名视频"
    chunks = [
        hit("a", "【语音转写】\nFIRST_VIDEO_FIRST_CHUNK。", title=same_title, index=1),
        hit("a", "【语音转写】\nFIRST_VIDEO_SECOND_CHUNK。", title=same_title, index=2),
        hit("b", "【语音转写】\nSECOND_VIDEO_FIRST_CHUNK。", title=same_title, index=1),
    ]
    service = rag_module.RagService()
    context = service._compress_chunks("查找正文", chunks)
    assert context.count("【资料1｜同名视频】") == 2
    assert context.count("【资料2｜同名视频】") == 1
    assert "【资料3｜" not in context
    assert [source["platform_item_id"] for source in service._cited_sources(
        "第二个视频的结论。[来源: 资料2]", chunks)] == ["b"]
    assert service._cited_sources("不知道哪个视频。[来源: 同名视频]", chunks) == []
    assert "[来源:" not in service._ensure_sources(
        "模糊同名引用。[来源: 同名视频]", service._source_candidates(chunks))

    monkeypatch.setattr(rag_module, "get_chroma_service", lambda: SimpleNamespace(
        count=lambda: 3, search=lambda *args, **kwargs: chunks))
    prompts = []

    def generate(**kwargs):
        prompts.append(kwargs)
        return "只采用第二个视频的正文。[来源: 资料2] 不接受模糊标注。[来源: 同名视频]"

    monkeypatch.setattr(rag_module.llm_client, "chat", generate)
    monkeypatch.setattr(rag_module.llm_client, "stream_chat", lambda **kwargs: iter([generate(**kwargs)]))
    result = finish(service, db, streaming, "第二个视频具体说了什么？", kb_id=kb.id)
    assert len(prompts) == 1 and context in prompts[0]["user_prompt"]
    assert [source["platform_item_id"] for source in result["sources"]] == ["b"]
    assert [item["chunk_id"] for item in result["trace"]["chunks"]] == ["a:1", "a:2", "b:1"]
    assistant = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
    assert json.loads(assistant.retrieved_video_ids) == ["b"]
    assert json.loads(assistant.retrieved_chunk_ids) == ["b:1"]


@pytest.mark.parametrize("route", ["vector", "db_content"])
def test_final_generation_contract_distinguishes_evidence_history_and_missing_details(rag_module, route):
    # This checks the actual messages sent to a model, not how any model obeys
    # them. Real-answer accuracy is measured separately on the fixed exam.
    service = rag_module.RagService()
    original = "【资料1｜测试视频】\n【语音转写】\n只说先备份，没有提期限或保存路径。"
    history = "助手: 上次错误地说要保留30天。"
    system, user, structured = service._build_prompts(
        route, "请列出六个原因、备份步骤和保留期限", original, history)
    assert structured
    assert original in user and history in user
    assert "仅帮助理解指代，不是事实证据" in user
    assert "只能依据本次提供的原文回答" in system
    assert "标题相近不等于正文支持" in system
    assert "当前资料未提供/没有找到依据" in system
    assert "数字、文件路径" in system and "操作步骤" in system
    assert "部分有依据时只答有依据的部分" in system
    assert "不要自动改答通用知识" in system
    assert "[来源: 资料1]" in system
    assert "不能为限制条数漏掉必要信息" in system
    assert "最多3点" not in system and "最多 3 点" not in system


@pytest.mark.parametrize("streaming", [False, True])
def test_database_summary_fallback_is_numbered_scoped_and_saved_as_actual_evidence(
        rag_module, db, monkeypatch, streaming):
    kb = add_scope(db, ["a"])
    row = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == "a"))
    row.transcript_text = "FALLBACK_FLAT_TRANSCRIPT 不应覆盖已有带时间的完整片段。"
    row.visual_analysis_text = "FALLBACK_VISUAL 原画面展示了导出按钮。"
    segments = [
        {"start_seconds": 10.5, "end_seconds": 21.25,
         "text": "FALLBACK_SPEECH 先打开设置，再选择导出目录。"},
        {"start_seconds": 21.25, "end_seconds": 37,
         "text": "FALLBACK_LAST 最后点击导出，原文没有提供备份保留期限。"},
    ]
    row.analysis_json = json.dumps({"transcript_segments": segments}, ensure_ascii=False)
    db.add(VideoCache(platform_item_id="foreign", title="FOREIGN_TITLE",
                      status="done", transcript_text="FOREIGN_UNNUMBERED_TEXT"))
    db.add(VideoCache(platform_item_id="pending", title="PENDING_TITLE",
                      status="pending", transcript_text="PENDING_UNNUMBERED_TEXT"))
    db.add(KnowledgeBaseVideo(knowledge_base_id=kb.id, platform_item_id="pending"))
    db.commit()

    service = rag_module.RagService()
    # Building from an explicitly empty evidence list must not secretly read
    # the database. The public pipeline must construct fallback records first.
    assert service._build_context("db_content", [], db, "总结一下", {"a"}) == ""
    retrievals, prompts = [], []

    def retrieve(query, scope_ids, *args, **kwargs):
        retrievals.append((query, scope_ids))
        return []

    monkeypatch.setattr(service, "_dense_retrieve", retrieve)

    def generate(**kwargs):
        prompts.append(kwargs)
        return "先打开设置并选择导出目录，再点击导出。资料没有提供备份期限。[来源: 资料1]"

    monkeypatch.setattr(rag_module.llm_client, "chat", generate)
    monkeypatch.setattr(rag_module.llm_client, "stream_chat", lambda **kwargs: iter([generate(**kwargs)]))
    result = finish(service, db, streaming, "总结一下", kb_id=kb.id)
    assert retrievals == [("总结一下", {"a"})]
    assert result["route_type"] == "db_content"
    assert len(prompts) == 1
    prompt = prompts[0]["user_prompt"]
    assert prompt.count("【资料1｜视频a】") == 2
    assert "语音文字稿（来自视频声音）" in prompt
    assert "画面分析（来自视频画面）" in prompt
    assert all(segment["text"] in prompt for segment in segments)
    assert "10.5" in prompt and "21.25" in prompt and "37 秒" in prompt
    assert "FALLBACK_VISUAL" in prompt
    assert "FALLBACK_FLAT_TRANSCRIPT" not in prompt
    assert all(token not in prompt for token in ["FOREIGN_TITLE", "FOREIGN_UNNUMBERED_TEXT",
                                               "PENDING_TITLE", "PENDING_UNNUMBERED_TEXT"])
    assert [source["platform_item_id"] for source in result["sources"]] == ["a"]
    traced = result["trace"]["chunks"]
    assert len(traced) == 2
    assert all(item["chunk_id"].startswith("a:db:") for item in traced)
    assert all(item["text"] in prompt for item in traced)
    assert any("FALLBACK_SPEECH" in item["text"] for item in traced)
    assert any("FALLBACK_VISUAL" in item["text"] for item in traced)
    assistant = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
    assert json.loads(assistant.retrieved_video_ids) == ["a"]
    assert json.loads(assistant.retrieved_chunk_ids) == [item["chunk_id"] for item in traced]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("answer,expected_ids", [
    pytest.param("第一项的来源是资料1；后面的说明同样来自资料1。", {"a"},
                 id="real-prose-source-is-and-also-from"),
    pytest.param("这一项内容来自资料2。", {"b"}, id="prose-from-second-source"),
    pytest.param("第一项依据资料1，第二项根据资料2。", {"a", "b"},
                 id="explicit-basis-and-according-to"),
    pytest.param("这一项的来源是资料99，也来自资料123。", set(),
                 id="unknown-prose-source-numbers-are-not-whitelisted"),
    pytest.param("这句话不是来自资料1，也不能根据资料2得出。", set(),
                 id="negated-prose-citations-do-not-become-evidence"),
])
def test_explicit_prose_citations_use_same_whitelist_in_answers_and_history(
        rag_module, db, monkeypatch, streaming, answer, expected_ids):
    kb = add_scope(db, ["a", "b"])
    chunks = [hit(item, f"【语音转写】\n[00:01] 实际正文证据_{item}。") for item in ["a", "b"]]
    monkeypatch.setattr(rag_module, "get_chroma_service", lambda: SimpleNamespace(
        count=lambda: 2, search=lambda *args, **kwargs: chunks))
    monkeypatch.setattr(rag_module.llm_client, "chat", lambda **kwargs: answer)
    monkeypatch.setattr(rag_module.llm_client, "stream_chat", lambda **kwargs: iter([answer]))
    service = rag_module.RagService()
    result = finish(service, db, streaming, "这些资料中具体说了什么？", kb_id=kb.id)
    assert {source["platform_item_id"] for source in result["sources"]} == expected_ids
    for item in expected_ids:
        assert f"《视频{item}》" in result["answer"]
    if expected_ids:
        assert "资料1" not in result["answer"] and "资料2" not in result["answer"]
    else:
        assert "《视频a》" not in result["answer"] and "《视频b》" not in result["answer"]
    assert "参考来源" not in result["answer"]
    assistant = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
    assert set(json.loads(assistant.retrieved_video_ids)) == expected_ids
    assert set(json.loads(assistant.retrieved_chunk_ids)) == {f"{item}:1" for item in expected_ids}
    assert assistant.content == result["answer"]
    restored = service.get_messages(db, result["session_id"])
    restored_assistant = next(message for message in restored if message["role"] == "assistant")
    assert {source["platform_item_id"] for source in restored_assistant["sources"]} == expected_ids
    assert restored_assistant["content"] == result["answer"]
