import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';
import { localModuleURL } from './load-local-ts.mjs';

const { collectionConnection } = await import(await localModuleURL(new URL('../src/collectionConnection.ts', import.meta.url)));
const ready = { status: 'logged_in', phase: 'ready', snapshot_ready: true, busy: false, message: '读取完成' };
const scraping = { status: 'pending', phase: 'scraping', snapshot_ready: false, busy: true, message: '正在读取收藏夹' };

test('a snapshot-ready terminal state is required by the new contract', () => {
  assert.equal(collectionConnection(ready).ready, true);
  for (const status of [
    { ...ready, snapshot_ready: false }, { ...ready, snapshot_ready: undefined },
    { ...ready, phase: 'scraping' }, { ...ready, phase: 'failed' },
    { ...ready, status: 'pending' }, { ...ready, snapshot_ready: 1 },
  ]) assert.equal(collectionConnection(status).ready, false);
  assert.equal(collectionConnection({ ...scraping, status: 'logged_in' }).phase, 'scraping');
});

test('legacy successful, pending and known failed collection messages stay distinguishable', () => {
  assert.equal(collectionConnection({ status: 'logged_in', message: '已同步7个收藏夹' }).ready, true);
  assert.equal(collectionConnection({ status: 'logged_in', message: '登录成功，正在同步收藏夹…' }).phase, 'scraping');
  assert.equal(collectionConnection({ status: 'logged_in', message: '登录成功，但抓取失败: module_not_found' }).phase, 'failed');
  assert.equal(collectionConnection({ status: 'failed', message: '登录超时' }).ready, false);
});

const asURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const reactURL = asURL(`
export function useState(initial) {
  const h=globalThis.__collectionHarness, index=h.cursor++;
  if (!h.hooks[index]) h.hooks[index]={value:typeof initial==='function'?initial():initial};
  return [h.hooks[index].value, next=>{
    const value=typeof next==='function'?next(h.hooks[index].value):next;
    if (!Object.is(value,h.hooks[index].value)) {h.hooks[index].value=value;h.dirty=true;}
  }];
}
export function useRef(initial) {
  const h=globalThis.__collectionHarness,index=h.cursor++;
  return h.hooks[index]??(h.hooks[index]={current:initial});
}
export function useEffect(callback,deps) {
  const h=globalThis.__collectionHarness,index=h.cursor++,previous=h.effects.get(index);
  if (!previous || deps.length!==previous.deps.length || deps.some((value,i)=>!Object.is(value,previous.deps[i])))
    h.pending.push({index,callback,deps});
}
export const useCallback=callback=>callback;
export const useId=()=> 'fixture-connection-title';
`);
const jsxURL = asURL('export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;export const Fragment="fragment";');
const apiURL = asURL(`
export async function loginStatus(){const h=globalThis.__collectionHarness;h.calls.status++;return h.status();}
export async function loginStart(){const h=globalThis.__collectionHarness;h.calls.start++;return h.start();}
export async function logout(){const h=globalThis.__collectionHarness;h.calls.logout++;return h.logout();}
`);
const connectionURL = await localModuleURL(new URL('../src/collectionConnection.ts', import.meta.url));
const stubURL = name => asURL(`export default function ${name}(){return null;}`);
async function component(path, imports) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8');
  let { outputText } = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX,
  }});
  for (const [name,url] of Object.entries(imports)) outputText=outputText.replaceAll(`'${name}'`,`'${url}'`).replaceAll(`"${name}"`,`"${url}"`);
  return (await import(asURL(outputText))).default;
}
const App = await component('../src/App.tsx', {
  react:reactURL,'react/jsx-runtime':jsxURL,'./api':apiURL,'./collectionConnection':connectionURL,
  './pages/LandingPage':stubURL('LandingPage'),'./pages/Workspace':stubURL('Workspace'),
  './components/LoginModal':stubURL('LoginModal'),
});
const iconsURL=asURL('export const CircleAlert=()=>null,CircleCheck=()=>null,LoaderCircle=()=>null,ScanLine=()=>null;');
const LoginModal = await component('../src/components/LoginModal.tsx', {
  react:reactURL,'react/jsx-runtime':jsxURL,'lucide-react':iconsURL,
});

function nodes(tree,predicate) {
  if (tree==null || typeof tree!=='object') return [];
  if (Array.isArray(tree)) return tree.flatMap(node=>nodes(node,predicate));
  return [...(predicate(tree)?[tree]:[]),...nodes(tree.props?.children,predicate)];
}
function text(tree) {
  if (tree==null || typeof tree==='boolean') return '';
  if (typeof tree==='string'||typeof tree==='number') return String(tree);
  return Array.isArray(tree)?tree.map(text).join(''):text(tree.props?.children);
}
function deferred() {
  let resolve;
  const promise=new Promise(complete=>{resolve=complete;});
  return {promise,resolve};
}
function fixture() {
  const originalSet=globalThis.setInterval, originalClear=globalThis.clearInterval;
  const h={cursor:0,hooks:[],effects:new Map(),pending:[],dirty:true,tree:null,timers:new Map(),nextTimer:1,
    calls:{status:0,start:0,logout:0},status:()=>({status:'idle',message:''}),
    logout:()=>({success:true,message:'退出完成'})};
  h.start=()=>{
    const started={success:true,status:'pending',phase:'logging_in',snapshot_ready:false,busy:true,message:'扫码登录'};
    h.status=()=>started;
    return started;
  };
  globalThis.__collectionHarness=h;
  globalThis.setInterval=callback=>{const id=h.nextTimer++;h.timers.set(id,callback);return id;};
  globalThis.clearInterval=id=>h.timers.delete(id);
  h.render=()=>{
    h.cursor=0;h.pending=[];h.dirty=false;h.tree=App();
    for(const effect of h.pending){h.effects.get(effect.index)?.cleanup?.();h.effects.set(effect.index,{...effect,cleanup:effect.callback()});}
  };
  h.flush=async()=>{
    for(let index=0;index<16;index++){await Promise.resolve();if(h.dirty)h.render();}
  };
  h.find=name=>nodes(h.tree,node=>node.type?.name===name)[0];
  h.poll=async()=>{for(const callback of [...h.timers.values()])callback();await h.flush();};
  h.close=()=>{for(const effect of h.effects.values())effect.cleanup?.();globalThis.setInterval=originalSet;globalThis.clearInterval=originalClear;};
  return h;
}

test('a cookie and scraping phase keep the connection modal open until snapshot readiness', async () => {
  const h=fixture();
  try {
    h.render();await h.flush();await h.find('LandingPage').props.onStartLogin();await h.flush();
    h.status=()=>({...scraping,status:'logged_in'});await h.poll();
    assert.ok(h.find('LoginModal'));assert.equal(h.find('Workspace'),undefined);
    assert.equal(h.find('LoginModal').props.connection.phase,'scraping');
    h.status=()=>ready;await h.poll();
    assert.equal(h.find('LoginModal'),undefined);assert.equal(h.find('Workspace').props.loggedIn,true);
    assert.equal(h.calls.start,1);
  } finally {h.close();}
});

test('failed capture retries through loginStart and never uses logout as a recovery step', async () => {
  const h=fixture();
  try {
    h.render();await h.flush();await h.find('LandingPage').props.onStartLogin();await h.flush();
    h.status=()=>({status:'failed',phase:'failed',snapshot_ready:false,busy:false,message:'收藏模块尚未加载，请重试读取收藏。'});await h.poll();
    assert.equal(h.find('Workspace'),undefined);
    assert.equal(h.find('LoginModal').props.connection.phase,'failed');
    await h.find('LoginModal').props.onRetry();await h.flush();
    assert.equal(h.calls.start,2);assert.equal(h.calls.logout,0);
    assert.equal(h.find('LoginModal').props.connection.phase,'logging_in');
  } finally {h.close();}
});

test('overlapping start clicks and a stale ready response cannot complete a new capture', async () => {
  const h=fixture(),oldStatus=deferred(),start=deferred();
  try {
    h.status=()=>oldStatus.promise;h.start=()=>start.promise;h.render();
    const click=h.find('LandingPage').props.onStartLogin;
    const pending=click();await click();await h.flush();assert.equal(h.calls.start,1);
    oldStatus.resolve(ready);await h.flush();assert.equal(h.find('Workspace'),undefined);
    start.resolve({success:true,...scraping});await pending;await h.flush();
    assert.ok(h.find('LoginModal'));assert.equal(h.find('Workspace'),undefined);
    h.status=()=>ready;await h.poll();assert.equal(h.find('Workspace').props.loggedIn,true);
  } finally {h.close();}
});

test('closing progress has no late modal-success timer and only one polling owner remains', async () => {
  const h=fixture();
  try {
    h.render();await h.flush();await h.find('LandingPage').props.onStartLogin();await h.flush();
    h.find('LoginModal').props.onClose();await h.flush();
    assert.equal(h.find('LoginModal'),undefined);assert.equal(h.timers.size,1);
    await h.find('LandingPage').props.onStartLogin();await h.flush();
    assert.ok(h.find('LoginModal'));assert.equal(h.calls.start,1);
  } finally {h.close();}
});

test('StrictMode effect replay discards the first polling owner and its late ready response', async () => {
  const h=fixture(),oldStatus=deferred();
  try {
    h.status=()=>h.calls.status===1?oldStatus.promise:scraping;
    h.render();
    for(const effect of h.effects.values()){effect.cleanup?.();effect.cleanup=effect.callback();}
    await h.flush();oldStatus.resolve(ready);await h.flush();
    assert.equal(h.find('Workspace'),undefined);assert.equal(h.timers.size,1);
    assert.equal(h.find('LandingPage')!==undefined,true);
  } finally {h.close();}
});

test('unmounting invalidates an outstanding status request before it can update application state', async () => {
  const h=fixture(),pending=deferred();
  try {
    h.status=()=>pending.promise;h.render();h.close();
    const states=h.hooks.map(value=>value.value);
    pending.resolve(ready);await h.flush();
    assert.deepEqual(h.hooks.map(value=>value.value),states);
    assert.equal(h.find('Workspace'),undefined);assert.equal(h.timers.size,0);
  } finally {h.close();}
});

test('a confirmed capture preserves an already selected AI workspace', async () => {
  const h=fixture();
  try {
    h.render();await h.flush();h.find('LandingPage').props.onTryChat();await h.flush();
    await h.find('Workspace').props.onConnectFavorites();await h.flush();
    h.status=()=>ready;await h.poll();
    assert.equal(h.find('Workspace').props.initialChatOnly,true);
    assert.equal(h.find('Workspace').props.loggedIn,true);
  } finally {h.close();}
});

test('returning home during capture retains the active attempt for progress reopening', async () => {
  const h=fixture();
  try {
    h.render();await h.flush();h.find('LandingPage').props.onOpenLocal();await h.flush();
    await h.find('Workspace').props.onConnectFavorites();await h.flush();
    h.find('LoginModal').props.onClose();await h.flush();
    await h.find('Workspace').props.onLogout();await h.flush();
    assert.ok(h.find('LandingPage'));await h.find('LandingPage').props.onStartLogin();await h.flush();
    assert.ok(h.find('LoginModal'));assert.equal(h.calls.start,1);assert.equal(h.calls.logout,0);
  } finally {h.close();}
});

test('a rejected logout remains in the connected workspace with its actionable message', async () => {
  const h=fixture();
  try {
    h.status=()=>ready;h.logout=()=>({success:false,message:'浏览器仍在使用，请稍后重试。'});
    h.render();await h.flush();await h.find('Workspace').props.onLogout();await h.flush();
    assert.equal(h.find('Workspace').props.loggedIn,true);
    assert.match(h.find('Workspace').props.loginError,/浏览器仍在使用/);
  } finally {h.close();}
});

test('failed native-dialog state has explicit retry, manual sync wording and no premature success callback', () => {
  const h=fixture();
  try {
    let retries=0,closed=0;h.cursor=0;
    const tree=LoginModal({connection:collectionConnection({status:'failed',phase:'failed',snapshot_ready:false,busy:false,message:'读取失败'}),onRetry(){retries++;},onClose(){closed++;}});
    assert.equal(tree.type,'dialog');assert.match(text(tree),/收藏读取未完成/);assert.match(text(tree),/点击“同步收藏夹”/);
    const retry=nodes(tree,node=>node.type==='button'&&text(node)==='重试读取收藏')[0];retry.props.onClick();assert.equal(retries,1);
    let prevented=false;tree.props.onCancel({preventDefault(){prevented=true;}});assert.equal(prevented,true);assert.equal(closed,1);
  } finally {h.close();}
});
