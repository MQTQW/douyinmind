"""Bounded comment-thread references, isolated from the user's chat history."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from app.mention_agent.contracts import Binding, validate_envelope
from app.storage_guard import (guarded_sqlite_file, verified_readonly_connection,
    SqliteOwnershipError, assert_no_orphan_sqlite_files)


class ConversationStore:
    COMPONENT = "DouyinMind.assistant.conversation.v1"
    ALL_TABLE = "comment_context_all_tasks"
    ALL_COLUMNS = ("task_id", "binding_id", "instance_id", "sender", "receiver", "library_id",
                   "video_id", "thread_id", "envelope", "proof", "query_scope")
    ALL_DDL = """CREATE TABLE comment_context_all_tasks(
      task_id TEXT PRIMARY KEY,binding_id TEXT NOT NULL,instance_id TEXT NOT NULL,
      sender TEXT NOT NULL,receiver TEXT NOT NULL,library_id INTEGER CHECK(library_id IS NULL),
      video_id TEXT NOT NULL,thread_id TEXT NOT NULL,envelope TEXT NOT NULL,proof TEXT NOT NULL,
      query_scope TEXT NOT NULL CHECK(query_scope='all_local'))"""

    def __init__(self, path, protected_paths=()):
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.protected_paths = tuple(Path(p).resolve() for p in protected_paths)
        self._guard_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with guarded_sqlite_file(self.path, timeout=10):
            self._guard(missing_ok=True)
            self._initialize()

    def _guard_path(self):
        if (self.path.resolve() != self.path or self.path.parent.resolve() != self.path.parent
                or any(self.path == p or self.path.is_relative_to(p) or p.is_relative_to(self.path)
                       for p in self.protected_paths)):
            raise ValueError("unsafe_conversation_path")

    def _guard(self, *, missing_ok=False):
        self._guard_path()
        for candidate in (self.path, *(Path(str(self.path)+suffix) for suffix in ('-journal', '-wal', '-shm'))):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                if candidate == self.path and not missing_ok:
                    raise ValueError('conversation_database_missing') from None
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or candidate.resolve() != candidate:
                raise ValueError('unsafe_conversation_path')

    def _initialize(self):
        if self.path.exists():
            def owned(db):
                row = db.execute("SELECT value FROM context_meta WHERE key='component'").fetchone()
                return row is not None and row[0] == self.COMPONENT
            try:
                with verified_readonly_connection(self.path, owned, guard=self._guard):
                    pass
            except SqliteOwnershipError:
                raise ValueError("foreign_conversation_database") from None
        else:
            assert_no_orphan_sqlite_files(self.path)
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = os.fstat(descriptor)
            os.close(descriptor)
            with self._db(initialize=True, bootstrap_identity=(created.st_dev,created.st_ino)) as db:
                db.executescript("""BEGIN IMMEDIATE;
                  CREATE TABLE context_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                  CREATE TABLE comment_context_tasks(task_id TEXT PRIMARY KEY,binding_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,sender TEXT NOT NULL,receiver TEXT NOT NULL,library_id INTEGER NOT NULL,
                    video_id TEXT NOT NULL,thread_id TEXT NOT NULL,envelope TEXT NOT NULL,proof TEXT NOT NULL);
                  CREATE TABLE comment_context_answers(task_id TEXT PRIMARY KEY,answer TEXT NOT NULL,
                    sources TEXT NOT NULL,created_at REAL NOT NULL);
                  INSERT INTO context_meta VALUES('component','DouyinMind.assistant.conversation.v1');
                  COMMIT;""")

    @contextmanager
    def _db(self, *, initialize=False, bootstrap_identity=None):
        with guarded_sqlite_file(self.path, timeout=10):
            with self._db_locked(initialize=initialize,bootstrap_identity=bootstrap_identity) as db:
                yield db

    @contextmanager
    def _db_locked(self, *, initialize=False, bootstrap_identity=None):
        self._guard()
        before = self.path.stat()
        identity = (before.st_dev,before.st_ino)
        if initialize:
            current = self.path.stat()
            if bootstrap_identity is None or (current.st_dev,current.st_ino) != bootstrap_identity or current.st_size != 0:
                raise ValueError('conversation_creation_conflict')
        else:
            def owned(check):
                row = check.execute("SELECT value FROM context_meta WHERE key='component'").fetchone()
                return row is not None and row[0] == self.COMPONENT
            try:
                with verified_readonly_connection(self.path, owned, guard=self._guard):
                    pass
            except SqliteOwnershipError:
                raise ValueError('foreign_conversation_database') from None
        db = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            self._guard()
            current = self.path.stat()
            if (current.st_dev,current.st_ino) != identity:
                raise ValueError('conversation_identity_changed')
            if initialize:
                if (current.st_dev,current.st_ino) != bootstrap_identity or current.st_size != 0:
                    raise ValueError('conversation_creation_conflict')
            if not initialize:
                try:
                    row = db.execute("SELECT value FROM context_meta WHERE key='component'").fetchone()
                except sqlite3.DatabaseError:
                    raise ValueError('foreign_conversation_database') from None
                if row is None or row[0] != self.COMPONENT:
                    raise ValueError('foreign_conversation_database')
                self._scope_v2_ready(db)
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _scope(binding):
        return (binding.binding_id, binding.instance_id, binding.sender_profile_url,
                binding.receiver_profile_url, binding.knowledge_base_id)

    @classmethod
    def _scope_v2_ready(cls, db):
        marker = db.execute("SELECT value FROM context_meta WHERE key='all_local_scope_version'").fetchone()
        table = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (cls.ALL_TABLE,)).fetchone()
        if marker is None and table is None:
            return False
        if marker is None or marker[0] != '1' or table is None:
            raise ValueError('conversation_scope_migration_incomplete')
        columns = db.execute('PRAGMA table_info('+cls.ALL_TABLE+')').fetchall()
        if (' '.join(table[0].split()) != ' '.join(cls.ALL_DDL.split())
                or tuple(row[1] for row in columns) != cls.ALL_COLUMNS
                or columns[0][5] != 1 or any(row[3] != 1 for row in columns[1:] if row[1] != 'library_id')):
            raise ValueError('conversation_scope_schema_invalid')
        if db.execute('SELECT 1 FROM '+cls.ALL_TABLE+" WHERE query_scope<>'all_local' OR library_id IS NOT NULL LIMIT 1").fetchone():
            raise ValueError('conversation_scope_schema_invalid')
        return True

    def prepare_scope_v2(self):
        """Explicit additive migration; never copy or reinterpret library history."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._scope_v2_ready(db):
                return False
            db.execute(self.ALL_DDL)
            db.execute("INSERT INTO context_meta VALUES('all_local_scope_version','1')")
            return True

    @classmethod
    def _table(cls, db, binding):
        if getattr(binding, 'query_scope', 'library') == 'all_local':
            if binding.knowledge_base_id is not None:
                raise ValueError('invalid_all_local_scope')
            if not cls._scope_v2_ready(db):
                raise ValueError('conversation_scope_migration_required')
            return cls.ALL_TABLE
        if type(binding.knowledge_base_id) is not int or binding.knowledge_base_id <= 0:
            raise ValueError('invalid_library_scope')
        return 'comment_context_tasks'

    @classmethod
    def _find_task(cls, db, task_id):
        row = db.execute('SELECT * FROM comment_context_tasks WHERE task_id=?', (task_id,)).fetchone()
        other = (db.execute('SELECT * FROM '+cls.ALL_TABLE+' WHERE task_id=?', (task_id,)).fetchone()
                 if cls._scope_v2_ready(db) else None)
        if row is not None and other is not None:
            raise ValueError('thread_scope_conflict')
        return row if row is not None else other

    @classmethod
    def _matches_scope(cls, row, binding):
        return (row is not None
            and tuple(row[key] for key in ('binding_id','instance_id','sender','receiver','library_id')) == cls._scope(binding)
            and ('query_scope' in row.keys()) == bool(getattr(binding,'is_all_local',False)))

    def bind_task(self, task_id, binding: Binding, envelope, thread_id, proof):
        envelope = validate_envelope(envelope, binding)
        if (not isinstance(task_id, str) or not 1 <= len(task_id) <= 128
                or not isinstance(thread_id, str) or not 1 <= len(thread_id) <= 256
                or not isinstance(proof, dict) or set(proof) - {"quality", "root_reference_id"}
                or proof.get("quality") not in {"current_comment", "verified_ancestor", "independent"}):
            raise ValueError("invalid_thread_proof")
        value = (task_id, *self._scope(binding), envelope["video_id"], thread_id,
                 json.dumps(envelope, ensure_ascii=False, sort_keys=True), json.dumps(proof, ensure_ascii=False, sort_keys=True))
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            table = self._table(db, binding)
            old = self._find_task(db, task_id)
            if table == self.ALL_TABLE:
                value += ('all_local',)
            if old is not None:
                if tuple(old) != value:
                    raise ValueError("thread_scope_conflict")
            else:
                db.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in value)+')', value)

    def task_scope(self, task_id):
        with self._db() as db:
            row = self._find_task(db, task_id)
            return dict(row) if row is not None else None

    @classmethod
    def _all_public_root_rows(cls, db, binding, video_id, reference_id=None):
        """Published real-mention anchors for the same physical owner namespace.

        Only callers already authorized for all_local may use this catalogue.
        It supplies DOM ancestor candidates, never retrieval evidence or a KB.
        """
        if not binding.is_all_local:
            return []
        rows=[]
        for table in ('comment_context_tasks',cls.ALL_TABLE):
            extra=' AND t.thread_id=?' if reference_id is not None else ''
            params=(binding.instance_id,binding.sender_profile_url,binding.receiver_profile_url,video_id)
            if reference_id is not None:params+=(reference_id,)
            rows.extend(db.execute('''SELECT t.thread_id,t.envelope,a.created_at AS answered_at FROM '''+table+''' t
              JOIN comment_context_answers a ON a.task_id=t.task_id WHERE t.instance_id=?
              AND t.sender=? AND t.receiver=? AND t.video_id=?
              AND json_extract(t.envelope,'$.reference_id')=t.thread_id'''+extra+'''
              ORDER BY a.created_at DESC LIMIT 24''',params).fetchall())
        valid=[]
        for row in rows:
            envelope=json.loads(row['envelope']);proof=envelope.get('evidence',{})
            if (proof.get('real_mention') is not True or proof.get('source_kind') is not None
                    or envelope.get('sender_profile_url')!=binding.sender_profile_url
                    or envelope.get('receiver_profile_url')!=binding.receiver_profile_url
                    or envelope.get('video_id')!=video_id):
                continue
            valid.append(row)
        return sorted(valid,key=lambda row:row['answered_at'],reverse=True)

    def roots(self, binding, video_id):
        with self._db() as db:
            table = self._table(db, binding)
            if binding.is_all_local:
                rows=self._all_public_root_rows(db,binding,video_id)
            else:
                rows = db.execute("""SELECT t.thread_id,t.envelope FROM """+table+""" t
                  JOIN comment_context_answers a ON a.task_id=t.task_id WHERE t.binding_id=? AND t.instance_id=?
                  AND t.sender=? AND t.receiver=? AND t.library_id IS ? AND t.video_id=?
                  ORDER BY a.created_at DESC LIMIT 24""", (*self._scope(binding), video_id)).fetchall()
        result, seen = [], set()
        for row in rows:
            envelope = json.loads(row["envelope"])
            if envelope["reference_id"] == row["thread_id"] and row["thread_id"] not in seen:
                result.append({"reference_id": row["thread_id"], "comment_text": envelope["comment_text"]})
                seen.add(row["thread_id"])
        return result[:24]

    def context_for(self, task_id, binding):
        with self._db() as db:
            table = self._table(db, binding)
            scope = self._find_task(db, task_id)
            if not self._matches_scope(scope, binding):
                return None
            rows = db.execute("""SELECT t.envelope,a.answer,a.created_at AS answered_at FROM """+table+""" t
              JOIN comment_context_answers a ON a.task_id=t.task_id WHERE t.binding_id=? AND t.instance_id=?
              AND t.sender=? AND t.receiver=? AND t.library_id IS ? AND t.video_id=? AND t.thread_id=?
              AND t.task_id<>? ORDER BY a.created_at DESC LIMIT 3""",
              (*self._scope(binding), scope["video_id"], scope["thread_id"], task_id)).fetchall()
            # The exact task still belongs to its all_local Binding. Only a
            # verified ancestor backed by a real published root permits taking
            # prior public wording from a named scope as reference context.
            proof=json.loads(scope['proof'])
            if (binding.is_all_local and proof.get('quality')=='verified_ancestor'
                    and proof.get('root_reference_id')==scope['thread_id']):
                anchors=self._all_public_root_rows(db,binding,scope['video_id'],scope['thread_id'])
                anchor_texts={json.loads(row['envelope'])['comment_text'] for row in anchors}
                if len(anchor_texts)==1:
                    cross=[]
                    for source_table in ('comment_context_tasks',self.ALL_TABLE):
                        cross.extend(db.execute('''SELECT t.envelope,a.answer,a.created_at AS answered_at FROM '''+source_table+''' t
                          JOIN comment_context_answers a ON a.task_id=t.task_id WHERE t.instance_id=?
                          AND t.sender=? AND t.receiver=? AND t.video_id=? AND t.thread_id=? AND t.task_id<>?
                          ORDER BY a.created_at DESC LIMIT 3''',
                          (binding.instance_id,binding.sender_profile_url,binding.receiver_profile_url,
                           scope['video_id'],scope['thread_id'],task_id)).fetchall())
                    rows=sorted(cross,key=lambda row:row['answered_at'],reverse=True)[:3]
        if not rows:
            return None
        questions, used = [], 0
        for row in rows:
            question = json.loads(row["envelope"])["comment_text"]
            if len(question) <= 1000 - used:
                questions.append(question)
                used += len(question)
        return {"prior_questions": list(reversed(questions)), "previous_answer": rows[0]["answer"]}

    def record_published(self, task_id, binding, reply):
        if (not isinstance(reply, dict) or reply.get("state") != "b_view_visible"
                or reply.get("kind") != "answer" or not isinstance(reply.get("text"), str)
                or not reply["text"].strip() or len(reply["text"]) > 1000):
            return False
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._table(db, binding)
            scope = self._find_task(db, task_id)
            if not self._matches_scope(scope, binding):
                return False
            old = db.execute("SELECT answer,sources FROM comment_context_answers WHERE task_id=?", (task_id,)).fetchone()
            sources = json.dumps(reply.get("sources", []), ensure_ascii=False, sort_keys=True)
            if old is not None:
                if old["answer"] != reply["text"] or old["sources"] != sources:
                    raise ValueError("published_context_conflict")
                return False
            db.execute("INSERT INTO comment_context_answers VALUES(?,?,?,?)", (task_id, reply["text"], sources, time.time()))
            return True
