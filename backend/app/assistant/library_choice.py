"""Durable library selection, separate from answer history and paid execution.

Only trusted normal-page evidence can select a frozen option. This store never
opens a browser, calls a model, imports a video, or grants library permission.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from app.mention_agent.contracts import Binding, canonical_json, public_profile, validate_envelope
from app.storage_guard import (StorageGuardError, guarded_sqlite_file,
    verified_readonly_connection, assert_no_orphan_sqlite_files)


DATABASE_NAME = 'library-choice.sqlite3'
MARKER_NAME = 'library-choice-required.json'
COMPONENT = 'DouyinMind.assistant.library_choice.v1'
MARKER_BYTES = (canonical_json({'component':COMPONENT,'version':1})+'\n').encode('ascii')
DEFAULT_PATH = Path(__file__).resolve().parents[1]/'storage'/'assistant'/DATABASE_NAME
_SECRET = frozenset({'api_key','cookie','cookies','password','access_token','refresh_token',
    'client_secret','credentials','storage_state','reasoning_content','raw_messages'})
_STATES = frozenset({'awaiting_ask','waiting_library','ask_unknown','ask_held',
    'selected','handoff_claimed','handed_off','needs_review','cancelled'})
_SCHEMA = {
    'choice_meta': 'CREATE TABLE choice_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
    'choice_sessions': """CREATE TABLE choice_sessions(
      choice_id TEXT PRIMARY KEY,root_key TEXT UNIQUE NOT NULL,immutable TEXT NOT NULL,
      root_task_id TEXT NOT NULL,root_binding TEXT NOT NULL,root_envelope TEXT NOT NULL,
      authorization_snapshot TEXT NOT NULL,ask_ref TEXT NOT NULL,ask_text TEXT NOT NULL,
      options TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>=1),
      ask_reply_ref TEXT,ask_proof TEXT,selection_key TEXT UNIQUE,selection_ref TEXT,
      selection_envelope TEXT,selected_kb_id INTEGER,selected_authorization TEXT,
      selection_token TEXT,child_key TEXT UNIQUE,handoff_token TEXT,handoff_owner TEXT,
      handoff_lease_until REAL,child_task_id TEXT UNIQUE,created_at REAL NOT NULL,updated_at REAL NOT NULL)""",
    'choice_selection_refs': """CREATE TABLE choice_selection_refs(
      selection_key TEXT PRIMARY KEY,choice_id TEXT UNIQUE NOT NULL,envelope_hash TEXT NOT NULL,
      created_at REAL NOT NULL)""",
    'choice_events': """CREATE TABLE choice_events(id INTEGER PRIMARY KEY,choice_id TEXT NOT NULL,
      event TEXT NOT NULL,metadata TEXT NOT NULL,created_at REAL NOT NULL)""",
    'choice_rechecks': """CREATE TABLE choice_rechecks(choice_id TEXT PRIMARY KEY,
      next_check_at REAL NOT NULL,updated_at REAL NOT NULL)""",
}


class LibraryChoiceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value, maximum=512):
    if not isinstance(value,str) or not value.strip() or '\x00' in value or len(value)>maximum:
        raise LibraryChoiceError('invalid_choice_text')
    return value  # Original platform text is never trimmed or rewritten.


def _json(value, maximum=131072):
    def check(item, depth=0):
        if depth>12: raise LibraryChoiceError('choice_metadata_too_deep')
        if isinstance(item,dict):
            for key, child in item.items():
                if not isinstance(key,str) or key.casefold() in _SECRET:
                    raise LibraryChoiceError('forbidden_choice_metadata')
                check(child,depth+1)
        elif isinstance(item,(list,tuple)):
            for child in item: check(child,depth+1)
    check(value)
    try: result=canonical_json(value)
    except (ValueError,TypeError,RecursionError): raise LibraryChoiceError('invalid_choice_json') from None
    if len(result.encode('utf8'))>maximum: raise LibraryChoiceError('choice_metadata_too_large')
    return result


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf8')).hexdigest()


def _auth(value, *, root=None, library=None):
    required={'registry_id','user_id','device_id','binding_id','instance_id','knowledge_base_id',
              'action','public_reply_allowed','user_revision','device_revision','grant_revision','grant_version','grant_id'}
    if not isinstance(value,dict) or not required.issubset(value):
        raise LibraryChoiceError('invalid_choice_authorization')
    for name in ('registry_id','user_id','device_id','binding_id','instance_id','grant_id'):
        _text(value[name],128)
    for name in ('user_revision','device_revision','grant_revision','grant_version'):
        if type(value[name]) is not int or value[name]<1: raise LibraryChoiceError('invalid_choice_authorization')
    if type(value['public_reply_allowed']) is not bool:
        raise LibraryChoiceError('invalid_choice_authorization')
    if library is not None and (value['knowledge_base_id']!=library or type(value['knowledge_base_id']) is not int
                               or value['action']!='import'):
        raise LibraryChoiceError('choice_option_not_import_authorized')
    if root is not None and any(value[name]!=root[name] for name in ('registry_id','user_id','device_id','instance_id')):
        raise LibraryChoiceError('choice_option_owner_conflict')
    _json(value)
    return json.loads(_json(value))


class LibraryChoiceStore:
    """One original save root -> one ask -> one selected reply -> one child.

    prepare() is explicit. A missing initialized database/marker never recreates
    a menu or a cleared selection. Cross-database handoff uses stable child_key;
    the caller must look up/reuse its real selection task before enqueueing.
    """
    def __init__(self,path=DEFAULT_PATH,*,clock=None,protected_paths=()):
        self.path=Path(path)
        self.marker_path=self.path.parent/MARKER_NAME
        self.clock=clock or time.time
        self.protected_paths=tuple(Path(value).resolve() for value in protected_paths)
        self._location()
        if not self.path.parent.is_dir(): raise LibraryChoiceError('choice_database_missing')
        if not self.path.exists() and not self.path.is_symlink():
            assert_no_orphan_sqlite_files(self.path)
            raise LibraryChoiceError('choice_database_missing')
        with self._db(): pass

    @classmethod
    def prepare(cls,path=DEFAULT_PATH,*,clock=None,protected_paths=()):
        value=cls.__new__(cls)
        value.path=Path(path);value.marker_path=value.path.parent/MARKER_NAME
        value.clock=clock or time.time
        value.protected_paths=tuple(Path(p).resolve() for p in protected_paths)
        value._location();value.path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with guarded_sqlite_file(value.path,timeout=10):
                value._guard(require_marker=False)
                if value.path.exists():
                    if value.path.stat().st_size==0: raise LibraryChoiceError('foreign_choice_database')
                    with value._db(): pass
                    return value
                if value.marker_path.exists() or value.marker_path.is_symlink():
                    raise LibraryChoiceError('choice_database_missing')
                assert_no_orphan_sqlite_files(value.path)
                descriptor=os.open(value.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
                try: created=os.fstat(descriptor)
                finally: os.close(descriptor)
                value._guard(require_marker=False)
                if value._identity(created)!=value._identity(value.path.lstat()):
                    raise LibraryChoiceError('choice_identity_changed')
                db=sqlite3.connect(value.path.as_uri()+'?mode=rw',uri=True)
                try:
                    db.execute('BEGIN IMMEDIATE')
                    if db.execute('SELECT name FROM sqlite_master').fetchone():
                        raise LibraryChoiceError('foreign_choice_database')
                    for ddl in _SCHEMA.values(): db.execute(ddl)
                    db.executemany('INSERT INTO choice_meta VALUES(?,?)',
                        [('component',COMPONENT),('version','1'),('requires_marker','1')])
                    db.commit()
                except BaseException:
                    db.rollback();raise
                finally: db.close()
                value._write_marker()
                with value._db(): pass
                return value
        except StorageGuardError: raise LibraryChoiceError('unsafe_choice_storage') from None

    @staticmethod
    def _identity(info): return info.st_dev,info.st_ino

    def _location(self):
        if (not self.path.is_absolute() or self.path.name!=DATABASE_NAME or self.path.resolve()!=self.path
                or self.path.parent.resolve()!=self.path.parent or any(self.path==p or self.path.is_relative_to(p)
                or p.is_relative_to(self.path) for p in self.protected_paths)):
            raise LibraryChoiceError('unsafe_choice_path')

    def _marker(self,required=True):
        try: info=self.marker_path.lstat()
        except FileNotFoundError:
            if required: raise LibraryChoiceError('choice_marker_missing') from None
            return False
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or self.marker_path.resolve()!=self.marker_path
                or info.st_size!=len(MARKER_BYTES)):
            raise LibraryChoiceError('unsafe_choice_marker')
        with self.marker_path.open('rb') as source:
            opened=os.fstat(source.fileno())
            if self._identity(opened)!=self._identity(info) or opened.st_nlink!=1 or source.read(len(MARKER_BYTES)+1)!=MARKER_BYTES:
                raise LibraryChoiceError('invalid_choice_marker')
        return True

    def _write_marker(self):
        if self._marker(False): return
        descriptor=os.open(self.marker_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            pending=MARKER_BYTES
            while pending:
                count=os.write(descriptor,pending)
                if count<=0: raise LibraryChoiceError('choice_marker_write_failed')
                pending=pending[count:]
            os.fsync(descriptor)
        finally: os.close(descriptor)
        self._marker()

    def _guard(self,require_marker=True):
        self._location()
        for suffix in ('','-journal','-wal','-shm'):
            path=Path(str(self.path)+suffix)
            try: info=path.lstat()
            except FileNotFoundError: continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or path.resolve()!=path:
                raise LibraryChoiceError('unsafe_choice_file')
        self._marker(require_marker)

    @staticmethod
    def _owned(db):
        try:
            if dict(db.execute('SELECT key,value FROM choice_meta'))!={'component':COMPONENT,'version':'1','requires_marker':'1'}:
                return False
            actual=dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            return set(actual)==set(_SCHEMA) and all(' '.join(actual[name].split())==' '.join(ddl.split()) for name,ddl in _SCHEMA.items())
        except sqlite3.Error: return False

    @contextmanager
    def _db(self,write=False):
        try:
            with guarded_sqlite_file(self.path,timeout=10):
                self._guard()
                if not self.path.exists():
                    assert_no_orphan_sqlite_files(self.path)
                    raise LibraryChoiceError('choice_database_missing')
                identity=self._identity(self.path.lstat())
                with verified_readonly_connection(self.path,self._owned,guard=self._guard) as checked:
                    checked.row_factory=sqlite3.Row;checked.execute('PRAGMA query_only=ON')
                    if not write:
                        yield checked;return
                self._guard()
                if self._identity(self.path.lstat())!=identity: raise LibraryChoiceError('choice_identity_changed')
                db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=10)
                db.row_factory=sqlite3.Row
                try:
                    self._guard()
                    if self._identity(self.path.lstat())!=identity or not self._owned(db):
                        raise LibraryChoiceError('foreign_choice_database')
                    db.execute('BEGIN IMMEDIATE');yield db;db.commit()
                except BaseException:
                    db.rollback();raise
                finally: db.close()
        except StorageGuardError: raise LibraryChoiceError('unsafe_choice_storage') from None
        except sqlite3.Error: raise LibraryChoiceError('choice_storage_unavailable') from None

    def _now(self):
        value=self.clock()
        if type(value) not in (float,int) or not math.isfinite(value) or value<0:
            raise LibraryChoiceError('invalid_choice_clock')
        return value

    @staticmethod
    def _event(db,choice,event,metadata,now):
        db.execute('INSERT INTO choice_events(choice_id,event,metadata,created_at) VALUES(?,?,?,?)',
                   (choice,event,_json(metadata),now))

    @staticmethod
    def _row(row,*,created=False,private=False):
        if row is None: return None
        value=dict(row)
        for field in ('root_binding','root_envelope','authorization_snapshot','options','ask_proof',
                      'selection_envelope','selected_authorization'):
            value[field]=json.loads(value[field]) if value[field] is not None else None
        value.pop('immutable',None)
        if not private:
            value.pop('selection_token',None);value.pop('handoff_token',None)
        value['created']=created
        return value

    def create(self,*,root_task_id,root_binding,root_envelope,ask_ref,ask_text,options,authorization):
        binding=Binding.from_dict(root_binding) if isinstance(root_binding,dict) else root_binding
        if not isinstance(binding,Binding): raise LibraryChoiceError('invalid_choice_binding')
        envelope=validate_envelope(root_envelope,binding)
        if envelope['evidence'].get('source_kind') is not None or envelope['evidence'].get('real_mention') is not True:
            raise LibraryChoiceError('choice_requires_original_mention_root')
        _text(root_task_id,128);_text(ask_ref,256);_text(ask_text,320)
        auth=_auth(authorization)
        if (auth['binding_id']!=binding.binding_id or auth['instance_id']!=binding.instance_id
                or auth['knowledge_base_id']!=binding.knowledge_base_id
                or auth.get('query_scope','library')!=binding.query_scope):
            raise LibraryChoiceError('choice_root_authorization_conflict')
        if not isinstance(options,list) or not 1<=len(options)<=128:
            raise LibraryChoiceError('invalid_choice_options')
        frozen=[];ids=set();ordinals=set()
        for option in options:
            if (not isinstance(option,dict) or not {'knowledge_base_id','name','authorization'}<=set(option)
                    or set(option)-{'knowledge_base_id','name','authorization','ordinal'}):
                raise LibraryChoiceError('invalid_choice_option')
            identity=option['knowledge_base_id']
            if type(identity) is not int or identity<=0 or identity in ids:
                raise LibraryChoiceError('invalid_choice_option')
            _text(option['name'],128);ids.add(identity)
            ordinal=option.get('ordinal',len(frozen)+1)
            if type(ordinal) is not int or not 1<=ordinal<=128 or ordinal in ordinals:
                raise LibraryChoiceError('invalid_choice_option_ordinal')
            ordinals.add(ordinal)
            frozen.append({'knowledge_base_id':identity,'name':option['name'],
                           'authorization':_auth(option['authorization'],root=auth,library=identity),
                           **({'ordinal':ordinal} if 'ordinal' in option else {})})
        root_identity={'instance_id':binding.instance_id,'sender':binding.sender_profile_url,
            'receiver':binding.receiver_profile_url,'video_id':envelope['video_id'],
            'source_mode':envelope['source_mode'],'root_reference_id':envelope['reference_id']}
        root_key=_digest(root_identity)
        immutable={'root_task_id':root_task_id,'root_binding':binding.to_dict(),
            'root_envelope':envelope,'authorization_snapshot':auth,'ask_ref':ask_ref,'ask_text':ask_text,'options':frozen}
        encoded=_json(immutable);choice_id=_digest({'kind':'library_choice_v1','root':root_identity,
            'ask_ref':ask_ref,'options':frozen,'authorization_snapshot':auth})
        with self._db(True) as db:
            task_rows=db.execute('SELECT root_key FROM choice_sessions WHERE root_task_id=?',(root_task_id,)).fetchall()
            if any(row['root_key']!=root_key for row in task_rows):
                raise LibraryChoiceError('choice_root_task_conflict')
            prior=db.execute('SELECT * FROM choice_sessions WHERE root_key=?',(root_key,)).fetchone()
            if prior is not None:
                old=json.loads(prior['root_envelope'])
                comparable=lambda value:{key:item for key,item in value.items() if key not in {'command_id','evidence'}}
                if comparable(old)!=comparable(envelope): raise LibraryChoiceError('choice_original_root_conflict')
                return self._row(prior)  # Never regenerate menu/auth or publish another ask on replay.
            now=self._now()
            db.execute('''INSERT INTO choice_sessions(choice_id,root_key,immutable,root_task_id,root_binding,root_envelope,
                authorization_snapshot,ask_ref,ask_text,options,state,revision,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'awaiting_ask',1,?,?)''',
                (choice_id,root_key,encoded,root_task_id,_json(binding.to_dict()),_json(envelope),_json(auth),
                 ask_ref,ask_text,_json(frozen),now,now))
            self._event(db,choice_id,'created',{'option_count':len(frozen)},now)
            return self._row(db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(choice_id,)).fetchone(),created=True)

    def get(self,choice_id):
        with self._db() as db: return self._row(db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(_text(choice_id,128),)).fetchone())

    def get_for_root_task(self,root_task_id):
        with self._db() as db:
            rows=db.execute('SELECT * FROM choice_sessions WHERE root_task_id=? LIMIT 2',(_text(root_task_id,128),)).fetchall()
            if len(rows)>1:raise LibraryChoiceError('choice_root_task_ambiguous')
            return self._row(rows[0]) if rows else None

    def get_for_selection_reference(self,sender,receiver,source_mode,reference_id,instance_id):
        sender,receiver=public_profile(sender),public_profile(receiver)
        if source_mode not in {'live_ui','replay','isolated'}:raise LibraryChoiceError('invalid_choice_source_mode')
        _text(reference_id,512);_text(instance_id,128)
        with self._db() as db:
            rows=db.execute('SELECT * FROM choice_sessions WHERE selection_ref=?',(reference_id,)).fetchall()
            found=[]
            for row in rows:
                binding=json.loads(row['root_binding']);envelope=json.loads(row['selection_envelope'])
                if (binding['sender_profile_url']==sender and binding['receiver_profile_url']==receiver
                        and binding['instance_id']==instance_id and envelope['source_mode']==source_mode):
                    found.append(row)
            if len(found)>1:raise LibraryChoiceError('choice_selection_reference_ambiguous')
            return self._row(found[0]) if found else None

    def hold_waiting(self,choice_id,reason_code='binding_changed'):
        if not isinstance(reason_code,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,127}',reason_code):
            raise LibraryChoiceError('invalid_choice_hold_reason')
        with self._db(True) as db:
            row=db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(_text(choice_id,128),)).fetchone()
            if row is None:raise LibraryChoiceError('choice_not_found')
            if row['state'] not in {'awaiting_ask','waiting_library','ask_unknown','ask_held'}:
                return self._row(row)
            now=self._now()
            db.execute("UPDATE choice_sessions SET state='needs_review',revision=revision+1,updated_at=? WHERE choice_id=?",(now,choice_id))
            self._event(db,choice_id,'waiting_held',{'reason_code':reason_code,'previous_state':row['state']},now)
            return self._row(db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(choice_id,)).fetchone())

    def _ask_proof(self,row,ask_reply_ref,proof):
        root=json.loads(row['root_envelope']);binding=json.loads(row['root_binding'])
        expected={'sender_profile_url':binding['sender_profile_url'],'receiver_profile_url':binding['receiver_profile_url'],
            'video_id':root['video_id'],'root_reference_id':root['reference_id'],'root_comment_text':root['comment_text'],
            'ask_ref':row['ask_ref'],'ask_text':row['ask_text'],'ask_reply_ref':ask_reply_ref}
        if (not isinstance(proof,dict) or any(proof.get(key)!=value for key,value in expected.items())
                or proof.get('source_kind')!='library_choice_ask' or proof.get('exact_body_in_original_thread') is not True
                or proof.get('ask_author_profile_url')!=binding['receiver_profile_url']
                or proof.get('receiver_verified') is not True or type(proof.get('unique_count')) is not int
                or proof['unique_count']!=1 or proof.get('reference_quality') not in {'native','verified_ui'}):
            raise LibraryChoiceError('choice_ask_not_verified')
        native=proof.get('native_comment_id')
        if proof['reference_quality']=='native':
            if not isinstance(native,str) or not re.fullmatch(r'[0-9]{5,30}',native):
                raise LibraryChoiceError('choice_ask_not_verified')
        elif native is not None: raise LibraryChoiceError('choice_ask_not_verified')
        self._fresh_observation(proof)
        _json(proof)

    def _fresh_observation(self,proof):
        try:
            observed=datetime.fromisoformat(proof['observed_at_utc'].replace('Z','+00:00'))
            if observed.tzinfo is None or not -5<=self._now()-observed.timestamp()<=120: raise ValueError
        except (KeyError,TypeError,ValueError,OverflowError):
            raise LibraryChoiceError('choice_observation_not_fresh') from None

    def record_ask_outcome(self,choice_id,*,ask_ref,outcome,ask_reply_ref=None,proof=None):
        if outcome not in {'b_view_visible','delivery_unknown','verification_required','failed_no_submit','needs_review'}:
            raise LibraryChoiceError('invalid_choice_ask_outcome')
        if outcome!='b_view_visible' and (ask_reply_ref is not None or proof is not None):
            raise LibraryChoiceError('choice_unknown_ask_cannot_claim_reference')
        with self._db(True) as db:
            row=db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(_text(choice_id,128),)).fetchone()
            if row is None or ask_ref!=row['ask_ref']: raise LibraryChoiceError('choice_ask_scope_conflict')
            if outcome=='b_view_visible':
                _text(ask_reply_ref,512);self._ask_proof(row,ask_reply_ref,proof)
                if row['ask_reply_ref'] is not None and row['ask_reply_ref']!=ask_reply_ref:
                    raise LibraryChoiceError('choice_ask_scope_conflict')
                if row['state'] in {'waiting_library','selected','handoff_claimed','handed_off'}:
                    return self._row(row)
                if row['state'] not in {'awaiting_ask','ask_unknown','ask_held'}:
                    raise LibraryChoiceError('choice_ask_not_waiting')
                state='waiting_library'
            else:
                if row['state'] not in {'awaiting_ask','ask_unknown','ask_held'}: return self._row(row)
                state='ask_unknown' if outcome in {'delivery_unknown','verification_required'} else 'ask_held'
                if row['state']==state:return self._row(row)  # Reconciliation replay adds no new intent.
            now=self._now()
            db.execute('''UPDATE choice_sessions SET state=?,revision=revision+1,ask_reply_ref=COALESCE(?,ask_reply_ref),
                ask_proof=COALESCE(?,ask_proof),updated_at=? WHERE choice_id=?''',
                (state,ask_reply_ref,_json(proof) if outcome=='b_view_visible' else None,now,choice_id))
            self._event(db,choice_id,'ask_observed',{'outcome':outcome,'state':state},now)
            return self._row(db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(choice_id,)).fetchone())

    def list_roots(self,*,sender,receiver,video_id,limit=24):
        sender,receiver=public_profile(sender),public_profile(receiver)
        if type(limit) is not int or not 1<=limit<=100: raise LibraryChoiceError('invalid_choice_limit')
        with self._db() as db:
            rows=db.execute("SELECT * FROM choice_sessions WHERE state='waiting_library' ORDER BY created_at DESC,choice_id").fetchall()
        result=[]
        for row in rows:
            value=self._row(row);binding=value['root_binding'];root=value['root_envelope']
            if (binding['sender_profile_url']!=sender or binding['receiver_profile_url']!=receiver or root['video_id']!=video_id): continue
            result.append({'choice_id':value['choice_id'],'revision':value['revision'],
                'root_reference_id':root['reference_id'],'root_comment_text':root['comment_text'],
                'ask_text':value['ask_text'],'ask_ref':value['ask_ref'],'ask_reply_ref':value['ask_reply_ref'],
                'options':value['options'],'instance_id':binding['instance_id'],
                'sender_profile_url':sender,'receiver_profile_url':receiver,'video_id':video_id})
            if len(result)>=limit: break
        return result

    def list_waiting(self,receiver_profile_url,limit=24,instance_id=None):
        return self._list_scheduled(receiver_profile_url,('waiting_library',),limit,instance_id)

    def list_asks_to_observe(self,receiver_profile_url,limit=24,instance_id=None):
        """List unresolved asks for caller-fenced observation, never publication."""
        return self._list_scheduled(receiver_profile_url,('awaiting_ask','ask_unknown','ask_held'),limit,instance_id)

    def _list_scheduled(self,receiver_profile_url,states,limit,instance_id):
        receiver=public_profile(receiver_profile_url)
        if type(limit) is not int or not 1<=limit<=100: raise LibraryChoiceError('invalid_choice_limit')
        if instance_id is not None: _text(instance_id,128)
        with self._db() as db:
            rows=db.execute("""SELECT s.*,COALESCE(r.next_check_at,0) AS next_check_at FROM choice_sessions s LEFT JOIN choice_rechecks r ON r.choice_id=s.choice_id
                WHERE s.state IN ("""+','.join('?' for _ in states)+""") AND COALESCE(r.next_check_at,0)<=?
                ORDER BY COALESCE(r.next_check_at,0),s.created_at,s.choice_id""",(*states,self._now())).fetchall()
        result=[]
        for row in rows:
            value=self._row(row);binding=value['root_binding']
            if binding['receiver_profile_url']!=receiver or instance_id is not None and binding['instance_id']!=instance_id:
                continue
            result.append(value)
            if len(result)>=limit: break
        return result

    def defer_waiting_recheck(self,choice_id,seconds=120):
        if type(seconds) is not int or not 1<=seconds<=600: raise LibraryChoiceError('invalid_choice_recheck_delay')
        with self._db(True) as db:
            row=db.execute('SELECT state FROM choice_sessions WHERE choice_id=?',(_text(choice_id,128),)).fetchone()
            if row is None: raise LibraryChoiceError('choice_not_found')
            if row['state'] not in {'waiting_library','awaiting_ask','ask_unknown','ask_held'}: return False
            now=self._now()
            db.execute('INSERT OR REPLACE INTO choice_rechecks VALUES(?,?,?)',(choice_id,now+seconds,now))
            return True  # Scheduling only: no state/revision, seen, or authorization mutation.

    def reserve_selection(self,choice_id,*,expected_revision,selection_ref,selection_envelope,
                          selected_kb_id,selected_authorization,proof=None,semantic_decision=None):
        if type(expected_revision) is not int or expected_revision<1 or type(selected_kb_id) is not int or selected_kb_id<=0:
            raise LibraryChoiceError('invalid_choice_selection')
        _text(selection_ref,512)
        # Import lazily: the shared validator is pure and owned by the platform
        # adapter integration, not a second hand-written @ constructor here.
        from app.mention_agent.choice_evidence import validate_choice_envelope
        semantic=None
        if semantic_decision is not None:
            # Read the owned issuer outside our write transaction. Inside the
            # CAS below only the verified immutable metadata is compared.
            from .semantic_plan_store import verify_semantic_decision
            try:semantic=verify_semantic_decision(semantic_decision,mode='choice',original_text=selection_envelope['comment_text'])
            except Exception:raise LibraryChoiceError('choice_semantic_decision_unverified') from None
        with self._db(True) as db:
            row=db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(_text(choice_id,128),)).fetchone()
            if row is None: raise LibraryChoiceError('choice_not_found')
            root_binding=json.loads(row['root_binding']);root_envelope=json.loads(row['root_envelope'])
            envelope=validate_choice_envelope(selection_envelope,root_binding=root_binding,root_envelope=root_envelope,
                ask_ref=row['ask_ref'],ask_reply_ref=row['ask_reply_ref'],ask_text=row['ask_text'],choice_id=choice_id)
            if envelope['reference_id']!=selection_ref or envelope['source_mode']!=root_envelope['source_mode']:
                raise LibraryChoiceError('choice_selection_scope_conflict')
            if proof is not None and _json(proof)!=_json(envelope['evidence']):
                raise LibraryChoiceError('choice_selection_scope_conflict')
            self._fresh_observation(envelope['evidence'])
            options=json.loads(row['options'])
            option=next((value for value in options if value['knowledge_base_id']==selected_kb_id),None)
            if option is None or _json(_auth(selected_authorization))!=_json(option['authorization']):
                raise LibraryChoiceError('choice_selection_authorization_changed')
            if semantic is None:
                from app.assistant.choice_intent import parse_library_selection
                value=envelope['evidence'].get('selection_value',envelope['comment_text'])
                try: parsed=parse_library_selection(value,options)
                except (ValueError,TypeError,KeyError): parsed=None
                if not isinstance(parsed,dict) or parsed.get('knowledge_base_id')!=selected_kb_id:
                    raise LibraryChoiceError('choice_selection_text_not_single_option')
            else:
                from .choice_intent import semantic_choice_catalog,semantic_choice_owner
                from .semantic_plan_store import ui_semantic_digest
                if (semantic.get('action')!='save' or semantic.get('target_state')!='one'
                        or semantic.get('grant_id')!=option['authorization']['grant_id']
                        or semantic.get('catalog_snapshot')!=semantic_choice_catalog(options)
                        or semantic.get('owner_scope')!=semantic_choice_owner(self._row(row),envelope)
                        or semantic.get('frozen_ui_digest')!=ui_semantic_digest(envelope)):
                    raise LibraryChoiceError('choice_semantic_decision_scope_changed')
            identity={'instance_id':root_binding['instance_id'],'sender':root_binding['sender_profile_url'],
                'receiver':root_binding['receiver_profile_url'],'source_mode':envelope['source_mode'],
                'reference':envelope['native_comment_id'] if envelope['reference_quality']=='native' else selection_ref,
                'quality':envelope['reference_quality']}
            selection_key=_digest(identity)
            frozen_envelope={**envelope,'evidence':{key:value for key,value in envelope['evidence'].items()
                if key not in {'observed_at_utc','diagnostics'}}}
            envelope_hash=_digest(frozen_envelope)
            existing=db.execute('SELECT * FROM choice_selection_refs WHERE selection_key=?',(selection_key,)).fetchone()
            if existing is not None:
                if (existing['choice_id']!=choice_id or existing['envelope_hash']!=envelope_hash
                        or row['selected_kb_id']!=selected_kb_id):
                    raise LibraryChoiceError('choice_selection_already_used')
                return self._row(row)
            if row['state']!='waiting_library' or row['revision']!=expected_revision:
                raise LibraryChoiceError('choice_selection_stale')
            if db.execute("SELECT COUNT(*) FROM choice_sessions WHERE root_key=? AND state='waiting_library'",(row['root_key'],)).fetchone()[0]!=1:
                raise LibraryChoiceError('choice_pending_root_ambiguous')
            now=self._now();token=secrets.token_hex(24)
            child_key='choice-child-'+_digest({'choice_id':choice_id,'selection_key':selection_key,'knowledge_base_id':selected_kb_id})
            db.execute('INSERT INTO choice_selection_refs VALUES(?,?,?,?)',(selection_key,choice_id,envelope_hash,now))
            db.execute('''UPDATE choice_sessions SET state='selected',revision=revision+1,selection_key=?,selection_ref=?,
                selection_envelope=?,selected_kb_id=?,selected_authorization=?,selection_token=?,child_key=?,updated_at=? WHERE choice_id=?''',
                (selection_key,selection_ref,_json(envelope),selected_kb_id,_json(selected_authorization),token,child_key,now,choice_id))
            self._event(db,choice_id,'selection_reserved',{'knowledge_base_id':selected_kb_id,'child_key':child_key},now)
            if semantic is not None:
                self._event(db,choice_id,'selection_semantic_recorded',{
                    'plan_id':semantic['plan_id'],'selection_ref':selection_ref,
                    'frozen_ui_digest':semantic['frozen_ui_digest'],
                    'catalog_revision':semantic['catalog_snapshot']['revision'],
                    'selected_grant_id':semantic['grant_id']},now)
            return self._row(db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(choice_id,)).fetchone(),private=True)

    def claim_handoff(self,choice_id,*,run_id,lease_seconds=30):
        _text(run_id,128)
        if type(lease_seconds) is not int or not 1<=lease_seconds<=300: raise LibraryChoiceError('invalid_choice_handoff_lease')
        with self._db(True) as db:
            row=db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(_text(choice_id,128),)).fetchone()
            if row is None: raise LibraryChoiceError('choice_not_found')
            now=self._now()
            if row['state']=='handed_off': return None
            if row['state'] not in {'selected','handoff_claimed'}: return None
            if row['state']=='handoff_claimed' and row['handoff_lease_until'] is not None and row['handoff_lease_until']>now: return None
            token=secrets.token_hex(24)
            db.execute("UPDATE choice_sessions SET state='handoff_claimed',revision=revision+1,handoff_token=?,handoff_owner=?,handoff_lease_until=?,updated_at=? WHERE choice_id=?",
                       (token,run_id,now+lease_seconds,now,choice_id))
            self._event(db,choice_id,'handoff_claimed',{'run_id':run_id},now)
            return self._row(db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(choice_id,)).fetchone(),private=True)

    def finish_handoff(self,choice_id,token,child_task_id):
        _text(child_task_id,128)
        with self._db(True) as db:
            row=db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(_text(choice_id,128),)).fetchone()
            if row is None: raise LibraryChoiceError('choice_not_found')
            if row['state']=='handed_off':
                if row['child_task_id']!=child_task_id: raise LibraryChoiceError('choice_child_conflict')
                return self._row(row)
            if (row['state']!='handoff_claimed' or not isinstance(token,str) or not row['handoff_token']
                    or not secrets.compare_digest(token,row['handoff_token']) or row['handoff_lease_until']<=self._now()):
                raise LibraryChoiceError('choice_handoff_stale')
            now=self._now()
            db.execute("UPDATE choice_sessions SET state='handed_off',revision=revision+1,child_task_id=?,handoff_lease_until=NULL,updated_at=? WHERE choice_id=?",(child_task_id,now,choice_id))
            self._event(db,choice_id,'child_linked',{'child_task_id':child_task_id},now)
            return self._row(db.execute('SELECT * FROM choice_sessions WHERE choice_id=?',(choice_id,)).fetchone())

    def semantic_selection_record(self,choice_id):
        """Read an existing interpretation link; never re-run a model."""
        with self._db() as db:
            rows=db.execute("SELECT metadata FROM choice_events WHERE choice_id=? AND event='selection_semantic_recorded' ORDER BY id LIMIT 2",
                (_text(choice_id,128),)).fetchall()
        if len(rows)>1:raise LibraryChoiceError('choice_semantic_record_ambiguous')
        return json.loads(rows[0]['metadata']) if rows else None

    def list_pending_handoffs(self,limit=100):
        if type(limit) is not int or not 1<=limit<=256: raise LibraryChoiceError('invalid_choice_limit')
        with self._db() as db:
            rows=db.execute("SELECT * FROM choice_sessions WHERE state IN ('selected','handoff_claimed') ORDER BY created_at,choice_id LIMIT ?",(limit,)).fetchall()
            return [self._row(row) for row in rows]

    def list_observed_selections(self,receiver_profile_url,limit=24,instance_id=None,sender=None,video_id=None):
        receiver=public_profile(receiver_profile_url)
        sender=public_profile(sender) if sender is not None else None
        if type(limit) is not int or not 1<=limit<=100:raise LibraryChoiceError('invalid_choice_limit')
        if instance_id is not None:_text(instance_id,128)
        if video_id is not None and (not isinstance(video_id,str) or not re.fullmatch(r'[0-9]{5,30}',video_id)):
            raise LibraryChoiceError('invalid_choice_video')
        with self._db() as db:
            rows=db.execute("SELECT * FROM choice_sessions WHERE state IN ('selected','handoff_claimed','handed_off') ORDER BY updated_at DESC,choice_id").fetchall()
            result=[]
            for row in rows:
                binding=json.loads(row['root_binding']);envelope=json.loads(row['selection_envelope'])
                if (binding['receiver_profile_url']!=receiver or instance_id is not None and binding['instance_id']!=instance_id
                        or sender is not None and binding['sender_profile_url']!=sender
                        or video_id is not None and envelope['video_id']!=video_id):continue
                result.append(self._row(row))
                if len(result)>=limit:break
            return result
