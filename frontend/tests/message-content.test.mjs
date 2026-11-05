import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { localModuleURL } from './load-local-ts.mjs';

const { default: MessageContent } = await import(await localModuleURL(new URL('../src/components/MessageContent.tsx', import.meta.url)));
const render = (content, sources = [], trace) => renderToStaticMarkup(createElement(MessageContent, { content, sources, trace }));

test('answers render headings, ordered lists, emphasis, code and tables as readable content', () => {
  const html = render('## 学习方法\n\n1. **先提问**\n2. 再复述\n\n`命令`\n\n| 步骤 | 内容 |\n| --- | --- |\n| 一 | 提问 |');
  assert.match(html, /<h2>学习方法<\/h2>/);
  assert.match(html, /<ol>/);
  assert.match(html, /<strong>先提问<\/strong>/);
  assert.match(html, /<code>命令<\/code>/);
  assert.match(html, /<table>/);
});

test('model HTML, executable links and image tracking cannot become active browser content', () => {
  const html = render('<script>alert(1)</script>\n\n[链接](javascript:alert%281%29)\n\n![演示](https://example.invalid/tracking.png)');
  assert.doesNotMatch(html, /<script|javascript:|<img|src=/i);
  assert.match(html, /图片：演示/);
});

test('external source links open with isolation and partial streamed text remains visible', () => {
  const html = render('[视频](https://example.invalid/video)\n\n**尚未结束的段落');
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /尚未结束的段落/);
});

test('literal angle-bracket parameters in source-backed answers are preserved as text', () => {
  const html = render('参数是 <workspace>，不要省略。');
  assert.match(html, /&lt;workspace&gt;/);
  assert.match(html, /不要省略/);
});

const sourceItem = (id, title) => ({ platform_item_id: id, title, url: `https://www.douyin.com/video/${id}`, score: .8 });

test('known multiline captions become compact numbered buttons while complete titles remain in disclosure', () => {
  const title = '代码发布流程\n一段很长的真实形状标题说明'.repeat(12);
  const html = render(`先测试。[来源: ${title}] 再发布。[来源: ${title}]`, [sourceItem('1001', title)]);
  assert.equal((html.match(/class="answer-citation"/g) || []).length, 2);
  assert.match(html, /aria-label="查看来源 1"/);
  assert.match(html, /\[1\]<\/button>/);
  assert.equal((html.match(/class="source-full-title"/g) || []).length, 1);
  assert.ok(html.includes(title));
  assert.match(html, /<details class="answer-source-detail"/);
  assert.match(html, /打开原视频/);
});

test('only trusted citation nodes become buttons, never forged model fragment links', () => {
  const html = render('依据。[来源: 科普]\n\n[伪造](#answer-citation-1)', [sourceItem('1001', '科普')]);
  assert.equal((html.match(/class="answer-citation"/g) || []).length, 1);
  assert.match(html, /<a href="#answer-citation-1"/);
});

test('unknown, ambiguous, streamed, quoted and code markers keep their literal text', () => {
  const title = '同名';
  for (const content of ['[来源: 未知]', '[来源: 资料1]', '`[来源: 同名]`', '“[来源: 同名]”', '> [来源: 同名]', '\\[来源: 同名]']) {
    assert.doesNotMatch(render(content, [sourceItem('1001', title)]), /class="answer-citation"/);
  }
  assert.doesNotMatch(render('[来源: 同名]', [sourceItem('1001', title), sourceItem('1002', title)]), /class="answer-citation"/);
  assert.doesNotMatch(render('[来源: 同名]', []), /class="answer-citation"/);
});

test('only source-ID-bound provided full text is shown, with no invented history excerpt', () => {
  const sources = [sourceItem('1001', '科普')];
  const trace = { route: 'vector', steps: [], chunks: [
    { chunk_id: '1001:1', title: '科普', text: '短预览', full_text: '[00:02–00:04] 实际提供的原句。', score: .8 },
    { chunk_id: '1002:1', title: '科普', text: '错误视频', full_text: '不可展示的同名视频段落', score: .8 },
    { chunk_id: '1001:2', title: '被替换的标题', text: '标题冲突', full_text: '不可展示的标题冲突段落', score: .8 },
  ] };
  const html = render('结论。[来源: 科普]', sources, trace);
  assert.match(html, /实际提供的原句/);
  assert.doesNotMatch(html, /不可展示/);
  assert.match(render('历史结论。[来源: 科普]', sources), /未保存可定位的原文片段/);
});

test('malformed source URLs never become external source links', () => {
  const html = render('结论。[来源: 科普]', [{ ...sourceItem('1001', '科普'), url: 'javascript:alert(1)' }]);
  assert.doesNotMatch(html, /javascript:|href="javascript/);
  assert.match(html, /原视频链接暂不可用/);
});
