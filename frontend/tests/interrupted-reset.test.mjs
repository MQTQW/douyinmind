import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

// Execute the production reset button's actual event callback with fake API
// and state collaborators. No browser, server, React hook copy or AI call.
const source = await readFile(new URL('../src/components/SourcesPanel.tsx', import.meta.url), 'utf8');
const tree = ts.createSourceFile('SourcesPanel.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let callback;
const declarations = new Map();
function visit(node) {
  if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
    declarations.set(node.name.text, node.initializer);
  }
  if (ts.isJsxElement(node) && node.openingElement.tagName.getText(tree) === 'button'
      && node.getText(tree).includes('重置失败/卡住的视频')) {
    const attribute = node.openingElement.attributes.properties.find(item => item.name?.getText(tree) === 'onClick');
    callback = attribute?.initializer?.expression;
  }
  ts.forEachChild(node, visit);
}
visit(tree);
assert.ok(callback, 'Production recovery button must remain present');
if (ts.isIdentifier(callback)) callback = declarations.get(callback.text);
assert.ok(callback && ts.isArrowFunction(callback), 'Recovery button must resolve to an event callback');
const { outputText } = ts.transpileModule(`const resetHandler = ${callback.getText(tree)};`, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.None },
});

function harness(reset, refresh = async () => true) {
  const state = { alerts: [], calls: [], videoKey: 4, baseKey: 7, resetting: false,
    message: '', selection: new Map([['kept-video', { platform_item_id: 'kept-video', status: 'done' }]]) };
  const bindings = {
    api: {
      resetFailedVideos: async () => { state.calls.push('reset'); return reset(); },
      syncKnowledge: () => assert.fail('Reset must never start ingestion'),
    },
    fetchStats: async () => { state.calls.push('stats'); return refresh(); },
    setVideosRefreshKey: update => { state.calls.push('videos'); state.videoKey = update(state.videoKey); },
    setBasesRefreshKey: update => { state.calls.push('bases'); state.baseKey = update(state.baseKey); },
    setResettingVideos: value => { state.resetting = value; },
    resettingVideosRef: { current: false },
    setBuildMessage: value => { state.message = value; },
    setSelectedVideos: () => assert.fail('Existing selection must be preserved'),
    openBuildConfirm: () => assert.fail('Recovery must not open an automatic import'),
    alert: value => state.alerts.push(value),
  };
  const handler = new Function(...Object.keys(bindings), `${outputText}; return resetHandler;`)(...Object.values(bindings));
  return { state, handler, busy: bindings.resettingVideosRef };
}

test('a busy success:false response is explained and never treated as reset success', async () => {
  const { state, handler, busy } = harness(async () => ({ success: false }));
  await handler();
  assert.equal(state.alerts.length, 1);
  assert.match(state.alerts[0], /任务|处理|等待|排队/);
  assert.deepEqual(state.calls, ['reset']);
  assert.equal(state.videoKey, 4);
  assert.equal(state.baseKey, 7);
  assert.equal(busy.current, false);
  assert.equal(state.resetting, false);
});

test('successful explicit recovery refreshes statistics and both row caches without clearing selection', async () => {
  const { state, handler, busy } = harness(async () => ({ success: true, reset_count: 3 }));
  const selection = state.selection;
  await handler();
  assert.equal(state.calls[0], 'reset');
  for (const action of ['reset', 'stats', 'videos', 'bases']) {
    assert.equal(state.calls.filter(call => call === action).length, 1);
  }
  assert.equal(state.videoKey, 5);
  assert.equal(state.baseKey, 8);
  assert.equal(state.selection, selection);
  assert.match(state.message, /3/);
  assert.match(state.message, /勾选|选择/);
  assert.equal(state.alerts.length, 0);
  assert.equal(busy.current, false);
});

test('a network failure leaves rows and selection intact and permits another attempt', async () => {
  const { state, handler, busy } = harness(async () => { throw new Error('synthetic network failure'); });
  await handler();
  assert.equal(state.alerts.length, 1);
  assert.match(state.alerts[0], /重置失败/);
  assert.deepEqual(state.calls, ['reset']);
  assert.equal(busy.current, false);
  assert.equal(state.resetting, false);
});

test('fast repeated clicks issue one reset and do not automatically import', async () => {
  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  const { state, handler, busy } = harness(() => pending);
  const first = handler();
  assert.equal(busy.current, true);
  await handler();
  assert.deepEqual(state.calls, ['reset']);
  finish({ success: true, reset_count: 1 });
  await first;
  for (const action of ['reset', 'stats', 'videos', 'bases']) {
    assert.equal(state.calls.filter(call => call === action).length, 1);
  }
  assert.equal(busy.current, false);
});

for (const [name, refresh] of [
  ['unsuccessful refresh', async () => false],
  ['failed refresh request', async () => { throw new Error('synthetic refresh outage'); }],
]) {
  test(`successful reset followed by ${name} is reported as reset done, not reset failed`, async () => {
    const { state, handler } = harness(async () => ({ success: true, reset_count: 2 }), refresh);
    const selection = state.selection;
    await handler();
    assert.equal(state.alerts.length, 0);
    assert.match(state.message, /已重置.*2/);
    assert.match(state.message, /未刷新|刷新失败|刷新.*重试/);
    assert.doesNotMatch(state.message, /重置失败/);
    assert.equal(state.videoKey, 5);
    assert.equal(state.baseKey, 8);
    assert.equal(state.selection, selection);
  });
}
