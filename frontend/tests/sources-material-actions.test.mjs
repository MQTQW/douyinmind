import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const asURL = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`;
const source = await readFile(new URL('../src/components/SourcesPanel.tsx', import.meta.url), 'utf8');
const tagHooks = context => {
  const visit = node => {
    if (ts.isVariableDeclaration(node) && node.initializer && ts.isCallExpression(node.initializer) && ts.isIdentifier(node.initializer.expression)) {
      const hook = node.initializer.expression.text;
      if (hook === 'useState' || hook === 'useRef') {
        const label = ts.isArrayBindingPattern(node.name) ? node.name.elements[0].name.text : node.name.text;
        return ts.factory.updateVariableDeclaration(node, node.name, node.exclamationToken, node.type,
          ts.factory.updateCallExpression(node.initializer, node.initializer.expression, node.initializer.typeArguments,
            [...(node.initializer.arguments.length ? node.initializer.arguments : [ts.factory.createVoidZero()]), ts.factory.createStringLiteral(label)]));
      }
    }
    return ts.visitEachChild(node, visit, context);
  };
  return node => ts.visitNode(node, visit);
};
const reactURL = asURL(`
export function useState(initial,label){const h=globalThis.__materialActions;if(!(label in h.state))h.state[label]=typeof initial==='function'?initial():initial;return[h.state[label],value=>{h.state[label]=typeof value==='function'?value(h.state[label]):value}];}
export function useRef(initial,label){const h=globalThis.__materialActions;if(!(label in h.refs))h.refs[label]={current:label==='aliveRef'?true:initial};return h.refs[label];}
export const useEffect=()=>{};export const useCallback=value=>value;
`);
const jsxURL = asURL('export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;export const Fragment="fixture-fragment";');
const stubURL = asURL('export default function Stub(){return null;}');
const iconNames = source.match(/^import \{([^}]+)\} from 'lucide-react';/m)[1].split(',').map(x => x.trim());
const apiURL = asURL(`
export async function deleteVideo(id){const h=globalThis.__materialActions;h.calls.push(['delete',id]);return h.api.deleteVideo(id);}
export async function removeKnowledgeBaseVideo(base,id){const h=globalThis.__materialActions;h.calls.push(['remove',base,id]);return h.api.removeKnowledgeBaseVideo(base,id);}
export async function getKnowledgeStats(){return {success:true,video_cache:{done:0}};}
`);
const imports = {
  react: reactURL, 'react/jsx-runtime': jsxURL, 'react-dom': asURL('export const createPortal=children=>children;'),
  'lucide-react': asURL(`export const ${iconNames.map(x=>`${x}=()=>null`).join(',')};`), '../api': apiURL,
  './ActionMenu': stubURL, './BuildConfirmModal': stubURL, './VideoContentModal': stubURL,
  './SessionActionModal': stubURL, './KnowledgeBasesPanel': stubURL,
  '../workspacePresentation': asURL('export const visibleHistory=(items)=>items;'),
};
let { outputText } = ts.transpileModule(source, {compilerOptions:{target:ts.ScriptTarget.ES2020,module:ts.ModuleKind.ESNext,jsx:ts.JsxEmit.ReactJSX},transformers:{before:[tagHooks]}});
for (const [name,url] of Object.entries(imports)) outputText=outputText.replaceAll(`'${name}'`,`'${url}'`).replaceAll(`"${name}"`,`"${url}"`);
const {default:SourcesPanel}=await import(asURL(outputText));
const walk=(node,predicate)=>!node||typeof node!=='object'?[]:Array.isArray(node)?node.flatMap(x=>walk(x,predicate)):[...(predicate(node)?[node]:[]),...walk(node.props?.children,predicate)];
const text=node=>node==null||typeof node==='boolean'?'':typeof node==='object'?Array.isArray(node)?node.map(text).join(''):text(node.props?.children):String(node);
const button=(tree,label)=>walk(tree,node=>node.type==='button'&&text(node)===label)[0];
const checkbox=(tree,label)=>walk(tree,node=>node.type==='input'&&node.props['aria-label']===label)[0];
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const videos=[{platform_item_id:'one',title:'Git 合并',author:'合成',status:'done'}, {platform_item_id:'two',title:'学习笔记',author:'合成',status:'done'}, {platform_item_id:'three',title:'GIT 分支',author:'合成',status:'done'}];
function harness({props={},state={},api={},confirm=true}={}){
  const h={state:{baseVideos:videos,baseVideosLoading:false,historyLoading:false,stats:{capabilities:{guarded_video_delete:true}},...state},refs:{},calls:[],busy:[],refreshes:0,confirmations:[],api:{deleteVideo:async()=>({success:true}),removeKnowledgeBaseVideo:async()=>({success:true}),...api}};
  const baseProps={view:'materials',contentContainer:{},chatOnly:false,onKnowledgeBaseUpdated(){},onBuildDone(){h.refreshes++;},selectedId:'all',onSelectCollection(){},selectedKnowledgeBase:{id:9,name:'合成知识库'},onSelectKnowledgeBase(){},statsRefreshKey:0,historyRefreshKey:0,activeSessionId:null,chatBusy:false,onSelectSession(){},onNewChat(){},onSessionRenamed(){},onSessionDeleted(){},onChangeView(){},onMutationBusyChange(value){h.busy.push(value);},...props};
  h.render=()=>{globalThis.__materialActions=h;globalThis.document={body:{}};globalThis.confirm=message=>{h.confirmations.push(message);return confirm;};return SourcesPanel(baseProps);};
  return h;
}

test('title search uses normalized titles and selection stays independent of hidden rows',()=>{
  const h=harness({props:{view:'collections'},state:{expandedId:'folder',expandedVideos:videos,collectionSearch:' ＧＩＴ ',selectedVideos:new Map([['two',videos[1]]])}});
  const tree=h.render();
  assert.equal(walk(tree,node=>node.props?.className?.startsWith('collection-video-row')).length,2);
  checkbox(tree,'全选搜索结果').props.onChange();
  assert.deepEqual([...h.state.selectedVideos.keys()].sort(),['one','three','two']);
  const next=h.render();checkbox(next,'全选搜索结果').props.onChange();
  assert.deepEqual([...h.state.selectedVideos.keys()],['two']);
  assert.equal(JSON.stringify(videos),JSON.stringify(h.state.expandedVideos));
});
test('whole-folder import retains every original video despite an active title filter',()=>{
  const h=harness({props:{view:'collections'},state:{expandedId:'folder',expandedVideos:videos,collectionSearch:'合并',collections:[{collection_id:'folder',title:'合成收藏夹'}]}});
  button(h.render(),'整夹导入').props.onClick();
  assert.deepEqual(h.state.buildSelection.map(v=>v.platform_item_id),['one','two','three']);
  assert.deepEqual(h.state.buildSource.video_ids,['one','two','three']);
  assert.equal(h.state.showBuildConfirm,true);
});
test('empty title results retain selected videos and report the filtered result',()=>{
  const h=harness({props:{view:'collections'},state:{expandedId:'folder',expandedVideos:videos,collectionSearch:'不存在',selectedVideos:new Map([['two',videos[1]]])}});
  const tree=h.render();assert.match(text(tree),/没有找到标题匹配的视频/);
  assert.equal(checkbox(tree,'全选搜索结果').props.disabled,true);
  assert.equal(h.state.selectedVideos.size,1);
});
test('bulk selection and cancellation never issue a deletion',async()=>{
  const h=harness({confirm:false});checkbox(h.render(),'全选可删除资料').props.onChange();
  button(h.render(),'批量删除').props.onClick();await flush();
  assert.equal(h.calls.length,0);assert.equal(h.state.selectedMaterialIds.size,3);
  assert.match(h.confirmations[0],/所有知识库/);assert.deepEqual(h.busy,[]);
});
test('bulk removal uses the frozen current library, keeps global data and refreshes after success',async()=>{
  const h=harness({state:{selectedMaterialIds:new Set(['one','two'])}});
  button(h.render(),'批量移出').props.onClick();await flush();
  assert.deepEqual(h.calls,[['remove',9,'one'],['remove',9,'two']]);
  assert.deepEqual(h.busy,[true,false]);assert.equal(h.state.selectedMaterialIds.size,0);
  assert.match(h.state.bulkFeedback,/已移出 2 \/ 2/);assert.equal(h.refreshes,1);
});
test('global bulk delete uses only explicitly selected completed IDs',async()=>{
  const h=harness({state:{selectedMaterialIds:new Set(['two','missing'])}});
  button(h.render(),'批量删除').props.onClick();await flush();
  assert.deepEqual(h.calls,[['delete','two']]);assert.match(h.state.bulkFeedback,/已删除入库资料 1 \/ 1/);
});
test('a rejected item stops the rest and retains failed and unsubmitted choices',async()=>{
  const h=harness({state:{selectedMaterialIds:new Set(['one','two','three'])},api:{deleteVideo:async id=>({success:id!=='two'})}});
  button(h.render(),'批量删除').props.onClick();await flush();
  assert.deepEqual(h.calls,[['delete','one'],['delete','two']]);
  assert.deepEqual([...h.state.selectedMaterialIds],['two','three']);
  assert.match(h.state.bulkFeedback,/1 \/ 3/);assert.match(h.state.bulkFeedback,/已停止后续项/);
});
test('an uncertain response never triggers retry or later deletion',async()=>{
  const h=harness({state:{selectedMaterialIds:new Set(['one','two','three'])},api:{deleteVideo:async id=>{if(id==='two')throw Error('synthetic connection lost');return {success:true};}}});
  button(h.render(),'批量删除').props.onClick();await flush();
  assert.deepEqual(h.calls,[['delete','one'],['delete','two']]);
  assert.match(h.state.bulkFeedback,/结果未确认/);assert.deepEqual(h.busy,[true,false]);
});
test('rapid repeated clicks share one operation and a live processing task blocks destructive actions',async()=>{
  let finish;const pending=new Promise(resolve=>{finish=resolve;});
  const h=harness({state:{selectedMaterialIds:new Set(['one'])},api:{deleteVideo:()=>pending}});
  const action=button(h.render(),'批量删除');action.props.onClick();action.props.onClick();
  assert.deepEqual(h.calls,[['delete','one']]);finish({success:true});await flush();
  assert.deepEqual(h.busy,[true,false]);
  for(const state of [{stats:{current_task_id:'active'}},{building:true},{stats:{video_cache:{transcribing:1}}}]){
    const blocked=harness({state:{selectedMaterialIds:new Set(['one']),...state}});
    const control=button(blocked.render(),'批量删除');assert.equal(control.props.disabled,true);control.props.onClick();await flush();assert.equal(blocked.calls.length,0);
  }
});
test('busy chat and demo mode cannot start a bulk mutation even through the handler',async()=>{
  for(const props of [{chatBusy:true},{chatOnly:true}]){
    const h=harness({props,state:{selectedMaterialIds:new Set(['one'])}});
    button(h.render(),'批量删除').props.onClick();await flush();assert.equal(h.calls.length,0);
  }
});

test('an older backend without the deletion guard capability never receives a bulk request',async()=>{
  const h=harness({state:{stats:{success:true,video_cache:{done:3}},selectedMaterialIds:new Set(['one'])}});
  const control=button(h.render(),'批量删除');assert.equal(control.props.disabled,true);
  control.props.onClick();await flush();assert.equal(h.calls.length,0);assert.equal(h.confirmations.length,0);
});
