import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api';
import KnowledgeBaseCreateModal from './KnowledgeBaseCreateModal';
import { Folder, FolderOpen, Library, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react';

interface Props {
  selectedId: number | null;
  refreshKey: number;
  disabled: boolean;
  disabledReason?: string;
  onSelect: (base: api.KnowledgeBaseItem | null) => void;
  onCreated: (base: api.KnowledgeBaseItem) => void;
  onListLoaded?: (items: api.KnowledgeBaseItem[]) => void;
  onDeleted?: (id: number) => void;
  onBusyChange?: (busy: boolean) => void;
  onRefresh: () => void;
}

export default function KnowledgeBasesPanel({ selectedId, refreshKey, disabled, disabledReason = '请等待当前回答完成', onSelect, onCreated, onListLoaded, onDeleted, onBusyChange, onRefresh }: Props) {
  const [bases, setBases] = useState<api.KnowledgeBaseItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editingBase, setEditingBase] = useState<api.KnowledgeBaseItem | null | undefined>(undefined);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState('');
  const [deleteSupported, setDeleteSupported] = useState(false);
  const deletingRef = useRef<number | null>(null);
  const basesRef = useRef(bases);
  basesRef.current = bases;
  const controlsRef = useRef({ selectedId, disabled, onSelect, onDeleted, onBusyChange, deleteSupported });
  controlsRef.current = { selectedId, disabled, onSelect, onDeleted, onBusyChange, deleteSupported };
  const mountedRef = useRef(true);
  const requestRef = useRef(0);
  const onListLoadedRef = useRef(onListLoaded);
  onListLoadedRef.current = onListLoaded;

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const fetchBases = useCallback(async () => {
    const requestId = ++requestRef.current;
    setLoading(true);
    setError('');
    try {
      const result = await api.listKnowledgeBases();
      if (!result.success) throw new Error('加载失败');
      if (requestId === requestRef.current) {
        basesRef.current = result.items;
        setBases(result.items);
        setDeleteSupported(result.capabilities?.delete_knowledge_base === true);
        setDeleteError('');
        onListLoadedRef.current?.(result.items);
      }
    } catch {
      if (requestId === requestRef.current) { setError('知识库加载失败，请重试。'); setDeleteSupported(false); }
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchBases();
    return () => { requestRef.current++; };
  }, [fetchBases, refreshKey]);

  const handleSaved = (base: api.KnowledgeBaseItem) => {
    requestRef.current++;
    const nextBases = basesRef.current.some(item => item.id === base.id)
      ? basesRef.current.map(item => item.id === base.id ? base : item)
      : [base, ...basesRef.current];
    basesRef.current = nextBases;
    setBases(nextBases);
    onListLoadedRef.current?.(nextBases);
    setLoading(false);
    setError('');
    if (editingBase === null) onCreated(base);
    else if (selectedId === base.id) onSelect(base);
  };

  const handleDelete = async (base: api.KnowledgeBaseItem) => {
    if (controlsRef.current.disabled || !controlsRef.current.deleteSupported || deletingRef.current !== null) return;
    deletingRef.current = base.id;
    if (!confirm(`删除知识库“${base.name}”？\n\n只删除知识库及其成员关系，视频原文会保留。`) || controlsRef.current.disabled) {
      deletingRef.current = null;
      return;
    }
    setDeletingId(base.id);
    setDeleteError('');
    const reportBusy = controlsRef.current.onBusyChange;
    try {
      reportBusy?.(true);
      try {
        const result = await api.deleteKnowledgeBase(base.id);
        if (result.success !== true || result.deleted_id !== base.id || !Number.isSafeInteger(result.removed_count) || result.removed_count < 0) throw new Error('删除结果未确认');
      } catch {
        if (mountedRef.current) setDeleteError(`删除“${base.name}”失败或结果未确认，请刷新知识库列表后重试。`);
        return;
      }
      if (!mountedRef.current) return;
      requestRef.current++;
      const nextBases = basesRef.current.filter(item => item.id !== base.id);
      basesRef.current = nextBases;
      setBases(nextBases);
      setLoading(false);
      onListLoadedRef.current?.(nextBases);
      // The parent coordinates history/statistics; the fallback only leaves a deleted current scope.
      if (controlsRef.current.onDeleted) controlsRef.current.onDeleted(base.id);
      else if (controlsRef.current.selectedId === base.id) controlsRef.current.onSelect(null);
    } finally {
      deletingRef.current = null;
      if (mountedRef.current) setDeletingId(null);
      reportBusy?.(false);
    }
  };

  return (
    <section className="flex flex-col gap-2" aria-label="我的知识库">
      <div className="flex items-center justify-between gap-2">
        <h2 className="section-title">我的知识库</h2>
        <div className="flex items-center gap-2">
          <button type="button" onClick={onRefresh} disabled={loading} aria-label="刷新知识库数量"
            className="icon-button" title="刷新知识库数量"><RefreshCw size={14} /></button>
          <button type="button" onClick={() => setEditingBase(null)} disabled={disabled}
            className="icon-button" aria-label="新建知识库" title={disabled ? disabledReason : '新建知识库'}><Plus size={17} /></button>
        </div>
      </div>
      <button type="button" onClick={() => onSelect(null)} disabled={disabled}
        aria-pressed={selectedId === null} title={disabled ? disabledReason : '全部已入库资料'}
        className={`all-materials-link ${selectedId === null ? 'active' : ''}`}>
        <Library size={17} />全部已入库资料
      </button>
      {loading && bases.length === 0 && <p role="status" className="px-2 py-2 text-xs text-[var(--color-ink-muted)]">正在加载知识库…</p>}
      {error && <p role="alert" className="px-2 text-xs text-red-600">{error} <button type="button" onClick={fetchBases} className="underline">重新加载</button></p>}
      {deleteError && <p role="alert" className="px-2 text-xs text-red-600">{deleteError}</p>}
      {!loading && !error && bases.length === 0 && <p className="rounded-xl border border-dashed border-[var(--color-border)] p-3 text-xs leading-relaxed text-[var(--color-ink-muted)]">还没有知识库。点击“新建”，再把勾选的视频导入其中。</p>}
      {bases.map(base => (
        <div key={base.id} className={`library-entry ${selectedId === base.id ? 'active' : ''}`}>
          <button type="button" onClick={() => onSelect(base)} disabled={disabled} aria-pressed={selectedId === base.id}
            title={disabled ? disabledReason : base.name}
            className="library-open">
            <span className="library-icon">{selectedId === base.id ? <FolderOpen size={19} /> : <Folder size={19} />}</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-[var(--color-ink)]">{base.name}</span>
              <span className="mt-1 block text-xs text-[var(--color-ink-muted)]">{base.video_count} 视频 · {base.ready_count} 可提问</span>
            </span>
          </button>
          <button type="button" onClick={() => setEditingBase(base)} disabled={disabled}
            aria-label={`重命名知识库：${base.name}`} title="重命名知识库"
            className="icon-button mr-1"><Pencil size={14} /></button>
          <button type="button" onClick={() => void handleDelete(base)} disabled={disabled || deletingId !== null || !deleteSupported}
            aria-label={`删除知识库：${base.name}`} aria-busy={deletingId === base.id}
            title={disabled ? disabledReason : !deleteSupported ? '后端更新加载后可删除知识库' : deletingId === base.id ? '正在删除知识库…' : '删除知识库（保留视频原文）'}
            className="icon-button mr-1"><Trash2 size={14} /></button>
        </div>
      ))}
      {editingBase !== undefined && <KnowledgeBaseCreateModal knowledgeBase={editingBase ?? undefined}
        onClose={() => setEditingBase(undefined)} onSaved={handleSaved} />}
    </section>
  );
}
