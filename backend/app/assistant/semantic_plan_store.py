"""Owned once-only semantic planning. A decision is meaning, never permission.

No production entrypoint imports/constructs this component automatically.
Original public comments are stored for exact source matching; model messages,
reasoning, provider errors and credentials are never persisted. Source claims,
actual UI proof and current Registry authorization remain the caller's gates.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from app.storage_guard import (guarded_sqlite_file, verified_readonly_connection,
    assert_no_orphan_sqlite_files, StorageGuardError)
from app.mention_agent.contracts import Binding, public_profile
from .semantic_intent_model import INTENT_TOOLS, intent_messages, parse_intent_turn, SemanticIntentError
from .usage_budget import UsageBudgetExceeded


class SemanticPlanError(RuntimeError):
    def __init__(self, code='semantic_plan_unavailable'):
        self.code = code
        super().__init__(code)


_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z', re.ASCII)
_HEX = re.compile(r'[a-f0-9]{64}\Z', re.ASCII)
_OWNER = {'user_id','device_id','instance_id','sender_profile_url','receiver_profile_url',
          'video_id','root_reference_id','reference_id'}
_BRAND = object()
_SCHEMA = {
    'semantic_meta': 'CREATE TABLE semantic_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
    'semantic_plans': '''CREATE TABLE semantic_plans(
        plan_id TEXT PRIMARY KEY,source_key TEXT UNIQUE NOT NULL,immutable_hash TEXT NOT NULL,
        ui_digest TEXT NOT NULL,owner_scope TEXT NOT NULL,catalog TEXT NOT NULL,mode TEXT NOT NULL,
        original_text TEXT NOT NULL,budget_scope_id TEXT NOT NULL,request_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL,claim_token TEXT NOT NULL,decision TEXT,usage TEXT NOT NULL,
        reason_code TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,
        native_facts TEXT,native_facts_digest TEXT)''',
    'semantic_agent_links': '''CREATE TABLE semantic_agent_links(
        agent_task_id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,budget_scope_id TEXT NOT NULL,
        envelope_digest TEXT NOT NULL,binding_digest TEXT NOT NULL,created_at REAL NOT NULL)''',
    'semantic_ui_bindings': '''CREATE TABLE semantic_ui_bindings(
        plan_id TEXT PRIMARY KEY,ui_digest TEXT NOT NULL,owner_scope TEXT NOT NULL,
        catalog TEXT NOT NULL,mode TEXT NOT NULL,executable INTEGER NOT NULL,
        created_at REAL NOT NULL,updated_at REAL NOT NULL)''',
    'semantic_budget_parents': '''CREATE TABLE semantic_budget_parents(
        plan_id TEXT PRIMARY KEY,parent_plan_id TEXT NOT NULL,
        budget_scope_id TEXT NOT NULL,created_at REAL NOT NULL)''',
}


def _dump(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _digest(value): return hashlib.sha256(_dump(value).encode('utf-8')).hexdigest()
def _normal(value): return re.sub(r'\s+','',value)


def _text(value, maximum=4000):
    if (type(value) is not str or not value.strip() or len(value)>maximum
            or any(ord(c)<32 and c not in '\r\n\t' for c in value)):
        raise SemanticPlanError('semantic_input_invalid')
    return value


def _key(value):
    if type(value) is not str or not _KEY.fullmatch(value):raise SemanticPlanError('semantic_input_invalid')
    return value


def _owner(value):
    if type(value) is not dict or set(value)!=_OWNER:raise SemanticPlanError('semantic_owner_invalid')
    result=deepcopy(value)
    for key in ('user_id','device_id','instance_id','root_reference_id','reference_id'):_key(result[key])
    for key in ('sender_profile_url','receiver_profile_url'):
        try:
            if public_profile(result[key])!=result[key]:raise ValueError()
        except (ValueError,TypeError):raise SemanticPlanError('semantic_owner_invalid') from None
    if result['sender_profile_url']==result['receiver_profile_url'] or type(result['video_id']) is not str or not re.fullmatch(r'[0-9]{5,30}',result['video_id']):
        raise SemanticPlanError('semantic_owner_invalid')
    return result


def _catalog(value):
    if type(value) is not dict or set(value)!={'revision','options'}:raise SemanticPlanError('semantic_catalog_invalid')
    _key(value['revision'])
    if type(value['options']) is not list or len(value['options'])>128:raise SemanticPlanError('semantic_catalog_invalid')
    result=deepcopy(value);ids=set();ordinals=set()
    for item in result['options']:
        if type(item) is not dict or set(item)!={'grant_id','name','aliases','can_query','can_import','ordinal'}:
            raise SemanticPlanError('semantic_catalog_invalid')
        _key(item['grant_id']);_text(item['name'],128)
        if item['grant_id'] in ids or type(item['ordinal']) is not int or not 1<=item['ordinal']<=128 or item['ordinal'] in ordinals:
            raise SemanticPlanError('semantic_catalog_invalid')
        ids.add(item['grant_id']);ordinals.add(item['ordinal'])
        if type(item['aliases']) is not list or len(item['aliases'])>16:raise SemanticPlanError('semantic_catalog_invalid')
        for alias in item['aliases']:_text(alias,128)
        if type(item['can_query']) is not bool or type(item['can_import']) is not bool:raise SemanticPlanError('semantic_catalog_invalid')
    return result


def budget_scope_for(owner_scope):
    scope=_owner(owner_scope)
    return 'command-'+_digest({key:scope[key] for key in ('user_id','device_id','instance_id',
        'sender_profile_url','receiver_profile_url','video_id','root_reference_id')})


def ui_semantic_digest(envelope):
    """Stable actual UI identity, not a proof builder or authorization check."""
    if type(envelope) is not dict:raise SemanticPlanError('semantic_input_invalid')
    value=deepcopy(envelope);value.pop('command_id',None)
    if type(value.get('comment_text')) is str:value['comment_text']=_normal(value['comment_text'])
    if type(value.get('evidence')) is dict:
        value['evidence'].pop('observed_at_utc',None);value['evidence'].pop('diagnostics',None)
    return _digest(value)


def _binding_dict(value):
    if isinstance(value,Binding):return value.to_dict()
    if type(value) is dict:return Binding.from_dict(value).to_dict()
    raise SemanticPlanError('semantic_binding_invalid')


class RecordedSemanticDecision:
    """Private issuer-backed receipt; serialized dictionaries are not receipts."""
    __slots__=('_store','_plan_id','_immutable','_view')
    def __init__(self,store,plan_id,immutable,*,_brand=None,_view='origin'):
        if _brand is not _BRAND:raise SemanticPlanError('semantic_recorded_decision_required')
        self._store,self._plan_id,self._immutable,self._view=store,plan_id,immutable,_view
    @property
    def plan_id(self):return self._plan_id
    @property
    def receipt_id(self):return self._plan_id
    @property
    def budget_scope_id(self):return decision_metadata(self)['budget_scope_id']
    @property
    def request_key(self):return decision_metadata(self)['request_key']
    @property
    def source_key(self):return decision_metadata(self)['source_key']
    @property
    def usage(self):return decision_metadata(self)['usage']
    @property
    def decision(self):return {k:decision_metadata(self)[k] for k in ('action','target_state','grant_id')}
    @property
    def created_at(self):return decision_metadata(self)['created_at']
    @property
    def updated_at(self):return decision_metadata(self)['updated_at']


def decision_metadata(receipt):
    if type(receipt) is not RecordedSemanticDecision:raise SemanticPlanError('semantic_recorded_decision_required')
    return receipt._store._receipt_metadata(receipt)


def verify_semantic_decision(receipt,*,mode,original_text,grant_id=None,frozen_ui_digest=None,owner_scope=None,catalog_snapshot=None):
    value=decision_metadata(receipt)
    if (value['mode']!=mode or value['original_text_sha256']!=hashlib.sha256(_normal(_text(original_text)).encode()).hexdigest()
            or grant_id is not None and value['grant_id']!=grant_id
            or frozen_ui_digest is not None and value['frozen_ui_digest']!=frozen_ui_digest
            or owner_scope is not None and value['owner_scope']!=_owner(owner_scope)
            or catalog_snapshot is not None and value['catalog_snapshot']!=_catalog(catalog_snapshot)):
        raise SemanticPlanError('semantic_decision_scope_changed')
    return value


class SemanticPlanStore:
    def __init__(self,path,*,create=False,clock=None,protected_paths=()):
        self.path=Path(path).absolute();self._clock=clock or time.time
        self.marker=self.path.with_suffix(self.path.suffix+'.owner.json')
        protected={Path(p).resolve() for p in protected_paths}
        if self.path.resolve()!=self.path or self.marker.resolve()!=self.marker or self.path in protected or self.marker in protected:
            raise SemanticPlanError('semantic_store_path_invalid')
        if create and not self.path.exists() and not self.marker.exists():
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with guarded_sqlite_file(self.path):
                try:assert_no_orphan_sqlite_files(self.path)
                except StorageGuardError:raise SemanticPlanError('semantic_store_path_invalid') from None
                self._owner=uuid.uuid4().hex
                fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
                created=os.fstat(fd);os.close(fd)
                if (created.st_dev,created.st_ino)!=(self.path.stat().st_dev,self.path.stat().st_ino):
                    raise SemanticPlanError('semantic_store_path_invalid')
                db=sqlite3.connect(self.path)
                try:
                    db.execute('PRAGMA synchronous=FULL')
                    for sql in _SCHEMA.values():db.execute(sql)
                    db.executemany('INSERT INTO semantic_meta VALUES(?,?)',[('owner',self._owner),('component','douyinmind.semantic-plan'),('version','1')]);db.commit()
                finally:db.close()
                fd=os.open(self.marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(fd,'w',encoding='utf-8') as out:
                    out.write(_dump({'component':'douyinmind.semantic-plan','version':1,'owner':self._owner}));out.flush();os.fsync(out.fileno())
        else:self._owner=self._read_marker()
        with self._connection():pass

    @classmethod
    def prepare(cls,path,**kwargs):return cls(path,create=True,**kwargs)

    def _regular(self,path):
        if path.resolve()!=path or not path.is_file() or path.stat().st_nlink!=1:
            raise SemanticPlanError('semantic_store_path_invalid')

    def _read_marker(self):
        try:
            self._regular(self.marker)
            if self.marker.stat().st_size>2048:raise ValueError()
            def pairs(items):
                result={}
                for key,item in items:
                    if key in result:raise ValueError()
                    result[key]=item
                return result
            value=json.loads(self.marker.read_text(encoding='utf-8'),object_pairs_hook=pairs,
                parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            if type(value) is not dict or set(value)!={'component','version','owner'} or value['component']!='douyinmind.semantic-plan' or type(value['version']) is not int or value['version']!=1 or type(value['owner']) is not str or not re.fullmatch('[a-f0-9]{32}',value['owner']):raise ValueError()
            return value['owner']
        except Exception:raise SemanticPlanError('semantic_store_owner_unavailable') from None

    def _guard(self):
        self._regular(self.path)
        if self._read_marker()!=self._owner:raise SemanticPlanError('semantic_store_owner_unavailable')
        for suffix in ('-journal','-wal','-shm'):
            path=Path(str(self.path)+suffix)
            if path.exists() or path.is_symlink():self._regular(path)

    def _owned(self,db):
        try:
            if dict(db.execute('SELECT key,value FROM semantic_meta'))!={'owner':self._owner,'component':'douyinmind.semantic-plan','version':'1'}:return False
            sqls=dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            normalize=lambda value:' '.join(value.lower().split())
            return set(sqls)==set(_SCHEMA) and all(normalize(sqls[k])==normalize(v) for k,v in _SCHEMA.items())
        except sqlite3.Error:return False

    @contextmanager
    def _connection(self,*,write=False):
        try:
            with guarded_sqlite_file(self.path):
                self._guard();before=self.path.stat()
                def same():
                    self._guard();after=self.path.stat()
                    if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):raise SemanticPlanError('semantic_store_changed')
                with verified_readonly_connection(self.path,self._owned,guard=self._guard) as ro:
                    same()
                    if not write:
                        ro.row_factory=sqlite3.Row;ro.execute('PRAGMA query_only=ON');yield ro;same();return
                same();db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=5);db.row_factory=sqlite3.Row
                try:
                    same()
                    if not self._owned(db):raise SemanticPlanError('semantic_store_owner_unavailable')
                    db.execute('BEGIN IMMEDIATE');yield db;same();db.commit()
                except BaseException:db.rollback();raise
                finally:db.close()
        except (StorageGuardError,sqlite3.Error):raise SemanticPlanError('semantic_store_unavailable') from None

    def _now(self):
        value=self._clock()
        if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise SemanticPlanError('semantic_clock_invalid')
        return float(value)

    @staticmethod
    def _gate(scope_authorizer,should_stop):
        try:
            if not callable(scope_authorizer) or not callable(should_stop) or should_stop() is not False or scope_authorizer() is not True:
                raise ValueError()
        except Exception:raise SemanticPlanError('semantic_scope_unavailable') from None

    def _finish(self,plan_id,token,state,reason=None,decision=None,usage=None):
        with self._connection(write=True) as db:
            row=db.execute('SELECT state,claim_token FROM semantic_plans WHERE plan_id=?',(plan_id,)).fetchone()
            if row is None or row['state']!='reserved' or row['claim_token']!=token:raise SemanticPlanError('semantic_claim_changed')
            db.execute('UPDATE semantic_plans SET state=?,reason_code=?,decision=?,usage=?,updated_at=? WHERE plan_id=?',
                (state,reason,_dump(decision) if decision is not None else None,_dump(usage or {}),self._now(),plan_id))

    def run_once(self,*,source_key,frozen_ui_digest,owner_scope,catalog_snapshot,mode,original_text,model,budget,scope_authorizer,should_stop,native_facts=None,budget_parent_receipt=None):
        source_key=_key(source_key);owner=_owner(owner_scope);catalog=_catalog(catalog_snapshot);text=_text(original_text)
        if type(frozen_ui_digest) is not str or not _HEX.fullmatch(frozen_ui_digest) or mode not in ('root','choice','native'):
            raise SemanticPlanError('semantic_input_invalid')
        if mode=='root' and owner['root_reference_id']!=owner['reference_id']:raise SemanticPlanError('semantic_owner_invalid')
        facts_digest=None;facts=None
        if mode=='native':
            from .native_semantic_inbox import validate_native_message,native_message_source_key,native_message_digest
            facts=validate_native_message(native_facts);facts_digest=native_message_digest(facts)
            if (source_key!=native_message_source_key(facts) or facts_digest!=frozen_ui_digest
                    or owner['root_reference_id']!=source_key or owner['reference_id']!=source_key
                    or any(owner[k]!=facts[k] for k in ('sender_profile_url','receiver_profile_url','video_id'))
                    or text!=facts['content']):raise SemanticPlanError('semantic_native_scope_changed')
        elif native_facts is not None:raise SemanticPlanError('semantic_input_invalid')
        parent=None;scope=budget_scope_for(owner)
        if budget_parent_receipt is not None:
            if mode!='choice' or type(budget_parent_receipt) is not RecordedSemanticDecision or budget_parent_receipt._store is not self:
                raise SemanticPlanError('semantic_budget_parent_invalid')
            parent=decision_metadata(budget_parent_receipt)
            po=parent['owner_scope']
            if (parent['mode']!='root' or any(po[k]!=owner[k] for k in ('user_id','device_id','instance_id','sender_profile_url','receiver_profile_url','video_id'))
                    or po['reference_id']!=owner['root_reference_id'] or po['root_reference_id']!=po['reference_id']):
                raise SemanticPlanError('semantic_budget_parent_invalid')
            scope=parent['budget_scope_id']
        identity={'source':source_key,'ui':frozen_ui_digest,'owner':owner,'catalog':catalog,'mode':mode,'text':_normal(text)}
        if parent is not None:identity['budget_parent']={'plan_id':parent['plan_id'],'budget_scope_id':scope}
        immutable=_digest(identity)
        pid='semantic-'+_digest(['semantic-plan-v1',source_key]);request='semantic-request-'+pid[9:]
        self._gate(scope_authorizer,should_stop)
        token=uuid.uuid4().hex;now=self._now()
        with self._connection(write=True) as db:
            old=db.execute('SELECT * FROM semantic_plans WHERE source_key=?',(source_key,)).fetchone()
            if old:
                if old['immutable_hash']!=immutable:raise SemanticPlanError('semantic_plan_scope_conflict')
                if old['state']=='complete':return RecordedSemanticDecision(self,old['plan_id'],immutable,_brand=_BRAND)
                raise SemanticPlanError(old['reason_code'] or 'semantic_plan_unconfirmed')
            db.execute('INSERT INTO semantic_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (pid,source_key,immutable,frozen_ui_digest,_dump(owner),_dump(catalog),mode,text,scope,request,'reserved',token,None,'{}',None,now,now,
                 _dump(facts) if facts is not None else None,facts_digest))
            if parent is not None:
                db.execute('INSERT INTO semantic_budget_parents VALUES(?,?,?,?)',(pid,parent['plan_id'],scope,now))
        reserved=False;known_usage={}
        try:
            self._gate(scope_authorizer,should_stop)
            messages=intent_messages(mode,text,catalog);tools=deepcopy(INTENT_TOOLS)
            estimate=len(_dump({'messages':messages,'tools':tools}).encode())+1500+2048
            try:
                reservation=budget.reserve(scope,request,estimate)
            except UsageBudgetExceeded:
                self._finish(pid,token,'budget_blocked','model_budget_exceeded')
                raise SemanticPlanError('model_budget_exceeded') from None
            reserved=True
            if reservation.get('may_request') is not True:raise SemanticPlanError('semantic_plan_unconfirmed')
            self._gate(scope_authorizer,should_stop)
            turn=model.complete(deepcopy(messages),deepcopy(tools))
            settled=budget.settle(request,turn.usage)
            if settled.get('state')!='settled':raise SemanticPlanError('model_budget_unknown')
            usage={'total_tokens':settled['total_tokens']}
            for output,key in (('prompt_tokens','input_tokens'),('completion_tokens','output_tokens')):
                if settled.get(key) is not None:usage[output]=settled[key]
            known_usage=deepcopy(usage)
            try:decision=parse_intent_turn(turn,catalog)
            except SemanticIntentError:
                self._finish(pid,token,'held','semantic_response_invalid',usage=usage)
                raise SemanticPlanError('semantic_response_invalid') from None
            self._gate(scope_authorizer,should_stop)
            self._finish(pid,token,'complete',decision=decision,usage=usage)
            return RecordedSemanticDecision(self,pid,immutable,_brand=_BRAND)
        except BaseException as error:
            code=error.code if isinstance(error,SemanticPlanError) else 'semantic_request_unknown'
            if code not in {'model_budget_exceeded','semantic_response_invalid'}:
                if reserved:
                    try:budget.mark_unknown(request,reason='request_interrupted')
                    except Exception:pass
                try:self._finish(pid,token,'unknown',code if code in {'semantic_scope_unavailable','model_budget_unknown','semantic_plan_unconfirmed'} else 'semantic_request_unknown',usage=known_usage)
                except Exception:pass
            if isinstance(error,(KeyboardInterrupt,SystemExit)):raise
            raise SemanticPlanError(code if code in {'model_budget_exceeded','model_budget_unknown','semantic_scope_unavailable','semantic_response_invalid','semantic_plan_unconfirmed'} else 'semantic_request_unknown') from None

    def _receipt_metadata(self,receipt):
        if type(receipt) is not RecordedSemanticDecision or receipt._store is not self:raise SemanticPlanError('semantic_recorded_decision_required')
        with self._connection() as db:
            row=db.execute('SELECT * FROM semantic_plans WHERE plan_id=?',(receipt._plan_id,)).fetchone()
            bound=db.execute('SELECT * FROM semantic_ui_bindings WHERE plan_id=?',(receipt._plan_id,)).fetchone() if receipt._view=='bound' else None
        if row is None or row['state']!='complete' or row['immutable_hash']!=receipt._immutable:raise SemanticPlanError('semantic_recorded_decision_unavailable')
        value=json.loads(row['decision']);owner=json.loads(row['owner_scope']);catalog=json.loads(row['catalog'])
        mode=row['mode'];ui=row['ui_digest']
        if receipt._view=='bound':
            if bound is None or bound['executable']!=1 or row['mode']!='native':raise SemanticPlanError('semantic_recorded_decision_unavailable')
            owner=json.loads(bound['owner_scope']);catalog=json.loads(bound['catalog']);mode=bound['mode'];ui=bound['ui_digest']
        result={'receipt_id':row['plan_id'],'plan_id':row['plan_id'],'source_key':row['source_key'],
            'frozen_ui_digest':ui,'mode':mode,'owner_scope':owner,'catalog_snapshot':catalog,
            'budget_scope_id':row['budget_scope_id'],'request_key':row['request_key'],**value,'usage':json.loads(row['usage']),
            'created_at':row['created_at'],'updated_at':row['updated_at'],
            'original_text_sha256':hashlib.sha256(_normal(row['original_text']).encode()).hexdigest()}
        if row['mode']=='native':result.update(origin_mode='native',native_facts_digest=row['native_facts_digest'],origin_owner_scope=json.loads(row['owner_scope']))
        return result

    def lookup_receipt(self,source_key):
        with self._connection() as db:row=db.execute('SELECT * FROM semantic_plans WHERE source_key=?',(_key(source_key),)).fetchone()
        if row is None:return None
        return {key:row[key] for key in ('plan_id','source_key','state','budget_scope_id','request_key','reason_code','created_at','updated_at')}|{'usage':json.loads(row['usage']),'dispatchable':False}

    def get_recorded_decision(self,envelope,*,binding=None,queue_task_id=None):
        if type(envelope) is not dict:return None
        digest=ui_semantic_digest(envelope);matches=[]
        with self._connection() as db:
            rows=db.execute("SELECT * FROM semantic_plans WHERE state='complete' AND mode!='native' AND ui_digest=?",(digest,)).fetchall()
            bound_rows=db.execute("SELECT p.*,b.owner_scope AS bound_owner FROM semantic_plans p JOIN semantic_ui_bindings b ON b.plan_id=p.plan_id WHERE p.state='complete' AND p.mode='native' AND b.executable=1 AND b.ui_digest=?",(digest,)).fetchall()
        for row in bound_rows:
            owner=json.loads(row['bound_owner'])
            if self._envelope_owner_matches(envelope,owner,binding):matches.append((row,'bound'))
        for row in rows:
            owner=json.loads(row['owner_scope'])
            if (any(envelope.get(key)!=owner[key] for key in ('sender_profile_url','receiver_profile_url','video_id','reference_id'))
                    or type(envelope.get('comment_text')) is not str or _normal(envelope['comment_text'])!=_normal(row['original_text'])):continue
            if binding is not None:
                b=_binding_dict(binding)
                if b['instance_id']!=owner['instance_id'] or any(b[k]!=owner[k] for k in ('sender_profile_url','receiver_profile_url')):continue
            if queue_task_id is not None:_key(queue_task_id)
            matches.append((row,'origin'))
        if len(matches)>1:raise SemanticPlanError('semantic_decision_ambiguous')
        return RecordedSemanticDecision(self,matches[0][0]['plan_id'],matches[0][0]['immutable_hash'],_brand=_BRAND,_view=matches[0][1]) if matches else None

    trusted_budget_receipt=get_recorded_decision

    @staticmethod
    def _envelope_owner_matches(envelope,owner,binding=None):
        if (type(envelope) is not dict or any(envelope.get(k)!=owner[k] for k in
                ('sender_profile_url','receiver_profile_url','video_id','reference_id'))):return False
        if binding is not None:
            bound=_binding_dict(binding)
            if bound['instance_id']!=owner['instance_id'] or any(bound[k]!=owner[k] for k in ('sender_profile_url','receiver_profile_url')):return False
        return True

    def get_native_decision(self,source_key):
        with self._connection() as db:row=db.execute("SELECT * FROM semantic_plans WHERE source_key=? AND mode='native' AND state='complete'",(_key(source_key),)).fetchone()
        return RecordedSemanticDecision(self,row['plan_id'],row['immutable_hash'],_brand=_BRAND) if row else None

    def _native_ui_context(self,source_key,envelope,owner_scope,source_facts,scope_authorizer,should_stop):
        from .native_semantic_inbox import validate_native_message,native_message_source_key,native_message_digest
        self._gate(scope_authorizer,should_stop)
        facts=validate_native_message(source_facts);owner=_owner(owner_scope)
        with self._connection() as db:row=db.execute("SELECT * FROM semantic_plans WHERE source_key=? AND mode='native'",(_key(source_key),)).fetchone()
        if row is None:raise SemanticPlanError('semantic_native_origin_unavailable')
        original=json.loads(row['owner_scope'])
        if (native_message_source_key(facts)!=source_key or native_message_digest(facts)!=row['native_facts_digest']
                or any(owner[k]!=original[k] for k in ('user_id','device_id','instance_id','sender_profile_url','receiver_profile_url','video_id'))
                or owner['root_reference_id']!=owner['reference_id']
                or not self._envelope_owner_matches(envelope,owner)
                or envelope.get('source_mode')!='live_ui'
                or envelope.get('native_comment_id') not in (None,facts['native_comment_id'])
                or type(envelope.get('comment_text')) is not str or _normal(envelope['comment_text'])!=_normal(facts['content'])):
            raise SemanticPlanError('semantic_native_ui_scope_changed')
        proof=envelope.get('evidence',{})
        try:
            observed=datetime.fromisoformat(proof['observed_at_utc'].replace('Z','+00:00'))
            if (observed.tzinfo is None or not -5<=self._now()-observed.timestamp()<=120
                    or proof.get('source_kind') is not None
                    or any(proof.get(k) is not True for k in ('real_mention','sender_verified','video_verified'))
                    or type(proof.get('unique_count')) is not int or proof['unique_count']!=1):raise ValueError()
        except Exception:raise SemanticPlanError('semantic_native_ui_unverified') from None
        self._gate(scope_authorizer,should_stop)
        return row,owner

    def _save_native_ui_view(self,row,envelope,owner,catalog,*,executable):
        digest=ui_semantic_digest(envelope);now=self._now()
        with self._connection(write=True) as db:
            current=db.execute('SELECT * FROM semantic_plans WHERE plan_id=?',(row['plan_id'],)).fetchone()
            if current is None or current['immutable_hash']!=row['immutable_hash'] or current['state']!=row['state']:
                raise SemanticPlanError('semantic_native_origin_changed')
            old=db.execute('SELECT * FROM semantic_ui_bindings WHERE plan_id=?',(row['plan_id'],)).fetchone()
            if old:
                if (old['ui_digest']!=digest or json.loads(old['owner_scope'])!=owner or old['mode']!='root'
                        or executable and json.loads(old['catalog'])!=catalog):
                    raise SemanticPlanError('semantic_native_ui_binding_conflict')
                # A noncomplete origin cannot gain an executable view. A
                # complete meaning may move from fee-only to actual UI proof.
                if executable and old['executable']==0:
                    db.execute('UPDATE semantic_ui_bindings SET executable=1,updated_at=? WHERE plan_id=?',(now,row['plan_id']))
            else:
                db.execute('INSERT INTO semantic_ui_bindings VALUES(?,?,?,?,?,?,?,?)',
                    (row['plan_id'],digest,_dump(owner),_dump(catalog),'root',int(executable),now,now))

    def bind_native_to_ui(self,receipt,envelope,*,owner_scope,catalog_snapshot,source_facts,scope_authorizer,should_stop):
        value=decision_metadata(receipt)
        if receipt._store is not self or value.get('mode')!='native':raise SemanticPlanError('semantic_native_origin_required')
        row,owner=self._native_ui_context(value['source_key'],envelope,owner_scope,source_facts,scope_authorizer,should_stop)
        catalog=_catalog(catalog_snapshot)
        if row['state']!='complete' or json.loads(row['catalog'])!=catalog:raise SemanticPlanError('semantic_native_catalog_changed')
        self._save_native_ui_view(row,envelope,owner,catalog,executable=True)
        return RecordedSemanticDecision(self,row['plan_id'],row['immutable_hash'],_brand=_BRAND,_view='bound')

    def bind_native_accounting(self,source_key,envelope,*,owner_scope,source_facts,scope_authorizer,should_stop,binding=None):
        row,owner=self._native_ui_context(source_key,envelope,owner_scope,source_facts,scope_authorizer,should_stop)
        self._save_native_ui_view(row,envelope,owner,json.loads(row['catalog']),executable=False)
        return self.get_recorded_accounting(envelope,binding=binding,owner_scope=owner,source_key=source_key)

    bind_native_accounting_to_ui=bind_native_accounting

    def get_recorded_accounting(self,envelope,*,binding=None,queue_task_id=None,source_key=None,owner_scope=None):
        """Exact fee provenance only; never returns an action/typed decision."""
        if type(envelope) is not dict:return None
        digest=ui_semantic_digest(envelope);matches=[]
        with self._connection() as db:
            rows=db.execute("SELECT * FROM semantic_plans WHERE mode!='native' AND ui_digest=?",(digest,)).fetchall()
            native=db.execute('SELECT p.*,b.owner_scope AS bound_owner FROM semantic_plans p JOIN semantic_ui_bindings b ON b.plan_id=p.plan_id WHERE p.mode=\'native\' AND b.ui_digest=?',(digest,)).fetchall()
        for row in rows+native:
            owner=json.loads(row['bound_owner'] if 'bound_owner' in row.keys() else row['owner_scope'])
            if source_key is not None and row['source_key']!=source_key:continue
            if owner_scope is not None and owner!=_owner(owner_scope):continue
            if not self._envelope_owner_matches(envelope,owner,binding):continue
            if type(envelope.get('comment_text')) is not str or _normal(envelope['comment_text'])!=_normal(row['original_text']):continue
            matches.append((row,owner))
        if len(matches)>1:raise SemanticPlanError('semantic_accounting_ambiguous')
        if not matches:
            if rows or native:raise SemanticPlanError('semantic_accounting_scope_changed')
            return None
        row,owner=matches[0]
        return {k:row[k] for k in ('plan_id','source_key','budget_scope_id','request_key','state','reason_code','created_at','updated_at')}|{
            'request_keys':[row['request_key']],'owner_scope':owner,'usage':json.loads(row['usage']),'dispatchable':False}

    def bind_agent_budget(self,receipt,agent_task_id,envelope,binding):
        metadata=decision_metadata(receipt)
        if receipt._store is not self:raise SemanticPlanError('semantic_recorded_decision_required')
        checked=self.get_recorded_decision(envelope,binding=binding)
        if checked is None or checked.plan_id!=receipt.plan_id:raise SemanticPlanError('semantic_decision_scope_changed')
        task=_key(agent_task_id);ed=ui_semantic_digest(envelope);bd=_digest(_binding_dict(binding))
        expected=(receipt.plan_id,metadata['budget_scope_id'],ed,bd)
        with self._connection(write=True) as db:
            old=db.execute('SELECT * FROM semantic_agent_links WHERE agent_task_id=?',(task,)).fetchone()
            if old:
                if tuple(old[k] for k in ('plan_id','budget_scope_id','envelope_digest','binding_digest'))!=expected:raise SemanticPlanError('semantic_agent_budget_conflict')
                return True
            db.execute('INSERT INTO semantic_agent_links VALUES(?,?,?,?,?,?)',(task,*expected,self._now()))
        return True

    def agent_budget_scope(self,agent_task_id,*,envelope=None,binding=None,for_accounting=False):
        """Read a mapped budget scope, never an execution authorization.

        The trusted historical-usage reconciler may supply for_accounting=True
        only while checking an ALREADY recorded request. It can account the
        original active binding after deactivation; it cannot reserve a new
        request or relax any other identity/target/public-scope difference.
        Normal execution callers must leave this flag false.
        """
        if type(for_accounting) is not bool:raise SemanticPlanError('semantic_agent_budget_conflict')
        with self._connection() as db:row=db.execute('SELECT * FROM semantic_agent_links WHERE agent_task_id=?',(_key(agent_task_id),)).fetchone()
        if row is None:return None
        if envelope is None or binding is None:raise SemanticPlanError('semantic_agent_budget_conflict')
        compared=_binding_dict(binding)
        if compared['active'] is False:
            if not for_accounting:raise SemanticPlanError('semantic_agent_budget_conflict')
            # Only True -> False is tolerated: the restored dictionary must
            # equal the originally linked digest in every other field.
            compared['active']=True
        if ui_semantic_digest(envelope)!=row['envelope_digest'] or _digest(compared)!=row['binding_digest']:
            raise SemanticPlanError('semantic_agent_budget_conflict')
        receipt=self.get_recorded_decision(envelope,binding=binding)
        if receipt is None or receipt.plan_id!=row['plan_id'] or receipt.budget_scope_id!=row['budget_scope_id']:
            raise SemanticPlanError('semantic_agent_budget_conflict')
        return row['budget_scope_id']
