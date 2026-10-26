import { useState, useRef, useEffect, useId, FormEvent } from 'react';
import * as api from '../api';
import ExecutionTrace from './ExecutionTrace';
import MessageContent from './MessageContent';
import { answerClipboardText, copyText } from '../clipboard';
import { ArrowUp, BookOpen, Check, Copy, ExternalLink, Sparkles } from 'lucide-react';

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
  const [copiedMessage, setCopiedMessage] = useState<number | null>(null);
  const [copyError, setCopyError] = useState('');
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
    : ['当前范围有哪些视频？', '帮我梳理视频的核心观点', '有哪些可以照做的步骤？'];
  const inputState = api.inspectQuestion(input);
  const shownInputError = inputError || (inputState.length > api.QUESTION_MAX_LENGTH ? inputState.error : '');
  const showInputCount = inputState.length >= 1800;

  return (
    <div className="chat-room h-full flex flex-col bg-[var(--color-panel)]">
      {(openedSessionId !== null || sessionId !== null || messages.length > 0 || Boolean(sessionTitle && sessionTitle !== '新对话')) && <div className="chat-session-heading">
        <div className="flex-1 min-w-0">
          <h2 className="truncate text-sm font-medium text-[var(--color-ink)]" title={sessionTitle || '新对话'}>{sessionTitle || '新对话'}</h2>
          <p className="mt-1 truncate text-xs text-[var(--color-ink-muted)]" title={knowledgeBaseName}>回答资料：{chatOnly ? '体验模式，不使用收藏视频' : knowledgeBaseId != null ? knowledgeBaseName : collectionId === 'all' ? '全部已入库资料' : '当前收藏夹的已入库资料'}</p>
        </div>
      </div>}
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
            <div aria-hidden="true" className="welcome-mark"><BookOpen size={30} /></div>
            <div>
              <h2 className="font-display text-xl text-[var(--color-ink)]">{chatOnly ? '先体验 AI 对话' : '想了解什么？'}</h2>
              <p className="text-sm text-[var(--color-ink-muted)] mt-1">{chatOnly ? '直接提问，体验回答逐步出现。收藏内容问答需要先登录并入库。' : '围绕下方资料范围提问，已完成处理的资料可用于回答。'}</p>
            </div>
            <div className="flex flex-wrap gap-2 justify-center max-w-md">
              {promptChips.map((text, i) => (
                <button key={i} onClick={() => { setInput(text); setInputError(''); }} disabled={!historyReady} className="prompt-chip">{text}</button>
              ))}
            </div>
          </div>
        ) : (
          <div className="chat-messages flex flex-col gap-5">
            {messages.map((msg, i) => (
              <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                <div className={`chat-bubble text-sm leading-relaxed ${
                  msg.role === 'user'
                    ? 'user-bubble'
                    : msg.role === 'system'
                      ? 'bg-red-50 text-red-600 border border-red-100'
                      : 'assistant-bubble'
                }`}>
                  {msg.role === 'assistant' && <div className="assistant-label"><span><Sparkles size={15} /></span><strong>DouyinMind</strong></div>}
                  <div className="break-words" aria-live="polite" aria-busy={msg.isStreaming || false}>
                    {msg.role === 'assistant' && msg.content ? <MessageContent content={msg.content} /> : <span className="whitespace-pre-wrap">{msg.content || (msg.isStreaming ? '正在查找资料并准备回答…' : '')}</span>}
                    {msg.isStreaming && <span aria-hidden="true" className="inline-block w-1.5 h-4 bg-current ml-0.5 animate-pulse rounded-sm" />}
                  </div>
                  {msg.error && <p role="alert" className="mt-2 text-xs text-red-600">{msg.error}</p>}
                  {/* Sources */}
                  {msg.sources && msg.sources.length > 0 && (
                    <div className="answer-sources">
                      <span className="w-full text-xs text-[var(--color-ink-muted)]">已引用的视频来源</span>
                      {msg.sources.map((s, j) => (
                        <a key={j} href={s.url} target="_blank" rel="noopener noreferrer"
                          className="answer-source">
                          <span>{s.title}<small>打开原视频</small></span><ExternalLink size={14} />
                        </a>
                      ))}
                    </div>
                  )}
                  {msg.trace && <ExecutionTrace trace={msg.trace} latency_ms={msg.latency_ms} />}
                  {msg.role === 'assistant' && msg.content && !msg.isStreaming && <button className="text-button mt-3" type="button" onClick={async () => {
                    setCopyError('');
                    try { await copyText(answerClipboardText(msg.content, msg.sources)); setCopiedMessage(i); }
                    catch { setCopyError('复制失败，请选中回答文字手动复制。'); }
                  }}>{copiedMessage === i ? <Check size={14} /> : <Copy size={14} />}{copiedMessage === i ? '已复制' : '复制回答'}</button>}
                </div>
              </div>
            ))}
          </div>
        )}
        </div>
      </div>

      {/* Input */}
      <form onSubmit={handleSubmit} className="chat-composer">
        <div className="composer-box">
        <textarea rows={2}
          value={input}
          onChange={e => { setInput(e.target.value); setInputError(''); }}
          placeholder={historyLoading ? '正在加载历史对话…' : !historyReady ? '请先重新加载历史对话' : chatOnly ? '输入你的问题…' : '输入问题，查找当前资料中的内容…'}
          className="composer-input"
          onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void handleSubmit(event); } }}
          aria-label="输入问题"
          aria-invalid={Boolean(shownInputError)}
          aria-describedby={shownInputError || showInputCount ? inputFeedbackId : undefined}
          disabled={loading || historyLoading || !historyReady}
        />
        <div className="composer-footer"><span className="text-xs text-[var(--color-ink-muted)]">{chatOnly ? '体验模式 · 不使用收藏资料' : knowledgeBaseId != null ? `资料范围：${knowledgeBaseName}` : collectionId === 'all' ? '资料范围：全部已入库资料' : '资料范围：当前收藏夹'}</span>
        <button type="submit" aria-label="发送问题" disabled={loading || historyLoading || !historyReady || !inputState.length} className="composer-send">
          {loading ? <span className="animate-pulse">···</span> : <ArrowUp size={19} />}
        </button>
        </div>
        {(shownInputError || showInputCount) && (
          <div id={inputFeedbackId} className="w-full flex items-center justify-between gap-3 text-xs">
            <span role={shownInputError ? 'alert' : undefined} className="text-red-600">{shownInputError}</span>
            {showInputCount && <span className={inputState.length > api.QUESTION_MAX_LENGTH ? 'text-red-600' : 'text-[var(--color-ink-muted)]'}>{inputState.length}/{api.QUESTION_MAX_LENGTH}</span>}
          </div>
        )}
        </div>
        <p className="composer-note">Enter 发送 · Shift + Enter 换行{!chatOnly && ' · 回答附带来源，方便核对原文'}</p>
        {copyError && <p role="status" className="mt-2 text-xs text-[var(--color-ink-muted)]">{copyError}</p>}
      </form>
    </div>
  );
}
