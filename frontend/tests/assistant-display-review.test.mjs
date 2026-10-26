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
const sharedImports = {
  ...reactImports, '../assistantView': viewURL,
  '../api': asURL('export {};'), './AssistantPanel.display.css': asURL('export {};'),
  '../assistantDemo': asURL('export const demoOverview={};export const demoTasks=[];export function demoTaskDetail(){}'),
  './AssistantSettings': asURL('export default function Settings(){return null}'),
  './AssistantOperations': asURL('export default function Operations(){return null}'),
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
const fakeAPIURL = asURL(`export let calls=[],overview,task,failAction=false;
export function reset(o,t){calls=[];overview=o;task=t;failAction=false;}
export function setTask(t){task=t;}
export function setFailure(v){failAction=v;}
export function assistantErrorMessage(c,f){return f;}
export async function getAssistantOverview(){calls.push(['overview']);return overview;}
export async function listAssistantTasks(p){calls.push(['list',p]);return{items:[task],total:1};}
export async function getAssistantTask(id){calls.push(['detail',id]);return task;}
export async function controlAssistant(action){calls.push(['control',action]);return{overview};}
export async function actOnAssistantTask(id,action){calls.push(['action',id,action]);if(failAction)throw new Error('合成本次网络错误');return{accepted:true};}`);
const fakeAPI = await import(fakeAPIURL);
const Panel = (await import(await moduleURL('../src/components/AssistantPanel.tsx', { ...sharedImports, react: hookURL, '../api': fakeAPIURL }))).default;
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
    assert.match(text(tree), /仅反映可见通知，不代表全部提醒已收齐/);
    assert.equal(fakeAPI.calls.some(c => ['control', 'action'].includes(c[0])), false);
  });
});

test('configuration recovery CTA, manual verification entry and abnormal explanation are not folded', async () => {
  await withPanel({ configuration: { state: 'unavailable' }, control: { state: 'paused', receiver_state: 'verification_required', process_alive: true,
    heartbeat_at: new Date().toISOString(), manual_verification: { available: true, active: false, url: 'http://127.0.0.1:8767/' } } }, async ({ tree }) => {
    assert.ok(buttons(tree).some(n => text(n) === '核对原有设置'));
    assert.equal(nodes(tree, n => n.type === 'a' && n.props.href === 'http://127.0.0.1:8767/').length, 1);
    assert.match(text(tree), /不要把密码或验证码发到聊天|助手仍暂停/);
    assert.equal(buttons(tree).find(n => text(n) === '开启助手').props.disabled, true);
  });
});

test('data-format and worker errors retain their explanation even with a fresh ready receiver', async () => {
  for (const control of [
    { reason_code: 'native_notice_format_unavailable' },
    { worker_state: 'error' },
  ]) {
    await withPanel({ control: { state: 'running', receiver_state: 'ready', heartbeat_at: new Date().toISOString(), ...control } }, async ({ tree }) => {
      assert.match(text(tree), control.worker_state ? /工具处理出现问题，任务记录已保留/ : /不用为这个数据问题输入验证码/);
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
    assert.match(text(copy), /无需重发评论，也不用为这次接口等待输入验证码/);
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
    assert.match(text(copy), /后台通知或页面暂时无法读取/);
    assert.match(text(copy), /不要重复发送评论/);
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
    assert.match(text(copy), /请先核对原有设置，确认后再开启助手/);
    assert.doesNotMatch(renderToStaticMarkup(copy), /接口暂时等待|稍后继续读取通知|收件接口正在短暂等待/);
    assert.equal(buttons(tree).find(n => text(n) === '开启助手').props.disabled, true);
    assert.equal(nodes(tree, n => n.type === 'a' && n.props.href === 'http:\/\/127.0.0.1:8767/').length, 0);
    assert.equal(fakeAPI.calls.some(c => ['control', 'action'].includes(c[0])), false);
  });
});
