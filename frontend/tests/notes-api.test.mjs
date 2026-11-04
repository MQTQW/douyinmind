import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const asURL = value => `data:text/javascript;base64,${Buffer.from(value).toString('base64')}`;
const source = (await readFile(new URL('../src/api.ts', import.meta.url), 'utf8'))
  .replace("'./sse'", `'${asURL('export function readSSE() {}')}'`);
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext } });
const api = await import(asURL(outputText));
const ID = '11111111-1111-4111-8111-111111111111', NOTE = '22222222-2222-4222-8222-222222222222';
const BODY = { request_id: ID, confirm: true, expected_source_fingerprint: 'a'.repeat(64) };
const CAP = { success: true, version: 1, ready: true, max_source_chars: 120000, chunk_chars: 12000, max_expected_calls: 11, usage_is_invoice: false };
const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
async function fakeFetch(callback, run) {
  const previous = globalThis.fetch, calls = [];
  globalThis.fetch = async (path, options = {}) => { calls.push([path, options]); return callback(path, options, calls); };
  try { await run(calls); } finally { globalThis.fetch = previous; }
}

for (const [name, capability] of [
  ['missing backend capability', null], ['old version', { ...CAP, version: 0 }],
  ['not ready', { ...CAP, ready: false }], ['missing invoice scope', { ...CAP, usage_is_invoice: undefined }],
]) {
  test(`${name} blocks paid POST without falling back to legacy AI GET`, async () => {
    await fakeFetch(() => capability ? response(capability) : response({ detail: 'PRIVATE_PROVIDER_BODY' }, 404), async calls => {
      await assert.rejects(api.createAiNote('synthetic-video', BODY), error => error instanceof api.AiNotesError && error.requestSent === false && !/PRIVATE/.test(error.message));
      assert.deepEqual(calls.map(([path]) => path), ['/api/notes/capabilities']);
      assert.ok(calls.every(([path]) => !path.includes('mode=ai')));
    });
  });
}

test('supported note generation sends stable UUID, explicit confirmation, fingerprint and redo identity only to POST', async () => {
  const body = { ...BODY, redo: true, recovery_of: NOTE };
  await fakeFetch(path => path.endsWith('capabilities') ? response(CAP) : response({ success: true, item: { request_id: ID } }), async calls => {
    await api.createAiNote('synthetic-video', body);
    assert.equal(calls.length, 2);
    assert.equal(calls[1][0], '/api/notes/videos/synthetic-video');
    assert.equal(calls[1][1].method, 'POST');
    assert.deepEqual(JSON.parse(calls[1][1].body), body);
  });
});

test('capability is checked afresh after backend restart even within one API module', async () => {
  let reads = 0;
  await fakeFetch(path => path.endsWith('capabilities') ? (++reads === 1 ? response(CAP) : response({}, 404)) : response({ success: true }), async calls => {
    await api.createAiNote('synthetic-video', BODY);
    await assert.rejects(api.createAiNote('synthetic-video', { ...BODY, request_id: NOTE }));
    assert.equal(calls.filter(([, options]) => options.method === 'POST').length, 1);
    assert.equal(reads, 2);
  });
});

test('transport uncertainty after POST remains distinguishable from a preflight refusal and never leaks raw errors', async () => {
  await fakeFetch(path => {
    if (path.endsWith('capabilities')) return response(CAP);
    throw new Error('PRIVATE_PROVIDER_BODY');
  }, async calls => {
    await assert.rejects(api.createAiNote('synthetic-video', BODY), error => error instanceof api.AiNotesError && error.requestSent === true && error.requestRegistered === undefined && !/PRIVATE/.test(error.message));
    assert.equal(calls.filter(([, options]) => options.method === 'POST').length, 1);
  });
});

test('explicit backend no-registration flag survives HTTP errors without guessing from 409 alone', async () => {
  for (const flags of [{ request_registered: false, needs_review: false }, {}, { request_registered: false, needs_review: true }]) {
    await fakeFetch(path => path.endsWith('capabilities') ? response(CAP) : response({ code: 'SOURCE_CHANGED', message: 'PRIVATE_PROVIDER_BODY', ...flags }, 409), async () => {
      await assert.rejects(api.createAiNote('synthetic-video', BODY), error => error instanceof api.AiNotesError
        && error.requestSent === true && error.requestRegistered === flags.request_registered && error.needsReview === flags.needs_review && !/PRIVATE/.test(error.message));
    });
  }
});

test('malformed IDs, fingerprints and redo scope fail before any request', async () => {
  await fakeFetch(() => assert.fail('invalid input must not fetch'), async calls => {
    for (const [video, body] of [['../outside', BODY], ['synthetic-video', { ...BODY, confirm: false }],
      ['synthetic-video', { ...BODY, request_id: 'wrong' }], ['synthetic-video', { ...BODY, expected_source_fingerprint: 'wrong' }],
      ['synthetic-video', { ...BODY, redo: true }], ['synthetic-video', { ...BODY, recovery_of: NOTE }]]) {
      await assert.rejects(api.createAiNote(video, body));
    }
    await assert.rejects(api.getAiNoteRequest('not-a-uuid'));
    assert.equal(calls.length, 0);
  });
});

test('history and request lookup use pure GET paths with no automatic POST', async () => {
  await fakeFetch(() => response({ success: true, found: false }), async calls => {
    await api.listAiNotes('synthetic-video', 10, 20);
    await api.getAiNoteRequest(ID);
    assert.deepEqual(calls.map(([path]) => path), ['/api/notes/videos/synthetic-video?limit=10&offset=20', `/api/notes/requests/${ID}`]);
    assert.ok(calls.every(([, options]) => options.method === undefined));
  });
});

test('download accepts fixed ready or stale artifact bytes only with matching capability and canonical URL', () => {
  const item = { note_id: NOTE, state: 'done', source_current: true, can_download: true, download_url: `/api/notes/artifacts/${NOTE}.md` };
  assert.equal(api.aiNoteDownloadURL(item), item.download_url);
  assert.equal(api.aiNoteDownloadURL({ ...item, state: 'stale', source_current: false }), item.download_url);
  for (const changed of [{ state: 'deleted' }, { state: 'needs_review' }, { state: 'running' }, { can_download: false },
    { note_id: ID }, { note_id: '../outside' }, { download_url: 'https://example.invalid/paid' }, { download_url: '/api/knowledge/export/video?mode=ai' }]) {
    assert.equal(api.aiNoteDownloadURL({ ...item, ...changed }), null);
  }
});
