import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const asURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
async function moduleURL(path, imports = {}, transform = source => source) {
  const source = transform(await readFile(new URL(path, import.meta.url), 'utf8'));
  let { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX } });
  for (const [name, url] of Object.entries(imports)) outputText = outputText.replaceAll(`'${name}'`, `'${url}'`).replaceAll(`"${name}"`, `"${url}"`);
  return asURL(outputText);
}
const view = await import(await moduleURL('../src/assistantView.ts'));
const sse = await moduleURL('../src/sse.ts');
const api = await import(await moduleURL('../src/api.ts', { './sse': sse }));
const base = { id: 'opaque-task-id', kind: 'import', state: 'queued', actions: { can_resume: true, can_reconcile: true } };
// Render the actual detail component; expose its private function only in this
// transpiled test copy. Effects, networking and the main panel never run.
const { TaskDetails } = await import(await moduleURL('../src/components/AssistantPanel.tsx', {
  react: import.meta.resolve('react'), 'react/jsx-runtime': import.meta.resolve('react/jsx-runtime'),
  'lucide-react': import.meta.resolve('lucide-react'), '../api': asURL('export {};'),
  '../assistantView': await moduleURL('../src/assistantView.ts'),
  '../assistantDemo': asURL('export const demoOverview = {}; export const demoTasks = []; export function demoTaskDetail() {}'),
  './AssistantSettings': asURL('export default function Settings() { return null; }'),
  './AssistantOperations': asURL('export default function Operations() { return null; }'),
  './NativeIntakeNotice': asURL('export default function NativeIntakeNotice() { return null; }'),
  './AssistantPanel.display.css': asURL('export {};'),
}, source => source.replace('function TaskDetails(', 'export function TaskDetails(')));
const renderDetails = task => renderToStaticMarkup(createElement(TaskDetails, {
  task, error: '', pending: '', demo: false, notice: '', noticeError: false,
  onClose() {}, onRefresh() {}, async onAction() {},
}));

test('budget blocks explain only the next request and retain prior usage', () => {
  const task = { ...base, state: 'needs_review', reason_code: 'model_budget_exceeded', usage: { total_tokens: 1140 } };
  const message = view.taskIssueExplanation(task);
  assert.match(message.message, /下一个模型请求/);
  assert.match(message.message, /原已发生用量保留/);
  assert.match(renderDetails(task).replace(/<[^>]*>/g, ''), /model_budget_exceeded（/);
  assert.equal(view.taskIssueExplanation({ ...task, state: 'b_view_visible' }), null);
});

test('unknown raw error strings never appear as user explanations', () => {
  const task = { ...base, state: 'needs_review', reason_code: 'Bearer private-api-key' };
  assert.equal(view.taskIssueExplanation(task), null);
  assert.doesNotMatch(renderDetails(task), /private-api-key/);
});

test('all actual choice lifecycle states distinguish asking, held, unknown, waiting and handed off', () => {
  const expected = {
    awaiting_ask: '正在准备选库询问', ask_held: '选库询问尚未发出', ask_unknown: '选库询问发送结果待核对',
    waiting_library: '已询问，等待选择知识库', selected: '已选择，准备保存',
    handoff_claimed: '已选择，正在关联保存任务', handed_off: '已选择，已建立保存任务',
    needs_review: '选库结果待核对', cancelled: '本次选库已取消',
  };
  for (const [state,label] of Object.entries(expected)) {
    const task = { ...base, details: { library_choice: { state } } };
    assert.equal(view.libraryChoiceStage(task).label,label,state);
    if (state === 'handed_off') assert.doesNotMatch(renderDetails(task), /资料已完成/);
  }
});

test('a waiting choice counts as received without becoming execution or an error', () => {
  assert.deepEqual(view.assistantCounts({ queued: 0, needs_review: 0, waiting_library: 1 }),
    { received: 1, queued: 0, processing: 0, attention: 0 });
});

test('normalized unsent recovery retains only the known original preflight explanation', () => {
  const task = { ...base, state:'needs_review', reason_code:'preflight_failed',
    details:{steps:[{kind:'error',metadata:{code:'original_notification_not_unique'}}]} };
  assert.equal(view.taskIssueExplanation(task).code,'original_notification_not_unique');
  assert.match(renderDetails(task), /无法唯一定位原提醒/);
  const unknown = { ...task, details:{steps:[{kind:'error',metadata:{code:'SYNTHETIC_PRIVATE_SECRET'}}]} };
  assert.equal(view.taskIssueExplanation(unknown).code,'preflight_failed');
  assert.doesNotMatch(renderDetails(unknown), /SYNTHETIC_PRIVATE_SECRET/);
});

test('a durable readback projection removes pending banner without claiming recipient-wide delivery', () => {
  const details = { steps:[{kind:'decision',metadata:{kind:'reply_readback_confirmation',status:'b_view_visible'}}] };
  const task = { ...base, state:'b_view_visible',reply_state:'b_view_visible',import_state:'succeeded',details };
  const html=renderDetails(task);
  assert.match(html,/资料已完成/); assert.match(html,/助手账号可见/); assert.match(html,/只读确认已有完成回执/);
  assert.doesNotMatch(html,/这项回复已经进入发送核对|提问账号已收到|所有人可见/);
  const waitingSelection=renderDetails({...task,kind:'clarify',state:'waiting_library',import_state:null,
    details:{library_choice:{state:'waiting_library',option_count:6}}});
  assert.doesNotMatch(waitingSelection,/这项回复已经进入发送核对|继续未开始任务/);
  assert.match(waitingSelection,/已询问，等待选择知识库/);
  const stillUnknown=renderDetails({...task,state:'delivery_unknown',reply_state:'delivery_unknown'});
  assert.match(stillUnknown,/结果待核对/); assert.match(stillUnknown,/这项回复已经进入发送核对/);
  assert.equal(view.taskStepLabel('decision',undefined,'SYNTHETIC_PRIVATE'),'准备结果');
});

test('usage policy posts complete values and original revision without converting zero to unset', async () => {
  const original = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (url, options) => { seen.push({ url, options }); return new Response(JSON.stringify({ initialized: true }), { status: 200 }); };
  try {
    await api.setAssistantUsageLimits('synthetic-admin', { per_task_tokens: 0, daily_tokens: null, expected_revision: 7 });
    assert.equal(seen[0].url, '/api/assistant/usage/limits');
    assert.equal(seen[0].options.method, 'PUT');
    assert.deepEqual(JSON.parse(seen[0].options.body), { per_task_tokens: 0, daily_tokens: null, expected_revision: 7 });
  } finally { globalThis.fetch = original; }
});

test('reply visibility does not manufacture an import completion or public delivery', () => {
  const task = { ...base, state: 'b_view_visible' };
  assert.equal(view.replyStage(task).label, '助手账号可见');
  assert.equal(view.processingStage(task).label, '尚未确认');
  assert.equal(view.processingStage({ ...task, import_state: 'index_ready' }).label, '资料已完成');
  assert.equal(view.processingStage({ ...base, kind: 'qa' }).label, '使用已有资料');
});

test('every send barrier blocks resume even if a server mistakenly offers it', () => {
  for (const state of ['b_view_visible', 'delivery_unknown', 'verification_required', 'send_reserved', 'reserved', 'failed_no_submit']) {
    assert.equal(view.canResumeTask({ ...base, state }), false, state);
    assert.equal(view.canResumeTask({ ...base, reply_state: state }), false, state);
  }
  assert.equal(view.canResumeTask(base), true);
  assert.equal(view.canResumeTask({ ...base, actions: { can_resume: false } }), false);
  assert.equal(view.canResumeTask({ ...base, actions: undefined }), false);
});

test('preflight verification is unsent and cannot override a later send barrier', () => {
  const task = { ...base, state: 'needs_review', reply_state: 'needs_review', reason_code: 'preflight_verification_required' };
  assert.equal(view.hasSendBarrier(task), false);
  assert.match(view.replyStage(task).label, /未提交/);
  assert.equal(view.canResumeTask(task), true, 'only explicit backend safe action can continue this unsent stage');
  const submitted = { ...task, state: 'verification_required', reply_state: 'verification_required' };
  assert.equal(view.hasSendBarrier(submitted), true);
  assert.equal(view.canResumeTask(submitted), false);
  assert.equal(view.replyStage(submitted).label, '等待本人核验');
});

test('private completion remains useful locally without becoming a publication', () => {
  const task = { ...base, state: 'completed_private', reply_state: 'ready', stages: { reply: 'not_public' } };
  assert.equal(view.replyStage(task).label, '已处理，未公开回复');
  assert.equal(view.hasSendBarrier(task), false);
  assert.equal(view.canResumeTask(task), false, 'turning on public permission later cannot replay an old private result');
  assert.equal(view.processingStage({ ...task, import_state: 'succeeded' }).label, '资料已完成');
});

test('product queue counters and string stages are interpreted without erasing distinctions', () => {
  assert.deepEqual(view.assistantCounts({ queued: 2, waiting_desktop: 1, executing: 1, waiting_ingestion: 2,
    ready_to_publish: 1, publishing: 1, completed_private: 3, delivery_unknown: 2, needs_reconcile: 1, needs_review: 1 }),
  { received: 15, queued: 3, processing: 5, attention: 4 });
  assert.equal(view.processingStage({ ...base, stages: { processing: 'polling' } }).label, '正在处理');
  assert.equal(view.processingStage({ ...base, state: 'waiting_desktop', stages: { processing: 'not_started' } }).label, '等待电脑上线');
  assert.equal(view.processingStage({ ...base, kind: 'qa', state: 'waiting_desktop' }).label, '等待电脑上线');
});

test('a running process alone is not evidence that Douyin receiving is ready', () => {
  const overview = receiver_state => ({ available: true, control: { state: 'running', receiver_state, heartbeat_at: new Date().toISOString() }, counts: {}, capabilities: {} });
  assert.equal(view.controlLabel(overview(undefined)).label, '进程运行中');
  assert.equal(view.controlLabel(overview('login_required')).label, '等待助手登录');
  assert.equal(view.controlLabel(overview('verification_required')).label, '等待本人核验');
  assert.equal(view.controlLabel(overview('ready')).label, '正在收件');
  assert.equal(view.controlLabel({ ...overview('ready'), control: { state: 'stopped', receiver_state: 'ready' } }).label, '已停止');
  assert.equal(view.controlLabel({ ...overview('ready'), control: { state: 'running', receiver_state: 'ready', worker_state: 'error' } }).label, '处理需要核对');
  assert.equal(view.controlLabel({ ...overview('ready'), control: { state: 'paused', receiver_state: 'verification_required' } }).label, '等待本人核验');
});

test('fresh web reads cannot turn an old or missing assistant heartbeat into ready intake', () => {
  const now = Date.parse('2026-10-21T10:00:00Z');
  const overview = heartbeat_at => ({ control: { state: 'running', receiver_state: 'ready', heartbeat_at } });
  assert.equal(view.controlLabel(overview('2026-10-21T09:56:59Z'), now).label, '最近报告已过期');
  assert.equal(view.controlLabel(overview('2026-10-21T09:59:00Z'), now).label, '正在收件');
  assert.equal(view.controlLabel(overview(undefined), now).label, '等待助手报告');
  assert.equal(view.controlLabel(overview('bad-time'), now).label, '等待助手报告');
  assert.equal(view.controlLabel(overview('2026-10-21T11:00:00Z'), now).label, '等待助手报告');
  assert.equal(view.assistantHeartbeat({ control: { state: 'stopped', heartbeat_at: '2026-10-20T00:00:00Z' } }, now).state, 'not_expected');
});

test('notification capture observations use their own fields and preserve an explicit zero', () => {
  const missing = view.captureSummary({ state: 'running', heartbeat_at: new Date().toISOString() });
  assert.equal(missing.time, '未记录');
  assert.equal(missing.cycles, '本次通知核对轮数：未记录');
  assert.equal(view.captureSummary({ state: 'starting', capture_cycles: 0, last_capture_at: null }).cycles, '本次已完成 0 轮通知核对');
  const at = '2026-10-21T08:00:00Z';
  const recorded = view.captureSummary({ state: 'running', capture_cycles: 3, last_capture_at: at, heartbeat_at: new Date().toISOString() });
  assert.equal(recorded.time, view.assistantTime(at));
  assert.equal(recorded.cycles, '本次已完成 3 轮通知核对');
  assert.match(view.captureSummary({ state: 'running', capture_cycles: Number.NaN }).cycles, /未记录/);
});

test('asynchronous task requests do not become task completion and cannot pile up while active', () => {
  for (const state of ['queued', 'processing']) {
    const task = { ...base, latest_action: { id: 'action', action: 'resume', state } };
    assert.equal(view.hasActiveAction(task), true);
    assert.equal(view.canResumeTask(task), false);
  }
  const failed = view.actionFeedback({ id: 'action', action: 'resume', state: 'failed', reason_code: 'operator_resume_unavailable', message: 'PRIVATE' });
  assert.match(failed.message, /核对助手窗口/);
  assert.equal(failed.message.includes('PRIVATE'), false);
  const done = view.actionFeedback({ id: 'action', action: 'reconcile', state: 'done' });
  assert.match(done.message, /不代表资料或评论已经完成/);
  assert.equal(done.tone, 'neutral');
});

test('a failed QA task remains visibly failed rather than a green received-only row', () => {
  assert.deepEqual(view.intakeStage({ ...base, kind: 'qa', state: 'failed' }), { label: '处理失败', tone: 'danger' });
  assert.deepEqual(view.intakeStage({ ...base, state: 'waiting_external' }), { label: '等待外部处理', tone: 'attention' });
});

test('missing usage and time remain missing, not zero', () => {
  assert.equal(view.assistantDuration(undefined), '未记录');
  assert.equal(view.assistantDuration(null), '未记录');
  assert.equal(view.assistantDuration(Number.NaN), '未记录');
  assert.equal(view.assistantTime('not-a-time'), '未记录');
  assert.equal(view.assistantDuration(0), '0.0 秒');
  assert.equal(view.assistantTokenUsage(undefined), '未记录');
  assert.equal(view.assistantTokenUsage(null), '未记录');
  assert.equal(view.assistantTokenUsage(Number.NaN), '未记录');
  assert.equal(view.assistantTokenUsage(0), '0 tokens');
  assert.equal(view.processingStage({ ...base, import_state: 'dispatch_unknown' }).tone, 'attention');
});

test('stage timings never borrow legacy lifetime or missing video attempts', () => {
  const task = { ...base, video_id: '7999999000000000001', usage: { elapsed_ms: 900000 } };
  assert.equal(view.assistantTimingRows(task).length, 6);
  assert.ok(view.assistantTimingRows(task).every(stage => stage.value === '未记录'));
  const video_processing = { run_id: 'run-1', request_id: 'request-1', video_id: '7999999000000000002',
    stages_seconds: { download: 12, recognition: 34, indexing: 5 } };
  assert.ok(view.assistantTimingRows({ ...task, details: { timings: { video_processing } } }).every(stage => stage.value === '未记录'));
  for (const field of ['run_id', 'request_id']) {
    const incomplete = { ...video_processing, video_id: task.video_id, [field]: '' };
    assert.ok(view.assistantTimingRows({ ...task, details: { timings: { video_processing: incomplete } } }).every(stage => stage.value === '未记录'));
  }
});

test('measured stage values retain zero, units, and command-plus-answer model scope', () => {
  const task = { ...base, video_id: '7999999000000000001', details: { timings: {
    execution_ms: 7000, retrieval_ms: 0, model_ms: 2500,
    video_processing: { run_id: 'run-1', request_id: 'request-1', video_id: '7999999000000000001',
      stages_seconds: { download: 1.234, recognition: 120, indexing: 0 } },
  } } };
  assert.deepEqual(view.assistantTimingRows(task), [
    { label: '本机处理', value: '7.0 秒' }, { label: '查找知识库', value: '0.0 秒' },
    { label: '指令理解与回答', value: '2.5 秒' }, { label: '下载与提取音频', value: '1.2 秒' },
    { label: '分段与声音识别', value: '2 分 0 秒' }, { label: '整理与资料入库', value: '0.0 秒' },
  ]);
});

test('malformed stage values cannot turn into measured zero or an invented total', () => {
  const video_id = '7999999000000000001';
  const timings = { execution_ms: 1.5, model_ms: Number.NaN, retrieval_ms: null,
    video_processing: { run_id: 'run-1', request_id: 'request-1', video_id,
      stages_seconds: { download: -1, recognition: true, indexing: Number.POSITIVE_INFINITY } } };
  assert.ok(view.assistantTimingRows({ ...base, video_id, details: { timings } }).every(stage => stage.value === '未记录'));
});

test('source time ranges display only recorded explicit ranges', () => {
  assert.equal(view.assistantSourceTime('00:17–00:55'), '视频时间：00:17–00:55');
  assert.equal(view.assistantSourceTime('01:02:03 - 01:03:04'), '视频时间：01:02:03 - 01:03:04');
  for (const value of [undefined, '', 'chunk:17', '推测约20秒', '<b>00:17–00:55</b>']) {
    assert.equal(view.assistantSourceTime(value), '视频时间未记录');
  }
});

test('actual task detail markup renders recorded source time and each separate timing', () => {
  const video_id = '7999999000000000001';
  const html = renderDetails({ ...base, video_id, details: {
    sources: [{ source_id: '资料1', title: '隔离演示原文', chunk_id: 'synthetic:1', time_range: '00:17–00:55' }],
    timings: { execution_ms: 7000, retrieval_ms: 1100, model_ms: 2500,
      video_processing: { run_id: 'synthetic-run', request_id: 'synthetic-request', video_id,
        stages_seconds: { download: 3, recognition: 40, indexing: 0 } } },
  } });
  for (const text of ['视频时间：00:17–00:55', '分阶段耗时', '本机处理', '7.0 秒', '查找知识库', '1.1 秒',
    '指令理解与回答', '2.5 秒', '下载与提取音频', '3.0 秒', '分段与声音识别', '40 秒', '整理与资料入库', '0.0 秒']) {
    assert.ok(html.includes(text), text);
  }
  assert.match(html, /本机处理不含等待入库和发表/);
  assert.match(html, /关联视频处理记录（复用时不算本次耗时）/);
  assert.equal((html.match(/复用时不算本次耗时/g) || []).length, 1);
});

test('legacy detail markup leaves unmeasured phases missing rather than showing old lifetime', () => {
  const html = renderDetails({ ...base, usage: { elapsed_ms: 900000 },
    details: { sources: [{ source_id: '资料1', title: '旧来源', chunk_id: 'synthetic:17' }] } });
  assert.match(html, /视频时间未记录/);
  assert.ok((html.match(/未记录/g) || []).length >= 6);
  assert.doesNotMatch(html, /15 分|视频时间：00:17/);
});

test('source links remain exact string video IDs and cannot open arbitrary endpoints', () => {
  const id = '7900000000000000001';
  assert.equal(view.safeAssistantLink(`https://douyin.com/video/${id}?tracking=discarded`), `https://www.douyin.com/video/${id}`);
  for (const link of ['javascript:alert(1)', 'https://example.com/video/12345', 'https://www.douyin.com:8443/video/12345',
    'https://user:password@www.douyin.com/video/12345', 'https://www.douyin.com/user/self', 'http://www.douyin.com/video/12345']) {
    assert.equal(view.safeAssistantLink(link), null);
  }
});

async function withFetch(mock, run) {
  const previous = globalThis.fetch; globalThis.fetch = mock;
  try { await run(); } finally { globalThis.fetch = previous; }
}
const response = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

test('control and task actions are explicit JSON posts and preserve opaque IDs', async () => {
  const calls = [];
  await withFetch(async (url, options) => { calls.push({ url, options }); return response({ success: true }); }, async () => {
    await api.controlAssistant('pause');
    await api.actOnAssistantTask('task/one?part=1', 'reconcile');
  });
  assert.equal(calls[0].url, '/api/assistant/pause');
  assert.equal(calls[1].url, '/api/assistant/tasks/task%2Fone%3Fpart%3D1/reconcile');
  for (const { options } of calls) {
    assert.equal(options.method, 'POST'); assert.equal(options.body, '{}');
    assert.equal(options.headers['Content-Type'], 'application/json');
  }
});

const retryableReadQuery = { id: 'query-task', kind: 'query', state: 'needs_review',
  reason_code: 'knowledge_query_failed', reply_state: null, import_state: null,
  actions: { can_reconcile: true, can_resume: false, can_retry_query: true },
  details: { reply_text: '' } };

test('query retry requires server permission and an unsent read-only failure', () => {
  assert.equal(view.canRetryQueryTask(retryableReadQuery), true);
  for (const change of [
    { actions: undefined }, { actions: { can_retry_query: false } },
    { kind: 'save' }, { state: 'completed_private' }, { reason_code: 'model_request_failed' },
    { import_state: 'processing' }, { reply_state: 'ready' },
    { details: { reply_text: 'already prepared' } }, { reply: { text: 'already prepared' } },
    { latest_action: { id: 'intent', action: 'retry_query', state: 'queued' } },
    { latest_action: { id: 'intent', action: 'retry_query', state: 'processing' } },
  ]) assert.equal(view.canRetryQueryTask({ ...retryableReadQuery, ...change }), false, JSON.stringify(change));
  for (const state of ['publishing', 'b_view_visible', 'delivery_unknown', 'verification_required', 'send_reserved', 'failed_no_submit']) {
    assert.equal(view.canRetryQueryTask({ ...retryableReadQuery, state }), false);
    assert.equal(view.canRetryQueryTask({ ...retryableReadQuery, reply_state: state }), false);
  }
});

test('actual task detail explains one read-only retry and preserves its model-cost meaning', () => {
  const html = renderDetails(retryableReadQuery);
  assert.match(html, /重试本次问答（仅一次）/);
  assert.match(html, /会重新调用模型并记录用量/);
  assert.match(html, /原失败记录保留，不会重新导入视频/);
  assert.doesNotMatch(html, /继续未开始任务/);
  assert.doesNotMatch(renderDetails({ ...retryableReadQuery, actions: { can_retry_query: false } }), /重试本次问答（仅一次）/);
  assert.doesNotMatch(renderDetails({ ...retryableReadQuery, reply_state: 'delivery_unknown' }), /重试本次问答（仅一次）/);
});

test('retry API uses one explicit POST and never retries a rejected request automatically', async () => {
  const calls = [];
  await withFetch(async (url, options) => { calls.push({ url, options }); return response({ success: true, accepted: true }); }, async () => {
    await api.actOnAssistantTask('task/one?part=1', 'retry_query');
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/assistant/tasks/task%2Fone%3Fpart%3D1/retry-query');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.body, '{}');
  let failures = 0;
  await withFetch(async () => { failures++; return response({ detail: { code: 'query_retry_already_used' } }, 409); }, async () => {
    await assert.rejects(api.actOnAssistantTask('task', 'retry_query'), /单次重试已登记/);
  });
  assert.equal(failures, 1);
});

test('query retry request completion is distinct from an answered or published task', () => {
  const feedback = view.actionFeedback({ id: 'intent', action: 'retry_query', state: 'done' });
  assert.equal(feedback.label, '问答重试请求已处理');
  assert.match(feedback.message, /不代表资料或评论已经完成/);
  const failed = view.actionFeedback({ id: 'intent', action: 'retry_query', state: 'failed',
    reason_code: 'query_retry_already_used', message: 'PRIVATE_DIAGNOSTIC' });
  assert.match(failed.message, /单次问答重试/);
  assert.doesNotMatch(failed.message, /PRIVATE_DIAGNOSTIC/);
});

test('assistant API never displays raw provider errors or a management token', async () => {
  const secret = 'SYNTHETIC_PRIVATE_PROVIDER_DETAIL';
  await withFetch(async () => response({ detail: secret, message: secret }, 500), async () => {
    await assert.rejects(api.getAssistantOverview(), error => !error.message.includes(secret) && /操作未完成/.test(error.message));
  });
  await withFetch(async () => response({ detail: { code: 'unknown', message: secret } }, 401), async () => {
    await assert.rejects(api.getAssistantRegistry(), /本机管理确认已过期/);
  });
  await withFetch(async () => response({ detail: { code: 'constructor', message: secret } }, 500), async () => {
    await assert.rejects(api.getAssistantOverview(), /操作未完成/);
  });
});

test('management authorization is sent only in the header, never in the URL or body', async () => {
  const token = 'synthetic-memory-token'; let called = 0;
  await withFetch(async (url, options) => {
    called++; assert.equal(url, '/api/assistant/registry/users');
    assert.equal(options.headers.Authorization, `Bearer ${token}`);
    assert.equal(url.includes(token), false); assert.equal(options.body.includes(token), false);
    return response({ success: true });
  }, async () => { await api.updateAssistantRegistry('/users', token, { label: 'fictional' }); });
  assert.equal(called, 1);
  await withFetch(() => assert.fail('no management token means no request'), async () => {
    await assert.rejects(api.updateAssistantRegistry('/users', '', {}), /先确认本机管理/);
  });
});


test('current typed native cooldown is a receive wait, never inferred from a stale session snapshot', () => {
  const now = Date.parse('2030-01-02T10:00:00Z');
  const overview = { control: { state: 'running', desired_state: 'running', receiver_state: 'page_unavailable',
    reason_code: 'native_session_cooldown', heartbeat_at: '2030-01-02T09:59:59Z' } };
  assert.equal(view.isNativeSessionCooldown(overview, now), true);
  assert.equal(view.controlLabel(overview, now).label, '接口暂时等待');
  for (const reason_code of ['page_not_ready', 'intake_result_unconfirmed', null, undefined]) {
    const ordinary = { control: { ...overview.control, reason_code, native_session_status: { state: 'cooldown' } } };
    assert.equal(view.isNativeSessionCooldown(ordinary, now), false);
    assert.equal(view.controlLabel(ordinary, now).label, '收件暂时受阻');
  }
});

test('stop, manual pause, real authentication, worker errors and heartbeat outrank a stale cooldown reason', () => {
  const now = Date.parse('2030-01-02T10:00:00Z');
  const base = { state: 'running', desired_state: 'running', receiver_state: 'page_unavailable',
    reason_code: 'native_session_cooldown', heartbeat_at: '2030-01-02T09:59:59Z' };
  for (const [delta, label] of [
    [{ state: 'stopped' }, '已停止'], [{ state: 'stopping' }, '正在停止'],
    [{ desired_state: 'stopped' }, '正在停止'], [{ state: 'pausing' }, '正在暂停'],
    [{ state: 'paused', desired_state: 'paused' }, '已暂停'], [{ desired_state: 'paused' }, '已暂停'],
    [{ receiver_state: 'login_required' }, '等待助手登录'],
    [{ receiver_state: 'verification_required' }, '等待本人核验'],
    [{ reason_code: 'verification_required' }, '等待本人核验'],
    [{ worker_state: 'error' }, '处理需要核对'],
    [{ heartbeat_at: '2030-01-02T09:56:59Z' }, '最近报告已过期'],
    [{ heartbeat_at: null }, '等待助手报告'],
  ]) {
    const overview = { control: { ...base, ...delta } };
    assert.equal(view.controlLabel(overview, now).label, label, JSON.stringify(delta));
    assert.equal(view.isNativeSessionCooldown(overview, now), false, JSON.stringify(delta));
  }
});


test('configuration unavailable cannot be called a receive cooldown and keeps higher-priority states', () => {
  const now = Date.parse('2030-01-02T10:00:00Z');
  const base = { control: { state: 'running', desired_state: 'running', receiver_state: 'page_unavailable',
    reason_code: 'native_session_cooldown', heartbeat_at: '2030-01-02T09:59:59Z' },
    configuration: { state: 'unavailable', reason_code: 'registry_missing_after_initialization' } };
  assert.equal(view.isNativeSessionCooldown(base, now), false);
  assert.equal(view.controlLabel(base, now).label, '设置需核对');
  for (const [delta, label] of [
    [{ state: 'stopped' }, '已停止'], [{ state: 'stopping' }, '正在停止'],
    [{ desired_state: 'stopped' }, '正在停止'], [{ state: 'pausing' }, '正在暂停'],
    [{ state: 'paused', desired_state: 'paused' }, '已暂停'],
    [{ receiver_state: 'login_required' }, '等待助手登录'],
    [{ receiver_state: 'verification_required' }, '等待本人核验'],
    [{ worker_state: 'error' }, '处理需要核对'],
    [{ heartbeat_at: '2030-01-02T09:56:59Z' }, '最近报告已过期'],
    [{ heartbeat_at: null }, '等待助手报告'],
  ]) {
    const overview = { ...base, control: { ...base.control, ...delta } };
    assert.equal(view.controlLabel(overview, now).label, label, JSON.stringify(delta));
    assert.equal(view.isNativeSessionCooldown(overview, now), false);
  }
});
