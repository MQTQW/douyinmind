"""Static same-origin manual verification viewer; no external assets/storage."""

PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="manual-nonce" content="__MANUAL_NONCE__"><title>DouyinMind 本人核验</title>
<style>body{font:16px system-ui;max-width:1120px;margin:24px auto;padding:16px;background:#f7f9f1;color:#294532}button,input{font:inherit;padding:10px;margin:5px;border:1px solid #b8c9ad;border-radius:8px}button{background:#e6f0dc;cursor:pointer}img{max-width:100%;border:1px solid #b8c9ad;touch-action:none;user-select:none}#screen{display:none}#status{padding:12px;background:#edf3e5}small{display:block;margin:10px}</style>
<h1>DouyinMind 本人核验</h1><p>仅操作本机助手 B 当前的抖音页面。完成后助手保持暂停，请回项目页面再开启。</p>
<div id="status">尚未开始。此入口不会自动发送评论。</div>
<button id="begin">开始本次核验</button><button id="shot">刷新画面</button>
<button id="finish">我已完成，核对结果</button><button id="cancel">结束本次核验</button>
<small>点击画面中的输入框，再在下面输入；滑块请在画面上亲手拖动。轨迹仅来自本次鼠标或触摸，不自动解题。文字不保存，不要在聊天中发送验证码。</small>
<input id="text" type="password" maxlength="256" autocomplete="off" placeholder="仅输入当前核验所需文字">
<button id="type">输入到已选中的框</button><button id="tab">Tab</button><button id="back">退格</button><button id="enter">Enter</button>
<p><img id="screen" alt="当前助手页面；点击画面中的控件"></p>
<script src="/viewer.js"></script></html>'''

SCRIPT = r'''(() => {
'use strict';
const nonce=document.querySelector('meta[name="manual-nonce"]').content;
const status=document.getElementById('status'),image=document.getElementById('screen');
let session=null,frameURL=null,frameGeneration=-1,inputEnabled=false,dragging=false,points=[],gestureStart=0,updating=false;
const messages={idle:'尚未开始',queued:'已登记，等待助手主线程处理',processing:'正在处理本次操作',active:'核验通道已开启',completed:'核验检查通过；助手仍暂停，请回项目页面开启',cancelled:'核验通道已关闭',expired:'本次核验已到期，请重新开始',verification_not_ready:'助手当前不满足核验条件，操作已停止',verification_failed:'核验尚未通过，或本次操作未完成'};
async function request(path,body){const options={method:body===undefined?'GET':'POST',cache:'no-store',headers:{'X-DouyinMind-Manual':nonce}};if(body!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}const response=await fetch(path,options);if(!response.ok)throw new Error('manual_request_failed');return response;}
async function action(name,payload={}){if(!session)throw new Error('manual_not_active');const command={session_id:session,action:name,payload};if(['click','drag','key','text'].includes(name))command.frame_generation=frameGeneration;await request('/api/action',command);}
function clearFrame(){if(frameURL)URL.revokeObjectURL(frameURL);frameURL=null;frameGeneration=-1;image.removeAttribute('src');image.style.display='none';}
async function update(){if(updating)return;updating=true;try{const value=await(await request('/api/status')).json();session=value.session_id;inputEnabled=value.input_enabled===true&&value.auth_region_known===true;status.textContent=messages[value.state]||'核验暂时不可用';if(value.state==='active'&&value.frame_available&&!inputEnabled)status.textContent+='；当前核验区域未识别，仅可查看画面或核对结果。';for(const id of ['text','type','tab','back','enter'])document.getElementById(id).disabled=!inputEnabled;if(!dragging){if(!value.frame_available)clearFrame();else if(!value.pending&&value.state==='active'&&value.frame_generation!==frameGeneration){const response=await request('/api/frame'),generation=Number(response.headers.get('X-DouyinMind-Frame-Generation')),blob=await response.blob();if(!Number.isSafeInteger(generation)||generation!==value.frame_generation)throw new Error('frame_changed');clearFrame();const actualURL=URL.createObjectURL(blob);frameURL=actualURL;image.onload=()=>{if(frameURL!==actualURL)return;frameGeneration=generation;image.style.display='block';};image.src=actualURL;}}}catch(_){status.textContent='核验状态暂时无法确认；请保留原任务。';if(!dragging)clearFrame();}finally{updating=false;}}
function bind(id,fn){document.getElementById(id).addEventListener('click',async()=>{try{await fn();await update();}catch(_){status.textContent='操作结果未确认，请先刷新画面核对，不要重复提交。';}});}
bind('begin',async()=>{const value=await(await request('/api/begin',{})).json();session=value.session_id;});
bind('shot',()=>{if(dragging)throw new Error('gesture_active');return action('screenshot');});bind('finish',()=>action('finish'));bind('cancel',()=>action('cancel'));
bind('type',async()=>{const field=document.getElementById('text'),text=field.value;field.value='';await action('text',{text});});
bind('tab',()=>action('key',{key:'Tab'}));bind('back',()=>action('key',{key:'Backspace'}));bind('enter',()=>action('key',{key:'Enter'}));
function point(event,t){const rect=image.getBoundingClientRect();return{x:Math.floor((event.clientX-rect.left)*image.naturalWidth/rect.width),y:Math.floor((event.clientY-rect.top)*image.naturalHeight/rect.height),t};}
function record(event){points.push(point(event,Math.floor(performance.now()-gestureStart)));if(points.length>80){const last=points[points.length-1];points=points.filter((_,index)=>index===0||index%2===0);if(points[points.length-1]!==last)points.push(last);}}
image.addEventListener('pointerdown',event=>{if(!inputEnabled||frameGeneration<0||dragging||event.button!==0)return;event.preventDefault();dragging=true;gestureStart=performance.now();points=[point(event,0)];image.setPointerCapture(event.pointerId);});
image.addEventListener('pointermove',event=>{if(dragging){event.preventDefault();record(event);}});
image.addEventListener('pointerup',async event=>{if(!dragging)return;record(event);dragging=false;const actual=points;points=[];try{if(actual[actual.length-1].t>10000)throw new Error('gesture_too_long');const first=actual[0],last=actual[actual.length-1];if(actual.every(p=>Math.abs(p.x-first.x)<3&&Math.abs(p.y-first.y)<3))await action('click',{x:last.x,y:last.y});else await action('drag',{points:actual});await update();}catch(_){status.textContent='手势结果未确认，请先刷新画面核对，不要重复提交。';}});
image.addEventListener('pointercancel',()=>{dragging=false;points=[];status.textContent='本次手势已取消，未发送。';});
window.addEventListener('pagehide',()=>{document.getElementById('text').value='';clearFrame();});
setInterval(update,1000);update();
})();'''
