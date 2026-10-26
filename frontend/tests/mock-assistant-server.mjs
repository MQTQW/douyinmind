// Isolated UI preview only: synthetic records, no platform/model/database access.
import { createServer } from 'node:http';
const port = Number(process.env.DOUYINMIND_MOCK_PORT || 18731);
const libraries = [{ id: 7, name: '学习与工作（演示）', video_count: 3, ready_count: 3, created_at: '', updated_at: '' },
  { id: 8, name: '生活灵感（演示）', video_count: 2, ready_count: 2, created_at: '', updated_at: '' }];
createServer((request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1');
  let result;
  if (request.method !== 'GET') {
    response.writeHead(405, { 'Content-Type': 'application/json' }); response.end(JSON.stringify({ code: 'mock_readonly' })); return;
  }
  if (url.pathname === '/api/auth/douyin/login/status') result = { status: 'logged_in', message: '演示数据 · 未连接抖音' };
  else if (url.pathname === '/api/knowledge-bases') result = { success: true, items: libraries, total: libraries.length };
  else if (url.pathname === '/api/knowledge/stats') result = { success: true, video_cache: { done: 5, pending: 0, failed: 0 } };
  else if (url.pathname === '/api/favorites/collections' || url.pathname === '/api/chat/sessions' || url.pathname.includes('/videos')) result = { success: true, items: [], total: 0 };
  else { response.writeHead(404, { 'Content-Type': 'application/json' }); response.end(JSON.stringify({ code: 'mock_endpoint_missing' })); return; }
  response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); response.end(JSON.stringify(result));
}).listen(port, '127.0.0.1', () => console.log(`Assistant synthetic preview only: http://127.0.0.1:${port}`));
