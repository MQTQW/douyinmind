import assert from 'node:assert/strict';
import test from 'node:test';
import { localModuleURL } from './load-local-ts.mjs';

const citations = await import(await localModuleURL(new URL('../src/citations.ts', import.meta.url)));
const clipboard = await import(await localModuleURL(new URL('../src/clipboard.ts', import.meta.url)));
const item = (id, title = `资料标题${id}`) => ({ platform_item_id: id, title, url: `https://www.douyin.com/video/${id}`, score: .8 });
const references = citations.citationReferences([item('1001', '原理'), item('1002', '步骤')]);
const compact = value => citations.compactCitationText(value, references);

test('message numbering uses unique source IDs and one unambiguous full title', () => {
  assert.equal(compact('事实[来源: 原理]，操作[来源：步骤]。'), '事实[1]，操作[2]。');
  assert.equal(compact('[来源: 资料1]'), '[来源: 资料1]');
  assert.equal(compact('[来源: 未返回的材料]'), '[来源: 未返回的材料]');
  assert.equal(citations.citationReferences([item('1001', '原理'), item('1001', '原理')]).length, 1);
  assert.equal(citations.citationReferences([item('1001', '原理'), item('1001', '另一标题')]).length, 0);
  const duplicate = citations.citationReferences([item('1001', '同名'), item('1002', '同名')]);
  assert.equal(citations.resolveCitation('同名', duplicate), undefined);
});

for (const content of [
  '`[来源: 原理]`', '``内容`[来源: 原理]``', '```text\n[来源: 原理]\n```',
  '~~~\n[来源: 原理]\n~~~', '    [来源: 原理]', '> [来源: 原理]',
  '\\[来源: 原理]', '“[来源: 原理]”', '「[来源: 原理]」', '"[来源: 原理]"',
  "'[来源: 原理]'", '[文字 [来源: 原理]](https://example.invalid)',
  '[文字 [来源: 原理]][ref]', '<script>[来源: 原理]</script>',
  '[ref]: https://example.invalid/[来源: 原理]',
  '[来源: 原理]: https://example.invalid/reference',
  '<https://example.invalid/[来源:原理]>', 'https://example.invalid/[来源:原理]',
  'www.example.invalid/[来源:原理]',
]) test(`literal Markdown and quotations are preserved: ${JSON.stringify(content)}`, () => {
  assert.equal(compact(content), content);
});

test('protected citations do not consume a later real citation or change surrounding Markdown', () => {
  assert.equal(compact('“[来源: 原理]” **结论**[来源: 原理]'), '“[来源: 原理]” **结论**[1]');
  assert.equal(compact('```\n[来源: 原理]\n```\n\n结论[来源: 原理]'), '```\n[来源: 原理]\n```\n\n结论[1]');
  assert.equal(compact("it's a fact [来源: 原理]"), "it's a fact [1]");
});

test('safe source URLs must identify that exact video on the known video site', () => {
  assert.equal(citations.safeSourceUrl(item('1001')), 'https://www.douyin.com/video/1001');
  for (const url of ['javascript:alert(1)', 'https://evil.invalid/video/1001', 'https://www.douyin.com/video/1002',
    'https://name:secret@www.douyin.com/video/1001', 'http://www.douyin.com/video/1001', 'https://www.douyin.com:444/video/1001']) {
    assert.equal(citations.safeSourceUrl({ ...item('1001'), url }), undefined);
  }
});

test('provided original text requires exact source ID prefix, title and full text', () => {
  const ref = references[0];
  const chunks = [
    { chunk_id: '1001:0', title: '原理', full_text: '实际原文' },
    { chunk_id: '10010:0', title: '原理', full_text: '相似ID误匹配' },
    { chunk_id: '1001:1', title: '另一标题', full_text: '标题误匹配' },
    { chunk_id: '1001:2', title: '原理', text: '只有短预览' },
    { chunk_id: '1001:0', title: '原理', full_text: '重复ID' },
  ];
  assert.deepEqual(citations.providedSourceChunks(ref, { chunks }), [{ chunk_id: '1001:0', text: '实际原文' }]);
});

test('copy has compact body and a single full source list without changing code or losing source-less text', () => {
  const content = '结论[来源: 原理]。再次引用[来源: 原理]\n\n`[来源: 原理]`';
  const result = clipboard.answerClipboardText(content, [item('1001', '原理')]);
  assert.equal(result, '结论[1]。再次引用[1]\n\n`[来源: 原理]`\n\n参考来源：\n[1] 原理\n  https://www.douyin.com/video/1001');
  assert.equal(clipboard.answerClipboardText(content), content);
});
