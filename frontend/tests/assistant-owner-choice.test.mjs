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
const ownerURL = await moduleURL('../src/components/AssistantOwnerAccessSettings.tsx', reactImports);
const owner = await import(ownerURL);
const view = await import(await moduleURL('../src/assistantView.ts'));
const api = await import(await moduleURL('../src/api.ts', { './sse': await moduleURL('../src/sse.ts') }));
const { TaskDetails } = await import(await moduleURL('../src/components/AssistantPanel.tsx', {
  ...reactImports, '../api': asURL('export {};'), '../assistantView': await moduleURL('../src/assistantView.ts'),
  '../assistantDemo': asURL('export const demoOverview = {}; export const demoTasks = []; export function demoTaskDetail() {}'),
  './AssistantSettings': asURL('export default function Settings() { return null; }'),
  './AssistantOperations': asURL('export default function Operations() { return null; }'),
  './NativeIntakeNotice': asURL('export default function NativeIntakeNotice() { return null; }'),
  './AssistantPanel.display.css': asURL('export {};'),
}, source => source.replace('function TaskDetails(', 'export function TaskDetails(')));
const { AssistantLibraryGrantRow } = await import(await moduleURL('../src/components/AssistantSettings.tsx', {
  ...reactImports, '../api': asURL('export {};'), '../assistantView': await moduleURL('../src/assistantView.ts'),
  './AssistantUsageSettings': asURL('export default function Usage() { return null; }'),
  './AssistantOwnerAccessSettings': asURL('export default function Owner() { return null; }'),
}));
const all = { configured: true, active: true, query_all_local: true, catalog_read_allowed: true, public_reply_allowed: true, version: 1 };
const blank = { configured: false, active: false, query_all_local: false, catalog_read_allowed: false, public_reply_allowed: false, version: 0 };
const props = { access: blank, local: true, verified: true, canManage: true, catalogCount: 6, onSave: async () => {} };
const renderOwner = override => renderToStaticMarkup(createElement(owner.default, { ...props, ...override }));
const renderDetails = task => renderToStaticMarkup(createElement(TaskDetails, { task, error: '', pending: '', demo: false, notice: '', noticeError: false,
  onClose() {}, onRefresh() {}, async onAction() {}, onOpenTask() {} }));

test('new or inactive owner flags are all off, even if per-library grants existed', () => {
  assert.deepEqual(owner.ownerAccessDraft(), { query_all_local: false, catalog_read_allowed: false, public_reply_allowed: false, grant_current_libraries: false });
  assert.deepEqual(owner.ownerAccessDraft({ ...all, active: false }), owner.ownerAccessDraft());
  assert.equal(owner.ownerAccessDraft({ ...all, grant_current_libraries: true }).grant_current_libraries, false);
  const html = renderOwner();
  assert.equal((html.match(/type="checkbox"/g) || []).length, 4);
  assert.equal((html.match(/checked=""/g) || []).length, 0);
  assert.match(html, /当前 6 个知识库/);
  assert.match(html, /未来新建的库不会自动获得保存权限/);
});

test('reading existing owner flags never grants current libraries or calls save', () => {
  let saves = 0;
  const html = renderOwner({ access: all, onSave: async () => { saves++; } });
  assert.equal((html.match(/checked=""/g) || []).length, 3);
  assert.equal(saves, 0);
  assert.match(html, /公开展示还需要同时允许下方公开回复/);
  assert.match(html, /未归入知识库的已入库视频/);
});

test('expired admin, unverified, remote and unread catalog all lock every owner control', () => {
  for (const change of [{ canManage: false }, { verified: false }, { local: false }, { unavailable: true }, { catalogCount: undefined }]) {
    const html = renderOwner(change);
    assert.equal((html.match(/type="checkbox"[^>]*disabled=""/g) || []).length, 4);
    assert.match(html, /type="submit"[^>]*disabled=""/);
  }
  assert.match(renderOwner({ catalogCount: undefined }), /未读取/);
  assert.doesNotMatch(renderOwner({ catalogCount: undefined }), /当前 0 个知识库/);
});

test('owner payload keeps every explicit false and requires directory before one-time grant', () => {
  const draft = owner.ownerAccessDraft();
  assert.deepEqual(owner.ownerAccessRequest(draft), { confirm: true, ...draft });
  assert.throws(() => owner.ownerAccessRequest({ ...draft, grant_current_libraries: true }), /先允许读取本人本机目录/);
  assert.throws(() => owner.ownerAccessRequest({ ...draft, public_reply_allowed: true }), /先选择全部资料或本机目录范围/);
});

test('owner API uses fixed local registry route and explicit body, no persisted session', async () => {
  const before = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response('{}', { status: 200 }); };
  try {
    const body = { confirm: true, query_all_local: true, catalog_read_allowed: true, public_reply_allowed: true, grant_current_libraries: true };
    await api.setAssistantLocalOwnerAccess('fixture-user', 'fixture-device', 'synthetic-admin', body);
    assert.equal(calls[0].url, '/api/assistant/registry/users/fixture-user/devices/fixture-device/local-owner-access');
    assert.equal(calls[0].options.method, 'PUT');
    assert.deepEqual(JSON.parse(calls[0].options.body), body);
    await assert.rejects(api.setAssistantLocalOwnerAccess('fixture-user', 'fixture-device', '', body), /先确认本机管理/);
    assert.equal(calls.length, 1);
  } finally { globalThis.fetch = before; }
});

const hookURL = asURL(`let states=[],cursor=0,effectCursor=0,effectDeps=[],pending=[];export function reset(){states=[];cursor=0;effectCursor=0;effectDeps=[];pending=[];}export function begin(){cursor=0;effectCursor=0;pending=[];}
  export function useState(initial){const i=cursor++;if(!(i in states))states[i]=typeof initial==='function'?initial():initial;return[states[i],value=>{states[i]=typeof value==='function'?value(states[i]):value;}];}
  export function useEffect(callback,deps){const i=effectCursor++;if(!effectDeps[i]||deps.some((value,index)=>!Object.is(value,effectDeps[i][index]))){effectDeps[i]=deps;pending.push(callback);}}
  export function flushEffects(){for(const callback of pending.splice(0))callback();}`);
const hooks = await import(hookURL);
const eventOwner = await import(await moduleURL('../src/components/AssistantOwnerAccessSettings.tsx', { ...reactImports, react: hookURL }));
function nodes(element, type, result = []) {
  if (Array.isArray(element)) { for (const item of element) nodes(item, type, result); return result; }
  if (!element || typeof element !== 'object') return result;
  if (element.type === type) result.push(element);
  nodes(element.props?.children, type, result); return result;
}
for (const failure of [false, true]) test(`one-time current-library grant clears after explicit ${failure ? 'failed' : 'successful'} submit`, async () => {
  hooks.reset(); const requests = [];
  const config = { ...props, onSave: async body => { requests.push(body); if (failure) throw new Error('isolated failure'); } };
  const render = () => { hooks.begin(); return eventOwner.default(config); };
  let tree = render();
  for (let index = 0; index < 4; index++) { nodes(tree, 'input')[index].props.onChange({ target: { checked: true } }); tree = render(); }
  assert.equal(requests.length, 0, 'checkboxes are not automatic authorization');
  nodes(tree, 'form')[0].props.onSubmit({ preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve)); tree = render();
  assert.equal(requests.length, 1); assert.equal(requests[0].grant_current_libraries, true);
  assert.equal(nodes(tree, 'input')[3].props.checked, false);
  assert.equal(nodes(tree, 'input')[0].props.checked, true);
  const expired = { ...config, canManage: false }; hooks.begin(); tree = eventOwner.default(expired);
  nodes(tree, 'form')[0].props.onSubmit({ preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve)); assert.equal(requests.length, 1);
});

test('catalog changes or unread state clear an unsubmitted one-time grant without calling API', () => {
  hooks.reset(); let calls = 0;
  let config = { ...props, catalogKey: 'six-library-ids', onSave: async () => { calls++; } };
  const render = () => { hooks.begin(); eventOwner.default(config); hooks.flushEffects(); hooks.begin(); return eventOwner.default(config); };
  let tree = render();
  nodes(tree, 'input')[1].props.onChange({ target: { checked: true } }); tree = render();
  nodes(tree, 'input')[3].props.onChange({ target: { checked: true } }); tree = render();
  assert.equal(nodes(tree, 'input')[3].props.checked, true);
  config = { ...config, catalogKey: 'seven-library-ids', catalogCount: 7 }; tree = render();
  assert.equal(nodes(tree, 'input')[3].props.checked, false); assert.equal(calls, 0);
  nodes(tree, 'input')[3].props.onChange({ target: { checked: true } }); tree = render();
  config = { ...config, unavailable: true }; tree = render();
  assert.equal(nodes(tree, 'input')[3].props.checked, false); assert.equal(nodes(tree, 'input')[3].props.disabled, true);
});

test('newly read revocation replaces stale edited owner flags without resubmitting', () => {
  hooks.reset(); let calls = 0;
  let config = { ...props, access: all, onSave: async () => { calls++; } };
  const render = () => { hooks.begin(); eventOwner.default(config); hooks.flushEffects(); hooks.begin(); return eventOwner.default(config); };
  let tree = render(); assert.equal(nodes(tree, 'input')[0].props.checked, true);
  config = { ...config, access: { ...all, version: 2, active: false } }; tree = render();
  assert.equal(nodes(tree, 'input')[0].props.checked, false); assert.equal(nodes(tree, 'input')[2].props.checked, false); assert.equal(calls, 0);
});

test('old backend missing owner field is unsupported, while new explicit false object allows first setup', async () => {
  assert.equal(owner.ownerAccessSupported(undefined), false);
  assert.equal(owner.ownerAccessSupported(blank), true);
  assert.equal(owner.ownerAccessSupported({ configured: false }), false);
  const old = renderOwner({ access: undefined });
  assert.match(old, /后台尚未加载此设置，更新并重启项目后再配置/);
  assert.match(old, /type="submit"[^>]*disabled=""/);
  assert.equal((old.match(/type="checkbox"[^>]*disabled=""/g) || []).length, 4);
  const first = renderOwner({ access: blank });
  assert.doesNotMatch(first, /后台尚未加载此设置/);
  assert.doesNotMatch(first.match(/<button type="submit"[^>]*>/)?.[0] || '', /disabled/);
  hooks.reset(); let calls = 0;
  hooks.begin(); const tree = eventOwner.default({ ...props, access: undefined, onSave: async () => { calls++; } });
  nodes(tree, 'form')[0].props.onSubmit({ preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve)); assert.equal(calls, 0);
});

test('active false library is visibly revoked with explicit regrant, despite missing revoked flag', () => {
  const grant = { grant_id: 'old-grant', knowledge_base_id: 6, name: '原有资料库', aliases: [], is_default: true,
    can_query: true, can_import: true, public_reply_allowed: true, active: false };
  const render = value => renderToStaticMarkup(createElement(AssistantLibraryGrantRow, {
    grant: value, editDisabled: false, revokeDisabled: false, onEdit() {}, onRevoke() {},
  }));
  const html = render(grant);
  assert.equal(view.assistantLibraryGrantActive(grant), false);
  assert.match(html, /已撤销/); assert.match(html, /重新授权/);
  assert.doesNotMatch(html, /允许入库|允许提问|允许公开回复|<em>默认/);
  const { active, ...historical } = grant;
  assert.equal(view.assistantLibraryGrantActive(historical), true);
  assert.match(render(historical), /允许入库/);
  assert.match(render({ ...historical, revoked: true }), /已撤销/);
});

test('all-local null library is explicitly named, and missing scope is never inferred as all', () => {
  assert.equal(view.assistantScopeLabel({ query_scope: 'all_local', knowledge_base_id: null }), '全部入库资料');
  assert.equal(view.assistantScopeLabel({ knowledge_base_id: null }), '尚未确定');
  assert.equal(view.assistantScopeLabel({ query_scope: 'all_local', knowledge_base_id: 6 }), '资料范围待核对');
  assert.equal(view.assistantScopeLabel({ knowledge_base_id: 6, details: { library_name: '原单库' } }), '原单库');
  const html = renderDetails({ id: 'all', kind: 'query', state: 'b_view_visible', query_scope: 'all_local', knowledge_base_id: null });
  assert.match(html, /全部入库资料/);
  assert.doesNotMatch(html, /知识库 0|尚未绑定/);
  const header = html.match(/<div class="assistant-detail-stages">([\s\S]*?)<\/div>/)?.[1] || '';
  assert.match(header, /已接收/); assert.match(header, /使用已有资料/); assert.match(header, /助手账号可见/);
  assert.equal((header.match(/class="assistant-stage /g) || []).length, 3);
});

test('waiting choice does not become import success or offer resume', () => {
  const task = { id: 'choice', kind: 'clarify', state: 'waiting_library', reply_state: 'b_view_visible', actions: { can_resume: true, can_reconcile: false },
    details: { library_choice: { state: 'waiting_library', option_count: 6 } } };
  assert.equal(view.intakeStage(task).label, '已接收');
  assert.equal(view.processingStage(task).label, '已询问，等待选择知识库');
  assert.equal(view.canResumeTask(task), false);
  assert.match(renderDetails(task), /选择前|不等于视频已经入库/);
  assert.doesNotMatch(renderDetails(task), /资料已完成/);
  const header = renderDetails(task).match(/<div class="assistant-detail-stages">([\s\S]*?)<\/div>/)?.[1] || '';
  assert.equal((header.match(/已询问，等待选择知识库/g) || []).length, 1);
  assert.match(header, /助手账号可见/);
});

test('selected choice links only an opaque local child and hides internal token fields', () => {
  const task = { id: 'choice', kind: 'clarify', state: 'b_view_visible', details: { library_choice: {
    state: 'handed_off', selected_library_name: '编程库', child_task_id: 'opaque-child', option_count: 6,
    nonce: 'SYNTHETIC_PRIVATE_NONCE', send_token: 'SYNTHETIC_PRIVATE_SEND', authorization: { secret: 'SYNTHETIC_PRIVATE_AUTH' },
  } } };
  const html = renderDetails(task);
  assert.match(html, /已选择，已建立保存任务/); assert.match(html, /已选知识库：编程库/); assert.match(html, /查看对应保存任务/);
  assert.doesNotMatch(html, /SYNTHETIC_PRIVATE/);
  assert.doesNotMatch(renderDetails({ ...task, details: { library_choice: { ...task.details.library_choice, child_task_id: 'https://evil.example' } } }), /查看对应保存任务|evil.example/);
  assert.equal(view.processingStage({ ...task, kind: 'save', import_state: 'succeeded' }).label, '资料已完成');
});

test('actual handed-off choice with positive ID and no name points to its save task', () => {
  const html = renderDetails({ id: 'parent', kind: 'clarify', state: 'b_view_visible', query_scope: 'all_local', knowledge_base_id: null,
    details: { library_name: '全部入库资料', library_choice: {
      state: 'handed_off', selected_knowledge_base_id: 5, child_task_id: 'ccccccccccccccccccccccccccccccc1', option_count: 6,
    } } });
  assert.match(html, /已选择目标知识库，请查看对应保存任务/);
  assert.match(html, /查看对应保存任务/);
  assert.doesNotMatch(html, /尚未确定本次保存到哪个知识库|已选知识库：全部入库资料|知识库 5/);
});

test('selected positive ID without a safe child waits for its related save task', () => {
  for (const child_task_id of [undefined, null, 'https://evil.example']) {
    const html = renderDetails({ id: 'parent', kind: 'clarify', state: 'waiting_library', details: { library_choice: {
      state: 'selected', selected_knowledge_base_id: 5, child_task_id,
    } } });
    assert.match(html, /已选定目标知识库，等待关联保存任务/);
    assert.doesNotMatch(html, /尚未确定本次保存到哪个知识库|查看对应保存任务|evil.example/);
  }
});

test('invalid or absent selected IDs do not claim a library choice', () => {
  for (const selected_knowledge_base_id of [undefined, null, 0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '5', true]) {
    const html = renderDetails({ id: 'parent', kind: 'clarify', state: 'waiting_library', details: { library_choice: {
      state: 'waiting_library', selected_knowledge_base_id,
    } } });
    assert.match(html, /尚未确定本次保存到哪个知识库/);
    assert.doesNotMatch(html, /已选择目标知识库|已选定目标知识库/);
  }
});
