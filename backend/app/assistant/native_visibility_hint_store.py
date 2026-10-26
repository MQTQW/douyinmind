"""Owned, explicit, once-only review of unknown-visibility public AT notices.

This ledger is not a second native receiver. A hint is historical until one
local request or the explicitly enabled first-new-hint policy requests a UI
read. Even a claimed hint cannot authorize a task. Only
the owner lane's independently verified ordinary-page plan may be carried to
NativeSourceStore by the private typed permit below. The two databases are
deliberately fail-closed, not represented as an atomic cross-database commit.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from app.storage_guard import (StorageGuardError, assert_no_orphan_sqlite_files,
                               guarded_sqlite_file, verified_readonly_connection)
from .native_events import canonical_profile
from .native_transport import decimal_id


class NativeVisibilityHintError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_dump(value).encode('utf-8')).hexdigest()


_HINT_KEYS = frozenset(('namespace', 'source_kind', 'native_notice_id', 'native_comment_id',
    'notice_type', 'recipient_uid', 'sender_uid', 'sender_sec_uid', 'sender_profile_url',
    'receiver_profile_url', 'video_id', 'content', 'created_time', 'parent_id_hint',
    'relation_status', 'root_comment_id', 'parent_comment_id', 'dispatchable', 'at_sub_type',
    'native_visibility', 'visible_category', 'hidden_category'))
_HEX = re.compile(r'[a-f0-9]{64}', re.ASCII)
_TOKEN = re.compile(r'[a-f0-9]{32}', re.ASCII)
_PERMIT_BRAND = object()
_AUTO_POLICY_ID = 'policy:automatic-new-visibility-ui-review-v1'
_AUTO_POLICY_KIND = 'automatic_new_hint_policy_enabled'
_AUTO_CHECKPOINT_CODES = frozenset({'native_identity_unverified', 'native_semantic_conflict',
    'native_visibility_hint_scope_changed', 'native_visibility_hint_skip_required',
    'native_visibility_hint_source_exists'})


def validate_review_hint(value):
    """Revalidate the exact small public projection, never raw response data."""
    try:
        if type(value) is not dict or set(value) != _HINT_KEYS:
            raise ValueError()
        hint = deepcopy(value)
        if (hint['namespace'] != 'douyin_web_v1'
                or hint['source_kind'] != 'native_visibility_review_hint'
                or hint['native_visibility'] != 'unknown'
                or hint['visible_category'] != 'other_int' or hint['hidden_category'] != 'int0'
                or type(hint['notice_type']) is not int or hint['notice_type'] != 45
                or type(hint['at_sub_type']) is not int or hint['at_sub_type'] != 2
                or hint['dispatchable'] is not False or hint['relation_status'] != 'unknown'
                or any(hint[k] is not None for k in ('parent_id_hint', 'root_comment_id', 'parent_comment_id'))):
            raise ValueError()
        for key in ('native_notice_id', 'native_comment_id', 'recipient_uid', 'sender_uid', 'video_id'):
            if type(hint[key]) is not str or decimal_id(hint[key]) != hint[key]:
                raise ValueError()
        if (hint['sender_uid'] == hint['recipient_uid']
                or canonical_profile(hint['sender_sec_uid']) != hint['sender_profile_url']
                or type(hint['receiver_profile_url']) is not str
                or not hint['receiver_profile_url'].startswith('https://www.douyin.com/user/')
                or canonical_profile(hint['receiver_profile_url'].rsplit('/', 1)[-1]) != hint['receiver_profile_url']
                or hint['sender_profile_url'] == hint['receiver_profile_url']):
            raise ValueError()
        text = hint['content']
        if (type(text) is not str or not 1 <= len(text) <= 4000 or not text.strip()
                or any(ord(c) < 32 and c not in '\r\n\t' for c in text)):
            raise ValueError()
        created = hint['created_time']
        if type(created) is not str or decimal_id(created, zero=True) != created or int(created) >= 10**12:
            raise ValueError()
        return hint
    except Exception:
        raise NativeVisibilityHintError('native_visibility_hint_invalid') from None


def hint_source_id(hint):
    return 'native:' + _digest(['douyin_web_v1', hint['recipient_uid'], hint['video_id'], hint['native_comment_id']])


def hint_id_for(hint):
    return 'native-hint:' + hint_source_id(hint).split(':', 1)[1]


class _VerifiedHintUIPlanPermit:
    __slots__ = ('ledger', 'source_store', 'hint_id', 'claim_token', 'snapshot', 'plan', 'plan_hash', 'used')

    def __init__(self, ledger, hint_id, claim_token, snapshot, plan, *, _brand=None):
        if _brand is not _PERMIT_BRAND:
            raise NativeVisibilityHintError('native_visibility_hint_ui_proof_required')
        self.ledger, self.source_store = ledger, ledger.source_store
        self.hint_id, self.claim_token = hint_id, claim_token
        self.snapshot, self.plan = deepcopy(snapshot), deepcopy(plan)
        self.plan_hash, self.used = _digest(plan), False


class NativeVisibilityHintStore:
    def __init__(self, path, receiver_uid, receiver_profile_url, source_store, *, create=False, clock=None):
        self.path = Path(path).absolute()
        self.receiver_uid = decimal_id(receiver_uid)
        if (type(receiver_profile_url) is not str
                or canonical_profile(receiver_profile_url.rsplit('/', 1)[-1]) != receiver_profile_url
                or source_store.receiver_uid != self.receiver_uid
                or source_store.receiver_profile_url != receiver_profile_url):
            raise NativeVisibilityHintError('native_visibility_hint_scope_mismatch')
        self.receiver_profile_url, self.source_store = receiver_profile_url, source_store
        self._clock = clock or time.time
        # A persisted fence alone never enables this process. The fixed local
        # product opt-in must explicitly call enable_new_hint_review().
        self._automatic_new_hint_review = False
        self._automatic_new_hint_fence = None
        self._marker = self.path.with_suffix(self.path.suffix + '.owner.json')
        if self.path.resolve() != self.path or self._marker.resolve() != self._marker:
            raise NativeVisibilityHintError('native_visibility_hint_path_not_owned')
        if create and not self.path.exists() and not self._marker.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with guarded_sqlite_file(self.path):
                    assert_no_orphan_sqlite_files(self.path)
            except StorageGuardError:
                raise NativeVisibilityHintError('native_visibility_hint_path_not_owned') from None
            self._owner = uuid.uuid4().hex
            marker = {'version': 1, 'owner': self._owner, 'receiver_uid': self.receiver_uid,
                      'receiver_profile_url': self.receiver_profile_url}
            fd = os.open(self._marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                out.write(_dump(marker)); out.flush(); os.fsync(out.fileno())
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.close(fd)
            db = sqlite3.connect(self.path)
            try:
                db.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE hints(hint_id TEXT PRIMARY KEY, source_id TEXT UNIQUE NOT NULL,
                        immutable_hash TEXT NOT NULL,hint TEXT NOT NULL,state TEXT NOT NULL,
                        first_seen REAL NOT NULL,last_seen REAL NOT NULL,page_fingerprint TEXT NOT NULL,
                        requested_at REAL,claim_owner TEXT,claim_token TEXT,lease_until REAL,
                        attempts INTEGER NOT NULL DEFAULT 0,hold_code TEXT,plan_hash TEXT,
                        admitted_source_id TEXT);
                    CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT,hint_id TEXT NOT NULL,
                        kind TEXT NOT NULL,at REAL NOT NULL,facts TEXT NOT NULL);''')
                db.executemany('INSERT INTO meta VALUES(?,?)', [('owner', self._owner),
                    ('schema', '1'), ('receiver_uid', self.receiver_uid),
                    ('receiver_profile_url', self.receiver_profile_url)])
                db.commit()
            finally:
                db.close()
        else:
            self._owner = self._read_marker()
        with self._connection():
            pass

    def _regular(self, path):
        if path.resolve() != path or not path.is_file() or path.stat().st_nlink != 1:
            raise NativeVisibilityHintError('native_visibility_hint_path_not_owned')

    def _read_marker(self):
        try:
            self._regular(self._marker)
            if self._marker.stat().st_size > 4096:
                raise ValueError()
            value = json.loads(self._marker.read_text(encoding='utf-8'))
            if (type(value) is not dict or set(value) != {'version', 'owner', 'receiver_uid', 'receiver_profile_url'}
                    or type(value['version']) is not int or value['version'] != 1
                    or value['receiver_uid'] != self.receiver_uid
                    or value['receiver_profile_url'] != self.receiver_profile_url
                    or type(value['owner']) is not str or not _TOKEN.fullmatch(value['owner'])):
                raise ValueError()
            return value['owner']
        except (OSError, ValueError, TypeError, KeyError):
            raise NativeVisibilityHintError('native_visibility_hint_owner_mismatch') from None

    def _guard(self):
        self._regular(self.path)
        if self._read_marker() != self._owner:
            raise NativeVisibilityHintError('native_visibility_hint_owner_mismatch')
        for suffix in ('-journal', '-wal', '-shm'):
            side = Path(str(self.path) + suffix)
            if side.exists() or side.is_symlink(): self._regular(side)

    def _owner_check(self, db):
        try:
            meta = dict(db.execute('SELECT key,value FROM meta'))
            return meta == {'owner': self._owner, 'schema': '1', 'receiver_uid': self.receiver_uid,
                            'receiver_profile_url': self.receiver_profile_url}
        except sqlite3.Error:
            return False

    @contextmanager
    def _connection(self, *, write=False):
        self._guard()
        try:
            with guarded_sqlite_file(self.path):
                self._guard(); before = self.path.stat()
                with verified_readonly_connection(self.path, self._owner_check, guard=self._guard) as readonly:
                    self._same_file(before)
                    if not write:
                        readonly.row_factory = sqlite3.Row; readonly.execute('PRAGMA query_only=ON')
                        yield readonly; self._same_file(before); return
                self._same_file(before)
                db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=5)
                db.row_factory = sqlite3.Row
                try:
                    self._same_file(before)
                    if not self._owner_check(db):
                        raise NativeVisibilityHintError('native_visibility_hint_owner_mismatch')
                    db.execute('BEGIN IMMEDIATE'); yield db
                    self._same_file(before); db.commit()
                except Exception:
                    db.rollback(); raise
                finally:
                    db.close()
        except StorageGuardError:
            raise NativeVisibilityHintError('native_visibility_hint_owner_unverified') from None

    def _same_file(self, before):
        self._guard(); after = self.path.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise NativeVisibilityHintError('native_visibility_hint_store_changed')

    def _now(self):
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise NativeVisibilityHintError('native_visibility_hint_clock_invalid')
        return float(value)

    def enable_new_hint_review(self):
        """Enable only future first observations, with one durable old fence.

        This reserves no source/task/model/import/send and does not touch old
        hints. The audit record is deliberately outside the exact owner meta.
        Reopening the helper reads the original fence, never starts a new one.
        """
        now = self._now()
        with self._connection(write=True) as db:
            rows = db.execute('SELECT kind,at,facts FROM audit WHERE hint_id=?', (_AUTO_POLICY_ID,)).fetchall()
            created = not rows
            if created:
                value = {'version': 1, 'operation': 'enable_new_hint_review', 'activation_fence': now,
                    'reason': 'automatic_fresh_ui_review_only', 'maximum_requests_per_hint': 1}
                db.execute('INSERT INTO audit(hint_id,kind,at,facts) VALUES(?,?,?,?)',
                    (_AUTO_POLICY_ID, _AUTO_POLICY_KIND, now, _dump(value)))
                fence = now
            else:
                try:
                    if len(rows) != 1 or rows[0]['kind'] != _AUTO_POLICY_KIND: raise ValueError()
                    def pairs(items):
                        result = {}
                        for key, item in items:
                            if key in result: raise ValueError()
                            result[key] = item
                        return result
                    value = json.loads(rows[0]['facts'], object_pairs_hook=pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if (type(value) is not dict or set(value) != {'version','operation','activation_fence','reason','maximum_requests_per_hint'}
                            or type(value['version']) is not int or value['version'] != 1
                            or value['operation'] != 'enable_new_hint_review'
                            or value['reason'] != 'automatic_fresh_ui_review_only'
                            or type(value['maximum_requests_per_hint']) is not int or value['maximum_requests_per_hint'] != 1
                            or type(value['activation_fence']) not in (int,float)
                            or not math.isfinite(value['activation_fence']) or value['activation_fence'] <= 0
                            or rows[0]['at'] != value['activation_fence']):
                        raise ValueError()
                    fence = float(value['activation_fence'])
                except Exception:
                    raise NativeVisibilityHintError('native_visibility_auto_policy_unavailable') from None
        self._automatic_new_hint_fence = fence
        self._automatic_new_hint_review = True
        return {'enabled': True, 'activation_fence': fence, 'created': created,
                'scope': 'fresh_ui_review_only', 'dispatchable': False}

    def _request_first_automatic_review(self, db, hint, hint_id, immutable, now):
        """Called only just after INSERT of a previously absent inert hint."""
        if not self._automatic_new_hint_review:
            return
        fence = self._automatic_new_hint_fence
        if int(hint['created_time']) <= fence:
            db.execute('UPDATE hints SET hold_code=? WHERE hint_id=?',
                ('native_visibility_hint_before_activation', hint_id))
            return
        try:
            checked = self.source_store.visibility_hint_checkpoint(hint)
            if type(checked) is not dict or checked.get('source_id') != hint_source_id(hint):
                raise NativeVisibilityHintError('native_visibility_hint_scope_changed')
        except Exception as error:
            reason = getattr(error, 'code', None)
            if type(reason) is not str or reason not in _AUTO_CHECKPOINT_CODES:
                reason = 'native_visibility_auto_checkpoint_unavailable'
            db.execute('UPDATE hints SET hold_code=? WHERE hint_id=?', (reason, hint_id))
            return  # One bad scope never prevents storing the remaining batch.
        db.execute("UPDATE hints SET state='pending',requested_at=? WHERE hint_id=? AND state='historical' AND attempts=0 AND requested_at IS NULL",
            (now, hint_id))
        db.execute('INSERT INTO audit(hint_id,kind,at,facts) VALUES(?,?,?,?)',
            (hint_id, 'automatic_review_requested', now, _dump({'reason': 'automatic_fresh_ui_review_only',
                'immutable_hash': immutable, 'activation_fence': fence, 'maximum_requests': 1})))

    def record_hints(self, hints, *, page_fingerprint):
        if type(hints) is not list or len(hints) > 100 or type(page_fingerprint) is not str or not _HEX.fullmatch(page_fingerprint):
            raise NativeVisibilityHintError('native_visibility_hint_invalid')
        clean = [validate_review_hint(hint) for hint in hints]
        if len({hint_id_for(h) for h in clean}) != len(clean):
            raise NativeVisibilityHintError('native_visibility_hint_conflict')
        now, result = self._now(), []
        with self._connection(write=True) as db:
            for hint in clean:
                if hint['recipient_uid'] != self.receiver_uid or hint['receiver_profile_url'] != self.receiver_profile_url:
                    raise NativeVisibilityHintError('native_visibility_hint_scope_mismatch')
                hid, sid, immutable = hint_id_for(hint), hint_source_id(hint), _digest(hint)
                old = db.execute('SELECT * FROM hints WHERE hint_id=?', (hid,)).fetchone()
                if old is None:
                    db.execute('INSERT INTO hints(hint_id,source_id,immutable_hash,hint,state,first_seen,last_seen,page_fingerprint) VALUES(?,?,?,?,?,?,?,?)',
                        (hid, sid, immutable, _dump(hint), 'historical', now, now, page_fingerprint))
                    self._request_first_automatic_review(db, hint, hid, immutable, now)
                elif old['immutable_hash'] != immutable:
                    db.execute("UPDATE hints SET state='held',hold_code='native_visibility_hint_payload_conflict',last_seen=? WHERE hint_id=?", (now, hid))
                else:
                    db.execute('UPDATE hints SET last_seen=? WHERE hint_id=?', (now, hid))
                result.append(hid)
        return result

    @staticmethod
    def _view(row):
        if row is None: return None
        return {'hint_id': row['hint_id'], 'source_id': row['source_id'], 'hint': json.loads(row['hint']),
            'state': row['state'], 'hold_code': row['hold_code'], 'attempts': row['attempts'],
            'requested': row['requested_at'] is not None, 'first_seen': row['first_seen'],
            'last_seen': row['last_seen'], 'dispatchable': False,
            'admitted_source_id': row['admitted_source_id']}

    def get_hint(self, hint_id):
        with self._connection() as db:
            return self._view(db.execute('SELECT * FROM hints WHERE hint_id=?', (hint_id,)).fetchone())

    def list_hints(self, state=None, limit=16):
        if type(limit) is not int or not 1 <= limit <= 16 or state not in (None, 'historical', 'pending', 'claimed', 'committing', 'admitted', 'held'):
            raise NativeVisibilityHintError('native_visibility_hint_query_invalid')
        with self._connection() as db:
            rows = db.execute('SELECT * FROM hints'+(' WHERE state=?' if state is not None else '')+' ORDER BY first_seen,hint_id LIMIT ?',
                (state, limit) if state is not None else (limit,))
            return [self._view(row) for row in rows]

    def request_review(self, hint_id, *, operator_reason):
        if operator_reason != 'explicit_operator_unknown_visibility_ui_review':
            raise NativeVisibilityHintError('native_visibility_hint_request_invalid')
        now = self._now()
        with self._connection(write=True) as db:
            row = db.execute('SELECT * FROM hints WHERE hint_id=?', (hint_id,)).fetchone()
            if row is None or row['requested_at'] is not None or row['state'] != 'historical' or row['attempts']:
                raise NativeVisibilityHintError('native_visibility_hint_request_unavailable')
            hint = validate_review_hint(json.loads(row['hint']))
            self.source_store.visibility_hint_checkpoint(hint)
            db.execute("UPDATE hints SET state='pending',requested_at=? WHERE hint_id=? AND state='historical' AND requested_at IS NULL", (now, hint_id))
            db.execute('INSERT INTO audit(hint_id,kind,at,facts) VALUES(?,?,?,?)', (hint_id, 'explicit_review_requested', now,
                _dump({'operator_reason': operator_reason, 'immutable_hash': row['immutable_hash'], 'maximum_requests': 1})))
        return {'hint_id': hint_id, 'status': 'visibility_review_requested', 'dispatchable': False}

    @staticmethod
    def _recovery_receipt(hint, receipt):
        from .semantic_plan_store import decision_metadata
        from .native_semantic_inbox import native_message_digest
        value=decision_metadata(receipt)
        source=hint_source_id(hint)
        owner=value['owner_scope']
        if (value['mode']!='native' or value['source_key']!=source
                or value.get('native_facts_digest')!=native_message_digest(hint)
                or owner['reference_id']!=source or owner['root_reference_id']!=source
                or any(owner[k]!=hint[k] for k in ('sender_profile_url','receiver_profile_url','video_id'))):
            raise NativeVisibilityHintError('native_visibility_recovery_scope_changed')
        return value

    def request_recovery_review(self,hint_id,*,operator_reason,semantic_receipt):
        """One explicit second READ; retain the first attempt and every audit.

        The private completed native meaning is reused. This creates no model,
        source claim, UI plan, task, import or publication capability.
        """
        if operator_reason!='explicit_operator_held_visibility_ui_recovery':
            raise NativeVisibilityHintError('native_visibility_recovery_request_invalid')
        # Read the issuer outside our write transaction to avoid cross-ledger
        # lock inversion. Its immutable scope is rechecked against the row.
        original=self.get_hint(hint_id)
        if not original:raise NativeVisibilityHintError('native_visibility_recovery_unavailable')
        value=self._recovery_receipt(original['hint'],semantic_receipt)
        now=self._now()
        with self._connection(write=True) as db:
            row=db.execute('SELECT * FROM hints WHERE hint_id=?',(hint_id,)).fetchone()
            if (row is None or row['state']!='held' or row['attempts']!=1
                    or row['hold_code']!='native_visibility_ui_unverified'
                    or row['admitted_source_id'] is not None or row['plan_hash'] is not None
                    or row['requested_at'] is None or type(row['claim_token']) is not str
                    or not _TOKEN.fullmatch(row['claim_token'])
                    or db.execute("SELECT 1 FROM audit WHERE hint_id=? AND kind IN ('explicit_recovery_requested','explicit_recovery_claimed')",(hint_id,)).fetchone()):
                raise NativeVisibilityHintError('native_visibility_recovery_unavailable')
            hint=validate_review_hint(json.loads(row['hint']))
            if (row['immutable_hash']!=_digest(hint) or _digest(hint)!=_digest(original['hint'])
                    or hint_id_for(hint)!=hint_id):
                raise NativeVisibilityHintError('native_visibility_recovery_scope_changed')
            scope=self.source_store.visibility_hint_checkpoint(hint)
            facts={'version':1,'reason':operator_reason,'maximum_recoveries':1,
                'source_id':row['source_id'],'immutable_hash':row['immutable_hash'],
                'old_attempts':1,'old_hold_code':row['hold_code'],
                'old_claim_sha256':hashlib.sha256(row['claim_token'].encode()).hexdigest(),
                'old_requested_at':row['requested_at'],'old_lease_until':row['lease_until'],
                'identity_revision':scope['identity_revision'],'semantic_plan_id':value['plan_id'],
                'semantic_request_key':value['request_key'],'semantic_budget_scope_id':value['budget_scope_id'],
                'semantic_owner_sha256':_digest(value['owner_scope']),
                'semantic_catalog_sha256':_digest(value['catalog_snapshot']),
                'semantic_facts_digest':value['native_facts_digest']}
            db.execute('INSERT INTO audit(hint_id,kind,at,facts) VALUES(?,?,?,?)',
                (hint_id,'explicit_recovery_requested',now,_dump(facts)))
            # Keep requested_at, lease, claim and hold code as first-attempt
            # evidence. Only claim_review may replace the claim and add #2.
            db.execute("UPDATE hints SET state='pending' WHERE hint_id=? AND state='held' AND attempts=1",(hint_id,))
        return {'hint_id':hint_id,'status':'visibility_recovery_requested','dispatchable':False}

    def _recovery_audit(self,db,row,*,claimed=False):
        requests=db.execute("SELECT at,facts FROM audit WHERE hint_id=? AND kind='explicit_recovery_requested'",(row['hint_id'],)).fetchall()
        claims=db.execute("SELECT at,facts FROM audit WHERE hint_id=? AND kind='explicit_recovery_claimed'",(row['hint_id'],)).fetchall()
        try:
            if len(requests)!=1 or len(claims)!=(1 if claimed else 0):raise ValueError()
            facts=json.loads(requests[0]['facts'])
            required={'version','reason','maximum_recoveries','source_id','immutable_hash','old_attempts',
                'old_hold_code','old_claim_sha256','old_requested_at','old_lease_until','identity_revision',
                'semantic_plan_id','semantic_request_key','semantic_budget_scope_id','semantic_owner_sha256',
                'semantic_catalog_sha256','semantic_facts_digest'}
            if (type(facts) is not dict or set(facts)!=required or type(facts['version']) is not int or facts['version']!=1
                    or facts['reason']!='explicit_operator_held_visibility_ui_recovery'
                    or type(facts['maximum_recoveries']) is not int or facts['maximum_recoveries']!=1
                    or type(facts['old_attempts']) is not int or facts['old_attempts']!=1
                    or facts['old_hold_code']!='native_visibility_ui_unverified'
                    or facts['source_id']!=row['source_id'] or facts['immutable_hash']!=row['immutable_hash']
                    or facts['old_requested_at']!=row['requested_at']):raise ValueError()
            for key in ('old_claim_sha256','semantic_owner_sha256','semantic_catalog_sha256','semantic_facts_digest'):
                if type(facts[key]) is not str or not _HEX.fullmatch(facts[key]):raise ValueError()
            for key in ('semantic_plan_id','semantic_request_key','semantic_budget_scope_id','identity_revision'):
                if type(facts[key]) is not str or not 1<=len(facts[key])<=256:raise ValueError()
            if type(requests[0]['at']) not in (int,float) or not math.isfinite(requests[0]['at']) or requests[0]['at']<=0:raise ValueError()
            if claimed:
                proof=json.loads(claims[0]['facts'])
                if (type(proof) is not dict or set(proof)!={'version','request_audit_sha256','claim_sha256','attempts'}
                        or type(proof['version']) is not int or proof['version']!=1
                        or type(proof['attempts']) is not int or proof['attempts']!=2
                        or proof['request_audit_sha256']!=_digest(facts)
                        or proof['claim_sha256']!=hashlib.sha256(row['claim_token'].encode()).hexdigest()
                        or claims[0]['at']<requests[0]['at']):raise ValueError()
            elif (row['hold_code']!=facts['old_hold_code'] or row['lease_until']!=facts['old_lease_until']
                    or hashlib.sha256(row['claim_token'].encode()).hexdigest()!=facts['old_claim_sha256']):raise ValueError()
            return facts
        except Exception:
            raise NativeVisibilityHintError('native_visibility_recovery_audit_unavailable') from None

    def claim_review(self, hint_id, owner, lease_seconds=120):
        if type(owner) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', owner) or type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 300:
            raise NativeVisibilityHintError('native_visibility_hint_claim_invalid')
        now = self._now()
        with self._connection(write=True) as db:
            row = db.execute('SELECT * FROM hints WHERE hint_id=?', (hint_id,)).fetchone()
            if row is None or row['state'] != 'pending' or row['requested_at'] is None or row['attempts'] not in (0,1):
                return None
            recovery=self._recovery_audit(db,row) if row['attempts']==1 else None
            hint = validate_review_hint(json.loads(row['hint']))
            scope = self.source_store.visibility_hint_checkpoint(hint)
            if recovery is not None and scope['identity_revision']!=recovery['identity_revision']:
                raise NativeVisibilityHintError('native_visibility_recovery_scope_changed')
            token = uuid.uuid4().hex
            db.execute("UPDATE hints SET state='claimed',claim_owner=?,claim_token=?,lease_until=?,attempts=? WHERE hint_id=? AND state='pending' AND attempts=?", (owner, token, now+lease_seconds,row['attempts']+1,hint_id,row['attempts']))
            if recovery is not None:
                # The old failure remains in the immutable recovery audit;
                # this current claim is no longer held. Admitted authority
                # must still require a clear current hold, never bypass it.
                db.execute('UPDATE hints SET hold_code=NULL WHERE hint_id=?',(hint_id,))
                db.execute('INSERT INTO audit(hint_id,kind,at,facts) VALUES(?,?,?,?)',
                    (hint_id,'explicit_recovery_claimed',now,_dump({'version':1,'request_audit_sha256':_digest(recovery),
                        'claim_sha256':hashlib.sha256(token.encode()).hexdigest(),'attempts':2})))
            candidate = self._candidate(hint, scope)
            return {'hint_id': hint_id, 'source_id': row['source_id'], 'claim_token': token, 'candidate': candidate}

    @staticmethod
    def _candidate(hint, scope):
        candidate = deepcopy(hint)
        candidate.update({'source_kind': 'native_notice_candidate', 'native_source_key': scope['source_id'],
            'source_id': scope['source_id'], 'identity_link_revision': scope['identity_revision'],
            'hint_provenance': {'hint_id': hint_id_for(hint), 'original_source_kind': hint['source_kind'],
                'native_visibility': 'unknown', 'hint_immutable_sha256': _digest(hint)}})
        return candidate

    def _claimed(self, db, hint_id, claim_token, *, state='claimed'):
        row = db.execute('SELECT * FROM hints WHERE hint_id=?', (hint_id,)).fetchone()
        if (row is None or row['state'] != state or row['attempts'] not in (1,2) or row['requested_at'] is None
                or type(claim_token) is not str or not _TOKEN.fullmatch(claim_token)
                or row['claim_token'] != claim_token or row['lease_until'] is None or self._now() > row['lease_until']):
            raise NativeVisibilityHintError('native_visibility_hint_claim_unavailable')
        if row['attempts']==2:self._recovery_audit(db,row,claimed=True)
        hint = validate_review_hint(json.loads(row['hint']))
        if row['immutable_hash'] != _digest(hint):
            raise NativeVisibilityHintError('native_visibility_hint_conflict')
        scope = self.source_store.visibility_hint_checkpoint(hint)
        if row['attempts']==2 and scope['identity_revision']!=self._recovery_audit(db,row,claimed=True)['identity_revision']:
            raise NativeVisibilityHintError('native_visibility_recovery_scope_changed')
        return row, hint, scope

    def check_claim(self, hint_id, claim_token):
        try:
            with self._connection() as db:
                row, hint, scope = self._claimed(db, hint_id, claim_token)
                return {'valid': True, 'reason': 'ok', 'semantic_conflict': False, 'candidate': self._candidate(hint, scope)}
        except Exception as error:
            code = getattr(error, 'code', 'native_visibility_hint_claim_unavailable')
            return {'valid': False, 'reason': code, 'semantic_conflict': code == 'native_semantic_conflict', 'candidate': None}

    def record_ui_review_diagnostic(self,hint_id,claim_token,diagnostic):
        """Append fixed READ facts; never changes a hint or grants recovery."""
        stages={'before_identity','original_lookup','after_ui','semantic_prepare','source_plan'}
        codes={'ui_plan_prepared','native_visibility_ui_unverified','native_visibility_review_scope_changed',
            'native_visibility_effects_unknown','native_visibility_commit_unknown','native_scope_changed',
            'native_ui_unverified','native_semantic_conflict','native_recovery_stopped',
            'native_identity_unverified','native_visibility_review_intervention','native_review_unconfirmed'}
        classes={'ReceiverDetailError','ReceiverPageError','ReceiverInterventionRequired','NativeLaneError',
            'NativeStoreError','NativeVisibilityHintError','TimeoutError','KeyError','TypeError','ValueError','local_error'}
        reasons={'checking','matched','invalid_original_target','native_correlation_requires_source_id',
            'receiver_binding_mismatch','original_notifications_unavailable','original_notification_not_unique',
            'original_notification_candidates_exceed_limit','original_notification_changed',
            'original_notification_not_found','original_candidate_unreadable','original_target_not_unique',
            'original_native_semantic_ambiguous','original_target_changed','original_lookup_stopped',
            'original_identity_not_ready','original_lookup_unavailable'}
        keys={'candidate_limit','candidate_count','checked_count','match_count','unreadable_count',
            'final_rechecks','final_verified','snapshot_changes','page_open_count','lookup_stage_index','history_scrolls'}
        if (type(diagnostic) is not dict or set(diagnostic)-{'stage','code','exception_type','lookup_counts','lookup_reason_code'}
                or type(diagnostic.get('stage')) is not str or diagnostic['stage'] not in stages
                or type(diagnostic.get('code')) is not str or diagnostic['code'] not in codes):
            raise NativeVisibilityHintError('native_visibility_hint_diagnostic_invalid')
        clean={k:diagnostic[k] for k in ('stage','code')}
        if 'exception_type' in diagnostic:
            if type(diagnostic['exception_type']) is not str or diagnostic['exception_type'] not in classes:
                raise NativeVisibilityHintError('native_visibility_hint_diagnostic_invalid')
            clean['exception_type']=diagnostic['exception_type']
        if 'lookup_reason_code' in diagnostic:
            if type(diagnostic['lookup_reason_code']) is not str or diagnostic['lookup_reason_code'] not in reasons:
                raise NativeVisibilityHintError('native_visibility_hint_diagnostic_invalid')
            clean['lookup_reason_code']=diagnostic['lookup_reason_code']
        if 'lookup_counts' in diagnostic:
            values=diagnostic['lookup_counts']
            if type(values) is not dict or any(k not in keys or type(v) is not int or not 0<=v<=1000000 for k,v in values.items()):
                raise NativeVisibilityHintError('native_visibility_hint_diagnostic_invalid')
            clean['lookup_counts']=dict(values)
        with self._connection(write=True) as db:
            row=db.execute('SELECT claim_token,attempts FROM hints WHERE hint_id=?',(hint_id,)).fetchone()
            if (row is None or row['claim_token']!=claim_token or type(claim_token) is not str
                    or not _TOKEN.fullmatch(claim_token) or row['attempts'] not in (1,2)):
                raise NativeVisibilityHintError('native_visibility_hint_claim_unavailable')
            facts={'attempts':row['attempts'],'claim_sha256':hashlib.sha256(claim_token.encode()).hexdigest(),
                'diagnostic':clean}
            db.execute('INSERT INTO audit(hint_id,kind,at,facts) VALUES(?,?,?,?)',
                (hint_id,'ui_review_diagnostic',self._now(),_dump(facts)))

    def hold(self, hint_id, claim_token, code):
        if type(code) is not str or not re.fullmatch(r'[a-z_]{1,80}', code):
            raise NativeVisibilityHintError('native_visibility_hint_hold_invalid')
        with self._connection(write=True) as db:
            row = db.execute('SELECT * FROM hints WHERE hint_id=?', (hint_id,)).fetchone()
            if row is None or row['claim_token'] != claim_token or row['state'] not in ('claimed', 'committing'):
                raise NativeVisibilityHintError('native_visibility_hint_claim_unavailable')
            db.execute("UPDATE hints SET state='held',hold_code=? WHERE hint_id=?", (code, hint_id))

    def mint_ui_plan_permit(self, hint_id, claim_token, plan):
        # Trusted owner-lane call AFTER actual UI/Registry proof; JSON flags
        # themselves are never a public minting or authorization endpoint.
        from .native_source_store import _safe_json
        _safe_json(plan)
        with self._connection() as db:
            row, hint, scope = self._claimed(db, hint_id, claim_token)
            snapshot = {'hint': hint, 'source_id': row['source_id'], 'immutable_hash': row['immutable_hash'],
                'claim_owner': row['claim_owner'], 'claim_token': row['claim_token'], 'lease_until': row['lease_until'],
                'scope': scope, 'page_fingerprint': row['page_fingerprint']}
        return _VerifiedHintUIPlanPermit(self, hint_id, claim_token, snapshot, plan, _brand=_PERMIT_BRAND)

    def _begin_source_admission(self, permit, source_store):
        if (type(permit) is not _VerifiedHintUIPlanPermit or permit.ledger is not self
                or permit.source_store is not source_store or self.source_store is not source_store
                or permit.used or _digest(permit.plan) != permit.plan_hash):
            raise NativeVisibilityHintError('native_visibility_hint_ui_proof_required')
        with self._connection(write=True) as db:
            row, hint, scope = self._claimed(db, permit.hint_id, permit.claim_token)
            snapshot = {'hint': hint, 'source_id': row['source_id'], 'immutable_hash': row['immutable_hash'],
                'claim_owner': row['claim_owner'], 'claim_token': row['claim_token'], 'lease_until': row['lease_until'],
                'scope': scope, 'page_fingerprint': row['page_fingerprint']}
            if snapshot != permit.snapshot:
                raise NativeVisibilityHintError('native_visibility_hint_scope_changed')
            db.execute("UPDATE hints SET state='committing',plan_hash=? WHERE hint_id=?", (permit.plan_hash, permit.hint_id))
            db.execute('INSERT INTO audit(hint_id,kind,at,facts) VALUES(?,?,?,?)', (permit.hint_id, 'source_admission_reserved', self._now(),
                _dump({'plan_sha256': permit.plan_hash, 'claim_sha256': hashlib.sha256(permit.claim_token.encode()).hexdigest()})))
        permit.used = True
        return deepcopy(snapshot)

    def _finish_source_admission(self, permit, source_id):
        with self._connection(write=True) as db:
            row = db.execute('SELECT state,claim_token,plan_hash FROM hints WHERE hint_id=?', (permit.hint_id,)).fetchone()
            if row is None or row['state'] != 'committing' or row['claim_token'] != permit.claim_token or row['plan_hash'] != permit.plan_hash:
                raise NativeVisibilityHintError('native_visibility_hint_commit_unknown')
            db.execute("UPDATE hints SET state='admitted',admitted_source_id=? WHERE hint_id=?", (source_id, permit.hint_id))
