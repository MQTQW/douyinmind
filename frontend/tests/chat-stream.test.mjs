import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

// Use the project's compiler so these tests also work on Node 18 without a TS loader.
async function moduleURL(path, imports = {}) {
  let source = await readFile(new URL(path, import.meta.url), 'utf8');
  for (const [name, url] of Object.entries(imports)) source = source.replace(`'${name}'`, `'${url}'`);
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext },
  });
  return `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`;
}

const sseURL = await moduleURL('../src/sse.ts');
const { readSSE } = await import(sseURL);
const { chatAskStream } = await import(await moduleURL('../src/api.ts', { './sse': sseURL }));
const encoder = new TextEncoder();
const wire = (event, data, newline = '\n') => `event: ${event}${newline}data: ${JSON.stringify(data)}${newline}${newline}`;

function stream(bytes, width = bytes.length) {
  return new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += width) controller.enqueue(bytes.slice(i, i + width));
      controller.close();
    },
  });
}

async function collect(events) {
  const result = [];
  for await (const event of events) result.push(event);
  return result;
}

async function withResponse(body, run, options = {}) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    options.checkRequest?.(...args);
    return new Response(body, { headers: { 'Content-Type': 'text/event-stream' }, ...options.response });
  };
  try { await run(); } finally { globalThis.fetch = originalFetch; }
}

test('Chinese, emoji and all events survive every byte boundary', async () => {
  const expected = [
    { event: 'sources', data: { sources: [{ title: '学习📚' }] } },
    { event: 'delta', data: { text: '你好，世界🧠\n下一行' } },
    { event: 'meta', data: { session_id: 7 } },
    { event: 'done', data: { ok: true } },
  ];
  for (const newline of ['\n', '\r\n', '\r']) {
    const bytes = encoder.encode(expected.map(e => wire(e.event, e.data, newline)).join(''));
    assert.deepEqual(await collect(readSSE(stream(bytes, 1))), expected);
    assert.deepEqual(await collect(readSSE(stream(bytes))), expected);
  }
});

test('comments, optional spaces and multi-line data are parsed', async () => {
  const text = ': heartbeat\n\nevent:delta\ndata: {\ndata: "text": "完整消息"\ndata: }\n\n';
  assert.deepEqual(await collect(readSSE(stream(encoder.encode(text), 3))), [
    { event: 'delta', data: { text: '完整消息' } },
  ]);
});

test('invalid JSON reports an error rather than silently dropping a message', async () => {
  await assert.rejects(collect(readSSE(stream(encoder.encode('event: delta\ndata: broken\n\n')))), SyntaxError);
});

test('an unterminated event is not treated as a complete event', async () => {
  assert.deepEqual(await collect(readSSE(stream(encoder.encode('event: delta\ndata: {"text":"半条消息"}\n')))), []);
});

test('leaving a stream cancels its reader', async () => {
  let cancelled = false;
  const body = new ReadableStream({
    start(controller) { controller.enqueue(encoder.encode(wire('delta', { text: '开头' }))); },
    cancel() { cancelled = true; },
  });
  const events = readSSE(body);
  assert.equal((await events.next()).value.data.text, '开头');
  await events.return();
  assert.equal(cancelled, true);
  assert.equal(body.locked, false);
});

test('request preserves collection and session, and yields before the response finishes', async () => {
  let controller;
  const body = new ReadableStream({ start(value) { controller = value; } });
  const signal = new AbortController().signal;
  await withResponse(body, async () => {
    const events = chatAskStream('总结', 7, 'collection-123', signal);
    const first = events.next();
    controller.enqueue(encoder.encode(wire('delta', { text: '先显示这一句' })));
    assert.deepEqual((await first).value, { _event: 'delta', text: '先显示这一句' });
    // The stream is still open: receiving this proves the client does not wait for EOF.
    controller.enqueue(encoder.encode(wire('meta', { session_id: 7, answer: '最终回答', sources: [] }) + wire('done', { ok: true })));
    controller.close();
    assert.deepEqual((await collect(events)).map(e => e._event), ['meta', 'done']);
  }, {
    checkRequest(url, options) {
      assert.equal(url, '/api/chat/ask/stream');
      assert.equal(options.signal, signal);
      assert.deepEqual(JSON.parse(options.body), { query: '总结', session_id: 7, collection_id: 'collection-123' });
    },
  });
});

test('premature disconnect and missing metadata are reported', async () => {
  for (const text of [wire('delta', { text: '部分回答' }), wire('done', { ok: true })]) {
    await withResponse(stream(encoder.encode(text)), async () => {
      await assert.rejects(collect(chatAskStream('问题')), /连接中断/);
    });
  }
});

test('server errors preserve the earlier delivered text and surface the failure', async () => {
  await withResponse(stream(encoder.encode(wire('delta', { text: '部分回答' }) + wire('error', { message: '服务暂时不可用' }))), async () => {
    const events = chatAskStream('问题');
    assert.equal((await events.next()).value.text, '部分回答');
    await assert.rejects(events.next(), /服务暂时不可用/);
  });
});

test('both metadata-before-done and legacy done-before-metadata are accepted', async () => {
  const meta = wire('meta', { session_id: 3, sources: [] });
  const done = wire('done', { ok: true });
  for (const text of [meta + done, done + meta]) {
    await withResponse(stream(encoder.encode(text), 2), async () => {
      assert.equal((await collect(chatAskStream('问题'))).length, 2);
    });
  }
});

test('HTTP failures and non-stream responses are reported', async () => {
  await withResponse(null, async () => {
    await assert.rejects(collect(chatAskStream('问题')), /请求失败，请稍后重试/);
  }, { response: { status: 503 } });
  await withResponse('not a stream', async () => {
    await assert.rejects(collect(chatAskStream('问题')), /回答格式异常/);
  }, { response: { headers: { 'Content-Type': 'text/html' } } });
});
