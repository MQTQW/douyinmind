import type { AssistantTask, AssistantOverview, AssistantActionStatus, AssistantLibraryGrant, AssistantRegistryUser, AssistantDevice } from './api';

export type AssistantTone = 'neutral' | 'active' | 'success' | 'attention' | 'danger';
export interface AssistantStage { label: string; tone: AssistantTone; }
export type AssistantNoticeSource = { kind: 'local' } | { kind: 'action'; task_id: string; action_id: string };

export function assistantNoticeCoveredByAction(task: AssistantTask | null, source?: AssistantNoticeSource): boolean {
  return source?.kind === 'action' && source.task_id === task?.id
    && Boolean(source.action_id) && source.action_id === task?.latest_action?.id;
}

export function assistantLibraryGrantActive(grant: Pick<AssistantLibraryGrant, 'active' | 'revoked'>): boolean {
  return grant.active !== false && grant.revoked !== true;
}

export function assistantScopeLabel(task: Pick<AssistantTask, 'query_scope' | 'knowledge_base_id' | 'scope_label' | 'details'>): string {
  if (task.query_scope === 'all_local') return task.knowledge_base_id == null ? '全部入库资料' : '资料范围待核对';
  return task.details?.library_name || task.scope_label || (task.knowledge_base_id != null ? `知识库 ${task.knowledge_base_id}` : '尚未确定');
}

export function libraryChoiceStage(task: AssistantTask): AssistantStage | null {
  const state = task.details?.library_choice?.state;
  if (state === 'handed_off') return { label: '已选择，已建立保存任务', tone: 'success' };
  if (state === 'handoff_claimed') return { label: '已选择，正在关联保存任务', tone: 'active' };
  if (task.state === 'waiting_library' || ['waiting_library', 'awaiting_library', 'waiting_user'].includes(state || '')) return { label: '已询问，等待选择知识库', tone: 'attention' };
  if (['selection_reserved', 'selected', 'handoff_pending', 'handoff_reserved', 'completed'].includes(state || '')) return { label: '已选择，准备保存', tone: 'active' };
  if (['awaiting_ask', 'prompt_queued', 'ask_prepared', 'ask_queued'].includes(state || '')) return { label: '正在准备选库询问', tone: 'neutral' };
  if (state === 'ask_held') return { label: '选库询问尚未发出', tone: 'attention' };
  if (state === 'ask_unknown') return { label: '选库询问发送结果待核对', tone: 'attention' };
  if (state === 'cancelled') return { label: '本次选库已取消', tone: 'neutral' };
  if (state === 'held' || state === 'needs_review') return { label: '选库结果待核对', tone: 'attention' };
  return state ? { label: '选库状态待核对', tone: 'attention' } : null;
}

const replyStates: Record<string, AssistantStage> = {
  ready_to_publish: { label: '等待回复', tone: 'neutral' },
  publishing: { label: '正在核对回复', tone: 'active' },
  not_public: { label: '已处理，未公开回复', tone: 'neutral' },
  answer_ready: { label: '等待回复', tone: 'neutral' },
  ready: { label: '等待回复', tone: 'neutral' },
  send_reserved: { label: '发送待核对', tone: 'attention' },
  reserved: { label: '发送待核对', tone: 'attention' },
  b_view_visible: { label: '助手账号可见', tone: 'success' },
  delivery_unknown: { label: '结果待核对', tone: 'attention' },
  verification_required: { label: '等待本人核验', tone: 'attention' },
  failed_no_submit: { label: '本次未提交', tone: 'neutral' },
};

export function replyStage(task: AssistantTask): AssistantStage {
  const stage = task.stages?.reply;
  const state = typeof stage === 'string' ? stage : stage?.state;
  if (task.state === 'completed_private' || state === 'not_public') return replyStates.not_public;
  if (task.state === 'needs_review' && task.reason_code === 'preflight_verification_required'
    && !hasSendBarrier(task)) return { label: '发布前需核验 · 未提交', tone: 'attention' };
  return replyStates[task.reply_state ?? ''] ?? replyStates[task.state] ?? replyStates[state ?? ''] ?? { label: '尚未回复', tone: 'neutral' };
}

export function processingStage(task: AssistantTask): AssistantStage {
  if (task.state === 'waiting_desktop') return { label: '等待电脑上线', tone: 'neutral' };
  const choice = libraryChoiceStage(task);
  if (choice && !task.import_state) return choice;
  if (['qa', 'question', 'answer', 'query'].includes(task.kind) && !task.import_state) return { label: '使用已有资料', tone: 'neutral' };
  const stage = task.stages?.processing;
  const state = task.import_state ?? (typeof stage === 'string' ? stage : stage?.state) ?? '';
  if (['done', 'completed', 'index_ready', 'already_ready', 'succeeded'].includes(state)) return { label: task.import_state || task.kind === 'import' ? '资料已完成' : '本机处理完成', tone: 'success' };
  if (['downloading', 'transcribing', 'embedding', 'running', 'processing'].includes(state)) return { label: '资料处理中', tone: 'active' };
  if (task.state === 'waiting_desktop' || ['queued', 'accepted', 'waiting_device', 'pending', 'submitted'].includes(state)) return { label: state === 'waiting_device' || task.state === 'waiting_desktop' ? '等待电脑上线' : '资料排队中', tone: 'neutral' };
  if (['executing', 'waiting_ingestion', 'polling', 'waiting_external'].includes(state)) return { label: '正在处理', tone: 'active' };
  if (['failed'].includes(state)) return { label: '资料处理失败', tone: 'danger' };
  if (state.includes('unknown')) return { label: '资料结果待核对', tone: 'attention' };
  if (['qa', 'question', 'answer', 'query'].includes(task.kind)) return { label: '使用已有资料', tone: 'neutral' };
  return { label: '尚未确认', tone: 'neutral' };
}

export function intakeStage(task: AssistantTask): AssistantStage {
  if (task.state === 'waiting_library') return { label: '已接收', tone: 'success' };
  if (task.state === 'queued') return { label: '已接收 · 排队中', tone: 'neutral' };
  if (['processing', 'executing', 'waiting_ingestion'].includes(task.state)) return { label: '正在处理', tone: 'active' };
  if (['needs_review', 'needs_reconcile'].includes(task.state)) return { label: '需要核对', tone: 'attention' };
  if (task.state === 'waiting_desktop') return { label: '等待电脑', tone: 'neutral' };
  if (task.state === 'failed') return { label: '处理失败', tone: 'danger' };
  if (task.state === 'waiting_external') return { label: '等待外部处理', tone: 'attention' };
  if (task.state === 'cancelled') return { label: '已取消', tone: 'neutral' };
  return { label: '已接收', tone: 'success' };
}

export function hasSendBarrier(task: AssistantTask): boolean {
  return ['b_view_visible', 'delivery_unknown', 'verification_required', 'send_reserved', 'reserved', 'publishing', 'failed_no_submit']
    .some(state => state === task.state || state === task.reply_state);
}

export function canResumeTask(task: AssistantTask): boolean {
  return task.actions?.can_resume === true && !hasSendBarrier(task) && !['completed_private', 'waiting_library'].includes(task.state) && !hasActiveAction(task);
}

export function nativeChannelStage(channel?: { configured: boolean; enabled: boolean; uid_present: boolean } | null,
  provision?: { active: boolean; stage: string; message?: string | null; last_result?: { ok?: boolean; code?: string } | null } | null): AssistantStage {
  if (provision?.active) return { label: '切换中', tone: 'active' };
  if (provision && !provision.active && provision.stage && ['provision_failed', 'provision_timeout', 'failed', 'stop_timeout', 'start_failed'].includes(provision.stage)) {
    return { label: '切换未完成', tone: 'danger' };
  }
  if (channel?.enabled === true) return { label: '接口收件已启用', tone: 'success' };
  if (channel?.configured === true) return { label: '接口收件未启用', tone: 'neutral' };
  return { label: '接口收件未配置', tone: 'neutral' };
}

export function canRetryUnsentTask(task: AssistantTask): boolean {
  // The backend only exposes this for replies it can prove were never sent;
  // that explicit proof also lifts the generic send barrier for this action.
  return task.actions?.can_retry_unsent === true
    && ['verification_required', 'needs_review'].includes(task.state)
    && !hasActiveAction(task);
}

export function canDismissUnsentTask(task: AssistantTask): boolean {
  // Bookkeeping only: the backend proves this reply never left the machine.
  return task.actions?.can_dismiss_unsent === true
    && ['verification_required', 'needs_review'].includes(task.state)
    && !hasActiveAction(task);
}

export function canReadbackUnsentTask(task: AssistantTask): boolean {
  return task.actions?.can_readback_unsent === true
    && ['verification_required', 'needs_review'].includes(task.state)
    && !hasActiveAction(task);
}

export function canRetryAfterReadbackTask(task: AssistantTask): boolean {
  return task.actions?.can_retry_after_readback === true
    && task.reason_code === 'readback_confirmed_unsent'
    && ['verification_required', 'needs_review'].includes(task.state)
    && !hasActiveAction(task);
}

export function canRetryQueryTask(task: AssistantTask): boolean {
  return task.actions?.can_retry_query === true && task.kind === 'query'
    && task.state === 'needs_review' && task.reason_code === 'knowledge_query_failed'
    && task.import_state == null && task.reply_state == null
    && !task.details?.reply_text?.trim() && !task.reply?.text?.trim()
    && !hasSendBarrier(task) && !hasActiveAction(task);
}

export function hasActiveAction(task: AssistantTask): boolean {
  return task.latest_action?.state === 'queued' || task.latest_action?.state === 'processing';
}

export type TaskGuidanceAction = 'retry_after_readback' | 'retry_unsent' | 'readback_unsent' | 'dismiss_unsent' | 'retry_query' | 'reconcile' | 'resume';
export interface TaskGuidance {
  tone: 'success' | 'attention' | 'danger' | 'neutral' | 'active';
  title: string;
  message: string;
  actions: TaskGuidanceAction[];
}

export function taskGuidance(task: AssistantTask): TaskGuidance {
  if (['queued', 'executing', 'waiting_ingestion', 'waiting_external', 'ready_to_publish', 'publishing'].includes(task.state)) {
    return { tone: 'active', title: '正在处理这条指令', message: '助手正在核对或执行；完成后会更新处理结果和回复状态。', actions: [] };
  }
  if (task.state === 'waiting_desktop') {
    return { tone: 'neutral', title: '等待对应电脑上线', message: '任务尚未调用模型或读取那台电脑的资料；电脑上线后会自动继续。', actions: [] };
  }
  if (task.state === 'waiting_library') {
    return { tone: 'success', title: '已在原评论询问知识库', message: '请在抖音原评论线程回复完整库名或序号；选定后助手会另建保存任务并在原线程回复结果。', actions: [] };
  }
  if (task.state === 'b_view_visible' || task.reply_state === 'b_view_visible') {
    return { tone: 'success', title: '回复已发出并在原评论线程可见', message: '助手账号已回读确认可见；提问账号或其他人的视角需另行核对。已发出的内容不会重复发送。', actions: [] };
  }
  if (task.reply_state === 'delivery_unknown' || task.state === 'delivery_unknown') {
    return { tone: 'danger', title: '发送结果未知，禁止自动重发', message: '系统无法确认这条回复是否已经发出；只能做只读核查，不会重新发送。', actions: ['reconcile'] };
  }
  if (task.reason_code === 'readback_confirmed_unsent' && task.actions?.can_retry_after_readback !== true) {
    return { tone: 'attention', title: '已尝试重新发表，仍被平台验证拦截', message: '这条任务已使用过一次重新发表。如果平台仍要求本人核验，建议标记为未提交并结束，然后重新在评论区 @ 助手发一条新指令（新指令会按新任务处理）。', actions: ['dismiss_unsent'] };
  }
  if (canRetryAfterReadbackTask(task)) {
    return { tone: 'attention', title: '已核对未发出，可重新发表一次', message: '只读核对确认原评论线程没有这条回复。可以重新发表同一条草稿（仅一次）；不会重复发送已发出的内容。', actions: ['retry_after_readback', 'dismiss_unsent'] };
  }
  if (canRetryUnsentTask(task)) {
    return { tone: 'attention', title: '这条回复尚未发出', message: '可以重新处理一次：系统会重新核对原评论并发表；已发出的内容不会重复发送。', actions: ['retry_unsent', 'readback_unsent', 'dismiss_unsent'] };
  }
  if (canReadbackUnsentTask(task)) {
    return { tone: 'attention', title: '等待本人核验，回复尚未发出', message: '抖音要求在发表前由本人完成安全验证。可先做只读核对，或把这条标记为未提交后重新发送指令。', actions: ['readback_unsent', 'dismiss_unsent'] };
  }
  if (canRetryQueryTask(task)) {
    return { tone: 'attention', title: '这次查找资料失败，回答尚未发出', message: '可以重试本条问答一次：会重新调用模型并记录用量；原失败记录保留，不会重新导入视频。', actions: ['retry_query', 'reconcile'] };
  }
  if (task.state === 'needs_review' || task.state === 'needs_reconcile') {
    return { tone: 'attention', title: '需要核对这项任务', message: '系统保留原记录，不会自动重发；请先核对原任务状态，再按允许的操作处理。', actions: ['reconcile'] };
  }
  if (task.state === 'failed_no_submit') {
    return { tone: 'neutral', title: '本次未提交', message: '这条回复没有发出。如需结果，请重新在评论区 @ 助手发送一次指令。', actions: [] };
  }
  if (task.state === 'failed') {
    return { tone: 'danger', title: '处理未完成', message: '任务记录已保留；请核对处理记录后再决定是否重新发送。', actions: [] };
  }
  if (task.state === 'completed_private') {
    return { tone: 'neutral', title: '已处理，未公开回复', message: '结果仅保存在本机，没有提交到抖音。', actions: [] };
  }
  if (task.state === 'cancelled') {
    return { tone: 'neutral', title: '任务已停止', message: '原记录保留；如需结果请重新发送指令。', actions: [] };
  }
  return { tone: 'neutral', title: '已接收', message: '任务已登记，助手会按当前运行状态处理。', actions: [] };
}

export function taskIssueExplanation(task: AssistantTask): { code: string; message: string } | null {
  if (!['needs_review', 'needs_reconcile', 'failed', 'delivery_unknown', 'verification_required', 'failed_no_submit'].includes(task.state)) return null;
  const messages: Record<string, string> = {
    model_budget_exceeded: '下一个模型请求超出已配置准入额度，未发出该请求。原已发生用量保留；请核对额度和原任务，不要重发评论。只有确认首次请求尚未开始且原处理期限已结束时，才可显式继续；否则保留待核对。',
    model_budget_unknown: '用量或台账状态尚未核清，已停止后续模型请求；未知用量不会当零，也不会自动重试。',
    model_preflight_failed: '模型请求前检查未通过；请核对本机用量台账，原任务记录保留。',
    knowledge_query_failed: '查找原文时发生异常，原资料保留。请先核查任务，再按页面允许的操作处理。',
    model_request_failed: '模型请求未正常完成，可能已经产生用量；请核对记录，不能按未调用或零费用处理。',
    model_response_incomplete: '模型返回不完整，未将其当成完成的答案；原调用和未知用量保留。',
    model_round_budget_exceeded: '模型没有在本次轮数限制内形成可发表结果，已停止继续调用，避免无限修正。',
    binding_changed: '账号或知识库授权已变化，已停止后续操作；请核对当前授权，原记录不会被换库重跑。',
    comment_thread_not_captured: '任务已登记，但原评论上下文尚未可靠保存；先核对原线程，不能借用其他评论的背景。',
    original_notification_not_unique: '无法唯一定位原提醒，尚未继续发表；请核对原评论，不会另找一条评论替代。',
    original_notification_not_found: '当前通知列表没有找到原提醒，尚未发表；原回复保留，不会改用相似评论。',
    original_notification_candidates_exceed_limit: '同账号的相同文字提醒超过本轮核对范围，尚未发表；不能只挑前几条或凭封面判断视频。',
    original_notification_changed: '核对过程中原提醒列表发生变化，尚未发表；需要重新核实原评论，不能沿用旧列表位置。',
    original_candidate_unreadable: '部分候选评论未能读取完整证据，尚未发表；缺失证据不能当成已经排除其他候选。',
    original_target_changed: '原评论、视频或账号关联已变化，未继续发表；原回答和记录保留。',
    preflight_failed: '回复发表前的定位或校验未通过，尚未提交。核对原评论和当前授权后，后台允许时可继续原回复，不会重新生成答案或导入资料。',
    preflight_verification_required: '发表前需要本人核验，完成前保持暂停并保留原任务，不会自动重发；核验入口目前尚未接通。',
    delivery_unknown: '发送结果未确认；先查看原线程并核查记录，禁止重复发送。',
    verification_required: '发送过程中要求本人核验，结果需核对；请本人完成，不要重新写或重发评论。',
  };
  const originalLookupFailure = task.details?.steps?.some(step => step.kind === 'error'
    && step.metadata?.code === 'original_notification_not_unique');
  const code = task.reason_code === 'preflight_failed' && originalLookupFailure
    ? 'original_notification_not_unique' : task.reason_code;
  return code && Object.prototype.hasOwnProperty.call(messages, code) ? { code, message: messages[code] } : null;
}

export function actionFeedback(action: AssistantActionStatus): AssistantStage & { message: string } {
  const name = action.action === 'retry_query' ? '问答重试请求' : action.action === 'retry_unsent' ? '重新处理请求' : action.action === 'dismiss_unsent' ? '关闭未提交记录' : action.action === 'readback_unsent' ? '只读核对请求' : action.action === 'retry_after_readback' ? '重新发表请求' : action.action === 'resume' ? '继续请求' : '核查请求';
  if (action.state === 'queued') return { label: `${name}排队中`, tone: 'neutral', message: '请求已登记，等待助手处理；任务是否完成请看任务状态。' };
  if (action.state === 'processing') return { label: `${name}处理中`, tone: 'active', message: '正在核对这项请求，原任务记录保留。' };
  if (action.state === 'done') return { label: `${name}已处理`, tone: 'neutral', message: '这项请求已处理，请以任务状态为准；不代表资料或评论已经完成。' };
  const messages: Record<string, string> = { task_not_found: '原任务已不存在。', task_busy: '原任务仍在处理中，请稍后核对。',
    reconciler_unavailable: '助手运行时尚未就绪，暂不能核查。', reconciliation_unavailable: '暂时无法核对原任务，请检查助手运行状态。',
    operator_resume_unavailable: '暂时不能继续原任务，请核对助手窗口和本人核验。', resume_not_safe: '当前证据不足，不能继续原任务。',
    resume_requires_operator_adapter: '原任务需要本人确认，当前不能继续。',
    query_retry_not_safe: '这项问答不满足单次重试条件，原失败记录保留。',
    query_retry_already_used: '单次问答重试已经登记，请核查原任务。',
    interrupted_action: '上次请求处理中断，结果未确认。原任务记录保留，不会自动重试。' };
  const message = typeof action.reason_code === 'string' && Object.prototype.hasOwnProperty.call(messages, action.reason_code) ? messages[action.reason_code] : '本次请求未完成，原任务记录已保留。';
  return { label: `${name}未完成`, tone: 'attention', message };
}

export function assistantHeartbeat(overview: AssistantOverview | null, now = Date.now()): { state: 'not_expected' | 'missing' | 'fresh' | 'stale'; at?: string } {
  if (!overview || !['running', 'listening', 'paused', 'pausing'].includes(overview.control.state)) return { state: 'not_expected' };
  const at = overview.control.heartbeat_at;
  const stamp = typeof at === 'string' ? Date.parse(at) : Number.NaN;
  if (!Number.isFinite(stamp) || stamp > now + 60000) return { state: 'missing' };
  return { state: now - stamp > 180000 ? 'stale' : 'fresh', at: at! };
}

// UI-only redundancy guard. Other capability and recovery gates remain independent.
export function isAssistantStartRedundant(overview: AssistantOverview | null): boolean {
  return Boolean(overview && overview.control.process_alive === true
    && overview.control.desired_state === 'running'
    && ['running', 'listening'].includes(overview.control.state));
}

// Current typed receiver reason only; a prior native-session snapshot is not evidence.
export function isNativeSessionCooldown(overview: AssistantOverview | null, now = Date.now(), checkHeartbeat = true): boolean {
  if (!overview || !['running', 'listening'].includes(overview.control.state)
    || ['paused', 'stopped'].includes(overview.control.desired_state || '')
    || overview.control.worker_state === 'error'
    || overview.configuration?.state === 'unavailable'
    || overview.control.receiver_state !== 'page_unavailable'
    || overview.control.reason_code !== 'native_session_cooldown') return false;
  return !checkHeartbeat || assistantHeartbeat(overview, now).state === 'fresh';
}

export function controlLabel(overview: AssistantOverview | null, now = Date.now(), checkHeartbeat = true): AssistantStage {
  if (!overview) return { label: '尚未连接', tone: 'neutral' };
  const state = overview.control.state;
  if (state === 'stopping') return { label: '正在停止', tone: 'neutral' };
  if (state === 'stopped' || state === 'closed') return { label: '已停止', tone: 'neutral' };
  if (state === 'interrupted') return { label: '进程已中断', tone: 'attention' };
  if (state === 'launch_unknown') return { label: '启动结果待核对', tone: 'attention' };
  if (state === 'pausing') return { label: '正在暂停', tone: 'neutral' };
  if (overview.control.desired_state === 'stopped') return { label: '正在停止', tone: 'neutral' };
  if (overview.control.worker_state === 'error') return { label: '处理需要核对', tone: 'attention' };
  const receiver = overview.control.receiver_state;
  if (receiver === 'login_required') return { label: '等待助手登录', tone: 'attention' };
  if (receiver === 'verification_required' || state === 'verification_required' || overview.control.reason_code === 'verification_required') return { label: '等待本人核验', tone: 'attention' };
  const heartbeat = checkHeartbeat ? assistantHeartbeat(overview, now) : { state: 'not_expected' };
  if (heartbeat.state === 'stale') return { label: '最近报告已过期', tone: 'attention' };
  if (heartbeat.state === 'missing') return { label: '等待助手报告', tone: 'neutral' };
  if (state === 'paused' || overview.control.desired_state === 'paused') return { label: '已暂停', tone: 'attention' };
  if (overview.configuration?.state === 'unavailable') return { label: '设置需核对', tone: 'attention' };
  if (isNativeSessionCooldown(overview, now, checkHeartbeat)) return { label: '接口暂时等待', tone: 'attention' };
  if (receiver === 'page_unavailable') return { label: '收件暂时受阻', tone: 'attention' };
  if (state === 'starting' || receiver === 'starting') return { label: '正在准备页面', tone: 'active' };
  if (['running', 'listening'].includes(state)) return receiver === 'ready' ? { label: '正在收件', tone: 'active' } : { label: '进程运行中', tone: 'neutral' };
  return { label: '等待配置', tone: 'neutral' };
}

export function assistantTime(value?: string | number | null): string {
  if (value == null || value === '') return '未记录';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? '未记录' : date.toLocaleString('zh-CN', { hour12: false });
}

export function assistantDuration(milliseconds?: number | null): string {
  if (milliseconds == null || !Number.isFinite(milliseconds) || milliseconds < 0) return '未记录';
  const seconds = milliseconds / 1000;
  return seconds < 60 ? `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
}

export function assistantTimingRows(task: AssistantTask): { label: string; value: string }[] {
  const timings = task.details?.timings;
  const milliseconds = (value: unknown) => assistantDuration(typeof value === 'number'
    && Number.isSafeInteger(value) && value >= 0 ? value : undefined);
  const seconds = (value: unknown) => assistantDuration(typeof value === 'number'
    && Number.isFinite(value) && value >= 0 ? value * 1000 : undefined);
  const processing = timings?.video_processing;
  // Keep old/unmatched video attempts from becoming this command's cost.
  const stages = processing && processing.video_id === task.video_id
    && typeof processing.run_id === 'string' && processing.run_id.length > 0
    && typeof processing.request_id === 'string' && processing.request_id.length > 0
    ? processing.stages_seconds : undefined;
  return [
    { label: '本机处理', value: milliseconds(timings?.execution_ms) },
    { label: '查找知识库', value: milliseconds(timings?.retrieval_ms) },
    { label: '指令理解与回答', value: milliseconds(timings?.model_ms) },
    { label: '下载与提取音频', value: seconds(stages?.download) },
    { label: '分段与声音识别', value: seconds(stages?.recognition) },
    { label: '整理与资料入库', value: seconds(stages?.indexing) },
  ];
}

export function assistantSourceTime(value?: string): string {
  return typeof value === 'string' && /^\d{2,3}:\d{2}(?::\d{2})?\s*[–—~\-]\s*\d{2,3}:\d{2}(?::\d{2})?$/.test(value)
    ? `视频时间：${value}` : '视频时间未记录';
}

export function assistantTokenUsage(value?: number | null): string {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? `${value.toLocaleString()} tokens` : '未记录';
}

export function captureSummary(control?: AssistantOverview['control']): { time: string; cycles: string } {
  const value = control?.capture_cycles;
  return { time: assistantTime(control?.last_capture_at),
    cycles: typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
      ? `本次已完成 ${value} 轮通知核对` : '本次通知核对轮数：未记录' };
}

export function safeAssistantLink(value?: string): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && ['www.douyin.com', 'douyin.com'].includes(url.hostname)
      && !url.username && !url.password && (!url.port || url.port === '443') && /^\/video\/\d{5,30}\/?$/.test(url.pathname)
      ? `https://www.douyin.com${url.pathname.replace(/\/$/, '')}` : null;
  } catch { return null; }
}

export function taskStepLabel(kind: string, tool?: string, detailKind?: unknown): string {
  if (kind === 'decision' && detailKind === 'reply_readback_confirmation') return '只读确认已有完成回执';
  if (kind === 'decision' && detailKind === 'semantic_planning') return '理解口语指令';
  const tools: Record<string, string> = { list_knowledge_bases: '核对知识库', query_knowledge_base: '查找原文依据',
    save_current_video: '提交当前视频', get_import_status: '检查入库状态', propose_import_status: '整理处理回执', propose_reply: '准备回复' };
  if (tool && tools[tool]) return tools[tool];
  return ({ received: '收到指令', queued: '进入队列', model_request: '理解指令', model_response: '完成理解',
    tool_call: '调用处理工具', tool_result: '记录处理结果', decision: '准备结果',
    send_reserved: '发布前核对', b_view_visible: '助手账号回读可见', error: '需要核对',
    reserved_execute: '开始本机处理', finished_execute: '记录本机处理结果', reserved_publish: '进入发布前核对',
    finished_publish: '记录回复结果', publication_blocked: '公开回复未获许可', restart_fence: '重启后保留不确定操作',
    reconciled: '核查已有处理记录', operator_resume: '本人请求继续未开始阶段', resolved_intake_issue: '补齐指令授权范围',
    reply_readback_confirmation: '只读确认已有完成回执', delivery_readback_claimed: '只读核对已有回执',
    delivery_readback_confirmed: '已有回执已确认可见', delivery_readback_unconfirmed: '已有回执暂未确认',
    preflight_issue: '原评论定位未通过' } as Record<string, string>)[kind] ?? '处理记录';
}

export function assistantCounts(counts: Record<string, number>): { received: number | undefined; queued: number | undefined; processing: number | undefined; attention: number | undefined } {
  const sum = (keys: string[]) => keys.reduce((total, key) => total + (typeof counts[key] === 'number' ? counts[key] : 0), 0);
  const states = ['queued', 'waiting_library', 'waiting_desktop', 'executing', 'waiting_ingestion', 'ready_to_publish', 'publishing', 'waiting_external', 'needs_reconcile', 'needs_review', 'b_view_visible', 'delivery_unknown', 'verification_required', 'failed_no_submit', 'failed', 'cancelled', 'completed_private'];
  if (!Object.keys(counts).length) return { received: undefined, queued: undefined, processing: undefined, attention: undefined };
  return { received: counts.received ?? counts.total ?? sum(states), queued: counts.queued == null ? undefined : sum(['queued', 'waiting_desktop']),
    processing: counts.processing ?? sum(['executing', 'waiting_ingestion', 'ready_to_publish', 'publishing', 'waiting_external']),
    attention: counts.needs_review == null ? undefined : sum(['needs_review', 'needs_reconcile', 'delivery_unknown', 'verification_required', 'failed', 'failed_no_submit']) };
}

// First-run setup is a normal product state, not an unreadable legacy record.
export function assistantSetupStepTone(state?: string): 'success' | 'attention' | 'neutral' {
  return state === 'ready' ? 'success' : state === 'blocked' ? 'attention' : 'neutral';
}

export function assistantSetupProgress(setup?: AssistantOverview['setup'] | null): { done: number; total: number; ready: boolean; nextTitle: string | null } {
  const steps = setup?.steps ?? [];
  const done = steps.filter(step => step.state === 'ready').length;
  const next = steps.find(step => step.state !== 'ready');
  return { done, total: steps.length, ready: Boolean(setup?.ready), nextTitle: next?.title ?? null };
}

export interface AssistantSetupContext {
  receiverReady: boolean;
  matchedUser?: AssistantRegistryUser;
  localDevice?: AssistantDevice;
  verifiedDevice?: AssistantDevice;
  hasScope: boolean;
}

// First-run actions target the same account/device the backend already paired.
export function assistantSetupContext(
  setup: AssistantOverview['setup'] | null | undefined,
  users: AssistantRegistryUser[] | undefined,
  localInstanceId?: string,
): AssistantSetupContext {
  const receiver = setup?.receiver;
  const receiverReady = receiver?.state === 'ready';
  const verified = (users ?? []).filter(user => user.active && user.verification_status === 'verified');
  const matchedUser = receiverReady && receiver?.receiver_profile_url
    ? verified.find(user => user.receiver_profile_url === receiver.receiver_profile_url) : undefined;
  const devices = matchedUser?.devices ?? [];
  const localDevice = devices.find(device => (device.is_local === true || device.instance_id === localInstanceId)
    && device.enabled && !device.revoked);
  const verifiedDevice = localDevice && localDevice.verification_status === 'verified' ? localDevice : undefined;
  const hasScope = Boolean(verifiedDevice && (verifiedDevice.libraries.some(grant => grant.can_query || grant.can_import)
    || verifiedDevice.local_owner_access?.active));
  return { receiverReady, matchedUser, localDevice, verifiedDevice, hasScope };
}
