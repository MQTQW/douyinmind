import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const source = await readFile(new URL('../src/components/MessageContent.tsx', import.meta.url), 'utf8');
let { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX } });
outputText = outputText.replace(/from (['"])([^'"]+)\1/g, (_match, _quote, name) => `from ${JSON.stringify(import.meta.resolve(name))}`);
const { default: MessageContent } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const render = content => renderToStaticMarkup(createElement(MessageContent, { content }));

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
