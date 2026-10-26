"""Actual Engine -> pinned transport -> ASGI service -> temporary SQL scope."""
import hashlib
import json

import pytest

from app.assistant import backend_knowledge as transport
from app.mention_agent.contracts import Binding
from app.mention_agent.engine import AgentEngine
from tests.test_all_local_evidence_transport import case, VIDEOS
from tests.test_mention_agent_source_coverage_review import Model, call, answer


@pytest.mark.parametrize('focus,expected', [('authorized_scope', [VIDEOS[0], VIDEOS[2]]), ('current_video', [VIDEOS[2]])])
def test_engine_relevance_choice_reaches_actual_http_contract_and_sql_projection(case, monkeypatch, focus, expected):
    case.task['envelope']['video_id'] = VIDEOS[2]
    case.task['envelope']['comment_text'] = '这个视频里的观点，在我以前收藏的视频里有没有类似的？'
    calls = []

    class Response:
        def __init__(self, response):
            self.response = response
            self.headers = response.headers
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def getcode(self): return self.response.status_code
        def geturl(self): return transport.EVIDENCE_URL
        def read(self, maximum): return self.response.content[:maximum]

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == transport.EVIDENCE_URL
            payload = json.loads(request.data)
            calls.append(payload)
            response = case.client.post('/api/assistant/evidence/query', json=payload, headers=dict(request.header_items()))
            assert response.status_code == 200
            return Response(response)

    monkeypatch.setattr(transport.urllib.request, 'build_opener', lambda *_: Opener())
    helper = transport.BackendKnowledgeTools(case.database, None, case.task['id'], focus_video_id=VIDEOS[2],
        execution_token=case.task['execution_token'], query_scope='all_local')
    declared = ['资料' + str(i + 1) for i in range(len(expected))]
    model = Model([call('query_knowledge_base', {'question': '比较已授权资料中的类似观点', 'focus': focus}, 'query'),
                   answer('已在本轮授权范围内找到相关原文。', declared)])
    before = hashlib.sha256(case.database.read_bytes()).hexdigest()
    records = []
    result = AgentEngine(model, helper).run(Binding.from_dict(case.task['binding']), case.task['envelope'],
        record=lambda *item: records.append(item))
    assert result.proposal['kind'] == 'answer'
    assert [source['platform_item_id'] for source in result.proposal['sources']] == expected
    assert calls == [{'task_id': case.task['id'], 'question': '比较已授权资料中的类似观点', 'focus': focus}]
    assert 'video_id' not in calls[0] and 'knowledge_base_id' not in calls[0] and 'user_id' not in calls[0]
    assert len(model.requests) == 2 and len(case.calls) == 1
    assert hashlib.sha256(case.database.read_bytes()).hexdigest() == before
    assert case.task['state'] == 'executing'
    assert next(meta for kind, meta in records if kind == 'tool_result' and meta['tool_name'] == 'propose_reply')['source_ids'] == declared
