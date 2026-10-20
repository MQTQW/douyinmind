import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';
const source = await readFile(new URL('../src/selectPosition.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext } });
const { selectPosition } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

test('a knowledge picker near the bottom opens above without losing field width', () => {
  const result=selectPosition({left:30,top:480,bottom:540,width:260},{width:320,height:568},312);
  assert.equal(result.width,260);
  assert.ok(result.top+result.maxHeight<480);
  assert.ok(result.left>=12 && result.left+result.width<=308);
});
test('short windows constrain the list instead of clipping its lower options', () => {
  for(const height of [320,480,650]){
    const result=selectPosition({left:30,top:120,bottom:180,width:260},{width:320,height},312);
    assert.ok(result.top>=12);
    assert.ok(result.top+result.maxHeight<=height-12);
  }
});
