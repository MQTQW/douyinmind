import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api';
import KnowledgeBaseCreateModal from './KnowledgeBaseCreateModal';
import { Folder, FolderOpen, Library, Pencil, Plus, RefreshCw } from 'lucide-react';

interface Props {
  selectedId: number | null;
  refreshKey: number;
  disabled: boolean;
  onSelect: (base: api.KnowledgeBaseItem | null) => void;
  onCreated: (base: api.KnowledgeBaseItem) => void;
  onListLoaded?: (items: api.KnowledgeBaseItem[]) => void;
  onRefresh: () => void;
}

export default function KnowledgeBasesPanel({ selectedId, refreshKey, disabled, onSelect, onCreated, onListLoaded, onRefresh }: Props) {
  const [bases, setBases] = useState<api.KnowledgeBaseItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editingBase, setEditingBase] = useState<api.KnowledgeBaseItem | null | undefined>(undefined);
  const requestRef = useRef(0);
  const onListLoadedRef = useRef(onListLoaded);
  onListLoadedRef.current = onListLoaded;

  const fetchBases = useCallback(async () => {
    const requestId = ++requestRef.current;
    setLoading(true);
    setError('');
    try {
      const result = await api.listKnowledgeBases();
      if (!result.success) throw new Error('加载失败');
      if (requestId === requestRef.current) {
        setBases(result.items);
        onListLoadedRef.current?.(result.items);
      }
    } catch {
      if (requestId === requestRef.current) setError('知识库加载失败，请重试。');
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
    const nextBases = bases.some(item => item.id === base.id)
      ? bases.map(item => item.id === base.id ? base : item)
      : [base, ...bases];
    setBases(nextBases);
    onListLoadedRef.current?.(nextBases);
    setLoading(false);
    setError('');
    if (editingBase === null) onCreated(base);
    else if (selectedId === base.id) onSelect(base);
  };

  return (
    <section className="flex flex-col gap-2" aria-label="我的知识库">
      <div className="flex items-center justify-between gap-2">
        <h2 className="section-title">我的知识库</h2>
        <div className="flex items-center gap-2">
          <button type="button" onClick={onRefresh} disabled={loading} aria-label="刷新知识库数量"
            className="icon-button" title="刷新知识库数量"><RefreshCw size={14} /></button>
          <button type="button" onClick={() => setEditingBase(null)} disabled={disabled}
            className="icon-button" aria-label="新建知识库" title="新建知识库"><Plus size={17} /></button>
        </div>
      </div>
      <button type="button" onClick={() => onSelect(null)} disabled={disabled}
        aria-pressed={selectedId === null}
        className={`all-materials-link ${selectedId === null ? 'active' : ''}`}>
        <Library size={17} />全部已入库资料
      </button>
      {loading && bases.length === 0 && <p role="status" className="px-2 py-2 text-xs text-[var(--color-ink-muted)]">正在加载知识库…</p>}
      {error && <p role="alert" className="px-2 text-xs text-red-600">{error} <button type="button" onClick={fetchBases} className="underline">重新加载</button></p>}
      {!loading && !error && bases.length === 0 && <p className="rounded-xl border border-dashed border-[var(--color-border)] p-3 text-xs leading-relaxed text-[var(--color-ink-muted)]">还没有知识库。点击“新建”，再把勾选的视频导入其中。</p>}
      {bases.map(base => (
        <div key={base.id} className={`library-entry ${selectedId === base.id ? 'active' : ''}`}>
          <button type="button" onClick={() => onSelect(base)} disabled={disabled} aria-pressed={selectedId === base.id}
            title={disabled ? '请等待当前回答完成' : base.name}
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
        </div>
      ))}
      {editingBase !== undefined && <KnowledgeBaseCreateModal knowledgeBase={editingBase ?? undefined}
        onClose={() => setEditingBase(undefined)} onSaved={handleSaved} />}
    </section>
  );
}
