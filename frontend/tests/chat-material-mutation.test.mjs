import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

// Real component handlers, inert JSX/hooks and a synthetic API module only.
// Product api.ts, fetch, a model and a development server are never invoked.
const asURL = value => `data:text/javascript;base64,${Buffer.from(value).toString('base64')}`;
const hooksURL = asURL(`
function slot(initial) {
  const h=globalThis.__chatMaterialMutationHarness, index=h.cursor++;
  if (!(index in h.slots)) h.slots[index]=initial;
  return [h,index];
}
export function useState(initial) {
  const [h,index]=slot(initial);
  return [h.slots[index],value=>{h.slots[index]=typeof value==='function'?value(h.slots[index]):value;}];
}
export function useRef(initial) { const [h,index]=slot({current:initial});return h.slots[index]; }
export function useId() { const [h,index]=slot(null);return 'mutation-feedback-'+index; }
export function useEffect(effect,deps) {
  const [h,index]=slot(undefined), old=h.slots[index];
  if (!old || deps.some((value,i)=>!Object.is(value,old.deps[i]))) {
    h.slots[index]={deps,cleanup:old?.cleanup};
    h.effects.push(()=>{h.slots[index].cleanup?.();h.slots[index].cleanup=effect();});
  }
}
`);
const jsxURL=asURL('export const jsx=(type,props)=>({type,props});export const jsxs=jsx;export const Fragment="fragment";');
const stubURL=asURL('export default function FixtureStub(){return null;}');
const iconsURL=asURL('export const ArrowUp=()=>null,BookOpen=()=>null,Check=()=>null,Copy=()=>null,ExternalLink=()=>null,Sparkles=()=>null;');
const clipboardURL=asURL('export const answerClipboardText=value=>value;export async function copyText(){}');
const apiURL=asURL(`
export const QUESTION_MAX_LENGTH=2000;
export function inspectQuestion(input) {
  const query=input.trim(),length=[...query].length;
  return {query,length,error:!length?'请输入问题。':length>2000?'问题请控制在2000字以内。':''};
}
export const chatAskStream=(...args)=>globalThis.__chatMaterialMutationHarness.api.stream(...args);
export const getSessionMessages=(...args)=>globalThis.__chatMaterialMutationHarness.api.history(...args);
`);
let {outputText}=ts.transpileModule(await readFile(new URL('../src/components/ChatPanel.tsx',import.meta.url),'utf8'),{
  compilerOptions:{target:ts.ScriptTarget.ES2020,module:ts.ModuleKind.ESNext,jsx:ts.JsxEmit.ReactJSX},
});
for(const [name,url] of Object.entries({
  react:hooksURL,'react/jsx-runtime':jsxURL,'../api':apiURL,
  './ExecutionTrace':stubURL,'./MessageContent':stubURL,
  '../clipboard':clipboardURL,'lucide-react':iconsURL,
})) outputText=outputText.replaceAll(`'${name}'`,`'${url}'`).replaceAll(`"${name}"`,`"${url}"`);
const {default:ChatPanel}=await import(asURL(outputText));

function findAll(node,predicate) {
  if(node==null||typeof node!=='object')return [];
  if(Array.isArray(node))return node.flatMap(child=>findAll(child,predicate));
  return [...(predicate(node)?[node]:[]),...findAll(node.props?.children,predicate)];
}
function text(node) {
  if(node==null||typeof node==='boolean')return '';
  if(typeof node==='string'||typeof node==='number')return String(node);
  return Array.isArray(node)?node.map(text).join(''):text(node.props?.children);
}
const textarea=tree=>findAll(tree,node=>node.type==='textarea')[0];
const submit=tree=>findAll(tree,node=>node.type==='form')[0].props.onSubmit;
const send=tree=>findAll(tree,node=>node.props?.['aria-label']==='发送问题')[0];
const chips=tree=>findAll(tree,node=>node.type==='button'&&node.props?.className==='prompt-chip');
const bubbles=tree=>findAll(tree,node=>node.props?.className?.split(' ').includes('chat-bubble'));
const answerContent=tree=>findAll(tree,node=>typeof node.props?.content==='string').map(node=>node.props.content).join('\n');
const event=()=>({preventDefault(){}});
const settle=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
const deferred=()=>{let resolve;const promise=new Promise(yes=>{resolve=yes;});return {promise,resolve};};

function harness(overrides={}) {
  const calls={stream:[],history:[],saved:[],changed:0,busy:[]};
  const h={cursor:0,slots:[],effects:[],api:{}};
  const props={
    collectionId:'all',statsRefreshKey:0,chatOnly:false,knowledgeBaseId:5,knowledgeBaseName:'合成测试资料',
    openedSessionId:null,sessionTitle:'',
    onSessionSaved:(...args)=>calls.saved.push(args),onHistoryChanged:()=>calls.changed++,
    onBusyChange:value=>calls.busy.push(value),onNewChat(){},...overrides,
  };
  h.api.stream=async function* (...args) {
    calls.stream.push(args);
    yield {_event:'delta',text:'合成回答'};
    yield {_event:'meta',session_id:27,answer:'合成回答',sources:[]};
  };
  h.api.history=async(...args)=>{
    calls.history.push(args);
    return {success:true,items:[]};
  };
  const render=changes=>{
    Object.assign(props,changes);globalThis.__chatMaterialMutationHarness=h;h.cursor=0;
    const tree=ChatPanel(props);h.effects.splice(0).forEach(effect=>effect());return tree;
  };
  return {render,calls,api:h.api};
}

test('material mutation disables textarea, send and every prompt and blocks manually invoked submit and chip handlers',async()=>{
  const h=harness();
  let tree=h.render();
  const draft='尚未发送的真实草稿';
  textarea(tree).props.onChange({target:{value:draft}});
  tree=h.render({interactionDisabled:true});
  assert.equal(textarea(tree).props.disabled,true);
  assert.equal(send(tree).props.disabled,true);
  assert.ok(chips(tree).length>0);
  for(const chip of chips(tree)) {
    assert.equal(chip.props.disabled,true);
    chip.props.onClick();
  }
  await submit(tree)(event());
  tree=h.render();
  assert.equal(textarea(tree).props.value,draft);
  assert.equal(bubbles(tree).length,0);
  assert.deepEqual(h.calls,{stream:[],history:[],saved:[],changed:0,busy:[]});
});

test('false to true to false preserves the draft and unlocking restores the normal explicit-send behavior',async()=>{
  const h=harness({interactionDisabled:false});
  let tree=h.render();
  const draft='  解锁后继续这个问题  ';
  textarea(tree).props.onChange({target:{value:draft}});
  tree=h.render({interactionDisabled:true});
  assert.equal(textarea(tree).props.value,draft);
  tree=h.render({interactionDisabled:false});
  assert.equal(textarea(tree).props.value,draft);
  assert.equal(textarea(tree).props.disabled,false);
  assert.equal(send(tree).props.disabled,false);
  assert.ok(chips(tree).every(chip=>chip.props.disabled===false));
  assert.equal(h.calls.stream.length,0);
  await submit(tree)(event());
  tree=h.render();
  assert.equal(h.calls.stream.length,1);
  assert.equal(h.calls.stream[0][0],draft.trim());
  assert.equal(h.calls.stream[0][2],'all');
  assert.equal(h.calls.stream[0][4],5);
  assert.equal(textarea(tree).props.value,'');
  assert.equal(bubbles(tree).length,2);
  assert.deepEqual(h.calls.busy,[true,false]);
  assert.equal(h.calls.changed,1);
});

test('unlocked prompts fill the input again without automatically asking',()=>{
  const h=harness({interactionDisabled:true});
  let tree=h.render();
  tree=h.render({interactionDisabled:false});
  const chip=chips(tree)[0],question=text(chip);
  chip.props.onClick();
  tree=h.render();
  assert.equal(textarea(tree).props.value,question);
  assert.equal(send(tree).props.disabled,false);
  assert.equal(h.calls.stream.length,0);
  assert.equal(bubbles(tree).length,0);
});

test('changing the mutation prop does not abort or reset an already active synthetic stream',async()=>{
  const h=harness(),gate=deferred();
  h.api.stream=async function* (...args) {
    h.calls.stream.push(args);
    yield {_event:'delta',text:'先到达的片段'};
    await gate.promise;
    yield {_event:'delta',text:'，后到达的片段'};
  };
  let tree=h.render();
  textarea(tree).props.onChange({target:{value:'流式测试问题'}});
  tree=h.render();
  const pending=submit(tree)(event());
  await settle();
  tree=h.render({interactionDisabled:true});
  assert.match(answerContent(tree),/先到达的片段/);
  const signal=h.calls.stream[0][3];
  assert.equal(signal.aborted,false);
  tree=h.render({interactionDisabled:false});
  assert.equal(signal.aborted,false);
  assert.equal(send(tree).props.disabled,true); // Existing streaming gate still applies.
  assert.equal(textarea(tree).props.disabled,true);
  gate.resolve();await pending;
  tree=h.render();
  assert.match(answerContent(tree),/先到达的片段，后到达的片段/);
  assert.equal(bubbles(tree).length,2);
  assert.equal(h.calls.stream.length,1);
  assert.deepEqual(h.calls.busy,[true,false]);
});

test('changing the mutation prop neither restarts nor clears a pending historical conversation read',async()=>{
  const h=harness({openedSessionId:9,sessionTitle:'合成历史对话'}),gate=deferred();
  h.api.history=(...args)=>{h.calls.history.push(args);return gate.promise;};
  let tree=h.render();
  tree=h.render({interactionDisabled:true});
  tree=h.render({interactionDisabled:false});
  assert.equal(h.calls.history.length,1);
  assert.equal(h.calls.history[0][0],9);
  assert.equal(h.calls.history[0][1].aborted,false);
  assert.equal(textarea(tree).props.disabled,true); // Existing historical-loading gate.
  assert.equal(send(tree).props.disabled,true);
  gate.resolve({success:true,items:[{role:'user',content:'原有历史内容'}]});await settle();
  tree=h.render();
  assert.match(text(tree),/原有历史内容/);
  assert.equal(textarea(tree).props.disabled,false);
  assert.equal(h.calls.history.length,1);
  assert.equal(h.calls.stream.length,0);
  assert.deepEqual(h.calls.busy,[true,false]);
});
