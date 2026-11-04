// In-memory browser fixtures. Serves the production bundle; never proxies APIs.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'dist');
const port = Number(process.argv[2] || 5199);
const libraries = [
  { id: 7, name: '学习笔记（合成验证）', video_count: 3, ready_count: 3 },
  { id: 8, name: '园艺资料（合成验证）', video_count: 2, ready_count: 2 },
];
const videos = Array.from({ length: 205 }, (_, i) => ({
  platform_item_id: String(7999999000000000000n + BigInt(i)),
  title: `合成验证视频 ${i + 1}：${i > 148 ? '保留较早处理记录，便于核对失败原因' : '整理笔记的方法与步骤'}`,
  author: '合成作者', duration: 120, status: i === 150 || i === 204 ? 'failed' : 'done',
  error_message: i === 150 || i === 204 ? '合成服务暂时不可用；本条没有重新提交。' : null,
}));
const sessions = [
  { id: 81, title: '学习资料里的整理步骤（合成）', knowledge_base_id: 7, chat_only: false, message_count: 2 },
  { id: 82, title: '不使用本地资料的对话（合成）', knowledge_base_id: null, chat_only: true, message_count: 2 },
];
const initialJobs = [
  { task_id: 'fixture-interrupted', status: 'interrupted', stage: 'transcribing', progress: 1, total: 2,
    message: '合成批次处理已中断，原记录保留。', metadata: { platform_item_ids: [videos[150].platform_item_id, videos[204].platform_item_id], knowledge_base_id: 7, analysis_mode: 'audio' },
    created_at: 1793790000, updated_at: 1793790120, needs_review: true, can_retry: true, usage_state: 'not_recorded' },
  ...Array.from({length: 12}, (_, i) => ({ task_id: `fixture-done-${i + 1}`, status: 'done', stage: 'finished', progress: 1, total: 1,
    metadata: { platform_item_ids: [videos[i].platform_item_id], knowledge_base_id: 7, analysis_mode: 'audio' },
    created_at: 1793780000 - i * 3600, updated_at: 1793780120 - i * 3600, can_retry: false, usage_state: 'not_recorded' })),
];
const jobs = structuredClone(initialJobs), retryResults = new Map(), operations = [];
const notes = new Map(), noteArtifacts = new Map();
let renderedNotesHTML = null;
const state = { loggedIn: true, listFailure: false, progressFailure: false, activeTask: false,
  taskHistoryFailure: false, retryAckFailure: false, deletionHistoryFailure: false,
  notesSupported: true, notesReady: true, notesLookupFailure: false, notesPostUnknown: false,
  notesSourceChanged: false, notesDeleted: false, notesHistoryFailure: false, calls: [] };
const noteFingerprint = () => (state.notesSourceChanged ? 'b' : 'a').repeat(64);
const noteView = row => {
  const { fingerprint, deleted: storedDeleted, ...value } = row;
  const stale = fingerprint !== noteFingerprint(), deleted = state.notesDeleted || storedDeleted === true;
  const downloadable = row.state === 'done' && !deleted;
  return { ...value, state: deleted ? 'deleted' : stale ? 'stale' : row.state,
    source_current: !stale && !deleted, can_download: downloadable,
    download_url: downloadable ? `/api/notes/artifacts/${row.note_id}.md` : null,
    can_redo: !deleted && (stale || ['failed', 'needs_review'].includes(row.state)) };
};
const json = (res, code, data) => { res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(data)); };
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml' };

createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  try {
    // A static DOM capture from the synthetic production browser, served from
    // the same origin so its real stylesheet/fonts keep their CORS contract.
    if (url.pathname === '/__test/notes-rendered') {
      if (req.method === 'POST') {
        let body = ''; for await (const chunk of req) { body += chunk; if (body.length > 1_000_000) throw new Error(); }
        renderedNotesHTML = body; json(res, 200, { success: true }); return;
      }
      if (!renderedNotesHTML) { res.writeHead(404); res.end(); return; }
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(renderedNotesHTML); return;
    }
    if (url.pathname === '/__test/state') {
      for (const key of ['loggedIn', 'listFailure', 'progressFailure', 'activeTask', 'taskHistoryFailure', 'retryAckFailure', 'deletionHistoryFailure',
        'notesSupported', 'notesReady', 'notesLookupFailure', 'notesPostUnknown', 'notesSourceChanged', 'notesDeleted', 'notesHistoryFailure']) {
        if (url.searchParams.has(key)) state[key] = url.searchParams.get(key) === 'true';
      }
      if (url.searchParams.has('resetRecovery')) {
        jobs.splice(0, jobs.length, ...structuredClone(initialJobs)); retryResults.clear(); operations.splice(0);
        videos.forEach((video, i) => { video.status = i === 150 || i === 204 ? 'failed' : 'done'; });
      }
      if (url.searchParams.has('clearCalls')) state.calls = [];
      if (url.searchParams.has('resetNotes')) { notes.clear(); noteArtifacts.clear(); }
      if (url.searchParams.get('notesDeleted') === 'true') for (const row of notes.values()) {
        row.deleted = true; noteArtifacts.delete(row.note_id);
      }
      if (url.searchParams.has('completeNotes')) for (const row of notes.values()) {
        if (!['queued', 'running'].includes(row.state)) continue;
        row.state = 'done'; row.progress = row.total;
        row.usage = { state: 'complete', known_tokens: { prompt_tokens: 320, completion_tokens: 180, total_tokens: 500 }, request_attempts: 1, complete: true, is_invoice: false, scope: 'generation_observed_tokens_not_invoice' };
        noteArtifacts.set(row.note_id, `# 合成笔记\n\n先核对已保存的识别内容，再整理要点。\n\n## 当时的原始识别结果\n\n合成文字稿版本 ${row.fingerprint[0]}：先核对原文。\n`);
      }
      json(res, 200, state); return;
    }
    if (url.pathname.startsWith('/api/')) {
      let body = null;
      if (req.method !== 'GET') {
        let raw = ''; for await (const chunk of req) raw += chunk;
        body = JSON.parse(raw || '{}');
      }
      state.calls.push({ method: req.method, path: url.pathname, query: url.search, body });
      if (url.pathname === '/api/notes/capabilities') {
        json(res, state.notesSupported ? 200 : 404, state.notesSupported ? { success: true, version: 1, ready: state.notesReady,
          max_source_chars: 120000, chunk_chars: 12000, max_expected_calls: 11, usage_is_invoice: false } : { code: 'NOT_FOUND' }); return;
      }
      const noteVideo = url.pathname.match(/^\/api\/notes\/videos\/([A-Za-z0-9_-]+)$/);
      if (noteVideo) {
        if (req.method === 'GET') {
          if (state.notesHistoryFailure) { json(res, 503, { success: false, code: 'NOTES_READ_UNAVAILABLE' }); return; }
          const limit = Number(url.searchParams.get('limit') || 10), offset = Number(url.searchParams.get('offset') || 0);
          const rows = [...notes.values()].filter(row => row.platform_item_id === noteVideo[1]).reverse().map(noteView);
          json(res, 200, { success: true, source: { status: state.notesDeleted ? 'unavailable' : 'ready',
            fingerprint: state.notesDeleted ? null : noteFingerprint(), char_count: state.notesDeleted ? null : 3200,
            chunk_count: state.notesDeleted ? null : 1, expected_call_count: state.notesDeleted ? null : 1 },
            items: rows.slice(offset, offset + limit), total: rows.length, limit, offset,
            current_note: rows.find(row => row.state === 'done' && row.source_current) || null }); return;
        }
        if (body.confirm !== true || body.expected_source_fingerprint !== noteFingerprint() || state.notesDeleted) {
          json(res, 409, { success: false, code: 'SOURCE_CHANGED', request_registered: false, needs_review: false }); return;
        }
        let row = notes.get(body.request_id), duplicate = Boolean(row);
        if (!row) {
          row = { request_id: body.request_id, platform_item_id: noteVideo[1], fingerprint: noteFingerprint(),
            state: state.notesPostUnknown ? 'needs_review' : 'queued', task_id: randomUUID(), note_id: randomUUID(),
            progress: 0, total: 1, expected_call_count: 1, reused_step_count: 0, reason_code: null,
            model_label: '合成验证模型', cached: false,
            created_at: new Date(Date.UTC(2026, 10, 4, 14, 0, 0) + notes.size * 1000).toISOString(),
            updated_at: new Date(Date.UTC(2026, 10, 4, 14, 0, 0) + notes.size * 1000).toISOString(),
            usage: { state: 'unknown', known_tokens: {}, request_attempts: null, complete: false, is_invoice: false, scope: 'generation_observed_tokens_not_invoice' } };
          notes.set(body.request_id, row);
        }
        if (state.notesPostUnknown) { json(res, 503, { success: false, code: 'NOTES_UNCONFIRMED', needs_review: true }); return; }
        json(res, 200, { success: true, duplicate, cached: false, item: noteView(row) }); return;
      }
      const noteRequest = url.pathname.match(/^\/api\/notes\/requests\/([a-f\d-]+)$/i);
      if (noteRequest) {
        if (state.notesLookupFailure) { json(res, 503, { success: false, code: 'NOTES_READ_UNAVAILABLE' }); return; }
        const row = notes.get(noteRequest[1]);
        json(res, 200, { success: true, found: Boolean(row), request_id: noteRequest[1], ...(row ? { item: noteView(row) } : {}) }); return;
      }
      const artifact = url.pathname.match(/^\/api\/notes\/artifacts\/([a-f\d-]+)\.md$/i);
      if (artifact) {
        if (state.notesDeleted || !noteArtifacts.has(artifact[1])) { json(res, 410, { code: 'NOTES_DELETED' }); return; }
        res.writeHead(200, { 'Content-Type': 'text/markdown; charset=utf-8', 'Content-Disposition': `attachment; filename="${artifact[1]}.md"`, 'Cache-Control': 'no-store' });
        res.end(noteArtifacts.get(artifact[1])); return;
      }
      if (url.pathname === '/api/chat/capabilities') { json(res, 200, { success: true, chat_only_sessions: true, session_mode_persistent: true, modes: ['knowledge', 'chat_only'] }); return; }
      if (url.pathname === '/api/auth/douyin/login/status') {
        json(res, 200, { status: state.loggedIn ? 'logged_in' : 'logged_out', message: state.loggedIn ? '合成验证 · 收藏已连接' : '合成验证 · 收藏未连接' }); return;
      }
      if (url.pathname === '/api/auth/douyin/login/start') { json(res, 200, { success: true, status: 'waiting' }); return; }
      if (url.pathname === '/api/auth/douyin/logout') { state.loggedIn = false; json(res, 200, { success: true }); return; }
      if (url.pathname === '/api/knowledge-bases') { json(res, 200, { success: true, items: libraries, total: libraries.length }); return; }
      if (/^\/api\/knowledge-bases\/\d+\/videos$/.test(url.pathname)) {
        json(res, 200, { success: true, items: videos.slice(0, 3), total: 3 }); return;
      }
      if (url.pathname === '/api/knowledge/stats') {
        const videoCache = Object.fromEntries(['done', 'failed', 'pending', 'deleting'].map(status => [status, videos.filter(v => v.status === status).length]));
        json(res, 200, { success: true, video_cache: videoCache,
          current_task_id: state.activeTask ? 'fixture-job' : null, analysis_mode: 'audio', capabilities: { guarded_video_delete: true } }); return;
      }
      if (url.pathname === '/api/knowledge/videos') {
        if (state.listFailure) { json(res, 503, { detail: 'synthetic list failure' }); return; }
        const all = url.searchParams.get('status') === 'all' ? videos : videos.filter(v => v.status === 'done');
        const page = Number(url.searchParams.get('page') || 1), size = Number(url.searchParams.get('size') || 100);
        json(res, 200, { success: true, items: all.slice((page - 1) * size, page * size), total: all.length }); return;
      }
      const content = url.pathname.match(/^\/api\/knowledge\/videos\/(\d+)\/content$/);
      if (content) {
        const video = videos.find(item => item.platform_item_id === content[1]) || videos[0];
        json(res, 200, { success: true, ...video, transcript_text: video.status === 'pending' ? '' : '合成文字稿：先核对原文，再整理成便于查找的笔记。',
          content: '合成文字稿：先核对原文，再整理成便于查找的笔记。', transcript_segments: [], visual_segments: [], warnings: [], visual_analysis_text: '', analysis_mode: 'audio' }); return;
      }
      if (url.pathname === '/api/knowledge/sync/fixture-job') {
        if (state.progressFailure) { json(res, 503, { detail: 'synthetic progress failure' }); return; }
        json(res, 200, { success: true, status: 'running', progress: 12, total: 30, message: '正在处理合成验证视频 13' }); return;
      }
      if (url.pathname === '/api/knowledge/tasks' && req.method === 'GET') {
        if (state.taskHistoryFailure) { json(res, 503, { code: 'INGESTION_HISTORY_UNAVAILABLE' }); return; }
        const limit = Number(url.searchParams.get('limit') || 10), offset = Number(url.searchParams.get('offset') || 0);
        json(res, 200, { success: true, items: jobs.slice(offset, offset + limit), total: jobs.length, limit, offset }); return;
      }
      const retry = url.pathname.match(/^\/api\/knowledge\/tasks\/([^/]+)\/(retry|retry-result)$/);
      if (retry) {
        const oldId = decodeURIComponent(retry[1]);
        const requestId = req.method === 'GET' ? url.searchParams.get('request_id') : body?.request_id;
        const key = oldId + ':' + requestId;
        if (retry[2] === 'retry-result') {
          const task = retryResults.get(key);
          json(res, 200, task ? { success: true, found: true, task_id: task.task_id, task, recovery_of: oldId }
            : { success: true, found: false, recovery_of: oldId }); return;
        }
        if (body?.confirm_reprocess !== true || typeof requestId !== 'string') { json(res, 422, { code: 'INGESTION_CONFIRMATION_REQUIRED' }); return; }
        let task = retryResults.get(key);
        if (!task) {
          const old = jobs.find(item => item.task_id === oldId);
          if (!old?.can_retry) { json(res, 200, { success: false, code: 'INGESTION_RETRY_NOT_ALLOWED' }); return; }
          task = { ...structuredClone(old), task_id: randomUUID(), status: 'queued', stage: 'queued', progress: 0,
            metadata: { ...old.metadata, recovery_of: oldId }, can_retry: false, needs_review: false,
            created_at: Date.now() / 1000, updated_at: Date.now() / 1000 };
          retryResults.set(key, task); jobs.unshift(task);
        }
        if (state.retryAckFailure) { json(res, 503, { code: 'INGESTION_RETRY_UNAVAILABLE' }); return; }
        json(res, 200, { success: true, task_id: task.task_id, task, recovery_of: oldId }); return;
      }
      const jobProgress = url.pathname.match(/^\/api\/knowledge\/sync\/([^/]+)$/);
      if (jobProgress) {
        const task = jobs.find(item => item.task_id === jobProgress[1]);
        json(res, 200, task ? { success: true, ...task } : { success: false }); return;
      }
      const deleteVideo = url.pathname.match(/^\/api\/knowledge\/videos\/(\d+)$/);
      if (deleteVideo && req.method === 'DELETE') {
        const video = videos.find(item => item.platform_item_id === deleteVideo[1]);
        const operation = { operation_id: randomUUID(), platform_item_id: video.platform_item_id,
          deletion_state: 'pending', last_error_code: 'index_delete_failed', active: true, can_resume: true,
          created_at: '2026-11-04T11:00:00Z', updated_at: '2026-11-04T11:01:00Z' };
        video.status = 'deleting'; operations.unshift(operation);
        json(res, 200, { success: false, code: 'DELETION_INCOMPLETE', operation_id: operation.operation_id, status: 'deleting' }); return;
      }
      if (url.pathname === '/api/knowledge/deletions') {
        if (state.deletionHistoryFailure) { json(res, 503, { code: 'DELETION_HISTORY_UNAVAILABLE' }); return; }
        const rows = url.searchParams.get('active_only') === 'true' ? operations.filter(item => item.active) : operations;
        const limit = Number(url.searchParams.get('limit') || 10), offset = Number(url.searchParams.get('offset') || 0);
        json(res, 200, { success: true, items: rows.slice(offset, offset + limit), total: rows.length, limit, offset }); return;
      }
      const resume = url.pathname.match(/^\/api\/knowledge\/deletions\/([^/]+)\/resume$/);
      if (resume && req.method === 'POST') {
        const operation = operations.find(item => item.operation_id === resume[1]);
        if (!operation || body?.confirm !== true) { json(res, 200, { success: false, code: 'DELETION_CONFIRMATION_REQUIRED' }); return; }
        if (operation.deletion_state !== 'done') {
          operation.active = false; operation.can_resume = false; operation.deletion_state = 'done';
          videos.find(item => item.platform_item_id === operation.platform_item_id).status = 'pending';
        }
        json(res, 200, { success: true, operation_id: operation.operation_id, deletion_state: 'done', already_completed: true }); return;
      }
      if (url.pathname === '/api/favorites/collections') {
        json(res, state.loggedIn ? 200 : 401, state.loggedIn ? { success: true, items: [{ id: 1, collection_id: 'all', title: '全部收藏（合成）', video_count: 2 }], total: 1 } : { detail: 'synthetic login required' }); return;
      }
      if (url.pathname.startsWith('/api/favorites/') && url.pathname.endsWith('/videos')) {
        json(res, state.loggedIn ? 200 : 401, { success: state.loggedIn, items: state.loggedIn ? videos.slice(0, 2) : [], total: state.loggedIn ? 2 : 0 }); return;
      }
      if (url.pathname === '/api/chat/sessions') { json(res, 200, { success: true, items: sessions, total: sessions.length }); return; }
      const history = url.pathname.match(/^\/api\/chat\/sessions\/(\d+)\/messages$/);
      if (history) {
        const session = sessions.find(item => item.id === Number(history[1]));
        json(res, 200, { success: true, ...session, session_id: session.id, items: [
          { role: 'user', content: session.chat_only ? '合成纯AI问题' : '原文如何整理笔记？' },
          { role: 'assistant', content: '这是合成历史回答，未调用真实模型。', sources: [] },
        ] }); return;
      }
      if (url.pathname === '/api/chat/ask/stream') {
        res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
        const send = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
        const sources = body.chat_only ? [] : [{ platform_item_id: videos[0].platform_item_id, title: videos[0].title,
          url: `https://www.douyin.com/video/${videos[0].platform_item_id}` }];
        const answer = body.chat_only ? '合成纯AI回答：本次未使用本地资料。' : '合成本地资料回答：先核对原文，再整理笔记。';
        send('sources', { sources }); send('delta', { text: answer });
        send('meta', { session_id: body.session_id || 83, chat_only: Boolean(body.chat_only), answer, sources, route_type: body.chat_only ? 'direct' : 'vector' });
        send('done', { ok: true }); res.end(); return;
      }
      json(res, 404, { code: 'synthetic_endpoint_not_defined' }); return;
    }
    const requested = resolve(root, '.' + decodeURIComponent(url.pathname));
    if (requested !== root && !requested.startsWith(root + '\\') && !requested.startsWith(root + '/')) { res.writeHead(403); res.end(); return; }
    const file = extname(requested) ? requested : resolve(root, 'index.html');
    res.writeHead(200, { 'Content-Type': mime[extname(file)] || 'application/octet-stream' });
    res.end(await readFile(file));
  } catch { if (!res.headersSent) res.writeHead(500); res.end(); }
}).listen(port, '127.0.0.1', () => console.log(`Synthetic production preview only: http://127.0.0.1:${port}`));
