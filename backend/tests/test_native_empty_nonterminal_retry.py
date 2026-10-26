"""Synthetic fixed GET responses and temporary native ledger; no account or UI."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import urllib.parse

from app.assistant.native_events import canonical_profile, project_page
from app.assistant.native_lane import NativeLane, NativeLaneError, NativeSessionPaused
from app.assistant.native_lifecycle import NativeSessionLifecycle, project_native_session_status
from app.assistant.native_receiver import NativeReceiver
from app.assistant.native_source_store import NativeSourceStore
from app.assistant.native_transport import NativeTransport, SecretSession
from app.mention_receiver.browser import ReceiverInterventionRequired


UID, SEC = '9001', 'fixture_empty_page_B'
AUTHOR, AUTHOR_SEC = '8001', 'fixture_empty_page_A'
VIDEO = '7999999000000000001'
PROFILE, AUTHOR_PROFILE = canonical_profile(SEC), canonical_profile(AUTHOR_SEC)


def page(rows=(), *, more=0, minimum='0', maximum='0'):
    return {'status_code': 0, 'notice_list_v2': list(rows), 'has_more': more,
            'min_time': minimum, 'max_time': maximum}


def notice(channel):
    if channel == 9:
        return {'nid_str': '1001', 'type': 45, 'user_id': UID, 'aweme_id': VIDEO, 'create_time': '90',
                'at': {'sub_type': 2, 'aweme': {'aweme_id': VIDEO},
                       'user_info': {'uid': AUTHOR, 'sec_uid': AUTHOR_SEC}, 'content': 'fixture first question',
                       'comment_status': 1, 'comment_unvisible': 0,
                       'schema_url': 'aweme://aweme/detail/'+VIDEO+'?cid=2001'}}
    return {'nid_str': '1002', 'type': 31, 'user_id': UID, 'create_time': '90',
            'comment': {'comment_type': 2, 'comment_unvisible': 0, 'parent_id': '2001',
                        'aweme': {'aweme_id': VIDEO}, 'comment': {'cid': '2002', 'aweme_id': VIDEO,
                        'status': 1, 'text': 'fixture second question',
                        'user': {'uid': AUTHOR, 'sec_uid': AUTHOR_SEC}}}}


class FixedNetwork:
    def __init__(self): self.mode, self.roles, self.page_cursors = 'empty', [], []

    def __call__(self, url, headers, timeout, maximum):
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path.endswith('query/user/'):
            self.roles.append('identity'); value = {'status_code': 0, 'user_uid': UID}
        elif parsed.path.endswith('notice/count/'):
            self.roles.append('notice_count')
            value = {'status_code': 0, 'interactive_group': [{'type': 9, 'group': '601'}, {'type': 7, 'group': '2'}]}
        elif parsed.path.endswith('notice/'):
            self.roles.append('notice_list')
            assert query['is_mark_read'] == ['0'] and query['count'] == ['10']
            self.page_cursors.append((query['notice_group'][0], query['min_time'][0], query['max_time'][0]))
            if self.mode == 'auth': return 401, b''
            if self.mode == 'verification': value = {'status_code': 0, 'verify_required': True}
            elif self.mode == 'empty': value = page(more=1, minimum='30', maximum='40')
            elif self.mode == 'stalled':
                value = page([notice(9)], more=1, minimum=query['min_time'][0], maximum=query['max_time'][0])
            elif self.mode == 'unknown': value = {**page(), 'has_more': 'unknown'}
            else: value = page()
        else:
            raise AssertionError('only fixed read endpoints are allowed')
        return 200, json.dumps(value).encode()


class EmptyPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-empty-page-'); self.addCleanup(self.temp.cleanup)
        self.now, self.network, self.ui_calls = [100.0], FixedNetwork(), []
        self.path = Path(self.temp.name).resolve()/'native'/'sources.sqlite3'
        self.store = NativeSourceStore(self.path, UID, PROFILE, create=True, clock=lambda: self.now[0])
        for channel, group in ((9, '601'), (7, '2')):
            context = self.store.prepare_channel(channel, group)
            raw = page([notice(channel)], more=1, minimum='10', maximum='20')
            projected = project_page(raw, channel, receiver_uid=UID, receiver_sec_uid=SEC,
                                     profile_allowed=lambda profile: profile == AUTHOR_PROFILE)
            self.store.record_page(context, projected, next_cursor=('10', '20'), has_more=True, allow_live=True)
        self.store.set_verified(); self.now[0] = 102.0
        self.fresh_runtime()

    def fresh_runtime(self):
        def session(): return SecretSession('synthetic=x', 'synthetic-UA', UID, 3000)
        vault = SimpleNamespace(expected_uid=UID, load=session, save=lambda value: self.fail('no vault refresh expected'))
        capture = SimpleNamespace(expected_uid=UID, guard=lambda: None,
                                  capture=lambda: self.fail('no browser credential capture expected'))
        self.life = NativeSessionLifecycle(expected_uid=UID, owned_vault=vault, context_capture=capture,
                                          network=self.network, clock=lambda: self.now[0])
        self.transport = NativeTransport(self.life.provider, UID, self.network, clock=lambda: self.now[0])
        self.receiver = NativeReceiver(self.transport, self.store, receiver_uid=UID, receiver_sec_uid=SEC,
                                       profile_allowed=lambda profile: profile == AUTHOR_PROFILE,
                                       clock=lambda: self.now[0])
        def no_ui(): self.ui_calls.append(True); self.fail('poll must not operate the browser')
        browser = SimpleNamespace(adapter=SimpleNamespace(expected_profile_url=PROFILE, check_intervention=no_ui),
            registry=object(), queue=object(), conversation=object(), should_stop=lambda: False,
            run_id='a'*32, last_baseline_created=False, last_detail_failure_count=0)
        self.lane = NativeLane(browser, self.receiver, self.store, session_lifecycle=self.life)

    def durable_rows(self):
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row; db.execute('PRAGMA query_only=ON')
            return {table: [dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in ('channels', 'scan_pages', 'identities', 'sources', 'notice_refs', 'notice_skips', 'observations')}

    def meta(self):
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)) as db:
            return dict(db.execute('SELECT key,value FROM meta'))

    def fail_poll(self):
        with self.assertRaises(NativeLaneError) as caught: self.lane.poll_native()
        self.assertEqual(caught.exception.code, 'native_session_cooldown')
        self.assertNotIsInstance(caught.exception, ReceiverInterventionRequired)
        return caught.exception.native_session_status

    def test_one_empty_page_cools_down_without_accepting_or_advancing_then_recovers(self):
        before, arm = self.durable_rows(), self.meta()['armed_at']
        status = self.fail_poll()
        self.assertEqual((status['state'], status['last_transient_code'], status['transient_failures'], status['retry_after_seconds']),
                         ('cooldown', 'empty_nonterminal', 1, 45))
        self.assertEqual(project_native_session_status(status), status)
        self.assertEqual(self.durable_rows(), before)
        self.assertEqual((self.meta()['pause_code'], self.meta()['verified_at'], self.meta()['armed_at']), ('empty_nonterminal', '0', arm))
        calls = len(self.network.roles); self.now[0] += 44
        self.fail_poll(); self.assertEqual(len(self.network.roles), calls)
        self.now[0] += 1; self.network.mode = 'ok'
        result = self.lane.poll_native()
        self.assertEqual(result['received'], 0)
        self.assertEqual((result['native_session_status']['state'], result['native_session_status']['transient_failures']), ('ready', 0))
        self.assertIsNone(result['native_session_status']['last_transient_code'])
        self.assertEqual(self.meta()['pause_code'], '')
        self.assertEqual(self.durable_rows()['sources'], before['sources'])
        self.assertTrue(all(row['baseline_done'] == 1 for row in self.durable_rows()['channels']))
        self.assertEqual(self.ui_calls, [])

    def test_three_empty_pages_keep_original_durable_rows_then_sticky_pause(self):
        before = self.durable_rows()
        first = self.fail_poll(); self.assertEqual(first['retry_after_seconds'], 45)
        self.now[0] += 45
        second = self.fail_poll(); self.assertEqual((second['transient_failures'], second['retry_after_seconds']), (2, 90))
        self.now[0] += 90
        with self.assertRaises(NativeSessionPaused) as caught: self.lane.poll_native()
        status = caught.exception.native_session_status
        self.assertEqual((status['state'], status['reason_code'], status['last_transient_code'], status['transient_failures']),
                         ('paused', 'session_transient_retry_exhausted', 'empty_nonterminal', 3))
        self.assertEqual(project_native_session_status(status), status)
        self.assertEqual(self.durable_rows(), before)
        self.assertEqual(self.meta()['pause_code'], 'empty_nonterminal')
        self.assertTrue(all(pair[1:] == ('10', '20') for pair in self.network.page_cursors))
        calls = len(self.network.roles); self.now[0] += 1000
        with self.assertRaises(NativeSessionPaused): self.lane.poll_native()
        self.assertEqual(len(self.network.roles), calls)
        self.assertEqual(self.ui_calls, [])

    def test_successful_identity_only_prepare_does_not_reset_empty_page_budget(self):
        self.fail_poll(); self.now[0] += 45
        with self.life.cycle(reset_failures_on_success=False):
            self.assertTrue(self.receiver.reverify_identity_checkpoint())
        self.assertEqual((self.life.safe_status()['transient_failures'], self.life.safe_status()['last_transient_code']), (1, 'empty_nonterminal'))
        second = self.fail_poll()
        self.assertEqual((second['transient_failures'], second['retry_after_seconds']), (2, 90))

    def test_auth_and_verification_pause_immediately_without_cursor_commit(self):
        for mode, code in (('auth', 'session_invalid'), ('verification', 'verification_required')):
            with self.subTest(mode=mode):
                # One independent lifecycle for each response; neither can be
                # resumed by the fake clock or by a successful identity GET.
                self.fresh_runtime()
                self.network.mode = mode; before = self.durable_rows()
                with self.assertRaises(NativeSessionPaused) as caught: self.lane.poll_native()
                status = caught.exception.native_session_status
                self.assertEqual((status['state'], status['reason_code'], status['transient_failures']), ('paused', code, 0))
                self.assertEqual(self.durable_rows(), before)
                self.assertEqual(self.ui_calls, [])

    def test_stalled_cursor_and_unknown_shape_remain_sticky_not_transient(self):
        for mode, source_code in (('stalled', 'cursor_stalled'), ('unknown', 'page_schema_unknown')):
            with self.subTest(mode=mode):
                self.fresh_runtime()
                self.network.mode = mode; before = self.durable_rows()
                with self.assertRaises(NativeSessionPaused) as caught: self.lane.poll_native()
                status = caught.exception.native_session_status
                self.assertEqual((status['state'], status['reason_code'], status['transient_failures']), ('paused', 'session_refresh_unavailable', 0))
                self.assertIsNone(status['last_transient_code'])
                self.assertEqual(self.meta()['pause_code'], source_code)
                self.assertEqual(self.durable_rows(), before)


if __name__ == '__main__': unittest.main()
