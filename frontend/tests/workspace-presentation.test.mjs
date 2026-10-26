import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const asURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
function tagHooks(context) {
  const visit = node => {
    if (ts.isVariableDeclaration(node) && ts.isArrayBindingPattern(node.name)
      && ts.isCallExpression(node.initializer) && ts.isIdentifier(node.initializer.expression)
      && node.initializer.expression.text === 'useState') {
      const label = node.name.elements[0].name.text;
      return ts.factory.updateVariableDeclaration(node, node.name, node.exclamationToken, node.type,
        ts.factory.updateCallExpression(node.initializer, node.initializer.expression, node.initializer.typeArguments,
          [...(node.initializer.arguments.length ? node.initializer.arguments : [ts.factory.createVoidZero()]), ts.factory.createStringLiteral(label)]));
    }
    return ts.visitEachChild(node, visit, context);
  };
  return source => ts.visitNode(source, visit);
}
async function moduleURL(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8');
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
    transformers: { before: [tagHooks] },
  });
  for (const [name, url] of Object.entries(imports)) outputText = outputText.replaceAll(`'${name}'`, `'${url}'`).replaceAll(`"${name}"`, `"${url}"`);
  return asURL(outputText);
}
const reactURL = asURL(`
export function useState(initial, label) {
  const h=globalThis.__workspaceHarness;
  if (!(label in h.state)) h.state[label]=typeof initial==='function'?initial():initial;
  return [h.state[label], value=>{ h.state[label]=typeof value==='function'?value(h.state[label]):value; }];
}
export const useEffect=()=>{};
export const useCallback=callback=>callback;
export const useRef=value=>({current:value});
export const useId=()=> 'fixture-feedback';
export const lazy=()=>()=>null;
export const Suspense=()=>null;
`);
const jsxURL = asURL('export const jsx=(type,props,key)=>({type,props,key}); export const jsxs=jsx; export const Fragment="fixture-fragment";');
const portalURL = asURL('export const createPortal=children=>children;');
const stubURL = asURL('export default function Stub() { return null; }');
const iconsURL = asURL('export const ' + [
  'AudioLines','Check','ChevronRight','Download','Ellipsis','FileAudio','FileDown','FolderInput','FolderMinus','History','Inbox',
  'ListChecks','MessageCircle','Pencil','RefreshCw','RotateCcw','Trash2','ArrowUp','BookOpen','Copy','ExternalLink','Sparkles',
  'ChevronLeft','Files','LogOut','Menu','MessagesSquare','Plus',
].map(name=>name+'=()=>null').join(',')+';');
const apiURL = asURL(`
export const QUESTION_MAX_LENGTH=2000;
export const inspectQuestion=value=>({query:value.trim(),length:[...value.trim()].length,error:''});
export async function loginStatus(){throw Error('fixture must not request');}
`);
const common = { react: reactURL, 'react/jsx-runtime': jsxURL, 'lucide-react': iconsURL, '../api': apiURL };
const helperURL = await moduleURL('../src/workspacePresentation.ts');
const { visibleHistory } = await import(helperURL);
const { default: SourcesPanel } = await import(await moduleURL('../src/components/SourcesPanel.tsx', {
  ...common, 'react-dom': portalURL, './ActionMenu': stubURL, './BuildConfirmModal': stubURL,
  './VideoContentModal': stubURL, './SessionActionModal': stubURL, './KnowledgeBasesPanel': stubURL,
  '../workspacePresentation': helperURL,
}));
const { default: ChatPanel } = await import(await moduleURL('../src/components/ChatPanel.tsx', {
  ...common, './ExecutionTrace': stubURL, './MessageContent': stubURL,
  '../clipboard': asURL('export const answerClipboardText=x=>x; export async function copyText(){}'),
}));
const { default: Workspace } = await import(await moduleURL('../src/pages/Workspace.tsx', {
  ...common, '../components/SourcesPanel': stubURL, '../components/ChatPanel': stubURL,
  '../pageRefresh': asURL('export const subscribePageReturn=()=>()=>{};'),
}));
function all(node, predicate) {
  if (node == null || typeof node !== 'object') return [];
  if (Array.isArray(node)) return node.flatMap(child=>all(child,predicate));
  return [...(predicate(node)?[node]:[]), ...all(node.props?.children,predicate)];
}
function text(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  return Array.isArray(node) ? node.map(text).join('') : text(node.props?.children);
}
const buttons = tree=>all(tree,node=>node.type==='button');
const findButton=(tree,label)=>buttons(tree).find(node=>text(node)===label);
function harness(Component, props, state={}) {
  const h={state:{...state}};
  const render=()=>{globalThis.__workspaceHarness=h; return Component(props);};
  return {render,state:h.state};
}
const sessions = Array.from({length:10},(_,i)=>({id:i+1,title:'重复标题',knowledge_base_id:1}));
const sourceProps = (overrides={}) => ({view:'chat',onChangeView(){},contentContainer:{},chatOnly:false,
  onKnowledgeBaseUpdated(){},onBuildDone(){},selectedId:'all',onSelectCollection(){},selectedKnowledgeBase:null,
  onSelectKnowledgeBase(){},statsRefreshKey:0,historyRefreshKey:0,activeSessionId:9,chatBusy:false,
  onSelectSession(){},onNewChat(){},onSessionRenamed(){},onSessionDeleted(){},...overrides});
const chatProps = overrides=>({collectionId:'all',statsRefreshKey:0,knowledgeBaseId:null,knowledgeBaseName:'',
  openedSessionId:null,sessionTitle:'新对话',onSessionSaved(){},onHistoryChanged(){},onBusyChange(){},onNewChat(){},...overrides});
globalThis.document = { body: {} }; // The portal target is an inert fixture, never a real DOM.

test('recent history preserves duplicate-title rows and an older active item without mutating the server list',()=>{
  const original=JSON.stringify(sessions);
  assert.deepEqual(visibleHistory(sessions,9,false).map(x=>x.id),[1,2,3,4,5,6,9]);
  assert.deepEqual(visibleHistory(sessions,2,false).map(x=>x.id),[1,2,3,4,5,6]);
  assert.equal(visibleHistory(sessions,9,true),sessions);
  assert.equal(JSON.stringify(sessions),original);
});
test('actual history disclosure expands, collapses and retains each displayed menu and older active opening',()=>{
  const opened=[];
  const base={id:1,name:'合成资料库',video_count:3,ready_count:2};
  const h=harness(SourcesPanel,sourceProps({onSelectSession:(...args)=>opened.push(args)}),
    {sessions,historyLoading:false,knowledgeBases:[base]});
  let tree=h.render();
  const rows=t=>all(t,n=>n.props?.className?.startsWith('history-row'));
  assert.equal(rows(tree).length,7);
  assert.equal(all(tree,n=>n.props?.label?.startsWith('管理对话')).length,7);
  const older=rows(tree).at(-1);
  all(older,n=>n.type==='button')[0].props.onClick();
  assert.equal(opened[0][0].id,9);
  assert.equal(opened[0][1],base);
  const toggle=findButton(tree,'全部历史 (10)');
  assert.equal(toggle.props['aria-expanded'],false);
  toggle.props.onClick();
  tree=h.render();
  assert.equal(rows(tree).length,10);
  assert.equal(all(tree,n=>n.props?.label?.startsWith('管理对话')).length,10);
  findButton(tree,'收起历史').props.onClick();
  assert.equal(rows(h.render()).length,7);
});
test('history supported-limit notice and busy/missing-library protections survive collapsed presentation',()=>{
  const hundred=Array.from({length:100},(_,i)=>({id:i+1,title:'会话',knowledge_base_id:99}));
  const tree=harness(SourcesPanel,sourceProps({activeSessionId:80,chatBusy:true}),{sessions:hundred,historyLoading:false}).render();
  assert.match(text(tree),/显示最近 100 段对话/);
  assert.ok(all(tree,n=>n.props?.className==='history-open').every(n=>n.props.disabled));
  assert.equal(all(tree,n=>n.props?.className?.startsWith('history-row')).length,7);
  const small=harness(SourcesPanel,sourceProps(),{sessions:sessions.slice(0,6),historyLoading:false}).render();
  assert.equal(all(small,n=>n.props?.className?.includes('history-toggle')).length,0);
});
test('empty new chat hides only its generic heading; draft, prompt chips and saved/custom headings remain',()=>{
  const h=harness(ChatPanel,chatProps());
  let tree=h.render();
  assert.equal(all(tree,n=>n.props?.className==='chat-session-heading').length,0);
  assert.ok(all(tree,n=>n.props?.className==='prompt-chip').length>0);
  all(tree,n=>n.type==='textarea')[0].props.onChange({target:{value:'尚未发送的草稿'}});
  tree=h.render();
  assert.equal(all(tree,n=>n.type==='textarea')[0].props.value,'尚未发送的草稿');
  for(const props of [{openedSessionId:7},{sessionTitle:'自定义会话'}]){
    const saved=harness(ChatPanel,chatProps(props)).render();
    assert.equal(all(saved,n=>n.props?.className==='chat-session-heading').length,1);
  }
});
test('scope stays explicit for all materials, a named empty library, a collection and demo',()=>{
  for(const [props,expected]of [
    [{},'资料范围：全部已入库资料'],[{knowledgeBaseId:4,knowledgeBaseName:'空的合成库'},'资料范围：空的合成库'],
    [{collectionId:'fixture-collection'},'资料范围：当前收藏夹'],[{chatOnly:true},'体验模式 · 不使用收藏资料'],
  ]){
    const tree=harness(ChatPanel,chatProps(props)).render();
    assert.ok(text(tree).includes(expected));
    assert.doesNotMatch(text(tree),/空的合成库.*已可提问/);
  }
});
test('workspace retains NewConversation on every view and removes only collections self-navigation',()=>{
  for(const view of ['chat','materials','collections','progress']){
    const h=harness(Workspace,{loggedIn:true,onLogout(){}},{view});
    const tree=h.render();
    assert.ok(findButton(tree,'新对话'));
    const mountedChat=all(tree,node=>typeof node.props?.onSessionSaved==='function')[0];
    assert.ok(mountedChat);
    assert.equal(Boolean(findButton(tree,'从收藏夹导入')),view!=='collections');
    if(view==='materials'){
      findButton(tree,'从收藏夹导入').props.onClick();
      assert.equal(h.state.view,'collections');
      assert.equal(all(h.render(),node=>typeof node.props?.onSessionSaved==='function')[0].key,mountedChat.key);
    }
    findButton(tree,'新对话').props.onClick();
    assert.equal(h.state.view,'chat');
    assert.equal(h.state.openedSessionId,null);
    assert.notEqual(all(h.render(),node=>typeof node.props?.onSessionSaved==='function')[0].key,mountedChat.key);
  }
});
test('sidebar compact navigation retains processing/failure state and equivalent view callbacks',()=>{
  const views=[];
  const h=harness(SourcesPanel,sourceProps({view:'progress',onChangeView:view=>views.push(view)}),
    {stats:{video_cache:{done:8,transcribing:1,failed:2}},historyLoading:false});
  const tree=h.render();
  const nav=all(tree,n=>n.props?.className?.startsWith('task-shortcut'));
  assert.equal(nav.length,2);
  assert.match(text(nav[1]),/视频正在处理中8 可提问 · 2 失败/);
  assert.equal(nav[1].props['aria-current'],'page');
  nav.forEach(n=>n.props.onClick());
  assert.deepEqual(views,['assistant','progress']);
  const demo=harness(SourcesPanel,sourceProps({chatOnly:true})).render();
  assert.equal(all(demo,n=>n.props?.className?.startsWith('task-shortcut')).length,1);
});
test('materials title remains a real detail action and exports/removal/global delete remain distinct',()=>{
  const video={platform_item_id:'fixture-video',title:'合成视频标题',status:'done'};
  const h=harness(SourcesPanel,sourceProps({view:'materials',selectedKnowledgeBase:{id:4,name:'合成库'}}),
    {baseVideos:[video],baseVideosLoading:false,historyLoading:false});
  const tree=h.render();
  assert.equal(findButton(tree,'添加视频'),undefined);
  assert.equal(findButton(tree,'查看识别结果'),undefined);
  assert.ok(findButton(tree,'导出原文与时间'));
  assert.ok(buttons(tree).some(button=>/AI.*导出/.test(text(button))));
  assert.ok(findButton(tree,'仅移出当前知识库'));
  assert.ok(buttons(tree).some(button=>button.props.className==='danger-action' && text(button).startsWith('删除全部')));
  const title=all(tree,n=>n.props?.className==='resource-title')[0];
  title.props.onClick();
  assert.equal(h.state.viewingItemId,'fixture-video');
});
test('collection failure reason and empty-materials import CTA are retained',()=>{
  const failed={platform_item_id:'failed-video',title:'合成失败视频',status:'failed'};
  const h=harness(SourcesPanel,sourceProps({view:'collections'}),{expandedId:'fixture-folder',expandedVideos:[failed],historyLoading:false});
  const reason=findButton(h.render(),'查看失败原因');
  assert.ok(reason);
  reason.props.onClick();
  assert.equal(h.state.viewingItemId,'failed-video');
  const empty=harness(SourcesPanel,sourceProps({view:'materials'}),{baseVideos:[],baseVideosLoading:false}).render();
  assert.ok(findButton(empty,'从收藏夹导入'));
});
test('compact navigation and disclosure retain a 44px minimum target without changing assistant-region selectors',async()=>{
  const css=await readFile(new URL('../src/index.css',import.meta.url),'utf8');
  for(const selector of ['.task-shortcut {','.history-toggle {','.library-open {']){
    const rule=css.split('\n').find(line=>line.startsWith(selector));
    assert.match(rule,/min-height: 44px/);
  }
});
