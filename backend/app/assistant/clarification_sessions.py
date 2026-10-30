"""Explicit opt-in intent clarification sessions; no browser/model/send APIs.

Only future real-mention admissions can arm a session. A spent clarification
reply, a consumed response and an unknown handoff are never recreated/reset.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from app.mention_agent.contracts import Binding, canonical_json, validate_envelope
from app.storage_guard import guarded_sqlite_file, verified_readonly_connection, assert_no_orphan_sqlite_files

COMPONENT = 'DouyinMind.intent-clarification.v1'
DATABASE_NAME = 'intent-clarification.sqlite3'
MARKER_NAME = 'intent-clarification-required.json'
SOURCE_KIND = 'intent_clarification_reply'
_TASK = re.compile(r'[a-f0-9]{32}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_SOURCE = re.compile(r'native:[a-f0-9]{64}\Z')


class ClarificationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def immutable_envelope(envelope):
    value = deepcopy(envelope)
    value.get('evidence', {}).pop('observed_at_utc', None)
    return value


class ClarificationSessionStore:
    def __init__(self, path, *, clock=None):
        self.path = Path(path).absolute()
        self.marker = self.path.parent / MARKER_NAME
        self.clock = clock or time.time
        self._location()
        with self._db() as db:
            value = db.execute("SELECT value FROM clarification_meta WHERE key='activated_at'").fetchone()
            self.activated_at = float(value[0]) if value else 0
        if not math.isfinite(self.activated_at) or self.activated_at <= 0:
            raise ClarificationError('clarification_activation_invalid')

    @classmethod
    def prepare(cls, path, *, enabled=False, clock=None):
        if enabled is not True:
            raise ClarificationError('clarification_feature_disabled')
        path = Path(path).absolute()
        if path.name != DATABASE_NAME or path.resolve() != path or path.parent.resolve() != path.parent:
            raise ClarificationError('clarification_path_unsafe')
        path.parent.mkdir(parents=True, exist_ok=True)
        marker = path.parent / MARKER_NAME
        with guarded_sqlite_file(path):
            if not path.exists():
                if marker.exists() or marker.is_symlink():
                    raise ClarificationError('clarification_store_missing')
                assert_no_orphan_sqlite_files(path)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                now = (clock or time.time)()
                if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
                    raise ClarificationError('clarification_activation_invalid')
                with sqlite3.connect(path.as_uri() + '?mode=rw', uri=True) as db:
                    db.executescript('''
                        CREATE TABLE clarification_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                        CREATE TABLE clarification_sessions(
                          session_id TEXT PRIMARY KEY,root_task_id TEXT UNIQUE NOT NULL,root TEXT NOT NULL,
                          immutable_hash TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,
                          published_reply TEXT,response_source_id TEXT UNIQUE,response_envelope TEXT,
                          response_hash TEXT,semantic_plan_id TEXT,decision TEXT,queue_item TEXT,
                          child_task_id TEXT UNIQUE,reason_code TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL);
                        CREATE TABLE clarification_events(id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,
                          kind TEXT NOT NULL,metadata TEXT NOT NULL,created_at REAL NOT NULL);
                    ''')
                    db.executemany('INSERT INTO clarification_meta VALUES(?,?)',
                        [('component', COMPONENT), ('version', '1'), ('activated_at', str(now))])
                with marker.open('x', encoding='utf-8') as stream:
                    stream.write(canonical_json({'component': COMPONENT, 'version': 1}))
                    stream.flush(); os.fsync(stream.fileno())
        return cls(path, clock=clock)

    def _location(self):
        if (self.path.name != DATABASE_NAME or self.path.resolve() != self.path
                or self.path.parent.resolve() != self.path.parent):
            raise ClarificationError('clarification_path_unsafe')
        for path in (self.path, self.marker, *(Path(str(self.path) + suffix) for suffix in ('-journal', '-wal', '-shm'))):
            try:
                info = path.lstat()
            except FileNotFoundError:
                if path in (self.path, self.marker):
                    raise ClarificationError('clarification_store_missing') from None
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.resolve() != path
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise ClarificationError('clarification_path_unsafe')
        try:
            if json.loads(self.marker.read_text(encoding='utf-8')) != {'component': COMPONENT, 'version': 1}:
                raise ValueError()
        except Exception:
            raise ClarificationError('clarification_marker_invalid') from None

    @staticmethod
    def _owned(db):
        try:
            row = db.execute("SELECT value FROM clarification_meta WHERE key='component'").fetchone()
            return row is not None and row[0] == COMPONENT
        except sqlite3.Error:
            return False

    @contextmanager
    def _db(self, *, write=False):
        self._location()
        with guarded_sqlite_file(self.path):
            with verified_readonly_connection(self.path, self._owned, guard=self._location) as probe:
                if not self._owned(probe):
                    raise ClarificationError('clarification_store_not_owned')
            db = sqlite3.connect(self.path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True)
            db.row_factory = sqlite3.Row
            try:
                db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                if not self._owned(db):
                    raise ClarificationError('clarification_store_not_owned')
                yield db
                if write: db.commit()
            except BaseException:
                if write: db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def _view(row):
        if row is None: return None
        value = dict(row)
        for key in ('root', 'published_reply', 'response_envelope', 'decision', 'queue_item'):
            value[key] = json.loads(value[key]) if value[key] is not None else None
        root=value['root']
        if (type(root) is not dict or value['immutable_hash']!=digest(root)
                or root.get('id')!=value['root_task_id']
                or value['session_id']!=digest({'root_task_id':root['id'],'root':root})):
            raise ClarificationError('clarification_immutable_root_changed')
        return value

    def get(self, session_id):
        with self._db() as db:
            return self._view(db.execute('SELECT * FROM clarification_sessions WHERE session_id=?', (session_id,)).fetchone())

    def for_root(self, task_id):
        with self._db() as db:
            return self._view(db.execute('SELECT * FROM clarification_sessions WHERE root_task_id=?', (task_id,)).fetchone())

    def _event(self, db, session_id, kind, value):
        db.execute('INSERT INTO clarification_events(session_id,kind,metadata,created_at) VALUES(?,?,?,?)',
            (session_id, kind, canonical_json(value), self.clock()))

    def arm(self, task, source, *, reason_code, semantic_plan_id):
        """Called only for a newly admitted, post-activation real mention."""
        if (reason_code != 'semantic_intent_unclear' or task.get('kind') != 'clarify'
                or type(semantic_plan_id) is not str or not re.fullmatch(r'semantic-[a-f0-9]{64}',semantic_plan_id)
                or not _TASK.fullmatch(task.get('id', '')) or task.get('created_at', 0) < self.activated_at
                or source.get('state') != 'admitted' or source.get('task_id') != task['id']
                or source.get('historical') is not False or source.get('first_seen', 0) < self.activated_at
                or source.get('attempts') != 1 or not _SOURCE.fullmatch(source.get('source_id', ''))
                or task['envelope'].get('evidence', {}).get('source_kind') is not None):
            raise ClarificationError('clarification_root_not_new')
        binding = Binding.from_dict(task['binding'])
        validate_envelope(task['envelope'], binding)
        if task['envelope']['source_mode'] != 'live_ui' or binding.public_reply_allowed is not True:
            raise ClarificationError('clarification_root_invalid')
        root = {key: deepcopy(task[key]) for key in ('id', 'binding', 'envelope', 'authorization', 'user_id', 'device_id', 'created_at')}
        root.update(source_id=source['source_id'], reason_code=reason_code, allowed_actions=['save', 'clarify', 'cancel'],
            semantic_plan_id=semantic_plan_id,
            source_native_comment_id=source['candidate']['native_comment_id'],
            activated_at=self.activated_at, capability='intent_clarification_v1')
        session_id = digest({'root_task_id': task['id'], 'root': root})
        now = self.clock()
        with self._db(write=True) as db:
            old = db.execute('SELECT * FROM clarification_sessions WHERE root_task_id=?', (task['id'],)).fetchone()
            if old is not None:
                if old['immutable_hash'] != digest(root): raise ClarificationError('clarification_root_changed')
                return self._view(old)
            db.execute('''INSERT INTO clarification_sessions(session_id,root_task_id,root,immutable_hash,state,
                revision,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)''',
                (session_id, task['id'], canonical_json(root), digest(root), 'awaiting_publication', now, now))
            self._event(db, session_id, 'armed_future_root', {'source_id': source['source_id']})
            return self._view(db.execute('SELECT * FROM clarification_sessions WHERE session_id=?', (session_id,)).fetchone())

    def published(self, session_id, reply):
        """No unknown send can create an awaiting-reply capability."""
        if (reply.get('state') != 'b_view_visible' or reply.get('kind') != 'clarification'
                or not re.fullmatch(r'[a-f0-9]{48}', reply.get('send_token') or '')
                or type(reply.get('text')) is not str or not reply['text'].strip()
                or not _TASK.fullmatch(reply.get('task_id',''))
                or reply.get('evidence', {}).get('exact_body_in_original_thread') is not True
                or reply['evidence'].get('submit_gate_passed') is not True
                or reply['evidence'].get('reply_sha256') != hashlib.sha256(reply['text'].encode()).hexdigest()):
            raise ClarificationError('clarification_reply_not_published')
        frozen = {key: deepcopy(reply[key]) for key in ('reply_id', 'text', 'target', 'evidence', 'verification')}
        frozen['text_sha256'] = hashlib.sha256(reply['text'].encode()).hexdigest()
        frozen['send_fingerprint'] = hashlib.sha256(reply['send_token'].encode()).hexdigest()
        frozen['agent_task_id'] = reply['task_id']
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM clarification_sessions WHERE session_id=?', (session_id,)).fetchone()
            if row is None: raise ClarificationError('clarification_session_missing')
            if row['published_reply'] is not None:
                if json.loads(row['published_reply']) != frozen: raise ClarificationError('clarification_reply_changed')
                return self._view(row)
            if row['state'] != 'awaiting_publication': raise ClarificationError('clarification_session_consumed')
            root = json.loads(row['root'])
            if any(reply['target'].get(key) != root['envelope'][key] for key in root['envelope'] if key != 'evidence'):
                raise ClarificationError('clarification_reply_scope_changed')
            if any(reply['target'].get(key)!=root['binding'][key] for key in ('binding_id','instance_id','knowledge_base_id')):
                raise ClarificationError('clarification_reply_scope_changed')
            db.execute("UPDATE clarification_sessions SET state='awaiting_reply',published_reply=?,revision=revision+1,updated_at=? WHERE session_id=?",
                (canonical_json(frozen), self.clock(), session_id))
            self._event(db, session_id, 'published_exact_clarification', {'reply_id': reply['reply_id'], 'text_sha256': frozen['text_sha256']})
            return self._view(db.execute('SELECT * FROM clarification_sessions WHERE session_id=?', (session_id,)).fetchone())

    def matching(self, candidate):
        with self._db() as db:
            rows = db.execute("SELECT * FROM clarification_sessions WHERE state IN ('awaiting_reply','responding','planned') ORDER BY created_at DESC LIMIT 101").fetchall()
        if len(rows) > 100: raise ClarificationError('clarification_session_limit')
        result = []
        for row in rows:
            value = self._view(row); root = value['root']['envelope']
            if (all(candidate.get(key) == root.get(key) for key in ('sender_profile_url', 'receiver_profile_url', 'video_id'))
                    and candidate.get('parent_id_hint') == value['root']['source_native_comment_id']):
                result.append(value)
        return result

    def claim_response(self, session_id, source_id, envelope):
        if not _SOURCE.fullmatch(source_id): raise ClarificationError('clarification_response_invalid')
        envelope_hash = digest(immutable_envelope(envelope))
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM clarification_sessions WHERE session_id=?', (session_id,)).fetchone()
            if row is None: raise ClarificationError('clarification_session_missing')
            if row['response_source_id'] is not None:
                if row['response_source_id'] != source_id or row['response_hash'] != envelope_hash:
                    raise ClarificationError('clarification_session_consumed')
                return self._view(row)
            if row['state'] != 'awaiting_reply': raise ClarificationError('clarification_session_not_ready')
            db.execute("UPDATE clarification_sessions SET state='responding',response_source_id=?,response_envelope=?,response_hash=?,revision=revision+1,updated_at=? WHERE session_id=?",
                (source_id, canonical_json(envelope), envelope_hash, self.clock(), session_id))
            self._event(db, session_id, 'response_consumed_once', {'source_id': source_id, 'envelope_hash': envelope_hash})
            return self._view(db.execute('SELECT * FROM clarification_sessions WHERE session_id=?', (session_id,)).fetchone())

    def plan(self, session_id, source_id, envelope, decision, queue_item):
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM clarification_sessions WHERE session_id=?', (session_id,)).fetchone()
            if (row is None or row['response_source_id'] != source_id
                    or row['response_hash'] != digest(immutable_envelope(envelope))):
                raise ClarificationError('clarification_response_changed')
            if row['queue_item'] is not None:
                if json.loads(row['queue_item']) != queue_item: raise ClarificationError('clarification_plan_changed')
                return self._view(row)
            if row['state'] != 'responding' or decision.get('action') not in {'save', 'clarify', 'cancel'}:
                raise ClarificationError('clarification_plan_invalid')
            db.execute("UPDATE clarification_sessions SET state='planned',semantic_plan_id=?,decision=?,queue_item=?,revision=revision+1,updated_at=? WHERE session_id=?",
                (decision['plan_id'], canonical_json(decision), canonical_json(queue_item), self.clock(), session_id))
            self._event(db, session_id, 'immutable_handoff_planned', {'source_id': source_id, 'plan_id': decision['plan_id']})
            return self._view(db.execute('SELECT * FROM clarification_sessions WHERE session_id=?', (session_id,)).fetchone())

    def admitted(self, session_id, source_id, task):
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM clarification_sessions WHERE session_id=?', (session_id,)).fetchone()
            if (row is None or row['response_source_id'] != source_id or row['queue_item'] is None
                    or immutable_envelope(json.loads(row['queue_item'])['envelope']) != immutable_envelope(task['envelope'])):
                raise ClarificationError('clarification_handoff_changed')
            if row['child_task_id'] not in (None, task['id']): raise ClarificationError('clarification_handoff_conflict')
            db.execute("UPDATE clarification_sessions SET state='handed_off',child_task_id=?,revision=revision+1,updated_at=? WHERE session_id=? AND child_task_id IS NULL",
                (task['id'], self.clock(), session_id))
            return self._view(db.execute('SELECT * FROM clarification_sessions WHERE session_id=?', (session_id,)).fetchone())
