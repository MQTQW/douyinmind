import { useState, useRef, useCallback, useEffect } from 'react';
import SourcesPanel from '../components/SourcesPanel';
import ChatPanel from '../components/ChatPanel';
import * as api from '../api';
import { subscribePageReturn } from '../pageRefresh';
import { BookOpen, ChevronLeft, Download, Files, LogOut, Menu, MessagesSquare, Plus } from 'lucide-react';
import type { WorkspaceView } from '../workspaceView';

interface Props { onLogout: () => void; loggedIn: boolean; }

export default function Workspace({ onLogout, loggedIn }: Props) {
  const [leftWidth, setLeftWidth] = useState(272);
  const [view, setView] = useState<WorkspaceView>('chat');
  const [contentContainer, setContentContainer] = useState<HTMLDivElement | null>(null);
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
    setView('chat');
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
    setView('chat');
    setShowSources(false);
  }, [chatBusy, selectedKnowledgeBase?.id]);
  const selectCollection = useCallback((id: string) => {
    if (chatBusy) return;
    setSelectedKnowledgeBase(null);
    setSelectedCollectionId(id);
    setCurrentSession(null);
    setOpenedSessionId(null);
    setConversationVersion(version => version + 1);
    setView('chat');
    setShowSources(false);
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
    setView('chat');
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
    <div className="workspace-shell h-dvh flex flex-col bg-[var(--color-bg)]">
      {/* Top Bar */}
      <header className="workspace-topbar">
        <div className="flex items-center gap-3">
          <div className="brand-mark"><BookOpen size={20} /></div>
          <h1 className="text-base font-semibold tracking-tight">DouyinMind</h1>
          <span className="hidden lg:block border-l border-[var(--color-border)] pl-3 text-xs text-[var(--color-ink-muted)]">收藏，成为你的知识</span>
        </div>
        <div className="workspace-account flex items-center gap-2 min-w-0">
          <button
            onClick={() => setShowSources(value => !value)}
            className="md:hidden flex-shrink-0 px-3 py-1.5 rounded-full text-xs border border-[var(--color-border)] bg-white/70"
            aria-expanded={showSources}
          >
            <Menu size={15} className="inline mr-1" />{showSources ? '返回工作区' : '资料库'}
          </button>
          <span className="workspace-status inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs max-w-md truncate" title={statusMsg}
            style={{ backgroundColor: 'rgba(91,140,90,0.12)', color: 'var(--color-success)' }}>
            <span className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ backgroundColor: 'var(--color-success)' }} /><span className="connection-detail">{statusMsg}</span><span className="connection-short">{loggedIn ? '抖音已连接' : '体验模式'}</span>
          </span>
          <button onClick={onLogout} aria-label={loggedIn ? '退出登录' : '返回首页'} title={loggedIn ? '退出登录' : '返回首页'} className="icon-button"><LogOut size={17} /></button>
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
            view={view} onChangeView={next => { setView(next); setShowSources(false); }} contentContainer={contentContainer} chatOnly={!loggedIn}
            onKnowledgeBaseUpdated={base => setSelectedKnowledgeBase(previous => previous?.id === base.id ? base : previous)}
          />
        </aside>

        {/* Resizer */}
        <div onMouseDown={handleMouseDown} className="hidden md:flex w-2 flex-shrink-0 cursor-col-resize relative group items-center justify-center">
          <div className="w-1 h-8 rounded-full bg-[var(--color-border)] group-hover:h-14 group-hover:bg-accent transition-all" />
        </div>

        {/* Right Panel */}
        <main className={`${showSources ? 'hidden' : 'flex'} md:!flex flex-col flex-1 h-full min-w-0 bg-[var(--color-panel)]`}>
          <div className="workspace-heading">
            <div className="min-w-0">
              <p className="eyebrow">{view === 'collections' || view === 'progress' ? '资料管理 /' : !loggedIn ? '体验模式 /' : '我的知识库 /'}</p>
              <h2 className="workspace-title">{view === 'collections' ? '我的收藏夹' : view === 'progress' ? '处理进度' : !loggedIn ? '先体验 AI 对话' : selectedKnowledgeBase?.name || (selectedCollectionId === 'all' ? '全部已入库资料' : '当前收藏夹的已入库资料')}</h2>
              {selectedKnowledgeBase && (view === 'chat' || view === 'materials') && <p className="mt-1 text-xs text-[var(--color-ink-muted)]">{selectedKnowledgeBase.video_count} 个视频 · {selectedKnowledgeBase.ready_count} 个可提问</p>}
            </div>
            <div className="flex flex-wrap gap-2">
              <button onClick={startNewChat} disabled={chatBusy} className="ui-button"><Plus size={16} />新对话</button>
              {loggedIn && <button onClick={() => setView('collections')} disabled={chatBusy} className="ui-button ui-button-primary"><Download size={16} />从收藏夹导入</button>}
            </div>
          </div>
          {(view === 'chat' || view === 'materials') ? <nav className="workspace-tabs" aria-label="知识库工作区">
            <button className={view === 'chat' ? 'active' : ''} onClick={() => setView('chat')} aria-current={view === 'chat' ? 'page' : undefined}><MessagesSquare size={16} />对话</button>
            {loggedIn && <button className={view === 'materials' ? 'active' : ''} onClick={() => setView('materials')} aria-current={view === 'materials' ? 'page' : undefined}><Files size={16} />视频资料</button>}
          </nav> : <div className="border-b border-[var(--color-border)] px-6 pb-3"><button className="text-button" onClick={() => setView('chat')}><ChevronLeft size={15} />返回知识库</button></div>}
          <div className={view === 'chat' ? 'flex-1 min-h-0' : 'hidden'}>
          <ChatPanel key={conversationVersion} collectionId={selectedKnowledgeBase ? 'all' : selectedCollectionId} statsRefreshKey={statsRefreshKey} chatOnly={!loggedIn}
            knowledgeBaseId={selectedKnowledgeBase?.id ?? null} knowledgeBaseName={selectedKnowledgeBase?.name ?? ''}
            openedSessionId={openedSessionId} sessionTitle={currentSession?.title ?? '新对话'}
            onSessionSaved={sessionSaved} onHistoryChanged={refreshHistory} onBusyChange={setChatBusy} onNewChat={startNewChat} />
          </div>
          <div ref={setContentContainer} className={view === 'chat' ? 'hidden' : 'flex-1 min-h-0 overflow-y-auto'} />
        </main>
      </div>
    </div>
  );
}
