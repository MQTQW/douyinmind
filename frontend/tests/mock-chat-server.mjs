// Browser verification only. Uses synthetic data and never contacts an AI service.
// Run this on port 8000, then start Vite. GET /__test/finish releases the final answer.
import { createServer } from 'node:http';
import { createHistoryHandler } from './mock-history-mode.mjs';

const demoMode = process.env.DOUYINMIND_DEMO === '1';
const historyMode = process.env.DOUYINMIND_MOCK_MODE === 'history';
const historyHandler = historyMode ? createHistoryHandler() : null;
const port = Number(process.env.DOUYINMIND_MOCK_PORT || 8000);
const sources = [{ platform_item_id: '123', title: demoMode ? '演示资料（模拟内容）' : '测试视频：学习方法', url: demoMode ? '#' : 'https://www.douyin.com/video/123', score: 0.9 }];
const pending = new Set();
let nextSession = 1;
const emit = (res, event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
const server = createServer(async (req, res) => {
  if (historyHandler) {
    await historyHandler(req, res);
    return;
  }
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname === '/api/chat/ask/stream') {
    let text = '';
    for await (const chunk of req) text += chunk;
    const body = JSON.parse(text);
    console.log('Streaming request:', JSON.stringify(body));
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
    emit(res, 'sources', { sources });
    if (demoMode) {
      const answer = `你刚刚问的是：“${body.query}”。\n\n这是本地模拟回答，用来演示文字一段段出现的效果，没有调用真实 AI。\n\n你可以看到，第一部分出现后，后面的文字还在继续生成，不用等待整段回答完成。\n\n真实使用时，程序会根据你的收藏视频内容回答，并显示对应来源。配置好 AI 服务密钥后，请使用实际项目页面。`;
      const sessionId = body.session_id ?? nextSession++;
      const started = Date.now();
      let offset = 0;
      const timer = setInterval(() => {
        if (offset < answer.length) {
          emit(res, 'delta', { text: answer.slice(offset, offset + 4) });
          offset += 4;
          return;
        }
        clearInterval(timer);
        emit(res, 'meta', { session_id: sessionId, route_type: 'direct', latency_ms: Date.now() - started, answer, sources });
        emit(res, 'done', { ok: true });
        res.end();
      }, 90);
      res.on('close', () => clearInterval(timer));
      return;
    }
    emit(res, 'delta', { text: '这是逐步显示的第一句。' });
    const item = { res, body, sessionId: body.session_id ?? nextSession++ };
    pending.add(item);
    res.on('close', () => pending.delete(item));
    return;
  }
  let data;
  if (url.pathname === '/__test/finish') {
    for (const item of pending) {
      const { res: stream, body, sessionId } = item;
      if (body.query.includes('失败')) {
        emit(stream, 'error', { message: '模拟连接故障，请重试。' });
      } else if (!body.query.includes('断线')) {
        emit(stream, 'delta', { text: '\n这是后续生成的第二句。🧠' });
        emit(stream, 'meta', {
          session_id: sessionId, route_type: 'vector', latency_ms: 123,
          answer: '这是逐步显示的第一句。\n这是后续生成的第二句。🧠', sources,
          trace: { route: 'vector', steps: [{ name: '向量检索', time_ms: 23 }, { name: 'LLM 生成', time_ms: 100 }], chunks: [{ chunk_id: '123-0', title: sources[0].title, text: '测试召回内容', score: 0.9 }] },
        });
        emit(stream, 'done', { ok: true });
      }
      stream.end();
    }
    pending.clear();
    data = { ok: true };
  } else if (url.pathname === '/api/auth/douyin/login/status') {
    data = { status: 'logged_in', message: demoMode ? '模拟演示 · 未连接抖音或真实 AI' : '本地模拟测试' };
  } else if (url.pathname === '/api/favorites/collections') {
    data = { success: true, items: [{ id: 1, collection_id: 'test-collection', title: '测试收藏夹', video_count: 1, is_active: true }], total: 1 };
  } else if (url.pathname === '/api/knowledge/stats') {
    data = { success: true, video_cache: { done: 1, pending: 0, failed: 0 } };
  } else if (url.pathname.includes('/videos') || url.pathname === '/api/chat/sessions') {
    data = { success: true, items: [], total: 0 };
  } else {
    res.writeHead(404);
    res.end();
    return;
  }
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(data));
});
server.listen(port, '127.0.0.1', () => console.log(`${historyMode ? '功能验证样例：历史对话' : 'Synthetic chat server'}: http://127.0.0.1:${port}`));
