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
const reactImports = { react: import.meta.resolve('react'), 'react/jsx-runtime': import.meta.resolve('react/jsx-runtime'), 'lucide-react': import.meta.resolve('lucide-react') };
const viewURL = await moduleURL('../src/assistantView.ts');
const view = await import(viewURL);
const NativeIntakeNotice = (await import(await moduleURL('../src/components/NativeIntakeNotice.tsx', { ...reactImports, '../assistantView': viewURL }))).default;
const sharedImports = {
  ...reactImports, '../assistantView': viewURL,
  '../api': asURL('export {};'), './AssistantPanel.display.css': asURL('export {};'),
  '../assistantDemo': asURL('export const demoOverview={};export const demoTasks=[];export function demoTaskDetail(){}'),
  './AssistantSettings': asURL('export default function Settings(){return null}'),
  './AssistantOperations': asURL('export default function Operations(){return null}'),
  './NativeIntakeNotice': await moduleURL('../src/components/NativeIntakeNotice.tsx', { ...reactImports, '../assistantView': viewURL }),
};
const { TaskDetails } = await import(await moduleURL('../src/components/AssistantPanel.tsx', sharedImports,
  source => source.replace('function TaskDetails(', 'export function TaskDetails(')));
const Operations = (await import(await moduleURL('../src/components/AssistantOperations.tsx', { ...reactImports, '../assistantView': viewURL }))).default;
const base = { id: 'synthetic-task', kind: 'query', state: 'b_view_visible', reply_state: 'b_view_visible', query_scope: 'all_local', knowledge_base_id: null,
  comment_text: '合成提问', reply: { text: '合成回答' }, actions: { can_reconcile: true, can_resume: false },
  usage: { total_tokens: 1234, model_responses: 2 }, details: {
    sources: [{ source_id: '资料1', title: '合成来源', time_range: '00:17–00:55', chunk_id: 'synthetic:17' }],
    steps: [{ kind: 'model_response', metadata: { round: 1 } }], timings: { execution_ms: 7000, model_ms: 3000, retrieval_ms: 1000 },
  } };
const detailProps = task => ({ task, error: '', pending: '', demo: true, notice: '', noticeError: false,
  onClose() {}, onRefresh() {}, async onAction() {}, onOpenTask() {} });
const renderDetails = (task, props = {}) => renderToStaticMarkup(createElement(TaskDetails, { ...detailProps(task), ...props }));
const beforeRecords = html => html.split('<details class="assistant-ui-records"')[0];
const sourceSummary = {
  available: true, observed_at: '2026-01-02T12:01:00Z', unresolved_count: 1, pending_count: 0,
  recent: [{ id: 'synthetic-source', state: 'held', created_at: '2026-01-02T11:55:00Z',
    message: '无法确认这条回复对应的原线程。', comment_excerpt: '请只保存，不需要总结。' }],
};
const renderNativeIntake = summary => renderToStaticMarkup(createElement(NativeIntakeNotice, { summary }));

test('source holds stay visible independently of empty task/configuration issue lists', () => {
  const html = renderNativeIntake(sourceSummary);
  for (const text of ['1 条收件记录需核对', '通知已登记', '尚未确认进入任务队列', '无法确认这条回复对应的原线程', '请只保存，不需要总结', '首次记录', '最近核对']) assert.ok(html.includes(text), text);
  assert.doesNotMatch(html, /未调用模型|不计费|无需费用|已完成任务|<button/);
  assert.doesNotMatch(html, /<details[^>]*\bopen(?:=|[ >])/);
});

test('unavailable source observations never render a reassuring zero', () => {
  const html = renderNativeIntake({ available: false });
  assert.match(html, /role="status"/);
  assert.match(html, /收件明细暂时无法读取，已有记录保留/);
  assert.doesNotMatch(html, /0 条|无异常|已完成/);
});

test('normal pending admission is a status rather than a failed task', () => {
  const html = renderNativeIntake({ ...sourceSummary, unresolved_count: 0, pending_count: 2, recent: [] });
  assert.match(html, /2 条指令正在核对受理条件/);
  assert.match(html, /role="status"/);
  assert.doesNotMatch(html, /需核对|失败|<details|<button/);
});

test('healthy or older servers leave the incumbent screen unchanged', () => {
  assert.equal(renderNativeIntake(null), '');
  assert.equal(renderNativeIntake(undefined), '');
  assert.equal(renderNativeIntake({ ...sourceSummary, unresolved_count: 0, recent: [] }), '');
});

test('source text is escaped and cannot turn a diagnostic into an action', () => {
  const html = renderNativeIntake({ ...sourceSummary, recent: [{ ...sourceSummary.recent[0], comment_excerpt: '<script>sendAgain()</script>' }] });
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>|<a |<button/);
});

test('bounded detail list does not pretend to show every held observation', () => {
  const html = renderNativeIntake({ ...sourceSummary, unresolved_count: 12, pending_count: 3 });
  assert.match(html, /12 条收件记录需核对/);
  assert.match(html, /另有 11 条记录需核对/);
  assert.match(html, /另有 3 条指令正在核对受理条件/);
});

for (const [name, task] of Object.entries({
  answer: base,
  waitingChoice: { ...base, kind: 'clarify', state: 'waiting_library', details: { library_choice: { state: 'waiting_library', option_count: 6 } } },
  unknown: { ...base, kind: 'import', state: 'delivery_unknown', reply_state: 'delivery_unknown', import_state: 'succeeded' },
  retryable: { ...base, state: 'needs_review', reply_state: null, reply: null, reason_code: 'knowledge_query_failed', actions: { can_reconcile: true, can_retry_query: true }, details: {} },
  private: { ...base, state: 'completed_private', reply_state: 'ready' },
})) test(`advanced records start closed while result and actions stay outside: ${name}`, () => {
  const html = renderDetails(task);
  assert.match(html, /<details class="assistant-ui-records"><summary>处理记录/);
  assert.doesNotMatch(html, /<details class="assistant-ui-records"[^>]*\bopen(?:=|[ >])/);
  const prefix = beforeRecords(html);
  assert.match(prefix, /原评论指令/);
  assert.match(prefix, /刷新记录/);
  assert.doesNotMatch(prefix, /<h4>资料来源|<h4>分阶段耗时|<h4>指令与回答用量|模型与工具步骤/);
  if (task.reply) assert.match(prefix, /合成回答/);
  if (name === 'retryable') assert.match(prefix, /重试本次问答（仅一次）|会重新调用模型并记录用量/);
  if (name === 'unknown') assert.match(prefix, /资料已完成|结果待核对|不能重新发送/);
  if (name === 'private') assert.match(prefix, /仅保存在本机，没有提交到抖音/);
});

test('advanced records retain actual sources, steps, all timing rows and one fee explanation', () => {
  const html = renderDetails(base);
  for (const text of ['资料1', '视频时间：00:17–00:55', 'synthetic:17', '模型与工具步骤', '分阶段耗时', '7.0 秒', '查找知识库', '未记录', '1,234 tokens']) assert.ok(html.includes(text), text);
  assert.equal((html.match(/指令与回答用量/g) || []).length, 1);
  assert.equal((html.match(/复用时不算本次耗时/g) || []).length, 1);
  assert.match(html, /不代表全部费用；未记录的数值不会按零计算/);
});

test('B-only visibility and a new read error remain outside collapsed records', () => {
  const html = renderDetails(base, { error: '合成读取失败', notice: '合成本次网络错误', noticeError: true, noticeSource: { kind: 'local' } });
  const prefix = beforeRecords(html);
  assert.match(prefix, /助手账号回读可见；提问账号和其他人视角需另核对/);
  assert.match(prefix, /合成读取失败|合成本次网络错误/);
  assert.doesNotMatch(prefix, /已经进入发送核对/);
  assert.equal(view.hasSendBarrier(base), true);
  assert.equal(view.canResumeTask({ ...base, actions: { can_resume: true } }), false);
});

test('only the same task/action-origin notice is suppressed, never equal local text', () => {
  const task = { ...base, latest_action: { id: 'synthetic-action', action: 'reconcile', state: 'queued' } };
  const message = view.actionFeedback(task.latest_action).message;
  const matching = { kind: 'action', task_id: task.id, action_id: task.latest_action.id };
  assert.equal(renderDetails(task, { notice: message, noticeSource: matching }).split(message).length - 1, 1);
  for (const source of [{ kind: 'local' }, { ...matching, task_id: 'other-task' }, { ...matching, action_id: 'other-action' }, undefined]) {
    assert.equal(renderDetails(task, { notice: message, noticeSource: source }).split(message).length - 1, 2);
  }
  assert.match(renderDetails(task, { notice: message, noticeError: true, noticeSource: { kind: 'local' } }), /class="assistant-notice is-error" role="alert"/);
});

test('completed action history leaves terminal task results clear and stays available in records', () => {
  for (const state of ['b_view_visible', 'completed_private']) {
    const task = { ...base, state, latest_action: { id: 'synthetic-action', action: 'retry_query', state: 'done' } };
    const message = view.actionFeedback(task.latest_action).message;
    const html = renderDetails(task);
    assert.doesNotMatch(beforeRecords(html), /问答重试请求已处理|不代表资料或评论已经完成/);
    assert.equal(html.split(message).length - 1, 1);
    assert.ok(html.indexOf('问答重试请求已处理') > html.indexOf('<details class="assistant-ui-records"'));
    assert.match(beforeRecords(html), state === 'b_view_visible' ? /提问账号和其他人视角需另核对/ : /仅保存在本机/);
  }
});

test('a finished action cannot hide uncertainty, work in progress or a fresh local error', () => {
  for (const state of ['delivery_unknown', 'needs_review', 'needs_reconcile', 'waiting_library']) {
    const html = renderDetails({ ...base, state, reply_state: state, latest_action: { id: 'synthetic-action', action: 'reconcile', state: 'done' } });
    assert.match(beforeRecords(html), /核查请求已处理/);
  }
  for (const actionState of ['queued', 'processing', 'failed']) {
    const html = renderDetails({ ...base, latest_action: { id: 'synthetic-action', action: 'reconcile', state: actionState } });
    assert.match(beforeRecords(html), /aria-label="任务核对请求"/);
  }
  const html = renderDetails({ ...base, latest_action: { id: 'synthetic-action', action: 'reconcile', state: 'done' } },
    { notice: '合成本次网络错误', noticeError: true, noticeSource: { kind: 'local' } });
  assert.match(beforeRecords(html), /合成本次网络错误/);
});

test('choice stage is not repeated, while count and actual child navigation remain visible', () => {
  const task = { ...base, kind: 'clarify', state: 'waiting_library', details: { library_choice: { state: 'waiting_library', option_count: 6 } } };
  const html = renderDetails(task);
  assert.equal((html.match(/已询问，等待选择知识库/g) || []).length, 1);
  assert.match(beforeRecords(html), /本轮可选知识库：6 个|不等于视频已经入库/);
  const selected = renderDetails({ ...task, details: { library_choice: { state: 'handed_off', option_count: 6, selected_library_name: '合成库', child_task_id: 'synthetic-child' } } });
  assert.match(beforeRecords(selected), /已选知识库：合成库|查看对应保存任务/);
  const independentStage = renderDetails({ ...task, import_state: 'succeeded', details: { library_choice: { state: 'needs_review' } } });
  assert.match(beforeRecords(independentStage), /资料已完成|选库结果待核对/);
});

test('runtime detail preserves separate heartbeat/capture meaning and pause/stop scope', () => {
  const html = renderToStaticMarkup(createElement(Operations, { demo: false, heartbeatAt: '2026-10-26T10:00:00Z', captureCycles: '本次已完成 0 轮通知核对' }));
  assert.match(html, /助手最近报告（心跳）|本次已完成 0 轮通知核对/);
  assert.match(html, /暂停处理时新指令继续排队；停止后不再收件/);
  assert.equal(renderToStaticMarkup(createElement(Operations, { demo: true })), '');
});

// This hook harness drives the actual panel with a fake API module. No service,
// fetch or browser is used; it tests UI events and response races in memory.
const hookURL = asURL(`let states=[],refs=[],callbacks=[],deps=[],cleanups=[],pending=[],s=0,r=0,c=0,e=0;
export function reset(){for(const f of cleanups)f?.();states=[];refs=[];callbacks=[];deps=[];cleanups=[];pending=[];}
export function begin(){s=0;r=0;c=0;e=0;pending=[];}
export function useState(initial){const i=s++;if(!(i in states))states[i]=typeof initial==='function'?initial():initial;return[states[i],v=>{states[i]=typeof v==='function'?v(states[i]):v;}];}
export function useRef(initial){const i=r++;return refs[i]??=( {current:initial} );}
export function useCallback(f,values){const i=c++;return callbacks[i]??=f;}
export function useEffect(f,values){const i=e++;if(!deps[i]||values.some((v,j)=>!Object.is(v,deps[i][j]))){deps[i]=values;pending.push(()=>{cleanups[i]?.();cleanups[i]=f();});}}
export function flush(){for(const f of pending.splice(0))f();}`);
const hooks = await import(hookURL);
const fakeAPIURL = asURL(`export let calls=[],overview,task,failAction=false,failDetail=false,helperRequests=[];
export function reset(o,t){calls=[];overview=o;task=t;failAction=false;failDetail=false;helperRequests=[];}
export function setTask(t){task=t;}
export function setFailure(v){failAction=v;}
export function setDetailFailure(v){failDetail=v;}
export function assistantErrorMessage(c,f){return f;}
export async function getAssistantOverview(){calls.push(['overview']);return overview;}
export async function listAssistantTasks(p){calls.push(['list',p]);return{items:[task],total:1};}
export async function getAssistantTask(id){calls.push(['detail',id]);if(failDetail)throw new Error('OLD_TASK_A_DETAIL_ERROR');return task;}
export async function controlAssistant(action){calls.push(['control',action]);return{overview};}
export async function actOnAssistantTask(id,action){calls.push(['action',id,action]);if(failAction)throw new Error('合成本次网络错误');return{accepted:true};}
function helper(kind,id,signal,regenerate=false){calls.push([kind,id]);return new Promise((resolve,reject)=>helperRequests.push({kind,id,signal,regenerate,resolve,reject}));}
export function proposeAssistantTaskPlan(id,signal,regenerate=false){return helper('plan',id,signal,regenerate);}
export function explainAssistantTask(id,signal){return helper('triage',id,signal);}`);
const fakeAPI = await import(fakeAPIURL);
const Panel = (await import(await moduleURL('../src/components/AssistantPanel.tsx', { ...sharedImports, react: hookURL, '../api': fakeAPIURL }))).default;
const HookDetails = (await import(await moduleURL('../src/components/AssistantPanel.tsx', {
  ...sharedImports, react: hookURL, '../api': fakeAPIURL,
}, source => source.replace('function TaskDetails(', 'export function TaskDetails(')))).TaskDetails;
function nodes(element, predicate, result = []) {
  if (Array.isArray(element)) { for (const child of element) nodes(child, predicate, result); return result; }
  if (!element || typeof element !== 'object') return result;
  if (predicate(element)) result.push(element);
  nodes(element.props?.children, predicate, result); return result;
}
function text(element) {
  if (Array.isArray(element)) return element.map(text).join('');
  if (element == null || typeof element === 'boolean') return '';
  return typeof element === 'object' ? text(element.props?.children) : String(element);
}
const buttons = tree => nodes(tree, n => n.type === 'button');
const modal = tree => nodes(tree, n => n.type?.name === 'TaskDetails')[0];
async function withPanel(change, run) {
  const prior = { window: globalThis.window, document: globalThis.document, fetch: globalThis.fetch };
  let realFetches = 0;
  globalThis.window = { location: { search: '' }, setInterval: () => 1 };
  globalThis.document = { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} };
  globalThis.fetch = async () => { realFetches++; throw new Error('Real network forbidden'); };
  const overview = { binding: { sender_display_name: '合成A', receiver_display_name: '合成B' }, counts: { queued: 0, b_view_visible: 1, needs_review: 0 },
    control: { state: 'running', receiver_state: 'ready', heartbeat_at: new Date().toISOString(), last_capture_at: new Date().toISOString(), capture_cycles: 2 },
    capabilities: { start: false, pause: true, stop: true }, ...change };
  fakeAPI.reset(overview, base); hooks.reset();
  const render = () => { hooks.begin(); return Panel(); };
  const settle = async () => { hooks.flush(); await new Promise(resolve => setImmediate(resolve)); };
  try {
    let tree = render(); await settle(); tree = render(); await settle(); tree = render();
    await run({ tree, render, settle });
    assert.equal(realFetches, 0);
  } finally { hooks.reset(); Object.assign(globalThis, prior); }
}

test('compact main display retains all 18 filter values, statistics, and distinct controls', async () => {
  await withPanel({}, async ({ tree }) => {
    const options = nodes(tree, n => n.type === 'option').map(n => n.props.value);
    assert.deepEqual([...options].sort(), ['', 'queued', 'waiting_library', 'waiting_desktop', 'executing', 'waiting_ingestion', 'ready_to_publish', 'publishing', 'waiting_external', 'needs_review', 'needs_reconcile', 'b_view_visible', 'completed_private', 'delivery_unknown', 'verification_required', 'failed_no_submit', 'failed', 'cancelled'].sort());
    const stats = nodes(tree, n => n.props.className === 'assistant-ui-compact-counts')[0];
    assert.equal(nodes(stats, n => n.type === 'dt').length, 4);
    assert.deepEqual(nodes(stats, n => n.type === 'dd').map(n => n.props.children), [1, 0, 0, 0]);
    for (const label of ['开启助手', '暂停处理', '停止', '账号与设备']) assert.ok(buttons(tree).some(n => text(n) === label));
    assert.ok(!buttons(tree).some(n => text(n) === '管理设置'));
    assert.doesNotMatch(text(tree), /暂停处理时，新指令继续排队/);
    assert.doesNotMatch(text(tree), /仅反映可见通知，不代表全部提醒已收齐/);
    assert.match(text(tree), /最近核对抖音通知/);
    assert.equal(fakeAPI.calls.some(c => ['control', 'action'].includes(c[0])), false);
  });
});

test('configuration recovery CTA, manual verification entry and abnormal explanation are not folded', async () => {
  await withPanel({ configuration: { state: 'unavailable' }, control: { state: 'paused', receiver_state: 'verification_required', process_alive: true,
    heartbeat_at: new Date().toISOString(), manual_verification: { available: true, active: false, url: 'http://127.0.0.1:8767/' } } }, async ({ tree }) => {
    assert.ok(buttons(tree).some(n => text(n) === '核对原有设置'));
    assert.equal(nodes(tree, n => n.type === 'a' && n.props.href === 'http://127.0.0.1:8767/').length, 1);
    const copy = nodes(tree, n => n.props.className === 'assistant-control-copy')[0];
    assert.match(nodes(copy, n => n.type === 'h4')[0].props.title, /不要把密码或验证码发到聊天|助手仍暂停/);
    assert.equal(buttons(tree).find(n => text(n) === '开启助手').props.disabled, true);
  });
});

async function withHelperDetails(run, task = {}) {
  hooks.reset();
  fakeAPI.reset({}, {});
  const props = { task: { ...base, state: 'needs_review', reason_code: 'preflight_failed',
    reply_state: 'needs_review', details: {}, ...task }, error: '', pending: '', demo: false,
    notice: '', noticeError: false, onClose() {}, onRefresh() {}, async onAction() {} };
  let scrolls = 0;
  const render = () => {
    hooks.begin();
    const tree = HookDetails(props);
    const dialog = nodes(tree, node => node.type === 'dialog')[0];
    dialog.props.ref.current = { open: true, showModal() {}, close() {} };
    const result = nodes(tree, node => node.props['aria-label'] === 'AI辅助结果')[0];
    if (result) result.props.ref.current = { scrollIntoView() { scrolls++; } };
    return tree;
  };
  const settle = async () => {
    hooks.flush();
    await new Promise(resolve => setImmediate(resolve));
    return render();
  };
  try {
    let tree = render(); tree = await settle(); tree = await settle();
    await run({ props, tree, render, settle, scrolls: () => scrolls });
  } finally { hooks.reset(); }
}

for (const kind of ['plan', 'triage']) {
  const originalLabel = kind === 'plan' ? '生成处理计划' : '让助手解释下一步';
  const cachedLabel = kind === 'plan' ? '查看处理计划' : '查看助手解释';
  const successful = kind === 'plan' ? { success: true, proposal: { summary: '合成处理计划结果' } }
    : { success: true, text: '合成助手解释结果' };
  test(`${kind} displays nearby loading and result, retains cached view and synchronously suppresses duplicate clicks`, async () => {
    await withHelperDetails(async ({ tree, render, settle, scrolls }) => {
      const button = buttons(tree).find(node => text(node) === originalLabel);
      const first = button.props.onClick();
      const second = button.props.onClick();
      tree = render(); tree = await settle();
      assert.equal(fakeAPI.helperRequests.length, 1);
      assert.match(text(tree), kind === 'plan' ? /正在生成处理计划/ : /正在根据当前任务状态生成解释/);
      const order = nodes(tree, () => true);
      const actions = order.findIndex(node => node.props.className === 'assistant-detail-actions');
      const result = order.findIndex(node => node.props['aria-label'] === 'AI辅助结果');
      const records = order.findIndex(node => node.props.className === 'assistant-ui-records');
      assert.ok(actions < result && result < records);
      assert.ok(scrolls() > 0);
      fakeAPI.helperRequests[0].resolve(successful);
      await first; await second; tree = await settle();
      assert.match(text(tree), /合成.*结果/);
      assert.ok(buttons(tree).some(node => text(node) === cachedLabel));
      await buttons(tree).find(node => text(node) === cachedLabel).props.onClick();
      assert.equal(fakeAPI.helperRequests.length, 1);
      assert.equal(nodes(tree, node => node.props['aria-live'] === 'polite').length, 1);
    });
  });

  test(`${kind} shows a readable adjacent API failure and releases the control`, async () => {
    await withHelperDetails(async ({ tree, settle }) => {
      const request = buttons(tree).find(node => text(node) === originalLabel).props.onClick();
      fakeAPI.helperRequests[0].reject(new Error('合成网络错误，请稍后再试。'));
      await request; tree = await settle();
      assert.match(text(nodes(tree, node => node.props['aria-label'] === 'AI辅助结果')[0]), /合成网络错误/);
      assert.equal(nodes(tree, node => node.props.role === 'alert').length, 1);
      assert.equal(buttons(tree).find(node => text(node) === originalLabel).props.disabled, false);
    });
  });

  for (const response of [null, {}, kind === 'plan' ? { proposal: { summary: ' ' } } : { text: ' ' }]) {
    test(`${kind} reports an empty or malformed success response`, async () => {
      await withHelperDetails(async ({ tree, settle }) => {
        const request = buttons(tree).find(node => text(node) === originalLabel).props.onClick();
        fakeAPI.helperRequests[0].resolve(response);
        await request; tree = await settle();
        assert.match(text(tree), /返回内容为空/);
      });
    });
  }

  test(`${kind} presents a pending request as status and accepts a later cached detail`, async () => {
    await withHelperDetails(async ({ props, tree, settle }) => {
      const request = buttons(tree).find(node => text(node) === originalLabel).props.onClick();
      fakeAPI.helperRequests[0].resolve(kind === 'plan'
        ? { pending: true, proposal: { summary: '同一计划正在生成', fallback: true } }
        : { pending: true, text: '同一解释正在生成' });
      await request; tree = await settle();
      assert.match(text(tree), /同一.*正在生成/);
      assert.ok(buttons(tree).some(node => text(node) === originalLabel));
      props.task = { ...props.task, details: kind === 'plan'
        ? { semantic_plan_proposal: successful.proposal }
        : { semantic_triage: successful.text, semantic_triage_context_key: 'fresh-context' } };
      tree = await settle(); tree = await settle();
      assert.match(text(tree), /合成.*结果/);
      assert.ok(buttons(tree).some(node => text(node) === cachedLabel));
      assert.equal(fakeAPI.helperRequests.length, 1);
    });
  });

  test(`${kind} aborts on a task change and ignores the late previous-task result`, async () => {
    await withHelperDetails(async ({ props, tree, render, settle }) => {
      const request = buttons(tree).find(node => text(node) === originalLabel).props.onClick();
      const pending = fakeAPI.helperRequests[0];
      props.task = { ...props.task, id: 'another-helper-task', details: {} };
      tree = render();
      assert.equal(nodes(tree, node => node.props['aria-label'] === 'AI辅助结果').length, 0);
      assert.equal(buttons(tree).find(node => text(node) === originalLabel).props.disabled, true);
      tree = await settle();
      assert.equal(pending.signal.aborted, true);
      pending.resolve(successful); await request; tree = await settle();
      assert.doesNotMatch(text(tree), /合成.*结果/);
      assert.equal(buttons(tree).find(node => text(node) === originalLabel).props.disabled, false);
    });
  });

  test(`${kind} rejects a saved old-task click handler after a new task render`, async () => {
    await withHelperDetails(async ({ props, tree, render, settle }) => {
      const oldClick = buttons(tree).find(node => text(node) === originalLabel).props.onClick;
      props.task = { ...props.task, id: 'new-click-target-task', details: {} };
      tree = render();
      await oldClick();
      assert.equal(fakeAPI.helperRequests.length, 0);
      tree = await settle(); tree = await settle();
      const request = buttons(tree).find(node => text(node) === originalLabel).props.onClick();
      assert.equal(fakeAPI.helperRequests[0].id, 'new-click-target-task');
      fakeAPI.helperRequests[0].resolve(successful); await request;
    });
  });

  test(`${kind} aborts a pending request on detail close`, async () => {
    await withHelperDetails(async ({ tree, settle }) => {
      const request = buttons(tree).find(node => text(node) === originalLabel).props.onClick();
      await settle();
      const pending = fakeAPI.helperRequests[0];
      hooks.reset();
      assert.equal(pending.signal.aborted, true);
      pending.resolve(successful); await request;
    });
  });
}

test('an explanation arriving after a fact change cannot replace current-state guidance', async () => {
  await withHelperDetails(async ({ props, tree, render, settle }) => {
    const request = buttons(tree).find(node => text(node) === '让助手解释下一步').props.onClick();
    const pending = fakeAPI.helperRequests[0];
    props.task = { ...props.task, state: 'waiting_library', reply_state: 'b_view_visible' };
    tree = render();
    pending.resolve({ text: '过时的预检失败解释' }); await request;
    tree = await settle(); tree = await settle();
    assert.equal(pending.signal.aborted, true);
    assert.doesNotMatch(text(tree), /过时的预检失败解释/);
    assert.match(text(tree), /任务状态已更新/);
  });
});

test('a completed plan is synchronously scoped to its task before switch effects run', async () => {
  await withHelperDetails(async ({ props, tree, render, settle }) => {
    await buttons(tree).find(node => text(node) === '查看处理计划').props.onClick();
    tree = await settle();
    assert.match(text(tree), /OLD_COMPLETED_PLAN/);
    props.task = { ...props.task, id: 'another-completed-plan-task', details: {} };
    tree = render();
    assert.doesNotMatch(text(tree), /OLD_COMPLETED_PLAN/);
    assert.equal(nodes(tree, node => node.props['aria-label'] === 'AI辅助结果').length, 0);
    const generate = buttons(tree).find(node => text(node) === '生成处理计划');
    assert.equal(generate.props.disabled, true);
    await generate.props.onClick();
    assert.equal(fakeAPI.helperRequests.length, 0);
    tree = await settle(); tree = await settle();
    assert.doesNotMatch(text(tree), /OLD_COMPLETED_PLAN/);
    assert.equal(buttons(tree).find(node => text(node) === '生成处理计划').props.disabled, false);
  }, { details: { semantic_plan_proposal: { summary: 'OLD_COMPLETED_PLAN' } } });
});

for (const oldFeedback of ['error', 'pending', 'regenerate']) {
  test(`task switch synchronously masks old ${oldFeedback} feedback and permission`, async () => {
    await withHelperDetails(async ({ props, tree, render, settle }) => {
      const request = buttons(tree).find(node => text(node) === '生成处理计划').props.onClick();
      if (oldFeedback === 'error') fakeAPI.helperRequests[0].reject(new Error('OLD_PLAN_ERROR'));
      else fakeAPI.helperRequests[0].resolve({ proposal: { summary: 'OLD_PLAN_MESSAGE', fallback: true },
        pending: oldFeedback === 'pending', can_regenerate: oldFeedback === 'regenerate' });
      await request; tree = await settle();
      assert.match(text(tree), /OLD_PLAN/);
      props.task = { ...props.task, id: 'another-feedback-task', details: {} };
      tree = render();
      assert.doesNotMatch(text(tree), /OLD_PLAN|确认重新生成一次/);
      assert.equal(nodes(tree, node => node.props['aria-label'] === 'AI辅助结果').length, 0);
      tree = await settle(); tree = await settle();
      assert.doesNotMatch(text(tree), /OLD_PLAN|确认重新生成一次/);
      assert.equal(fakeAPI.helperRequests.length, 1);
    });
  });
}

test('selected task and retained detail must match before any detail action is rendered', async () => {
  await withPanel({}, async ({ tree, render, settle }) => {
    nodes(tree, node => node.props.className === 'assistant-task-row')[0].props.onClick();
    tree = render(); await settle(); tree = render(); await settle(); tree = render();
    assert.equal(modal(tree).props.task.id, base.id);
    modal(tree).props.onOpenTask('another-selected-task');
    tree = render();
    assert.equal(modal(tree).key, 'another-selected-task');
    assert.equal(modal(tree).props.task, null);
    const html = renderToStaticMarkup(createElement(TaskDetails, modal(tree).props));
    assert.match(html, /正在读取处理记录/);
    assert.doesNotMatch(html, /生成处理计划|让助手解释下一步|重新处理这条回复/);
    await settle(); tree = render(); await settle(); tree = render();
    assert.equal(modal(tree).props.task, null);
    assert.equal(fakeAPI.helperRequests.length, 0);
  });
});

test('a previous detail-read error cannot flash during another selected task loading window', async () => {
  await withPanel({}, async ({ tree, render, settle }) => {
    nodes(tree, node => node.props.className === 'assistant-task-row')[0].props.onClick();
    tree = render(); await settle(); tree = render(); await settle(); tree = render();
    fakeAPI.setDetailFailure(true);
    modal(tree).props.onRefresh();
    tree = render(); await settle(); tree = render(); await settle(); tree = render();
    assert.equal(modal(tree).props.error, 'OLD_TASK_A_DETAIL_ERROR');
    modal(tree).props.onOpenTask('another-error-task');
    tree = render();
    assert.equal(modal(tree).props.task, null);
    assert.equal(modal(tree).props.error, '');
    const html = renderToStaticMarkup(createElement(TaskDetails, modal(tree).props));
    assert.match(html, /正在读取处理记录/);
    assert.doesNotMatch(html, /OLD_TASK_A_DETAIL_ERROR/);
  });
});

test('a completed task clears an in-flight explanation and points to its current result', async () => {
  await withHelperDetails(async ({ props, tree, render, settle }) => {
    const request = buttons(tree).find(node => text(node) === '让助手解释下一步').props.onClick();
    const pending = fakeAPI.helperRequests[0];
    props.task = { ...props.task, state: 'b_view_visible', reply_state: 'b_view_visible' };
    tree = render(); tree = await settle();
    pending.resolve({ text: '过时的失败处理建议' }); await request;
    tree = await settle();
    assert.doesNotMatch(text(tree), /过时的失败处理建议|请重新生成助手解释/);
    assert.match(text(tree), /请查看当前处理结果/);
    assert.ok(!buttons(tree).some(node => text(node) === '让助手解释下一步'));
  });
});

test('unchanged old cached explanation is hidden immediately when task facts change', async () => {
  await withHelperDetails(async ({ props, tree, render, settle }) => {
    await buttons(tree).find(node => text(node) === '查看助手解释').props.onClick();
    tree = await settle();
    assert.match(text(tree), /旧状态解释/);
    props.task = { ...props.task, state: 'waiting_library', reply_state: 'b_view_visible' };
    tree = render();
    assert.doesNotMatch(text(tree), /旧状态解释/);
    tree = await settle(); tree = await settle();
    assert.doesNotMatch(text(tree), /旧状态解释/);
    assert.ok(buttons(tree).some(node => text(node) === '让助手解释下一步'));
    assert.equal(fakeAPI.helperRequests.length, 0);
  }, { details: { semantic_triage: '旧状态解释', semantic_triage_context_key: 'old-context' } });
});

for (const counter of ['execution_attempts', 'publish_attempts']) {
  test(`${counter} change invalidates an in-flight explanation even when state and reason match`, async () => {
    await withHelperDetails(async ({ props, tree, render, settle }) => {
      const request = buttons(tree).find(node => text(node) === '让助手解释下一步').props.onClick();
      const pending = fakeAPI.helperRequests[0];
      props.task = { ...props.task, [counter]: 2,
        actions: { ...props.task.actions, can_retry_unsent: false } };
      tree = render();
      pending.resolve({ text: '过时的尚有重试机会说明' }); await request;
      tree = await settle(); tree = await settle();
      assert.equal(pending.signal.aborted, true);
      assert.doesNotMatch(text(tree), /过时的尚有重试机会说明/);
      assert.match(text(tree), /任务状态已更新/);
      assert.equal(props.task.state, 'needs_review');
      assert.equal(props.task.reason_code, 'preflight_failed');
      assert.ok(buttons(tree).some(node => text(node) === '让助手解释下一步'));
      assert.equal(fakeAPI.helperRequests.length, 1);
    }, { execution_attempts: 1, publish_attempts: 1 });
  });

  test(`${counter} change immediately hides an old explanation cache with the same state and reason`, async () => {
    await withHelperDetails(async ({ props, tree, render, settle }) => {
      await buttons(tree).find(node => text(node) === '查看助手解释').props.onClick();
      tree = await settle();
      assert.match(text(tree), /旧尝试次数解释/);
      props.task = { ...props.task, [counter]: 2 };
      tree = render();
      assert.doesNotMatch(text(tree), /旧尝试次数解释/);
      tree = await settle(); tree = await settle();
      assert.doesNotMatch(text(tree), /旧尝试次数解释/);
      assert.ok(buttons(tree).some(node => text(node) === '让助手解释下一步'));
      assert.equal(fakeAPI.helperRequests.length, 0);
    }, { execution_attempts: 1, publish_attempts: 1,
      details: { semantic_triage: '旧尝试次数解释', semantic_triage_context_key: 'old-attempts-context' } });
  });
}

test('a late inactive helper response preserves the suggestion the user is viewing', async () => {
  await withHelperDetails(async ({ tree, render, settle, scrolls }) => {
    const plan = buttons(tree).find(node => text(node) === '生成处理计划').props.onClick();
    tree = render(); tree = await settle();
    const triage = buttons(tree).find(node => text(node) === '让助手解释下一步').props.onClick();
    tree = render(); tree = await settle();
    const before = scrolls();
    fakeAPI.helperRequests[0].resolve(null); await plan;
    tree = await settle(); tree = await settle();
    const result = nodes(tree, node => node.props['aria-label'] === 'AI辅助结果')[0];
    assert.match(text(result), /助手解释/);
    assert.doesNotMatch(text(result), /处理计划返回内容为空/);
    assert.equal(scrolls(), before);
    fakeAPI.helperRequests[1].resolve({ text: '最新解释结果' }); await triage;
  });
});

test('a lost settled plan requires the visible usage confirmation before its one regeneration request', async () => {
  await withHelperDetails(async ({ tree, render, settle }) => {
    const first = buttons(tree).find(node => text(node) === '生成处理计划').props.onClick();
    assert.equal(fakeAPI.helperRequests[0].regenerate, false);
    fakeAPI.helperRequests[0].resolve({ proposal: { summary: '上次计划结果需要重新生成。', fallback: true }, can_regenerate: true });
    await first; tree = await settle();
    assert.match(text(tree), /重新生成会新增一次 AI 模型用量/);
    assert.equal(fakeAPI.helperRequests.length, 1);
    const confirmation = buttons(tree).find(node => text(node) === '确认重新生成一次');
    const regenerated = confirmation.props.onClick();
    confirmation.props.onClick();
    assert.equal(fakeAPI.helperRequests.length, 2);
    assert.equal(fakeAPI.helperRequests[1].regenerate, true);
    fakeAPI.helperRequests[1].resolve({ proposal: { summary: '明确再生成的计划结果' } });
    await regenerated; tree = render(); tree = await settle();
    assert.match(text(tree), /明确再生成的计划结果/);
    assert.ok(!buttons(tree).some(node => text(node) === '确认重新生成一次'));
    await buttons(tree).find(node => text(node) === '查看处理计划').props.onClick();
    assert.equal(fakeAPI.helperRequests.length, 2);
  });
});

test('a pending or unknown plan has no regeneration permission control', async () => {
  await withHelperDetails(async ({ tree, settle }) => {
    const request = buttons(tree).find(node => text(node) === '生成处理计划').props.onClick();
    fakeAPI.helperRequests[0].resolve({ proposal: { summary: '上次请求结果待核对。', fallback: true }, pending: true });
    await request; tree = await settle();
    assert.ok(!buttons(tree).some(node => text(node) === '确认重新生成一次'));
    assert.equal(fakeAPI.helperRequests[0].regenerate, false);
  });
});

test('data-format and worker errors retain their explanation even with a fresh ready receiver', async () => {
  for (const control of [
    { reason_code: 'native_notice_format_unavailable' },
    { worker_state: 'error' },
  ]) {
    await withPanel({ control: { state: 'running', receiver_state: 'ready', heartbeat_at: new Date().toISOString(), ...control } }, async ({ tree }) => {
      assert.match(text(tree), control.worker_state ? /工具处理出现问题，任务记录已保留/ : /通知数据格式需要核对，收件已暂停/);
      assert.doesNotMatch(text(tree), /助手正在留意新指令/);
    });
  }
});

test('pause and stop buttons still call their distinct fake endpoints only after a click', async () => {
  await withPanel({}, async ({ tree, render, settle }) => {
    assert.equal(fakeAPI.calls.some(c => c[0] === 'control'), false);
    buttons(tree).find(n => text(n) === '暂停处理').props.onClick();
    await settle(); tree = render();
    buttons(tree).find(n => text(n) === '停止').props.onClick();
    await settle();
    assert.deepEqual(fakeAPI.calls.filter(c => c[0] === 'control'), [['control', 'pause'], ['control', 'stop']]);
  });
});

test('a local action failure survives the next durable action update and is not duplicated behind the modal', async () => {
  const action = { id: 'synthetic-action', action: 'reconcile', state: 'done' };
  await withPanel({}, async ({ tree, render, settle }) => {
    fakeAPI.setTask({ ...base, latest_action: action });
    nodes(tree, n => n.props.className === 'assistant-task-row')[0].props.onClick();
    tree = render(); await settle(); tree = render(); await settle(); tree = render();
    assert.equal(modal(tree).props.noticeSource.kind, 'action');
    let html = renderToStaticMarkup(createElement(TaskDetails, modal(tree).props));
    assert.equal(html.split(view.actionFeedback(action).message).length - 1, 1);
    fakeAPI.setFailure(true);
    await modal(tree).props.onAction(modal(tree).props.task, 'reconcile');
    tree = render();
    assert.equal(modal(tree).props.notice, '合成本次网络错误');
    fakeAPI.setTask({ ...base, latest_action: { ...action, state: 'failed' } });
    modal(tree).props.onRefresh(); tree = render(); await settle(); tree = render(); await settle(); tree = render();
    assert.equal(modal(tree).props.noticeError, true);
    assert.equal(modal(tree).props.noticeSource.kind, 'local');
    assert.equal(modal(tree).props.notice, '合成本次网络错误');
    html = renderToStaticMarkup(createElement(TaskDetails, modal(tree).props));
    assert.equal(html.split('合成本次网络错误').length - 1, 1);
    assert.equal(nodes(tree, n => n.props.className?.startsWith('assistant-notice')).length, 0);
    assert.deepEqual(fakeAPI.calls.filter(c => c[0] === 'action'), [['action', base.id, 'reconcile']]);
  });
});


test('native cooldown heading and help describe notification wait without requesting verification', async () => {
  await withPanel({ control: { state: 'running', desired_state: 'running', receiver_state: 'page_unavailable',
    reason_code: 'native_session_cooldown', heartbeat_at: new Date().toISOString() } }, async ({ tree }) => {
    const copy = nodes(tree, n => n.props.className === 'assistant-control-copy')[0];
    assert.match(renderToStaticMarkup(copy), /接口暂时等待/);
    assert.match(text(copy), /稍后继续读取通知/);
    assert.match(nodes(copy, n => n.type === 'h4')[0].props.title, /无需重发评论，也不用为这次接口等待输入验证码/);
    assert.doesNotMatch(text(copy), /无需重发评论，也不用为这次接口等待输入验证码/);
    assert.doesNotMatch(text(copy), /先恢复助手页面|等待本人核验|助手窗口/);
    assert.equal(buttons(tree).filter(n => ['开启助手', '暂停处理', '停止'].includes(text(n))).length, 3);
    assert.equal(nodes(tree, n => n.type === 'a' && n.props.href === 'http:\/\/127.0.0.1:8767/').length, 0);
    assert.equal(fakeAPI.calls.some(c => c[0] === 'control'), false);
  });
});

test('generic page failure stays uncertain and cannot be relabelled by old native session metadata', async () => {
  await withPanel({ control: { state: 'running', receiver_state: 'page_unavailable', reason_code: 'page_not_ready',
    native_session_status: { state: 'cooldown' }, heartbeat_at: new Date().toISOString() } }, async ({ tree }) => {
    const copy = nodes(tree, n => n.props.className === 'assistant-control-copy')[0];
    assert.match(text(copy), /收件暂时受阻，请核对通知状态/);
    const help = nodes(copy, n => n.type === 'h4')[0].props.title;
    assert.match(help, /后台通知或页面暂时无法读取/);
    assert.match(help, /不要重复发送评论/);
    assert.doesNotMatch(text(copy), /后台通知或页面暂时无法读取|不要重复发送评论/);
    assert.doesNotMatch(text(copy), /接口暂时等待|稍后继续读取通知|先恢复助手页面/);
  });
});

test('manual pause, stop and expired reports keep priority over receive-wait heading and help', async () => {
  for (const [delta, expected] of [
    [{ state: 'paused', desired_state: 'paused' }, /处理已暂停，新指令继续排队/],
    [{ state: 'stopped', desired_state: 'stopped' }, /助手已停止/],
    [{ heartbeat_at: '2000-01-01T00:00:00Z' }, /助手运行状态需要核对/],
  ]) await withPanel({ control: { state: 'running', desired_state: 'running', receiver_state: 'page_unavailable',
    reason_code: 'native_session_cooldown', heartbeat_at: new Date().toISOString(), ...delta } }, async ({ tree }) => {
    const copy = nodes(tree, n => n.props.className === 'assistant-control-copy')[0];
    assert.match(text(copy), expected);
    assert.doesNotMatch(text(copy), /接口暂时等待|稍后继续读取通知/);
  });
});


test('configuration failure controls badge and help while preserving the recovery alert and disabled Start', async () => {
  await withPanel({ configuration: { state: 'unavailable', reason_code: 'registry_missing_after_initialization' },
    control: { state: 'running', desired_state: 'running', receiver_state: 'page_unavailable',
      reason_code: 'native_session_cooldown', heartbeat_at: new Date().toISOString() } }, async ({ tree }) => {
    const copy = nodes(tree, n => n.props.className === 'assistant-control-copy')[0];
    assert.ok(nodes(tree, n => n.props.className?.includes('assistant-configuration-error'))[0]);
    assert.match(renderToStaticMarkup(copy), /设置需核对/);
    assert.match(text(copy), /先核对原有设置，任务记录会保留/);
    assert.match(nodes(copy, n => n.type === 'h4')[0].props.title, /请先核对原有设置，确认后再开启助手/);
    assert.doesNotMatch(renderToStaticMarkup(copy), /接口暂时等待|稍后继续读取通知|收件接口正在短暂等待/);
    assert.equal(buttons(tree).find(n => text(n) === '开启助手').props.disabled, true);
    assert.equal(nodes(tree, n => n.type === 'a' && n.props.href === 'http:\/\/127.0.0.1:8767/').length, 0);
    assert.equal(fakeAPI.calls.some(c => ['control', 'action'].includes(c[0])), false);
  });
});


test('already running or listening disables redundant Start including a normal cooldown', async () => {
  for (const change of [
    { state: 'running', receiver_state: 'ready' },
    { state: 'listening', receiver_state: 'ready' },
    { state: 'running', receiver_state: 'page_unavailable', reason_code: 'native_session_cooldown',
      native_session_status: { state: 'cooldown', generation: 1, refresh_count: 1,
        source: 'same_context', seal_state: 'sealed', reason_code: null,
        transient_failures: 2, last_transient_code: 'network_timeout', retry_after_seconds: 90 } },
  ]) {
    await withPanel({ capabilities: { start: true, pause: true, stop: true }, control: {
      ...change, desired_state: 'running', process_alive: true, heartbeat_at: new Date().toISOString(),
    } }, async ({ tree }) => {
      const start = buttons(tree).find(n => text(n) === '开启助手');
      assert.equal(start.props.disabled, true, JSON.stringify(change));
      assert.match(renderToStaticMarkup(start), /disabled=""/);
      assert.equal(buttons(tree).find(n => text(n) === '暂停处理').props.disabled, false);
      assert.equal(buttons(tree).find(n => text(n) === '停止').props.disabled, false);
      assert.equal(fakeAPI.calls.some(c => c[0] === 'control'), false);
    });
  }
});

test('paused stopped and recovery Start entries retain the existing fake action', async () => {
  for (const change of [
    { state: 'paused', desired_state: 'paused', process_alive: true },
    { state: 'running', desired_state: 'paused', process_alive: true },
    { state: 'stopped', desired_state: 'stopped', process_alive: false },
    { state: 'interrupted', desired_state: 'running', process_alive: false },
    { state: 'recovering', desired_state: 'running', process_alive: false },
    { state: 'running', desired_state: 'running', process_alive: false },
    { state: 'running', desired_state: 'running' },
  ]) {
    await withPanel({ capabilities: { start: true, pause: true, stop: true }, control: {
      ...change, heartbeat_at: new Date().toISOString(),
    } }, async ({ tree, settle }) => {
      const start = buttons(tree).find(n => text(n) === '开启助手');
      assert.equal(start.props.disabled, false, JSON.stringify(change));
      assert.doesNotMatch(renderToStaticMarkup(start), /disabled=""/);
      start.props.onClick(); await settle();
      assert.deepEqual(fakeAPI.calls.filter(c => c[0] === 'control'), [['control', 'start']]);
    });
  }
});

test('existing capability configuration and manual gates still disable Start', async () => {
  const control = { state: 'paused', desired_state: 'paused', process_alive: true, heartbeat_at: new Date().toISOString() };
  for (const change of [
    { capabilities: { start: false, pause: true, stop: true }, control },
    { capabilities: { start: true, pause: true, stop: true }, control, configuration: { state: 'unavailable' } },
    { capabilities: { start: true, pause: true, stop: true }, control: { ...control, manual_verification: {
      available: true, active: true, url: 'http://127.0.0.1:8767/',
    } } },
  ]) {
    await withPanel(change, async ({ tree }) => {
      assert.equal(buttons(tree).find(n => text(n) === '开启助手').props.disabled, true);
      assert.equal(fakeAPI.calls.some(c => c[0] === 'control'), false);
    });
  }
});
