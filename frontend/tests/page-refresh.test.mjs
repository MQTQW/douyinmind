import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const source = await readFile(new URL('../src/pageRefresh.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext },
});
const { subscribePageReturn } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

function surfaces(visibilityState = 'visible') {
  const page = new EventTarget();
  const visibilityDocument = new EventTarget();
  visibilityDocument.visibilityState = visibilityState;
  return { page, visibilityDocument };
}

test('returning to a visible page refreshes an old count from the latest data', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const { page, visibilityDocument } = surfaces();
  let shownCount = 12;
  const dispose = subscribePageReturn(() => { shownCount = 14; }, page, visibilityDocument);
  page.dispatchEvent(new Event('focus'));
  assert.equal(shownCount, 12);
  t.mock.timers.tick(150);
  assert.equal(shownCount, 14);
  dispose();
});

test('hidden pages neither refresh nor retain a pending visible refresh', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const { page, visibilityDocument } = surfaces('hidden');
  let refreshes = 0;
  const dispose = subscribePageReturn(() => { refreshes++; }, page, visibilityDocument);
  page.dispatchEvent(new Event('focus'));
  visibilityDocument.dispatchEvent(new Event('visibilitychange'));
  t.mock.timers.tick(200);
  assert.equal(refreshes, 0);

  visibilityDocument.visibilityState = 'visible';
  visibilityDocument.dispatchEvent(new Event('visibilitychange'));
  t.mock.timers.tick(100);
  visibilityDocument.visibilityState = 'hidden';
  visibilityDocument.dispatchEvent(new Event('visibilitychange'));
  t.mock.timers.tick(200);
  assert.equal(refreshes, 0);
  dispose();
});

test('visibility and focus events are merged into one refresh after 150 ms', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const { page, visibilityDocument } = surfaces();
  let refreshes = 0;
  const dispose = subscribePageReturn(() => { refreshes++; }, page, visibilityDocument);
  visibilityDocument.dispatchEvent(new Event('visibilitychange'));
  t.mock.timers.tick(75);
  page.dispatchEvent(new Event('focus'));
  t.mock.timers.tick(149);
  assert.equal(refreshes, 0);
  t.mock.timers.tick(1);
  assert.equal(refreshes, 1);
  t.mock.timers.tick(500);
  assert.equal(refreshes, 1);
  dispose();
});

test('disposing removes listeners and cancels pending updates', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const { page, visibilityDocument } = surfaces();
  let refreshes = 0;
  const dispose = subscribePageReturn(() => { refreshes++; }, page, visibilityDocument);
  page.dispatchEvent(new Event('focus'));
  dispose();
  t.mock.timers.tick(200);
  page.dispatchEvent(new Event('focus'));
  visibilityDocument.dispatchEvent(new Event('visibilitychange'));
  t.mock.timers.tick(200);
  assert.equal(refreshes, 0);
});
