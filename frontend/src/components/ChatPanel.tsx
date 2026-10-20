import { useState, useRef, useEffect, useId, FormEvent } from 'react';
import * as api from '../api';
import ExecutionTrace from './ExecutionTrace';

interface Props {
  collectionId: string;
  statsRefreshKey: number;
  chatOnly?: boolean;
  knowledgeBaseId: number | null;
  knowledgeBaseName: string;
  openedSessionId: number | null;
  sessionTitle: string;
  onSessionSaved: (id: number, title: string) => void;
  onHistoryChanged: () => void;
  onBusyChange: (busy: boolean) => void;
  onNewChat: () => void;
}

interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
  sources?: api.SourceItem[];
  isStreaming?: boolean;
  trace?: api.TraceData;
  latency_ms?: number;
  error?: string;
}

export default function ChatPanel({
  collectionId,
  chatOnly = false,
  knowledgeBaseId,
  knowledgeBaseName,
  openedSessionId,
  sessionTitle,
  onSessionSaved,
  onHistoryChanged,
  onBusyChange,
  onNewChat,
}: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [inputError, setInputError] = useState('');
  const inputFeedbackId = useId();
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState<number | null>(openedSessionId);
  const [historyLoading, setHistoryLoading] = useState(openedSessionId !== null);
  const [historyReady, setHistoryReady] = useState(openedSessionId === null);
  const [historyError, setHistoryError] = useState('');
  const [historyRetry, setHistoryRetry] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const requestRef = useRef<AbortController | null>(null);
  // Saving a new conversation updates its id during streaming; only load the mount-time id.
  const initialSessionIdRef = useRef(openedSessionId);
  const mountedRef = useRef(false);
  const callbacksRef = useRef({ onSessionSaved, onHistoryChanged, onBusyChange });
  callbacksRef.current = { onSessionSaved, onHistoryChanged, onBusyChange };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestRef.current?.abort();
      callbacksRef.current.onBusyChange(false);
    };
  }, []);

  useEffect(() => {
    const historicalSessionId = initialSessionIdRef.current;
    if (historicalSessionId === null) return;
    const controller = new AbortController();
    requestRef.current = controller;
    setHistoryLoading(true);
    setHistoryReady(false);
    setHistoryError('');
    callbacksRef.current.onBusyChange(true);

    const isCurrent = () => mountedRef.current && !controller.signal.aborted && requestRef.current === controller;
    void (async () => {
      try {
        const result = await api.getSessionMessages(historicalSessionId, controller.signal);
        if (!isCurrent()) return;
        if (!result.success) throw new Error(result.message || '历史对话加载失败，请重试。');
        const history: Message[] = result.items
          .filter(item => item.role === 'user' || item.role === 'assistant' || item.role === 'system')
          .map(item => ({
            role: item.role as Message['role'],
            content: item.content,
            sources: item.sources,
            latency_ms: item.latency_ms ?? undefined,
          }));
        setMessages(history);
        setSessionId(historicalSessionId);
        setHistoryReady(true);
      } catch (error) {
        if (isCurrent()) {
          setHistoryError(error instanceof Error && error.message.includes('404')
            ? '这段历史对话已不存在，请选择其他对话或开始新对话。'
            : '历史对话加载失败，请重试。');
        }
      } finally {
        if (isCurrent()) {
          requestRef.current = null;
          setHistoryLoading(false);
          callbacksRef.current.onBusyChange(false);
        }
      }
    })();

    return () => {
      controller.abort();
      if (requestRef.current === controller) {
        requestRef.current = null;
        callbacksRef.current.onBusyChange(false);
      }
    };
  }, [historyRetry]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages]);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (loading || historyLoading || !historyReady || requestRef.current) return;
    const { query: q, error } = api.inspectQuestion(input);
    if (error) {
      setInputError(error);
      return;
    }
    setInputError('');
    const controller = new AbortController();
    requestRef.current = controller;
    setInput('');
    setLoading(true);
    callbacksRef.current.onBusyChange(true);

    const userMsg: Message = { role: 'user', content: q };
    const assistantMsg: Message = { role: 'assistant', content: '', isStreaming: true };
    setMessages(prev => [...prev, userMsg, assistantMsg]);

    const updateAssistant = (update: (message: Message) => Message) => {
      if (!mountedRef.current || controller.signal.aborted || requestRef.current !== controller) return;
      setMessages(prev => prev.map(m =>
        m.role === 'assistant' && m.isStreaming ? update(m) : m
      ));
    };

    try {
      for await (const event of api.chatAskStream(q, sessionId, collectionId, controller.signal, knowledgeBaseId)) {
        if (!mountedRef.current || controller.signal.aborted || requestRef.current !== controller) return;
        switch (event._event) {
          case 'sources':
            updateAssistant(m => ({ ...m, sources: event.sources }));
            break;
          case 'delta':
            updateAssistant(m => ({ ...m, content: m.content + event.text }));
            break;
          case 'meta':
            setSessionId(event.session_id);
            callbacksRef.current.onSessionSaved(event.session_id, sessionId === null ? q.slice(0, 50) : sessionTitle || q);
            updateAssistant(m => ({
              ...m,
              content: event.answer ?? m.content,
              sources: event.sources,
              trace: event.trace,
              latency_ms: event.latency_ms,
            }));
            break;
        }
      }
      if (mountedRef.current && !controller.signal.aborted && requestRef.current === controller) {
        callbacksRef.current.onHistoryChanged();
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        updateAssistant(m => ({
          ...m,
          error: error instanceof Error ? error.message : '请求失败，请稍后重试。',
        }));
      }
    } finally {
      if (mountedRef.current && !controller.signal.aborted && requestRef.current === controller) {
        updateAssistant(m => ({ ...m, isStreaming: false }));
        setLoading(false);
        callbacksRef.current.onBusyChange(false);
      }
      if (requestRef.current === controller) requestRef.current = null;
    }
  };

  const promptChips = chatOnly
    ? ['你好，介绍一下你自己', '用三句话解释什么是知识库', '给我一个学习建议']
    : ['💡 当前范围有哪些视频？', '📝 帮我总结技术类视频的要点', '🔍 有哪些关于AI的视频？'];
  const inputState = api.inspectQuestion(input);
  const shownInputError = inputError || (inputState.length > api.QUESTION_MAX_LENGTH ? inputState.error : '');
  const showInputCount = inputState.length >= 1800;

  return (
    <div className="chat-room h-full flex flex-col bg-[var(--color-panel)]">
      <div className="flex items-center gap-3 px-6 py-3 border-b border-[var(--color-border)]">
        <div className="flex-1 min-w-0">
          <h2 className="truncate text-sm font-medium text-[var(--color-ink)]" title={sessionTitle || '新对话'}>{sessionTitle || '新对话'}</h2>
          <p className="mt-1 truncate text-[11px] text-[var(--color-ink-muted)]" title={knowledgeBaseName}>回答资料：{knowledgeBaseId != null ? knowledgeBaseName : collectionId === 'all' ? '全部已入库资料' : '当前收藏夹的已入库资料'}</p>
        </div>
        <button type="button" onClick={onNewChat} disabled={loading || historyLoading}
          className="flex-shrink-0 px-3 py-1.5 rounded-lg border border-[var(--color-border)] text-xs text-[var(--color-ink-soft)] hover:bg-white/80 disabled:opacity-40 transition-colors">
          ＋ 新对话
        </button>
      </div>
      {/* Messages */}
      <div className="chat-canvas">
        <div ref={scrollRef} className="chat-thread">
        {historyLoading ? (
          <div role="status" className="h-full flex items-center justify-center text-sm text-[var(--color-ink-muted)]">正在加载历史对话…</div>
        ) : historyError ? (
          <div className="h-full flex flex-col items-center justify-center gap-3 text-center px-4">
            <p role="alert" className="text-sm text-red-600">{historyError}</p>
            <p className="text-xs text-[var(--color-ink-muted)]">加载成功后，可以继续这段对话。</p>
            <button type="button" onClick={() => setHistoryRetry(value => value + 1)}
              className="px-4 py-2 rounded-xl border border-[var(--color-border)] text-sm text-[var(--color-ink-soft)] hover:bg-white/80">
              重新加载
            </button>
          </div>
        ) : messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center gap-6 text-center">
            <div aria-hidden="true" className="w-[72px] h-[72px] shrink-0 rounded-[22px] flex items-center justify-center border border-accent/15 bg-gradient-to-br from-white to-accent-light text-accent">
              <svg width="42" height="42" viewBox="0 0 48 48" fill="none" focusable="false" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M24 18c-4.5-3.3-10-4.5-16-3v21c6-1.5 11.5-.3 16 3.5 4.5-3.8 10-5 16-3.5V24" fill="white" fillOpacity=".75" />
                <path d="M24 18v21.5M24 18c2.1-1.5 4.4-2.5 7-3" />
                <path d="M13 22c2.2-.1 4.2.3 6 1.1M13 28c2.2-.1 4.2.3 6 1.1M29 29c1.8-.8 3.8-1.2 6-1.1" strokeOpacity=".45" />
                <path d="m37 5 1.9 5.1L44 12l-5.1 1.9L37 19l-1.9-5.1L30 12l5.1-1.9L37 5Z" fill="var(--color-panel)" stroke="var(--color-amber)" strokeWidth="1.6" />
              </svg>
            </div>
            <div>
              <h2 className="font-display text-xl text-[var(--color-ink)]">{knowledgeBaseId != null ? knowledgeBaseName : chatOnly ? '先体验 AI 对话' : '你的收藏夹知识库'}</h2>
              <p className="text-sm text-[var(--color-ink-muted)] mt-1">{knowledgeBaseId != null ? '根据这个知识库中已完成的视频提问，或从收藏夹导入更多资料。' : chatOnly ? '直接提问，体验回答逐步出现。收藏内容问答需要先登录并入库。' : '创建知识库 → 勾选视频导入 → 开始对话'}</p>
            </div>
            <div className="flex flex-wrap gap-2 justify-center max-w-md">
              {promptChips.map((text, i) => (
                <button key={i} onClick={() => { setInput(text); setInputError(''); }} disabled={!historyReady} className="px-4 py-2.5 rounded-xl text-sm transition-all hover:shadow-sm disabled:opacity-40"
                  style={{ backgroundColor: i === 0 ? 'rgba(232,89,74,0.08)' : i === 1 ? 'rgba(212,148,58,0.08)' : 'rgba(0,0,0,0.04)' }}>
                  <span style={{ color: i === 0 ? 'var(--color-accent)' : i === 1 ? 'var(--color-amber)' : 'var(--color-ink-soft)' }}>{text}</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="chat-messages flex flex-col gap-5">
            {messages.map((msg, i) => (
              <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                <div className={`chat-bubble px-4 py-3 rounded-2xl text-sm leading-relaxed ${
                  msg.role === 'user'
                    ? 'bg-gradient-to-r from-accent to-accent-hover text-white'
                    : msg.role === 'system'
                      ? 'bg-red-50 text-red-600 border border-red-100'
                      : 'bg-white/80 border border-[var(--color-border)] text-[var(--color-ink)]'
                }`}>
                  <div className="whitespace-pre-wrap break-words" aria-live="polite" aria-busy={msg.isStreaming || false}>
                    {msg.content || (msg.isStreaming ? '正在查找资料并准备回答…' : '')}
                    {msg.isStreaming && <span aria-hidden="true" className="inline-block w-1.5 h-4 bg-current ml-0.5 animate-pulse rounded-sm" />}
                  </div>
                  {msg.error && <p role="alert" className="mt-2 text-xs text-red-600">{msg.error}</p>}
                  {/* Execution Trace */}
                  {msg.trace && <ExecutionTrace trace={msg.trace} latency_ms={msg.latency_ms} />}
                  {/* Sources */}
                  {msg.sources && msg.sources.length > 0 && (
                    <div className="mt-3 pt-3 border-t border-dashed border-black/10">
                      <span className="text-[11px] text-[var(--color-ink-muted)]">📎 来源：</span>
                      {msg.sources.map((s, j) => (
                        <a key={j} href={s.url} target="_blank" rel="noopener noreferrer"
                          className="block mt-1 px-2 py-1 rounded-lg text-xs text-accent bg-accent-light hover:underline">
                          {s.title}{typeof s.score === 'number' ? ` (相关度: ${s.score.toFixed(2)})` : ''}
                        </a>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
        </div>
      </div>

      {/* Input */}
      <form onSubmit={handleSubmit} className="flex flex-wrap items-center gap-3 px-6 py-4 border-t border-[var(--color-border)]">
        <input
          value={input}
          onChange={e => { setInput(e.target.value); setInputError(''); }}
          placeholder={historyLoading ? '正在加载历史对话…' : !historyReady ? '请先重新加载历史对话' : chatOnly ? '输入你的问题…' : '输入你的问题，搜索收藏夹中的知识...'}
          className="flex-1 min-w-0 h-12 px-5 rounded-2xl border border-[var(--color-border)] bg-white/90 text-sm outline-none
                     focus:border-accent/50 focus:ring-4 focus:ring-accent/10 transition-all
                     placeholder:text-[var(--color-ink-muted)]"
          aria-label="输入问题"
          aria-invalid={Boolean(shownInputError)}
          aria-describedby={shownInputError || showInputCount ? inputFeedbackId : undefined}
          disabled={loading || historyLoading || !historyReady}
        />
        <button type="submit" aria-label="发送问题" disabled={loading || historyLoading || !historyReady || !inputState.length}
          className="w-12 h-12 rounded-2xl flex items-center justify-center bg-gradient-to-r from-accent to-accent-hover text-white text-xl font-bold
                     shadow-md shadow-accent/20 hover:shadow-lg disabled:opacity-40 transition-all flex-shrink-0">
          {loading ? '⋯' : '→'}
        </button>
        {(shownInputError || showInputCount) && (
          <div id={inputFeedbackId} className="w-full flex items-center justify-between gap-3 text-xs">
            <span role={shownInputError ? 'alert' : undefined} className="text-red-600">{shownInputError}</span>
            {showInputCount && <span className={inputState.length > api.QUESTION_MAX_LENGTH ? 'text-red-600' : 'text-[var(--color-ink-muted)]'}>{inputState.length}/{api.QUESTION_MAX_LENGTH}</span>}
          </div>
        )}
      </form>
    </div>
  );
}
