import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const asURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
async function moduleURL(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8');
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
  });
  for (const [name, url] of Object.entries(imports)) {
    outputText = outputText.replaceAll(`'${name}'`, `'${url}'`).replaceAll(`"${name}"`, `"${url}"`);
  }
  return asURL(outputText);
}
const sseURL = await moduleURL('../src/sse.ts');
const apiURL = await moduleURL('../src/api.ts', { './sse': sseURL });
const { inspectQuestion, chatAsk, chatAskStream } = await import(apiURL);

async function collect(events) {
  const result = [];
  for await (const event of events) result.push(event);
  return result;
}
async function withFetch(mock, run) {
  const previous = globalThis.fetch;
  globalThis.fetch = mock;
  try { await run(); } finally { globalThis.fetch = previous; }
}
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
const successStream = () => new Response('event: meta\ndata: {"session_id":1,"sources":[]}\n\nevent: done\ndata: {"ok":true}\n\n', {
  headers: { 'Content-Type': 'text/event-stream' },
});

test('blank and overlong questions never fetch through either chat API', async () => {
  await withFetch(() => assert.fail('invalid questions must not make a network request'), async () => {
    for (const text of [' \n\t\u3000', '\u0085\u001c', '字'.repeat(2001), '😀'.repeat(2001)]) {
      const expected = [...text].length > 2000 ? /问题请控制在2000字以内/ : /请输入问题/;
      await assert.rejects(chatAsk(text), expected);
      await assert.rejects(collect(chatAskStream(text)), expected);
    }
  });
});

test('both APIs accept exactly 2000 Unicode code points and send the trimmed question', async () => {
  let count = 0;
  const question = '😀'.repeat(1999) + '问';
  assert.equal(question.length, 3999); // UTF-16 units must not be the user limit.
  assert.equal(inspectQuestion(question).length, 2000);
  await withFetch(async (url, options) => {
    count++;
    assert.equal(JSON.parse(options.body).query, question);
    return url.endsWith('/stream') ? successStream() : json({ success: true, answer: '已回答' });
  }, async () => {
    assert.equal((await chatAsk(`\u3000\n${question}\t `)).success, true);
    assert.equal((await collect(chatAskStream(`\u0085${question}\u001f`))).length, 2);
  });
  assert.equal(count, 2);
});

test('trim preserves meaningful inner whitespace and matches backend treatment of BOM', () => {
  assert.deepEqual(inspectQuestion(' \u3000甲\n乙\t '), { query: '甲\n乙', length: 3, error: '' });
  assert.deepEqual(inspectQuestion('\uFEFF'), { query: '\uFEFF', length: 1, error: '' });
  assert.equal(inspectQuestion('👩‍💻').length, 3); // Three code points, as counted by Python len().
});

test('known Chinese server details are shown without technical status codes', async () => {
  const detail = '该历史对话已不存在，请新建对话。';
  await withFetch(async () => json({ detail }, 404), async () => {
    await assert.rejects(collect(chatAskStream('问题')), { message: detail });
  });
});

test('server query validation is translated without echoing its raw input', async () => {
  for (const [type, ctx, expected] of [
    ['string_too_long', { max_length: 2000 }, '问题请控制在2000字以内。'],
    ['string_too_short', { min_length: 1 }, '请输入问题。'],
  ]) {
    await withFetch(async () => json({ detail: [{ loc: ['body', 'query'], type, ctx, input: 'PRIVATE_INPUT' }] }, 422), async () => {
      await assert.rejects(collect(chatAskStream('有效问题')), { message: expected });
    });
  }
});

test('unknown validation details and plain upstream bodies use safe fallbacks', async () => {
  for (const response of [
    json({ detail: '后台错误，密钥 PRIVATE_SECRET，路径 C:/private/settings' }, 500),
    new Response('<html>PRIVATE_SECRET</html>', { status: 502 }),
    json({ detail: [{ loc: ['body', 'session_id'], type: 'greater_than', input: 'PRIVATE_INPUT' }] }, 422),
  ]) {
    await withFetch(async () => response, async () => {
      await assert.rejects(collect(chatAskStream('有效问题')), error => {
        assert.match(error.message, /请求失败|输入内容不正确/);
        assert.doesNotMatch(error.message, /PRIVATE_|422|C:\/private/);
        return true;
      });
    });
  }
});

test('an SSE provider error preserves delivered text but does not expose arbitrary details', async () => {
  await withFetch(async () => new Response('event: delta\ndata: {"text":"已有回答"}\n\nevent: error\ndata: {"message":"服务报错 PRIVATE_SECRET"}\n\n', {
    headers: { 'Content-Type': 'text/event-stream' },
  }), async () => {
    const events = chatAskStream('问题');
    assert.equal((await events.next()).value.text, '已有回答');
    await assert.rejects(events.next(), { message: '回答生成失败，请重试。' });
  });
});

// Exercise the real component event handlers with a tiny hook/JSX harness.
test('finite chat codes select local messages and never trust accompanying provider text', async () => {
  const expected = '语义检索尚未配置，请在本地填写 DASHSCOPE_API_KEY 后重启后台。';
  for (const code of ['CHAT_RETRIEVAL_NOT_CONFIGURED', 'UNRECOGNIZED_PRIVATE_CODE', '__proto__']) {
    const payload = { code, message: 'PRIVATE_PROVIDER_SECRET', success: false };
    await withFetch(async url => url.endsWith('/stream')
      ? new Response(`event: error\ndata: ${JSON.stringify(payload)}\n\n`, { headers: { 'Content-Type': 'text/event-stream' } })
      : json(payload), async () => {
      const wanted = code === 'CHAT_RETRIEVAL_NOT_CONFIGURED' ? expected : '回答生成失败，请重试。';
      assert.equal((await chatAsk('有效问题')).message, wanted);
      await assert.rejects(collect(chatAskStream('有效问题')), { message: wanted });
    });
  }
});

test('nonstreaming HTTP errors share the safe boundary instead of echoing response bodies', async () => {
  await withFetch(async () => json({ detail: 'PRIVATE_PROVIDER_SECRET' }, 500), async () => {
    await assert.rejects(chatAsk('有效问题'), { message: '请求失败，请稍后重试。' });
  });
});

// No browser, DOM package, model, or development server is involved.
const reactURL = asURL(`
function slot(initial) {
  const h = globalThis.__chatInputHarness;
  const i = h.cursor++;
  if (!(i in h.slots)) h.slots[i] = initial;
  return [h, i];
}
export function useState(initial) {
  const [h,i] = slot(initial);
  return [h.slots[i], value => { h.slots[i] = typeof value === 'function' ? value(h.slots[i]) : value; }];
}
export function useRef(initial) { const [h,i] = slot({current:initial}); return h.slots[i]; }
export function useId() { const [h,i] = slot(null); return 'feedback-' + i; }
export function useEffect(effect, deps) {
  const [h,i] = slot(undefined);
  const old = h.slots[i];
  if (!old || deps.some((v,j) => v !== old[j])) h.effects.push(effect);
  h.slots[i] = deps;
}
`);
const jsxURL = asURL('export const jsx=(type,props)=>({type,props}); export const jsxs=jsx;');
const traceURL = asURL('export default function ExecutionTrace() { return null; }');
const { default: ChatPanel } = await import(await moduleURL('../src/components/ChatPanel.tsx', {
  react: reactURL, 'react/jsx-runtime': jsxURL, '../api': apiURL, './ExecutionTrace': traceURL,
}));

function findAll(node, predicate) {
  if (node == null || typeof node !== 'object') return [];
  if (Array.isArray(node)) return node.flatMap(child => findAll(child, predicate));
  return [...(predicate(node) ? [node] : []), ...findAll(node.props?.children, predicate)];
}
function componentHarness() {
  const h = { cursor: 0, slots: [], effects: [] };
  const busy = [];
  const props = { collectionId: 'all', statsRefreshKey: 0, knowledgeBaseId: 5, knowledgeBaseName: '测试',
    openedSessionId: null, sessionTitle: '', onSessionSaved: () => assert.fail('invalid input saved a session'),
    onHistoryChanged: () => assert.fail('invalid input changed history'), onBusyChange: value => busy.push(value), onNewChat() {} };
  const render = () => {
    globalThis.__chatInputHarness = h;
    h.cursor = 0;
    const tree = ChatPanel(props);
    h.effects.splice(0).forEach(effect => effect());
    return tree;
  };
  return { render, busy };
}

test('ChatPanel rejects overlong pasted emoji before clearing input or adding messages', async () => {
  await withFetch(() => assert.fail('invalid UI input must not fetch'), async () => {
    const { render, busy } = componentHarness();
    let tree = render();
    const pasted = `  ${'😀'.repeat(2001)}\n`;
    findAll(tree, node => node.type === 'input')[0].props.onChange({ target: { value: pasted } });
    tree = render();
    await findAll(tree, node => node.type === 'form')[0].props.onSubmit({ preventDefault() {} });
    tree = render();
    assert.equal(findAll(tree, node => node.type === 'input')[0].props.value, pasted);
    assert.equal(findAll(tree, node => node.props?.className?.includes('chat-bubble')).length, 0);
    assert.equal(findAll(tree, node => node.props?.role === 'alert')[0].props.children, '问题请控制在2000字以内。');
    assert.ok(findAll(tree, node => Array.isArray(node.props?.children) && node.props.children.join('') === '2001/2000').length);
    assert.deepEqual(busy, []);
  });
});

test('ChatPanel preserves whitespace-only input on submit and clears the inline warning after editing', async () => {
  await withFetch(() => assert.fail('blank UI input must not fetch'), async () => {
    const { render, busy } = componentHarness();
    let tree = render();
    const input = () => findAll(tree, node => node.type === 'input')[0];
    input().props.onChange({ target: { value: ' \n\t\u3000' } });
    tree = render();
    await findAll(tree, node => node.type === 'form')[0].props.onSubmit({ preventDefault() {} });
    tree = render();
    assert.equal(input().props.value, ' \n\t\u3000');
    assert.equal(findAll(tree, node => node.props?.role === 'alert')[0].props.children, '请输入问题。');
    assert.equal(findAll(tree, node => node.props?.className?.includes('chat-bubble')).length, 0);
    input().props.onChange({ target: { value: '有效问题' } });
    tree = render();
    assert.equal(findAll(tree, node => node.props?.role === 'alert').length, 0);
    assert.equal(input().props.value, '有效问题');
    assert.deepEqual(busy, []);
  });
});
