"""Real TEMP inbox/plans/Registry; fake provider and fixed GETs, no account/UI."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import ast
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from app.assistant.native_events import project_page
from app.assistant.native_semantic_inbox import NativeSemanticInbox
from app.assistant.native_source_store import NativeSourceStore
from app.assistant.native_lifecycle import SessionLifecycleError
from app.assistant.registry import Registry
from app.assistant.semantic_plan_store import SemanticPlanStore, SemanticPlanError
from app.assistant.semantic_routing import SemanticIntentInterpreter
from app.assistant.usage_budget import UsageBudget
from app.mention_agent.contracts import Binding
from app.mention_agent.model import ModelTurn
from tests import test_native_empty_nonterminal_retry as fixtures


class Planner:
    def __init__(self): self.calls, self.hook = 0, None
    def complete(self, messages, tools):
        self.calls += 1
        if self.hook: self.hook()
        return ModelTurn({'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'synthetic-meaning-once', 'type': 'function', 'function': {'name': 'submit_intent',
             'arguments': json.dumps({'action': 'query', 'target_state': 'unspecified', 'grant_id': None})}}]},
             {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20}, 'tool_calls')


class PendingTests(unittest.TestCase):
    def setUp(self):
        f = fixtures.EmptyPageTests('test_one_empty_page_cools_down_without_accepting_or_advancing_then_recovers')
        f.setUp(); self.addCleanup(f.doCleanups); self.f = f
        f.network.mode = 'ok'; f.lane.poll_native(); f.network.mode = 'empty'
        self.assertTrue(f.store.arm_if_ready(True))
        root = Path(f.temp.name).resolve()
        self.inbox = NativeSemanticInbox.prepare(root/'meaning.sqlite3', fixtures.UID, fixtures.PROFILE,
            clock=lambda: f.now[0], source_authorizer=f.store.native_meaning_checkpoint)
        self.inbox.enable_new_messages(); f.now[0] += 1
        raw = fixtures.notice(9); raw['nid_str'] = '1003'; raw['create_time'] = str(int(f.now[0]))
        raw['at']['schema_url'] = 'aweme://aweme/detail/'+fixtures.VIDEO+'?cid=2003'
        raw['at']['content'] = 'fixture valid pending question'
        projection = project_page(fixtures.page([raw], more=1, minimum='10', maximum='20'), 9,
            receiver_uid=fixtures.UID, receiver_sec_uid=fixtures.SEC,
            profile_allowed=lambda profile: profile == fixtures.AUTHOR_PROFILE)
        f.store.record_page(f.store.prepare_channel(9, '601'), projection,
            next_cursor=('10', '20'), has_more=True, allow_live=True)
        f.store.set_verified(); self.message = projection['candidates'][0]
        self.assertTrue(f.store.native_meaning_checkpoint(self.message))
        [self.key] = self.inbox.record_messages([self.message], page_fingerprint=projection['fingerprint'])
        self.registry = Registry(root/'registry.sqlite3', catalog_reader=lambda: [{'id': 7, 'name': 'Fixture notes'}])
        binding = Binding('fixture-empty-binding', 'fixture-empty-instance', fixtures.AUTHOR_PROFILE, fixtures.PROFILE, 7, True)
        migrated = self.registry.import_legacy(binding, verified_by='fixture-local-owner', allow_import=True)
        self.migrated = migrated
        self.registry.set_local_owner_access(migrated['user_id'], migrated['device_id'], query_all_local=True,
            catalog_read_allowed=True, public_reply_allowed=True, grant_current_libraries=True, verified_by='fixture-owner')
        self.plans = SemanticPlanStore.prepare(root/'semantic-plans.sqlite3', clock=lambda: f.now[0])
        self.budget = UsageBudget.prepare(root/'usage-budget.sqlite3', clock=lambda: datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.model = Planner(); self.stopped = False
        self.interpreter = SemanticIntentInterpreter(self.registry, self.plans, self.model, self.budget,
            should_stop=lambda: self.stopped)
        self.interpreter.configure_native(self.inbox, f.store.native_meaning_checkpoint,
            source_store=f.store, session_lifecycle=f.life)

    def pending(self):
        row = self.inbox.lookup(self.key)
        self.assertEqual((row['state'], row['attempts']), ('pending', 0))

    def test_cooldown_and_expired_wait_preserve_pending_bytes_then_understand_once(self):
        self.pending(); before = self.inbox.path.read_bytes()
        self.f.fail_poll()
        for advance in (0, 45):
            self.f.now[0] += advance
            self.assertIsNone(self.interpreter.process_native_once('fixture-worker'))
            self.assertEqual(self.inbox.path.read_bytes(), before); self.pending()
        self.assertEqual(self.model.calls, 0)
        self.f.network.mode = 'ok'; self.f.lane.poll_native()
        result = self.interpreter.process_native_once('fixture-worker')
        self.assertEqual(result['state'], 'understood', result)
        self.assertIsNone(self.interpreter.process_native_once('fixture-worker'))
        row = self.inbox.lookup(self.key)
        self.assertEqual((row['state'], row['attempts'], self.model.calls), ('understood', 1, 1))
        self.assertEqual(self.budget.summary()['all_time']['request_count'], 1)
        self.assertEqual(self.f.ui_calls, [])

    def test_worker_in_source_pause_to_failure_record_gap_does_not_claim(self):
        before = self.inbox.path.read_bytes(); enter = threading.Event(); done = threading.Event()
        results, errors = [], []
        def worker():
            if not enter.wait(3): errors.append('owner barrier not reached'); return
            try: results.append(self.interpreter.process_native_once('fixture-worker'))
            except BaseException as error: errors.append(type(error).__name__)
            finally: done.set()
        thread = threading.Thread(target=worker); thread.start()
        original = self.f.life._record_poll_failure
        def record(code, **facts):
            # Receiver.pause is already durable, but cycle has not yet counted
            # the failure and has cleared the pinned session: the old gap.
            self.assertFalse(self.f.store.native_meaning_checkpoint(self.message))
            self.assertEqual(self.f.life.safe_status()['transient_failures'], 0)
            enter.set(); self.assertTrue(done.wait(3))
            return original(code, **facts)
        try:
            with patch.object(self.f.life, '_record_poll_failure', side_effect=record): self.f.fail_poll()
        finally:
            enter.set(); thread.join(3)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, []); self.assertEqual(results, [None])
        self.assertEqual(self.inbox.path.read_bytes(), before); self.pending(); self.assertEqual(self.model.calls, 0)

    def test_worker_claims_first_model_is_not_locked_and_later_revoke_does_not_replay(self):
        provider_entered, provider_release = threading.Event(), threading.Event()
        results, errors = [], []
        def provider():
            provider_entered.set()
            if not provider_release.wait(4): raise AssertionError('model held by scheduling lock')
        self.model.hook = provider
        def worker():
            try: results.append(self.interpreter.process_native_once('fixture-worker'))
            except BaseException as error: errors.append(type(error).__name__)
        thread = threading.Thread(target=worker); thread.start()
        try:
            self.assertTrue(provider_entered.wait(3), {'results': results, 'errors': errors,
                'inbox_state': self.inbox.lookup(self.key)['state'], 'reason_code': self.inbox.lookup(self.key)['reason_code']})
            self.assertEqual(self.inbox.lookup(self.key)['state'], 'claimed')
            # Owner network can complete while the provider is blocked. Its
            # later Source revocation remains authoritative to the old gates.
            self.f.fail_poll()
        finally:
            provider_release.set(); thread.join(4)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(results[0]['state'], 'held')
        self.assertEqual((self.inbox.lookup(self.key)['attempts'], self.model.calls), (1, 1))
        self.f.now[0] += 45; self.f.network.mode = 'ok'; self.f.lane.poll_native()
        self.assertIsNone(self.interpreter.process_native_once('fixture-worker'))
        self.assertEqual((self.inbox.lookup(self.key)['state'], self.model.calls), ('held', 1))

    def test_auth_is_not_temporary_permission(self):
        before = self.inbox.path.read_bytes()
        self.f.network.mode = 'auth'
        with self.assertRaises(fixtures.NativeSessionPaused): self.f.lane.poll_native()
        self.assertIsNone(self.interpreter.process_native_once('fixture-worker'))
        self.pending(); self.assertEqual(self.inbox.path.read_bytes(), before)
        self.assertEqual(self.model.calls, 0)
        self.f.now[0] += 1000
        self.assertIsNone(self.interpreter.process_native_once('fixture-worker'))
        self.assertEqual(self.f.life.safe_status()['state'], 'paused')

    def test_unknown_or_unrelated_source_failure_is_not_deferred(self):
        self.f.store.pause('unrelated_source_failure')
        self.assertIsNone(self.interpreter.process_native_once('fixture-worker'))
        self.assertEqual((self.inbox.lookup(self.key)['state'], self.model.calls), ('held', 0))

    def test_real_registry_withdrawal_is_not_deferred_or_allowed(self):
        self.registry.revoke_device(self.migrated['device_id'])
        self.assertEqual(self.interpreter.process_native_once('fixture-worker')['state'], 'held')
        self.assertEqual((self.inbox.lookup(self.key)['state'], self.model.calls), ('held', 0))

    def test_main_injects_only_the_actual_native_lane_lifecycle(self):
        from app.assistant import semantic_routing
        path = Path(semantic_routing.__file__).with_name('__main__.py')
        tree = ast.parse(path.read_text(encoding='utf8'))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == 'configure_native']
        self.assertEqual(len(calls), 1)
        values = [kw.value for kw in calls[0].keywords if kw.arg == 'session_lifecycle']
        self.assertEqual(len(values), 1)
        self.assertIsInstance(values[0], ast.Call)
        self.assertEqual(values[0].func.id, 'getattr')
        self.assertEqual(values[0].args[0].id, 'lane')
        self.assertEqual(values[0].args[1].value, 'session_lifecycle')
        self.assertIsNone(values[0].args[2].value)

    def test_stopped_worker_and_wrong_lifecycle_type_do_not_claim(self):
        before = self.inbox.path.read_bytes(); self.stopped = True
        self.assertIsNone(self.interpreter.process_native_once('fixture-worker'))
        self.assertEqual(self.inbox.path.read_bytes(), before)
        for value in ('cooldown', {'state': 'cooldown'}, lambda: True):
            with self.subTest(type=type(value).__name__), self.assertRaises(SemanticPlanError):
                self.interpreter.configure_native(self.inbox, self.f.store.native_meaning_checkpoint, session_lifecycle=value)
        self.pending()

    def test_prepare_auth_pause_before_Source_pause_never_claims_old_verified_text(self):
        before = self.inbox.path.read_bytes(); enter = threading.Event(); done = threading.Event()
        results, errors, observed = [], [], []
        def worker():
            if not enter.wait(3): errors.append('owner barrier not reached'); return
            try: results.append(self.interpreter.process_native_once('fixture-worker'))
            except BaseException as error: errors.append(type(error).__name__)
            finally: done.set()
        thread = threading.Thread(target=worker); thread.start(); original = self.f.store.pause
        def pause(code):
            if code == 'verification_required' and not observed:
                # cycle.finally has cleared the active marker, but Lane has
                # not yet committed Source.pause: old freshness is still true.
                self.assertTrue(self.f.store.native_meaning_checkpoint(self.message))
                self.assertEqual(self.f.life.safe_status()['reason_code'], 'verification_required')
                observed.append(True); enter.set(); self.assertTrue(done.wait(3))
            return original(code)
        try:
            with (patch.object(self.f.life._capture, 'guard', side_effect=SessionLifecycleError('verification_required')),
                  patch.object(self.f.store, 'pause', side_effect=pause)):
                with self.assertRaises(fixtures.NativeSessionPaused): self.f.lane.poll_native()
        finally:
            enter.set(); thread.join(3)
        self.assertEqual(observed, [True]); self.assertFalse(thread.is_alive())
        self.assertEqual(errors, []); self.assertEqual(results, [None]); self.pending()
        self.assertEqual((self.inbox.path.read_bytes(), self.model.calls), (before, 0))

    def test_not_started_and_invalid_status_veto_without_using_old_source_freshness(self):
        before = self.inbox.path.read_bytes(); self.f.fresh_runtime()
        self.interpreter.configure_native(self.inbox, self.f.store.native_meaning_checkpoint,
            source_store=self.f.store, session_lifecycle=self.f.life)
        self.assertTrue(self.f.store.native_meaning_checkpoint(self.message))
        self.assertIsNone(self.interpreter.process_native_once('fixture-worker'))
        with patch.object(self.f.life, 'safe_status', return_value={'state': 'ready'}):
            self.assertIsNone(self.interpreter.process_native_once('fixture-worker'))
        self.assertEqual(self.inbox.path.read_bytes(), before); self.pending(); self.assertEqual(self.model.calls, 0)

    def test_actual_Source_UID_and_profile_must_match_inbox_and_lifecycle(self):
        for uid, profile in (('9002', fixtures.PROFILE),
                             (fixtures.UID, 'https://www.douyin.com/user/fixture_other_receiver')):
            with self.subTest(uid=uid, profile=profile):
                source = NativeSourceStore(Path(self.f.temp.name).resolve()/('wrong-'+uid+str(len(profile))+'.sqlite3'),
                    uid, profile, create=True, clock=lambda: self.f.now[0])
                with self.assertRaises(SemanticPlanError):
                    self.interpreter.configure_native(self.inbox, source.native_meaning_checkpoint,
                        source_store=source, session_lifecycle=self.f.life)
        self.pending(); self.assertEqual(self.model.calls, 0)


if __name__ == '__main__': unittest.main()
