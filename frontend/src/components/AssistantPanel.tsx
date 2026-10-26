import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, ArrowLeft, ArrowRight, BookOpen, Check, ChevronRight, CircleHelp, Clock3, ExternalLink, Inbox, LoaderCircle, Pause, Play, RefreshCw, Search, Settings2, ShieldCheck, Square, Users, X } from 'lucide-react';
import * as api from '../api';
import { actionFeedback, assistantCounts, assistantDuration, assistantHeartbeat, assistantNoticeCoveredByAction, assistantScopeLabel, assistantSourceTime, assistantTime, assistantTimingRows, assistantTokenUsage, canResumeTask, canRetryQueryTask, captureSummary, controlLabel, hasActiveAction, hasSendBarrier, intakeStage, libraryChoiceStage, processingStage, replyStage, safeAssistantLink, taskStepLabel, taskIssueExplanation } from '../assistantView';
import type { AssistantNoticeSource, AssistantStage } from '../assistantView';
import { demoOverview, demoTaskDetail, demoTasks } from '../assistantDemo';
import AssistantSettings from './AssistantSettings';
import AssistantOperations from './AssistantOperations';
import './AssistantPanel.display.css';

function Stage({ value }: { value: AssistantStage }) {
  return <span className={`assistant-stage tone-${value.tone}`}><i />{value.label}</span>;
}

export default function AssistantPanel() {
  const [demo, setDemo] = useState(() => new URLSearchParams(window.location.search).get('assistantDemo') === '1');
  const [tab, setTab] = useState<'tasks' | 'settings'>('tasks');
  const [overview, setOverview] = useState<api.AssistantOverview | null>(null);
  const [items, setItems] = useState<api.AssistantTask[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState('');
  const [input, setInput] = useState('');
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState<{ message: string; error: boolean; source: AssistantNoticeSource } | null>(null);
  const [revision, setRevision] = useState(0);
  const [pending, setPending] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<api.AssistantTask | null>(null);
  const [detailError, setDetailError] = useState('');
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [healthNow, setHealthNow] = useState(() => Date.now());
  const pageSize = 10;
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  const announce = (message: string, error = false) => setNotice({ message, error, source: { kind: 'local' } });
  useEffect(() => {
    const update = () => { if (document.visibilityState === 'visible') setHealthNow(Date.now()); };
    const timer = window.setInterval(update, 30000);
    document.addEventListener('visibilitychange', update);
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', update); };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let polling = false;
    setLoading(true);
    setError('');
    const load = async () => {
      if (polling || document.visibilityState === 'hidden') return;
      polling = true;
      try {
        if (demo) {
          const filtered = demoTasks.filter(item => (!filter || item.state === filter) && (!search || `${item.video_title} ${item.comment_text}`.toLowerCase().includes(search.toLowerCase())));
          if (active) {
            setOverview(previous => previous?.binding?.binding_id === 'demo-binding' ? previous : demoOverview);
            setItems(filtered.slice((page - 1) * pageSize, page * pageSize)); setTotal(filtered.length);
          }
        } else {
          const [status, tasks] = await Promise.all([
            api.getAssistantOverview(controller.signal),
            api.listAssistantTasks({ state: filter, q: search, page, page_size: pageSize }, controller.signal),
          ]);
          if (!status.control || !status.capabilities || !Array.isArray(tasks.items)) throw new Error('助手状态返回不完整，请重新加载。');
          if (active) { setOverview(status); setItems(tasks.items); setTotal(tasks.total); }
        }
        if (active) { setError(''); setLastUpdated(new Date().toISOString()); }
      } catch (reason) {
        if (active && !(reason instanceof Error && reason.name === 'AbortError')) setError(reason instanceof Error ? reason.message : '助手状态暂时无法读取，请重新加载。');
      } finally { polling = false; if (active) setLoading(false); }
    };
    void load();
    const timer = window.setInterval(() => { if (!demo) void load(); }, 5000);
    const visible = () => { if (document.visibilityState === 'visible') void load(); };
    document.addEventListener('visibilitychange', visible);
    return () => { active = false; controller.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [demo, page, filter, search, revision]);

  useEffect(() => { if (page > Math.max(1, Math.ceil(total / pageSize))) setPage(Math.max(1, Math.ceil(total / pageSize))); }, [page, total]);

  useEffect(() => {
    setDetail(null); setDetailError('');
    if (!selected) return;
    const controller = new AbortController();
    if (demo) { setDetail(demoTaskDetail(selected)); return; }
    let polling = false;
    const load = async () => {
      if (polling || document.visibilityState === 'hidden') return;
      polling = true;
      try { const value = await api.getAssistantTask(selected, controller.signal); if (!controller.signal.aborted) { setDetail(value); setDetailError(''); } }
      catch (reason) { if (!controller.signal.aborted) setDetailError(reason instanceof Error ? reason.message : '任务详情暂时无法读取。'); }
      finally { polling = false; }
    };
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [selected, demo, revision]);

  useEffect(() => {
    if (!detail?.latest_action) return;
    const action = detail.latest_action;
    const feedback = actionFeedback(action);
    setNotice(current => current?.error && current.source.kind === 'local' ? current : {
      message: feedback.message, error: action.state === 'failed',
      source: { kind: 'action', task_id: detail.id, action_id: action.id },
    });
  }, [detail?.id, detail?.latest_action?.id, detail?.latest_action?.state, detail?.latest_action?.reason_code]);

  const control = async (action: 'start' | 'pause' | 'stop') => {
    if (pending) return;
    setPending(action); setNotice(null);
    try {
      if (demo) {
        const state = action === 'start' ? 'running' : action === 'pause' ? 'paused' : 'stopped';
        setOverview(current => ({ ...(current ?? demoOverview), control: { state, receiver_state: state === 'running' ? 'ready' : undefined }, capabilities: { ...demoOverview.capabilities, start: state !== 'running', pause: state === 'running', stop: state !== 'stopped' } }));
        announce('演示状态已更新，没有操作真实账号。');
      } else {
        const result = await api.controlAssistant(action);
        if (result.overview) setOverview(result.overview);
        announce(action === 'start' ? '已请求开启，请以运行状态为准。' : action === 'pause' ? '已请求暂停新处理和发评；新指令继续排队，后台已受理的入库继续。' : '已请求停止收件和新处理；后台已受理的入库不会被强行取消，任务记录会保留。');
        refresh();
      }
    } catch (reason) { announce(reason instanceof Error ? reason.message : '操作未完成，请刷新核对。', true); }
    finally { setPending(''); }
  };

  const taskAction = async (task: api.AssistantTask, action: api.AssistantTaskAction) => {
    if (pending || hasActiveAction(task) || (action === 'resume' && !canResumeTask(task)) || (action === 'retry_query' && !canRetryQueryTask(task))) return;
    setPending(`${action}:${task.id}`); setNotice(null);
    try {
      if (demo) { announce(action === 'retry_query' ? '演示：登记一次只读问答重试，保留原失败与用量，不会重新入库。' : action === 'reconcile' ? '演示：记录一次核查，结果不明的回复仍不会重发。' : '演示：仅继续尚未开始的示例任务。'); }
      else { const result = await api.actOnAssistantTask(task.id, action); announce(action === 'retry_query' ? '本次问答重试已登记；会重新查询并生成答案，原失败记录保留，请以任务状态为准。' : result.accepted ? '请求已登记，等待助手运行时核对原记录；不会直接重复提交。' : action === 'reconcile' ? '核查记录已更新，请查看当前任务状态。' : '已请求继续，后台会再次核对是否可以安全执行。'); refresh(); }
    } catch (reason) { announce(reason instanceof Error ? reason.message : '操作未完成，请刷新核对。', true); }
    finally { setPending(''); }
  };
  const binding = overview?.binding;
  const receiver = controlLabel(overview, healthNow, !demo);
  const heartbeat = demo ? { state: 'not_expected' as const } : assistantHeartbeat(overview, healthNow);
  const capture = captureSummary(overview?.control);
  const configurationError = overview?.configuration?.state === 'unavailable'
    ? api.assistantErrorMessage(overview.configuration.reason_code, '助手设置暂时无法读取，已保留原记录。请核对原有授权和助手设置。') : '';
  const receiverState = overview?.control.receiver_state;
  const workerError = overview?.control.worker_state === 'error';
  const manual = overview?.control.manual_verification;
  const manualAvailable = !demo && manual?.available === true && manual.url === 'http://127.0.0.1:8767/' && overview?.control.process_alive === true;
  const manualNeeded = ['login_required', 'verification_required'].includes(receiverState || '');
  const receiverHelp = workerError ? '工具处理出现问题，任务记录已保留。请先查看任务详情，再核对处理状态。' : manualAvailable && manualNeeded ? '请打开“本人登录/核验”，直接在核验页处理，不要把密码或验证码发到聊天。检查通过后助手仍暂停，再明确开启并核对原任务，不会自动重发。' : receiverState === 'login_required' ? '后台助手需要重新登录，相关操作已暂停；任务已保留，不会自动重发。登录入口当前不可用，请保留暂停状态。'
    : receiverState === 'verification_required' || overview?.control.reason_code === 'verification_required' ? '后台助手需要本人核验，相关操作已暂停；任务已保留，不会自动重发。核验入口当前不可用，请保留暂停状态。'
      : overview?.control.reason_code === 'native_notice_format_unavailable' ? '通知数据的格式需要核对，收件已暂停，原指令和任务都保留。无需重新发送评论，也不用为这个数据问题输入验证码。'
        : receiverState === 'page_unavailable' ? '后台通知或页面暂时无法读取，原指令和任务已保留。请先核对运行提示，不要重复发送评论。'
        : heartbeat.state === 'stale' ? '超过 3 分钟没有新的助手报告，请核对运行状态和助手窗口；这不代表已经断线，不会自动重启。'
          : heartbeat.state === 'missing' ? '尚未收到有效的助手运行报告，请核对助手窗口。读取到网页不等于助手正在收件。'
            : '暂停处理时，新指令继续排队；停止后不再收件。后台已受理的入库会继续，任务记录保留。';
  const receiverHealthy = !workerError && !configurationError && receiverState === 'ready'
    && overview?.control.state === 'running' && !overview.control.reason_code && (demo || heartbeat.state === 'fresh');
  const counters = assistantCounts(overview?.counts ?? {});
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const recordsUnread = !lastUpdated || Boolean(error && !items.length);
  const switchDemo = (value: boolean) => {
    setDemo(value); setOverview(null); setSelected(null); setItems([]); setTotal(0); setLastUpdated(null);
    setPage(1); setFilter(''); setSearch(''); setInput(''); setNotice(null); setError('');
  };

  return <div className="assistant-panel">
    {demo && <div className="assistant-demo-banner"><div><strong>演示数据</strong><span>这些账号和任务是示例；操作只改变本页，不会启动账号、调用模型或发送评论。</span></div><button className="text-button" onClick={() => switchDemo(false)}>返回本机状态<ArrowRight size={14} /></button></div>}
    {configurationError && <div className="assistant-feedback attention assistant-configuration-error" role="alert"><ShieldCheck size={18} /><div><strong>原有助手设置暂不可读</strong><p>{configurationError}</p><button className="text-button" onClick={() => setTab('settings')}>核对原有设置</button></div></div>}
    <section className="assistant-control-card" aria-label="助手运行状态">
      <div className="assistant-control-copy"><Stage value={receiver} /><h4>{workerError ? '工具处理出现问题，任务记录已保留' : configurationError ? '先核对原有设置，任务记录会保留' : receiver.label === '最近报告已过期' || receiver.label === '等待助手报告' ? '助手运行状态需要核对' : receiver.label === '已暂停' ? '处理已暂停，新指令继续排队' : receiverState === 'ready' && overview?.control.state === 'running' ? '助手正在留意新指令' : ['login_required', 'verification_required', 'page_unavailable'].includes(receiverState || '') ? '先恢复助手页面，再接收新指令' : binding ? '开启后接收新的 @ 指令' : '先确认账号与知识库，再开启收件'}</h4>{!receiverHealthy && <p>{receiverHelp}</p>}<p className="assistant-muted assistant-capture-observation">最近核对抖音通知：{capture.time}<br />仅反映可见通知，不代表全部提醒已收齐。</p></div>
      <div className="assistant-control-buttons">{manualAvailable && manualNeeded && <a className="ui-button ui-button-primary" href="http://127.0.0.1:8767/" target="_blank" rel="noreferrer"><ShieldCheck size={15} />本人登录/核验</a>}<button className="ui-button ui-button-primary" onClick={() => void control('start')} disabled={Boolean(pending) || !overview?.capabilities.start || Boolean(configurationError) || (manualAvailable && manual?.active === true)}>{pending === 'start' ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}开启助手</button><button className="ui-button" onClick={() => void control('pause')} disabled={Boolean(pending) || !overview?.capabilities.pause}><Pause size={15} />暂停处理</button><button className="ui-button" onClick={() => void control('stop')} disabled={Boolean(pending) || !overview?.capabilities.stop}><Square size={13} />停止</button></div>
    </section>
    <AssistantOperations diagnostics={demo ? undefined : overview?.diagnostics} demo={demo} heartbeatAt={overview?.control.heartbeat_at} captureCycles={capture.cycles} />
    <div className="assistant-binding-strip assistant-ui-compact-binding"><div><Users size={16} /><span>提问账号<strong>{configurationError ? '设置暂不可读' : binding?.sender_display_name || binding?.sender_public_number || (binding ? '已设置账号 A' : '尚未设置')}</strong></span></div><div><ShieldCheck size={16} /><span>助手账号<strong>{configurationError ? '设置暂不可读' : binding?.receiver_display_name || binding?.receiver_public_number || (binding ? '已设置助手 B' : '尚未设置')}</strong></span></div><div><BookOpen size={16} /><span>查询资料范围<strong>{configurationError ? '设置暂不可读' : binding ? assistantScopeLabel({ ...binding, details: { library_name: binding.knowledge_base_name } }) : '等待选择'}</strong></span></div></div>
    <nav className="assistant-tabs" aria-label="助手工作区"><button className={tab === 'tasks' ? 'active' : ''} onClick={() => setTab('tasks')}><Inbox size={16} />任务队列</button><button className={tab === 'settings' ? 'active' : ''} onClick={() => setTab('settings')}><Settings2 size={16} />账号与设备</button><button className="text-button assistant-refresh" onClick={refresh} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} />刷新</button></nav>
    {error && <div className="assistant-feedback attention" role="alert"><CircleHelp size={18} /><div><strong>{error}</strong><p>{items.length ? '以下保留上次读取的记录，当前状态尚未更新。' : '连接后会显示本机任务，不会用示例替代真实记录。'}</p><div className="assistant-inline-actions"><button className="text-button" onClick={refresh}>重新连接</button>{!demo && <button className="text-button" onClick={() => switchDemo(true)}>查看演示数据</button>}</div></div></div>}
    {notice && !selected && <p className={`assistant-notice${notice.error ? ' is-error' : ''}`} role={notice.error ? 'alert' : 'status'}>{notice.error ? <CircleHelp size={15} /> : <Check size={15} />}{notice.message}</p>}
    {Boolean(overview?.intake_issues?.unresolved_count) && <details className="assistant-intake-issues"><summary>{overview?.intake_issues?.unresolved_count} 条指令待配置，尚未执行<ChevronRight size={14} /></summary><p>这些指令未调用模型或处理资料，不计作已完成任务。请核对账号、设备和知识库授权。</p>{overview?.intake_issues?.recent.map(issue => <div key={issue.id}><strong>{issue.message || '当前账号或设备还没有可用的知识库范围。'}</strong><span>{issue.comment_excerpt}</span><small>{assistantTime(issue.created_at)} · 未执行</small></div>)}</details>}
    {tab === 'tasks' ? <>
      <dl className="assistant-ui-compact-counts" aria-label="任务统计">{[['已接收', counters.received], ['排队中', counters.queued], ['处理中', counters.processing], ['需要核对', counters.attention]].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{typeof value === 'number' ? value : '—'}</dd></div>)}</dl>
      <form className="assistant-filters" onSubmit={event => { event.preventDefault(); setSearch(input.trim()); setPage(1); }}><label className="assistant-search"><Search size={16} /><input value={input} onChange={event => setInput(event.target.value)} placeholder="搜索视频、评论或任务" aria-label="搜索任务" maxLength={256} /><button type="submit">搜索</button></label><label className="assistant-filter"><span>状态</span><select value={filter} onChange={event => { setFilter(event.target.value); setPage(1); }}><option value="">全部状态</option><optgroup label="等待与处理"><option value="queued">排队中</option><option value="waiting_library">等待选择知识库</option><option value="waiting_desktop">等待电脑上线</option><option value="executing">处理中</option><option value="waiting_ingestion">入库处理中</option><option value="waiting_external">等待外部处理</option></optgroup><optgroup label="回复与结果"><option value="ready_to_publish">等待回复</option><option value="publishing">正在核对回复</option><option value="b_view_visible">助手账号可见</option><option value="completed_private">已处理，未公开回复</option><option value="cancelled">已取消</option></optgroup><optgroup label="需要核对"><option value="needs_review">需要人工确认</option><option value="needs_reconcile">需要核对结果</option><option value="delivery_unknown">回复结果不明</option><option value="verification_required">等待本人核验</option><option value="failed_no_submit">本次未提交</option><option value="failed">处理失败</option></optgroup></select></label></form>
      <div className="assistant-queue" aria-busy={loading}><div className="assistant-queue-heading"><span>指令与视频</span><span>收件 / 队列</span><span>资料处理</span><span>原评论回复</span><span /></div>
        {!loading && !items.length ? <div className="assistant-empty"><Inbox size={30} /><h4>{recordsUnread ? '任务记录尚未读取' : search || filter ? '没有符合条件的任务' : '还没有收到指令'}</h4><p>{recordsUnread ? '连接后显示已有任务，请重新连接后核对。' : search || filter ? '试着更换关键词或状态。' : '完成账号设置并开启助手后，用已绑定账号在评论区从 @ 列表选中助手。'}</p>{!recordsUnread && (search || filter) && <button className="ui-button" onClick={() => { setFilter(''); setInput(''); setSearch(''); setPage(1); }}>清除筛选</button>}</div> : items.map(task => <button key={task.id} className="assistant-task-row" onClick={() => { setNotice(null); setSelected(task.id); }}><span className="assistant-task-copy"><strong>{task.video_title || (task.kind === 'import' ? '视频入库指令' : '知识库提问')}</strong><span>{task.comment_text || '查看指令详情'}</span><small>{assistantTime(task.created_at)}</small></span><span><Stage value={intakeStage(task)} /></span><span><Stage value={processingStage(task)} /></span><span><Stage value={replyStage(task)} /></span><ChevronRight size={16} /></button>)}
        {loading && !items.length && <div className="assistant-empty" role="status"><LoaderCircle className="spin" size={25} /><p>正在读取任务记录…</p></div>}
      </div><div className="assistant-pagination"><span>共 {recordsUnread ? '—' : total} 项{lastUpdated && <small>最近读取 {new Date(lastUpdated).toLocaleTimeString('zh-CN', { hour12: false })}</small>}</span><div><button className="icon-button" aria-label="上一页" disabled={recordsUnread || page <= 1 || loading} onClick={() => setPage(value => value - 1)}><ArrowLeft size={16} /></button><span>{recordsUnread ? '— / —' : `${page} / ${pages}`}</span><button className="icon-button" aria-label="下一页" disabled={recordsUnread || page >= pages || loading} onClick={() => setPage(value => value + 1)}><ArrowRight size={16} /></button></div></div>
      <p className="assistant-boundary-note"><ShieldCheck size={14} />收到指令、资料完成与回复可见分别记录。回复结果不明时只核查，不自动重发。</p>
    </> : <AssistantSettings demo={demo} binding={overview?.binding} onChanged={refresh} />}
    {selected && <TaskDetails task={detail} error={detailError} pending={pending} demo={demo} notice={notice?.message ?? ''} noticeError={notice?.error ?? false} noticeSource={notice?.source} onClose={() => setSelected(null)} onRefresh={refresh} onAction={taskAction} onOpenTask={id => { setNotice(null); setSelected(id); }} />}
  </div>;
}

function TaskDetails({ task, error, pending, demo, notice, noticeError, noticeSource, onClose, onRefresh, onAction, onOpenTask }: { task: api.AssistantTask | null; error: string; pending: string; demo: boolean; notice: string; noticeError: boolean; noticeSource?: AssistantNoticeSource; onClose: () => void; onRefresh: () => void; onAction: (task: api.AssistantTask, action: api.AssistantTaskAction) => Promise<void>; onOpenTask?: (id: string) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const node = dialog.current; node?.showModal(); return () => { if (node?.open) node.close(); }; }, []);
  const replyText = task?.reply?.text ?? task?.details?.reply_text;
  const sources = task?.sources ?? task?.details?.sources;
  const latestAction = task?.latest_action;
  const actionStatus = latestAction ? actionFeedback(latestAction) : null;
  const timingRows = task ? assistantTimingRows(task) : [];
  const issue = task ? taskIssueExplanation(task) : null;
  const choice = task?.details?.library_choice;
  const choiceStage = task ? libraryChoiceStage(task) : null;
  const stages = task ? [intakeStage(task), processingStage(task), replyStage(task)].filter((stage, index, values) =>
    values.findIndex(value => value.label === stage.label && value.tone === stage.tone) === index) : [];
  const child = typeof choice?.child_task_id === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(choice.child_task_id) && choice.child_task_id !== task?.id ? choice.child_task_id : null;
  const hasSelectedLibraryId = typeof choice?.selected_knowledge_base_id === 'number' && Number.isSafeInteger(choice.selected_knowledge_base_id) && choice.selected_knowledge_base_id > 0;
  const choiceTargetLabel = typeof choice?.selected_library_name === 'string' && choice.selected_library_name.trim()
    ? `已选知识库：${choice.selected_library_name}`
    : hasSelectedLibraryId ? child ? '已选择目标知识库，请查看对应保存任务' : '已选定目标知识库，等待关联保存任务'
      : '尚未确定本次保存到哪个知识库。';
  const completedActionHistory = latestAction?.state === 'done' && ['b_view_visible', 'completed_private'].includes(task?.state ?? '');
  const actionRecord = latestAction && actionStatus && <section className="assistant-detail-section assistant-action-status" aria-label="任务核对请求"><Stage value={actionStatus} /><p role={latestAction.state === 'failed' ? 'alert' : 'status'}>{actionStatus.message}</p><small>请求最近更新：{assistantTime(latestAction.updated_at ?? latestAction.created_at)}</small></section>;
  const timeline = (steps: api.AssistantTimelineStep[]) => <ol className="assistant-timeline">{steps.map((step, index) => <li key={step.id ?? index}><i /><div><strong>{step.label || taskStepLabel(step.kind, step.metadata?.tool_name, step.metadata?.kind)}</strong><small>{assistantTime(step.created_at ?? step.at)}{(step.elapsed_ms ?? step.duration_ms ?? step.metadata?.elapsed_ms) != null && ` · ${assistantDuration(step.elapsed_ms ?? step.duration_ms ?? step.metadata?.elapsed_ms)}`}</small></div></li>)}</ol>;
  return <dialog ref={dialog} className="assistant-detail-dialog" onCancel={onClose} aria-labelledby="assistant-detail-title" onClick={event => { if (event.target === dialog.current) onClose(); }}><div className="assistant-detail-inner"><header><div><p className="eyebrow">{demo ? '演示任务 · 仅本地示例' : '任务记录'}</p><h3 id="assistant-detail-title">{task?.video_title || '指令详情'}</h3></div><button className="icon-button" onClick={onClose} aria-label="关闭任务详情" autoFocus><X size={19} /></button></header>
    {!task ? <div className="assistant-empty">{error ? <><CircleHelp size={25} /><p role="alert">{error}</p><button className="ui-button" onClick={onRefresh}>重新读取</button></> : <><LoaderCircle className="spin" /><p>正在读取处理记录…</p></>}</div> : <>
      <div className="assistant-detail-stages">{stages.map(stage => <Stage key={`${stage.tone}:${stage.label}`} value={stage} />)}</div>
      {issue && <div className="assistant-feedback attention"><CircleHelp size={18} /><p><code>{issue.code}</code>（{issue.message}）</p></div>}
      {!completedActionHistory && actionRecord}
      {task.state === 'waiting_desktop' && <div className="assistant-feedback attention"><CircleHelp size={18} /><p>对应电脑尚未连接；任务正在等待，尚未调用模型或读取该电脑的资料。</p></div>}
      {task.state === 'needs_review' && task.reason_code === 'preflight_verification_required' && !hasSendBarrier(task) && <div className="assistant-feedback attention"><ShieldCheck size={18} /><p>发布前需要本人核验，这条回复尚未提交。完成核验后，先开启助手，再核查原任务；后台允许时才可显式继续，不会自动重发。</p></div>}
      {canRetryQueryTask(task) && <div className="assistant-feedback attention"><RefreshCw size={18} /><p>这次查找资料失败，尚未准备或发送回答。可重试本条问答一次，会重新调用模型并记录用量；原失败记录保留，不会重新导入视频。</p></div>}
      {error && <div className="assistant-feedback attention" role="alert"><p>{error} 以下保留上次读取的详情。</p></div>}
      {hasSendBarrier(task) && task.state !== 'b_view_visible' && task.reply_state !== 'b_view_visible' && <div className="assistant-feedback attention"><ShieldCheck size={18} /><p>这项回复已经进入发送核对。保留原记录，只能核查，不能重新发送。</p></div>}
      <section className="assistant-detail-section"><h4>原评论指令</h4><blockquote>{task.comment_text || '未记录评论正文'}</blockquote><dl><div><dt>收到时间</dt><dd>{assistantTime(task.created_at)}</dd></div><div><dt>最近更新</dt><dd>{assistantTime(task.updated_at)}</dd></div><div><dt>资料范围</dt><dd>{assistantScopeLabel(task)}</dd></div></dl></section>
      {choiceStage && <section className="assistant-detail-section" aria-label="本轮选库状态"><h4>本轮保存位置</h4>{!stages.some(stage => stage.label === choiceStage.label && stage.tone === choiceStage.tone) && <Stage value={choiceStage} />}<p>{choiceTargetLabel}</p>{typeof choice?.option_count === 'number' && Number.isSafeInteger(choice.option_count) && choice.option_count >= 0 && <p>本轮可选知识库：{choice.option_count} 个</p>}<p className="assistant-muted">请在抖音原评论线程回答库名或序号。选定后另建保存任务；已询问和已选择都不等于视频已经入库。</p>{child && <button className="text-button" disabled={!onOpenTask || Boolean(pending)} onClick={() => onOpenTask?.(child)}>查看对应保存任务<ChevronRight size={14} /></button>}</section>}
      {replyText && <section className="assistant-detail-section"><h4>{task.state === 'completed_private' ? '本机处理结果' : '回复内容'}</h4><p className="assistant-reply-text">{replyText}</p><small>{task.state === 'completed_private' ? '结果仅保存在本机，没有提交到抖音。之后更改公开许可也不会自动发表这条旧记录。' : task.reply_state === 'b_view_visible' || task.state === 'b_view_visible' ? '已确认助手账号回读可见；提问账号和其他人视角需另核对。' : '准备好回复不代表已经发表，请以上方回复状态为准。'}</small></section>}
      {notice && !assistantNoticeCoveredByAction(task, noticeSource) && <p className={`assistant-notice${noticeError ? ' is-error' : ''}`} role={noticeError ? 'alert' : 'status'}>{notice}</p>}<footer className="assistant-detail-actions">{task.actions?.can_reconcile && <button className="ui-button" disabled={Boolean(pending) || hasActiveAction(task)} onClick={() => void onAction(task, 'reconcile')}><ShieldCheck size={15} />核查任务状态</button>}{canResumeTask(task) && <button className="ui-button ui-button-primary" disabled={Boolean(pending)} onClick={() => void onAction(task, 'resume')}><Play size={15} />继续未开始任务</button>}{canRetryQueryTask(task) && <button className="ui-button ui-button-primary" disabled={Boolean(pending)} onClick={() => void onAction(task, 'retry_query')}><RefreshCw size={15} />重试本次问答（仅一次）</button>}<button className="text-button" onClick={onRefresh} disabled={Boolean(pending)}>刷新记录</button></footer>
      <details className="assistant-ui-records"><summary>处理记录<span>来源、步骤、耗时与用量</span></summary>
        {completedActionHistory && actionRecord}
        <section className="assistant-detail-section"><h4>处理过程</h4>{task.timeline?.length ? timeline(task.timeline) : <p className="assistant-muted">此任务没有可展示的详细步骤。</p>}{Boolean(task.details?.steps?.length) && <div className="assistant-ui-record-substeps"><h5>模型与工具步骤</h5>{timeline(task.details!.steps!)}</div>}</section>
        <section className="assistant-detail-section"><h4>资料来源</h4>{sources?.length ? sources.map((source, index) => { const link = safeAssistantLink(source.url); return <div className="assistant-source" key={source.source_id ?? source.chunk_id ?? index}><BookOpen size={16} /><div><strong>{source.source_id || `资料 ${index + 1}`} · {source.title || '视频原文'}</strong><small>{assistantSourceTime(source.time_range)}</small><small>{source.chunk_id ? `原文片段 ${source.chunk_id}` : '未记录片段编号'}</small></div>{link && <a className="icon-button" href={link} target="_blank" rel="noreferrer" aria-label="打开来源视频"><ExternalLink size={15} /></a>}</div>; }) : <p className="assistant-muted">入库回执不伪造原文来源；问答来源将在实际检索后显示。</p>}</section>
        <section className="assistant-detail-section"><h4>分阶段耗时</h4><div className="assistant-usage">{timingRows.slice(0, 3).map(stage => <div key={stage.label}><Clock3 size={16} /><span>{stage.label}<strong>{stage.value}</strong></span></div>)}</div><p className="assistant-muted">本机处理不含等待入库和发表；模型耗时包含指令理解与回答。</p><p className="assistant-muted">关联视频处理记录（复用时不算本次耗时）</p><div className="assistant-usage">{timingRows.slice(3).map(stage => <div key={stage.label}><Clock3 size={16} /><span>{stage.label}<strong>{stage.value}</strong></span></div>)}</div><p className="assistant-muted">视频阶段来自关联记录；未测量的项目显示“未记录”。</p></section>
        <section className="assistant-detail-section"><h4>指令与回答用量</h4><div className="assistant-usage"><div><ArrowDownToLine size={16} /><span>文字用量<strong>{assistantTokenUsage(task.usage?.total_tokens)}</strong></span></div><div><CircleHelp size={16} /><span>{task.usage?.model_responses != null ? '模型返回' : '模型调用'}<strong>{(task.usage?.model_responses ?? task.usage?.model_calls) != null ? `${task.usage?.model_responses ?? task.usage?.model_calls} 次` : '未记录'}</strong></span></div></div><p className="assistant-muted">仅记录指令与回答模型的用量，不含声音识别、向量检索等服务，不代表全部费用；未记录的数值不会按零计算。</p></section>
      </details>
    </>}
  </div></dialog>;
}
