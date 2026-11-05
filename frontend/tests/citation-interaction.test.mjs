import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import { localModuleURL } from './load-local-ts.mjs';

const asURL = value => `data:text/javascript;base64,${Buffer.from(value).toString('base64')}`;
const hooks = asURL(`export const useMemo=fn=>fn();export const useRef=initial=>({current:initial});`);
let { outputText } = ts.transpileModule(await readFile(new URL('../src/components/MessageContent.tsx', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
});
for (const match of [...outputText.matchAll(/from (['"])([^'"]+)\1/g)]) {
  const name = match[2];
  const url = name === 'react' ? hooks : name.startsWith('.')
    ? await localModuleURL(new URL(name === './CitationSources' ? '../src/components/CitationSources.tsx' : '../src/citations.ts', import.meta.url))
    : import.meta.resolve(name);
  outputText = outputText.replaceAll(match[0], `from ${JSON.stringify(url)}`);
}
const { default: MessageContent } = await import(asURL(outputText));
const source = { platform_item_id: '1001', title: '真实形状的合成资料', url: 'https://www.douyin.com/video/1001', score: .8 };
const children = element => Array.isArray(element.props.children) ? element.props.children : [element.props.children];

test('real citation handlers open/focus the matching source and Escape restores the invoking button', () => {
  const tree = MessageContent({ content: '结论[来源: 真实形状的合成资料]', sources: [source] });
  const [content, sources] = children(tree);
  const anchor = content.props.children.props.components.a;
  const calls = [];
  const summary = { focus: options => calls.push(['summary-focus', options]) };
  const panel = { open: false, querySelector: name => { assert.equal(name, 'summary'); return summary; }, scrollIntoView: options => calls.push(['scroll', options]) };
  const trigger = { isConnected: true, focus: () => calls.push(['trigger-focus']) };
  sources.props.register(1, panel);
  const button = anchor({ node: { properties: { 'data-citation-number': 1 } }, href: '#answer-citation-1' });
  assert.equal(button.type, 'button');
  assert.equal(button.props.type, 'button');
  button.props.onClick({ currentTarget: trigger });
  assert.equal(panel.open, true);
  assert.deepEqual(calls, [['summary-focus', { preventScroll: true }], ['scroll', { block: 'nearest', behavior: 'auto' }]]);
  sources.props.onEscape(1);
  assert.equal(panel.open, false);
  assert.deepEqual(calls.at(-1), ['trigger-focus']);
  sources.props.register(1, null);
  button.props.onClick({ currentTarget: trigger });
  assert.equal(calls.length, 3, 'unmounted source cannot be opened or guessed');
});

test('a forged fragment does not become a citation, and a detached trigger falls back to summary focus', () => {
  const tree = MessageContent({ content: '结论', sources: [source] });
  const [content, sources] = children(tree); const anchor = content.props.children.props.components.a;
  assert.equal(anchor({ href: '#answer-citation-1', children: '伪造' }).type, 'a');
  assert.equal(anchor({ node: { properties: { 'data-citation-number': 5 } }, href: '#answer-citation-5' }).type, 'a');
  let focused = 0;
  const panel = { open: true, querySelector: () => ({ focus: () => focused++ }), scrollIntoView() {} };
  sources.props.register(1, panel);
  const button = anchor({ node: { properties: { 'data-citation-number': 1 } }, href: '#answer-citation-1' });
  button.props.onClick({ currentTarget: { isConnected: false } });
  sources.props.onEscape(1);
  assert.equal(panel.open, false);
  assert.equal(focused, 2);
});

test('Escape on a different manually opened source never jumps back to an unrelated citation', () => {
  const second = { ...source, platform_item_id: '1002', title: '另一资料', url: 'https://www.douyin.com/video/1002' };
  const tree = MessageContent({ content: '结论', sources: [source, second] });
  const [content, sources] = children(tree); const anchor = content.props.children.props.components.a;
  const calls = [];
  const panel = number => ({ open: true, querySelector: () => ({ focus: () => calls.push(`summary${number}`) }), scrollIntoView() {} });
  sources.props.register(1, panel(1)); sources.props.register(2, panel(2));
  anchor({ node: { properties: { 'data-citation-number': 1 } }, href: '#answer-citation-1' }).props.onClick({ currentTarget: { isConnected: true, focus: () => calls.push('wrong-trigger') } });
  sources.props.onEscape(2);
  assert.deepEqual(calls, ['summary1', 'summary2']);
});
