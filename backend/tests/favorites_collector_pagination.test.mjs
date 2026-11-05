/**
 * Execute the collector's actual embedded pagination function with authored
 * pages only. This does not import Python/config, launch a browser, or make
 * network, model, child-process, or real-database calls.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const collectorPath = fileURLToPath(new URL('../app/services/douyin_collector.py', import.meta.url));
const collectorSource = readFileSync(collectorPath, 'utf8');
const embedded = collectorSource.match(/result\s*=\s*page\.evaluate\("""([\s\S]*?)"""\)/)?.[1];
assert.ok(embedded, 'the actual collector pagination JavaScript must be found');

function collection(id = '101', count = 1) {
  return { collectionFolderId: id, collectionFolderName: `Synthetic ${id}`, videoTotal: count };
}
function video(id = '1001') {
  return { awemeId: id, itemTitle: `Synthetic ${id}`, authorInfo: { nickname: 'Authored fixture' },
    video: { duration: 1000 } };
}
function page(data = [], hasMore = false, cursor = 0) {
  return { statusCode: 0, data, hasMore, cursor };
}

async function collect(collectionPages, videoPages = {}, options = {}) {
  const calls = [];
  const requiredModules = [];
  let waits = 0;
  let clock = 0;
  let collectionIndex = 0;
  const videoIndexes = new Map();
  const take = (pages, index) => {
    assert.ok(calls.length <= 500, 'fixture has a finite pagination-call budget');
    assert.ok(Array.isArray(pages) && index < pages.length, 'unexpected extra pagination request');
    return structuredClone(pages[index]);
  };
  // The source resolves the API module by inspecting these two route strings.
  function markerModule() {
    return ['/aweme/v1/web/collects/list/', '/aweme/v1/web/collects/video/list/'];
  }
  const defaultApi = {
    async So(args) {
      calls.push({ kind: 'collections', ...args });
      return take(collectionPages, collectionIndex++);
    },
    async d6(args) {
      calls.push({ kind: 'videos', ...args });
      const id = String(args.collectsId);
      const index = videoIndexes.get(id) ?? 0;
      videoIndexes.set(id, index + 1);
      return take(videoPages[id], index);
    },
  };
  const api = options.apiFactory ? options.apiFactory(defaultApi) : defaultApi;
  function requireModule(id) {
    const factory = requireModule.m[id];
    assert.equal(typeof factory, 'function', 'required module must exist');
    const source = Function.prototype.toString.call(factory);
    assert.ok(source.includes('/aweme/v1/web/collects/list/')
      && source.includes('/aweme/v1/web/collects/video/list/'), 'only the verified API factory may run');
    requiredModules.push(String(id));
    if (options.requireError) throw new Error('authored module initialization failure');
    return api;
  }
  requireModule.m = options.modules ?? { 7: markerModule };
  const chunks = [];
  chunks.push = () => requireModule;
  const window = options.noWebpack ? {} : { webpackChunkdouyin_web: chunks };
  const context = vm.createContext({ window, Date: { now: () => clock },
    setTimeout(resolve, milliseconds) {
      waits++; clock += milliseconds;
      options.onWait?.({ waits, calls, requiredModules, requireModule, markerModule, window, chunks, api, defaultApi });
      resolve();
    },
  }, {
    name: 'authored-favorites-pagination-only',
    codeGeneration: { strings: false, wasm: false },
  });
  // Each awaited API call uses the bounded fixture above. vm is not an OS
  // sandbox; the executed expression is the reviewed repository function.
  const result = await new vm.Script(`(${embedded})()`, { filename: collectorPath })
    .runInContext(context, { timeout: 1000 });
  return { result, calls, waits, requiredModules, clock };
}

function incomplete(result) {
  assert.equal(result?.ok, false, 'a partial or unknown snapshot must fail closed');
  assert.notEqual(result?.complete, true, 'a failed snapshot cannot assert completeness');
}

test('specific API module can arrive after DOM-ready without an early collection request', async () => {
  const { result, calls, waits, requiredModules } = await collect([page()], {}, {
    modules: {},
    onWait(state) {
      assert.equal(state.calls.length, 0);
      assert.equal(state.requiredModules.length, 0);
      if (state.waits === 2) state.requireModule.m[7] = state.markerModule;
    },
  });
  assert.equal(result.complete, true);
  assert.equal(calls.length, 1);
  assert.equal(waits, 2);
  assert.deepEqual(requiredModules, ['7']);
});

test('webpack itself can become ready before the collection module', async () => {
  const { result, waits, calls } = await collect([page()], {}, {
    noWebpack: true,
    onWait(state) {
      assert.equal(state.calls.length, 0);
      if (state.waits === 3) state.window.webpackChunkdouyin_web = state.chunks;
    },
  });
  assert.equal(result.complete, true);
  assert.equal(waits, 3);
  assert.equal(calls.length, 1);
});

test('verified module exports can become available without requiring the factory again', async () => {
  const { result, requiredModules, waits } = await collect([page()], {}, {
    apiFactory: () => ({}),
    onWait(state) {
      assert.equal(state.calls.length, 0);
      if (state.waits === 2) Object.assign(state.api, state.defaultApi);
    },
  });
  assert.equal(result.complete, true);
  assert.equal(waits, 2);
  assert.deepEqual(requiredModules, ['7']);
});

test('module wait stops with no API calls when the target never loads', async () => {
  const { result, calls, requiredModules, waits, clock } = await collect([], {}, {
    modules: { unrelated: () => 'unrelated', notAFunction: {} },
  });
  incomplete(result);
  assert.equal(result.error, 'module_not_found');
  assert.equal(result.module_wait_timeout, true);
  assert.deepEqual(calls, []);
  assert.deepEqual(requiredModules, []);
  assert.equal(waits, 79);
  assert.ok(clock < 20000);
});

test('separate route factories are not guessed or called as a combined API', async () => {
  function listOnly() { return '/aweme/v1/web/collects/list/'; }
  function videoOnly() { return '/aweme/v1/web/collects/video/list/'; }
  const { result, calls, requiredModules } = await collect([], {}, { modules: { one: listOnly, two: videoOnly } });
  incomplete(result);
  assert.equal(result.error, 'module_not_found');
  assert.deepEqual(calls, []);
  assert.deepEqual(requiredModules, []);
});

test('multiple matching factories fail before any factory or API is called', async () => {
  function first() { return ['/aweme/v1/web/collects/list/', '/aweme/v1/web/collects/video/list/']; }
  const { result, calls, requiredModules } = await collect([], {}, { modules: { a: first, b: first } });
  incomplete(result);
  assert.equal(result.error, 'ambiguous_collection_module');
  assert.deepEqual(calls, []);
  assert.deepEqual(requiredModules, []);
});

test('webpack module identity is preserved as a string', async () => {
  function target() { return ['/aweme/v1/web/collects/list/', '/aweme/v1/web/collects/video/list/']; }
  const { result, requiredModules } = await collect([page()], {}, { modules: { 'api-collections': target } });
  assert.equal(result.complete, true);
  assert.deepEqual(requiredModules, ['api-collections']);
});

test('missing or aliased exports time out with no collection requests', async () => {
  for (const apiFactory of [() => ({}), api => ({ So: api.So }), api => ({ So: api.So, d6: api.So })]) {
    const { result, calls, requiredModules } = await collect([], {}, { apiFactory });
    incomplete(result);
    assert.equal(result.error, 'bad_exports');
    assert.equal(result.module_wait_timeout, true);
    assert.deepEqual(calls, []);
    assert.deepEqual(requiredModules, ['7']);
  }
});

test('unready export getters are retried without making a request', async () => {
  let available = false;
  const { result, requiredModules } = await collect([page()], {}, {
    apiFactory(api) {
      return { get So() { if (!available) throw new Error('not initialized'); return api.So; },
        get d6() { if (!available) throw new Error('not initialized'); return api.d6; } };
    },
    onWait(state) { assert.equal(state.calls.length, 0); available = true; },
  });
  assert.equal(result.complete, true);
  assert.deepEqual(requiredModules, ['7']);
});

test('a verified factory initialization error is not automatically replayed', async () => {
  const { result, calls, requiredModules } = await collect([], {}, { requireError: true });
  incomplete(result);
  assert.equal(result.error, 'collection_module_unavailable');
  assert.deepEqual(requiredModules, ['7']);
  assert.deepEqual(calls, []);
});

test('explicit endpoint-bearing exports work independently of legacy alias names', async () => {
  const { result, calls } = await collect([page()], {}, {
    apiFactory(api) {
      return {
        async listing(args) { void '/aweme/v1/web/collects/list/'; return api.So(args); },
        async videos(args) { void '/aweme/v1/web/collects/video/list/'; return api.d6(args); },
      };
    },
  });
  assert.equal(result.complete, true);
  assert.equal(calls.length, 1);
});

test('an explicitly finished empty collection list is complete', async () => {
  const { result, calls } = await collect([page()]);
  assert.equal(result.ok, true);
  assert.equal(result.complete, true);
  assert.equal(result.collections.length, 0);
  assert.equal(calls.length, 1);
});

test('a known empty collection is complete without inventing videos', async () => {
  const { result, calls } = await collect([page([collection('101', 0)])], { 101: [page()] });
  assert.equal(result.ok, true);
  assert.equal(result.complete, true);
  assert.equal(result.itemsByCollection['101'].length, 0);
  assert.equal(calls.length, 2);
});

test('multiple collection and video pages finish before marking complete', async () => {
  const { result, calls } = await collect([
    page([collection('101', 2)], true, 1),
    page([collection('102', 1)], false, 2),
  ], {
    101: [page([video('1001')], true, 1), page([video('1002')], false, 2)],
    102: [page([video('2001')], false, 1)],
  });
  assert.equal(result.ok, true);
  assert.equal(result.complete, true);
  assert.equal(result.collections.length, 2);
  assert.deepEqual(Array.from(result.itemsByCollection['101'], item => item.awemeId), ['1001', '1002']);
  assert.deepEqual(Array.from(result.itemsByCollection['102'], item => item.awemeId), ['2001']);
  assert.equal(calls.length, 5);
});

test('numeric 1 and 0 are accepted as explicit pagination flags', async () => {
  const { result } = await collect([
    page([collection('101')], 1, 1), page([collection('102')], 0, 2),
  ], { 101: [page([video('1001')], 0, 1)], 102: [page([video('2001')], 0, 1)] });
  assert.equal(result.ok, true);
  assert.equal(result.complete, true);
});

test('a collection-page API failure after a successful page is incomplete', async () => {
  const { result, calls } = await collect([
    page([collection()], true, 1), { statusCode: 500, data: [], hasMore: false },
  ]);
  incomplete(result);
  assert.equal(calls.length, 2);
});

test('a video-page API failure after a successful page is incomplete', async () => {
  const { result, calls } = await collect([page([collection('101', 2)])], {
    101: [page([video('1001')], true, 1), { statusCode: 500, data: [], hasMore: false }],
  });
  incomplete(result);
  assert.equal(calls.length, 3);
});

test('failure in a later collection cannot promote earlier rows into a complete snapshot', async () => {
  const { result } = await collect([page([collection('101'), collection('102')])], {
    101: [page([video('1001')])], 102: [{ statusCode: 500, data: [], hasMore: false }],
  });
  incomplete(result);
});

const unknownFlags = [undefined, null, 'false', '0', '', 2, -1, {}, []];
for (const [index, flag] of unknownFlags.entries()) {
  test(`unknown collection hasMore ${index} is not a terminal-page signal`, async () => {
    const value = page([], flag);
    if (flag === undefined) delete value.hasMore;
    incomplete((await collect([value])).result);
  });
  test(`unknown video hasMore ${index} is not a terminal-page signal`, async () => {
    const value = page([], flag);
    if (flag === undefined) delete value.hasMore;
    incomplete((await collect([page([collection('101', 0)])], { 101: [value] })).result);
  });
}

for (const [index, data] of [undefined, null, {}, 'not-an-array'].entries()) {
  test(`malformed collection data ${index} is not an empty complete list`, async () => {
    const value = page(data);
    if (data === undefined) delete value.data;
    incomplete((await collect([value])).result);
  });
  test(`malformed video data ${index} is not an empty complete list`, async () => {
    const value = page(data);
    if (data === undefined) delete value.data;
    incomplete((await collect([page([collection('101', 0)])], { 101: [value] })).result);
  });
}

for (const statusCode of [undefined, null, '0', 1, -1, 500]) {
  test(`unknown or unsuccessful collection status ${String(statusCode)} fails closed`, async () => {
    incomplete((await collect([{ ...page(), statusCode }])).result);
  });
  test(`unknown or unsuccessful video status ${String(statusCode)} fails closed`, async () => {
    incomplete((await collect([page([collection('101', 0)])], {
      101: [{ ...page(), statusCode }],
    })).result);
  });
}

test('a collection cursor that does not advance fails before another request', async () => {
  const { result, calls } = await collect([page([collection()], true, 0)]);
  incomplete(result);
  assert.equal(calls.length, 1);
});

test('a video cursor that does not advance fails before another request', async () => {
  const { result, calls } = await collect([page([collection()])], {
    101: [page([video()], true, 0)],
  });
  incomplete(result);
  assert.equal(calls.length, 2);
});

test('a repeated collection cursor fails instead of looping', async () => {
  const { result, calls } = await collect([
    page([collection('101')], true, 1),
    page([collection('102')], true, 2),
    page([collection('103')], true, 1),
  ]);
  incomplete(result);
  assert.equal(calls.length, 3);
});

test('a repeated video cursor fails instead of looping', async () => {
  const { result, calls } = await collect([page([collection('101', 3)])], { 101: [
    page([video('1001')], true, 1), page([video('1002')], true, 2), page([video('1003')], true, 1),
  ] });
  incomplete(result);
  assert.equal(calls.length, 4);
});

for (const cursor of [undefined, null, '', 'not-a-number', -1, {}, []]) {
  test(`unknown/nonadvancing collection continuation cursor ${JSON.stringify(cursor)} fails`, async () => {
    incomplete((await collect([{ ...page([collection()], true), cursor }])).result);
  });
  test(`unknown/nonadvancing video continuation cursor ${JSON.stringify(cursor)} fails`, async () => {
    incomplete((await collect([page([collection()])], {
      101: [{ ...page([video()], true), cursor }],
    })).result);
  });
}

test('reaching the collection count limit is incomplete even on a claimed final page', async () => {
  const rows = Array.from({ length: 100 }, (_, index) => collection(String(1000 + index)));
  const { result, calls } = await collect([page(rows)]);
  incomplete(result);
  assert.equal(calls.length, 1);
});

test('reaching the per-collection video limit is incomplete even on a claimed final page', async () => {
  const rows = Array.from({ length: 500 }, (_, index) => video(String(10000 + index)));
  const { result } = await collect([page([collection('101', 500)])], { 101: [page(rows)] });
  incomplete(result);
});

test('collection pagination guard exhaustion never marks the snapshot complete', async () => {
  const pages = Array.from({ length: 30 }, (_, index) =>
    page([collection(String(1000 + index))], true, index + 1));
  const { result, calls } = await collect(pages);
  incomplete(result);
  assert.equal(calls.length, 30);
});

test('video pagination guard exhaustion never marks the snapshot complete', async () => {
  const pages = Array.from({ length: 120 }, (_, index) =>
    page([video(String(10000 + index))], true, index + 1));
  const { result, calls } = await collect([page([collection('101', 120)])], { 101: pages });
  incomplete(result);
  assert.equal(calls.length, 121);
});

test('a claimed terminal collection page at the page-count cap is still incomplete', async () => {
  const pages = Array.from({ length: 30 }, (_, index) =>
    page([collection(String(1000 + index))], index < 29, index + 1));
  const { result, calls } = await collect(pages);
  incomplete(result);
  assert.equal(calls.length, 30);
});

test('a claimed terminal video page at the page-count cap is still incomplete', async () => {
  const pages = Array.from({ length: 120 }, (_, index) =>
    page([video(String(10000 + index))], index < 119, index + 1));
  const { result, calls } = await collect([page([collection('101', 120)])], { 101: pages });
  incomplete(result);
  assert.equal(calls.length, 121);
});
