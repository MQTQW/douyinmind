/**
 * 后端 API 调用封装
 */

import { readSSE } from './sse';

const BASE = '/api';

// ---- Douyin assistant (separate from the existing chat and ingestion APIs) ----
export interface AssistantBindingSummary {
  binding_id?: string; sender_profile_url?: string; receiver_profile_url?: string;
  sender_display_name?: string; receiver_display_name?: string;
  sender_public_number?: string; receiver_public_number?: string;
  knowledge_base_id?: number | null; knowledge_base_name?: string; query_scope?: 'library' | 'all_local'; scope_label?: string;
  public_reply_allowed?: boolean; allow_import?: boolean; instance_id?: string;
}
export interface AssistantOverview {
  available: boolean;
  control: { state: string; desired_state?: string; run_id?: string | null; pid?: number | null; heartbeat_at?: string | null; reason_code?: string | null; receiver_state?: string; message?: string; process_alive?: boolean; worker_state?: 'idle' | 'processing' | 'error'; worker_reason?: string | null; capture_cycles?: number; last_capture_at?: string | null; last_capture_admitted_count?: number | null; last_capture_baseline_created?: boolean | null; manual_verification?: { available: boolean; url?: string | null; state: string; active: boolean; observed_at?: string } };
  counts: Record<string, number>;
  capabilities: { start: boolean; pause: boolean; stop: boolean; multi_user?: boolean; public_visibility?: string };
  binding?: AssistantBindingSummary | null;
  configuration?: { state: 'ready' | 'not_configured' | 'unavailable'; reason_code?: string | null; message?: string | null };
  intake_issues?: { unresolved_count: number; recent: { id: string; comment_excerpt?: string; message?: string; state: string; created_at?: string; }[] };
  diagnostics?: AssistantDiagnostics;
}
export interface AssistantDiagnostics {
  disk_source_version: string | null;
  backend_build: { source_version: string | null; matches_disk: boolean | null; captured_at?: string | null };
  helper_build: { source_version: string | null; matches_disk: boolean | null; captured_at?: string | null };
  heartbeat_age_seconds: number | null; capture_age_seconds: number | null;
  unverified_notice_deferrals?: number;
  notices: { code: string; message: string }[];
  intake_scope: { poll_seconds: number; visible_candidates_per_batch: number; all_notifications_confirmed: false };
  delivery: { b_view_visible: number; requires_review: number; recipient_visibility: string };
}
export interface AssistantTimelineStep {
  id?: string | number; kind: string; label?: string; created_at?: string | number; at?: string;
  elapsed_ms?: number; duration_ms?: number; status?: string;
  metadata?: { tool_name?: string; elapsed_ms?: number; source_count?: number; status?: string; usage?: AssistantUsage; code?: string; kind?: string };
}
export interface AssistantUsage { total_tokens?: number | null; prompt_tokens?: number | null; completion_tokens?: number | null; input_tokens?: number | null; output_tokens?: number | null; model_calls?: number; model_responses?: number; elapsed_ms?: number; }
export type AssistantTaskAction = 'reconcile' | 'resume' | 'retry_query';
export interface AssistantActionStatus { id: string; action: AssistantTaskAction; state: 'queued' | 'processing' | 'done' | 'failed'; reason_code?: string | null; message?: string; created_at?: string | number | null; updated_at?: string | number | null; }
export interface AssistantTimings {
  execution_ms?: number; model_ms?: number; retrieval_ms?: number;
  video_processing?: { run_id: string; request_id: string; video_id: string;
    stages_seconds: { download?: number; recognition?: number; indexing?: number } };
}
export interface AssistantTask {
  id: string; kind: string; state: string; status_label?: string; binding_id?: string; user_id?: string; device_id?: string; knowledge_base_id?: number | null; query_scope?: 'library' | 'all_local'; scope_label?: string; execution_note?: string;
  video_id?: string; video_title?: string; comment_text?: string; created_at?: string | number; updated_at?: string | number;
  agent_task_id?: string; reason_code?: string | null; import_state?: string | null; reply_state?: string | null;
  actions?: { can_reconcile: boolean; can_resume: boolean; can_retry_query?: boolean };
  stages?: Record<string, string | { state?: string; label?: string }>;
  usage?: AssistantUsage | null; timeline?: AssistantTimelineStep[];
  sources?: { source_id?: string; chunk_id?: string; title?: string; url?: string; platform_item_id?: string; time_range?: string }[];
  reply?: { text?: string; state?: string; kind?: string } | null;
  details?: { reply_text?: string; library_name?: string; sources?: AssistantTask['sources']; steps?: AssistantTimelineStep[]; timings?: AssistantTimings;
    library_choice?: { choice_id?: string; state?: string; option_count?: number; selected_knowledge_base_id?: number | null; selected_library_name?: string | null; child_task_id?: string | null } };
  latest_action?: AssistantActionStatus | null;
}
export interface AssistantTaskPage { items: AssistantTask[]; total: number; page: number; page_size: number; }

const ASSISTANT_ERRORS: Record<string, string> = {
  not_configured: '请先完成助手账号与知识库设置。', binding_missing: '尚未设置助手绑定，请先完成设置。',
  binding_inactive: '当前账号绑定已停用，请在设置中核对。', task_not_resumable: '这项任务不能直接继续，请先核对处理记录。',
  unsafe_resume: '这项任务有未确认操作，请先核对，不能直接重试。', verification_required: '助手窗口需要本人完成核验，当前操作已暂停。',
  invalid_state: '状态已经变化，请刷新后核对。', action_not_allowed: '当前状态不允许这个操作，请先核对。',
  remote_catalog_not_verified: '这台设备的资料目录尚未经过核对，请在对应电脑上配置。',
  alias_conflict: '这个别名已指向其他知识库，请换一个名称。',
  record_not_found: '这项账号、设备或授权已不存在，请刷新后核对。',
  profile_already_registered: '这个账号组合已经登记，请管理原记录。',
  device_instance_already_registered: '这个账号已经登记了该设备，请管理原设备。',
  library_not_in_local_catalog: '这个知识库不在本机可用清单中，请重新核对。',
  catalog_unavailable: '本机知识库清单暂时无法读取，原有授权没有改动。',
  invalid_legacy_binding: '未找到可用的原本机绑定，请先核对账号设置。',
  legacy_migration_requires_empty_devices: '已有设备登记，请先核对现有设置，未替换原授权。',
  assistant_not_configured: '请先完成助手账号与知识库设置，再开启收件。',
  resume_not_safe: '当前证据不足，不能直接恢复，请先核对任务记录。',
  query_retry_not_safe: '这项问答不满足单次重试条件，原记录已保留。',
  query_retry_already_used: '这项问答的单次重试已登记，请核查原任务，不要重复操作。',
  reconciler_unavailable: '当前助手运行时尚不能核查，请先确认助手服务已就绪。',
  runtime_stopping: '助手正在停止，请等待本次运行结束后再操作。',
  launch_pending_requires_review: '上次启动结果尚未确认，请先核对现有助手窗口。',
  registry_missing_after_initialization: '原授权登记库缺失，已停止执行。请恢复原授权状态，不会自动重建或回退旧绑定。',
  registry_required_marker_missing: '授权保护标记缺失，已停止执行。请恢复原授权状态。',
  registry_required_marker_write_failed: '授权保护标记未可靠保存，本次设置尚未完成。',
  invalid_registry_required_marker: '授权保护标记无效，已停止执行。请核对原授权状态。',
  unsafe_registry_required_marker: '授权保护标记路径异常，已停止执行。请核对原授权状态。',
  registry_missing_or_empty: '原授权登记库缺失或为空，已停止执行，不会自动修复或重建。',
  foreign_registry_database: '授权登记库格式不符，已停止执行。请核对原授权状态。',
  registry_required_but_missing: '原授权登记库不可用，已停止执行。请恢复原授权状态，不会回退旧绑定。',
  assistant_configuration_unavailable: '助手设置暂时无法读取，已保留原记录。请核对原有授权和助手设置。',
  assistant_receiver_not_configured: '助手收件配置尚不可用，请核对原有助手设置。',
  assistant_setup_required: '请先完成助手账号与知识库设置，再开启助手。',
  verified_local_binding_required: '尚无可用的本机授权，请在账号与设备中确认账号、设备和知识库范围。',
  task_not_found: '这个任务已不存在，请刷新任务记录。',
  task_busy: '任务仍在处理中，请稍后核对。',
  usage_policy_changed: '额度已被其他操作修改，请重新读取后再保存。',
  usage_budget_missing: '原用量台账缺失，已保留任务。请恢复原记录，不能重新建空台账。',
  usage_budget_marker_missing: '用量台账保护标记缺失，请核对原记录。',
  invalid_usage_budget_marker: '用量台账保护标记异常，请核对原记录。',
  unsafe_usage_budget_marker: '用量台账保护标记不可用，请核对原记录。',
  foreign_usage_budget: '用量台账格式不符，未修改原记录。',
  usage_budget_unavailable: '用量台账暂不可读，未把未知费用按零计算。请核对助手状态。',
  model_budget_exceeded: '本次请求超过配置的文字用量准入额度，尚未发起模型请求。',
  model_budget_unknown: '存在未核清的模型用量或台账状态，本次未继续调用模型。',
  local_owner_verified_device_required: '请先在本人电脑上确认账号和本机设备；另一台电脑不会继承全部资料权限。',
  catalog_owner_confirmation_required: '登记当前全部库的保存权限前，需要明确允许查看本人本机目录。',
  local_owner_scope_required: '请先选择全部资料查询或本机目录范围，再选择公开许可。',
  invalid_local_owner_access_schema: '本机全部资料授权记录格式异常，已停止操作，原有记录保留。',
};

export function assistantErrorMessage(code: unknown, fallback: string): string {
  return typeof code === 'string' && Object.prototype.hasOwnProperty.call(ASSISTANT_ERRORS, code) ? ASSISTANT_ERRORS[code] : fallback;
}

export class AssistantRequestError extends Error {
  constructor(message: string, public status = 0) { super(message); this.name = 'AssistantRequestError'; }
}

async function assistantRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/assistant${path}`, {
      ...options, headers: { 'Content-Type': 'application/json', ...options.headers },
    });
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw error;
    throw new Error('助手服务暂时无法连接，请检查本机服务后重试。');
  }
  const data = await response.json().catch(() => null);
  if (!response.ok || data?.success === false) {
    const code = typeof data?.code === 'string' ? data.code : data?.detail?.code;
    throw new AssistantRequestError(assistantErrorMessage(code, response.status === 401 ? '本机管理确认已过期，请重新确认。' : response.status === 404 ? '助手管理服务尚未就绪，请稍后重新连接。' : '操作未完成，请刷新状态后核对。'), response.status);
  }
  if (!data || typeof data !== 'object') throw new Error('助手状态返回不完整，请重新加载。');
  return data as T;
}

export function getAssistantOverview(signal?: AbortSignal): Promise<AssistantOverview> {
  return assistantRequest('/overview', { signal });
}
export interface AssistantBudgetTotals {
  request_count: number; actual_tokens: number; held_estimated_tokens: number;
  unknown_unbounded_requests: number; unsettled_requests: number; overrun_requests: number;
  committed_tokens: number; needs_reconciliation: boolean;
}
export interface AssistantBudgetSummary {
  initialized: boolean; day_key?: string; currency_cost: null;
  policy?: { per_task_tokens: number | null; daily_tokens: number | null; revision: number };
  daily?: AssistantBudgetTotals; all_time?: AssistantBudgetTotals;
}
export function getAssistantUsage(signal?: AbortSignal): Promise<AssistantBudgetSummary> {
  return assistantRequest('/usage', { signal });
}
export function setAssistantUsageLimits(token: string, body: { per_task_tokens: number | null; daily_tokens: number | null; expected_revision: number }): Promise<AssistantBudgetSummary> {
  return assistantRequest('/usage/limits', { method: 'PUT', headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(body) });
}
export function listAssistantTasks(filters: { state?: string; q?: string; page?: number; page_size?: number }, signal?: AbortSignal): Promise<AssistantTaskPage> {
  const query = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => { if (value != null && value !== '') query.set(key, String(value)); });
  return assistantRequest(`/tasks?${query}`, { signal });
}
export function getAssistantTask(id: string, signal?: AbortSignal): Promise<AssistantTask> {
  return assistantRequest(`/tasks/${encodeURIComponent(id)}`, { signal });
}
export function controlAssistant(action: 'start' | 'pause' | 'stop'): Promise<{ success: boolean; overview: AssistantOverview }> {
  return assistantRequest(`/${action}`, { method: 'POST', body: '{}' });
}
export function actOnAssistantTask(id: string, action: AssistantTaskAction): Promise<{ success: boolean; accepted?: boolean; task?: AssistantTask; message?: string }> {
  const operation = action === 'retry_query' ? 'retry-query' : action;
  return assistantRequest(`/tasks/${encodeURIComponent(id)}/${operation}`, { method: 'POST', body: '{}' });
}

export interface AssistantLibraryGrant {
  grant_id?: string; knowledge_base_id: number; name: string; aliases: string[]; is_default: boolean;
  can_query: boolean; can_import: boolean; public_reply_allowed: boolean; revoked?: boolean; binding_id?: string; library_exists?: boolean;
  active?: boolean;
}
export interface AssistantDevice {
  device_id: string; label: string; instance_id: string; enabled: boolean; revoked: boolean;
  verification_status?: string; is_local?: boolean; online?: boolean; last_seen_at?: string | null;
  libraries: AssistantLibraryGrant[];
  local_owner_access?: AssistantLocalOwnerAccess;
}
export interface AssistantLocalOwnerAccess {
  configured: boolean; active: boolean; query_all_local: boolean; catalog_read_allowed: boolean;
  public_reply_allowed: boolean; version?: number; auth_revision?: number;
}
export interface AssistantLocalOwnerRequest {
  confirm: true; query_all_local: boolean; catalog_read_allowed: boolean; public_reply_allowed: boolean; grant_current_libraries: boolean;
}
export interface AssistantRegistryUser {
  user_id: string; label: string; sender_profile_url: string; receiver_profile_url: string;
  verification_status: string; verification_method?: string; active: boolean; default_device_id?: string | null;
  devices: AssistantDevice[];
}
export interface AssistantRegistry {
  configured: boolean; users: AssistantRegistryUser[]; local_catalog: { id: number; name: string }[];
  local_instance_id?: string; cross_device_status?: string; verification_scope?: string;
  catalog_error?: string | null;
}
export interface AssistantAdminSession { admin_token: string; expires_at_utc: string; admin_id?: string; }

export function getAssistantRegistry(signal?: AbortSignal): Promise<AssistantRegistry> {
  return assistantRequest('/registry', { signal });
}
export function confirmAssistantAdmin(): Promise<AssistantAdminSession> {
  return assistantRequest('/registry/admin-session', { method: 'POST', body: JSON.stringify({ confirm_local_admin: true }) });
}
export function updateAssistantRegistry(path: string, token: string, body: Record<string, unknown>, method: 'POST' | 'PUT' | 'DELETE' = 'POST'): Promise<unknown> {
  if (!token) return Promise.reject(new Error('请先确认本机管理身份。'));
  return assistantRequest(`/registry${path}`, { method, headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(body) });
}
export function setAssistantLocalOwnerAccess(userId: string, deviceId: string, token: string, body: AssistantLocalOwnerRequest): Promise<unknown> {
  return updateAssistantRegistry(`/users/${encodeURIComponent(userId)}/devices/${encodeURIComponent(deviceId)}/local-owner-access`, token, { ...body }, 'PUT');
}

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
    // Both terminal facts are required, including the legacy done-before-meta
    // order. Once complete, release the reader instead of waiting for HTTP EOF.
    if (completed && hasMeta) break;
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
