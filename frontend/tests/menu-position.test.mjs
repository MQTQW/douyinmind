import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';
const source = await readFile(new URL('../src/menuPosition.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext } });
const { menuPosition } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

test('a menu near the bottom of the observed short window opens upward within the viewport', () => {
  const anchor={left:590,right:620,top:448,bottom:478},menu={width:212,height:203},viewport={width:649,height:604};
  const result=menuPosition(anchor,menu,viewport);
  assert.ok(result.top+menu.height<=anchor.top);
  assert.ok(result.top>=12 && result.left>=12);
  assert.ok(result.left+menu.width<=viewport.width-12);
});
test('menus with room below stay below; left-edge and narrow-window positions remain reachable', () => {
  assert.deepEqual(menuPosition({left:200,right:230,top:60,bottom:90},{width:212,height:150},{width:649,height:604}),{left:18,top:95});
  const result=menuPosition({left:0,right:30,top:70,bottom:100},{width:212,height:200},{width:320,height:568});
  assert.equal(result.left,12);
  assert.ok(result.top+200<=568-12);
});
