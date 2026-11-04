import { useCallback, useEffect, useRef, useState } from 'react';
import { ListChecks, RefreshCw, RotateCcw, Trash2 } from 'lucide-react';
import * as api from '../api';

interface Props {
  kind: 'tasks' | 'deletions';
  refreshKey: number;
  loggedIn: boolean;
  onConnectFavorites?: () => void;
  onStateChanged: () => void;
  onSubmitted?: (taskId: string) => void;
}

type RetryIntent = { requestId: string; checkedAbsent: boolean };
type RetryState = { intents: Record<string, RetryIntent>; volatile: boolean };
const STORAGE_KEY = 'douyinmind.ingestion-retry-intents.v1';
const PAGE_SIZE = 10;
const validTaskId = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);
const validRequestId = (value: unknown): value is string => typeof value === 'string' && /^[a-f\d]{8}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{12}$/i.test(value);

function readIntents(): RetryState {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (raw == null) return { intents: {}, volatile: false };
    if (raw.length > 64000) throw new Error();
    const value = JSON.parse(raw);
    if (value?.version !== 1 || !Array.isArray(value.intents) || value.intents.length > 100) throw new Error();
    const intents: Record<string, RetryIntent> = {};
    for (const item of value.intents) {
      if (!validTaskId(item?.taskId) || !validRequestId(item?.requestId) || item.unknown !== true || intents[item.taskId]) throw new Error();
      intents[item.taskId] = { requestId: item.requestId, checkedAbsent: false };
    }
    return { intents, volatile: false };
  } catch { return { intents: {}, volatile: true }; }
}

function writeIntents(intents: Record<string, RetryIntent>): boolean {
  try {
    const items = Object.entries(intents).map(([taskId, intent]) => ({ taskId, requestId: intent.requestId, unknown: true }));
    if (items.length) window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ version: 1, intents: items }));
    else window.sessionStorage.removeItem(STORAGE_KEY);
    return true;
  } catch { return false; }
}

function mergeRows<T>(previous: T[], incoming: T[], identity: (item: T) => string): T[] {
  return [...new Map([...previous, ...incoming].map(item => [identity(item), item])).values()];
}

function dateLabel(value?: number | string | null): string {
  if (value == null) return '时间未记录';
  const parsed = typeof value === 'number' ? value * 1000 : Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z');
  if (!Number.isFinite(parsed) || parsed <= 0) return '时间未记录';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(parsed));
}

function canRetry(task: api.IngestionTaskItem): boolean {
  return task.can_retry === true && ['failed', 'interrupted', 'needs_review'].includes(task.status);
}

function canResume(operation: api.DeletionOperationItem): boolean {
  return operation.active === true && operation.can_resume === true && ['pending', 'index_deleted', 'files_deleted'].includes(operation.deletion_state);
}

function taskLabel(task: api.IngestionTaskItem): string {
  const labels: Record<string, string> = { queued: '等待处理', running: '正在处理', done: '已完成', failed: '处理失败', interrupted: '处理已中断', needs_review: '需要核对', cancelled: '已取消' };
  return labels[task.status] || '状态待核对';
}

function taskProgress(task: api.IngestionTaskItem): string {
  const progress = task.progress, total = task.total;
  if (task.status === 'done' && progress === 0 && total === 0
      && Array.isArray(task.metadata?.platform_item_ids) && task.metadata.platform_item_ids.length === 0) {
    return '0 / 0 视频 · 无需重复处理';
  }
  return Number.isSafeInteger(progress) && Number.isSafeInteger(total) && progress! >= 0 && total! > 0 && progress! <= total!
    ? `${progress} / ${total} 视频` : '进度未完整记录';
}

function deletionLabel(state: string): string {
  return ({ pending: '删除意图已保存', index_deleted: '索引清理已确认', files_deleted: '缓存清理已确认，等待最终提交', done: '删除已完成' } as Record<string, string>)[state] || '删除进度待核对';
}

export default function KnowledgeRecoveryPanel(props: Props) {
  const { kind, refreshKey } = props;
  const [tasks, setTasks] = useState<api.IngestionTaskItem[]>([]);
  const [deletions, setDeletions] = useState<api.DeletionOperationItem[]>([]);
  const [taskTotal, setTaskTotal] = useState(0);
  const [deletionTotal, setDeletionTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [retryState, setRetryState] = useState<RetryState>(readIntents);
  const propsRef = useRef(props); propsRef.current = props;
  const rowsRef = useRef({ tasks, deletions }); rowsRef.current = { tasks, deletions };
  const retryRef = useRef(retryState); retryRef.current = retryState;
  const busyRef = useRef('');
  const aliveRef = useRef(false);
  const loadSequence = useRef(0);
  const offsetRef = useRef({ tasks: 0, deletions: 0 });
  const completedDeletions = useRef(new Set<string>());

  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; loadSequence.current++; };
  }, []);

  const changeIntent = useCallback((taskId: string, next: RetryIntent | null, expected?: string): boolean => {
    const stored = readIntents();
    const intents = stored.volatile || retryRef.current.volatile
      ? { ...retryRef.current.intents, ...stored.intents } : { ...stored.intents };
    for (const [id, intent] of Object.entries(intents)) {
      if (retryRef.current.intents[id]?.requestId === intent.requestId) intents[id] = retryRef.current.intents[id];
    }
    const current = intents[taskId];
    if ((expected && current && current.requestId !== expected)
        || (next && current && current.requestId !== next.requestId)
        || (next && !current && Object.keys(intents).length >= 100)) {
      retryRef.current = { intents, volatile: stored.volatile || retryRef.current.volatile };
      if (aliveRef.current) {
        setRetryState(retryRef.current);
        setNotice('已有重新提交请求需要核对，请先读取原请求结果。');
      }
      return false;
    }
    if (next) intents[taskId] = next; else delete intents[taskId];
    const saved = writeIntents(intents);
    const volatile = stored.volatile || retryRef.current.volatile || !saved;
    retryRef.current = { intents, volatile };
    if (aliveRef.current) setRetryState(retryRef.current);
    return true;
  }, []);

  const load = useCallback(async (append = false) => {
    const sequence = ++loadSequence.current;
    const selectedKind = propsRef.current.kind;
    const offset = append ? offsetRef.current[selectedKind] : 0;
    setLoading(true); setError('');
    try {
      if (selectedKind === 'tasks') {
        const result = await api.listIngestionTasks(PAGE_SIZE, offset);
        if (!result.success || !Array.isArray(result.items) || !Number.isSafeInteger(result.total) || result.total < 0
            || result.items.some(item => !validTaskId(item.task_id)) || (!result.items.length && offset < result.total)) throw new Error();
        if (!aliveRef.current || sequence !== loadSequence.current || propsRef.current.kind !== selectedKind) return;
        setTasks(previous => mergeRows(append ? previous : [], result.items, item => item.task_id));
        setTaskTotal(result.total); offsetRef.current.tasks = offset + result.items.length;
      } else {
        const result = await api.listDeletionOperations(PAGE_SIZE, offset, true);
        if (!result.success || !Array.isArray(result.items) || !Number.isSafeInteger(result.total) || result.total < 0
            || result.items.some(item => !validRequestId(item.operation_id)) || (!result.items.length && offset < result.total)) throw new Error();
        if (!aliveRef.current || sequence !== loadSequence.current || propsRef.current.kind !== selectedKind) return;
        const items = result.items.map(item => completedDeletions.current.has(item.operation_id)
          ? { ...item, deletion_state: 'done', active: false, can_resume: false } : item);
        setDeletions(previous => mergeRows(append ? previous : [], items, item => item.operation_id));
        setDeletionTotal(result.total); offsetRef.current.deletions = offset + result.items.length;
      }
    } catch {
      if (aliveRef.current && sequence === loadSequence.current) setError(selectedKind === 'tasks'
        ? '入库任务记录暂时无法读取，已显示的记录会保留。请重新读取。'
        : '删除记录暂时无法读取，已显示的记录会保留。请重新读取。');
    } finally { if (aliveRef.current && sequence === loadSequence.current) setLoading(false); }
  }, []);

  useEffect(() => { setNotice(''); void load(); }, [kind, refreshKey, load]);

  const accepted = useCallback((taskId: string, requestId: string, nextTask: string) => {
    changeIntent(taskId, null, requestId);
    if (!aliveRef.current) return;
    setNotice('已找到本次提交对应的任务。原任务和费用记录保留。');
    propsRef.current.onStateChanged();
    if (propsRef.current.kind === 'tasks') {
      propsRef.current.onSubmitted?.(nextTask);
      void load();
    }
  }, [changeIntent, load]);

  const checkRetry = useCallback(async (taskId: string) => {
    const intent = retryRef.current.intents[taskId];
    if (!intent || busyRef.current) return;
    busyRef.current = `check:${taskId}`; setBusy(busyRef.current); setNotice('');
    try {
      const result = await api.getIngestionRetryResult(taskId, intent.requestId);
      const nextTask = result.task_id || result.task?.task_id;
      if (result.success && result.found === true && validTaskId(nextTask) && !result.needs_review) {
        accepted(taskId, intent.requestId, nextTask);
      } else if (result.success && result.found === false && !result.needs_review) {
        changeIntent(taskId, { ...intent, checkedAbsent: true }, intent.requestId);
        if (aliveRef.current) setNotice('本次核查暂未看到已登记的新任务。可再次确认后，使用原请求提交；这不表示未知费用为零。');
      } else {
        changeIntent(taskId, { ...intent, checkedAbsent: false }, intent.requestId);
        if (aliveRef.current) setNotice(api.knowledgeRecoveryMessage(result, '这次提交的结果仍需核对。当前没有再次提交，请保留原请求。'));
      }
    } catch {
      changeIntent(taskId, { ...intent, checkedAbsent: false }, intent.requestId);
      if (aliveRef.current) setNotice('这次提交的结果暂时无法核查。当前没有再次提交，请保留原请求后重试核查。');
    } finally {
      busyRef.current = '';
      if (aliveRef.current) setBusy('');
    }
  }, [accepted, changeIntent]);

  const retryTask = useCallback(async (taskId: string) => {
    if (busyRef.current || propsRef.current.kind !== 'tasks') return;
    const pending = retryRef.current.intents[taskId];
    if (pending && !pending.checkedAbsent) { await checkRetry(taskId); return; }
    const current = rowsRef.current.tasks.find(task => task.task_id === taskId);
    if (!current || !canRetry(current)) return;
    if (!propsRef.current.loggedIn) {
      setNotice('重新提交需要先连接抖音收藏。本地任务记录仍可读取和核查。');
      propsRef.current.onConnectFavorites?.(); return;
    }
    busyRef.current = `retry:${taskId}`; setBusy(busyRef.current); setNotice('');
    try {
      if (!confirm('重新提交这项任务？\n\n可能再次产生模型、声音识别和向量服务费用。旧任务和未知费用保留，已完成资料会复用。')) return;
      const fresh = rowsRef.current.tasks.find(task => task.task_id === taskId);
      if (propsRef.current.kind !== 'tasks' || !propsRef.current.loggedIn || !fresh || !canRetry(fresh)) return;
      const requestId = pending?.requestId || window.crypto?.randomUUID();
      if (!validRequestId(requestId)) { setNotice('无法创建可靠的提交编号，本次未重新提交。请保留当前页并稍后重试。'); return; }
      if (!changeIntent(taskId, { requestId, checkedAbsent: false }, pending?.requestId)) return;
      try {
        const result = await api.retryIngestionTask(taskId, requestId);
        const nextTask = result.task_id || result.task?.task_id;
        if (result.success && validTaskId(nextTask) && !result.needs_review) accepted(taskId, requestId, nextTask);
        else if (aliveRef.current) setNotice(api.knowledgeRecoveryMessage(result, '重新提交结果尚未确认。请先核查这一次请求，当前不会再次提交。'));
      } catch {
        if (aliveRef.current) setNotice('重新提交的网络结果尚未确认。请先核查这一次请求，当前不会再次提交。');
      }
    } finally { busyRef.current = ''; if (aliveRef.current) setBusy(''); }
  }, [accepted, changeIntent, checkRetry]);

  const resumeDeletion = useCallback(async (operationId: string) => {
    if (busyRef.current || propsRef.current.kind !== 'deletions') return;
    const operation = rowsRef.current.deletions.find(item => item.operation_id === operationId);
    if (!operation || !canResume(operation) || completedDeletions.current.has(operationId)) return;
    busyRef.current = `delete:${operationId}`; setBusy(busyRef.current); setNotice('');
    try {
      if (!confirm('继续完成这一次资料删除？\n\n只继续原删除操作。最终提交后，这个视频在所有知识库中的原文和归属会被清除。')) return;
      const current = rowsRef.current.deletions.find(item => item.operation_id === operationId);
      if (propsRef.current.kind !== 'deletions' || !current || !canResume(current) || completedDeletions.current.has(operationId)) return;
      try {
        const result = await api.resumeDeletionOperation(operationId);
        if (result.operation_id && result.operation_id !== operationId) {
          if (aliveRef.current) setNotice('返回的删除记录与原操作不一致，请保留原记录并刷新核对。');
          return;
        }
        if (result.success && (result.deletion_state === 'done' || result.already_completed)) {
          completedDeletions.current.add(operationId);
          if (aliveRef.current) setDeletions(previous => previous.map(item => item.operation_id === operationId
            ? { ...item, deletion_state: 'done', active: false, can_resume: false } : item));
        }
        if (aliveRef.current) setNotice(api.knowledgeRecoveryMessage(result, result.success
          ? (result.deletion_state === 'done' || result.already_completed ? '原删除操作已核查完成。' : '已读取原删除操作结果，请刷新记录核对进度。')
          : '删除尚未确认完成，请保留原操作并刷新核对。'));
        if (aliveRef.current && result.success) { propsRef.current.onStateChanged(); void load(); }
      } catch {
        if (aliveRef.current) setNotice('继续删除的结果尚未确认，请刷新原操作记录后核对。当前没有新建删除请求。');
      }
    } finally { busyRef.current = ''; if (aliveRef.current) setBusy(''); }
  }, [load]);

  const rows = kind === 'tasks' ? tasks : deletions;
  const total = kind === 'tasks' ? taskTotal : deletionTotal;
  const pendingOutsideList = kind === 'tasks' ? Object.keys(retryState.intents).filter(id => !tasks.some(task => task.task_id === id)) : [];
  return <section aria-label={kind === 'tasks' ? '入库任务历史与恢复' : '未完成的资料删除'} className="knowledge-recovery mt-6">
    <div className="page-section-heading flex flex-wrap items-start justify-between gap-3">
      <div><h3 className="section-title">{kind === 'tasks' ? '入库任务记录' : '待完成的资料删除'}</h3>
        <p>{kind === 'tasks' ? '重启后仍可查看已登记的任务。重新处理会建立新任务，原记录保留。' : '这里保留已登记但尚未完成的删除；继续时沿用原操作。'}</p></div>
      <button type="button" className="ui-button" onClick={() => void load()} disabled={loading || Boolean(busy)} aria-busy={loading}><RefreshCw size={14} />{loading ? '正在读取…' : '刷新记录'}</button>
    </div>
    {error && <p role="alert" className="empty-hint text-red-600">{error} <button type="button" className="text-button underline" onClick={() => void load()} disabled={loading}>重新读取</button></p>}
    {kind === 'tasks' && retryState.volatile && <p role="status" className="empty-hint">当前环境无法可靠保存待核查的请求编号，请保留当前页，先核查结果再离开。</p>}
    {notice && <p role="status" className="empty-hint" aria-live="polite">{notice}</p>}
    {loading && !rows.length && <p role="status" className="empty-hint">正在读取本机记录…</p>}
    {!loading && !error && !rows.length && <p className="empty-hint">{kind === 'tasks' ? '还没有已登记的入库任务。从收藏选择视频后，这里会保留任务记录。' : '当前没有需要继续的删除操作。'}</p>}
    {kind === 'tasks' ? <div className="resource-list">{tasks.map(task => {
      const intent = retryState.intents[task.task_id];
      const count = Array.isArray(task.metadata?.platform_item_ids) ? task.metadata.platform_item_ids.length : null;
      return <div key={task.task_id} className="resource-row flex-wrap">
        <span className="resource-icon"><ListChecks size={18} /></span>
        <div className="resource-title"><strong>导入任务 · {dateLabel(task.created_at)}</strong>
          <small>{taskLabel(task)} · {taskProgress(task)}{count == null ? ' · 资料范围未记录完整' : ` · 涉及 ${count} 个视频`}</small>
          <small>任务编号：{task.task_id}{task.needs_review ? ' · 原结果和用量仍需核对' : ''}</small>
          {intent && <small>有重新提交请求待核对，原请求编号会保留。</small>}</div>
        {intent && <button type="button" className="ui-button" onClick={() => void checkRetry(task.task_id)} disabled={Boolean(busy)} aria-label={`核查重新提交结果：${task.task_id}`}><RefreshCw size={14} />核查提交结果</button>}
        {canRetry(task) && (!intent || intent.checkedAbsent) && <button type="button" className="ui-button" onClick={() => void retryTask(task.task_id)} disabled={Boolean(busy) || (!props.loggedIn && !props.onConnectFavorites)} aria-label={`重新提交任务：${task.task_id}`} aria-busy={busy === `retry:${task.task_id}`}><RotateCcw size={14} />{!props.loggedIn ? '先连接收藏' : intent ? '用原请求再次确认' : '确认后重新提交'}</button>}
      </div>;
    })}</div> : <div className="resource-list">{deletions.map(operation => <div key={operation.operation_id} className="resource-row flex-wrap">
      <span className="resource-icon"><Trash2 size={18} /></span>
      <div className="resource-title"><strong>视频资料：{operation.platform_item_id}</strong><small>{deletionLabel(operation.deletion_state)} · {dateLabel(operation.created_at)}</small>
        {operation.last_error_code && <small>{api.knowledgeRecoveryMessage({ code: operation.last_error_code }, '上次操作尚未确认完成，原文保留到最终提交。')}</small>}</div>
      {canResume(operation) && <button type="button" className="ui-button danger-action" onClick={() => void resumeDeletion(operation.operation_id)} disabled={Boolean(busy)} aria-label={`继续原删除操作：${operation.operation_id}`} aria-busy={busy === `delete:${operation.operation_id}`}><Trash2 size={14} />确认后继续删除</button>}
    </div>)}</div>}
    {pendingOutsideList.map(taskId => <div className="resource-row flex-wrap" key={taskId}><div className="resource-title"><strong>待核查的重新提交请求</strong><small>原任务：{taskId}。先核查原请求，未读取到原任务前不会重新提交。</small></div><button type="button" className="ui-button" onClick={() => void checkRetry(taskId)} disabled={Boolean(busy)} aria-label={`核查重新提交结果：${taskId}`}>核查提交结果</button></div>)}
    <div className="mt-3 flex flex-wrap items-center justify-between gap-3"><p className="text-xs text-[var(--color-ink-muted)]">已读取 {rows.length} / {total} 条记录</p>
      {offsetRef.current[kind] < total && <button type="button" className="ui-button" onClick={() => void load(true)} disabled={loading || Boolean(busy)} aria-busy={loading}>载入更多记录</button>}</div>
    {kind === 'tasks' && <p className="mt-3 text-xs leading-relaxed text-[var(--color-ink-muted)]">模型、声音识别与向量服务费用未在这里完整汇总；未知用量和费用不会按零计算。</p>}
  </section>;
}
