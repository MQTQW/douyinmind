import { useState, useEffect, useId, useMemo, useRef, type FormEvent } from 'react';
import * as api from '../api';
import { collectionLibraryName, isWholeCollectionSelected, type CollectionImportSource } from '../collectionImport';
import KnowledgeBaseSelect from './KnowledgeBaseSelect';

interface Props {
  onClose: () => void;
  onConfirm: (selectedIds: string[], knowledgeBaseId: number, targetBase?: api.KnowledgeBaseItem) => void | Promise<void>;
  pendingCount: number;
  analysisMode?: api.AnalysisMode;
  selectedVideos?: api.VideoItem[];
  sourceCollection?: CollectionImportSource;
  knowledgeBases: api.KnowledgeBaseItem[];
  selectedKnowledgeBaseId: number | null;
  onKnowledgeBaseCreated: (base: api.KnowledgeBaseItem) => void;
}

export default function BuildConfirmModal({
  onClose, onConfirm, analysisMode = 'audio', selectedVideos, sourceCollection, knowledgeBases,
  selectedKnowledgeBaseId, onKnowledgeBaseCreated,
}: Props) {
  const [videos, setVideos] = useState<api.VideoItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loadedVideoIds, setLoadedVideoIds] = useState<string[]>([]);
  const [automaticTarget, setAutomaticTarget] = useState(true);
  const [loading, setLoading] = useState(true);
  const [building, setBuilding] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [submitError, setSubmitError] = useState('');
  const [reload, setReload] = useState(0);
  const [createdBases, setCreatedBases] = useState<api.KnowledgeBaseItem[]>([]);
  const [targetId, setTargetId] = useState<number | null>(() =>
    knowledgeBases.some(base => base.id === selectedKnowledgeBaseId) ? selectedKnowledgeBaseId
      : knowledgeBases.length === 1 ? knowledgeBases[0].id : null);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');
  const dialogRef = useRef<HTMLElement>(null);
  const allCheckboxRef = useRef<HTMLInputElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const createInputRef = useRef<HTMLInputElement>(null);
  const onCloseRef = useRef(onClose);
  const busyRef = useRef(false);
  const titleId = useId();
  const descriptionId = useId();
  const targetSelectId = useId();
  const isSelectedEntry = selectedVideos !== undefined;
  const includeVisual = analysisMode === 'audio_visual';
  const allChecked = videos.length > 0 && selected.size === videos.length;
  const hasSelection = selected.size > 0;
  const bases = useMemo(() => [...new Map([...knowledgeBases, ...createdBases].map(base => [base.id, base])).values()], [knowledgeBases, createdBases]);
  const selectedItems = videos.filter(video => selected.has(video.platform_item_id));
  const recognizeCount = selectedItems.filter(video => ['pending', 'failed'].includes(video.status)).length;
  const reusedCount = selectedItems.filter(video => video.status === 'done').length;
  const processingCount = selectedItems.length - recognizeCount - reusedCount;
  const targetBase = bases.find(base => base.id === targetId);
  const collectionSource = useMemo(() => sourceCollection && {
    ...sourceCollection,
    video_ids: sourceCollection.video_ids.length ? sourceCollection.video_ids : loadedVideoIds,
  }, [sourceCollection, loadedVideoIds]);
  const isWholeCollection = !loading && isWholeCollectionSelected(collectionSource, selected);
  const automaticMode = isWholeCollection && automaticTarget;
  const automaticName = collectionLibraryName(collectionSource);
  onCloseRef.current = onClose;
  busyRef.current = building || creating;

  useEffect(() => {
    setTargetId(previous => bases.some(base => base.id === previous) ? previous
      : bases.some(base => base.id === selectedKnowledgeBaseId) ? selectedKnowledgeBaseId
        : bases.length === 1 ? bases[0].id : null);
  }, [bases, selectedKnowledgeBaseId]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setLoadError('');
    setVideos([]);
    setLoadedVideoIds([]);
    setSelected(new Set());
    (async () => {
      try {
        const items = selectedVideos ?? await api.listAllCollectionVideos('all');
        if (!active) return;
        setLoadedVideoIds(items.map(video => video.platform_item_id));
        const candidates = [...new Map(items
          .filter(video => ['pending', 'failed', 'done', 'downloading', 'transcribing', 'embedding'].includes(video.status))
          .map(video => [video.platform_item_id, video] as const)).values()];
        setVideos(candidates);
        setSelected(new Set(candidates.map(video => video.platform_item_id)));
      } catch {
        if (active) setLoadError('视频加载失败，请重试。');
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, [selectedVideos, reload]);

  useEffect(() => {
    if (allCheckboxRef.current) allCheckboxRef.current.indeterminate = hasSelection && !allChecked;
  }, [hasSelection, allChecked]);

  useEffect(() => { if (showCreate) createInputRef.current?.focus(); }, [showCreate]);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    cancelRef.current?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (!busyRef.current) onCloseRef.current();
      } else if (event.key === 'Tab') {
        const controls = dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]');
        if (!controls?.length) return;
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && (document.activeElement === first || !dialogRef.current?.contains(document.activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !dialogRef.current?.contains(document.activeElement))) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('keydown', handleKey);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, []);

  const close = () => { if (!busyRef.current) onClose(); };
  const toggleOne = (id: string) => {
    setSelected(previous => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    setSubmitError('');
  };
  const toggleAll = () => {
    setSelected(allChecked ? new Set() : new Set(videos.map(video => video.platform_item_id)));
    setSubmitError('');
  };

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = newName.trim();
    if (creating || building || !trimmed || trimmed.length > 128) return;
    setCreating(true);
    setCreateError('');
    try {
      const result = await api.createKnowledgeBase(trimmed);
      if (!result.success || !result.item) throw new Error('创建失败');
      setCreatedBases(previous => [...previous, result.item]);
      setTargetId(result.item.id);
      onKnowledgeBaseCreated(result.item);
      setNewName('');
      setShowCreate(false);
      setSubmitError('');
    } catch (cause) {
      setCreateError(cause instanceof Error && cause.message.includes('409')
        ? '已有同名知识库，请换一个名称。' : '创建失败，请稍后重试。');
    } finally {
      setCreating(false);
    }
  };

  const handleConfirm = async () => {
    const selectedIds = videos.filter(video => selected.has(video.platform_item_id)).map(video => video.platform_item_id);
    if (loading || busyRef.current || showCreate || loadError || selectedIds.length === 0 || (!automaticMode && !targetBase)) return;
    busyRef.current = true;
    setBuilding(true);
    setSubmitError('');
    try {
      let destination = targetBase;
      if (automaticMode && collectionSource) {
        const result = await api.knowledgeBaseFromCollection(collectionSource.collection_id);
        if (!result.success || !result.item) throw new Error('无法自动创建知识库，请重试。');
        destination = result.item;
        onKnowledgeBaseCreated(result.item);
      }
      if (!destination) throw new Error('请选择目标知识库。');
      await onConfirm(selectedIds, destination.id, destination);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message.replace(/^\d+:\s*/, '') : '入库任务未能启动，请重试。');
      setBuilding(false);
      busyRef.current = false;
    }
  };

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-3 sm:p-4" onClick={close}>
      <section ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId}
        className="bg-[var(--color-panel)] rounded-2xl w-full max-w-[480px] max-h-[90vh] flex flex-col shadow-2xl" onClick={event => event.stopPropagation()}>
        <header className="px-4 sm:px-6 pt-5 pb-3 shrink-0">
          <h2 id={titleId} className="font-display text-xl">{isSelectedEntry ? '导入所选视频' : '选择要导入的视频'}</h2>
          <p id={descriptionId} className="text-xs text-[var(--color-ink-muted)] mt-2 leading-relaxed">{automaticMode ? `已选择“${sourceCollection?.title}”收藏夹的全部视频。` : '选择目标知识库，再确认要导入的视频。'}</p>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 sm:px-6 pb-1">
          {automaticMode ? <div className="rounded-xl border border-accent/20 bg-accent-light p-3">
            <p className="text-xs font-semibold text-[var(--color-ink)]">自动使用收藏夹名称</p>
            <p className="mt-2 break-words text-base font-semibold text-accent">{automaticName}</p>
            <p className="mt-1 text-xs leading-relaxed text-[var(--color-ink-muted)]">确认后创建同名知识库；已有同名库时直接导入，不会重复创建。</p>
            <button type="button" onClick={() => setAutomaticTarget(false)} disabled={building || creating} className="mt-2 text-xs text-accent hover:underline disabled:opacity-40">改为手动选择知识库</button>
          </div> : <div className="rounded-xl border border-[var(--color-border)] bg-white/50 p-3">
            <div className="flex items-center justify-between gap-2">
              <label id={`${targetSelectId}-label`} htmlFor={targetSelectId} className="text-xs font-semibold text-[var(--color-ink)]">导入到知识库</label>
              {!showCreate && <button type="button" onClick={() => { setAutomaticTarget(false); setShowCreate(true); setCreateError(''); }} disabled={building || creating}
                className="text-xs text-accent hover:underline disabled:opacity-40">＋ 新建知识库</button>}
            </div>
            <KnowledgeBaseSelect id={targetSelectId} labelId={`${targetSelectId}-label`} value={targetId} options={bases}
              disabled={building || creating || showCreate}
              onChange={value => { setAutomaticTarget(false); setTargetId(value); setSubmitError(''); }} />
            {showCreate && <form onSubmit={handleCreate} className="mt-3 border-t border-[var(--color-border)] pt-3">
              <label className="block text-xs text-[var(--color-ink-soft)]">新知识库名称
                <input ref={createInputRef} value={newName} onChange={event => setNewName(event.target.value)} maxLength={128} required
                  disabled={creating || building} placeholder="例如：编程学习"
                  className="mt-1.5 h-10 w-full rounded-lg border border-[var(--color-border)] bg-white px-3 text-sm text-[var(--color-ink)] outline-none focus:border-accent" />
              </label>
              {createError && <p role="alert" className="mt-2 text-xs text-red-600">{createError}</p>}
              <div className="mt-2 flex justify-end flex-wrap gap-2">
                <button type="button" onClick={() => setShowCreate(false)} disabled={creating}
                  className="rounded-full border border-[var(--color-border)] px-3 py-1.5 text-xs text-[var(--color-ink-soft)] disabled:opacity-40">取消新建</button>
                <button type="submit" disabled={creating || building || !newName.trim() || newName.trim().length > 128}
                  className="rounded-full bg-accent px-3 py-1.5 text-xs font-semibold text-white hover:bg-accent-hover disabled:opacity-40">{creating ? '正在创建…' : '创建并选择'}</button>
              </div>
            </form>}
            {isWholeCollection && <button type="button" onClick={() => { setAutomaticTarget(true); setShowCreate(false); }} disabled={building || creating} className="mt-2 text-xs text-accent hover:underline disabled:opacity-40">使用收藏夹名称“{automaticName}”自动创建</button>}
            {collectionSource && !isWholeCollection && !loading && <p className="mt-2 text-xs text-[var(--color-ink-muted)]">当前不是完整收藏夹，请选择目标知识库。</p>}
          </div>}

          <div className="mt-3 space-y-1 text-xs leading-relaxed text-[var(--color-ink-muted)]">
            <p>已入库的视频直接加入，不重新识别；新视频按{includeVisual ? '声音与画面' : '音频'}流程处理。</p>
            <p>{includeVisual ? '识别声音与画面 → 保存文字稿、画面分析和时间信息' : '提取音频 → 完整转写 → 保存文字稿和时间信息'}</p>
          </div>
          <div className="flex items-center justify-between gap-3 py-3">
            <label className="flex items-center gap-2 text-xs text-[var(--color-ink-soft)] cursor-pointer">
              <input ref={allCheckboxRef} type="checkbox" checked={allChecked} onChange={toggleAll}
                disabled={loading || building || creating || Boolean(loadError) || videos.length === 0} className="h-4 w-4 accent-[var(--color-accent)]" />
              全选下方视频
            </label>
            <span aria-live="polite" className="text-xs text-accent font-bold whitespace-nowrap">已选 {selected.size} / {videos.length} 个</span>
          </div>
          <div aria-busy={loading} className="border border-[var(--color-border)] rounded-xl divide-y divide-[var(--color-border)]">
            {loading ? (
              <p role="status" className="text-center text-xs text-[var(--color-ink-muted)] py-10">正在加载视频…</p>
            ) : loadError ? (
              <div className="text-center text-xs py-8 px-4">
                <p role="alert" className="text-red-600">{loadError}</p>
                <button type="button" onClick={() => setReload(value => value + 1)} className="mt-3 text-accent underline underline-offset-2">重新加载</button>
              </div>
            ) : videos.length === 0 ? (
              <p className="text-center text-xs text-[var(--color-ink-muted)] py-10">{isSelectedEntry ? '所选视频正在处理中，请完成后再导入。' : '没有可导入的视频，请先同步收藏夹。'}</p>
            ) : videos.map(video => (
              <label key={video.platform_item_id} className="w-full flex items-center gap-3 px-3 py-3 text-left cursor-pointer hover:bg-black/[0.02] transition-colors">
                <input type="checkbox" checked={selected.has(video.platform_item_id)} onChange={() => toggleOne(video.platform_item_id)}
                  disabled={building || creating} className="h-4 w-4 flex-shrink-0 accent-[var(--color-accent)]" />
                <span className="flex-1 min-w-0">
                  <span className="block text-sm text-[var(--color-ink)] break-words">{video.title || '未命名视频'}</span>
                  <span className="block text-[11px] text-[var(--color-ink-muted)] mt-1 leading-relaxed">
                    {video.author ? `@${video.author}` : '作者未知'}{video.duration ? ` · ${Math.floor(video.duration / 60)}:${String(video.duration % 60).padStart(2, '0')}` : ''}
                    {video.status === 'failed' && <span className="ml-2 text-amber">上次失败，将重试</span>}
                    {video.status === 'done' && <span className="ml-2 text-success">已入库，直接加入</span>}
                    {['downloading', 'transcribing', 'embedding'].includes(video.status) && <span className="ml-2 text-amber">处理中，完成后自动可用</span>}
                  </span>
                </span>
              </label>
            ))}
          </div>
          <div aria-live="polite" className="mt-3 px-3 py-2 rounded-xl text-xs text-[var(--color-amber)] bg-amber-light leading-relaxed">
            {recognizeCount > 0 ? <>
              {recognizeCount} 个视频需要{includeVisual ? '声音与画面' : '音频'}识别，会调用千问识别及资料检索服务，按实际用量计费。
              {reusedCount > 0 && <>另有 {reusedCount} 个已入库视频直接加入，不重新识别。</>}
            </> : hasSelection ? '所选视频只加入目标知识库，不会重复调用 AI 识别。' : '请勾选至少一个视频。'}
            {processingCount > 0 && <>其中 {processingCount} 个视频正在处理，完成后会自动可用。</>}
          </div>
        </div>

        <footer className="px-4 sm:px-6 py-4 shrink-0">
          {submitError && <p role="alert" className="mb-3 text-xs text-red-600">{submitError}</p>}
          <div className="flex justify-end flex-wrap gap-3">
            <button ref={cancelRef} type="button" onClick={close} disabled={building || creating}
              className="px-4 py-2 rounded-full text-sm text-[var(--color-ink-soft)] border border-[var(--color-border)] hover:bg-black/3 transition-colors disabled:opacity-40">取消</button>
            <button type="button" onClick={handleConfirm}
              disabled={loading || building || creating || showCreate || Boolean(loadError) || !hasSelection || (!automaticMode && !targetBase)}
              className="px-4 py-2 rounded-full bg-gradient-to-r from-accent to-accent-hover text-white text-sm font-bold shadow-md shadow-accent/20 hover:shadow-lg transition-all disabled:opacity-40">
              {building ? '正在启动…' : `确认导入 ${selected.size} 个视频`}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
