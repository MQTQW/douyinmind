import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const asURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const source = await readFile(new URL('../src/components/ExecutionTrace.tsx', import.meta.url), 'utf8');
async function loadComponent(open) {
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
  });
  // Use real React markup rendering; only force the component's existing
  // expanded state for tests that inspect its otherwise collapsed body.
  const reactURL = open
    ? asURL('export const useState = () => [true, () => {}];')
    : import.meta.resolve('react');
  for (const [name, url] of [['react', reactURL], ['react/jsx-runtime', import.meta.resolve('react/jsx-runtime')]]) {
    outputText = outputText.replaceAll(`'${name}'`, `'${url}'`).replaceAll(`"${name}"`, `"${url}"`);
  }
  return (await import(asURL(outputText))).default;
}
const ClosedTrace = await loadComponent(false);
const OpenTrace = await loadComponent(true);
const chunk = (score, score_kind) => ({ chunk_id: 'video:1', title: '原视频', text: '有出处的原文片段', score, ...(score_kind ? { score_kind } : {}) });
const trace = overrides => ({ route: 'vector', steps: [{ name: '查找原文', time_ms: 15 }], chunks: [chunk(.78)], ...overrides });
const render = (value, open = true) => renderToStaticMarkup(createElement(open ? OpenTrace : ClosedTrace, { trace: value, latency_ms: 120 }));

test('old trace data remains compatible in its collapsed and expanded views', () => {
  const old = trace({});
  const closed = render(old, false);
  assert.match(closed, /语义检索/);
  assert.doesNotMatch(closed, /语义服务暂时不可用|有出处的原文片段/);
  const expanded = render(old);
  assert.match(expanded, /78%/);
  assert.match(expanded, /有出处的原文片段/);
  assert.match(expanded, /15ms/);
  assert.match(expanded, /1 段资料/);
  assert.doesNotMatch(expanded, /个召回|召回片段|查看本次提供的原文|<details/);
});

test('hybrid search is labelled as combined search and distinguishes lexical-only chunks', () => {
  const html = render(trace({ retrieval_mode: 'hybrid', chunks: [chunk(0, 'lexical'), chunk(.72, 'semantic')] }));
  assert.match(html, /组合查找/);
  assert.match(html, /关键词匹配/);
  assert.match(html, /72%/);
  assert.doesNotMatch(html, /(?:^|>)0%|语义服务暂时不可用/);
});

test('successful fallback is explained normally without telling the user to retry', () => {
  const html = render(trace({ retrieval_mode: 'lexical_fallback', retrieval_fallback_reason: 'embedding_timeout', chunks: [chunk(0, 'lexical')] }));
  assert.match(html, /关键词查找（后备）/);
  assert.match(html, /语义服务暂时不可用，本次使用关键词查找/);
  assert.match(html, /role="status"/);
  assert.match(html, /关键词匹配/);
  assert.doesNotMatch(html, /role="alert"|text-red|重试|0%|embedding_timeout/);
  // The explanation remains visible even when the trace body is collapsed.
  assert.match(render(trace({ retrieval_mode: 'lexical_fallback' }), false), /本次使用关键词查找/);
});

test('fallback metadata never presents a legacy zero score as semantic similarity', () => {
  const html = render(trace({ retrieval_mode: 'lexical_fallback', chunks: [chunk(0)] }));
  assert.match(html, /关键词匹配/);
  assert.doesNotMatch(html, /0%/);
});

test('a lexical-only chunk is labelled truthfully even without a mode field', () => {
  const html = render(trace({ chunks: [chunk(.91, 'lexical')] }));
  assert.match(html, /关键词匹配/);
  assert.doesNotMatch(html, /91%/);
});

test('unknown or absent scores are omitted rather than rendered as zero or NaN', () => {
  for (const score of [null, undefined, Number.NaN]) {
    const html = render(trace({ retrieval_mode: 'semantic', chunks: [chunk(score)] }));
    assert.match(html, /语义检索/);
    assert.doesNotMatch(html, /NaN%|0%/);
  }
});

test('direct and legacy routes keep their original label and untrusted fallback details stay hidden', () => {
  assert.match(render(trace({ route: 'direct', retrieval_mode: 'none', chunks: [] })), /直接回复/);
  assert.match(render(trace({ route: 'db_list', retrieval_mode: 'none', chunks: [] })), /列表查询/);
  const html = render(trace({ retrieval_mode: 'lexical_fallback', retrieval_fallback_reason: 'PRIVATE_PROVIDER_DETAIL' }));
  assert.doesNotMatch(html, /PRIVATE_PROVIDER_DETAIL/);
  assert.match(html, /关键词查找（后备）/);
});

test('retrieval detail is optional, shows zero honestly and does not double-count nested time', () => {
  const value = trace({
    retrieval_mode: 'lexical_fallback',
    retrieval_details: { stages_ms: { query_embedding: 5000, keyword_fusion: 0 }, counts: { dense_candidates: 0, scope_records: 27 }, scope_video_count: 4 },
    context_stats: { provided_chunks: 3, provided_videos: 2, provided_chars: 1900, max_chars: 8000 },
  });
  const html = render(value);
  assert.match(html, /把问题转换成检索用的数字/);
  assert.match(html, /5000ms/);
  assert.match(html, /按关键词匹配并排序/);
  assert.match(html, /按含义找到的候选：0 段/);
  assert.match(html, /当前范围可供匹配的原文：27 段/);
  assert.match(html, /3 段，共 1900 字符/);
  assert.match(html, /已计入上方查找阶段/);
  assert.doesNotMatch(html, / = 120ms|合并含义与关键词匹配结果/);
  assert.doesNotMatch(render(value, false), /查找过程明细|5000ms|1900 字符/);
});

test('invalid detail values and unknown fields are never displayed', () => {
  const html = render(trace({
    retrieval_details: { stages_ms: { query_embedding: Number.NaN, vector_search: -1, PRIVATE_DETAIL: 9999 }, counts: { dense_candidates: Infinity, scope_records: -5 }, scope_video_count: null },
    context_stats: { provided_chunks: 1, provided_chars: -7 },
  }));
  assert.doesNotMatch(html, /NaNms|Infinity|9999|PRIVATE_DETAIL|-1ms|-5 段|-7 字符/);
});

test('full provided evidence is available in native details with selectable preserved lines', () => {
  const full = `${'同一段原文的背景说明。'.repeat(30)}\n不得带电拆卸；重启前必须等待17秒。`;
  const html = render(trace({ chunks: [{ ...chunk(.78), text: full.slice(0, 200), full_text: full }] }));
  assert.match(html, /提供给 AI 的原文/);
  assert.match(html, /不代表全部被回答引用/);
  assert.match(html, /<details[^>]*>/);
  assert.match(html, /<summary[^>]*>查看本次提供的原文<\/summary>/);
  assert.match(html, /不得带电拆卸；重启前必须等待17秒。/);
  assert.match(html, /max-h-64 overflow-y-auto whitespace-pre-wrap break-words select-text/);
  assert.doesNotMatch(html, /<details[^>]* open/);
  assert.doesNotMatch(render(trace({ chunks: [{ ...chunk(.78), full_text: full }] }), false), /查看本次提供的原文|17秒/);
});

test('empty or malformed full text does not pretend a legacy preview is complete', () => {
  for (const full_text of [undefined, '', null, 123]) {
    const html = render(trace({ chunks: [{ ...chunk(.78), full_text }] }));
    assert.match(html, /有出处的原文片段/);
    assert.doesNotMatch(html, /<details|查看本次提供的原文/);
  }
});

test('provided source text is displayed literally, not executed as HTML', () => {
  const html = render(trace({ chunks: [{ ...chunk(.78), full_text: '<script>PRIVATE_LITERAL</script>\n[后文未提供]' }] }));
  assert.match(html, /&lt;script&gt;PRIVATE_LITERAL&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /\[后文未提供\]/);
});
