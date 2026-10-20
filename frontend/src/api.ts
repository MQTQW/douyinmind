/**
 * 后端 API 调用封装
 */

import { readSSE } from './sse';

const BASE = '/api';

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let message = await res.text().catch(() => '请求失败，请稍后重试。');
    try {
      const details = JSON.parse(message);
      if (typeof details.detail === 'string') message = details.detail;
      else if (typeof details.message === 'string') message = details.message;
      else if (Array.isArray(details.detail)) message = '输入内容不正确，请检查后重试。';
    } catch { /* Keep a plain-text server error. */ }
    throw new Error(`${res.status}: ${message}`);
  }
  return res.json();
}

// ---- Auth ----

export async function loginStart(): Promise<{ success: boolean; message: string; status: string }> {
  return request('/auth/douyin/login/start', { method: 'POST' });
}

export async function loginStatus(): Promise<{ status: string; message: string }> {
  return request('/auth/douyin/login/status');
}

export async function logout(): Promise<{ success: boolean; message: string }> {
  return request('/auth/douyin/logout', { method: 'POST' });
}

// ---- Favorites ----

export async function syncFavorites(): Promise<{
  success: boolean;
  collections_total?: number;
  videos_total?: number;
  added_videos?: number;
  removed_videos?: number;
  message?: string;
}> {
  return request('/favorites/sync', { method: 'POST' });
}

export async function listCollections(): Promise<{ success: boolean; items: CollectionItem[]; total: number }> {
  return request('/favorites/collections');
}

export async function listCollectionVideos(
  collectionId: string, page = 1, size = 20
): Promise<{ success: boolean; items: VideoItem[]; total: number }> {
  return request(`/favorites/collections/${collectionId}/videos?page=${page}&size=${size}`);
}

export async function listAllCollectionVideos(collectionId: string): Promise<VideoItem[]> {
  const videos = new Map<string, VideoItem>();
  let loaded = 0;
  for (let page = 1; ; page++) {
    const result = await listCollectionVideos(collectionId, page, 100);
    if (!result.success) throw new Error('视频列表加载失败，请重试。');
    for (const video of result.items) videos.set(video.platform_item_id, video);
    loaded += result.items.length;
    if (loaded >= result.total) return [...videos.values()];
    if (result.items.length === 0) throw new Error('视频列表未加载完整，请重试。');
  }
}

// ---- Knowledge ----

export interface KnowledgeBaseItem {
  id: number;
  name: string;
  video_count: number;
  ready_count: number;
  created_at: string;
  updated_at: string;
}

export async function listKnowledgeBases(): Promise<{ success: boolean; items: KnowledgeBaseItem[]; total: number }> {
  return request('/knowledge-bases');
}

function knowledgeBaseName(name: string): string {
  const trimmed = name.trim();
  if (!trimmed || [...trimmed].length > 128) throw new Error('知识库名称请填写 1～128 个字。');
  return trimmed;
}

export async function createKnowledgeBase(name: string): Promise<{ success: boolean; item: KnowledgeBaseItem }> {
  return request('/knowledge-bases', { method: 'POST', body: JSON.stringify({ name: knowledgeBaseName(name) }) });
}

export async function knowledgeBaseFromCollection(collectionId: string): Promise<{ success: boolean; item: KnowledgeBaseItem; reused: boolean }> {
  return request('/knowledge-bases/from-collection', {
    method: 'POST', body: JSON.stringify({ collection_id: collectionId }),
  });
}

export async function renameKnowledgeBase(id: number, name: string): Promise<{ success: boolean; item: KnowledgeBaseItem }> {
  return request(`/knowledge-bases/${id}`, { method: 'PATCH', body: JSON.stringify({ name: knowledgeBaseName(name) }) });
}

export async function listKnowledgeBaseVideos(id: number, page = 1, size = 100): Promise<{ success: boolean; items: VideoItem[]; total: number }> {
  return request(`/knowledge-bases/${id}/videos?page=${page}&size=${size}`);
}

export async function listAllKnowledgeBaseVideos(id: number): Promise<VideoItem[]> {
  const videos = new Map<string, VideoItem>();
  let loaded = 0;
  for (let page = 1; ; page++) {
    const result = await listKnowledgeBaseVideos(id, page, 100);
    if (!result.success) throw new Error('知识库视频加载失败，请重试。');
    result.items.forEach(video => videos.set(video.platform_item_id, video));
    loaded += result.items.length;
    if (loaded >= result.total) return [...videos.values()];
    if (result.items.length === 0) throw new Error('知识库视频未加载完整，请重试。');
  }
}

export async function removeKnowledgeBaseVideo(id: number, itemId: string): Promise<{ success: boolean; removed_count: number }> {
  return request(`/knowledge-bases/${id}/videos/${encodeURIComponent(itemId)}`, { method: 'DELETE' });
}

export async function syncKnowledge(selectedIds?: string[], knowledgeBaseId?: number | null): Promise<{ success: boolean; task_id?: string; pending_count?: number; assigned_count?: number; existing_count?: number; message?: string }> {
  if (selectedIds?.length === 0) throw new Error('请至少选择一个视频。');
  const body: {selected_ids?: string[]; knowledge_base_id?: number} = selectedIds === undefined ? {} : {selected_ids: selectedIds};
  if (knowledgeBaseId != null) body.knowledge_base_id = knowledgeBaseId;
  return request('/knowledge/sync', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function resetFailedVideos(): Promise<{ success: boolean; reset_count: number }> {
  return request('/knowledge/reset-failed', { method: 'POST' });
}

export async function deleteVideo(platformItemId: string): Promise<{ success: boolean; message?: string }> {
  return request(`/knowledge/videos/${platformItemId}`, { method: 'DELETE' });
}

export async function getSyncProgress(taskId: string): Promise<any> {
  return request(`/knowledge/sync/${taskId}`);
}

export async function getKnowledgeStats(): Promise<any> {
  return request('/knowledge/stats');
}

export async function listKnowledgeVideos(status: 'done' | 'all' = 'done', page = 1, size = 100): Promise<{ success: boolean; items: VideoItem[]; total: number }> {
  return request(`/knowledge/videos?status=${status}&page=${page}&size=${size}`);
}

export async function listAllKnowledgeVideos(): Promise<VideoItem[]> {
  const videos = new Map<string, VideoItem>();
  let loaded = 0;
  for (let page = 1; ; page++) {
    const result = await listKnowledgeVideos('done', page);
    if (!result.success) throw new Error('已入库资料加载失败，请重试。');
    result.items.forEach(video => videos.set(video.platform_item_id, video));
    loaded += result.items.length;
    if (loaded >= result.total) return [...videos.values()];
    if (!result.items.length) throw new Error('已入库资料未加载完整，请重试。');
  }
}

export type AnalysisMode = 'audio' | 'audio_visual';

export interface ProcessingMetrics {
  status: string;
  started_at: string;
  ended_at: string | null;
  total_processing_seconds: number | null;
  stages: Record<string, { seconds: number | null; status: string }>;
}

export interface VideoContent {
  success: boolean;
  message?: string;
  platform_item_id: string;
  title: string;
  status: string;
  transcript_text: string;
  visual_analysis_text: string;
  analysis_mode: AnalysisMode;
  analysis_model: string;
  duration_seconds: number;
  transcript_segments: { start_seconds: number; end_seconds: number; text: string }[];
  visual_segments: { start_seconds: number; end_seconds: number; description: string; visible_text: string }[];
  warnings: string[];
  error_message: string;
  processing_metrics?: ProcessingMetrics | null;
}

export async function getVideoContent(itemId: string): Promise<VideoContent> {
  return request(`/knowledge/videos/${encodeURIComponent(itemId)}/content`);
}

// ---- Chat ----

export const QUESTION_MAX_LENGTH = 2000;

export function inspectQuestion(input: string): { query: string; length: number; error: string } {
  // Match Python str.strip(), including U+0085 and U+001C..001F; a BOM is not whitespace there.
  const query = input.replace(/^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+|[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+$/g, '');
  const length = [...query].length;
  return {
    query,
    length,
    error: !length ? '请输入问题。' : length > QUESTION_MAX_LENGTH ? '问题请控制在2000字以内。' : '',
  };
}

function checkedQuestion(input: string): string {
  const result = inspectQuestion(input);
  if (result.error) throw new Error(result.error);
  return result.query;
}

// Only known, user-facing messages may cross the API boundary. Raw provider
// errors can include request details, paths or credentials even when in Chinese.
const CHAT_ERROR_MESSAGES: Record<string, string> = {
  CHAT_SESSION_NOT_FOUND: '该历史对话已不存在，请新建对话。',
  CHAT_SESSION_DELETED: '该历史对话已被删除，请新建对话。',
  CHAT_SCOPE_MISMATCH: '这段历史对话属于另一个知识库，请切回原知识库或新建对话。',
  CHAT_AI_NOT_CONFIGURED: 'AI 服务尚未配置，请在本地填写 DEEPSEEK_API_KEY 后重启后台。',
  CHAT_RETRIEVAL_NOT_CONFIGURED: '语义检索尚未配置，请在本地填写 DASHSCOPE_API_KEY 后重启后台。',
  CHAT_RETRIEVAL_UNAVAILABLE: '语义检索服务暂时不可用，请稍后重试。',
  CHAT_RETRIEVAL_INVALID_RESPONSE: '检索服务返回的数据异常，请稍后重试或检查模型配置。',
  CHAT_RETRIEVAL_AUTH_ERROR: '检索服务访问失败，请检查本地服务密钥、区域和权限。',
  CHAT_RETRIEVAL_REQUEST_ERROR: '检索服务配置或请求异常，请检查服务区域和模型配置。',
  CHAT_AI_TIMEOUT: 'AI 服务响应超时，请稍后重试。',
  CHAT_AI_CONNECTION_ERROR: '暂时无法连接 AI 服务，请稍后重试。',
  CHAT_AI_AUTH_ERROR: 'AI 服务访问失败，请检查本地服务密钥和权限。',
  CHAT_AI_RATE_LIMITED: 'AI 服务请求较多，请稍后重试。',
  CHAT_AI_UNAVAILABLE: 'AI 服务暂时不可用，请稍后重试。',
  CHAT_AI_INVALID_RESPONSE: 'AI 服务返回的数据异常，请稍后重试。',
  CHAT_AI_REQUEST_ERROR: 'AI 服务配置或请求异常，请检查模型配置后重试。',
  CHAT_INTERNAL_ERROR: '处理问题时发生异常，请稍后重试；若持续失败，请检查后台服务。',
};

function safeChatMessage(value: unknown, fallback: string, code?: unknown): string {
  if (code != null) {
    return typeof code === 'string' && Object.prototype.hasOwnProperty.call(CHAT_ERROR_MESSAGES, code)
      ? CHAT_ERROR_MESSAGES[code] : fallback;
  }
  const messages = new Map([
    ['请输入问题。', '请输入问题。'],
    ['问题请控制在2000字以内。', '问题请控制在2000字以内。'],
    ['该历史对话已不存在，请新建对话。', '该历史对话已不存在，请新建对话。'],
    ['该历史对话已被删除，请新建对话。', '该历史对话已被删除，请新建对话。'],
    ['这段历史对话属于另一个知识库，请切回原知识库或新建对话。', '这段历史对话属于另一个知识库，请切回原知识库或新建对话。'],
    ['服务暂时不可用', '服务暂时不可用'],
    ['服务暂时不可用，请稍后重试。', '服务暂时不可用，请稍后重试。'],
    ['请先在 backend/.env 中填写 DEEPSEEK_API_KEY，然后重启后台服务。', 'AI 服务尚未配置，请完成配置后重试。'],
  ]);
  return typeof value === 'string' ? messages.get(value.trim()) ?? fallback : fallback;
}

async function chatRequestError(response: Response): Promise<string> {
  const fallback = response.status === 422 ? '输入内容不正确，请检查后重试。' : '请求失败，请稍后重试。';
  try {
    const data = await response.json();
    if (response.status === 422 && Array.isArray(data?.detail)) {
      for (const issue of data.detail) {
        if (!Array.isArray(issue?.loc) || !issue.loc.includes('query')) continue;
        if (issue.type === 'string_too_long' && issue.ctx?.max_length === QUESTION_MAX_LENGTH) {
          return '问题请控制在2000字以内。';
        }
        if (issue.type === 'string_too_short' && issue.ctx?.min_length === 1) return '请输入问题。';
      }
    }
    return safeChatMessage(data?.detail ?? data?.message, fallback, data?.code);
  } catch {
    return fallback;
  }
}

export async function chatAsk(query: string, sessionId?: number | null, collectionId?: string | null, knowledgeBaseId?: number | null): Promise<{
  success: boolean;
  answer?: string;
  sources?: SourceItem[];
  session_id?: number;
  route_type?: string;
  latency_ms?: number;
  message?: string;
  code?: string;
}> {
  query = checkedQuestion(query);
  const response = await fetch(`${BASE}/chat/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, session_id: sessionId ?? null, collection_id: collectionId ?? null, ...(knowledgeBaseId != null ? {knowledge_base_id: knowledgeBaseId} : {}) }),
  });
  if (!response.ok) throw new Error(await chatRequestError(response));
  const result = await response.json();
  if (result?.success === false) {
    result.message = safeChatMessage(result.message, '回答生成失败，请重试。', result.code);
  }
  return result;
}

export interface TraceData {
  route: string;
  retrieval_mode?: string;
  retrieval_fallback_reason?: string | null;
  retrieval_details?: {
    stages_ms: Partial<Record<'query_embedding' | 'vector_search' | 'scope_documents' | 'keyword_fusion' | 'neighbor_expansion', number>>;
    counts: Partial<Record<'dense_candidates' | 'scope_records' | 'final_hits' | 'expanded_hits', number>>;
    scope_video_count: number | null;
  } | null;
  context_stats?: { provided_chunks: number; provided_videos: number; provided_chars: number; max_chars: number };
  steps: { name: string; time_ms: number }[];
  chunks: { chunk_id: string; title: string; text: string; full_text?: string; score: number | null; score_kind?: 'lexical' | 'semantic' }[];
}

export type ChatStreamEvent =
  | { _event: 'sources'; sources: SourceItem[] }
  | { _event: 'delta'; text: string }
  | { _event: 'meta'; session_id: number; route_type: string; latency_ms: number; sources: SourceItem[]; answer?: string; trace?: TraceData }
  | { _event: 'done'; ok: boolean };

export async function* chatAskStream(
  query: string,
  sessionId?: number | null,
  collectionId?: string | null,
  signal?: AbortSignal,
  knowledgeBaseId?: number | null,
): AsyncGenerator<ChatStreamEvent> {
  query = checkedQuestion(query);
  const res = await fetch(`${BASE}/chat/ask/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ query, session_id: sessionId ?? null, collection_id: collectionId ?? null, ...(knowledgeBaseId != null ? {knowledge_base_id: knowledgeBaseId} : {}) }),
    signal,
  });
  if (!res.ok) throw new Error(await chatRequestError(res));
  if (!res.body) throw new Error('没有收到回答，请重试。');
  if (!res.headers.get('content-type')?.includes('text/event-stream')) {
    await res.body.cancel();
    throw new Error('回答格式异常，请重试。');
  }

  let completed = false;
  let hasMeta = false;
  for await (const { event, data } of readSSE(res.body)) {
    if (!['sources', 'delta', 'meta', 'done', 'error'].includes(event)) continue;
    const payload = data as Record<string, unknown>;
    if (event === 'error') {
      throw new Error(safeChatMessage(payload.message, '回答生成失败，请重试。', payload.code));
    }
    if (event === 'meta') hasMeta = true;
    if (event === 'done') {
      if (payload.ok !== true) throw new Error('回答生成失败，请重试。');
      completed = true;
    }
    yield { ...payload, _event: event } as ChatStreamEvent;
  }
  if (!completed || !hasMeta) throw new Error('连接中断，回答可能不完整，请重试。');
}

export async function listSessions(): Promise<{ success: boolean; items: SessionItem[] }> {
  return request('/chat/sessions?limit=100');
}

export async function getSessionMessages(sessionId: number, signal?: AbortSignal): Promise<{ success: boolean; message?: string; title?: string; items: MessageItem[] }> {
  return request(`/chat/sessions/${sessionId}/messages`, { signal });
}

export async function deleteSession(sessionId: number): Promise<{ success: boolean }> {
  return request(`/chat/sessions/${sessionId}`, { method: 'DELETE' });
}

export async function renameSession(sessionId: number, title: string): Promise<{ success: boolean; session_id: number; title: string }> {
  const trimmed = title.trim();
  if (!trimmed || trimmed.length > 128) throw new Error('对话名称请填写 1～128 个字。');
  return request(`/chat/sessions/${sessionId}`, {
    method: 'PATCH',
    body: JSON.stringify({ title: trimmed }),
  });
}

// ---- Types ----

export interface CollectionItem {
  id: number;
  collection_id: string;
  title: string;
  video_count: number;
  is_active: boolean;
}

export interface VideoItem {
  id: number;
  collection_id: string;
  platform_item_id: string;
  url: string;
  title: string;
  author: string;
  duration: number;
  status: string;
}

export interface SourceItem {
  platform_item_id: string;
  title: string;
  url: string;
  score: number | null;
}

export interface SessionItem {
  id: number;
  title: string;
  message_count: number;
  last_message_at: string | null;
  created_at: string;
  knowledge_base_id?: number | null;
}

export interface MessageItem {
  id: number;
  session_id: number;
  role: 'user' | 'assistant';
  content: string;
  route_type: string;
  created_at: string;
  sources?: SourceItem[];
  latency_ms?: number | null;
}
