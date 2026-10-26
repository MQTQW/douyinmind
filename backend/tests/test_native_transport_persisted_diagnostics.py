"""Synthetic transport/lifecycle and temporary control DB; no accounts/network."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import ast
import hashlib
import json
import sqlite3
import tempfile
import unittest
import urllib.parse

from app.assistant.control import AssistantControl
from app.assistant.queue import AssistantQueue, QueueError
from app.assistant.native_lifecycle import NativeSessionLifecycle, SessionLifecycleError, project_native_session_status
from app.assistant.native_receiver import NativeReceiver
from app.assistant.native_transport import NativeTransport, NativeTransportError, SecretSession
from app.assistant import native_lane as lane_module
from app.mention_receiver.browser import ReceiverInterventionRequired

RUN = 'a' * 32
UID = '9001'
PROFILE = 'https://www.douyin.com/user/fixture_B'
FACTS = {'endpoint_role': 'notice_count', 'http_status': 200, 'api_status_code': 80000}


def baseline():
    return {'state': 'ready', 'reason_code': None, 'generation': 1, 'refresh_count': 0,
            'source': 'owned', 'seal_state': 'not_attempted', 'transient_failures': 0,
            'last_transient_code': None, 'retry_after_seconds': 0}


class NeverInspect:
    def __str__(self): raise AssertionError('Unknown private values must not be inspected')
    __repr__ = __str__


class Network:
    def __init__(self): self.mode = 'ok'; self.roles = []
    def __call__(self, url, headers, timeout, maximum):
        # The fake records only fixed path roles, never headers or URL queries.
        path = urllib.parse.urlsplit(url).path
        role = 'identity' if path.endswith('query/user/') else 'notice_count' if path.endswith('notice/count/') else 'notice_list'
        self.roles.append(role)
        if role == 'notice_count':
            if self.mode == 'api_error': return 200, json.dumps({'status_code': 80000}).encode()
            if self.mode == 'http_error': return 503, b''
            if self.mode == 'timeout': raise TimeoutError('synthetic-private-text')
        if role == 'identity': value = {'status_code': 0, 'user_uid': UID}
        elif role == 'notice_count': value = {'status_code': 0, 'interactive_group': [{'type': 9, 'group': '601'}, {'type': 7, 'group': '2'}]}
        else: value = {'status_code': 0, 'notice_list_v2': [], 'has_more': 0, 'min_time': '0', 'max_time': '0'}
        return 200, json.dumps(value).encode()


class Fixture:
    def __init__(self):
        self.now = [100.0]; self.network = Network(); self.guard_error = None
        def session(): return SecretSession('synthetic=x', 'synthetic-UA', UID, 1000)
        def guard():
            if self.guard_error: raise SessionLifecycleError(self.guard_error)
        vault = SimpleNamespace(expected_uid=UID, load=session, save=lambda value: None)
        capture = SimpleNamespace(expected_uid=UID, guard=guard, capture=session)
        self.life = NativeSessionLifecycle(expected_uid=UID, owned_vault=vault, context_capture=capture,
                                           network=self.network, clock=lambda: self.now[0])
        store = SimpleNamespace(receiver_uid=UID, receiver_profile_url=PROFILE, pause=lambda code: None,
            safe_status=lambda: {'armed': True}, pending_count=lambda: 0,
            prepare_channel=lambda channel, group: {'cursor': ('0', '0')}, set_verified=lambda: None,
            record_page=lambda *a, **k: [], arm_if_ready=lambda allowed: None)
        transport = NativeTransport(self.life.provider, UID, self.network, clock=lambda: self.now[0])
        receiver = NativeReceiver(transport, store, receiver_uid=UID, receiver_sec_uid='fixture_B',
                                  profile_allowed=lambda profile: False)
        browser = SimpleNamespace(adapter=SimpleNamespace(expected_profile_url=PROFILE), registry=object(),
            queue=object(), conversation=object(), should_stop=lambda: False, run_id=RUN,
            last_baseline_created=False, last_detail_failure_count=0)
        self.lane = lane_module.NativeLane(browser, receiver, store, session_lifecycle=self.life)


class ProjectionTests(unittest.TestCase):
    def test_no_facts_is_exact_legacy_nine_keys_and_unknown_values_not_read(self):
        old = baseline(); old['cookie'] = NeverInspect()
        self.assertEqual(project_native_session_status(old), baseline())
        self.assertEqual(len(project_native_session_status(old)), 9)

    def test_optional_projection_exports_only_known_keys_and_copies_values(self):
        raw = baseline(); raw['transport_facts'] = {**FACTS, 'url': NeverInspect(), 'body': NeverInspect(),
                                                  'status_msg': NeverInspect(), 'headers': NeverInspect(), 'cookie': NeverInspect()}
        actual = project_native_session_status(raw)
        self.assertEqual(actual['transport_facts'], FACTS)
        raw['transport_facts']['api_status_code'] = 9
        self.assertEqual(actual['transport_facts']['api_status_code'], 80000)

    def test_known_invalid_optional_fields_reject_without_coercion_or_secret_text(self):
        for facts in (None, True, [], {}, {'endpoint_role': 'https://invalid.example'},
                {'endpoint_role': NeverInspect()}, {'endpoint_role': 'identity', 'http_status': True},
                {'endpoint_role': 'identity', 'http_status': '200'}, {'endpoint_role': 'identity', 'http_status': 600},
                {'endpoint_role': 'identity', 'api_status_code': True}, {'endpoint_role': 'identity', 'api_status_code': '0'},
                {'endpoint_role': 'identity', 'api_status_code': 2**31}):
            with self.subTest(case_type=type(facts).__name__):
                with self.assertRaisesRegex(SessionLifecycleError, '^invalid_native_session_status$'):
                    project_native_session_status({**baseline(), 'transport_facts': facts})

    def test_zero_and_bounded_signed_business_codes_remain_integers(self):
        for code in (0, -(2**31), 2**31-1):
            raw = {**baseline(), 'transport_facts': {'endpoint_role': 'identity', 'http_status': 200, 'api_status_code': code}}
            self.assertEqual(project_native_session_status(raw)['transport_facts']['api_status_code'], code)


class LifecycleTests(unittest.TestCase):
    def test_actual_receiver_failure_reaches_current_status_and_lane_error(self):
        fixture = Fixture(); fixture.network.mode = 'api_error'
        with self.assertRaises(ReceiverInterventionRequired) as caught: fixture.lane.poll_native()
        self.assertEqual(fixture.life.safe_status()['transport_facts'], FACTS)
        self.assertEqual(caught.exception.native_session_status['transport_facts'], FACTS)
        self.assertEqual(fixture.life.safe_status()['reason_code'], 'api_error')

    def test_next_no_facts_error_clears_previous_numbers_without_changing_retry_budget(self):
        fixture = Fixture(); fixture.network.mode = 'http_error'
        with self.assertRaises(Exception): fixture.lane.poll_native()
        first = fixture.life.safe_status()
        self.assertEqual(first['transport_facts'], {'endpoint_role': 'notice_count', 'http_status': 503})
        self.assertEqual((first['transient_failures'], first['retry_after_seconds']), (1, 45))
        fixture.now[0] += 45; fixture.guard_error = 'session_existing_draft'
        with self.assertRaises(Exception): fixture.lane.poll_native()
        second = fixture.life.safe_status()
        self.assertNotIn('transport_facts', second)
        self.assertEqual(second['reason_code'], 'session_existing_draft')
        self.assertEqual(second['transient_failures'], 1)

    def test_later_timeout_does_not_inherit_previous_http_or_business_code(self):
        fixture = Fixture(); fixture.network.mode = 'http_error'
        with self.assertRaises(Exception): fixture.lane.poll_native()
        fixture.now[0] += 45; fixture.network.mode = 'timeout'
        with self.assertRaises(Exception): fixture.lane.poll_native()
        status = fixture.life.safe_status()
        self.assertEqual(status['transport_facts'], {'endpoint_role': 'notice_count'})
        self.assertEqual((status['transient_failures'], status['retry_after_seconds']), (2, 90))

    def test_successful_poll_clears_diagnostics_and_keeps_legacy_shape(self):
        fixture = Fixture(); fixture.network.mode = 'http_error'
        with self.assertRaises(Exception): fixture.lane.poll_native()
        fixture.now[0] += 45; fixture.network.mode = 'ok'
        result = fixture.lane.poll_native()
        self.assertEqual(len(result['native_session_status']), 9)
        self.assertNotIn('transport_facts', result['native_session_status'])
        self.assertEqual(result['native_session_status']['transient_failures'], 0)

    def test_successful_identity_only_cycle_clears_facts_without_resetting_failure_budget(self):
        fixture = Fixture(); fixture.network.mode = 'http_error'
        with self.assertRaises(Exception): fixture.lane.poll_native()
        fixture.now[0] += 45; fixture.network.mode = 'ok'
        with fixture.life.cycle(reset_failures_on_success=False): pass
        self.assertNotIn('transport_facts', fixture.life.safe_status())
        self.assertEqual(fixture.life.safe_status()['transient_failures'], 1)

    def test_third_transient_failure_is_still_sticky_at_original_intervals(self):
        fixture = Fixture(); fixture.network.mode = 'http_error'
        for advance in (0, 45, 90):
            fixture.now[0] += advance
            with self.assertRaises(Exception): fixture.lane.poll_native()
        status = fixture.life.safe_status()
        self.assertEqual((status['state'], status['reason_code'], status['transient_failures']),
                         ('paused', 'session_transient_retry_exhausted', 3))


class PersistentControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='diagnostic-control-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.path = self.root/'queue.sqlite3'
        self.queue = AssistantQueue(self.path)
        self.queue.update_control(lambda current: {**current, 'run_id': RUN, 'state': 'running',
            'desired_state': 'running', 'pid': 22222})
        self.control = AssistantControl(self.queue, process_probe=lambda pid: True)

    def schema(self):
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            return db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()

    def test_actual_main_heartbeat_fragment_persists_and_reopens_only_safe_facts(self):
        fixture = Fixture(); fixture.network.mode = 'api_error'
        with self.assertRaises(ReceiverInterventionRequired) as caught: fixture.lane.poll_native()
        source = Path(lane_module.__file__).with_name('__main__.py')
        tree = ast.parse(source.read_text(encoding='utf8'))
        handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)
            and isinstance(n.type, ast.Name) and n.type.id == 'ReceiverInterventionRequired'
            and len(n.body) > 1 and isinstance(n.body[0], ast.Assign)
            and isinstance(n.body[0].targets[0], ast.Name) and n.body[0].targets[0].id == 'session_status']
        self.assertEqual(len(handlers), 1)
        fragment = ast.Module(body=handlers[0].body[:2], type_ignores=[]); ast.fix_missing_locations(fragment)
        before_schema = self.schema()
        exec(compile(fragment, 'actual-main-heartbeat-fragment', 'exec'),
             {'error': caught.exception, 'control': self.control, 'run_id': RUN})
        reopened = AssistantQueue(self.path).get_control()
        self.assertEqual(reopened['native_session_status']['transport_facts'], FACTS)
        self.assertIn('native_session_status_observed_at', reopened)
        self.assertEqual(self.schema(), before_schema)

    def test_invalid_facts_or_wrong_owner_does_not_write(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(QueueError, 'invalid_native_session_status'):
            self.control.heartbeat(RUN, native_session_status={**baseline(), 'transport_facts': {'endpoint_role': 'identity', 'http_status': True}})
        with self.assertRaisesRegex(QueueError, 'runtime_owner_changed'):
            self.control.heartbeat('b'*32, native_session_status={**baseline(), 'transport_facts': FACTS})
        self.assertEqual(before, self.path.read_bytes())

    def test_plain_heartbeat_does_not_retimestamp_old_facts_and_new_success_removes_them(self):
        self.control.heartbeat(RUN, native_session_status={**baseline(), 'transport_facts': FACTS})
        observed = self.queue.get_control()['native_session_status_observed_at']
        self.control.heartbeat(RUN)
        self.assertEqual(self.queue.get_control()['native_session_status_observed_at'], observed)
        self.control.heartbeat(RUN, native_session_status=baseline())
        self.assertEqual(self.queue.get_control()['native_session_status'], baseline())


if __name__ == '__main__': unittest.main()
