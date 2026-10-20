import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

async function moduleURL(path, imports = {}) {
  let source = await readFile(new URL(path, import.meta.url), 'utf8');
  for (const [name, url] of Object.entries(imports)) source = source.replace(`'${name}'`, `'${url}'`);
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext },
  });
  return `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`;
}
const sseURL = await moduleURL('../src/sse.ts');
const { syncKnowledge, listAllCollectionVideos, createKnowledgeBase, knowledgeBaseFromCollection, removeKnowledgeBaseVideo, chatAskStream } = await import(await moduleURL('../src/api.ts', { './sse': sseURL }));

async function withFetch(mock, run) {
  const previous = globalThis.fetch;
  globalThis.fetch = mock;
  try { await run(); } finally { globalThis.fetch = previous; }
}
const json = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });

test('empty selection is rejected before an ingestion request can be sent', async () => {
  await withFetch(() => { assert.fail('empty selection must not reach the backend'); }, async () => {
    await assert.rejects(syncKnowledge([]), /至少选择一个/);
  });
});

test('selected ingestion sends only the chosen video IDs, while all uses an omitted list', async () => {
  const requests = [];
  await withFetch(async (url, options) => {
    assert.equal(url, '/api/knowledge/sync');
    requests.push(JSON.parse(options.body));
    return json({ success: true, task_id: 'test' });
  }, async () => {
    await syncKnowledge(['video-a', 'video-c']);
    await syncKnowledge();
  });
  assert.deepEqual(requests, [{ selected_ids: ['video-a', 'video-c'] }, {}]);
});

test('video selection includes videos beyond the first page and deduplicates IDs', async () => {
  const first = Array.from({ length: 100 }, (_, id) => ({ platform_item_id: String(id), status: 'pending' }));
  const urls = [];
  await withFetch(async url => {
    urls.push(url);
    return json({ success: true, total: 102, items: urls.length === 1 ? first : [first[0], { platform_item_id: 'last' }] });
  }, async () => {
    const items = await listAllCollectionVideos('test');
    assert.equal(items.length, 101);
    assert.equal(items.at(-1).platform_item_id, 'last');
  });
  assert.deepEqual(urls, [
    '/api/favorites/collections/test/videos?page=1&size=100',
    '/api/favorites/collections/test/videos?page=2&size=100',
  ]);
});

test('an incomplete list reports an error instead of presenting a partial all-selection', async () => {
  let calls = 0;
  await withFetch(async () => json({ success: true, total: 2, items: calls++ === 0 ? [{ platform_item_id: 'first' }] : [] }), async () => {
    await assert.rejects(listAllCollectionVideos('test'), /未加载完整/);
  });
});

test('imports send the explicit target library and do not discard completed videos', async () => {
  await withFetch(async (url, options) => {
    assert.equal(url, '/api/knowledge/sync');
    assert.deepEqual(JSON.parse(options.body), { selected_ids: ['already-done', 'new-video'], knowledge_base_id: 4 });
    return json({ success: true, assigned_count: 2 });
  }, () => syncKnowledge(['already-done', 'new-video'], 4));
});

test('creation trims the library name and rejects blank names without requests', async () => {
  let calls = 0;
  await withFetch(async (url, options) => {
    calls++;
    assert.equal(url, '/api/knowledge-bases');
    assert.deepEqual(JSON.parse(options.body), { name: '编程' });
    return json({ success: true, item: { id: 4, name: '编程' } });
  }, async () => {
    await createKnowledgeBase(' 编程 ');
    await assert.rejects(createKnowledgeBase('  '), /名称/);
  });
  assert.equal(calls, 1);
});

test('automatic naming sends the collection id and uses the server-confirmed existing library', async () => {
  await withFetch(async (url, options) => {
    assert.equal(url, '/api/knowledge-bases/from-collection');
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), { collection_id: 'folder-1' });
    return json({ success: true, reused: true, item: { id: 4, name: '收藏夹真实名称' } });
  }, async () => {
    const result = await knowledgeBaseFromCollection('folder-1');
    assert.equal(result.item.id, 4);
    assert.equal(result.reused, true);
  });
});

test('removing from a library uses only its membership endpoint', async () => {
  await withFetch(async (url, options) => {
    assert.equal(url, '/api/knowledge-bases/4/videos/video-a');
    assert.equal(options.method, 'DELETE');
    return json({ success: true, removed_count: 1 });
  }, () => removeKnowledgeBaseVideo(4, 'video-a'));
});

test('streamed questions include the selected knowledge base and abort signal', async () => {
  const signal = new AbortController().signal;
  await withFetch(async (url, options) => {
    assert.equal(options.signal, signal);
    assert.deepEqual(JSON.parse(options.body), { query: '有哪些视频', session_id: null, collection_id: 'all', knowledge_base_id: 4 });
    return new Response('event: meta\ndata: {"session_id":7,"sources":[]}\n\nevent: done\ndata: {"ok":true}\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
  }, async () => {
    for await (const event of chatAskStream('有哪些视频', null, 'all', signal, 4)) assert.ok(event);
  });
});
