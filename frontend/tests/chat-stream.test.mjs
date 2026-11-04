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
const { chatAsk, chatAskStream } = await import(await moduleURL('../src/api.ts', { './sse': sseURL }));
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
    if (args[0] === '/api/chat/capabilities') return new Response(JSON.stringify({ success: true,
      chat_only_sessions: true, session_mode_persistent: true, modes: ['knowledge', 'chat_only'] }), { headers: { 'Content-Type': 'application/json' } });
    options.checkRequest?.(...args);
    return new Response(body, { headers: { 'Content-Type': 'text/event-stream' }, ...options.response });
  };
  try { await run(); } finally { globalThis.fetch = originalFetch; }
}

test('explicit pure AI scope is sent for streams without local library identifiers', async () => {
  const bytes = encoder.encode(wire('meta', { session_id: 82, chat_only: true }) + wire('done', { ok: true }));
  await withResponse(stream(bytes), async () => {
    await collect(chatAskStream('纯AI合成问题', null, null, undefined, null, true));
  }, { checkRequest(_url, options) {
    assert.deepEqual(JSON.parse(options.body), { query: '纯AI合成问题', session_id: null, collection_id: null, chat_only: true });
  }});
});

test('explicit local mode and legacy omitted mode remain distinct requests', async () => {
  for (const mode of [false, undefined]) {
    const bytes = encoder.encode(wire('meta', { session_id: 81 }) + wire('done', { ok: true }));
    await withResponse(stream(bytes), async () => {
      await collect(chatAskStream('本地资料问题', 81, 'all', undefined, 7, mode));
    }, { checkRequest(_url, options) {
      const body = JSON.parse(options.body);
      assert.equal(body.knowledge_base_id, 7);assert.equal(body.session_id, 81);
      assert.equal(Object.hasOwn(body, 'chat_only'), mode !== undefined);
      if (mode !== undefined) assert.equal(body.chat_only, false);
    }});
  }
});

test('nonstreaming AI requests carry the same explicit mode', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    if (url === '/api/chat/capabilities') return new Response(JSON.stringify({ success: true,
      chat_only_sessions: true, session_mode_persistent: true, modes: ['knowledge', 'chat_only'] }), { headers: { 'Content-Type': 'application/json' } });
    assert.equal(JSON.parse(options.body).chat_only, true);
    return new Response(JSON.stringify({ success: true, answer: '合成回答' }), { headers: { 'Content-Type': 'application/json' } });
  };
  try { assert.equal((await chatAsk('纯AI问题', null, null, null, true)).success, true); }
  finally { globalThis.fetch = originalFetch; }
});

test('old or unverified backends never receive a pure AI question', async () => {
  const originalFetch = globalThis.fetch;
  try {
    for (const backend of ['missing', 'incomplete', 'network']) {
      const calls = [];
      globalThis.fetch = async url => {
        calls.push(url);
        assert.equal(url, '/api/chat/capabilities', 'an unsupported backend must never receive the question');
        if (backend === 'network') throw new TypeError('synthetic network failure');
        if (backend === 'missing') return new Response('{}', { status: 404 });
        return new Response(JSON.stringify({ success: true, chat_only_sessions: true, modes: ['chat_only'] }));
      };
      await assert.rejects(collect(chatAskStream('不能泄漏到旧资料模式的问题', null, null, undefined, null, true)), /没有发送问题或读取本地资料/);
      assert.deepEqual(calls, ['/api/chat/capabilities']);
    }
  } finally { globalThis.fetch = originalFetch; }
});

test('capability failure in AI mode does not disable a local library request', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push(url);
    if (url === '/api/chat/capabilities') return new Response('{}', { status: 404 });
    assert.equal(JSON.parse(options.body).chat_only, false);
    return new Response(JSON.stringify({ success: true, answer: '本地合成回答' }), { headers: { 'Content-Type': 'application/json' } });
  };
  try {
    await assert.rejects(chatAsk('纯AI问题', null, null, null, true), /更新并重新启动后台/);
    assert.equal((await chatAsk('本地问题', null, 'all', 7, false)).success, true);
    assert.deepEqual(calls, ['/api/chat/capabilities', '/api/chat/ask']);
  } finally { globalThis.fetch = originalFetch; }
});

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

test('complete meta and done frames release a stream even when HTTP does not close', async () => {
  for (const events of [
    [['meta', { session_id: 501 }], ['done', { ok: true }]],
    [['done', { ok: true }], ['meta', { session_id: 501 }]],
  ]) {
    let controller;
    let cancelled = 0;
    let timer;
    const body = new ReadableStream({
      start(value) { controller = value; for (const [event, data] of events) value.enqueue(encoder.encode(wire(event, data))); },
      cancel() { cancelled++; },
    });
    try {
      await withResponse(body, async () => {
        const result = await Promise.race([
          collect(chatAskStream('synthetic EOF test')),
          new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('completed protocol still waits for HTTP EOF')), 500); }),
        ]);
        assert.equal(result.length, 2);
      });
      assert.equal(cancelled, 1, 'the completed response reader is explicitly released');
    } finally {
      clearTimeout(timer);
      try { controller.close(); } catch { /* already cancelled */ }
    }
  }
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
