"""Read-only meaning first; actual UI, source and permissions before execution."""
from .commands import CommandResolver
from .semantic_resolution import (catalog_snapshot_from_grants,owner_scope_from_envelope,semantic_source_key)
from .semantic_plan_store import SemanticPlanError,ui_semantic_digest
from .native_lifecycle import NativeSessionLifecycle
from .native_source_store import NativeSourceStore


class SemanticIntentInterpreter:
    def __init__(self,registry,store,model,budget,*,should_stop=lambda:False):
        self.registry,self.store,self.model,self.budget=registry,store,model,budget
        self.should_stop=should_stop
        self.resolver=CommandResolver(registry)
        self.native_inbox=None
        self.native_source_authorizer=None
        self.native_source_store=None
        self.native_hint_store=None
        self.native_session_lifecycle=None

    def configure_native(self,inbox,source_authorizer,*,source_store=None,hint_store=None,session_lifecycle=None):
        if not callable(source_authorizer):raise SemanticPlanError('semantic_scope_unavailable')
        if session_lifecycle is not None and (type(session_lifecycle) is not NativeSessionLifecycle
                or type(source_store) is not NativeSourceStore
                or session_lifecycle.expected_uid != inbox.receiver_uid
                or source_store.receiver_uid != inbox.receiver_uid
                or source_store.receiver_profile_url != inbox.receiver_profile_url):
            raise SemanticPlanError('semantic_scope_unavailable')
        self.native_inbox=inbox
        self.native_source_authorizer=source_authorizer
        self.native_source_store=source_store
        self.native_hint_store=hint_store
        self.native_session_lifecycle=session_lifecycle

    def native_intake_ready(self):
        if self.native_inbox is None:return True
        if self.native_inbox.is_processing():return False
        # A Source commit and the separate meaning inbox are deliberately
        # not atomic. A failed capture must not spend a UI claim in that gap.
        candidate=None
        if self.native_source_store is not None:
            candidate=self.native_source_store.peek_ui_candidate()
        if candidate is None and self.native_hint_store is not None:
            pending=self.native_hint_store.list_hints('pending',limit=1)
            if pending:candidate=pending[0]['hint']
        if candidate is not None and candidate['source_kind'] in {'native_notice_candidate','native_visibility_review_hint'}:
            from .native_semantic_inbox import native_message_source_key
            row=self.native_inbox.lookup(native_message_source_key(candidate))
            return row is not None and row['state'] not in {'pending','claimed'}
        return True

    def native_authority_current(self,task):
        """A later native payload conflict revokes effects, not old fee records."""
        if self.native_inbox is None:return True
        try:
            receipt=self.store.get_recorded_decision(task['envelope'],binding=task.get('binding'))
            if receipt is None:
                if task['envelope'].get('evidence',{}).get('source_kind')=='conversation_followup':
                    return False
                # Losing a new semantic UI view must not reactivate the old
                # keyword resolver. Legacy tasks with no meaning source retain
                # their original path; fee-only records grant no execution.
                if self.native_source_store is not None:
                    source=self.native_source_store.lookup_source_for_task(task['id'])
                    if source is not None:
                        row=self.native_inbox.lookup(source['source_id'])
                        if row is not None and row['state']!='historical':return False
                return True
            from .semantic_plan_store import decision_metadata
            value=decision_metadata(receipt)
            if value.get('origin_mode')!='native':return True
            row=self.native_inbox.lookup(value['source_key'])
            return (row is not None and row['state']=='understood'
                and row['facts_digest']==value['native_facts_digest']
                and row['plan_id']==value['plan_id'])
        except Exception:return False

    def process_native_once(self,owner='semantic-tools-worker'):
        """Tools thread only: never calls a browser, RAG, import or publisher."""
        if self.native_inbox is None or self.should_stop():return None
        if self.native_session_lifecycle is None:
            claim=self.native_inbox.claim_next(owner,lease_seconds=300)
        else:
            with self.native_session_lifecycle.native_meaning_claim_window() as can_claim:
                if can_claim is not True:return None
                claim=self.native_inbox.claim_next(owner,lease_seconds=300)
        if claim is None:return None
        message=claim['message'];key=claim['source_key'];token=claim['claim_token']
        try:
            context=self._context(message)
            if context is None:raise SemanticPlanError('semantic_scope_unavailable')
            user,device,grants=context
            scope={k:message[k] for k in ('sender_profile_url','receiver_profile_url','video_id')}
            scope.update(user_id=user['user_id'],device_id=device['device_id'],instance_id=device['instance_id'],
                reference_id=key,root_reference_id=key)
            catalog=catalog_snapshot_from_grants(grants)
            def authorized():
                if self.should_stop() or self.native_source_authorizer(message) is not True:return False
                current=self._context(message)
                if current is None:return False
                u,d,g=current
                row=self.native_inbox.lookup(key)
                return (row is not None and row['state']=='claimed' and row['claim_token']==token
                    and row['message']==message and u['user_id']==scope['user_id']
                    and d['device_id']==scope['device_id'] and d['instance_id']==scope['instance_id']
                    and catalog_snapshot_from_grants(g)==catalog)
            from .native_semantic_inbox import native_message_digest
            receipt=self.store.run_once(source_key=key,frozen_ui_digest=native_message_digest(message),
                owner_scope=scope,catalog_snapshot=catalog,mode='native',original_text=message['content'],
                native_facts=message,model=self.model,budget=self.budget,
                scope_authorizer=authorized,should_stop=self.should_stop)
            if not authorized():raise SemanticPlanError('semantic_scope_unavailable')
            self.native_inbox.finish(key,token,receipt)
            return {'state':'understood','source_key':key}
        except Exception as error:
            code=getattr(error,'code','semantic_request_unknown')
            self.native_inbox.hold(key,token,code=code)
            return {'state':'held','source_key':key,'reason_code':code}

    def native_candidate_action(self,candidate):
        """Meaning may select a lane; it cannot grant permission to execute."""
        if self.native_inbox is None:return None
        try:
            from .native_semantic_inbox import native_message_source_key
            key=native_message_source_key(candidate)
            row=self.native_inbox.lookup(key)
            if row is None or row['state']!='understood':return None
            receipt=self.store.get_native_decision(key)
            if receipt is None:return None
            return receipt.decision['action']
        except Exception:return None

    def _context(self,envelope):
        snapshot=self.registry.snapshot()
        users=[u for u in snapshot['users'] if u['sender_profile_url']==envelope['sender_profile_url']
            and u['receiver_profile_url']==envelope['receiver_profile_url']]
        if len(users)!=1:return None
        user=users[0]
        if user.get('active') is not True or user.get('verification_status')!='verified':return None
        devices=[d for d in user['devices'] if d.get('enabled') is True and not d.get('revoked')
            and d.get('verification_status')=='verified' and d.get('is_local') is True]
        if user.get('default_device_id'):
            devices=[d for d in devices if d['device_id']==user['default_device_id']]
        if len(devices)!=1:return None
        device=devices[0]
        carrier=self.registry.intake_binding(envelope['sender_profile_url'],envelope['receiver_profile_url'])
        if carrier is None or carrier.public_reply_allowed is not True:return None
        grants=[g for g in device['libraries'] if g.get('active') and g.get('library_exists')]
        return user,device,grants

    def __call__(self,envelope,*,preferred_binding_id=None,source_checkpoint=None,native_candidate=None):
        """No model-selected Binding/UID/URL is accepted by this interface."""
        native_accounting=None
        try:
            context=self._context(envelope)
            if context is None:raise SemanticPlanError('semantic_scope_unavailable')
            user,device,grants=context
            owner=owner_scope_from_envelope(envelope,user,device)
            catalog=catalog_snapshot_from_grants(grants)
            def authorized():
                if self.should_stop():return False
                if source_checkpoint is not None and (not callable(source_checkpoint) or source_checkpoint() is not True):return False
                current=self._context(envelope)
                if current is None:return False
                u,d,g=current
                return (owner_scope_from_envelope(envelope,u,d)==owner
                    and catalog_snapshot_from_grants(g)==catalog)
            if self.native_inbox is not None and native_candidate is not None:
                from .native_semantic_inbox import native_message_source_key
                key=native_message_source_key(native_candidate)
                row=self.native_inbox.lookup(key)
                if row is not None:native_accounting=(key,row['message'],owner,authorized)
                if row is None or row['state']!='understood':
                    raise SemanticPlanError('semantic_plan_unconfirmed')
                receipt=self.store.get_native_decision(key)
                if receipt is None:raise SemanticPlanError('semantic_plan_unconfirmed')
                receipt=self.store.bind_native_to_ui(receipt,envelope,owner_scope=owner,
                    catalog_snapshot=catalog,source_facts=row['message'],scope_authorizer=authorized,
                    should_stop=self.should_stop)
            else:
                receipt=self.store.run_once(source_key=semantic_source_key(envelope),
                    frozen_ui_digest=ui_semantic_digest(envelope),owner_scope=owner,catalog_snapshot=catalog,
                    mode='root',original_text=envelope['comment_text'],model=self.model,budget=self.budget,
                    scope_authorizer=authorized,should_stop=self.should_stop)
            if not authorized():raise SemanticPlanError('semantic_scope_unavailable')
            return self.resolver.resolve_verified_intent(envelope,receipt,preferred_binding_id=preferred_binding_id)
        except Exception as error:
            if self.should_stop():return None
            if native_accounting is not None:
                key,message,current_owner,current_authorizer=native_accounting
                try:
                    # A paid but unusable interpretation is still a cost.
                    # This view carries no typed decision or execution right.
                    self.store.bind_native_accounting_to_ui(key,envelope,owner_scope=current_owner,
                        source_facts=message,scope_authorizer=current_authorizer,should_stop=self.should_stop)
                except Exception:
                    pass  # Global provider accounting is retained independently.
            return self.resolver.resolve_semantic_failure(envelope,getattr(error,'code',None))

    def recorded_resolution(self,task):
        """Existing durable roots may predate this feature; never re-call a model."""
        if not self.native_authority_current(task):
            return self.resolver.resolve_semantic_failure(task['envelope'],'semantic_scope_unavailable')
        receipt=self.store.get_recorded_decision(task['envelope'],binding=task.get('binding'),queue_task_id=task.get('id'))
        if receipt is None:
            if task['envelope'].get('evidence',{}).get('source_kind')=='conversation_followup':
                return self.resolver.resolve_semantic_failure(task['envelope'],'semantic_scope_unavailable')
            return self.resolver.resolve(task['envelope'])
        return self.resolver.resolve_verified_intent(task['envelope'],receipt)
