import json
from types import SimpleNamespace
import pytest
from app.assistant.native_visibility_diagnostics import load_visibility_probe,record_visibility_probe
from app.assistant.native_configuration import visibility_review_enabled


PROBE={'video_id':'7999999000000000001','current_text':'public test','previous_text':None}
ITEM={'channel':9,'notice_type':45,'at_sub_type':2,'notice_id_sha256':'a'*64,
 'visible_category':'int1','hidden_category':'missing','video_id_valid':True,'video_matches_probe':True,
 'comment_id_valid':True,'current_comment_id_matches':None,'previous_comment_id_matches':None,
 'content_is_string':True,'current_text_matches':True,'previous_text_matches':None}


def test_visibility_review_optin_never_defaults_to_enabled(tmp_path):
    assert not visibility_review_enabled(tmp_path)
    path=tmp_path/'native-visibility-review.json'
    for value in ({'version':1,'enabled':False},{'version':True,'enabled':True},
                  {'version':1,'enabled':1},{'version':1,'enabled':True,'permission':True}):
        path.write_text(json.dumps(value),encoding='utf-8')
        assert not visibility_review_enabled(tmp_path)
    path.write_text('{"version":1,"enabled":true,"enabled":true}',encoding='utf-8')
    assert not visibility_review_enabled(tmp_path)
    path.write_text('{"version":1,"enabled":true}',encoding='utf-8')
    assert visibility_review_enabled(tmp_path)


def test_only_fixed_public_probe_loads(tmp_path):
    assert load_visibility_probe(tmp_path) is None
    file=tmp_path/'native-visibility-probe.json'
    file.write_text(json.dumps(PROBE),encoding='utf-8')
    assert load_visibility_probe(tmp_path)['current_text']=='public test'
    file.write_text(json.dumps({**PROBE,'credential_path':'unexpected'}),encoding='utf-8')
    assert load_visibility_probe(tmp_path) is None
    file.write_text('{"video_id":"1","video_id":"2"}',encoding='utf-8')
    assert load_visibility_probe(tmp_path) is None


@pytest.mark.parametrize('change',[{'raw_body':'never retain'},{'current_text_matches':1},
 {'notice_id_sha256':'raw-url'},{'channel':7},{'hidden_category':'raw-value'},
 {'current_comment_id_matches':'private-id'}])
def test_unsafe_projection_never_writes(change):
    writes=[]
    control=SimpleNamespace(queue=SimpleNamespace(update_control=lambda f:writes.append(f)))
    receiver=SimpleNamespace(visibility_diagnostic_probe=PROBE,last_visibility_diagnostics=[{**ITEM,**change}])
    assert not record_visibility_probe(control,'run',SimpleNamespace(native_receiver=receiver))
    assert writes==[]


def test_projection_contains_no_probe_text_and_is_not_authority():
    state={'run_id':'run'}
    control=SimpleNamespace(queue=SimpleNamespace(update_control=lambda f:f(state)))
    receiver=SimpleNamespace(visibility_diagnostic_probe=PROBE,last_visibility_diagnostics=[ITEM])
    assert record_visibility_probe(control,'run',SimpleNamespace(native_receiver=receiver))
    encoded=json.dumps(state)
    assert 'public test' not in encoded and PROBE['video_id'] not in encoded
    assert state['native_visibility_diagnostics']['items']==[ITEM]
    assert 'not_visibility_or_execution_evidence' in state['native_visibility_diagnostics']['scope']


def test_projection_write_failure_cannot_fail_native_poll():
    def unavailable(f):raise RuntimeError('fake unavailable')
    control=SimpleNamespace(queue=SimpleNamespace(update_control=unavailable))
    receiver=SimpleNamespace(visibility_diagnostic_probe=PROBE,last_visibility_diagnostics=[ITEM])
    assert not record_visibility_probe(control,'run',SimpleNamespace(native_receiver=receiver))
