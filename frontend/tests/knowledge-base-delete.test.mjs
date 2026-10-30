import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

// Inert JSX/hooks and mocked fetch only; these tests cannot delete product data.
const asURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
function labelState(context) {
  const visit = node => {
    if (ts.isVariableDeclaration(node) && ts.isArrayBindingPattern(node.name)
      && ts.isCallExpression(node.initializer) && node.initializer.expression.getText() === 'useState') {
      return ts.factory.updateVariableDeclaration(node, node.name, node.exclamationToken, node.type,
        ts.factory.updateCallExpression(node.initializer, node.initializer.expression, node.initializer.typeArguments,
          [...(node.initializer.arguments.length ? node.initializer.arguments : [ts.factory.createVoidZero()]),
            ts.factory.createStringLiteral(node.name.elements[0].name.text)]));
    }
    return ts.visitEachChild(node, visit, context);
  };
  return source => ts.visitNode(source, visit);
}
async function moduleURL(path, imports = {}, jsx = false) {
  const input = await readFile(new URL(path, import.meta.url), 'utf8');
  let { outputText } = ts.transpileModule(input, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
    transformers: jsx ? { before: [labelState] } : undefined,
  });
  for (const [name, url] of Object.entries(imports)) {
    outputText = outputText.replaceAll(`'${name}'`, `'${url}'`).replaceAll(`"${name}"`, `"${url}"`);
  }
  return asURL(outputText);
}
const apiModule = await import(await moduleURL('../src/api.ts', { './sse': await moduleURL('../src/sse.ts') }));
const json = body => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
async function withFetch(mock, run) {
  const original = globalThis.fetch;
  globalThis.fetch = mock;
  try { await run(); } finally { globalThis.fetch = original; }
}

test('library deletion rejects non-positive, non-integer and unsafe IDs without requesting anything', async () => {
  await withFetch(() => { assert.fail('invalid ID must not reach an API'); }, async () => {
    for (const id of [0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '4', null, undefined]) {
      await assert.rejects(apiModule.deleteKnowledgeBase(id), /正整数/);
    }
  });
});

test('library deletion uses only the local DELETE endpoint with an empty JSON body and no admin credentials', async () => {
  const calls = [];
  await withFetch(async (url, options) => {
    calls.push(url);
    assert.equal(url, '/api/knowledge-bases/4');
    assert.equal(options.method, 'DELETE');
    assert.deepEqual(JSON.parse(options.body), {});
    const headers = new Headers(options.headers);
    assert.equal(headers.get('Content-Type'), 'application/json');
    assert.equal(headers.get('Authorization'), null);
    assert.equal(headers.get('X-Assistant-Admin-Token'), null);
    return json({ success: true, deleted_id: 4, removed_count: 0 });
  }, async () => {
    assert.deepEqual(await apiModule.deleteKnowledgeBase(4), { success: true, deleted_id: 4, removed_count: 0 });
  });
  assert.deepEqual(calls, ['/api/knowledge-bases/4']);
});

const reactURL = asURL(`
const current=()=>globalThis.__knowledgeDeleteHarness;
const changed=(old,deps)=>!old || !deps || deps.some((value,index)=>!Object.is(value,old[index]));
export function useState(initial,label) {
  const h=current();h.cursor++;
  if (!(label in h.state)) h.state[label]=typeof initial==='function'?initial():initial;
  return [h.state[label],value=>{h.state[label]=typeof value==='function'?value(h.state[label]):value;}];
}
export function useRef(value) {
  const h=current(),index=h.cursor++;
  return h.slots[index]??={current:value};
}
export function useCallback(callback,deps) {
  const h=current(),index=h.cursor++,old=h.slots[index];
  if (!old || changed(old.deps,deps)) h.slots[index]={deps,callback};
  return h.slots[index].callback;
}
export function useEffect(effect,deps) {
  const h=current(),index=h.cursor++,old=h.slots[index];
  if (changed(old?.deps,deps)) {
    h.slots[index]={deps,cleanup:old?.cleanup};
    h.effects.push(()=>{h.slots[index].cleanup?.();h.slots[index].cleanup=effect();});
  }
}
`);
const jsxURL = asURL('export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;export const Fragment="fragment";');
const apiURL = asURL('export const listKnowledgeBases=(...args)=>globalThis.__knowledgeDeleteHarness.api.list(...args);export const deleteKnowledgeBase=(...args)=>globalThis.__knowledgeDeleteHarness.api.delete(...args);');
const iconsURL = asURL('export const '+['Folder','FolderOpen','Library','Pencil','Plus','RefreshCw','Trash2'].map(name=>name+'=()=>null').join(',')+';');
const stubURL = asURL('export default function CreateFixture(){return null;}');
const { default: KnowledgeBasesPanel } = await import(await moduleURL('../src/components/KnowledgeBasesPanel.tsx', {
  react: reactURL, 'react/jsx-runtime': jsxURL, '../api': apiURL, 'lucide-react': iconsURL,
  './KnowledgeBaseCreateModal': stubURL,
}, true));
const first = { id: 4, name: '第一资料库', video_count: 2, ready_count: 2 };
const second = { id: 8, name: '第二资料库', video_count: 0, ready_count: 0 };
const settle = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness(overrides = {}, seed = {}) {
  const selected = [], deleted = [], lists = [], busy = [], events = [];
  const h = { cursor: 0, slots: [], effects: [], state: { bases: [first, second], loading: false, deleteSupported: true, ...seed },
    confirms: [], confirm: true, calls: [], api: {
      list: async () => ({ success: true, items: [first, second], capabilities: { delete_knowledge_base: true } }),
      delete: async id => { h.calls.push(id); return { success: true, deleted_id: id, removed_count: 0 }; },
    } };
  const props = { selectedId: first.id, refreshKey: 0, disabled: false, onRefresh() {},
    onSelect: value => selected.push(value), onCreated() {},
    onListLoaded: value => lists.push(value), onBusyChange: value => { busy.push(value); events.push(value ? 'busy' : 'idle'); },
    ...overrides };
  globalThis.confirm = message => { const active = globalThis.__knowledgeDeleteHarness; active.confirms.push(message); return active.confirm; };
  const render = changes => {
    Object.assign(props, changes); globalThis.__knowledgeDeleteHarness = h; h.cursor = 0;
    return KnowledgeBasesPanel(props);
  };
  const flush = async () => {
    globalThis.__knowledgeDeleteHarness = h;
    h.effects.splice(0).forEach(effect => effect());
    await settle();
  };
  return { ...h, state: h.state, selected, deleted, lists, busy, events, render, flush,
    setConfirm: value => { h.confirm = value; },
    dispose: () => h.slots.forEach(slot => slot?.cleanup?.()),
  };
}
function all(node, predicate) {
  if (node == null || typeof node !== 'object') return [];
  if (Array.isArray(node)) return node.flatMap(child => all(child, predicate));
  return [...(predicate(node) ? [node] : []), ...all(node.props?.children, predicate)];
}
function text(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  return Array.isArray(node) ? node.map(text).join('') : text(node.props?.children);
}
const deleteButton = (tree, base = first) => all(tree, node => node.type === 'button' && node.props['aria-label'] === `删除知识库：${base.name}`)[0];
const deletedHandler = h => id => { h.deleted.push(id); h.events.push('deleted'); };

test('an older or unread backend cannot enable library deletion until its capability is read', async () => {
  const h = harness({}, {deleteSupported:false});
  let control = deleteButton(h.render());
  assert.equal(control.props.disabled,true);control.props.onClick();await settle();assert.deepEqual(h.calls,[]);
  await h.flush();
  control=deleteButton(h.render());assert.equal(control.props.disabled,false);
  h.state.deleteSupported=false;
  h.api.list=async()=>({success:true,items:[first,second]});
  h.render({refreshKey:1});await h.flush();
  control=deleteButton(h.render());assert.equal(control.props.disabled,true);control.props.onClick();await settle();assert.deepEqual(h.calls,[]);
});

test('cancelled confirmation explains retained originals and never requests deletion', async () => {
  const h = harness();
  h.setConfirm(false);
  deleteButton(h.render()).props.onClick(); await settle();
  assert.equal(h.confirms.length, 1);
  assert.match(h.confirms[0], /第一资料库/);
  assert.match(h.confirms[0], /只删除知识库及其成员关系，视频原文会保留/);
  assert.deepEqual(h.calls, []);
  assert.deepEqual(h.busy, []);
  assert.deepEqual(h.state.bases, [first, second]);
  assert.deepEqual(h.selected, []);
});

test('busy and experience-mode disabled states block deletion even if a handler is called directly', async () => {
  for (const disabledReason of ['请等待当前回答完成', '体验模式尚未连接抖音']) {
    const h = harness({ disabled: true, disabledReason });
    const control = deleteButton(h.render());
    assert.equal(control.props.disabled, true);
    assert.equal(control.props.title, disabledReason);
    control.props.onClick(); await settle();
    assert.deepEqual(h.confirms, []);
    assert.deepEqual(h.calls, []);
    assert.deepEqual(h.busy, []);
  }
});

test('fast repeated or second-library clicks send one request and keep the mutation locked across a disabled prop change', async () => {
  const pending = deferred();
  const h = harness();
  h.api.delete = id => { h.calls.push(id); return pending.promise; };
  const originalTree = h.render();
  deleteButton(originalTree).props.onClick();
  deleteButton(originalTree).props.onClick();
  deleteButton(originalTree, second).props.onClick();
  assert.deepEqual(h.calls, [first.id]);
  assert.equal(h.confirms.length, 1);
  assert.deepEqual(h.busy, [true]);
  const locked = h.render({ disabled: true, onDeleted: deletedHandler(h) });
  assert.ok(all(locked, node => node.type === 'button' && node.props['aria-label']?.startsWith('删除知识库：')).every(node => node.props.disabled));
  pending.resolve({ success: true, deleted_id: first.id, removed_count: 2 }); await settle();
  assert.deepEqual(h.state.bases, [second]);
  assert.deepEqual(h.deleted, [first.id]);
  assert.deepEqual(h.busy, [true, false]);
  assert.deepEqual(h.events, ['busy', 'deleted', 'idle']);
});

test('a confirmed empty current library removes only its own row and uses the current-scope fallback', async () => {
  const h = harness({ selectedId: second.id });
  deleteButton(h.render(), second).props.onClick(); await settle();
  assert.deepEqual(h.state.bases, [first]);
  assert.deepEqual(h.lists, [[first]]);
  assert.deepEqual(h.selected, [null]);
  assert.deepEqual(h.busy, [true, false]);
});

test('deleting another library preserves the current session while notifying the parent when supplied', async () => {
  const h = harness();
  h.render({ onDeleted: deletedHandler(h) });
  deleteButton(h.render(), second).props.onClick(); await settle();
  assert.deepEqual(h.state.bases, [first]);
  assert.deepEqual(h.selected, []);
  assert.deepEqual(h.deleted, [second.id]);
  assert.deepEqual(h.lists, [[first]]);
  const fallback = harness();
  deleteButton(fallback.render(), second).props.onClick(); await settle();
  assert.deepEqual(fallback.selected, []);
});

test('failure and malformed confirmation preserve the complete list, report an error and release busy', async () => {
  const responses = [
    { success: false, deleted_id: first.id, removed_count: 2 },
    { success: true, deleted_id: second.id, removed_count: 2 },
    { success: true, deleted_id: first.id, removed_count: -1 },
    { success: true, deleted_id: first.id },
    new Error('synthetic network failure'),
  ];
  for (const response of responses) {
    const h = harness();
    const originalList = h.state.bases;
    h.api.delete = async id => { h.calls.push(id); if (response instanceof Error) throw response; return response; };
    deleteButton(h.render({ onDeleted: deletedHandler(h) })).props.onClick(); await settle();
    assert.equal(h.state.bases, originalList);
    assert.deepEqual(h.selected, []);
    assert.deepEqual(h.deleted, []);
    assert.deepEqual(h.lists, []);
    assert.deepEqual(h.busy, [true, false]);
    assert.equal(h.state.deletingId, null);
    assert.match(text(all(h.render(), node => node.props?.role === 'alert')[0]), /删除.*未确认/);
  }
});

test('a failed attempt can be retried and an earlier list response cannot resurrect a confirmed deleted library', async () => {
  const stale = deferred();
  const h = harness();
  h.api.list = () => stale.promise;
  h.api.delete = async id => { h.calls.push(id); throw new Error('synthetic failure'); };
  h.render(); await h.flush();
  deleteButton(h.render()).props.onClick(); await settle();
  assert.deepEqual(h.state.bases, [first, second]);
  h.api.delete = async id => { h.calls.push(id); return { success: true, deleted_id: id, removed_count: 2 }; };
  deleteButton(h.render()).props.onClick(); await settle();
  stale.resolve({ success: true, items: [first, second] }); await settle();
  assert.deepEqual(h.state.bases, [second]);
  assert.deepEqual(h.calls, [first.id, first.id]);
  assert.deepEqual(h.busy, [true, false, true, false]);
  assert.equal(h.state.loading, false);
});

test('completion uses the latest selected scope instead of resetting a different conversation', async () => {
  const pending = deferred();
  const h = harness();
  h.api.delete = id => { h.calls.push(id); return pending.promise; };
  deleteButton(h.render()).props.onClick();
  h.render({ selectedId: second.id });
  pending.resolve({ success: true, deleted_id: first.id, removed_count: 2 }); await settle();
  assert.deepEqual(h.state.bases, [second]);
  assert.deepEqual(h.selected, []);
});

test('unmounted deletion completion does not update another view or report a deleted scope', async () => {
  const pending = deferred();
  const h = harness();
  h.api.delete = id => { h.calls.push(id); return pending.promise; };
  h.render({ onDeleted: deletedHandler(h) }); await h.flush();
  h.lists.length = 0;
  deleteButton(h.render()).props.onClick();
  h.dispose();
  pending.resolve({ success: true, deleted_id: first.id, removed_count: 2 }); await settle();
  assert.deepEqual(h.deleted, []);
  assert.deepEqual(h.selected, []);
  assert.deepEqual(h.lists, []);
  assert.deepEqual(h.busy, [true, false]);
});
