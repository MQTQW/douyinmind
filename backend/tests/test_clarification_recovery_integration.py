"""Real temporary ledgers across two crash windows; providers/UI are fake."""
from tests.test_assistant_choice_controller import case
from tests.test_intent_clarification_flow import flow, prepare


def test_recorded_publication_then_existing_child_link_never_replays_effects(flow):
    c = flow
    # Crash after Agent's exact B-view result but before Session projection.
    with c.store._db(write=True) as db:
        db.execute("UPDATE clarification_sessions SET state='awaiting_publication',published_reply=NULL,revision=1 WHERE session_id=?",
                   (c.session['session_id'],))
    peers = (c.sources.path, c.c.queue.path, c.c.agent.path, c.meaning.path, c.budget.path)
    before = {p: p.read_bytes() for p in peers}
    calls = len(c.planner.calls)
    assert c.coordinator.reconcile_existing()['publications_observed'] == 1
    assert c.store.for_root(c.root['id'])['state'] == 'awaiting_reply'
    assert c.coordinator.reconcile_existing()['publications_observed'] == 0
    assert all(p.read_bytes() == data for p, data in before.items())
    assert len(c.planner.calls) == calls and c.backend.posts == 0 and c.platform.submits == 1

    # A fresh reply is interpreted normally. Crash after Source/Queue admitted
    # the sole child, before Session recorded its existing-task relationship.
    sid, _, result, _ = prepare(c)
    row, thread = result[0][0], result[1]
    task = c.c.queue.enqueue(**row)
    c.c.history.bind_task(task['id'], row['binding'], row['envelope'], thread['root_reference_id'], thread)
    c.sources.admit(sid, task)
    assert c.store.for_root(c.root['id'])['state'] == 'planned'
    assert c.lane.authority_current(task) is False
    before = {p: p.read_bytes() for p in peers}
    calls = len(c.planner.calls)
    assert c.coordinator.recover_links([task])['links_repaired'] == 1
    linked = c.store.for_root(c.root['id'])
    assert linked['child_task_id'] == task['id'] and linked['state'] == 'handed_off'
    assert c.lane.authority_current(task) is True
    session_bytes = c.store.path.read_bytes()
    assert c.coordinator.recover_links([task])['links_repaired'] == 0
    assert c.coordinator.reconcile_existing()['links_repaired'] == 0
    assert c.store.path.read_bytes() == session_bytes
    assert all(p.read_bytes() == data for p, data in before.items())
    assert len(c.planner.calls) == calls and c.backend.posts == 0 and c.platform.submits == 1
