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
const { listSessions, getSessionMessages, renameSession, deleteSession } = await import(
  await moduleURL('../src/api.ts', { './sse': sseURL }),
);

async function withFetch(mock, run) {
  const previous = globalThis.fetch;
  globalThis.fetch = mock;
  try { await run(); } finally { globalThis.fetch = previous; }
}
const json = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });

test('history list requests the supported limit', async () => {
  await withFetch(async url => {
    assert.equal(url, '/api/chat/sessions?limit=100');
    return json({ success: true, items: [] });
  }, async () => assert.deepEqual(await listSessions(), { success: true, items: [] }));
});

test('opening history preserves its id, source metadata and AbortSignal', async () => {
  const signal = new AbortController().signal;
  const history = {
    success: true,
    title: '功能验证样例',
    items: [{ id: 1, session_id: 101, role: 'assistant', content: '保存的回答', sources: [
      { platform_item_id: 'sample', title: '样例来源', url: '#', score: null },
    ] }],
  };
  await withFetch(async (url, options) => {
    assert.equal(url, '/api/chat/sessions/101/messages');
    assert.equal(options.signal, signal);
    assert.equal(options.method ?? 'GET', 'GET');
    return json(history);
  }, async () => assert.deepEqual(await getSessionMessages(101, signal), history));
});

test('a cancelled history request propagates cancellation to the caller', async () => {
  const controller = new AbortController();
  await withFetch((_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')), { once: true });
  }), async () => {
    const loading = getSessionMessages(101, controller.signal);
    controller.abort();
    await assert.rejects(loading, { name: 'AbortError' });
  });
});

test('renaming history uses PATCH with a trimmed title', async () => {
  await withFetch(async (url, options) => {
    assert.equal(url, '/api/chat/sessions/102');
    assert.equal(options.method, 'PATCH');
    assert.deepEqual(JSON.parse(options.body), { title: '我的学习计划' });
    return json({ success: true, session_id: 102, title: '我的学习计划' });
  }, async () => {
    const result = await renameSession(102, '  我的学习计划\n');
    assert.equal(result.title, '我的学习计划');
  });
});

test('blank and oversized history titles are rejected before sending a request', async () => {
  await withFetch(() => assert.fail('invalid titles must not reach the backend'), async () => {
    await assert.rejects(renameSession(101, ' \n\t '), /1～128/);
    await assert.rejects(renameSession(101, '字'.repeat(129)), /1～128/);
  });
});

test('deleting history uses DELETE on the selected session only', async () => {
  await withFetch(async (url, options) => {
    assert.equal(url, '/api/chat/sessions/101');
    assert.equal(options.method, 'DELETE');
    assert.equal(options.body, undefined);
    return json({ success: true });
  }, async () => assert.deepEqual(await deleteSession(101), { success: true }));
});

// Existing chat-stream.test.mjs verifies that continuing a conversation sends its
// saved session id and AbortSignal while preserving incremental answers.
