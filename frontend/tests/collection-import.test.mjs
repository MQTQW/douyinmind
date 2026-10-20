import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const source = await readFile(new URL('../src/collectionImport.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext },
});
const { collectionLibraryName, isWholeCollectionSelected } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
);

const collection = { collection_id: 'folder-a', title: ' 编程学习 ', video_ids: ['a', 'b', 'c'] };

test('only the complete collection qualifies, independent of selection order', () => {
  assert.equal(isWholeCollectionSelected(collection, ['c', 'a', 'b']), true);
  assert.equal(isWholeCollectionSelected(collection, new Set(['b', 'c', 'a'])), true);
  assert.equal(isWholeCollectionSelected(collection, ['a', 'b']), false);
  assert.equal(isWholeCollectionSelected(collection, []), false);
});

test('cross-collection additions and replacements do not inherit the collection name', () => {
  assert.equal(isWholeCollectionSelected(collection, ['a', 'b', 'c', 'other-folder-video']), false);
  assert.equal(isWholeCollectionSelected(collection, ['a', 'b', 'other-folder-video']), false);
});

test('duplicate source and selected IDs represent the same complete set', () => {
  assert.equal(isWholeCollectionSelected({ ...collection, video_ids: ['a', 'a', 'b'] }, ['b', 'a', 'b']), true);
  assert.equal(isWholeCollectionSelected({ ...collection, video_ids: ['a', 'a', 'b'] }, ['a', 'a']), false);
});

test('missing, empty and unnamed collections cannot create an automatic library', () => {
  assert.equal(isWholeCollectionSelected(undefined, ['a']), false);
  assert.equal(isWholeCollectionSelected({ ...collection, video_ids: [] }, []), false);
  assert.equal(isWholeCollectionSelected({ ...collection, title: ' \t\n ' }, collection.video_ids), false);
  assert.equal(collectionLibraryName(undefined), '');
  assert.equal(collectionLibraryName({ ...collection, title: ' \t\n ' }), '');
});

test('automatic names trim surrounding whitespace while preserving internal text', () => {
  assert.equal(collectionLibraryName(collection), '编程学习');
  assert.equal(collectionLibraryName({ ...collection, title: '  AI 视频 🔖 学习  ' }), 'AI 视频 🔖 学习');
});

test('the 128-character limit counts Chinese and emoji without splitting surrogate pairs', () => {
  const title = `${'知'.repeat(127)}🔖尾部`;
  const result = collectionLibraryName({ ...collection, title: `  ${title}  ` });
  assert.equal(result, `${'知'.repeat(127)}🔖`);
  assert.equal(Array.from(result).length, 128);
  assert.equal(collectionLibraryName({ ...collection, title: '🔖'.repeat(129) }), '🔖'.repeat(128));
});
