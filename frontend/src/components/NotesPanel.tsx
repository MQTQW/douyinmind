import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowLeft, Download, FileText, RefreshCw, X } from 'lucide-react';
import * as api from '../api';

interface Props { video: { platform_item_id: string; title: string }; onClose: () => void; onStateChanged?: () => void }
type Intent = { videoId: string; requestId: string };
type IntentStore = { items: Intent[]; blocked: boolean };
type Confirmation = { fingerprint: string; steps: number; recovery?: api.AiNoteRequest };
const STORAGE_KEY = 'douyinmind.notes.requests.v1';
const UUID = /^[a-f\d]{8}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{12}$/i;
const STATES = ['queued', 'running', 'done', 'needs_review', 'failed', 'stale', 'deleted'];
const PAGE_SIZE = 10;

function readIntents(): IntentStore {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return { items: [], blocked: false };
    if (raw.length > 32000) throw new Error();
    const value = JSON.parse(raw), seen = new Set();
    if (value?.version !== 1 || !Array.isArray(value.items) || value.items.length > 100) throw new Error();
    for (const item of value.items) {
      if (typeof item?.videoId !== 'string' || typeof item?.requestId !== 'string'
          || !/^[A-Za-z0-9_-]{1,64}$/.test(item.videoId) || !UUID.test(item.requestId)
          || seen.has(item.videoId)) throw new Error();
      seen.add(item.videoId);
    }
    return { items: value.items.map((item: Intent) => ({ videoId: item.videoId, requestId: item.requestId })), blocked: false };
  } catch { return { items: [], blocked: true }; }
}

function saveIntent(videoId: string, requestId: string | null, expected?: string): boolean {
  try {
    const stored = readIntents();
    if (stored.blocked) return false;
    const current = stored.items.find(item => item.videoId === videoId);
    if (current && current.requestId !== expected) return false;
    const items = stored.items.filter(item => item.videoId !== videoId);
    if (requestId) items.push({ videoId, requestId });
    if (items.length > 100) return false;
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ version: 1, items }));
    return readIntents().items.find(item => item.videoId === videoId)?.requestId === (requestId ?? undefined);
  } catch { return false; }
}

function noteValid(item: api.AiNoteRequest | undefined, videoId: string, requestId?: string): item is api.AiNoteRequest {
  return Boolean(item && UUID.test(item.request_id) && item.platform_item_id === videoId
    && (!requestId || requestId === item.request_id) && STATES.includes(item.state));
}
function mergeNotes(previous: api.AiNoteRequest[], incoming: api.AiNoteRequest[]): api.AiNoteRequest[] {
  return [...new Map([...previous, ...incoming].map(item => [item.request_id, item])).values()]
    .sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0));
}
const number = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0;
const label = (state: string) => ({ queued: '等待整理', running: '正在整理', done: '笔记已保存',
  needs_review: '结果需要核对', failed: '整理未完成', stale: '此前识别版本的笔记', deleted: '资料已删除' } as Record<string, string>)[state] || '状态待核对';
function dateLabel(value: string): string {
  const time = Date.parse(value);
  return Number.isFinite(time) ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai',
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(time) : '时间未记录';
}
function usageLabel(item: api.AiNoteRequest): string {
  const usage = item.usage, total = usage?.known_tokens?.total_tokens;
  const known = number(total) ? `已记录 ${total.toLocaleString('zh-CN')} token` : 'Token 用量未记录完整';
  const attempts = number(usage?.request_attempts) ? ` · ${usage.request_attempts} 次请求` : '';
  return known + attempts + (usage?.complete === true && usage?.is_invoice === false ? ' · 非费用账单' : ' · 用量或结果仍不完整');
}

export default function NotesPanel({ video, onClose, onStateChanged }: Props) {
  const id = video.platform_item_id;
  const initial = useRef(readIntents());
  const [capability, setCapability] = useState<api.AiNotesCapabilities | null>(null);
  const [source, setSource] = useState<api.AiNotesSource | null>(null);
  const [items, setItems] = useState<api.AiNoteRequest[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [requestError, setRequestError] = useState('');
  const [notice, setNotice] = useState('');
  const [intent, setIntent] = useState<string | null>(initial.current.items.find(item => item.videoId === id)?.requestId ?? null);
  const [storageBlocked, setStorageBlocked] = useState(initial.current.blocked);
  const [busy, setBusy] = useState(false);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const alive = useRef(false), loadingRef = useRef(false), busyRef = useRef(false), checkingRef = useRef(false);
  const sequence = useRef(0), offset = useRef(0), title = useRef<HTMLHeadingElement>(null);
  const dialog = useRef<HTMLElement>(null), cancel = useRef<HTMLButtonElement>(null);
  const stateChanged = useRef(onStateChanged); stateChanged.current = onStateChanged;
  const current = useRef({ source, items, intent, capability, confirmation, storageBlocked });
  current.current = { source, items, intent, capability, confirmation, storageBlocked };
  const closeConfirmation = useCallback(() => {
    current.current.confirmation = null;
    setConfirmation(null);
  }, []);

  const applyItem = useCallback((item: api.AiNoteRequest) => {
    if (!alive.current) return;
    const previousState = current.current.items.find(row => row.request_id === item.request_id)?.state;
    setItems(previous => mergeNotes(previous, [item]));
    setTotal(previous => Math.max(previous, current.current.items.length + (current.current.items.some(row => row.request_id === item.request_id) ? 0 : 1)));
    setRequestError('');
    if (previousState !== item.state) stateChanged.current?.();
    if (['done', 'failed', 'stale', 'deleted'].includes(item.state)
        && saveIntent(id, null, item.request_id)) setIntent(null);
  }, [id]);

  const checkRequest = useCallback(async (requestId: string, announce = true) => {
    if (checkingRef.current) return;
    checkingRef.current = true;
    try {
      const result = await api.getAiNoteRequest(requestId);
      if (!alive.current) return;
      if (result.success && result.found === true && noteValid(result.item, id, requestId)) {
        applyItem(result.item);
        if (announce) setNotice('已读取原请求的最新记录，没有重新调用模型。');
      } else if (result.success && result.found === false) {
        setNotice('暂未找到这次请求的已登记结果。原编号已保留；当前不会再次提交，请稍后继续核查。');
      } else setRequestError('返回的记录尚不能确认属于原请求。请保留编号，再次核查。');
    } catch {
      if (alive.current) setRequestError('生成结果暂时无法核查。原请求编号已保留，当前不会再次提交。');
    } finally { checkingRef.current = false; }
  }, [applyItem, id]);

  const load = useCallback(async (more = false) => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    const token = ++sequence.current;
    setLoading(true); setLoadError('');
    try {
      let cap = current.current.capability;
      if (!more) {
        cap = await api.getAiNotesCapabilities().catch(() => null);
        if (!alive.current || sequence.current !== token) return;
        setCapability(cap);
      }
      if (!api.aiNotesSupported(cap)) {
        setCapability(null);
        setLoadError(api.aiNotesFailureMessage('NOTES_BACKEND_UNSUPPORTED'));
        return;
      }
      const start = more ? offset.current : 0;
      const result = await api.listAiNotes(id, PAGE_SIZE, start);
      if (!alive.current || sequence.current !== token) return;
      if (!result.success || !Array.isArray(result.items) || !number(result.total) || !result.source) throw new Error();
      const rows = result.items.filter(item => noteValid(item, id));
      if (rows.length !== result.items.length) throw new Error();
      if (noteValid(result.current_note ?? undefined, id)) rows.push(result.current_note!);
      offset.current = start + result.items.length;
      setSource(result.source); setTotal(result.total);
      setItems(previous => mergeNotes(more ? previous : [], rows));
    } catch {
      if (alive.current && sequence.current === token) setLoadError('笔记记录暂时无法读取，已显示的记录会保留。请重新读取；当前未生成新笔记。');
    } finally {
      loadingRef.current = false;
      if (alive.current && sequence.current === token) setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    alive.current = true;
    title.current?.focus({ preventScroll: true });
    void load();
    const storageChanged = (event: StorageEvent) => {
      if (event.key !== STORAGE_KEY) return;
      const state = readIntents();
      setStorageBlocked(state.blocked);
      setIntent(state.items.find(item => item.videoId === id)?.requestId ?? null);
    };
    window.addEventListener('storage', storageChanged);
    return () => { alive.current = false; sequence.current++; window.removeEventListener('storage', storageChanged); };
  }, [id, load]);

  const pollingId = intent || items.find(item => ['queued', 'running'].includes(item.state))?.request_id;
  useEffect(() => {
    if (!pollingId || !api.aiNotesSupported(capability)) return;
    void checkRequest(pollingId, false);
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'hidden') void checkRequest(pollingId, false);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [pollingId, capability, checkRequest]);

  useEffect(() => {
    if (!confirmation || !dialog.current) return;
    const node = dialog.current, previous = document.activeElement as HTMLElement | null;
    const app = document.getElementById('root'), wasInert = app?.hasAttribute('inert');
    app?.setAttribute('inert', ''); cancel.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); if (!busyRef.current) closeConfirmation(); }
      if (event.key !== 'Tab') return;
      const controls = [...node.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],[tabindex="0"]')];
      const first = controls[0], last = controls[controls.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    node.addEventListener('keydown', keydown);
    return () => {
      node.removeEventListener('keydown', keydown);
      if (!wasInert) app?.removeAttribute('inert');
      if (previous?.isConnected) previous.focus(); else title.current?.focus();
    };
  }, [confirmation, closeConfirmation]);

  const prepare = (recovery?: api.AiNoteRequest) => {
    const state = current.current;
    if (!alive.current || busyRef.current || loadingRef.current || state.storageBlocked || !api.aiNotesSupported(state.capability)
        || !state.capability?.ready || state.source?.status !== 'ready'
        || !/^[a-f\d]{64}$/i.test(state.source.fingerprint || '')
        || !number(state.source.expected_call_count) || state.source.expected_call_count < 1
        || state.source.expected_call_count > state.capability.max_expected_calls || loadError) return;
    if (recovery) {
      const fresh = state.items.find(item => item.request_id === recovery.request_id);
      if (!fresh?.can_redo || !['needs_review', 'failed', 'stale'].includes(fresh.state)
          || (state.intent && state.intent !== fresh.request_id)) return;
    } else if (state.intent || state.items.some(item => item.state !== 'deleted')) return;
    setRequestError('');
    setConfirmation({ fingerprint: state.source.fingerprint!, steps: state.source.expected_call_count, recovery });
  };

  const submit = async () => {
    const plan = current.current.confirmation;
    if (!alive.current || !plan || busyRef.current || current.current.storageBlocked) return;
    busyRef.current = true; setBusy(true); setRequestError('');
    let requestId: string | undefined;
    try {
      requestId = window.crypto?.randomUUID();
      if (!UUID.test(requestId || '') || !saveIntent(id, requestId!, plan.recovery?.request_id)) {
        setStorageBlocked(true);
        setRequestError('无法可靠保存本次请求编号，因此没有提交。请保留已有记录，恢复本机存储后再试。');
        return;
      }
      setIntent(requestId!); closeConfirmation();
      const result = await api.createAiNote(id, { request_id: requestId!, confirm: true,
        expected_source_fingerprint: plan.fingerprint,
        ...(plan.recovery ? { redo: true, recovery_of: plan.recovery.request_id } : {}) });
      if (!alive.current) return;
      if (result.success && noteValid(result.item, id, requestId)) {
        applyItem(result.item);
        setNotice(result.cached ? '已找到保存的笔记。下载读取固定文件，不会重新整理。' : '请求已登记。离开此页不会取消任务，返回后可核查原请求。');
      } else {
        if (result.request_registered === false && result.needs_review !== true && saveIntent(id, null, requestId)) {
          setIntent(null); setNotice('后台已确认本次未登记。请刷新整理范围后，再明确确认是否生成。'); void load();
        }
        setRequestError(api.aiNotesFailureMessage(result.code));
      }
    } catch (error) {
      if (!alive.current) return;
      if (error instanceof api.AiNotesError && requestId
          && (!error.requestSent || error.requestRegistered === false && error.needsReview !== true)
          && saveIntent(id, null, requestId)) {
        setIntent(null);
        if (!error.requestSent) setCapability(null); else void load();
        setRequestError(api.aiNotesFailureMessage(error.code));
      } else setRequestError('提交的网络结果尚未确认。编号已保存在本机，请核查原请求；当前不会再次提交。');
    } finally {
      busyRef.current = false;
      if (alive.current) { setBusy(false); closeConfirmation(); if (requestId) stateChanged.current?.(); }
    }
  };

  const supported = api.aiNotesSupported(capability);
  const canGenerate = supported && capability?.ready && source?.status === 'ready'
    && /^[a-f\d]{64}$/i.test(source.fingerprint || '') && number(source.expected_call_count)
    && source.expected_call_count > 0 && source.expected_call_count <= capability.max_expected_calls
    && !loading && !loadError && !storageBlocked && !busy;
  const pendingItem = items.find(item => item.request_id === intent);
  const newGeneration = !intent && items.every(item => item.state === 'deleted');
  return <section className="notes-panel" aria-labelledby="notes-heading">
    <div className="notes-heading-row"><button type="button" className="text-button" onClick={onClose}><ArrowLeft size={15} />返回资料</button>
      <button type="button" className="ui-button" onClick={() => void load()} disabled={loading || busy} aria-busy={loading}><RefreshCw size={14} />{loading ? '正在读取…' : '刷新笔记'}</button></div>
    <h2 id="notes-heading" ref={title} tabIndex={-1}>AI 笔记</h2>
    <p className="notes-video-title">{video.title || '未命名视频'}</p>
    <p className="notes-scope">仅整理这个视频已保存的文字稿、语音时间记录、画面分析和识别提示（有则包含），不会重新识别视频或读取其他资料。笔记保存在本机，不会自动加入知识库检索。</p>
    <a className="text-button notes-original" href={`/api/knowledge/export/${encodeURIComponent(id)}?mode=original`}><FileText size={14} />导出当前原文与时间</a>
    {loadError && <p role="alert" className="notes-feedback">{loadError} <button type="button" className="text-button" onClick={() => void load()} disabled={loading}>重新读取</button></p>}
    {requestError && <p role="alert" className="notes-feedback">{requestError}</p>}
    {notice && <p role="status" aria-live="polite" className="notes-notice">{notice}</p>}
    {storageBlocked && <p role="status" className="notes-notice">本机无法可靠保存请求编号，当前不生成新笔记。已有笔记和原文仍可读取。</p>}
    {supported && !capability?.ready && <p role="status" className="notes-notice">笔记生成服务尚未就绪。已有记录仍可核查，当前不会提交生成请求。</p>}
    {source && <div className="notes-source-summary"><dl>
      <div><dt>已保存内容</dt><dd>{number(source.char_count) ? `${source.char_count.toLocaleString('zh-CN')} 字符` : '未完整记录'}</dd></div>
      <div><dt>整理范围</dt><dd>{number(source.chunk_count) ? `${source.chunk_count} 段` : '未完整记录'}</dd></div>
      <div><dt>预计整理步骤</dt><dd>{number(source.expected_call_count) ? `${source.expected_call_count} 步` : '未完整记录'}</dd></div>
    </dl>{newGeneration && <button type="button" className="ui-button ui-button-primary" disabled={!canGenerate} onClick={() => prepare()}>生成并保存笔记</button>}</div>}
    {source?.status === 'ready' && newGeneration && items.length > 0 && <p className="notes-notice">当前资料已重新入库。旧删除记录保留；确认后将为当前识别版本建立新的生成请求。</p>}
    {source?.status === 'too_large' && <p className="notes-notice">已保存内容超过当前整理上限，本次不能生成。原文仍可导出核对。</p>}
    {source?.status === 'unavailable' && <p className="notes-notice">当前没有可整理的完整已保存内容，或资料正在删除。请先核对原资料。</p>}
    {intent && <div className="notes-pending"><div><strong>{pendingItem ? label(pendingItem.state) : '有生成请求需要核查'}</strong><small>原请求编号：{intent}</small></div>
      <button type="button" className="ui-button" onClick={() => void checkRequest(intent)} disabled={!supported || busy}>核查生成结果</button></div>}
    <div className="notes-history-heading"><h3>已保存的笔记与生成记录</h3><span>{items.length} / {total} 条</span></div>
    {loading && !items.length && <p role="status" className="empty-hint">正在读取本机笔记记录…</p>}
    {!loading && supported && !loadError && !items.length && <p className="empty-hint">还没有笔记。确认整理范围和可能产生的费用后，再开始生成。</p>}
    <div className="resource-list">{items.map(item => {
      const download = api.aiNoteDownloadURL(item);
      const redo = item.can_redo === true && ['needs_review', 'failed', 'stale'].includes(item.state)
        && (!intent || intent === item.request_id);
      const model = typeof item.model_label === 'string' && item.model_label.length <= 100 && !/https?:|[\r\n?=@]/i.test(item.model_label) ? item.model_label : null;
      return <article className="resource-row notes-record" key={item.request_id} aria-label={`笔记记录：${item.request_id}`}>
        <div className="resource-title"><strong>{label(item.state)} · {dateLabel(item.created_at)}</strong>
          {item.state === 'stale' && <p>{download ? '基于此前保存的识别版本；下载保留当时的笔记与原始识别结果。' : '基于此前保存的识别版本；当前没有可下载的已完成笔记。'}</p>}
          {item.state === 'deleted' && <p>资料已删除，笔记正文不再提供下载。</p>}
          {item.state === 'needs_review' && <p>原结果仍需核对，当前没有自动重试。重新生成可能再次产生费用。</p>}
          {item.cached === true && <p>复用已保存笔记，本次没有新增整理调用；下方用量属于原笔记。</p>}
          <small>{number(item.progress) && number(item.total) && item.total > 0 ? `${item.progress} / ${item.total} 整理步骤` : '步骤进度未完整记录'}{number(item.reused_step_count) && item.reused_step_count > 0 ? ` · 已复用 ${item.reused_step_count} 步` : ''}{model ? ` · ${model}` : ''}</small>
          <small>{usageLabel(item)}</small><small>请求编号：{item.request_id}</small>
        </div><div className="notes-record-actions">
          {download && <a className="ui-button" href={download}><Download size={14} />下载笔记</a>}
          {redo && <button type="button" className="ui-button" disabled={!canGenerate} onClick={() => prepare(item)}>确认后重新生成</button>}
          {['queued', 'running', 'needs_review'].includes(item.state) && <button type="button" className="text-button" disabled={!supported || busy} onClick={() => void checkRequest(item.request_id)}>核查记录</button>}
        </div>
      </article>;
    })}</div>
    {offset.current < total && <button type="button" className="ui-button notes-load-more" onClick={() => void load(true)} disabled={loading || busy}>载入更多记录</button>}
    <p className="notes-footer">离开此页只关闭查看，不取消已经登记的任务。下载读取已保存的固定文件，不会再调用模型。用量属于生成该笔记的过程，不跨复用请求合计，也不是服务商账单；未知结果或费用不会按零计算。</p>
    {confirmation && createPortal(<div className="notes-confirm-backdrop"><section ref={dialog} role="dialog" aria-modal="true" aria-labelledby="note-confirm-title" aria-describedby="note-confirm-description" className="notes-confirm-dialog">
      <div className="notes-confirm-heading"><h2 id="note-confirm-title">{confirmation.recovery ? '确认重新生成笔记' : '确认生成 AI 笔记'}</h2><button type="button" className="icon-button" aria-label="取消生成确认" disabled={busy} onClick={closeConfirmation}><X size={18} /></button></div>
      <p className="notes-video-title">{video.title || '未命名视频'}</p>
      <p id="note-confirm-description">仅使用这个视频当前已保存的识别内容，预计 {confirmation.steps} 个整理步骤。生成可能产生模型费用；长文会分段整理后汇总。未知用量和费用不会按零计算。</p>
      {confirmation.recovery && <p>这是一次新的明确请求。旧记录和未知费用保留；后台只复用已确认可用的步骤，不会重新执行原请求。</p>}
      <p>完成后笔记保存在本机，附上当时的原始识别结果。不会重新识别视频，也不会改变知识库检索范围。</p>
      {requestError && <p role="alert" className="notes-feedback">{requestError}</p>}
      <div className="notes-confirm-actions"><button ref={cancel} type="button" className="ui-button" onClick={closeConfirmation} disabled={busy}>暂不生成</button><button type="button" className="ui-button ui-button-primary" onClick={() => void submit()} disabled={busy || storageBlocked} aria-busy={busy}>{busy ? '正在登记…' : '确认并生成'}</button></div>
    </section></div>, document.body)}
  </section>;
}
