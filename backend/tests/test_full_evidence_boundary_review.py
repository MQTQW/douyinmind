"""Independent trace-vs-prompt boundary review with real RAG and fake generation."""
from copy import deepcopy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.services import rag_service as module
from app.services.retrieval_context import expand_hit_context

SPEECH = '语音文字稿（来自视频声音）：\n'
PRIVATE = 'FOREIGN_LIBRARY_PRIVATE_SENTINEL'
GOLD = 'GOLD_EXTRA_METADATA_MUST_NOT_ENTER_PROMPT'


def hit(index, body, *, video='A', title='测试源视频'):
    return {'platform_item_id': video, 'chunk_id': f'{video}:{index}', 'chunk_index': index,
            'title': title, 'text': SPEECH+body, 'score': .7}


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([KnowledgeBase(id=1, name='当前库', name_key='当前库'),
                    KnowledgeBase(id=2, name='其他库', name_key='其他库')])
        for video, kb in [('A', 1), ('C', 1), ('B', 2)]:
            db.add(VideoCache(platform_item_id=video, title='测试源视频', status='done'))
            db.flush()
            db.add(KnowledgeBaseVideo(knowledge_base_id=kb, platform_item_id=video))
        db.commit()
        yield db
    engine.dispose()


def run(db, monkeypatch, hits, *, streaming, budget=8000, query='测试条件和结果分别是什么？'):
    calls = []
    service = module.RagService()
    monkeypatch.setattr(settings, 'rag_prompt_max_context_chars', budget)
    monkeypatch.setattr(settings, 'rag_context_count', 8)
    def fake_chat(system_prompt, user_prompt, **kwargs):
        calls.append((system_prompt, user_prompt))
        return '按实际原文回答。[来源: 资料1]'
    def fake_stream(system_prompt, user_prompt, **kwargs):
        calls.append((system_prompt, user_prompt))
        yield '按实际原文回答。[来源: 资料1]'
    monkeypatch.setattr(module.llm_client, 'chat', fake_chat)
    monkeypatch.setattr(module.llm_client, 'stream_chat', fake_stream)
    monkeypatch.setattr(service, '_dense_retrieve', lambda *args, **kwargs: hits)
    if streaming:
        events = list(service.answer_stream(db, query, None, knowledge_base_id=1))
        assert events[-1] == ('done', {'ok': True})
        result = next(payload for event, payload in events if event == 'meta')
    else:
        result = service.answer(db, query, None, knowledge_base_id=1)
    assert len(calls) == 1, 'Evidence expansion must not add a generation call'
    assert PRIVATE not in str(calls)+str(result)
    assert GOLD not in str(calls)+str(result)
    return service, result, calls[0]


def assert_exact_model_context(service, result, call, allowed, marker='相关内容：\n'):
    chunks = result['trace']['chunks']
    hits = []
    for chunk in chunks:
        video = chunk['chunk_id'].split(':', 1)[0]
        assert video in allowed
        assert chunk['text'] == chunk['full_text'][:200]
        hits.append({'platform_item_id': video, 'title': chunk['title'], 'text': chunk['full_text']})
    expected = service._format_context_blocks(hits)
    actual = call[1].split(marker, 1)[1]
    assert actual == expected, 'All full_text must reconstruct exactly what the model received'
    return chunks, actual


@pytest.mark.parametrize('streaming', [False, True])
def test_trace_full_text_is_final_scoped_deduplicated_prompt_not_original_record(db, monkeypatch, streaming):
    body = '独立事实需要逐句核对。'*25+'关键句后半真正结束。'
    original = hit(1, body)
    original['full_text'] = 'WRONG_UNPROVIDED_DOCUMENT_TAIL'
    original['gold_answer'] = GOLD
    metadata = {'platform_item_id': 'A', 'chunk_id': 'A:0', 'title': '测试源视频',
                'text': '视频标题：测试源视频\n作者：不是正文', 'score': .9}
    hits = [hit(1, PRIVATE, video='B'), metadata, original, deepcopy(original),
            hit(2, '独立事实需要逐句核对。')]
    before = deepcopy(hits)
    service, result, call = run(db, monkeypatch, hits, streaming=streaming)
    chunks, context = assert_exact_model_context(service, result, call, {'A'})
    assert len(chunks) == 1
    assert chunks[0]['full_text'] == SPEECH+body
    assert '关键句后半真正结束' not in chunks[0]['text']
    assert '关键句后半真正结束' in chunks[0]['full_text']
    assert 'WRONG_UNPROVIDED_DOCUMENT_TAIL' not in context+str(result)
    assert hits == before, 'Trace assembly must not mutate candidates'


@pytest.mark.parametrize('streaming', [False, True])
def test_oversize_first_hit_exposes_only_bounded_sentences_and_limit_marker(db, monkeypatch, streaming):
    original = hit(1, '首句是完整的事实。'+'超长未结束内容'*180+'UNPROVIDED_TAIL_SENTINEL')
    before = deepcopy(original)
    service, result, call = run(db, monkeypatch, [original], streaming=streaming, budget=140)
    chunks, context = assert_exact_model_context(service, result, call, {'A'})
    assert len(chunks) == 1 and len(context) <= 140
    assert '[此片段超过长度限制，后文未提供]' in chunks[0]['full_text']
    assert 'UNPROVIDED_TAIL_SENTINEL' not in str(result)+call[1]
    assert original == before


@pytest.mark.parametrize('streaming', [False, True])
def test_budget_includes_long_headers_and_skips_not_provided_later_hits(db, monkeypatch, streaming):
    first = hit(1, '当前短事实完整。', title='很长的正式标题'*24)
    # This second document is in the current scope, but does not fit the
    # remaining formatted context budget. It must not become a details entry.
    omitted = hit(1, 'WITHIN_SCOPE_BUT_NOT_PROVIDED_SENTINEL。'+'有句号的较长事实。'*20,
                  video='C', title='另一条长标题'*15)
    budget = len(module.RagService._format_context_blocks([first]))
    service, result, call = run(db, monkeypatch, [first, omitted], streaming=streaming, budget=budget)
    chunks, context = assert_exact_model_context(service, result, call, {'A'})
    assert len(context) == budget
    assert len(chunks) == 1
    assert 'WITHIN_SCOPE_BUT_NOT_PROVIDED_SENTINEL' not in str(result)+call[1]


@pytest.mark.parametrize('streaming', [False, True])
def test_neighbor_completed_text_and_provenance_match_exact_provided_block(db, monkeypatch, streaming):
    overlap = '这段唯一重叠原文用于可靠定位前后内容不能改写也不能擅自替换任何原有文字'
    previous = hit(0, '前一句结束。需要补回的完整开头'+overlap)
    center = hit(1, overlap+'这一句最终结束。')
    wrong = hit(0, PRIVATE+overlap, video='B')
    expanded = expand_hit_context([center], [wrong, previous, center], max_extra_chars=100)
    assert '需要补回的完整开头' in expanded[0]['text']
    service, result, call = run(db, monkeypatch, expanded, streaming=streaming)
    chunks, context = assert_exact_model_context(service, result, call, {'A'})
    assert chunks[0]['full_text'] == expanded[0]['text']
    assert chunks[0]['expanded_from_chunk_ids'] == ['A:0', 'A:1']
    assert PRIVATE not in context


@pytest.mark.parametrize('streaming', [False, True])
def test_overall_summary_database_fallback_trace_matches_actual_scoped_prompt(db, monkeypatch, streaming):
    db.query(VideoCache).filter(VideoCache.platform_item_id == 'A').one().transcript_text = '当前库整段真实原话。'*30+'总结原句的末尾。'
    db.query(VideoCache).filter(VideoCache.platform_item_id == 'B').one().transcript_text = PRIVATE
    db.commit()
    service, result, call = run(db, monkeypatch, [], streaming=streaming, query='总结一下')
    assert result['route_type'] == 'db_content'
    chunks, context = assert_exact_model_context(service, result, call, {'A'}, marker='内容片段：\n')
    assert chunks and '总结原句的末尾' in context


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('query,route', [('你好', 'direct'), ('列出所有视频', 'db_list')])
def test_greeting_or_catalogue_does_not_advertise_candidate_bodies_as_full_text(db, monkeypatch, streaming, query, route):
    service, result, call = run(db, monkeypatch, [hit(1, '候选但本路由未提供的正文。')],
                                streaming=streaming, query=query)
    assert result['route_type'] == route
    assert result['trace']['chunks'] == []
    assert '候选但本路由未提供的正文' not in call[1]
    assert 'full_text' not in str(result)
    assert ' (B)' not in call[1]
