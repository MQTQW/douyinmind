import { useState, useRef, useCallback, useEffect } from 'react';
import SourcesPanel from '../components/SourcesPanel';
import ChatPanel from '../components/ChatPanel';
import * as api from '../api';
import { subscribePageReturn } from '../pageRefresh';

interface Props { onLogout: () => void; loggedIn: boolean; }

export default function Workspace({ onLogout, loggedIn }: Props) {
  const [leftWidth, setLeftWidth] = useState(340);
  const [isDragging, setIsDragging] = useState(false);
  const [selectedCollectionId, setSelectedCollectionId] = useState<string>('all');
  const [selectedKnowledgeBase, setSelectedKnowledgeBase] = useState<api.KnowledgeBaseItem | null>(null);
  const [statsRefreshKey, setStatsRefreshKey] = useState(0);
  const [statusMsg, setStatusMsg] = useState(loggedIn ? '已登录' : 'AI 对话体验 · 尚未登录抖音');
  const [showSources, setShowSources] = useState(false);
  const [currentSession, setCurrentSession] = useState<{id: number; title: string} | null>(null);
  const [openedSessionId, setOpenedSessionId] = useState<number | null>(null);
  const [conversationVersion, setConversationVersion] = useState(0);
  const [historyRefreshKey, setHistoryRefreshKey] = useState(0);
  const [chatBusy, setChatBusy] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleBuildDone = useCallback(() => setStatsRefreshKey(key => key + 1), []);
  // Background imports may finish while this page is idle or another tab is open.
  // Refresh source data on return without replacing the selected chat/library.
  useEffect(() => subscribePageReturn(handleBuildDone), [handleBuildDone]);
  const refreshHistory = useCallback(() => setHistoryRefreshKey(key => key + 1), []);
  const startNewChat = useCallback(() => {
    if (chatBusy) return;
    setCurrentSession(null);
    setOpenedSessionId(null);
    setConversationVersion(version => version + 1);
    setShowSources(false);
  }, [chatBusy]);
  const selectKnowledgeBase = useCallback((base: api.KnowledgeBaseItem | null) => {
    if (chatBusy) return;
    if ((selectedKnowledgeBase?.id ?? null) !== (base?.id ?? null)) {
      setCurrentSession(null);
      setOpenedSessionId(null);
      setConversationVersion(version => version + 1);
    }
    setSelectedKnowledgeBase(base);
    setSelectedCollectionId('all');
  }, [chatBusy, selectedKnowledgeBase?.id]);
  const selectCollection = useCallback((id: string) => {
    if (chatBusy) return;
    setSelectedCollectionId(id);
  }, [chatBusy]);
  const selectSession = useCallback((session: api.SessionItem, base: api.KnowledgeBaseItem | null = null) => {
    if (chatBusy) return;
    if (currentSession?.id !== session.id) {
      setSelectedKnowledgeBase(base);
      setSelectedCollectionId('all');
      setCurrentSession({id: session.id, title: session.title});
      setOpenedSessionId(session.id);
      setConversationVersion(version => version + 1);
    }
    setShowSources(false);
  }, [chatBusy, currentSession?.id]);
  const sessionSaved = useCallback((id: number, title: string) => {
    setCurrentSession(previous => previous?.id === id ? previous : {id, title});
    refreshHistory();
  }, [refreshHistory]);
  const sessionRenamed = useCallback((id: number, title: string) => {
    setCurrentSession(previous => previous?.id === id ? {id, title} : previous);
    refreshHistory();
  }, [refreshHistory]);
  const sessionDeleted = useCallback((id: number) => {
    if (currentSession?.id === id) startNewChat();
    refreshHistory();
  }, [currentSession?.id, startNewChat, refreshHistory]);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  // Poll login status for detailed message
  useEffect(() => {
    const poll = setInterval(async () => {
      try {
        const s = await api.loginStatus();
        if (s.message) setStatusMsg(s.message);
      } catch {}
    }, 3000);
    return () => clearInterval(poll);
  }, []);

  useEffect(() => {
    if (!isDragging) return;
    const move = (e: MouseEvent) => {
      if (!containerRef.current) return;
      const r = containerRef.current.getBoundingClientRect();
      setLeftWidth(Math.max(240, Math.min(r.width * 0.5, e.clientX - r.left)));
    };
    const up = () => setIsDragging(false);
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    return () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isDragging]);

  return (
    <div className="h-screen flex flex-col bg-[var(--color-bg)]">
      {/* Top Bar */}
      <header className="flex flex-wrap items-center justify-between gap-3 px-4 md:px-7 py-3.5 bg-white/80 backdrop-blur border-b border-[var(--color-border)] flex-shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl flex items-center justify-center bg-gradient-to-br from-accent to-amber shadow-lg shadow-accent/20">
            <span className="font-display text-white text-xl font-bold">D</span>
          </div>
          <div>
            <h1 className="font-display text-base md:text-lg text-[var(--color-ink)]">DouyinMind · 收藏夹知识库</h1>
            <span className="text-[10px] text-[var(--color-ink-muted)] tracking-[0.15em] uppercase">Save · Learn · Ask</span>
          </div>
        </div>
        <div className="flex items-center gap-2 min-w-0 max-md:w-full">
          <button
            onClick={() => setShowSources(value => !value)}
            className="md:hidden flex-shrink-0 px-3 py-1.5 rounded-full text-xs border border-[var(--color-border)] bg-white/70"
            aria-expanded={showSources}
          >
            {showSources ? '返回聊天' : '📁 资料库'}
          </button>
          <span className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs max-w-md truncate"
            style={{ backgroundColor: 'rgba(91,140,90,0.12)', color: 'var(--color-success)' }}>
            <span className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ backgroundColor: 'var(--color-success)' }} /> {statusMsg}
          </span>
          <button onClick={onLogout} className="w-8 h-8 rounded-full border border-[var(--color-border)] bg-white/70 flex items-center justify-center hover:border-accent/50 transition-colors text-sm text-[var(--color-ink-muted)]">
            ⏻
          </button>
        </div>
      </header>

      {/* Main Area */}
      <div ref={containerRef} className="flex-1 flex min-h-0" style={{ userSelect: isDragging ? 'none' : undefined }}>
        {/* Left Panel */}
        <aside style={{ width: leftWidth, flexShrink: 0 }} className={`${showSources ? 'block' : 'hidden'} md:block h-full max-md:!w-full`}>
          <SourcesPanel
            onBuildDone={handleBuildDone}
            selectedId={selectedCollectionId}
            onSelectCollection={selectCollection}
            selectedKnowledgeBase={selectedKnowledgeBase}
            onSelectKnowledgeBase={selectKnowledgeBase}
            statsRefreshKey={statsRefreshKey}
            historyRefreshKey={historyRefreshKey}
            activeSessionId={currentSession?.id ?? null}
            chatBusy={chatBusy}
            onSelectSession={selectSession}
            onNewChat={startNewChat}
            onSessionRenamed={sessionRenamed}
            onSessionDeleted={sessionDeleted}
          />
        </aside>

        {/* Resizer */}
        <div onMouseDown={handleMouseDown} className="hidden md:flex w-2 flex-shrink-0 cursor-col-resize relative group items-center justify-center">
          <div className="w-1 h-8 rounded-full bg-[var(--color-border)] group-hover:h-14 group-hover:bg-accent transition-all" />
        </div>

        {/* Right Panel */}
        <main className={`${showSources ? 'hidden' : 'block'} md:block flex-1 h-full min-w-0`}>
          <ChatPanel key={conversationVersion} collectionId={selectedKnowledgeBase ? 'all' : selectedCollectionId} statsRefreshKey={statsRefreshKey} chatOnly={!loggedIn}
            knowledgeBaseId={selectedKnowledgeBase?.id ?? null} knowledgeBaseName={selectedKnowledgeBase?.name ?? ''}
            openedSessionId={openedSessionId} sessionTitle={currentSession?.title ?? '新对话'}
            onSessionSaved={sessionSaved} onHistoryChanged={refreshHistory} onBusyChange={setChatBusy} onNewChat={startNewChat} />
        </main>
      </div>
    </div>
  );
}
