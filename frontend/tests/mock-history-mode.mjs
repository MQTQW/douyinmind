// Isolated browser verification. All records are in memory; no real data or model calls.
// DOUYINMIND_MOCK_MODE=history selects this handler in mock-chat-server.mjs.
// GET /__test/finish completes pending answers. Restarting resets the sample history.
const sendJSON = (res, status, data) => {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(data));
};
const emit = (res, event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

async function readJSON(req) {
  let text = '';
  for await (const chunk of req) {
    text += chunk;
    if (text.length > 100_000) throw new Error('样例请求过长');
  }
  const body = JSON.parse(text || '{}');
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('样例请求应为对象');
  return body;
}

export function createHistoryHandler() {
  const sampleSources = [{ platform_item_id: 'history-sample', title: '功能验证样例来源（模拟内容）', url: '#', score: null }];
  const now = Date.now();
  const sessions = new Map([
    [101, {
      id: 101, title: '功能验证样例：知识库概念', created_at: new Date(now - 60_000).toISOString(),
      messages: [
        { id: 1, session_id: 101, role: 'user', content: '什么是知识库？', created_at: new Date(now - 60_000).toISOString(), sources: [] },
        { id: 2, session_id: 101, role: 'assistant', content: '这是功能验证样例。知识库是集中保存资料、方便检索和提问的地方。你可以在这段历史对话中继续提问。', created_at: new Date(now - 59_000).toISOString(), sources: sampleSources, latency_ms: 123 },
      ],
    }],
    [102, {
      id: 102, title: '功能验证样例：学习计划', created_at: new Date(now - 120_000).toISOString(),
      messages: [
        { id: 3, session_id: 102, role: 'user', content: '怎样安排每天的学习？', created_at: new Date(now - 120_000).toISOString(), sources: [] },
        { id: 4, session_id: 102, role: 'assistant', content: '这是另一段功能验证样例。每天选一个小目标，学习后记录自己的理解，再做一次练习。', created_at: new Date(now - 119_000).toISOString(), sources: [], latency_ms: 100 },
      ],
    }],
  ]);
  const pending = new Set();
  const failNextHistory = new Set();
  let nextSession = 103;
  let nextMessage = 5;

  return async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const method = req.method || 'GET';
    if (url.pathname === '/__test/finish' && method === 'GET') {
      for (const item of [...pending]) {
        const { stream, body, sessionId } = item;
        if (body.query.includes('失败')) {
          emit(stream, 'error', { message: '功能验证样例：模拟回答失败，请重试。' });
        } else if (!body.query.includes('断线')) {
          const ending = `\n你刚刚问的是：“${body.query}”。这次回答已保存到同一段历史对话中。`;
          const answer = item.beginning + ending;
          const timestamp = new Date().toISOString();
          const session = sessions.get(sessionId) || {
            id: sessionId, title: body.query.slice(0, 128), created_at: timestamp, messages: [],
          };
          session.messages.push(
            { id: nextMessage++, session_id: sessionId, role: 'user', content: body.query, created_at: timestamp, sources: [] },
            { id: nextMessage++, session_id: sessionId, role: 'assistant', content: answer, created_at: timestamp, sources: sampleSources, latency_ms: 123 },
          );
          sessions.set(sessionId, session);
          emit(stream, 'delta', { text: ending });
          emit(stream, 'meta', { session_id: sessionId, route_type: 'direct', latency_ms: 123, answer, sources: sampleSources });
          emit(stream, 'done', { ok: true });
        }
        pending.delete(item);
        stream.end();
      }
      sendJSON(res, 200, { ok: true });
      return;
    }
    if (url.pathname === '/__test/history/fail-next' && method === 'GET') {
      failNextHistory.add(Number(url.searchParams.get('session_id') || 101));
      sendJSON(res, 200, { ok: true });
      return;
    }
    if (url.pathname === '/api/chat/sessions' && method === 'GET') {
      const items = [...sessions.values()].map(session => ({
        id: session.id, title: session.title, message_count: session.messages.length,
        created_at: session.created_at, last_message_at: session.messages.at(-1)?.created_at ?? null,
      })).sort((a, b) => (b.last_message_at || '').localeCompare(a.last_message_at || ''));
      sendJSON(res, 200, { success: true, items });
      return;
    }

    const match = url.pathname.match(/^\/api\/chat\/sessions\/(\d+)(\/messages)?$/);
    if (match) {
      const id = Number(match[1]);
      const session = sessions.get(id);
      if (!session) {
        sendJSON(res, 404, { detail: '功能验证样例：对话不存在' });
        return;
      }
      if (match[2] && method === 'GET') {
        if (failNextHistory.delete(id)) {
          sendJSON(res, 503, { detail: '功能验证样例：模拟历史加载失败' });
        } else {
          sendJSON(res, 200, { success: true, title: session.title, items: session.messages });
        }
        return;
      }
      if (!match[2] && (method === 'PATCH' || method === 'DELETE')) {
        if ([...pending].some(item => item.sessionId === id)) {
          sendJSON(res, 409, { detail: '功能验证样例：请等待当前回答完成' });
          return;
        }
        if (method === 'DELETE') {
          sessions.delete(id);
          sendJSON(res, 200, { success: true });
          return;
        }
        let body;
        try { body = await readJSON(req); } catch {
          sendJSON(res, 400, { detail: '功能验证样例：请求格式错误' });
          return;
        }
        const title = typeof body.title === 'string' ? body.title.trim() : '';
        if (!title || title.length > 128) {
          sendJSON(res, 400, { detail: '对话名称请填写 1～128 个字。' });
          return;
        }
        session.title = title;
        sendJSON(res, 200, { success: true, session_id: id, title });
        return;
      }
    }
    if (url.pathname === '/api/chat/ask/stream' && method === 'POST') {
      let body;
      try { body = await readJSON(req); } catch {
        sendJSON(res, 400, { detail: '功能验证样例：请求格式错误' });
        return;
      }
      if (typeof body.query !== 'string' || !body.query.trim()) {
        sendJSON(res, 400, { detail: '问题不能为空' });
        return;
      }
      const sessionId = body.session_id ?? nextSession++;
      if (body.session_id != null && !sessions.has(sessionId)) {
        sendJSON(res, 404, { detail: '功能验证样例：对话不存在' });
        return;
      }
      res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-cache' });
      const beginning = '这是功能验证样例的模拟续聊回答。';
      emit(res, 'sources', { sources: sampleSources });
      emit(res, 'delta', { text: beginning });
      const item = { stream: res, body, sessionId, beginning };
      pending.add(item);
      res.on('close', () => pending.delete(item));
      return;
    }
    if (url.pathname === '/api/auth/douyin/login/status') {
      sendJSON(res, 200, { status: 'logged_in', message: '功能验证样例 · 未连接抖音或真实 AI' });
      return;
    }
    if (url.pathname === '/api/favorites/collections') {
      sendJSON(res, 200, { success: true, items: [], total: 0 });
      return;
    }
    if (url.pathname === '/api/knowledge/stats') {
      sendJSON(res, 200, { success: true, video_cache: { done: 0, pending: 0, failed: 0 }, analysis_mode: 'audio' });
      return;
    }
    sendJSON(res, 404, { detail: '功能验证样例：接口不存在' });
  };
}
