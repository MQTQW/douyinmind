import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

// Execute the real component with inert hooks/JSX and fake API promises only.
const asURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
function labelState(context) {
  const visit = node => {
    if (ts.isVariableDeclaration(node) && ts.isArrayBindingPattern(node.name)
        && node.initializer && ts.isCallExpression(node.initializer) && node.initializer.expression.getText() === 'useState') {
      return ts.factory.updateVariableDeclaration(node, node.name, node.exclamationToken, node.type,
        ts.factory.updateCallExpression(node.initializer, node.initializer.expression, node.initializer.typeArguments,
          [...node.initializer.arguments, ts.factory.createStringLiteral(node.name.elements[0].name.text)]));
    }
    return ts.visitEachChild(node, visit, context);
  };
  return source => ts.visitNode(source, visit);
}
const reactURL = asURL(`
const current=()=>globalThis.__knowledgeRecoveryHarness;
const changed=(old,deps)=>!old || !deps || deps.some((value,index)=>!Object.is(value,old[index]));
export function useState(initial,label){const h=current();h.cursor++;if(!(label in h.state))h.state[label]=typeof initial==='function'?initial():initial;return[h.state[label],value=>{h.state[label]=typeof value==='function'?value(h.state[label]):value}];}
export function useRef(value){const h=current(),index=h.cursor++;return h.slots[index]??={current:value};}
export function useCallback(callback,deps){const h=current(),index=h.cursor++,old=h.slots[index];if(!old||changed(old.deps,deps))h.slots[index]={deps,callback};return h.slots[index].callback;}
export function useEffect(effect,deps){const h=current(),index=h.cursor++,old=h.slots[index];if(changed(old?.deps,deps)){h.slots[index]={deps,cleanup:old?.cleanup};h.effects.push(()=>{h.slots[index].cleanup?.();h.slots[index].cleanup=effect()})}}
`);
const jsxURL = asURL('export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;export const Fragment="fragment";');
const apiURL = asURL(`
const api=()=>globalThis.__knowledgeRecoveryHarness.api;
export const listIngestionTasks=(...args)=>api().tasks(...args);
export const retryIngestionTask=(...args)=>api().retry(...args);
export const getIngestionRetryResult=(...args)=>api().lookup(...args);
export const listDeletionOperations=(...args)=>api().deletions(...args);
export const resumeDeletionOperation=(...args)=>api().resume(...args);
export const knowledgeRecoveryMessage=(result,fallback)=>result.code==='INGESTION_BUSY'?'有任务正在处理，尚未重新提交。':fallback;
`);
const iconsURL = asURL('export const ListChecks=()=>null,RefreshCw=()=>null,RotateCcw=()=>null,Trash2=()=>null;');
const source = await readFile(new URL('../src/components/KnowledgeRecoveryPanel.tsx', import.meta.url), 'utf8');
let { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
  transformers: { before: [labelState] },
});
for (const [name, url] of Object.entries({ react: reactURL, 'react/jsx-runtime': jsxURL, '../api': apiURL, 'lucide-react': iconsURL })) {
  outputText = outputText.replaceAll(`'${name}'`, `'${url}'`).replaceAll(`"${name}"`, `"${url}"`);
}
const { default: KnowledgeRecoveryPanel } = await import(asURL(outputText));

const KEY = 'douyinmind.ingestion-retry-intents.v1';
const ID = '11111111-1111-4111-8111-111111111111';
const PRIVATE = 'PRIVATE_PROVIDER_KEY_BODY_NEVER_RENDERED';
const task = { task_id: 'old-task', status: 'interrupted', stage: 'recognition', progress: 1, total: 2,
  message: PRIVATE, metadata: { platform_item_ids: ['video-a', 'video-b'], knowledge_base_id: 7 },
  can_retry: true, needs_review: true, usage_state: 'unknown', created_at: 1_800_000_000 };
const operation = { operation_id: ID, platform_item_id: 'video-a', deletion_state: 'index_deleted',
  active: true, can_resume: true, created_at: '2026-11-04T10:00:00' };
const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function nodes(node, match) {
  if (node == null || typeof node !== 'object') return [];
  if (Array.isArray(node)) return node.flatMap(child => nodes(child, match));
  return [...(match(node) ? [node] : []), ...nodes(node.props?.children, match)];
}
function text(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'number' || typeof node === 'string') return String(node);
  return Array.isArray(node) ? node.map(text).join('') : text(node.props?.children);
}
const button = (tree, label) => nodes(tree, node => node.type === 'button' && node.props['aria-label'] === label)[0];
const named = (tree, value) => nodes(tree, node => node.type === 'button' && text(node) === value)[0];

function harness(overrides = {}, options = {}) {
  const storage = options.storage || new Map();
  const h = { cursor: 0, slots: [], effects: [], state: {}, storage, storageBroken: false, uuidCount: 0,
    confirms: [], confirmed: true, posts: [], lookups: [], resumes: [], lists: [], changed: [], submitted: [], connected: [] };
  h.api = {
    tasks: async (limit, offset) => { h.lists.push(['tasks', limit, offset]); return { success: true, items: [task], total: 1 }; },
    deletions: async (limit, offset, activeOnly) => { h.lists.push(['deletions', limit, offset, activeOnly]); return { success: true, items: [operation], total: 1 }; },
    retry: async (taskId, requestId) => { h.posts.push([taskId, requestId]); return { success: true, task_id: 'new-task' }; },
    lookup: async (taskId, requestId) => { h.lookups.push([taskId, requestId]); return { success: true, found: false }; },
    resume: async id => { h.resumes.push(id); return { success: true, operation_id: id, deletion_state: 'done' }; },
  };
  const props = { kind: 'tasks', refreshKey: 0, loggedIn: true,
    onConnectFavorites: () => h.connected.push(true), onStateChanged: () => h.changed.push(true),
    onSubmitted: id => h.submitted.push(id), ...overrides };
  const activate = () => {
    globalThis.__knowledgeRecoveryHarness = h;
    globalThis.window = {
      sessionStorage: {
        getItem(key) { if (h.storageBroken) throw new Error(PRIVATE); return storage.get(key) ?? null; },
        setItem(key, value) { if (h.storageBroken) throw new Error(PRIVATE); storage.set(key, value); },
        removeItem(key) { if (h.storageBroken) throw new Error(PRIVATE); storage.delete(key); },
      },
      crypto: { randomUUID() { h.uuidCount++; return `00000000-0000-4000-8000-${String(h.uuidCount).padStart(12, '0')}`; } },
    };
    globalThis.confirm = message => { h.confirms.push(message); return typeof h.confirmed === 'function' ? h.confirmed() : h.confirmed; };
  };
  h.render = updates => { Object.assign(props, updates); activate(); h.cursor = 0; return KnowledgeRecoveryPanel(props); };
  h.flush = async () => { activate(); h.effects.splice(0).forEach(effect => effect()); await settle(); };
  h.mount = async () => { h.render(); await h.flush(); return h.render(); };
  h.dispose = () => { activate(); h.slots.forEach(slot => slot?.cleanup?.()); };
  return h;
}

test('cancelling a retry makes no POST, UUID or pending request and states possible extra fees', async () => {
  const h = harness(); await h.mount(); h.confirmed = false;
  button(h.render(), '重新提交任务：old-task').props.onClick(); await settle();
  assert.deepEqual(h.posts, []); assert.equal(h.uuidCount, 0); assert.equal(h.storage.has(KEY), false);
  assert.match(h.confirms[0], /可能再次产生.*费用/);
  assert.match(h.confirms[0], /旧任务和未知费用保留.*已完成资料会复用/);
  h.dispose();
});

test('unknown POST result permits only GET lookup, then explicit confirmation reuses the same request ID', async () => {
  const h = harness(); await h.mount();
  h.api.retry = async (id, requestId) => { h.posts.push([id, requestId]); throw new Error(PRIVATE); };
  button(h.render(), '重新提交任务：old-task').props.onClick(); await settle();
  const requestId = h.posts[0][1];
  assert.equal(h.posts.length, 1); assert.equal(h.lookups.length, 0); assert.equal(h.uuidCount, 1);
  assert.equal(button(h.render(), '重新提交任务：old-task'), undefined);
  assert.doesNotMatch(text(h.render()), new RegExp(PRIVATE));
  const persisted = JSON.parse(h.storage.get(KEY));
  assert.deepEqual(persisted, { version: 1, intents: [{ taskId: 'old-task', requestId, unknown: true }] });
  button(h.render(), '核查重新提交结果：old-task').props.onClick(); await settle();
  assert.deepEqual(h.lookups, [['old-task', requestId]]); assert.equal(h.posts.length, 1);
  assert.match(text(h.render()), /暂未看到.*不表示未知费用为零/);
  h.confirmed = false;
  button(h.render(), '重新提交任务：old-task').props.onClick(); await settle();
  assert.equal(h.posts.length, 1);
  h.confirmed = true;
  h.api.retry = async (id, value) => { h.posts.push([id, value]); return { success: true, task_id: 'existing-retry' }; };
  button(h.render(), '重新提交任务：old-task').props.onClick(); await settle();
  assert.deepEqual(h.posts, [['old-task', requestId], ['old-task', requestId]]);
  assert.equal(h.uuidCount, 1); assert.deepEqual(h.submitted, ['existing-retry']); assert.equal(h.storage.has(KEY), false);
  h.dispose();
});

test('fast repeated retry clicks have one confirmation and one POST', async () => {
  const h = harness(); await h.mount(); const wait = deferred();
  h.api.retry = (id, value) => { h.posts.push([id, value]); return wait.promise; };
  const action = button(h.render(), '重新提交任务：old-task');
  action.props.onClick(); action.props.onClick();
  assert.equal(h.posts.length, 1); assert.equal(h.confirms.length, 1); assert.equal(h.uuidCount, 1);
  wait.resolve({ success: false, needs_review: true, message: PRIVATE }); await settle();
  assert.match(text(h.render()), /核查提交结果/); assert.doesNotMatch(text(h.render()), new RegExp(PRIVATE));
  h.dispose();
});

test('a persisted unknown intent survives remount and found:true only opens the already registered task', async () => {
  const storage = new Map([[KEY, JSON.stringify({ version: 1, intents: [{ taskId: 'old-task', requestId: ID, unknown: true }] })]]);
  const h = harness({ loggedIn: false }, { storage }); await h.mount();
  assert.equal(h.uuidCount, 0); assert.equal(h.posts.length, 0);
  h.api.lookup = async (id, requestId) => { h.lookups.push([id, requestId]); return { success: true, found: true, task: { ...task, task_id: 'registered-task' } }; };
  button(h.render(), '核查重新提交结果：old-task').props.onClick(); await settle();
  assert.deepEqual(h.lookups, [['old-task', ID]]); assert.deepEqual(h.submitted, ['registered-task']);
  assert.deepEqual(h.posts, []); assert.deepEqual(h.connected, []); assert.equal(h.confirms.length, 0);
  h.dispose();
});

test('found:true without a task ID or needs_review cannot offer another POST', async () => {
  const storage = new Map([[KEY, JSON.stringify({ version: 1, intents: [{ taskId: 'old-task', requestId: ID, unknown: true }] })]]);
  const h = harness({}, { storage }); await h.mount();
  h.api.lookup = async () => ({ success: true, found: true, needs_review: true, message: PRIVATE });
  button(h.render(), '核查重新提交结果：old-task').props.onClick(); await settle();
  assert.equal(button(h.render(), '重新提交任务：old-task'), undefined);
  assert.deepEqual(h.posts, []); assert.equal(h.storage.has(KEY), true); assert.doesNotMatch(text(h.render()), new RegExp(PRIVATE));
  h.dispose();
});

test('an inconclusive not-found response marked needs_review also stays read-only', async () => {
  const storage = new Map([[KEY, JSON.stringify({ version: 1, intents: [{ taskId: 'old-task', requestId: ID, unknown: true }] })]]);
  const h = harness({}, { storage }); await h.mount();
  h.api.lookup = async () => ({ success: true, found: false, needs_review: true });
  button(h.render(), '核查重新提交结果：old-task').props.onClick(); await settle();
  assert.equal(button(h.render(), '重新提交任务：old-task'), undefined);
  assert.deepEqual(h.posts, []); assert.equal(h.storage.has(KEY), true);
  h.dispose();
});

test('history and unknown request lookup are readable without connecting collection', async () => {
  const h = harness({ loggedIn: false }); await h.mount();
  assert.deepEqual(h.lists, [['tasks', 10, 0]]); assert.deepEqual(h.connected, []);
  button(h.render(), '重新提交任务：old-task').props.onClick(); await settle();
  assert.equal(h.connected.length, 1); assert.deepEqual(h.posts, []); assert.equal(h.uuidCount, 0); assert.equal(h.confirms.length, 0);
  h.dispose();
});

test('storage failure keeps the original unknown request in memory and warns to retain the page', async () => {
  const h = harness(); h.storageBroken = true; await h.mount();
  h.api.retry = async (id, value) => { h.posts.push([id, value]); throw new Error(PRIVATE); };
  button(h.render(), '重新提交任务：old-task').props.onClick(); await settle();
  assert.match(text(h.render()), /请保留当前页/);
  assert.equal(h.posts.length, 1); assert.ok(button(h.render(), '核查重新提交结果：old-task'));
  assert.equal(h.uuidCount, 1); assert.doesNotMatch(text(h.render()), new RegExp(PRIVATE));
  h.dispose();
});

test('load-more failures retain earlier rows and the next successful page advances from the same offset', async () => {
  const h = harness();
  h.api.tasks = async (limit, offset) => { h.lists.push(['tasks', limit, offset]); return { success: true, items: [task], total: 2 }; };
  await h.mount();
  h.api.tasks = async (limit, offset) => { h.lists.push(['tasks', limit, offset]); throw new Error(PRIVATE); };
  named(h.render(), '载入更多记录').props.onClick(); await settle();
  assert.equal(h.state.tasks.length, 1); assert.match(text(h.render()), /已显示的记录会保留/);
  assert.doesNotMatch(text(h.render()), new RegExp(PRIVATE));
  h.api.tasks = async (limit, offset) => { h.lists.push(['tasks', limit, offset]); return { success: true, items: [{ ...task, task_id: 'second-task', status: 'done' }], total: 2 }; };
  named(h.render(), '载入更多记录').props.onClick(); await settle();
  assert.deepEqual(h.state.tasks.map(item => item.task_id), ['old-task', 'second-task']);
  assert.deepEqual(h.lists.map(item => item[2]), [0, 1, 1]);
  h.dispose();
});

test('late task list response cannot replace deletion records after changing panel kind', async () => {
  const h = harness(); const waiting = deferred(); h.api.tasks = () => waiting.promise;
  h.render(); await h.flush(); h.render({ kind: 'deletions' }); await h.flush();
  waiting.resolve({ success: true, items: [task], total: 1 }); await settle();
  const tree = h.render(); assert.match(text(tree), /待完成的资料删除/); assert.doesNotMatch(text(tree), /old-task/);
  assert.equal(h.state.tasks.length, 0); assert.equal(h.state.deletions[0].operation_id, ID);
  h.dispose();
});

test('cancelled deletion resume and stale done-operation handlers cannot issue a POST', async () => {
  const h = harness({ kind: 'deletions' }); await h.mount(); h.confirmed = false;
  const action = button(h.render(), `继续原删除操作：${ID}`);
  action.props.onClick(); await settle(); assert.deepEqual(h.resumes, []);
  h.confirmed = true; h.state.deletions = [{ ...operation, active: false, deletion_state: 'done', can_resume: true }]; h.render();
  action.props.onClick(); await settle(); assert.deepEqual(h.resumes, []); assert.equal(h.confirms.length, 1);
  h.dispose();
});

test('deletion resume always uses its original operation ID and never starts ingestion or a new deletion', async () => {
  const h = harness({ kind: 'deletions', loggedIn: false }); await h.mount();
  button(h.render(), `继续原删除操作：${ID}`).props.onClick(); await settle();
  assert.deepEqual(h.resumes, [ID]); assert.deepEqual(h.posts, []); assert.equal(h.uuidCount, 0);
  assert.match(h.confirms[0], /只继续原删除操作.*最终提交后/s); assert.equal(h.changed.length, 1);
  h.dispose();
});

test('metadata and billing gaps never become zero cost and raw task error messages stay hidden', async () => {
  const h = harness(); h.api.tasks = async () => ({ success: true, items: [{ ...task, metadata: undefined }], total: 1 }); await h.mount();
  const rendered = text(h.render());
  assert.match(rendered, /资料范围未记录完整/); assert.match(rendered, /未知用量和费用不会按零计算/);
  assert.doesNotMatch(rendered, new RegExp(PRIVATE)); assert.doesNotMatch(rendered, /费用[:：]\s*0|0\s*tokens/);
  h.dispose();
});

test('unknown retry intent outside the loaded page remains findable through read-only lookup', async () => {
  const storage = new Map([[KEY, JSON.stringify({ version: 1, intents: [{ taskId: 'older-hidden-task', requestId: ID, unknown: true }] })]]);
  const h = harness({ loggedIn: false }, { storage }); await h.mount();
  assert.ok(button(h.render(), '核查重新提交结果：older-hidden-task'));
  button(h.render(), '核查重新提交结果：older-hidden-task').props.onClick(); await settle();
  assert.deepEqual(h.lookups, [['older-hidden-task', ID]]); assert.deepEqual(h.posts, []); assert.equal(h.confirms.length, 0);
  h.dispose();
});

test('known zero-work receipt is distinguished from a legacy unrecorded zero without inventing zero fees', async () => {
  const h = harness();
  h.api.tasks = async () => ({ success: true, items: [
    { ...task, task_id: 'known-empty', status: 'done', can_retry: false, progress: 0, total: 0, metadata: { platform_item_ids: [] } },
    { ...task, task_id: 'legacy-zero', progress: 0, total: 0, metadata: undefined },
  ], total: 2 });
  await h.mount();
  const rows = nodes(h.render(), node => node.props?.className === 'resource-row flex-wrap');
  assert.match(text(rows[0]), /0 \/ 0 视频 · 无需重复处理/);
  assert.match(text(rows[1]), /进度未完整记录.*资料范围未记录完整/);
  assert.match(text(h.render()), /未知用量和费用不会按零计算/);
  h.dispose();
});

test('confirmed completion cannot regain a resume action through a stale active-list reply', async () => {
  const h = harness({ kind: 'deletions' }); await h.mount();
  const old = button(h.render(), `继续原删除操作：${ID}`);
  old.props.onClick(); await settle();
  // The fixture list deliberately still reports the previous active state.
  assert.equal(button(h.render(), `继续原删除操作：${ID}`), undefined);
  old.props.onClick(); await settle();
  assert.deepEqual(h.resumes, [ID]); assert.equal(h.confirms.length, 1);
  h.dispose();
});

test('late response from an unmounted volatile panel cannot erase a newer stored request ID', async () => {
  const storage = new Map();
  const first = harness({}, { storage }); first.storageBroken = true; await first.mount();
  const waiting = deferred();
  first.api.retry = (id, requestId) => { first.posts.push([id, requestId]); return waiting.promise; };
  button(first.render(), '重新提交任务：old-task').props.onClick(); await settle(); first.dispose();
  storage.set(KEY, JSON.stringify({ version: 1, intents: [{ taskId: 'old-task', requestId: ID, unknown: true }] }));
  const second = harness({}, { storage }); await second.mount();
  waiting.resolve({ success: true, task_id: 'earlier-registered-task' }); await settle();
  assert.equal(JSON.parse(storage.get(KEY)).intents[0].requestId, ID);
  assert.deepEqual(first.submitted, []); assert.deepEqual(second.posts, []);
  second.dispose();
});
