import { useState, useEffect, useCallback, useRef } from 'react';
import { createPortal } from 'react-dom';
import { AudioLines, Check, ChevronRight, Download, Ellipsis, FileAudio, FileDown, FolderMinus, History, Inbox, ListChecks, MessageCircle, Pencil, RefreshCw, RotateCcw, Trash2 } from 'lucide-react';
import type { WorkspaceView } from '../workspaceView';
import ActionMenu from './ActionMenu';
import * as api from '../api';
import BuildConfirmModal from './BuildConfirmModal';
import VideoContentModal from './VideoContentModal';
import SessionActionModal from './SessionActionModal';
import KnowledgeBasesPanel from './KnowledgeBasesPanel';
import type { CollectionImportSource } from '../collectionImport';
import { visibleHistory } from '../workspacePresentation';

interface Props {
  view: WorkspaceView;
  onChangeView: (view: WorkspaceView) => void;
  contentContainer: HTMLElement | null;
  chatOnly: boolean;
  onKnowledgeBaseUpdated: (base: api.KnowledgeBaseItem) => void;
  onBuildDone: () => void;
  selectedId: string;
  onSelectCollection: (id: string) => void;
  selectedKnowledgeBase: api.KnowledgeBaseItem | null;
  onSelectKnowledgeBase: (base: api.KnowledgeBaseItem | null) => void;
  statsRefreshKey: number;
  historyRefreshKey: number;
  activeSessionId: number | null;
  chatBusy: boolean;
  onSelectSession: (session: api.SessionItem, base?: api.KnowledgeBaseItem | null) => void;
  onNewChat: () => void;
  onSessionRenamed: (id: number, title: string) => void;
  onSessionDeleted: (id: number) => void;
  onMutationBusyChange?: (busy: boolean) => void;
  onKnowledgeBaseDeleted?: (id: number) => void;
}

export default function SourcesPanel({ view, onChangeView, contentContainer, chatOnly, onKnowledgeBaseUpdated, onBuildDone, selectedId, onSelectCollection, selectedKnowledgeBase, onSelectKnowledgeBase, statsRefreshKey, historyRefreshKey, activeSessionId, chatBusy, onSelectSession, onNewChat, onSessionRenamed, onSessionDeleted, onMutationBusyChange, onKnowledgeBaseDeleted }: Props) {
  const [collections, setCollections] = useState<api.CollectionItem[]>([]);
  const [stats, setStats] = useState<any>(null);
  const collectionsRequestRef = useRef(0);
  const statsRequestRef = useRef(0);
  const [syncing, setSyncing] = useState(false);
  const [resettingVideos, setResettingVideos] = useState(false);
  const resettingVideosRef = useRef(false);
  const [building, setBuilding] = useState(false);
  const [buildProgress, setBuildProgress] = useState(0);
  const [buildTotal, setBuildTotal] = useState(0);
  const [buildMessage, setBuildMessage] = useState('');
  const [showBuildConfirm, setShowBuildConfirm] = useState(false);
  const [buildSelection, setBuildSelection] = useState<api.VideoItem[] | undefined>();
  const [buildSource, setBuildSource] = useState<CollectionImportSource | undefined>();
  const [selectedVideos, setSelectedVideos] = useState<Map<string, api.VideoItem>>(new Map());
  const [submittedTaskId, setSubmittedTaskId] = useState<string | null>(null);
  const startingBuildRef = useRef(false);
  const [viewingItemId, setViewingItemId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<api.SessionItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState('');
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const historyRequestRef = useRef(0);
  const [sessionAction, setSessionAction] = useState<{session: api.SessionItem; action: 'rename' | 'delete'} | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [expandedVideos, setExpandedVideos] = useState<api.VideoItem[]>([]);
  const loadedCollectionIdRef = useRef<string | null>(null);
  const [loadingVideos, setLoadingVideos] = useState(false);
  const [videoLoadError, setVideoLoadError] = useState('');
  const [videosRefreshKey, setVideosRefreshKey] = useState(0);
  const [knowledgeBases, setKnowledgeBases] = useState<api.KnowledgeBaseItem[]>([]);
  const [basesRefreshKey, setBasesRefreshKey] = useState(0);
  const [baseVideos, setBaseVideos] = useState<api.VideoItem[]>([]);
  const [baseVideosLoading, setBaseVideosLoading] = useState(false);
  const [baseVideosError, setBaseVideosError] = useState('');
  const [removingVideoId, setRemovingVideoId] = useState<string | null>(null);
  const [taskVideos, setTaskVideos] = useState<api.VideoItem[]>([]);
  const [taskVideosError, setTaskVideosError] = useState('');
  const [taskVideosTotal, setTaskVideosTotal] = useState(0);
  const [taskVideosLoading, setTaskVideosLoading] = useState(false);
  const [collectionSearch, setCollectionSearch] = useState('');
  const [selectedMaterialIds, setSelectedMaterialIds] = useState<Set<string>>(new Set());
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const [bulkFeedback, setBulkFeedback] = useState('');
  const bulkDeletingRef = useRef(false);
  const aliveRef = useRef(false);
  const mutationBusyChangeRef = useRef(onMutationBusyChange);
  mutationBusyChangeRef.current = onMutationBusyChange;
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; mutationBusyChangeRef.current?.(false); };
  }, []);
  useEffect(() => { setSelectedMaterialIds(new Set()); setBulkFeedback(''); }, [selectedKnowledgeBase?.id, selectedId]);
  useEffect(() => {
    if (baseVideosLoading || baseVideosError) return;
    const available = new Set(baseVideos.filter(video => video.status === 'done').map(video => video.platform_item_id));
    setSelectedMaterialIds(previous => new Set([...previous].filter(id => available.has(id))));
  }, [baseVideos, baseVideosLoading, baseVideosError]);

  const fetchStats = useCallback(async () => {
    const requestId = ++statsRequestRef.current;
    try {
      const r = await api.getKnowledgeStats();
      if (r.success && requestId === statsRequestRef.current) {
        setStats(r);
        return true;
      }
    } catch {}
    return false;
  }, []);

  const baseCreated = (base: api.KnowledgeBaseItem) => {
    setKnowledgeBases(previous => [...previous.filter(item => item.id !== base.id), base]);
    setBasesRefreshKey(key => key + 1);
  };
  useEffect(() => {
    let active = true;
    setBaseVideos([]);
    setBaseVideosLoading(true);
    setBaseVideosError('');
    (selectedKnowledgeBase ? api.listAllKnowledgeBaseVideos(selectedKnowledgeBase.id) : selectedId !== 'all' ? api.listAllCollectionVideos(selectedId).then(videos => videos.filter(video => video.status === 'done')) : api.listAllKnowledgeVideos()).then(videos => {
      if (active) setBaseVideos(videos);
    }).catch(() => {
      if (active) setBaseVideosError('知识库内容加载失败，请重试。');
    }).finally(() => { if (active) setBaseVideosLoading(false); });
    return () => { active = false; };
  }, [selectedKnowledgeBase?.id, selectedId, statsRefreshKey, basesRefreshKey]);

  useEffect(() => {
    if (!selectedKnowledgeBase) return;
    const fresh = knowledgeBases.find(base => base.id === selectedKnowledgeBase.id);
    if (fresh && (fresh.name !== selectedKnowledgeBase.name || fresh.video_count !== selectedKnowledgeBase.video_count || fresh.ready_count !== selectedKnowledgeBase.ready_count)) onKnowledgeBaseUpdated(fresh);
  }, [knowledgeBases, selectedKnowledgeBase, onKnowledgeBaseUpdated]);

  useEffect(() => {
    if (view === 'collections' && expandedId === null && collections.length) setExpandedId(collections.find(item => item.collection_id === 'all')?.collection_id ?? collections[0].collection_id);
  }, [view, collections, expandedId]);

  useEffect(() => {
    if (view !== 'progress') return;
    let active = true;
    let loading = false;
    const refresh = async () => {
      if (loading) return;
      loading = true;
      try {
        const [result] = await Promise.all([api.listKnowledgeVideos('all'), fetchStats()]);
        if (active) {
          if (!result.success) throw new Error('加载失败');
          setTaskVideos(result.items); setTaskVideosTotal(result.total); setTaskVideosError('');
        }
      } catch { if (active) setTaskVideosError('处理状态加载失败，请重新加载。'); }
      finally { loading = false; if (active) setTaskVideosLoading(false); }
    };
    setTaskVideosLoading(true);
    void refresh();
    const timer = setInterval(refresh, 2500);
    return () => { active = false; clearInterval(timer); };
  }, [view, statsRefreshKey, videosRefreshKey, fetchStats]);

  const removeFromBase = async (video: api.VideoItem) => {
    if (!selectedKnowledgeBase || removingVideoId) return;
    if (!confirm(`仅从“${selectedKnowledgeBase.name}”移出这个视频？原文字稿和其他知识库中的资料会保留。`)) return;
    setRemovingVideoId(video.platform_item_id);
    try {
      const result = await api.removeKnowledgeBaseVideo(selectedKnowledgeBase.id, video.platform_item_id);
      if (!result.success) throw new Error('移出失败');
      setBasesRefreshKey(key => key + 1);
    } catch { alert('移出知识库失败，请稍后重试。'); }
    finally { setRemovingVideoId(null); }
  };

  const fetchCollections = useCallback(async () => {
    const requestId = ++collectionsRequestRef.current;
    try {
      const r = await api.listCollections();
      if (r.success && requestId === collectionsRequestRef.current) setCollections(r.items);
    } catch {}
  }, []);

  const fetchSessions = useCallback(async () => {
    const requestId = ++historyRequestRef.current;
    setHistoryLoading(true);
    setHistoryError('');
    try {
      const r = await api.listSessions();
      if (requestId !== historyRequestRef.current) return;
      if (!r.success) throw new Error('加载失败');
      setSessions(r.items);
    } catch { if (requestId === historyRequestRef.current) setHistoryError('历史对话加载失败，请重试。'); }
    finally { if (requestId === historyRequestRef.current) setHistoryLoading(false); }
  }, []);

  useEffect(() => {
    fetchCollections();
    fetchStats();
    return () => { collectionsRequestRef.current++; statsRequestRef.current++; };
  }, [fetchCollections, fetchStats, statsRefreshKey]);
  useEffect(() => { fetchSessions(); }, [fetchSessions, historyRefreshKey]);

  useEffect(() => {
    if (!expandedId) return;
    let cancelled = false;
    setLoadingVideos(true);
    setVideoLoadError('');
    setExpandedVideos([]);
    api.listAllCollectionVideos(expandedId).then(videos => {
      if (cancelled) return;
      loadedCollectionIdRef.current = expandedId;
      setExpandedVideos(videos);
      setSelectedVideos(previous => {
        const next = new Map(previous);
        for (const video of videos) {
          if (!next.has(video.platform_item_id)) continue;
          if (['pending', 'failed', 'done'].includes(video.status)) next.set(video.platform_item_id, video);
          else next.delete(video.platform_item_id);
        }
        return next;
      });
    }).catch((error: Error) => {
      if (!cancelled) setVideoLoadError(error.message || '视频列表加载失败');
    }).finally(() => {
      if (!cancelled) setLoadingVideos(false);
    });
    return () => { cancelled = true; };
  }, [expandedId, statsRefreshKey, videosRefreshKey]);

  const handleSync = async () => {
    setSyncing(true);
    try {
      const r = await api.syncFavorites();
      if (r.success) {
        setSelectedVideos(new Map());
        await fetchCollections();
        await fetchStats();
        setVideosRefreshKey(key => key + 1);
      } else {
        alert(r.message || '同步失败');
      }
    } catch (e: any) {
      alert('同步失败: ' + e.message);
    }
    setSyncing(false);
  };

  const handleBuild = async (selectedIds: string[], knowledgeBaseId: number, targetBase?: api.KnowledgeBaseItem) => {
    if (selectedIds?.length === 0 || building || startingBuildRef.current) return;
    startingBuildRef.current = true;
    setBuilding(true);
    setBuildProgress(0);
    setBuildTotal(0);
    setBuildMessage('准备中...');
    try {
      const r = await api.syncKnowledge(selectedIds, knowledgeBaseId);
      if (r.success) {
        setSelectedVideos(previous => {
          const next = new Map(previous);
          selectedIds.forEach(id => next.delete(id));
          return next;
        });
        const target = targetBase ?? knowledgeBases.find(base => base.id === knowledgeBaseId);
        if (target) onSelectKnowledgeBase(target);
        setBasesRefreshKey(key => key + 1);
      }
      if (r.success && r.task_id) {
        setBuildTotal(r.pending_count || 0);
        setSubmittedTaskId(r.task_id);
        onChangeView('progress');
      } else {
        setBuilding(false);
        setBuildMessage(r.message || '没有可入库的视频');
        if (!r.success) alert(r.message || '入库任务未启动');
        fetchStats();
        setVideosRefreshKey(key => key + 1);
        if (r.success) onBuildDone();
      }
    } catch (e: any) {
      alert('入库失败: ' + e.message);
      setBuilding(false);
    } finally {
      startingBuildRef.current = false;
    }
  };

  const handleResetInterrupted = async () => {
    if (resettingVideosRef.current) return;
    resettingVideosRef.current = true;
    setResettingVideos(true);
    try {
      const result = await api.resetFailedVideos();
      if (!result.success) {
        throw new Error('有入库任务正在处理或排队，未重置视频。请等待任务结束后重试。');
      }
      let refreshed = false;
      try { refreshed = await fetchStats(); } catch { /* Reset already succeeded. */ }
      setVideosRefreshKey(key => key + 1);
      setBasesRefreshKey(key => key + 1);
      setBuildMessage(refreshed
        ? `已重置 ${result.reset_count} 个视频，请重新勾选需要入库的视频。`
        : `已重置 ${result.reset_count} 个视频，统计暂未刷新，请点击“刷新数量”重试。视频列表会重新加载。`);
    } catch (error: any) {
      alert('重置失败: ' + (error?.message || '请稍后重试。'));
    } finally {
      resettingVideosRef.current = false;
      setResettingVideos(false);
    }
  };

  const handleCollectionClick = (collectionId: string) => {
    if (chatBusy) return;
    setExpandedId(previous => previous === collectionId ? null : collectionId);
  };

  const searchTitle = collectionSearch.trim().normalize('NFKC').toLocaleLowerCase();
  const visibleCollectionVideos = searchTitle ? expandedVideos.filter(video => (video.title || '').normalize('NFKC').toLocaleLowerCase().includes(searchTitle)) : expandedVideos;
  const selectableVideos = visibleCollectionVideos.filter(video => ['pending', 'failed', 'done'].includes(video.status));
  const allVisibleSelected = selectableVideos.length > 0 && selectableVideos.every(video => selectedVideos.has(video.platform_item_id));
  const toggleVideo = (video: api.VideoItem) => {
    setSelectedVideos(previous => {
      const next = new Map(previous);
      if (next.has(video.platform_item_id)) next.delete(video.platform_item_id);
      else next.set(video.platform_item_id, video);
      return next;
    });
  };
  const toggleCollection = () => {
    setSelectedVideos(previous => {
      const next = new Map(previous);
      for (const video of selectableVideos) {
        if (allVisibleSelected) next.delete(video.platform_item_id);
        else next.set(video.platform_item_id, video);
      }
      return next;
    });
  };
  const sourceForCollection = (id: string, videos: api.VideoItem[]): CollectionImportSource | undefined => {
    const collection = collections.find(item => item.collection_id === id);
    return collection ? { collection_id: id, title: collection.title, video_ids: videos.map(video => video.platform_item_id) } : undefined;
  };
  const openBuildConfirm = (selection?: api.VideoItem[], source?: CollectionImportSource) => {
    setBuildSelection(selection);
    const loadedCollectionId = loadedCollectionIdRef.current;
    setBuildSource(source ?? (selection && loadedCollectionId && !loadingVideos && !videoLoadError
      ? sourceForCollection(loadedCollectionId, expandedVideos) : undefined));
    setShowBuildConfirm(true);
  };

  const handleExport = (platformItemId: string, mode: 'original' | 'ai') => {
    const url = `/api/knowledge/export/${platformItemId}?mode=${mode}`;
    window.open(url, '_blank');
  };

  const handleDelete = async (platformItemId: string) => {
    if (!confirm('确定要删除这个视频的全部文字稿和检索资料吗？\n\n它也会从所有知识库移除，需要时可重新识别入库。')) return;
    try {
      const result = await api.deleteVideo(platformItemId);
      if (!result.success) throw new Error(result.message || '删除未完成，请稍后重试。');
      fetchStats();
      setVideosRefreshKey(key => key + 1);
      setBasesRefreshKey(key => key + 1);
    } catch (e: any) {
      alert('删除失败: ' + e.message);
    }
  };

  const doneCount = stats?.video_cache?.done ?? 0;
  const failedCount = stats?.video_cache?.failed ?? 0;
  const processingCount = (stats?.video_cache?.downloading ?? 0) + (stats?.video_cache?.transcribing ?? 0) + (stats?.video_cache?.embedding ?? 0);
  const totalCount = Object.values(stats?.video_cache ?? {}).reduce((a: number, b: any) => a + b, 0) as number;
  const pendingCount = stats?.video_cache?.pending ?? 0;
  const allFavoriteCount = collections.find(collection => collection.collection_id === 'all')?.video_count ?? 0;
  const isStuck = (processingCount > 0 && !stats?.current_task_id) || (failedCount > 0 && pendingCount === 0 && processingCount === 0);
  const analysisMode: api.AnalysisMode = stats?.analysis_mode === 'audio_visual' ? 'audio_visual' : 'audio';
  const resultDescription = analysisMode === 'audio_visual' ? '完整文字稿、画面分析和时间信息' : '完整文字稿和时间信息';
  const selectableMaterials = baseVideos.filter(video => video.status === 'done');
  const allMaterialsSelected = selectableMaterials.length > 0 && selectableMaterials.every(video => selectedMaterialIds.has(video.platform_item_id));
  const mutationBlocked = chatBusy || chatOnly || building || processingCount > 0 || Boolean(stats?.current_task_id) || bulkDeleting || Boolean(removingVideoId);
  const bulkDeleteSupported = stats?.capabilities?.guarded_video_delete === true;
  const toggleMaterial = (id: string) => {
    if (mutationBlocked) return;
    setSelectedMaterialIds(previous => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const toggleAllMaterials = () => {
    if (mutationBlocked) return;
    setSelectedMaterialIds(allMaterialsSelected ? new Set() : new Set(selectableMaterials.map(video => video.platform_item_id)));
  };
  const bulkDeleteMaterials = async (mode: 'library' | 'global') => {
    if (bulkDeletingRef.current || mutationBlocked || !bulkDeleteSupported || chatOnly || !aliveRef.current) return;
    const targetBase = selectedKnowledgeBase;
    if (mode === 'library' && !targetBase) return;
    const plan = selectableMaterials.filter(video => selectedMaterialIds.has(video.platform_item_id)).map(video => video.platform_item_id);
    if (!plan.length) return;
    const confirmed = confirm(mode === 'library'
      ? `仅从“${targetBase!.name}”移出所选 ${plan.length} 个视频？\n\n文字稿和其他知识库中的资料会保留。`
      : `删除所选 ${plan.length} 个视频的全部入库资料？\n\n这会删除它们的文字稿和检索资料，并从所有知识库移除。收藏夹中的原视频不受影响。`);
    if (!confirmed) return;
    bulkDeletingRef.current = true;
    setBulkDeleting(true);
    setBulkFeedback('');
    mutationBusyChangeRef.current?.(true);
    const completed: string[] = [];
    let failure = '';
    try {
      for (const id of plan) {
        if (!aliveRef.current) break;
        try {
          const result = mode === 'library' ? await api.removeKnowledgeBaseVideo(targetBase!.id, id) : await api.deleteVideo(id);
          if (!result.success) { failure = '有视频未能完成操作，已停止后续项。'; break; }
          completed.push(id);
        } catch { failure = '有请求的结果未确认，后续项尚未提交。请刷新核对后再操作。'; break; }
      }
      if (aliveRef.current) {
        setSelectedMaterialIds(previous => new Set([...previous].filter(id => !completed.includes(id))));
        setBulkFeedback(`${mode === 'library' ? '已移出' : '已删除入库资料'} ${completed.length} / ${plan.length} 个视频。${failure}`);
        if (completed.length || failure) {
          void fetchStats();
          setBasesRefreshKey(key => key + 1);
          setVideosRefreshKey(key => key + 1);
          onBuildDone();
        }
      }
    } finally {
      bulkDeletingRef.current = false;
      if (aliveRef.current) { setBulkDeleting(false); mutationBusyChangeRef.current?.(false); }
    }
  };

  // Restore progress for an active job after refreshing; only orphaned jobs are stuck.
  const activeTaskId = submittedTaskId || stats?.current_task_id;
  useEffect(() => {
    const taskId = activeTaskId;
    if (!taskId) {
      if (!startingBuildRef.current) setBuilding(false);
      return;
    }
    setBuilding(true);
    let cancelled = false;
    let polling = false;
    const poll = setInterval(async () => {
      if (polling) return;
      polling = true;
      try {
        const progress = await api.getSyncProgress(taskId);
        if (cancelled) return;
        setBuildProgress(progress.progress || 0);
        setBuildTotal(progress.total || 0);
        setBuildMessage(progress.message || '正在处理视频…');
        if (!progress.success || progress.status === 'done' || progress.status === 'failed') {
          clearInterval(poll);
          setBuilding(false);
          setSubmittedTaskId(null);
          fetchStats();
          setVideosRefreshKey(key => key + 1);
          onBuildDone();
        }
      } catch {} finally { polling = false; }
    }, 800);
    return () => { cancelled = true; clearInterval(poll); };
  }, [activeTaskId, fetchStats, onBuildDone]);

  const statusLabel = (status: string) => ({done: '可提问', failed: '失败', pending: '待入库', downloading: '下载中', transcribing: '转写中', embedding: '保存中'} as Record<string, string>)[status] || '处理中';
  const videoActions = (video: api.VideoItem, inLibrary = false) => <ActionMenu label={`更多操作：${video.title}`}>

      {video.status === 'done' && <>
        <button onClick={() => handleExport(video.platform_item_id, 'original')}><FileDown size={15} />导出原文与时间</button>
        <button onClick={() => handleExport(video.platform_item_id, 'ai')}><ListChecks size={15} />AI 整理导出</button>
      </>}
      {inLibrary && selectedKnowledgeBase && <button onClick={() => removeFromBase(video)} disabled={chatBusy || Boolean(removingVideoId)}><FolderMinus size={15} />仅移出当前知识库</button>}
      {video.status === 'done' && <button className="danger-action" onClick={() => handleDelete(video.platform_item_id)} disabled={chatBusy}><Trash2 size={15} />删除全部入库资料</button>}
  </ActionMenu>;

  return (
    <div className="sources-sidebar h-full flex flex-col">
      <div className="flex-1 min-h-0 overflow-y-auto p-3 md:p-4">
        <KnowledgeBasesPanel selectedId={selectedKnowledgeBase?.id ?? null} refreshKey={statsRefreshKey + basesRefreshKey}
          disabled={chatBusy || chatOnly} disabledReason={chatBusy ? '请等待当前回答完成' : '体验模式尚未连接抖音；登录后可选择和管理知识库。'} onListLoaded={setKnowledgeBases} onSelect={onSelectKnowledgeBase}
          onRefresh={onBuildDone} onCreated={base => { baseCreated(base); onSelectKnowledgeBase(base); }}
          onBusyChange={onMutationBusyChange} onDeleted={id => {
            setBasesRefreshKey(key => key + 1);
            setVideosRefreshKey(key => key + 1);
            if (onKnowledgeBaseDeleted) onKnowledgeBaseDeleted(id);
            else if (selectedKnowledgeBase?.id === id) onSelectKnowledgeBase(null);
            onBuildDone();
          }} />
        <section className="history-section" aria-label="历史对话">
          <div className="flex items-center justify-between mb-3"><h2 className="section-title"><History size={16} />历史对话</h2><span className="text-xs text-[var(--color-ink-muted)]">自动保存</span></div>
          {historyError && <p role="alert" className="text-xs text-red-600">{historyError} <button onClick={fetchSessions} className="underline">重新加载</button></p>}
          {historyLoading && sessions.length === 0 && <p className="text-xs text-[var(--color-ink-muted)]">正在加载历史对话…</p>}
          {!historyLoading && !historyError && sessions.length === 0 && <p className="text-xs text-[var(--color-ink-muted)] leading-relaxed">还没有对话。提问后会自动保存，方便下次继续。</p>}
          <div id="workspace-history-list">
          {visibleHistory(sessions, activeSessionId, historyExpanded).map(s => <div key={s.id} className={`history-row ${activeSessionId === s.id ? 'active' : ''}`}>
            <button onClick={() => onSelectSession(s, knowledgeBases.find(base => base.id === s.knowledge_base_id) ?? null)} disabled={chatBusy || (s.knowledge_base_id != null && !knowledgeBases.some(base => base.id === s.knowledge_base_id))} aria-current={activeSessionId === s.id ? 'true' : undefined} title={s.title} className="history-open">
              <MessageCircle size={14} className="shrink-0" /><span className="truncate">{s.title}</span>
            </button>
            <ActionMenu label={`管理对话：${s.title}`}>
              <button onClick={() => setSessionAction({session: s, action: 'rename'})} disabled={chatBusy && activeSessionId === s.id}><Pencil size={15} />重命名</button>
              <button className="danger-action" onClick={() => setSessionAction({session: s, action: 'delete'})} disabled={chatBusy && activeSessionId === s.id}><Trash2 size={15} />删除对话</button>
            </ActionMenu>
          </div>)}
          </div>
          {sessions.length > 6 && <button type="button" className="text-button history-toggle" aria-expanded={historyExpanded} aria-controls="workspace-history-list" onClick={() => setHistoryExpanded(value => !value)}>{historyExpanded ? '收起历史' : `全部历史 (${sessions.length})`}</button>}
          {sessions.length === 100 && <p className="mt-2 text-xs text-[var(--color-ink-muted)]">显示最近 100 段对话</p>}
        </section>
      </div>
      <div className="sidebar-footer assistant-sidebar-shortcuts"><button className={`task-shortcut ${view === 'assistant' ? 'assistant-shortcut-active' : ''}`} onClick={() => onChangeView('assistant')} aria-current={view === 'assistant' ? 'page' : undefined}><Inbox size={17} /><span><strong>抖音助手</strong></span><ChevronRight size={15} /></button>{!chatOnly && <button className="task-shortcut" onClick={() => onChangeView('progress')} aria-current={view === 'progress' ? 'page' : undefined}><AudioLines size={17} /><span><strong>{building || processingCount ? '视频正在处理中' : '处理进度'}</strong><small>{doneCount} 可提问{failedCount ? ` · ${failedCount} 失败` : ''}</small></span><ChevronRight size={15} /></button>}</div>

      {contentContainer && view !== 'chat' && view !== 'assistant' && createPortal(<div className="source-workspace">
        {view === 'materials' && <>
          {!baseVideosLoading && !baseVideosError && baseVideos.length > 0 && <div className="materials-bulk-toolbar" aria-label="批量管理视频资料">
            <label><input type="checkbox" checked={allMaterialsSelected} ref={element => { if (element) element.indeterminate = selectedMaterialIds.size > 0 && !allMaterialsSelected; }} onChange={toggleAllMaterials} disabled={mutationBlocked || !selectableMaterials.length} aria-label="全选可删除资料" />全选</label>
            <span>已选 {selectedMaterialIds.size} 项</span>
            <div>{selectedKnowledgeBase && <button type="button" className="ui-button" onClick={() => void bulkDeleteMaterials('library')} disabled={mutationBlocked || !bulkDeleteSupported || !selectedMaterialIds.size} title={!bulkDeleteSupported ? '后端更新加载后可批量操作' : undefined}>批量移出</button>}
            <button type="button" className="ui-button danger-action" onClick={() => void bulkDeleteMaterials('global')} disabled={mutationBlocked || !bulkDeleteSupported || !selectedMaterialIds.size} title={!bulkDeleteSupported ? '后端更新加载后可批量操作' : undefined}><Trash2 size={15} />{bulkDeleting ? '正在处理…' : '批量删除'}</button></div>
          </div>}
          {bulkFeedback && <p role="status" className="materials-bulk-feedback">{bulkFeedback}</p>}
          {baseVideosLoading ? <p role="status" className="empty-hint">正在加载视频资料…</p> : baseVideosError ? <p role="alert" className="empty-hint text-red-600">{baseVideosError} <button onClick={() => setBasesRefreshKey(key => key + 1)} className="underline">重新加载</button></p> : baseVideos.length === 0 ? <div className="empty-resource"><FileAudio size={30} /><h3>还没有可查看的视频</h3><p>从收藏夹选择视频，导入到知识库后就能查看文字稿。</p><button className="ui-button ui-button-primary" onClick={() => onChangeView('collections')}>从收藏夹导入</button></div> : <div className="resource-list" aria-label="视频资料列表">{baseVideos.map(video => <div key={video.platform_item_id} className={`resource-row ${selectedMaterialIds.has(video.platform_item_id) ? 'material-selected' : ''}`}>
            <input type="checkbox" checked={selectedMaterialIds.has(video.platform_item_id)} onChange={() => toggleMaterial(video.platform_item_id)} disabled={mutationBlocked || video.status !== 'done'} aria-label={`选择资料：${video.title || '未命名视频'}`} />
            <span className="resource-icon"><FileAudio size={20} /></span><button className="resource-title" onClick={() => setViewingItemId(video.platform_item_id)}><strong>{video.title || '未命名视频'}</strong><small>{video.author ? `@${video.author} · ` : ''}{video.duration ? `${Math.floor(video.duration / 60)}:${String(video.duration % 60).padStart(2, '0')} · ` : ''}查看完整识别结果</small></button>
            <span className={`status-badge ${video.status === 'failed' ? 'status-failed' : ''}`}>{video.status === 'done' && <Check size={13} />}{statusLabel(video.status)}</span>{videoActions(video, true)}
          </div>)}</div>}
          <p className="mt-5 text-xs text-[var(--color-ink-muted)] leading-relaxed">“移出”仅调整当前知识库；“删除全部入库资料”会影响所有知识库。原始导出保留完整文字稿与时间，AI 整理导出会额外调用模型。</p>
        </>}
        {view === 'collections' && <>
          <div className="page-section-heading"><div><h3>选择收藏的视频</h3><p>支持跨收藏夹选择，再统一指定目标知识库。</p></div><label className="collection-title-search"><span>搜索视频标题</span><input type="search" value={collectionSearch} onChange={event => setCollectionSearch(event.target.value)} placeholder="输入视频标题关键词" aria-label="搜索收藏视频标题" /></label><button className="ui-button" onClick={handleSync} disabled={syncing || building}><RefreshCw size={15} className={syncing ? 'animate-spin' : ''} />{syncing ? '同步中…' : '同步收藏夹'}</button></div>
          <div className="collection-workspace"><nav className="collection-navigation" aria-label="抖音收藏夹">{collections.map(col => <button key={col.collection_id} onClick={() => handleCollectionClick(col.collection_id)} disabled={chatBusy} aria-pressed={expandedId === col.collection_id} className={expandedId === col.collection_id ? 'active' : ''}><span>{col.title}</span><small>{col.video_count}</small></button>)}{collections.length === 0 && <p className="empty-hint">还没有收藏夹，点击同步后再查看。</p>}</nav>
          <div className="min-w-0">
            {loadingVideos ? <p role="status" className="empty-hint">正在加载视频…</p> : videoLoadError ? <p role="alert" className="empty-hint text-red-600">{videoLoadError} <button onClick={() => setVideosRefreshKey(key => key + 1)} className="underline">重新加载</button></p> : !expandedId ? <p className="empty-hint">选择一个收藏夹查看视频。</p> : expandedVideos.length === 0 ? <p className="empty-hint">这个收藏夹暂无视频。</p> : <>
              <div className="collection-selection-heading"><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={allVisibleSelected} onChange={toggleCollection} disabled={building || !selectableVideos.length} aria-label={searchTitle ? '全选搜索结果' : '全选本收藏夹'} />{searchTitle ? '全选搜索结果' : '全选本收藏夹'}</label><span className="collection-search-count">{visibleCollectionVideos.length} / {expandedVideos.length} 个视频</span><button className="text-button" title={`导入这个收藏夹的全部 ${expandedVideos.length} 个视频，不受搜索筛选影响`} onClick={() => openBuildConfirm(expandedVideos, sourceForCollection(expandedId, expandedVideos))} disabled={building || chatBusy}>整夹导入<ChevronRight size={14} /></button></div>
              {searchTitle && visibleCollectionVideos.length === 0 && <p className="empty-hint" role="status">没有找到标题匹配的视频。</p>}
              {visibleCollectionVideos.map(video => <div key={video.platform_item_id} className={`collection-video-row ${selectedVideos.has(video.platform_item_id) ? 'selected' : ''}`}>
                <input type="checkbox" checked={selectedVideos.has(video.platform_item_id)} onChange={() => toggleVideo(video)} disabled={building || !['pending', 'failed', 'done'].includes(video.status)} aria-label={`选择视频：${video.title}`} />
                <button className="resource-title" onClick={() => setViewingItemId(video.platform_item_id)}><strong>{video.title || '未命名视频'}</strong><small>{video.author ? `@${video.author}` : '作者未知'}{video.duration ? ` · ${Math.floor(video.duration / 60)}:${String(video.duration % 60).padStart(2, '0')}` : ''}</small></button>
                <button onClick={() => setViewingItemId(video.platform_item_id)} className={`status-badge ${video.status === 'failed' ? 'status-failed' : ''}`} aria-label={video.status === 'failed' ? `查看入库失败原因：${video.title}` : `查看视频状态：${video.title}`}>{video.status === 'failed' ? '查看失败原因' : statusLabel(video.status)}</button>{videoActions(video)}
              </div>)}
              <button className="text-button mt-5" onClick={() => onSelectCollection(expandedId)} disabled={chatBusy}><MessageCircle size={15} />在此收藏夹的已入库资料中提问</button>
            </>}
          </div></div>
          <div className="selection-toolbar"><div><strong>已选 {selectedVideos.size} 个视频</strong><small>切换收藏夹保留已选视频，确认导入时选择知识库。</small></div><button className="text-button" onClick={() => setSelectedVideos(new Map())} disabled={!selectedVideos.size || building}>清空</button><button className="ui-button ui-button-primary" onClick={() => openBuildConfirm([...selectedVideos.values()])} disabled={!selectedVideos.size || building || chatBusy}><Download size={16} />导入所选视频 ({selectedVideos.size})</button></div>
          <button className="text-button mt-4" onClick={() => openBuildConfirm(undefined, sourceForCollection('all', []))} disabled={allFavoriteCount === 0 || building || chatBusy}>导入全部收藏 ({allFavoriteCount})</button>
        </>}
        {view === 'progress' && <>
          <div className="page-section-heading processing-section-heading"><p>下载视频 → 识别转文字 → 建立索引</p><button className="ui-button" onClick={onBuildDone}><RefreshCw size={15} />刷新状态</button></div>
          <div className="processing-summary"><span>{doneCount} 可提问</span><span>{processingCount} 处理中</span><span>{pendingCount} 待入库</span><span>{failedCount} 失败</span><small>{totalCount} 个视频总计</small></div>
          {building && <div className="processing-current"><div className="flex gap-3 items-start"><AudioLines size={22} /><div className="min-w-0"><strong>正在处理视频</strong><p className="mt-2 text-sm break-words">{buildMessage || '正在读取进度…'}</p></div></div><div className="processing-track"><div style={{width: `${buildTotal ? buildProgress / buildTotal * 100 : 0}%`}} /></div><p className="mt-2 text-xs text-[var(--color-ink-muted)]">已完成 {buildProgress} / {buildTotal} 个视频</p></div>}
          {taskVideosLoading && <p role="status" className="empty-hint">正在读取视频状态…</p>}
          {taskVideosError && <p role="alert" className="text-sm text-red-600">{taskVideosError} <button className="underline" onClick={() => setVideosRefreshKey(key => key + 1)}>重新加载</button></p>}
          <div className="resource-list">{taskVideos.filter(video => video.status !== 'done').map(video => <div key={video.platform_item_id} className="resource-row flex-wrap"><span className="resource-icon"><FileAudio size={19} /></span><button className="resource-title" onClick={() => setViewingItemId(video.platform_item_id)}><strong>{video.title || '未命名视频'}</strong><small>{statusLabel(video.status)}</small></button>{video.status === 'failed' && <><button className="ui-button" onClick={() => setViewingItemId(video.platform_item_id)}>查看原因</button><button className="ui-button" disabled={building || chatBusy} onClick={() => { setSelectedVideos(previous => new Map(previous).set(video.platform_item_id, video)); const col = collections.find(item => item.id === Number(video.collection_id)); setExpandedId(col?.collection_id ?? 'all'); onChangeView('collections'); }}>选择重试<RotateCcw size={14} /></button></>}</div>)}</div>
          {taskVideosTotal > taskVideos.length && <p className="mt-3 text-xs text-[var(--color-ink-muted)]">此处展示最近 {taskVideos.length} 个视频的未完成项。其他视频也可以从收藏夹查看。</p>}
          {(isStuck || failedCount > 0) && <div className="mt-6"><button onClick={handleResetInterrupted} disabled={resettingVideos || building} aria-busy={resettingVideos} className="ui-button"><RefreshCw size={15} />{resettingVideos ? '正在重置…' : '重置失败/卡住的视频'}</button><p className="mt-2 text-xs text-[var(--color-ink-muted)]">任务进行期间不能重置；重置后重新选择需要导入的视频。</p></div>}
          {buildMessage && !building && <p role="status" className="mt-4 text-sm text-[var(--color-ink-soft)]">{buildMessage}</p>}
          {!building && !failedCount && !pendingCount && !taskVideosLoading && <div className="empty-resource"><Check size={28} /><h3>资料已准备好</h3><p>选择知识库，就可以开始提问。</p></div>}
        </>}
      </div>, contentContainer)}

      {createPortal(<>
      {showBuildConfirm && (
        <BuildConfirmModal
          pendingCount={pendingCount}
          selectedVideos={buildSelection}
          sourceCollection={buildSource}
          knowledgeBases={knowledgeBases}
          selectedKnowledgeBaseId={selectedKnowledgeBase?.id ?? null}
          onKnowledgeBaseCreated={baseCreated}
          analysisMode={analysisMode}
          onClose={() => setShowBuildConfirm(false)}
          onConfirm={(selectedIds, knowledgeBaseId, targetBase) => {
            setShowBuildConfirm(false);
            handleBuild(selectedIds, knowledgeBaseId, targetBase);
          }}
        />
      )}
      {viewingItemId && <VideoContentModal itemId={viewingItemId} onClose={() => setViewingItemId(null)} />}
      {sessionAction && <SessionActionModal session={sessionAction.session} action={sessionAction.action}
        onClose={() => setSessionAction(null)}
        onRenamed={(id, title) => {
          historyRequestRef.current++;
          setSessions(previous => previous.map(session => session.id === id ? {...session, title} : session));
          onSessionRenamed(id, title);
        }}
        onDeleted={id => {
          historyRequestRef.current++;
          setSessions(previous => previous.filter(session => session.id !== id));
          onSessionDeleted(id);
        }} />}
      </>, document.body)}
    </div>
  );
}
