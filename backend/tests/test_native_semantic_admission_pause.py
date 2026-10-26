"""Semantic opt-in cannot consume a native/hint claim while processing pauses."""
from tests.test_assistant_native_lane import native_system
from tests.test_assistant_native_visibility_lane import case,request


def test_paused_root_planner_leaves_normal_source_unclaimed(tmp_path):
    lane,store,_,adapter,queue,*_=native_system(tmp_path)
    lane.browser_lane.semantic_resolution_provider=lambda *a,**kw:None
    queue.update_control(lambda old:{**old,'desired_state':'paused'})
    assert lane.collect()==[] and store.state=='pending'
    assert adapter.native_calls==[] and store.held==[]


def test_paused_choice_planner_leaves_normal_source_unclaimed(tmp_path):
    from types import SimpleNamespace
    lane,store,_,adapter,queue,*_=native_system(tmp_path)
    lane.choice_controller=SimpleNamespace(semantic_choice_planner=lambda **kw:None)
    queue.update_control(lambda old:{**old,'desired_state':'paused'})
    assert lane.collect()==[] and store.state=='pending'
    assert adapter.native_calls==[]


def test_paused_semantic_flag_leaves_explicit_hint_pending_without_spending(case):
    request(case)
    case.lane.browser_lane.semantic_resolution_provider=lambda *a,**kw:None
    assert case.lane.collect()==[]
    row=case.ledger.get_hint(case.hid)
    assert row['state']=='pending' and row['attempts']==0
    assert case.adapter.native_calls==[]
