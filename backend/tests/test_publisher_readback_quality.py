"""Evidence quality is bounded metadata, never a second publish decision."""
import json

import pytest

from tests.test_mention_agent_publisher import FakeAdapter, make_context


class QualityAdapter(FakeAdapter):
    def __init__(self, quality):
        super().__init__()
        self.quality=quality
    def submit_reply(self,target,text,*,before_submit):
        result=super().submit_reply(target,text,before_submit=before_submit)
        return {**result,'reply_readback_quality':self.quality}


@pytest.mark.parametrize('quality',['weak_unique_root','direct_selected_parent'])
def test_positive_quality_retained_without_changing_gate_or_click(make_context,quality):
    context=make_context(adapter=QualityAdapter(quality))
    result=context.publisher.publish(context.task_id)
    assert result['state']=='b_view_visible'
    assert context.adapter.gates==context.adapter.clicks==1
    assert context.store.get_reply(context.task_id)['evidence']['reply_readback_quality']==quality
    saved=json.loads(next((context.root/'evidence').rglob('result.json')).read_text(encoding='utf-8'))
    assert saved['reply_readback_quality']==quality


@pytest.mark.parametrize('quality',[None,{'secret':'SYNTHETIC_PRIVATE'},['weak_unique_root'],17,'SYNTHETIC_PRIVATE'])
def test_unknown_or_private_quality_not_projected_or_treated_as_publication_failure(make_context,quality):
    context=make_context(adapter=QualityAdapter(quality))
    assert context.publisher.publish(context.task_id)['state']=='b_view_visible'
    assert context.adapter.gates==context.adapter.clicks==1
    assert 'reply_readback_quality' not in context.store.get_reply(context.task_id)['evidence']
    saved=json.loads(next((context.root/'evidence').rglob('result.json')).read_text(encoding='utf-8'))
    assert 'reply_readback_quality' not in saved
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(saved)


def test_failed_target_lookup_saves_only_bounded_integer_counts(make_context):
    adapter=FakeAdapter(prepare_error=RuntimeError('SYNTHETIC_PRIVATE_ERROR'))
    adapter.last_original_lookup={'candidate_limit':3,'candidate_count':2,'checked_count':1,
        'match_count':True,'final_verified':'SYNTHETIC_PRIVATE','page_open_count':100001,
        'reason_code':'SYNTHETIC_PRIVATE','raw_body':'SYNTHETIC_PRIVATE'}
    context=make_context(adapter=adapter)
    result=context.publisher.publish(context.task_id)
    assert result['state']=='preflight_blocked'
    assert adapter.gates==adapter.clicks==0
    saved=json.loads(next((context.root/'evidence').rglob('preflight_failure.json')).read_text(encoding='utf-8'))
    assert saved=={'reason_code':'preflight_failed','original_lookup':{
        'candidate_limit':3,'candidate_count':2,'checked_count':1}}
    assert context.store.get_reply(context.task_id)['send_token'] is None
