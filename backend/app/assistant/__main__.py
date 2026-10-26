"""Launch the registered daily assistant, reusing its own normal B browser."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import threading
import time

from .control import AssistantControl
from .integration import STATE, STORAGE, RECEIVER_STATE, AGENT_PATH, registry_if_configured, start_guard
from .queue import AssistantQueue, utc
from .background_mode import product_browser_headless
from .native_configuration import build_intake_lane


def receive_bounded_batch(runtime, lane, admission_lock, should_stop):
    """At most three fresh details, each committed before opening the next."""
    admitted_tasks=[]
    try:
        for _ in range(3):
            if should_stop():
                break
            with admission_lock:
                if should_stop():
                    break
                admitted=runtime.receive_once()
                lane.admitted(admitted)
                admitted_tasks.extend(admitted)
            if not getattr(lane,'has_buffered_notices',False):
                break
        return admitted_tasks
    finally:
        # Never retain a hidden fourth candidate for rapid draining. Failure
        # clears only transient state; already admitted records remain durable.
        clear=getattr(lane,'clear_buffer',None)
        if clear is not None:
            clear()


def signal_native_ui_wakeup(event, outcome):
    """Internal worker completion may wake the owner, never authorize effects."""
    if isinstance(outcome, dict) and outcome.get('state') == 'understood':
        event.set()


def native_ui_receipt_current(interpreter, candidate):
    """RO private native receipt/current owner check for a scheduling hint."""
    try:
        from .native_semantic_inbox import native_message_source_key
        from .semantic_plan_store import decision_metadata
        from .semantic_resolution import catalog_snapshot_from_grants
        key = native_message_source_key(candidate)
        row = interpreter.native_inbox.lookup(key)
        if row is None or row['state'] != 'understood':
            return False
        receipt = interpreter.store.get_native_decision(key)
        if receipt is None:
            return False
        meta = decision_metadata(receipt)
        message = row['message']
        fields = ('namespace','recipient_uid','sender_uid','sender_sec_uid','sender_profile_url',
                  'receiver_profile_url','video_id','native_comment_id','content','notice_type','at_sub_type')
        if (any(candidate.get(k) != message.get(k) for k in fields)
                or meta.get('mode') != 'native' or meta.get('source_key') != key
                or meta.get('plan_id') != row.get('plan_id')
                or meta.get('native_facts_digest') != row.get('facts_digest')
                or meta.get('action') not in {'query','save','clarify','cancel'}
                or interpreter.native_source_authorizer(message) is not True):
            return False
        context = interpreter._context(message)
        if context is None:
            return False
        user, device, grants = context
        owner = {k: message[k] for k in ('sender_profile_url','receiver_profile_url','video_id')}
        owner.update(user_id=user['user_id'],device_id=device['device_id'],instance_id=device['instance_id'],
                     reference_id=key,root_reference_id=key)
        return (meta['owner_scope'] == owner and meta['catalog_snapshot'] == catalog_snapshot_from_grants(grants)
                and not interpreter.should_stop())
    except Exception:
        return False


def receive_native_ui_wakeup(event, runtime, lane, admission_lock, should_stop):
    """Consume a lossy hint without polling the API or marking capture fresh."""
    if not event.is_set():
        return []
    event.clear()
    ready = getattr(lane, 'ready_persisted_ui_source', None)
    if should_stop() or not callable(ready) or ready() is not True:
        return []
    # All durable once-claim/source/Registry/UI gates remain in this same path.
    return receive_bounded_batch(runtime, lane, admission_lock, should_stop)


def worker_health_state(control_snapshot, counts):
    if control_snapshot.get('desired_state')!='running':
        return 'idle'
    return 'processing' if counts.get('queued',0) or counts.get('waiting_ingestion',0) else 'idle'


def manual_needs_owner(server):
    return bool(server is not None and (server.session_active or server.owner_finalization_pending))


def intervention_health(error):
    from .native_lane import NativeSessionPaused
    if isinstance(error, NativeSessionPaused):
        from .native_lifecycle import project_native_session_status
        try:
            status = project_native_session_status(error.native_session_status)
            if status['state'] != 'paused' or status['reason_code'] != error.native_failure_code:
                raise ValueError('native_pause_status_changed')
        except Exception:
            return 'page_unavailable','native_failure_unconfirmed'
        reason = status['reason_code']
        if getattr(error,'native_intake_data_code',None) in {
                'invalid_native_visibility','invalid_notice_page','invalid_notice_schema','invalid_notice_type',
                'invalid_notice_time','unknown_comment_type','group_schema_unknown'}:
            return 'page_unavailable','native_notice_format_unavailable'
        if reason == 'api_error':
            return 'page_unavailable','native_api_error'
        if reason == 'verification_required':
            return 'verification_required','native_verification_required'
        if reason in {'session_invalid','session_expired','authorization_expired','session_cookie_expired'}:
            return 'login_required','native_session_invalid'
        if reason == 'session_identity_mismatch':
            return 'login_required','native_identity_mismatch'
        if reason in {'invalid_native_visibility','invalid_notice_page','invalid_notice_schema','invalid_notice_type',
                      'invalid_notice_time','unknown_comment_type','group_schema_unknown'}:
            return 'page_unavailable','native_notice_format_unavailable'
        return 'page_unavailable','native_session_needs_review'
    return ('login_required' if '登录' in str(error) else 'verification_required','human_intervention_required')


def receiver_page_health(error):
    """Keep a current typed native cooldown distinct from a DOM failure.

    This changes reporting only: the existing capture delay, source pause and
    all UI/identity/send gates remain unchanged. Old status attached to a real
    browser exception cannot reclassify that exception as native transport.
    """
    from .native_lane import NativeLaneError
    from .native_lifecycle import project_native_session_status
    if isinstance(error,NativeLaneError) and error.code=='native_session_cooldown':
        try:
            status=project_native_session_status(getattr(error,'native_session_status',None))
        except Exception:
            status=None
        if status is not None and status['state']=='cooldown':
            return 'page_unavailable','native_session_cooldown'
    return 'page_unavailable','page_not_ready'


def record_manual_verification(control, run_id, server):
    status=server.safe_status() if server is not None else {'state':'unavailable'}
    value={'available':server is not None,'url':'http://127.0.0.1:8767/' if server is not None else None,
           'state':status.get('state','unavailable'),
           'active':manual_needs_owner(server)}
    def update(current):
        if current.get('run_id')!=run_id:raise ValueError('runtime_owner_changed')
        previous=current.get('manual_verification') or {}
        if any(previous.get(key)!=item for key,item in value.items()):
            current['manual_verification']={**value,'observed_at':utc()}
        return current
    control.queue.update_control(update)


def pump_manual_verification(server, owner, control, run_id, admission_lock, hold):
    """Human commands exclusively own the page; never run an ordinary job."""
    if server is None or not (hold.is_set() or manual_needs_owner(server)):return False
    epoch=server.finalization_epoch
    hold.set()
    # Drop a concurrent Start; the tool worker's independent hold also fences
    # effects when an already-loaded older web API accepts the request.
    control.pause();control.heartbeat(run_id)
    with admission_lock:server.process_one(owner)
    record_manual_verification(control,run_id,server)
    if not server.session_active:
        control.pause();control.heartbeat(run_id)
        server.acknowledge_finalization(epoch)
        if not manual_needs_owner(server):hold.clear()
        record_manual_verification(control,run_id,server)
    return True


def run_registered(queue, control, *, poll_seconds=45, publish_gap_seconds=30):
    from playwright.sync_api import sync_playwright, Error as BrowserError
    from app.core.config import settings
    from app.mention_agent.service import configured_database
    from app.mention_receiver.__main__ import configuration, preflight_state, receiver_lock
    from app.mention_receiver.browser import (NormalPageReceiver, ReceiverInterventionRequired,
        ReceiverPageError, ReceiverDetailError, browser_options, SELF_URL)
    from .conversation import ConversationStore
    from .legacy_bridge import LegacyBridge
    from .runtime import AssistantRuntime

    snapshot=control.snapshot()
    if snapshot['state']!='starting' or not snapshot.get('run_id'):
        raise ValueError('registered_launch_required')
    run_id=snapshot['run_id']
    database=configured_database()
    registry=registry_if_configured(queue.path.parent,database)
    if registry is None:
        raise ValueError('assistant_setup_required')
    config=configuration(RECEIVER_STATE)
    protected=Path(settings.playwright_user_data_dir)
    protected=protected if protected.is_absolute() else Path(__file__).resolve().parents[2]/protected
    profile=preflight_state(RECEIVER_STATE,protected,database)
    admission_lock=threading.RLock()
    stop_event=threading.Event()
    worker=None
    manual_server=None
    manual_hold=threading.Event()
    browser_intervention=threading.Event()
    worker_inflight=threading.Event()
    owner_review=threading.Event()
    reason='normal_stop'
    with receiver_lock(RECEIVER_STATE):
        from .usage_budget import UsageBudget
        usage_budget = UsageBudget.prepare(queue.path.parent/'usage-budget.sqlite3')
        from .library_choice import LibraryChoiceStore
        conversation = ConversationStore(queue.path.parent/'conversation.sqlite3', protected_paths=(database, AGENT_PATH))
        conversation.prepare_scope_v2()
        choice_store = LibraryChoiceStore.prepare(queue.path.parent/'library-choice.sqlite3',
            protected_paths=(database, AGENT_PATH, queue.path, conversation.path,
                             queue.path.parent/'registry.sqlite3', queue.path.parent/'usage-budget.sqlite3'))
        control.register(run_id)
        with sync_playwright() as p:
            headless=product_browser_headless()
            context=p.chromium.launch_persistent_context(str(profile),headless=headless,
                viewport={'width':1100,'height':800},**browser_options(STORAGE))
            try:
                page=context.pages[0] if context.pages else context.new_page()
                adapter=NormalPageReceiver(page,config['receiver_public_number'],config['receiver_profile_url'],
                    open_strategy=config.get('notification_open_strategy','hover'))
                if headless:adapter.readback_viewport_height=1600
                lane=build_intake_lane(adapter,registry,queue,conversation,queue.path.parent)
                lane.run_id = run_id
                should_stop=lambda:stop_event.is_set() or control.should_stop(run_id)
                owner_thread=threading.get_ident()
                page_operations_stop=lambda:should_stop() or manual_needs_owner(manual_server) or ((manual_hold.is_set() or browser_intervention.is_set()) and not (owner_review.is_set() and threading.get_ident()==owner_thread))
                lane.should_stop=page_operations_stop
                adapter.choice_should_stop=page_operations_stop
                from .semantic_configuration import build_semantic_interpreter
                semantic_should_stop=lambda:page_operations_stop() or control.snapshot().get('desired_state')!='running'
                protected_semantic_paths=[database,AGENT_PATH,queue.path,conversation.path,
                    choice_store.path,registry.path,usage_budget.path]
                for source in (getattr(lane,'source_store',None),getattr(lane,'visibility_hint_store',None)):
                    if source is not None:protected_semantic_paths.append(source.path)
                interpreter=build_semantic_interpreter(queue.path.parent,registry,usage_budget,
                    should_stop=semantic_should_stop,protected_paths=protected_semantic_paths)
                semantic_store=interpreter.store if interpreter is not None else None
                semantic_path=queue.path.parent/'semantic-plans.sqlite3'
                if semantic_store is None and semantic_path.exists():
                    # Disabling new understanding does not erase trustworthy
                    # mappings needed to account for earlier model requests.
                    from .semantic_plan_store import SemanticPlanStore
                    semantic_store=SemanticPlanStore(semantic_path,protected_paths=protected_semantic_paths)
                if interpreter is not None:
                    getattr(lane,'browser_lane',lane).semantic_resolution_provider=interpreter
                    native_source=getattr(lane,'source_store',None)
                    native_receiver=getattr(lane,'native_receiver',None)
                    if native_source is not None and native_receiver is not None:
                        from .native_semantic_inbox import NativeSemanticInbox
                        native_inbox=NativeSemanticInbox.prepare(queue.path.parent/'native-semantic-inbox.sqlite3',
                            native_source.receiver_uid,native_source.receiver_profile_url,
                            source_authorizer=native_source.native_meaning_checkpoint,
                            protected_paths=(*protected_semantic_paths,semantic_store.path))
                        native_inbox.enable_new_messages()
                        interpreter.configure_native(native_inbox,native_source.native_meaning_checkpoint,
                            source_store=native_source,hint_store=getattr(lane,'visibility_hint_store',None),
                            session_lifecycle=getattr(lane,'session_lifecycle',None))
                        native_receiver.semantic_message_sink=native_inbox.record_messages
                        lane.semantic_intake_ready=interpreter.native_intake_ready
                        lane.semantic_candidate_action=interpreter.native_candidate_action
                        lane.semantic_ui_source_ready=lambda candidate:native_ui_receipt_current(interpreter,candidate)
                source_authority=getattr(lane,'authority_current',None)
                if interpreter is not None and callable(source_authority):
                    base_source_authority=source_authority
                    source_authority=lambda task:base_source_authority(task) is True and interpreter.native_authority_current(task) is True
                bridge=LegacyBridge(registry,queue,control,agent_path=AGENT_PATH,conversation=conversation,adapter=adapter,
                                    database=database,admission_lock=admission_lock,should_stop=page_operations_stop,
                                    usage_budget=usage_budget, choice_store=choice_store,
                                    source_authorizer=source_authority,semantic_store=semantic_store)
                lane.choice_controller = bridge.choices
                if interpreter is not None and getattr(lane, 'source_store', None) is not None:
                    from .thread_followup import ThreadFollowupCoordinator
                    lane.followup_coordinator = ThreadFollowupCoordinator(lane.browser_lane, bridge)
                if interpreter is not None:
                    def plan_choice(**arguments):
                        authority=arguments.pop('scope_authorizer')
                        previous_stop=arguments.pop('should_stop')
                        owner=arguments['owner_scope']
                        root=queue.lookup_by_source_reference(owner['sender_profile_url'],owner['receiver_profile_url'],
                            'live_ui',owner['root_reference_id'])
                        if root is not None:
                            parent=semantic_store.get_recorded_decision(root['envelope'],binding=root['binding'],
                                queue_task_id=root['id'])
                            if parent is not None:
                                arguments['budget_parent_receipt']=parent
                            elif semantic_store.get_recorded_accounting(root['envelope'],binding=root['binding']) is not None:
                                from .semantic_plan_store import SemanticPlanError
                                raise SemanticPlanError('semantic_scope_unavailable')
                        return semantic_store.run_once(**arguments,model=interpreter.model,budget=usage_budget,
                            scope_authorizer=lambda:not semantic_should_stop() and authority(),
                            should_stop=lambda:semantic_should_stop() or previous_stop())
                    bridge.choices.semantic_choice_planner=plan_choice
                    bridge.choices.semantic_choice_receipt_loader=semantic_store.get_recorded_decision
                    bridge.choices.semantic_root_resolution_provider=interpreter.recorded_resolution
                runtime=AssistantRuntime(queue,control,execute=bridge.execute,publish=bridge.publish,
                    reconcile=bridge.reconcile,resume_adapter=bridge.operator_resume,
                    retry_adapter=bridge.operator_retry_query,intake=lane.collect,run_id=run_id,
                    delivery_readback_adapter=bridge.observe_existing_delivery,
                    operator_readback_adapter=bridge.observe_existing_delivery,
                    recorded_readback_adapter=bridge.get_recorded_delivery)
                runtime.recover()
                recover_native=getattr(lane,'recover',None)
                if recover_native is not None:
                    # Only repair already durable queue/source links. A saved
                    # UI observation is never replayed as fresh admission.
                    recover_native()

                from .manual_verification import ManualVerificationServer, ManualVerificationError
                from .manual_verification_owner import ManualVerificationOwner, verify_registered_receiver
                manual_owner=ManualVerificationOwner(adapter,lane,control,queue,run_id,
                    operation_current=lambda:manual_server is not None and manual_server.operation_current(),
                    worker_inflight=worker_inflight.is_set)
                try:
                    manual_server=ManualVerificationServer(run_id,allowed=manual_owner.allowed,
                        process_guard=lambda:control.snapshot().get('run_id')==run_id)
                    manual_server.start()
                except ManualVerificationError as error:
                    if error.code!='manual_port_unavailable':raise
                    manual_server=None
                record_manual_verification(control,run_id,manual_server)
                from .native_recovery_mailbox import NativeRecoveryMailbox
                from .native_recovery_owner import NativeRecoveryOwner
                recovery_mailbox=NativeRecoveryMailbox(queue.path.parent,run_id,control)
                recovery_owner=NativeRecoveryOwner(lane,control,queue,run_id,admission_lock,manual_hold,
                    owner_review,worker_inflight,lambda:manual_needs_owner(manual_server),reply_inspector=bridge.inspect_existing_reply)
                native_ui_wakeup=threading.Event()

                def work():
                    last_health=None
                    def health(state,code=None):
                        nonlocal last_health
                        if last_health!=(state,code):
                            control.set_worker_health(run_id,state,reason_code=code)
                            last_health=(state,code)
                    while not should_stop():
                        try:
                            if manual_hold.is_set() or browser_intervention.is_set() or manual_needs_owner(manual_server):
                                health('idle');stop_event.wait(.2);continue
                            counts=queue.counts()
                            health(worker_health_state(control.snapshot(),counts))
                            with admission_lock:
                                if not (manual_hold.is_set() or browser_intervention.is_set()
                                        or manual_needs_owner(manual_server)):
                                    worker_inflight.set()
                            if worker_inflight.is_set():
                                try:
                                    if interpreter is not None:
                                        meaning=interpreter.process_native_once(run_id)
                                        signal_native_ui_wakeup(native_ui_wakeup,meaning)
                                    runtime.execute_once()
                                    runtime.poll_waiting_once()
                                finally:
                                    worker_inflight.clear()
                        except Exception:
                            # Surface a fixed worker error independently from
                            # the browser's health. Reservations remain intact.
                            try:
                                health('error','tool_scheduler_unavailable')
                            except Exception:
                                pass  # The control DB may itself be unavailable.
                            stop_event.wait(2)
                        stop_event.wait(.5)

                worker=threading.Thread(target=work,name='DouyinMindAssistantTools',daemon=True)
                worker.start()
                next_capture=next_publish=next_heartbeat=next_delivery_readback=0
                intervention=False
                data_intervention=False
                while not should_stop():
                    if page.is_closed():
                        reason='assistant_window_closed'
                        break
                    now=time.monotonic()
                    if now>=next_heartbeat:
                        control.heartbeat(run_id)
                        lane.touch_devices()
                        next_heartbeat=now+5
                    current=control.snapshot()
                    if pump_manual_verification(manual_server,manual_owner,control,run_id,admission_lock,manual_hold):
                        browser_intervention.set();intervention=True
                        page.wait_for_timeout(100)
                        continue
                    recovery_result=recovery_mailbox.process_one(recovery_owner)
                    if recovery_result is not None:
                        if control.snapshot().get('receiver_state') in {'verification_required','login_required'}:
                            browser_intervention.set();intervention=True
                        page.wait_for_timeout(100)
                        continue
                    if intervention:
                        # No navigation/input while verification is pending.
                        # Explicit user Start is required even after the human
                        # completes the page; its old send token never resets.
                        if current['desired_state']=='running':
                            if data_intervention:
                                control.pause()
                                control.set_receiver_health(run_id,'page_unavailable',reason_code='native_notice_format_unavailable')
                                page.wait_for_timeout(250)
                                continue
                            try:
                                adapter.check_intervention()
                                status=verify_registered_receiver(adapter,lane)
                                control.heartbeat(run_id,native_session_status=status)
                            except ReceiverInterventionRequired:
                                control.pause()
                            except Exception:
                                control.pause()
                                control.set_receiver_health(run_id,'verification_required',reason_code='human_identity_unconfirmed')
                            else:
                                intervention=False
                                browser_intervention.clear()
                                next_capture=0
                        page.wait_for_timeout(500)
                        continue
                    try:
                        if page.url=='about:blank':
                            page.goto(SELF_URL,wait_until='domcontentloaded',timeout=20000)
                        adapter.check_intervention()
                        if now>=next_capture:
                            # Interface receipt is separate from the exact
                            # public-page target proof. Poll once per cycle,
                            # then durably admit at most three bounded details.
                            poll_native=getattr(lane,'poll_native',None)
                            native_ready=True
                            if poll_native is not None:
                                observation=poll_native()
                                session_status = observation.get('native_session_status') if isinstance(observation, dict) else None
                                if session_status is not None:
                                    control.heartbeat(run_id, native_session_status=session_status)
                                native_status=observation.get('status') if isinstance(observation,dict) else None
                                if not isinstance(native_status,dict) or type(native_status.get('armed')) is not bool:
                                    raise ValueError('native_poll_status_unconfirmed')
                                native_ready=native_status['armed']
                                from .native_visibility_diagnostics import record_visibility_probe
                                record_visibility_probe(control,run_id,lane)
                            admitted=receive_bounded_batch(runtime,lane,admission_lock,page_operations_stop)
                            if should_stop():
                                break
                            control.record_capture(run_id,admitted_count=len(admitted),
                                baseline_created=getattr(lane,'last_baseline_created',False),
                                unavailable_details=getattr(lane,'last_detail_failure_count',0))
                            control.set_receiver_health(run_id,'ready' if native_ready else 'starting',
                                reason_code=None if native_ready else 'native_history_baseline_in_progress')
                            next_capture=time.monotonic()+poll_seconds
                        elif native_ui_wakeup.is_set():
                            try:
                                receive_native_ui_wakeup(native_ui_wakeup,runtime,lane,admission_lock,page_operations_stop)
                            except ReceiverInterventionRequired:
                                raise
                            except Exception:
                                # No new API capture, lease retry or timer reset.
                                # Unknown read outcomes retain their original ledger.
                                control.set_receiver_health(run_id,'page_unavailable',reason_code='native_ui_wakeup_unconfirmed')
                        runtime.process_action_once()
                        # This lane only reads already attempted public replies.
                        # Unknown send reservations never become publish jobs.
                        if current['desired_state']=='running' and now>=next_delivery_readback:
                            # Back off even if a local ledger/adapter raises;
                            # one old unknown must not starve later publication.
                            next_delivery_readback=time.monotonic()+30
                            readback=runtime.reconcile_delivery_once()
                            next_delivery_readback=time.monotonic()+(120 if readback is not None else 30)
                        # A prepare-only resume can encounter a new challenge;
                        # generic runtime errors must never hide it before a
                        # subsequent navigation or publication.
                        adapter.check_intervention()
                        if now>=next_publish and current['desired_state']=='running':
                            outcome=runtime.publish_once()
                            if outcome is not None:
                                next_publish=time.monotonic()+publish_gap_seconds
                                if (outcome['state'] in {'verification_required','delivery_unknown'}
                                        or outcome.get('reason_code')=='preflight_verification_required'):
                                    control.pause()
                                    control.set_receiver_health(run_id,'verification_required' if outcome['state']=='verification_required'
                                        or outcome.get('reason_code')=='preflight_verification_required'
                                        else 'page_unavailable',reason_code=outcome.get('reason_code') or outcome['state'])
                                    intervention=True
                                    browser_intervention.set()
                    except ReceiverInterventionRequired as error:
                        session_status = getattr(error, 'native_session_status', None)
                        if session_status is not None:
                            control.heartbeat(run_id, native_session_status=session_status)
                        control.pause()
                        health_state,health_code=intervention_health(error)
                        control.set_receiver_health(run_id,health_state,reason_code=health_code)
                        intervention=True
                        data_intervention=health_code=='native_notice_format_unavailable'
                        browser_intervention.set()
                    except (ReceiverPageError,ReceiverDetailError,BrowserError) as error:
                        session_status = getattr(error, 'native_session_status', None)
                        if session_status is not None:
                            control.heartbeat(run_id, native_session_status=session_status)
                        health_state,health_code=receiver_page_health(error)
                        control.set_receiver_health(run_id,health_state,reason_code=health_code)
                        next_capture=time.monotonic()+poll_seconds
                    except Exception as error:
                        session_status = getattr(error, 'native_session_status', None)
                        if session_status is not None:
                            control.heartbeat(run_id, native_session_status=session_status)
                        control.set_receiver_health(run_id,'page_unavailable',reason_code='intake_result_unconfirmed')
                        next_capture=time.monotonic()+poll_seconds
                    page.wait_for_timeout(250)
            finally:
                stop_event.set()
                if manual_server is not None:
                    manual_server.close()
                    try:record_manual_verification(control,run_id,None)
                    except Exception:pass
                if worker is not None:
                    worker.join(timeout=10)
                context.close()
                control.finish(run_id,reason)


def main(argv=None):
    parser=argparse.ArgumentParser(description='DouyinMind 日常抖音助手')
    parser.add_argument('--state-dir',type=Path,default=STATE)
    parser.add_argument('--run-registered',action='store_true',help=argparse.SUPPRESS)
    recovery=parser.add_mutually_exclusive_group()
    recovery.add_argument('--review-native-source',help='仅登记一个旧入队前来源的本机安全审查')
    recovery.add_argument('--resume-native-source',help='仅登记一个有可信中断收据的本机来源恢复审查')
    recovery.add_argument('--review-native-visibility-hint',help='仅登记一个已隔离通知的原网页复核请求')
    recovery.add_argument('--inspect-native-visibility-hint',help='仅只读检查一个已暂停通知的原网页定位，不恢复执行')
    recovery.add_argument('--recover-native-visibility-hint',help='仅登记一次原网页读取失败通知的显式恢复复核')
    recovery.add_argument('--inspect-assistant-reply',help='仅登记已发表任务的B侧原线程只读检查，填写32位任务ID')
    recovery.add_argument('--review-thread-followup',help='明确登记一条从未执行的普通追问重新核对原线程，填写完整native来源号')
    recovery.add_argument('--inspect-thread-followup',help='只读定位一条未处理追问，不理解、不创建任务或发送')
    args=parser.parse_args(argv)
    if args.state_dir.absolute()!=STATE or args.state_dir.resolve()!=STATE:
        parser.error('助手目录由项目固定，不能指定其他文件或数据库。')
    if args.run_registered and (args.review_native_source or args.resume_native_source or args.review_native_visibility_hint or args.inspect_native_visibility_hint or args.recover_native_visibility_hint or args.inspect_assistant_reply or args.review_thread_followup or args.inspect_thread_followup):
        parser.error('后台运行与本机审查请求不能合并。')
    queue=AssistantQueue(STATE/'queue.sqlite3',protected_paths=(STORAGE/'douyinrag.db',STORAGE/'mention_agent',
        RECEIVER_STATE,STORAGE/'chroma',STATE/'registry.sqlite3',STATE/'conversation.sqlite3'))
    control=AssistantControl(queue,STATE)
    initial=control.snapshot()
    launching_run=initial.get('run_id') if args.run_registered and initial.get('state')=='starting' else None
    try:
        start_guard()
        if args.review_native_source or args.resume_native_source or args.review_native_visibility_hint or args.inspect_native_visibility_hint or args.recover_native_visibility_hint or args.inspect_assistant_reply or args.review_thread_followup or args.inspect_thread_followup:
            from .native_recovery_mailbox import NativeRecoveryMailbox
            request=NativeRecoveryMailbox(STATE,initial.get('run_id'),control).submit(
                ('assistant-task:'+args.inspect_assistant_reply) if args.inspect_assistant_reply else
                (args.review_native_source or args.resume_native_source or args.review_native_visibility_hint or args.inspect_native_visibility_hint or args.recover_native_visibility_hint or args.review_thread_followup or args.inspect_thread_followup),
                'review_legacy_prequeue' if args.review_native_source else
                'resume_interrupted_prequeue' if args.resume_native_source else
                'inspect_visibility_hint' if args.inspect_native_visibility_hint else
                'recover_visibility_hint' if args.recover_native_visibility_hint else
                'inspect_reply' if args.inspect_assistant_reply else
                'inspect_thread_followup' if args.inspect_thread_followup else
                'request_followup_ui_review' if args.review_thread_followup else 'request_visibility_ui_review')
            print(json.dumps(request,ensure_ascii=False))
            return 0
        if not args.run_registered:
            control.start()
            print('助手启动请求已登记。请在项目的“抖音助手”查看收件、任务和需要本人处理的提示。')
            return 0
        run_registered(queue,control)
        return 0
    except Exception as error:
        current=control.snapshot()
        owns_registered_pid=current.get('pid')==os.getpid()
        owns_pending_launch=(launching_run is not None and current.get('run_id')==launching_run
            and current.get('state')=='starting' and current.get('pid') in {None,os.getpid(),os.getppid()})
        if args.run_registered and current.get('run_id') and (owns_registered_pid or owns_pending_launch):
            # Only this newly registered product is marked; no process is killed.
            control.finish(current['run_id'],'runtime_start_failed')
        print('助手未能继续运行（'+type(error).__name__+'）。任务记录保留，请在项目中核对配置和助手窗口。')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
