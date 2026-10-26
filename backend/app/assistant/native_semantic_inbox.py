"""Default-off native meaning queue. It has no model or action capability."""
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

from app.storage_guard import guarded_sqlite_file,verified_readonly_connection,assert_no_orphan_sqlite_files,StorageGuardError
from .native_events import canonical_profile
from .native_transport import decimal_id


class NativeSemanticInboxError(RuntimeError):
    def __init__(self,code='native_semantic_inbox_unavailable'):
        self.code=code;super().__init__(code)


def _dump(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def _digest(v):return hashlib.sha256(_dump(v).encode()).hexdigest()
_BASE={'namespace','source_kind','native_notice_id','native_comment_id','notice_type','recipient_uid',
    'sender_uid','sender_sec_uid','sender_profile_url','receiver_profile_url','video_id','content','created_time',
    'parent_id_hint','relation_status','root_comment_id','parent_comment_id','dispatchable','at_sub_type'}
_IMMUTABLE=('namespace','recipient_uid','sender_uid','sender_sec_uid','sender_profile_url','receiver_profile_url',
            'video_id','native_comment_id','content','notice_type','at_sub_type')


def native_message_source_key(message):
    """Address only: accepts an augmented UI candidate, never proves its facts."""
    try:
        if type(message) is not dict or message.get('namespace')!='douyin_web_v1':raise ValueError()
        values=[]
        for key in ('recipient_uid','video_id','native_comment_id'):
            value=message[key]
            if type(value) is not str or decimal_id(value)!=value:raise ValueError()
            values.append(value)
        return 'native:'+_digest(['douyin_web_v1',*values])
    except Exception:raise NativeSemanticInboxError('native_semantic_message_invalid') from None


def validate_native_message(message):
    try:
        if type(message) is not dict:raise ValueError()
        value=deepcopy(message)
        if value.get('source_kind')=='native_visibility_review_hint':
            from .native_visibility_hint_store import validate_review_hint
            value=validate_review_hint(value)
        elif value.get('source_kind')!='native_notice_candidate' or set(value)!=_BASE:raise ValueError()
        if (value['namespace']!='douyin_web_v1' or type(value['notice_type']) is not int or value['notice_type']!=45
                or type(value['at_sub_type']) is not int or value['at_sub_type']!=2 or value['dispatchable'] is not False
                or value['relation_status']!='unknown'
                or any(value[k] is not None for k in ('parent_id_hint','parent_comment_id','root_comment_id'))):raise ValueError()
        for key in ('native_notice_id','native_comment_id','recipient_uid','sender_uid','video_id'):
            if type(value[key]) is not str or decimal_id(value[key])!=value[key]:raise ValueError()
        if (value['sender_uid']==value['recipient_uid'] or canonical_profile(value['sender_sec_uid'])!=value['sender_profile_url']
                or type(value['receiver_profile_url']) is not str
                or canonical_profile(value['receiver_profile_url'].rsplit('/',1)[-1])!=value['receiver_profile_url']
                or value['sender_profile_url']==value['receiver_profile_url']):raise ValueError()
        text=value['content']
        if type(text) is not str or not text.strip() or len(text)>4000 or any(ord(c)<32 and c not in '\r\n\t' for c in text):raise ValueError()
        created=value['created_time']
        if created is not None and (type(created) is not str or decimal_id(created,zero=True)!=created or int(created)>=10**12):raise ValueError()
        return value
    except Exception:raise NativeSemanticInboxError('native_semantic_message_invalid') from None


def native_message_digest(message):
    value=validate_native_message(message)
    # Notice IDs, observation timestamps and changing API visibility are not
    # a second user command. Keep the complete first message separately.
    return _digest({key:value[key] for key in _IMMUTABLE})


class NativeSemanticInbox:
    def __init__(self,path,receiver_uid,receiver_profile_url,*,create=False,clock=None,source_authorizer=None,protected_paths=()):
        self.path=Path(path).absolute();self.marker=self.path.with_suffix(self.path.suffix+'.owner.json')
        self.receiver_uid=decimal_id(receiver_uid);self.receiver_profile_url=receiver_profile_url
        self._clock=clock or time.time;self.source_authorizer=source_authorizer;self._enabled=False;self._fence=None
        protected={Path(p).resolve() for p in protected_paths}
        if (self.path.resolve()!=self.path or self.marker.resolve()!=self.marker or self.path in protected or self.marker in protected
                or canonical_profile(receiver_profile_url.rsplit('/',1)[-1])!=receiver_profile_url
                or source_authorizer is not None and not callable(source_authorizer)):
            raise NativeSemanticInboxError('native_semantic_inbox_path_invalid')
        if create and not self.path.exists() and not self.marker.exists():
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with guarded_sqlite_file(self.path):
                try:assert_no_orphan_sqlite_files(self.path)
                except StorageGuardError:raise NativeSemanticInboxError('native_semantic_inbox_path_invalid') from None
                self._owner=uuid.uuid4().hex
                fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600);created=os.fstat(fd);os.close(fd)
                if (created.st_dev,created.st_ino)!=(self.path.stat().st_dev,self.path.stat().st_ino):raise NativeSemanticInboxError('native_semantic_inbox_path_invalid')
                db=sqlite3.connect(self.path)
                try:
                    db.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                        CREATE TABLE messages(source_key TEXT PRIMARY KEY,immutable_hash TEXT NOT NULL,message TEXT NOT NULL,
                            state TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,claim_owner TEXT,
                            claim_token TEXT,lease_until REAL,attempts INTEGER NOT NULL DEFAULT 0,plan_id TEXT,reason_code TEXT);
                        CREATE TABLE observations(source_key TEXT NOT NULL,notice_id TEXT NOT NULL,page_fingerprint TEXT NOT NULL,
                            observed_at REAL NOT NULL,PRIMARY KEY(source_key,notice_id));
                        CREATE TABLE audit(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,at REAL NOT NULL,facts TEXT NOT NULL);''')
                    db.executemany('INSERT INTO meta VALUES(?,?)',[('owner',self._owner),('component','douyinmind.native-semantic-inbox'),
                        ('version','1'),('receiver_uid',self.receiver_uid),('receiver_profile_url',self.receiver_profile_url)]);db.commit()
                finally:db.close()
                fd=os.open(self.marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w',encoding='utf-8') as out:
                    out.write(_dump({'owner':self._owner,'component':'douyinmind.native-semantic-inbox','version':1}));out.flush();os.fsync(out.fileno())
        else:self._owner=self._read_marker()
        with self._connection():pass

    @classmethod
    def prepare(cls,path,receiver_uid,receiver_profile_url,**kwargs):return cls(path,receiver_uid,receiver_profile_url,create=True,**kwargs)

    def _regular(self,path):
        if path.resolve()!=path or not path.is_file() or path.stat().st_nlink!=1:raise NativeSemanticInboxError('native_semantic_inbox_path_invalid')
    def _read_marker(self):
        try:
            self._regular(self.marker)
            if self.marker.stat().st_size>2048:raise ValueError()
            value=json.loads(self.marker.read_text(encoding='utf-8'))
            if set(value)!={'owner','component','version'} or value['component']!='douyinmind.native-semantic-inbox' or type(value['version']) is not int or value['version']!=1 or type(value['owner']) is not str or not re.fullmatch('[a-f0-9]{32}',value['owner']):raise ValueError()
            return value['owner']
        except Exception:raise NativeSemanticInboxError('native_semantic_inbox_owner_unavailable') from None
    def _guard(self):
        self._regular(self.path)
        if self._read_marker()!=self._owner:raise NativeSemanticInboxError('native_semantic_inbox_owner_unavailable')
        for suffix in ('-journal','-wal','-shm'):
            path=Path(str(self.path)+suffix)
            if path.exists() or path.is_symlink():self._regular(path)
    def _owned(self,db):
        try:return dict(db.execute('SELECT key,value FROM meta'))=={'owner':self._owner,'component':'douyinmind.native-semantic-inbox','version':'1','receiver_uid':self.receiver_uid,'receiver_profile_url':self.receiver_profile_url}
        except sqlite3.Error:return False
    @contextmanager
    def _connection(self,*,write=False):
        try:
            with guarded_sqlite_file(self.path):
                self._guard();before=self.path.stat()
                def same():
                    self._guard();after=self.path.stat()
                    if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):raise NativeSemanticInboxError('native_semantic_inbox_changed')
                with verified_readonly_connection(self.path,self._owned,guard=self._guard) as ro:
                    same()
                    if not write:ro.row_factory=sqlite3.Row;ro.execute('PRAGMA query_only=ON');yield ro;same();return
                same();db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=5);db.row_factory=sqlite3.Row
                try:
                    same()
                    if not self._owned(db):raise NativeSemanticInboxError('native_semantic_inbox_owner_unavailable')
                    db.execute('BEGIN IMMEDIATE');yield db;same();db.commit()
                except BaseException:db.rollback();raise
                finally:db.close()
        except (StorageGuardError,sqlite3.Error):raise NativeSemanticInboxError('native_semantic_inbox_unavailable') from None
    def _now(self):
        value=self._clock()
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise NativeSemanticInboxError('native_semantic_clock_invalid')
        return float(value)

    def enable_new_messages(self):
        with self._connection(write=True) as db:
            rows=db.execute("SELECT at,facts FROM audit WHERE kind='activation'").fetchall()
            if rows:
                if len(rows)!=1:raise NativeSemanticInboxError('native_semantic_activation_invalid')
                value=json.loads(rows[0]['facts']);fence=value.get('activation_fence')
                if type(fence) not in (int,float) or not math.isfinite(fence) or fence<=0 or fence!=rows[0]['at']:
                    raise NativeSemanticInboxError('native_semantic_activation_invalid')
            else:
                fence=self._now();db.execute('INSERT INTO audit(kind,at,facts) VALUES(?,?,?)',('activation',fence,_dump({'activation_fence':fence})))
        self._enabled=True;self._fence=float(fence)
        return {'enabled':True,'activation_fence':self._fence,'created':not rows,'dispatchable':False}

    def _authorized(self,message):
        try:return callable(self.source_authorizer) and self.source_authorizer(deepcopy(message)) is True
        except Exception:return False

    def record_messages(self,messages,*,page_fingerprint):
        if type(messages) is not list or len(messages)>16 or type(page_fingerprint) is not str or not re.fullmatch('[a-f0-9]{64}',page_fingerprint):
            raise NativeSemanticInboxError('native_semantic_message_invalid')
        clean=[validate_native_message(v) for v in messages];now=self._now();keys=[]
        with self._connection(write=True) as db:
            for message in clean:
                if message['recipient_uid']!=self.receiver_uid or message['receiver_profile_url']!=self.receiver_profile_url:
                    raise NativeSemanticInboxError('native_semantic_scope_changed')
                key=native_message_source_key(message);digest=native_message_digest(message)
                old=db.execute('SELECT immutable_hash FROM messages WHERE source_key=?',(key,)).fetchone()
                if old is None:
                    fresh=(self._enabled and message['created_time'] is not None and int(message['created_time'])>self._fence)
                    state='pending' if fresh and self._authorized(message) else 'historical'
                    reason=None if state=='pending' else 'native_semantic_not_activated_or_unverified'
                    db.execute('INSERT INTO messages(source_key,immutable_hash,message,state,first_seen,last_seen,reason_code) VALUES(?,?,?,?,?,?,?)',
                        (key,digest,_dump(message),state,now,now,reason))
                elif old['immutable_hash']!=digest:
                    db.execute("UPDATE messages SET state='held',reason_code='native_semantic_payload_conflict',last_seen=? WHERE source_key=?",(now,key))
                else:db.execute('UPDATE messages SET last_seen=? WHERE source_key=?',(now,key))
                db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?)',(key,message['native_notice_id'],page_fingerprint,now));keys.append(key)
        return keys

    @staticmethod
    def _view(row):
        if row is None:return None
        return {k:row[k] for k in ('source_key','state','claim_token','lease_until','attempts','plan_id','reason_code','first_seen','last_seen')}|{'message':json.loads(row['message']),'facts_digest':row['immutable_hash'],'dispatchable':False}
    def lookup(self,source_key):
        with self._connection() as db:return self._view(db.execute('SELECT * FROM messages WHERE source_key=?',(source_key,)).fetchone())
    def list_pending(self,limit=16):
        if type(limit) is not int or not 1<=limit<=16:raise NativeSemanticInboxError('native_semantic_limit_invalid')
        with self._connection() as db:return [self._view(r) for r in db.execute("SELECT * FROM messages WHERE state='pending' ORDER BY first_seen,source_key LIMIT ?",(limit,))]
    @staticmethod
    def _expire(db,now):db.execute("UPDATE messages SET state='held',reason_code='native_semantic_claim_expired' WHERE state='claimed' AND lease_until<?",(now,))
    def is_processing(self):
        with self._connection(write=True) as db:
            self._expire(db,self._now())
            return db.execute("SELECT 1 FROM messages WHERE state IN ('pending','claimed') LIMIT 1").fetchone() is not None
    def claim_next(self,owner,lease_seconds=300):
        if type(owner) is not str or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',owner) or type(lease_seconds) not in (int,float) or not 1<=lease_seconds<=300:
            raise NativeSemanticInboxError('native_semantic_claim_invalid')
        now=self._now()
        with self._connection(write=True) as db:
            self._expire(db,now)
            row=db.execute("SELECT * FROM messages WHERE state='pending' ORDER BY first_seen,source_key LIMIT 1").fetchone()
            if row is None:return None
            message=json.loads(row['message'])
            if not self._authorized(message):
                db.execute("UPDATE messages SET state='held',reason_code='native_semantic_scope_changed' WHERE source_key=?",(row['source_key'],));return None
            token=uuid.uuid4().hex
            db.execute("UPDATE messages SET state='claimed',claim_owner=?,claim_token=?,lease_until=?,attempts=1 WHERE source_key=? AND state='pending' AND attempts=0",(owner,token,now+lease_seconds,row['source_key']))
            return self._view(db.execute('SELECT * FROM messages WHERE source_key=?',(row['source_key'],)).fetchone())
    def finish(self,source_key,claim_token,receipt):
        from .semantic_plan_store import decision_metadata
        value=decision_metadata(receipt)
        with self._connection(write=True) as db:
            row=db.execute('SELECT * FROM messages WHERE source_key=?',(source_key,)).fetchone()
            if (row is None or row['state']!='claimed' or row['claim_token']!=claim_token or row['lease_until']<self._now()
                    or value.get('mode')!='native' or value['source_key']!=source_key or value.get('native_facts_digest')!=row['immutable_hash']):
                raise NativeSemanticInboxError('native_semantic_finish_unconfirmed')
            db.execute("UPDATE messages SET state='understood',plan_id=?,reason_code=NULL WHERE source_key=?",(value['plan_id'],source_key))
        return self.lookup(source_key)
    def hold(self,source_key,claim_token,code='native_semantic_plan_unconfirmed'):
        if type(code) is not str or not re.fullmatch('[a-z_]{1,80}',code):raise NativeSemanticInboxError('native_semantic_hold_invalid')
        with self._connection(write=True) as db:
            row=db.execute('SELECT * FROM messages WHERE source_key=?',(source_key,)).fetchone()
            if row is None or row['state']!='claimed' or row['claim_token']!=claim_token:raise NativeSemanticInboxError('native_semantic_claim_changed')
            db.execute("UPDATE messages SET state='held',reason_code=? WHERE source_key=?",(code,source_key))
