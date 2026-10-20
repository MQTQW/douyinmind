import { useState, useEffect, useCallback, useRef } from 'react';
import * as api from '../api';
import BuildConfirmModal from './BuildConfirmModal';
import VideoContentModal from './VideoContentModal';
import SessionActionModal from './SessionActionModal';
import KnowledgeBasesPanel from './KnowledgeBasesPanel';
import type { CollectionImportSource } from '../collectionImport';

interface Props {
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
}

export default function SourcesPanel({ onBuildDone, selectedId, onSelectCollection, selectedKnowledgeBase, onSelectKnowledgeBase, statsRefreshKey, historyRefreshKey, activeSessionId, chatBusy, onSelectSession, onNewChat, onSessionRenamed, onSessionDeleted }: Props) {
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

  const baseCreated = (base: api.KnowledgeBaseItem) => {
    setKnowledgeBases(previous => [...previous.filter(item => item.id !== base.id), base]);
    setBasesRefreshKey(key => key + 1);
  };
  useEffect(() => {
    if (!selectedKnowledgeBase) { setBaseVideos([]); return; }
    let active = true;
    setBaseVideos([]);
    setBaseVideosLoading(true);
    setBaseVideosError('');
    api.listAllKnowledgeBaseVideos(selectedKnowledgeBase.id).then(videos => {
      if (active) setBaseVideos(videos);
    }).catch(() => {
      if (active) setBaseVideosError('知识库内容加载失败，请重试。');
    }).finally(() => { if (active) setBaseVideosLoading(false); });
    return () => { active = false; };
  }, [selectedKnowledgeBase?.id, statsRefreshKey, basesRefreshKey]);

  const removeFromBase = async (video: api.VideoItem) => {
    if (!selectedKnowledgeBase || removingVideoId) return;
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
    onSelectCollection(collectionId);
    setExpandedId(previous => previous === collectionId ? null : collectionId);
  };

  const selectableVideos = expandedVideos.filter(video => ['pending', 'failed', 'done'].includes(video.status));
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
      await api.deleteVideo(platformItemId);
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

  return (
    <div className="h-full flex flex-col bg-[var(--color-panel)] border-r border-[var(--color-border)]">
      <div className="flex-1 flex flex-col min-h-0 p-4 gap-3 overflow-y-auto">
        <KnowledgeBasesPanel selectedId={selectedKnowledgeBase?.id ?? null} refreshKey={statsRefreshKey + basesRefreshKey}
          disabled={chatBusy} onListLoaded={setKnowledgeBases} onSelect={onSelectKnowledgeBase}
          onRefresh={onBuildDone}
          onCreated={base => { baseCreated(base); onSelectKnowledgeBase(base); }} />
        {selectedKnowledgeBase && <div className="rounded-xl border border-[var(--color-border)] bg-white/50 p-3">
          <h3 className="text-xs font-semibold text-[var(--color-ink)]">{selectedKnowledgeBase.name} · 视频资料</h3>
          {baseVideosLoading ? <p className="mt-2 text-xs text-[var(--color-ink-muted)]">正在加载…</p>
            : baseVideosError ? <p role="alert" className="mt-2 text-xs text-red-600">{baseVideosError} <button onClick={() => setBasesRefreshKey(key => key + 1)} className="underline">重试</button></p>
            : baseVideos.length === 0 ? <p className="mt-2 text-xs leading-relaxed text-[var(--color-ink-muted)]">还没有视频。从下方收藏夹勾选视频，再导入到这个知识库。</p>
            : <div className="mt-2 max-h-56 overflow-y-auto space-y-1.5">{baseVideos.map(video => <div key={video.platform_item_id} className="flex items-center gap-2 text-xs">
              <button onClick={() => setViewingItemId(video.platform_item_id)} title={video.title} className="min-w-0 flex-1 truncate text-left text-[var(--color-ink)] hover:text-accent">{video.title}</button>
              <span className={video.status === 'done' ? 'text-success' : 'text-[var(--color-ink-muted)]'}>{video.status === 'done' ? '已完成' : video.status === 'failed' ? '失败' : '处理中'}</span>
              <button onClick={() => removeFromBase(video)} disabled={chatBusy || Boolean(removingVideoId)} title="只从这个知识库移出，保留原文字稿" className="flex-shrink-0 text-[var(--color-ink-muted)] hover:text-accent disabled:opacity-40">{removingVideoId === video.platform_item_id ? '移出中' : '移出'}</button>
            </div>)}</div>}
          <p className="mt-2 text-[10px] text-[var(--color-ink-muted)]">移出只调整当前知识库，原文字稿仍保留。</p>
        </div>}
        <hr className="border-[var(--color-border)]" />
        {/* Header */}
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-bold text-[var(--color-ink)]">📁 我的收藏夹</h2>
          <button
            onClick={handleSync}
            disabled={syncing}
            className="flex items-center gap-1 px-3 py-1.5 rounded-full text-xs text-[var(--color-ink-soft)] bg-black/3 hover:bg-black/6 transition-colors disabled:opacity-50"
          >
            {syncing ? '⏳' : '🔄'} 同步
          </button>
        </div>

        <hr className="border-[var(--color-border)]" />

        {/* Collections */}
        <p className="text-[11px] text-[var(--color-ink-muted)]">展开收藏夹，勾选要入库的视频</p>
        <div className="flex flex-col gap-2">
          {collections.map(col => {
            const isSelected = selectedId === col.collection_id;
            const isExpanded = expandedId === col.collection_id;
            return (
              <div key={col.collection_id}>
                <button
                  onClick={() => handleCollectionClick(col.collection_id)}
                  disabled={chatBusy}
                  className={`w-full text-left p-2.5 rounded-xl transition-all border ${
                    isSelected
                      ? 'border-accent/40 bg-accent-light shadow-sm'
                      : 'border-transparent hover:border-[var(--color-border)] hover:bg-white/60'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-semibold text-[var(--color-ink)] truncate flex-1">{col.title}</span>
                    <span className="text-[11px] text-[var(--color-ink-muted)] ml-2 flex-shrink-0">
                      {col.video_count} 个 {isExpanded ? '▲' : '▼'}
                    </span>
                  </div>
                </button>
                {/* Expanded video list */}
                {isExpanded && (
                  <div className="ml-3 mt-1 border-l-2 border-[var(--color-border)] pl-3 py-1">
                    {loadingVideos ? (
                      <p className="text-xs text-[var(--color-ink-muted)] py-2">加载中...</p>
                    ) : videoLoadError ? (
                      <div className="py-2 text-xs text-red-500">
                        <p>{videoLoadError}</p>
                        <button onClick={() => setVideosRefreshKey(key => key + 1)} className="mt-1 underline">重新加载</button>
                      </div>
                    ) : expandedVideos.length === 0 ? (
                      <p className="text-xs text-[var(--color-ink-muted)] py-2">暂无视频，请先同步</p>
                    ) : (
                      <>
                      {selectableVideos.length > 0 && (
                        <div className="flex items-center justify-between gap-2 pb-1.5 text-[11px]">
                          <button onClick={toggleCollection} disabled={building} className="text-accent hover:underline disabled:opacity-40">
                            {allVisibleSelected ? '取消本收藏夹选择' : '全选本收藏夹'}
                          </button>
                          <button onClick={() => openBuildConfirm(expandedVideos, sourceForCollection(col.collection_id, expandedVideos))}
                            disabled={building || chatBusy} title={`全部导入，并自动使用“${col.title}”作为知识库名称`}
                            className="text-accent hover:underline disabled:opacity-40">整夹导入</button>
                          <span className="text-[var(--color-ink-muted)]">{selectableVideos.filter(video => selectedVideos.has(video.platform_item_id)).length} / {selectableVideos.length}</span>
                        </div>
                      )}
                      {expandedVideos.map(v => (
                        <div key={v.platform_item_id} className={`flex items-center justify-between py-1.5 gap-2 rounded-md ${selectedVideos.has(v.platform_item_id) ? 'bg-accent-light' : ''}`}>
                          <input
                            type="checkbox"
                            checked={selectedVideos.has(v.platform_item_id)}
                            onChange={() => toggleVideo(v)}
                            disabled={building || !['pending', 'failed', 'done'].includes(v.status)}
                            aria-label={`选择视频：${v.title}`}
                            title={v.status === 'done' ? '已有文字稿，可直接加入知识库' : '选择这个视频导入知识库'}
                            className="w-4 h-4 flex-shrink-0 cursor-pointer accent-[var(--color-accent)] disabled:cursor-default disabled:opacity-40"
                          />
                          <button onClick={() => setViewingItemId(v.platform_item_id)} className="text-xs text-[var(--color-ink)] truncate flex-1 text-left hover:text-accent" title={v.status === 'failed' ? '查看入库失败原因' : `查看${resultDescription}`}>{v.title}</button>
                          {v.status === 'done' && (
                            <div className="flex gap-1 flex-shrink-0">
                              <button onClick={() => handleExport(v.platform_item_id, 'original')}
                                className="text-[10px] px-1.5 py-0.5 rounded hover:bg-black/5 transition-colors text-[var(--color-ink-muted)]"
                                title={`导出${resultDescription}`}>📄</button>
                              <button onClick={() => handleExport(v.platform_item_id, 'ai')}
                                className="text-[10px] px-1.5 py-0.5 rounded hover:bg-black/5 transition-colors text-[var(--color-ink-muted)]"
                                title="AI 整理导出">✨</button>
                              <button onClick={() => handleDelete(v.platform_item_id)}
                                className="text-[10px] px-1.5 py-0.5 rounded hover:bg-red-50 transition-colors text-[var(--color-ink-muted)] hover:text-red-500"
                                title="删除入库数据">🗑️</button>
                            </div>
                          )}
                          {v.status === 'failed' ? (
                            <button onClick={() => setViewingItemId(v.platform_item_id)}
                              aria-label={`查看入库失败原因：${v.title}`}
                              className="text-[10px] ml-1 px-2 py-1 rounded-full flex-shrink-0 bg-red-50 text-red-600 hover:bg-red-100 transition-colors">
                              ✗ 查看原因
                            </button>
                          ) : <span title={({done: '已入库', pending: '待入库'} as Record<string, string>)[v.status]} className={`text-[10px] ml-1 px-1.5 py-0.5 rounded-full flex-shrink-0 ${
                            v.status === 'done' ? 'bg-success-light text-success' :
                            v.status === 'pending' ? 'bg-amber-light text-amber' :
                            'bg-black/5 text-[var(--color-ink-muted)]'
                          }`}>
                            {({done: '✓', pending: '⏳', downloading: '下载中', transcribing: '识别中', embedding: '保存中'} as Record<string, string>)[v.status] || '处理中'}
                          </span>}
                        </div>
                      ))}
                      </>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <hr className="border-[var(--color-border)]" />

        {/* Build Section */}
        <div className="flex flex-col gap-2">
          <h3 className="text-xs text-[var(--color-ink-soft)]">📥 {building ? '入库进度' : '入库状态'}</h3>

          {building ? (
            <>
              <p className="text-[11px] text-[var(--color-ink)] truncate">{buildMessage}</p>
              <div className="h-2 rounded-full bg-black/5 overflow-hidden">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-accent to-amber transition-all duration-700 ease-linear"
                  style={{ width: `${buildTotal ? (buildProgress / buildTotal) * 100 : 0}%` }}
                />
              </div>
              <div className="flex justify-between text-[11px]">
                <span className="text-[var(--color-ink)] font-semibold">{buildProgress} / {buildTotal}</span>
                <span className="text-accent font-bold">{buildTotal ? Math.round((buildProgress / buildTotal) * 100) : 0}%</span>
              </div>
              <button disabled className="w-full py-3 rounded-full text-sm font-bold transition-all opacity-40 cursor-not-allowed"
                style={{ backgroundColor: 'rgba(0,0,0,0.06)', color: 'var(--color-ink-muted)' }}>
                ⏳ 入库中...
              </button>
            </>
          ) : (
            <>
              <p className="text-[11px] text-[var(--color-ink-muted)]">
                ✅ {doneCount} 已入库 · ⏳ {pendingCount} 待入库{failedCount > 0 ? ` · ❌ ${failedCount} 失败` : ''}{isStuck ? ' · ⚠️ 卡住' : ''} · 📊 {totalCount} 总计
              </p>
              <div className="h-1.5 rounded-full bg-black/5">
                <div className="h-full rounded-full bg-gradient-to-r from-accent to-amber transition-all duration-500" style={{ width: `${totalCount ? (doneCount / totalCount) * 100 : 0}%` }} />
              </div>
              <div className="flex items-center justify-between gap-2 text-xs pt-1">
                <span className="font-semibold text-[var(--color-ink)]">已选 {selectedVideos.size} 个视频</span>
                <button onClick={() => setSelectedVideos(new Map())} disabled={selectedVideos.size === 0} className="text-[var(--color-ink-muted)] hover:text-accent disabled:opacity-40">清空选择</button>
              </div>
              <p className="text-[11px] text-[var(--color-ink-muted)]">可跨收藏夹勾选，导入时选择目标知识库</p>
              {failedCount > 0 && <p className="text-[11px] text-red-600">失败的视频可点“查看原因”，处理问题后再勾选重试。</p>}
              {isStuck && (
                <button
                  onClick={handleResetInterrupted}
                  disabled={resettingVideos}
                  aria-busy={resettingVideos}
                  className="w-full py-2 rounded-full text-xs text-accent border border-accent/30 hover:bg-accent-light transition-colors disabled:opacity-50"
                >
                  {resettingVideos ? '正在重置…' : '🔄 重置失败/卡住的视频'}
                </button>
              )}
              <button
                onClick={() => openBuildConfirm([...selectedVideos.values()])}
                disabled={selectedVideos.size === 0}
                className="w-full py-3 rounded-full bg-gradient-to-r from-accent to-accent-hover text-white text-sm font-bold shadow-md shadow-accent/20 hover:shadow-lg transition-all disabled:opacity-40 disabled:cursor-not-allowed"
              >
                🚀 导入所选视频 ({selectedVideos.size})
              </button>
              <button
                onClick={() => openBuildConfirm(undefined, sourceForCollection('all', []))}
                disabled={allFavoriteCount === 0}
                className="w-full py-2 rounded-full text-xs text-[var(--color-ink-soft)] border border-[var(--color-border)] hover:bg-white/60 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                导入全部收藏 ({allFavoriteCount})
              </button>
              {buildMessage && <p role="status" className="text-[11px] text-[var(--color-ink-muted)]">{buildMessage}</p>}
            </>
          )}
        </div>

        {/* History */}
        <>
            <hr className="border-[var(--color-border)]" />
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-xs text-[var(--color-ink-soft)]">💬 历史对话</h3>
                <button onClick={onNewChat} disabled={chatBusy} className="rounded-full border border-[var(--color-border)] px-2.5 py-1 text-xs text-accent hover:bg-accent-light disabled:opacity-40">＋ 新对话</button>
              </div>
              {historyError && <p role="alert" className="text-xs text-red-600">{historyError} <button onClick={fetchSessions} className="underline">重新加载</button></p>}
              {historyLoading && sessions.length === 0 && <p className="py-2 text-xs text-[var(--color-ink-muted)]">正在加载历史对话…</p>}
              {!historyLoading && !historyError && sessions.length === 0 && <p className="py-2 text-xs text-[var(--color-ink-muted)]">还没有历史对话，开始聊天后会自动保存。</p>}
              {sessions.map(s => (
                <div key={s.id} className={`flex items-center gap-1 rounded-lg border text-xs transition-colors ${activeSessionId === s.id ? 'border-accent/40 bg-accent-light' : 'border-transparent hover:bg-black/3'}`}>
                  <button onClick={() => onSelectSession(s, knowledgeBases.find(base => base.id === s.knowledge_base_id) ?? null)} disabled={chatBusy || (s.knowledge_base_id != null && !knowledgeBases.some(base => base.id === s.knowledge_base_id))} aria-current={activeSessionId === s.id ? 'true' : undefined}
                    title={chatBusy ? '请等待当前回答完成' : s.title} className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left disabled:opacity-60">
                    <span className="truncate flex-1 text-[var(--color-ink)]">{s.title}</span>
                    <span title={`${s.message_count} 条消息`} className="flex-shrink-0 text-[var(--color-ink-muted)]">{s.message_count}</span>
                  </button>
                  <button onClick={() => setSessionAction({session: s, action: 'rename'})} disabled={chatBusy && activeSessionId === s.id}
                    aria-label={`重命名对话：${s.title}`} title="重命名" className="h-7 w-7 flex-shrink-0 rounded-md text-[var(--color-ink-muted)] hover:bg-white hover:text-accent disabled:opacity-40">✎</button>
                  <button onClick={() => setSessionAction({session: s, action: 'delete'})} disabled={chatBusy && activeSessionId === s.id}
                    aria-label={`删除对话：${s.title}`} title="删除" className="mr-1 h-7 w-7 flex-shrink-0 rounded-md text-[var(--color-ink-muted)] hover:bg-red-50 hover:text-red-600 disabled:opacity-40">🗑</button>
                </div>
              ))}
              {sessions.length === 100 && <p className="text-[11px] text-[var(--color-ink-muted)]">显示最近 100 段对话</p>}
            </div>
        </>
      </div>

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
    </div>
  );
}
