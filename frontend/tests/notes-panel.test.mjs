import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const asURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const apiSource = (await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')).replace("'./sse'", `'${asURL('export function readSSE(){}')}'`);
const apiURL = asURL(ts.transpileModule(apiSource, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext } }).outputText);
const { AiNotesError } = await import(apiURL);
function labelState(context) {
  const visit = node => {
    if (ts.isVariableDeclaration(node) && ts.isArrayBindingPattern(node.name) && node.initializer && ts.isCallExpression(node.initializer)
        && node.initializer.expression.getText() === 'useState') {
      return ts.factory.updateVariableDeclaration(node, node.name, node.exclamationToken, node.type,
        ts.factory.updateCallExpression(node.initializer, node.initializer.expression, node.initializer.typeArguments,
          [...node.initializer.arguments, ts.factory.createStringLiteral(node.name.elements[0].name.text)]));
    }
    return ts.visitEachChild(node, visit, context);
  };
  return source => ts.visitNode(source, visit);
}
const reactURL = asURL(`
const current=()=>globalThis.__notesHarness;
const changed=(a,b)=>!a||!b||b.some((v,i)=>!Object.is(v,a[i]));
export function useState(value,label){const h=current();h.cursor++;if(!(label in h.state))h.state[label]=typeof value==='function'?value():value;return[h.state[label],next=>{h.state[label]=typeof next==='function'?next(h.state[label]):next}];}
export function useRef(value){const h=current(),i=h.cursor++;return h.slots[i]??={current:value};}
export function useCallback(callback,deps){const h=current(),i=h.cursor++,old=h.slots[i];if(!old||changed(old.deps,deps))h.slots[i]={deps,callback};return h.slots[i].callback;}
export function useEffect(effect,deps){const h=current(),i=h.cursor++,old=h.slots[i];if(changed(old?.deps,deps)){h.slots[i]={deps,effect,cleanup:old?.cleanup};h.effects.push(()=>{h.slots[i].cleanup?.();h.slots[i].cleanup=effect()})}}
`);
const fakeAPI = asURL(`
export {AiNotesError,aiNotesSupported,aiNoteDownloadURL,aiNotesFailureMessage} from '${apiURL}';
const h=()=>globalThis.__notesHarness;
export const getAiNotesCapabilities=(...args)=>h().api.capability(...args);
export const listAiNotes=(...args)=>h().api.list(...args);
export const getAiNoteRequest=(...args)=>h().api.lookup(...args);
export const createAiNote=(...args)=>h().api.create(...args);
`);
const jsxURL = asURL('export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;export const Fragment="fragment";');
const source = await readFile(new URL('../src/components/NotesPanel.tsx', import.meta.url), 'utf8');
let { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX }, transformers: { before: [labelState] } });
for (const [name, value] of Object.entries({ react: reactURL, 'react/jsx-runtime': jsxURL, '../api': fakeAPI,
  'react-dom': asURL('export const createPortal=child=>child;'),
  'lucide-react': asURL('export const ArrowLeft=()=>null,Download=()=>null,FileText=()=>null,RefreshCw=()=>null,X=()=>null;') })) {
  outputText = outputText.replaceAll(`'${name}'`, `'${value}'`).replaceAll(`"${name}"`, `"${value}"`);
}
const { default: NotesPanel } = await import(asURL(outputText));
const KEY = 'douyinmind.notes.requests.v1', VIDEO = 'synthetic-video';
const OLD = '11111111-1111-4111-8111-111111111111', NOTE = '22222222-2222-4222-8222-222222222222';
const FP = 'a'.repeat(64), PRIVATE = 'PRIVATE_PROVIDER_BODY_NOT_FOR_UI';
const CAP = { success: true, version: 1, ready: true, max_source_chars: 120000, chunk_chars: 12000, max_expected_calls: 11, usage_is_invoice: false };
const SOURCE = { status: 'ready', fingerprint: FP, char_count: 1000, chunk_count: 1, expected_call_count: 1 };
const item = (changes = {}) => ({ request_id: OLD, platform_item_id: VIDEO, state: 'done', task_id: null, note_id: NOTE,
  progress: 1, total: 1, expected_call_count: 1, reused_step_count: 0, source_current: true, can_download: true,
  download_url: `/api/notes/artifacts/${NOTE}.md`, can_redo: false, reason_code: null, model_label: 'synthetic-model',
  usage: { state: 'unknown', known_tokens: {}, request_attempts: null, complete: false, is_invoice: false },
  created_at: '2026-11-04T14:00:00Z', updated_at: '2026-11-04T14:00:00Z', ...changes });
const pending = requestId => JSON.stringify({ version: 1, items: [{ videoId: VIDEO, requestId }] });
const settle = async () => { for (let i = 0; i < 35; i++) await Promise.resolve(); };
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
function nodes(node, matches) {
  if (node === null || typeof node !== 'object') return [];
  if (Array.isArray(node)) return node.flatMap(value => nodes(value, matches));
  return [...(matches(node) ? [node] : []), ...nodes(node.props?.children, matches)];
}
function text(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'number' || typeof node === 'string') return String(node);
  return Array.isArray(node) ? node.map(text).join('') : text(node.props?.children);
}
const button = (tree, label) => nodes(tree, node => node.type === 'button' && text(node) === label)[0];
const links = tree => nodes(tree, node => node.type === 'a');
function harness(options = {}) {
  const storage = options.storage || new Map();
  const h = { cursor: 0, slots: [], state: {}, effects: [], storage, rows: options.rows || [], total: options.total,
    posts: [], lookups: [], lists: [], caps: [], writes: [], uuids: 0, timers: new Map(), listeners: new Map(), closed: 0,
    readBroken: false, writeBroken: false };
  h.api = {
    capability: async () => { h.caps.push(true); return CAP; },
    list: async (video, limit, offset) => { h.lists.push([video, limit, offset]); return { success: true, source: SOURCE,
      items: h.rows.slice(offset, offset + limit), current_note: null, total: h.total ?? h.rows.length, limit, offset }; },
    lookup: async requestId => { h.lookups.push(requestId); const note = h.rows.find(row => row.request_id === requestId);
      return note ? { success: true, found: true, request_id: requestId, item: note } : { success: true, found: false, request_id: requestId }; },
    create: async (video, body) => { h.posts.push([video, body]); const note = item({ request_id: body.request_id,
      state: 'queued', progress: 0, can_download: false, download_url: null, note_id: null }); h.rows = [note, ...h.rows]; return { success: true, item: note }; },
  };
  const activate = () => {
    globalThis.__notesHarness = h;
    globalThis.window = { localStorage: {
      getItem(key) { if (h.readBroken) throw new Error(PRIVATE); return storage.get(key) ?? null; },
      setItem(key, value) { if (h.writeBroken) throw new Error(PRIVATE); h.writes.push([key, value]); storage.set(key, value); },
    }, crypto: { randomUUID() { h.uuids++; return `00000000-0000-4000-8000-${String(h.uuids).padStart(12, '0')}`; } },
    addEventListener(name, callback) { h.listeners.set(name, callback); }, removeEventListener(name) { h.listeners.delete(name); },
    setInterval(callback) { const id = h.timers.size + 1; h.timers.set(id, callback); return id; }, clearInterval(id) { h.timers.delete(id); } };
    globalThis.document = { body: {}, visibilityState: 'visible', activeElement: null, getElementById: () => null };
  };
  const props = { video: { platform_item_id: options.videoId || VIDEO, title: '整理笔记的方法（合成验证）' }, onClose: () => h.closed++ };
  h.render = () => { activate(); h.cursor = 0; return NotesPanel(props); };
  h.flush = async () => { activate(); h.effects.splice(0).forEach(effect => effect()); await settle(); };
  h.mount = async () => { for (let i = 0; i < 3; i++) { h.render(); await h.flush(); } return h.render(); };
  h.strictReplayEffects = async () => {
    activate(); const effects=h.slots.filter(slot=>slot?.effect);
    effects.forEach(slot=>slot.cleanup?.());effects.forEach(slot=>{slot.cleanup=slot.effect()});await settle();
  };
  h.open = async label => { button(h.render(), label || '生成并保存笔记').props.onClick(); h.render(); await h.flush(); return h.render(); };
  h.submit = async () => { button(h.render(), '确认并生成').props.onClick(); await settle(); return h.render(); };
  h.dispose = () => { activate(); h.slots.forEach(slot => slot?.cleanup?.()); };
  return h;
}

test('old or unreadable backend has explicit update guidance and no legacy paid fallback', async () => {
  const h = harness(); h.api.capability = async () => { throw new Error(PRIVATE); }; await h.mount();
  assert.match(text(h.render()), /更新后台/); assert.doesNotMatch(text(h.render()), new RegExp(PRIVATE));
  assert.equal(h.lists.length, 0); assert.equal(h.posts.length, 0);
  assert.ok(links(h.render()).every(link => !link.props.href.includes('mode=ai'))); h.dispose();
});
test('StrictMode effect replay starts a fresh read and an obsolete finally cannot release the newer read', async () => {
  const h=harness(),first=deferred(),second=deferred(),listing=deferred();let caps=0,lists=0;
  h.api.capability=()=> (++caps===1?first:second).promise;
  h.api.list=()=>{lists++;return listing.promise};
  h.render();await h.flush();assert.equal(caps,1);
  await h.strictReplayEffects();assert.equal(caps,2);
  second.resolve(CAP);await settle();h.render();assert.equal(lists,1);
  first.resolve({...CAP,version:0});await settle();
  // A queued stale refresh handler must not start a third read while the
  // replacement epoch still owns its list request.
  button(h.render(),'正在读取…').props.onClick();await settle();assert.equal(caps,2);
  listing.resolve({success:true,source:SOURCE,items:[],current_note:null,total:0});await settle();
  assert.equal(button(h.render(),'生成并保存笔记').props.disabled,false);
  assert.equal(h.state.capability.version,1);assert.equal(h.state.loading,false);
  assert.equal(h.posts.length,0);assert.equal(h.uuids,0);h.dispose();
});
test('a late read for an unmounted video cannot enter a newly mounted video view', async () => {
  const old=harness(),late=deferred();old.api.list=()=>late.promise;
  old.render();await old.flush();old.dispose();
  const fresh=harness({videoId:'another-synthetic-video'});await fresh.mount();
  late.resolve({success:true,source:{...SOURCE,char_count:99999},items:[item()],current_note:null,total:1});await settle();
  assert.equal(old.state.source,null);assert.equal(fresh.state.source.char_count,SOURCE.char_count);
  assert.deepEqual(fresh.state.items,[]);assert.equal(fresh.posts.length,0);fresh.dispose();
});

test('scope and fees are confirmed before UUID creation or POST; cancelling or a stale cancelled handler cannot submit', async () => {
  const h = harness(); await h.mount(); await h.open();
  const tree = h.render(), oldSubmit = button(tree, '确认并生成').props.onClick;
  assert.match(text(tree), /仅使用这个视频当前已保存.*可能产生模型费用/);
  assert.match(text(tree), /不会重新识别视频.*不会改变知识库检索范围/);
  assert.equal(h.uuids, 0); assert.equal(h.posts.length, 0);
  button(tree, '暂不生成').props.onClick(); oldSubmit(); await settle();
  assert.equal(h.uuids, 0); assert.equal(h.posts.length, 0); h.dispose();
});

test('request ID is persisted before the only POST and storage contains IDs only', async () => {
  const h = harness(); await h.mount(); await h.open();
  const original = h.api.create;
  h.api.create = async (video, body) => {
    assert.equal(JSON.parse(h.storage.get(KEY)).items[0].requestId, body.request_id);
    return original(video, body);
  };
  await h.submit();
  assert.equal(h.posts.length, 1); assert.equal(h.uuids, 1);
  assert.deepEqual(h.posts[0][1], { request_id: h.state.intent, confirm: true, expected_source_fingerprint: FP });
  assert.deepEqual(Object.keys(JSON.parse(h.storage.get(KEY)).items[0]).sort(), ['requestId', 'videoId']);
  assert.doesNotMatch(h.storage.get(KEY), /fingerprint|原文|费用|synthetic-model/); h.dispose();
});

test('unknown POST result survives remount and permits only GET lookup with the same UUID', async () => {
  const storage = new Map(), h = harness({ storage }); await h.mount(); await h.open();
  h.api.create = async (video, body) => { h.posts.push([video, body]); throw new Error(PRIVATE); };
  await h.submit(); const requestId = h.posts[0][1].request_id;
  assert.equal(h.posts.length, 1); assert.doesNotMatch(text(h.render()), new RegExp(PRIVATE));
  assert.equal(button(h.render(), '生成并保存笔记'), undefined); h.dispose();
  const reopened = harness({ storage }); await reopened.mount();
  button(reopened.render(), '核查生成结果').props.onClick(); await settle();
  assert.ok(reopened.lookups.length > 0 && reopened.lookups.every(id => id === requestId));
  assert.equal(reopened.posts.length, 0); assert.equal(reopened.uuids, 0);
  assert.equal(JSON.parse(storage.get(KEY)).items[0].requestId, requestId); reopened.dispose();
});

test('needs_review never auto-replays; only explicit redo confirmation creates a new UUID and recovery link', async () => {
  const storage = new Map([[KEY, pending(OLD)]]), old = item({ state: 'needs_review', can_download: false, can_redo: true });
  const h = harness({ storage, rows: [old] }); await h.mount();
  assert.equal(h.posts.length, 0); assert.match(text(h.render()), /没有自动重试/);
  await h.open('确认后重新生成');
  assert.equal(h.uuids, 0); assert.match(text(h.render()), /旧记录和未知费用保留/);
  await h.submit();
  assert.equal(h.posts.length, 1); assert.notEqual(h.posts[0][1].request_id, OLD);
  assert.equal(h.posts[0][1].redo, true); assert.equal(h.posts[0][1].recovery_of, OLD); h.dispose();
});

for (const broken of ['readBroken', 'writeBroken']) test(`${broken} blocks generation before any POST`, async () => {
  const h = harness(); h[broken] = true; await h.mount();
  if (broken === 'writeBroken') { await h.open(); await h.submit(); }
  assert.equal(h.posts.length, 0); assert.match(text(h.render()), /不能可靠保存|无法可靠保存|无法可靠保存请求编号|无法可靠/); h.dispose();
});
test('non-string persisted identities fail closed instead of silently losing an unknown request', async () => {
  for (const entry of [{videoId:123,requestId:OLD},{videoId:null,requestId:OLD},{videoId:VIDEO,requestId:123}]) {
    const raw=JSON.stringify({version:1,items:[entry]}),storage=new Map([[KEY,raw]]),h=harness({storage});
    await h.mount();assert.equal(button(h.render(),'生成并保存笔记').props.disabled,true);
    button(h.render(),'生成并保存笔记').props.onClick();await settle();
    assert.match(text(h.render()),/本机无法可靠保存请求编号/);assert.equal(h.posts.length,0);assert.equal(h.uuids,0);
    assert.equal(storage.get(KEY),raw);assert.equal(h.writes.length,0);h.dispose();
  }
});

test('stale completed notes download fixed old bytes; deleted and malformed URLs never become links', async () => {
  const h = harness({ rows: [item({ state: 'stale', source_current: false, can_redo: true }),
    item({ request_id: '33333333-3333-4333-8333-333333333333', state: 'deleted', can_download: true }),
    item({ request_id: '44444444-4444-4444-8444-444444444444', download_url: 'https://example.invalid/paid' })] });
  await h.mount();
  const downloads = links(h.render()).filter(link => text(link) === '下载笔记');
  assert.equal(downloads.length, 1); assert.equal(downloads[0].props.href, `/api/notes/artifacts/${NOTE}.md`);
  assert.match(text(h.render()), /基于此前保存的识别版本/); assert.match(text(h.render()), /资料已删除/);
  assert.equal(h.posts.length, 0); h.dispose();
});

test('cached generation usage stays attributed to original notes and unknown fees are never shown as zero', async () => {
  const h = harness({ rows: [item({ cached: true })] }); await h.mount(); const value = text(h.render());
  assert.match(value, /本次没有新增整理调用.*用量属于原笔记/); assert.match(value, /Token 用量未记录完整/);
  assert.match(value, /不跨复用请求合计/); assert.doesNotMatch(value, /0 元|0元|¥0|￥0/); h.dispose();
});

test('known zero tokens are shown as observed usage without a zero-cost claim', async () => {
  const h = harness({ rows: [item({ usage: { state: 'complete', known_tokens: { total_tokens: 0 }, request_attempts: 1, complete: true, is_invoice: false } })] });
  await h.mount(); assert.match(text(h.render()), /已记录 0 token.*非费用账单/); h.dispose();
});
test('deleted history survives same-video reimport and a new explicit generation uses a fresh UUID without reviving old requests', async () => {
  const h = harness({ rows: [item({ state: 'deleted', can_download: false, can_redo: false })] }); await h.mount();
  assert.match(text(h.render()), /旧删除记录保留/);
  assert.equal(button(h.render(),'生成并保存笔记').props.disabled,false);
  assert.equal(h.posts.length,0); await h.open(); assert.equal(h.uuids,0);
  await h.submit(); assert.equal(h.posts.length,1); assert.notEqual(h.posts[0][1].request_id,OLD);
  assert.equal(h.posts[0][1].redo,undefined); assert.equal(h.posts[0][1].recovery_of,undefined);
  assert.equal(h.state.items.find(row=>row.request_id===OLD).state,'deleted');
  assert.equal(h.state.items.length,2); h.dispose();
});

test('pagination failure preserves old rows and a fresh read can recover without generation', async () => {
  const rows = Array.from({ length: 12 }, (_, i) => item({ request_id: `11111111-1111-4111-8111-${String(i).padStart(12, '0')}` }));
  const h = harness({ rows }); await h.mount(); assert.equal(h.state.items.length, 10);
  const read = h.api.list; h.api.list = async () => { throw new Error(PRIVATE); };
  button(h.render(), '载入更多记录').props.onClick(); await settle();
  assert.equal(h.state.items.length, 10); assert.match(text(h.render()), /已显示的记录会保留/);
  h.api.list = read; button(h.render(), '载入更多记录').props.onClick(); await settle();
  assert.equal(h.state.items.length, 12); assert.equal(h.posts.length, 0); h.dispose();
});

test('a server-confirmed unregistered rejection clears only that intent, while unknown or review flags preserve it', async () => {
  for (const flags of [{ request_registered: false, needs_review: false }, {}, { request_registered: false, needs_review: true }]) {
    const h = harness(); await h.mount(); await h.open();
    h.api.create = async (video, body) => { h.posts.push([video, body]); return { success: false, code: 'SOURCE_CHANGED', ...flags }; };
    await h.submit();
    const remaining = JSON.parse(h.storage.get(KEY)).items.length;
    assert.equal(remaining, flags.request_registered === false && flags.needs_review !== true ? 0 : 1);
    assert.equal(h.posts.length, 1); h.dispose();
  }
});

test('preflight refusal clears unsent UUID without hiding the fixed update guidance', async () => {
  const h = harness(); await h.mount(); await h.open(); h.api.create = async () => { throw new AiNotesError('NOTES_BACKEND_UNSUPPORTED', false); };
  await h.submit(); assert.equal(h.state.intent, null); assert.match(text(h.render()), /更新后台/); h.dispose();
});

test('unmounted or stale confirmation cannot generate and closing viewing does not submit a cancellation', async () => {
  const h = harness(); await h.mount(); await h.open(); const old = button(h.render(), '确认并生成').props.onClick;
  h.dispose(); old(); await settle(); assert.equal(h.posts.length, 0); assert.equal(h.uuids, 0);
  const view = harness({ rows: [item({ state: 'running', can_download: false })] }); await view.mount();
  button(view.render(), '返回资料').props.onClick(); assert.equal(view.closed, 1); assert.equal(view.posts.length, 0); view.dispose();
});

test('late completed response cannot clear a different newer persisted request', async () => {
  const h = harness(); await h.mount(); await h.open(); const wait = deferred();
  let old; h.api.create = async (video, body) => { old = body.request_id; return wait.promise; };
  button(h.render(), '确认并生成').props.onClick(); await settle(); h.dispose();
  h.storage.set(KEY, pending(OLD)); wait.resolve({ success: true, item: item({ request_id: old }) }); await settle();
  assert.equal(JSON.parse(h.storage.get(KEY)).items[0].requestId, OLD);
});
