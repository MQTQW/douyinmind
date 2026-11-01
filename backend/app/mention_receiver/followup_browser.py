"""Ordinary thread follow-ups: shared DOM mechanics, distinct query evidence.

No ChoiceStore, library grammar, model or native-parent promotion is used here.
The reused JS only reads public structure. Its temporary choice-shaped fields
are discarded; the exported proof is exclusively conversation_followup.
"""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import re
import time
import unicodedata

from app.mention_agent.contracts import public_profile,canonical_json
from app.mention_agent.followup_evidence import make_followup_envelope,validate_followup_envelope
from .browser import ReceiverDetailError,ReceiverInterventionRequired,ReceiverPageError,REPLY_SEND_SELECTOR
from .choice_browser import (CHOICE_THREAD_JS,CHOICE_EXPAND_HANDLE_JS,CHOICE_ROW_IDENTITY_JS,
                             _choice_checkpoint,_choice_should_stop,_readback_viewport)


FOLLOWUP_HELPERS_JS=r"""
 const fpPublic=e=>{if(!e||!visible(e))return false;for(let p=e;p&&p.nodeType===1;p=p.parentElement){
   const s=getComputedStyle(p);if(s.display==='none'||s.visibility!=='visible'||parseFloat(s.opacity)===0)return false;}return true;};
 const fpInput=Array.from(document.querySelectorAll('#comment-input-container')).filter(rendered);
 const fpActors=new Set(fpInput.flatMap(e=>Array.from(e.querySelectorAll('a[href]')).filter(a=>rendered(a)&&a.querySelector('img')).map(profile)).filter(Boolean));
 const fpNumbers=new Set(Array.from((document.body.innerText||'').matchAll(/抖音号[：:\s]*(\d+)/g)).map(m=>m[1]));
 const fpHere=new URL(location.href);
 const fpActor=(fpInput.length===1&&fpActors.size===1&&fpActors.has(args.receiver))||
   (fpActors.size===0&&fpHere.pathname==='/user/self'&&fpHere.searchParams.get('modal_id')===args.video&&
    args.identity_fresh===true&&fpNumbers.size===1&&fpNumbers.has(args.expected_public_number));
 const fpPlainBody=(n,wanted,requirePublic=true)=>{
   const own=e=>e.closest(selector)===n,show=requirePublic?fpPublic:()=>true;
   const headers=Array.from(n.querySelectorAll('.comment-item-info-wrap')).filter(e=>own(e)&&show(e));
   const stats=Array.from(n.querySelectorAll('.comment-item-stats-container')).filter(e=>own(e)&&show(e));
   if(headers.length!==1||stats.length!==1||!show(n))return false;
   const header=headers[0],group=header.parentElement,body=header.nextElementSibling;
   let statBlock=stats[0];while(statBlock.parentElement&&statBlock.parentElement!==group&&own(statBlock))statBlock=statBlock.parentElement;
   if(!body||statBlock.parentElement!==group||!own(body)||!show(body)||body.querySelector(
      selector+',a,button,svg,img,time,[contenteditable],.comment-item-info-wrap,.comment-item-stats-container')||normal(body.innerText)!==normal(wanted))return false;
   let metadata=null;
   if(body.nextElementSibling!==statBlock){metadata=body.nextElementSibling;
     if(!metadata||metadata.nextElementSibling!==statBlock||metadata.tagName!=='DIV'||!own(metadata)||!show(metadata)||
       metadata.querySelector('a,button,svg,img,time,[contenteditable],'+selector)||
       !/^(?:刚刚|\d+(?:秒|分钟|小时|天|周|个月|年)前|\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?)(?:·[\p{L}]{1,32})?$/u.test(normal(metadata.innerText)))return false;}
   const avatars=Array.from(n.querySelectorAll('.comment-item-avatar')).filter(own);
   const names=Array.from(header.querySelectorAll('a[href]')).filter(a=>own(a)&&show(a)&&a.querySelector('[data-click-from="title"]')&&[args.sender,args.receiver].includes(profile(a)));
   const allowed=[body,metadata,...avatars].filter(Boolean),walker=document.createTreeWalker(n,NodeFilter.SHOW_TEXT);
   while(walker.nextNode()){const text=walker.currentNode,parent=text.parentElement,word=normal(text.nodeValue);
     if(!word||!parent||!own(parent)||!show(parent))continue;
     if(allowed.some(e=>e===parent||e.contains(parent)))continue;
     if(header.contains(parent)){
       if(names.some(a=>a===parent||a.contains(parent)))continue;
       if(parent.closest('.comment-item-tag')&&/^(?:作者|好友|互相关注|置顶)$/.test(word))continue;
       if(parent.closest('[data-e2e="video-comment-more"]')&&/^(?:\.{3}|…{1,2})$/.test(word))continue;
     }
     if(stats[0].contains(parent)&&/^(?:\d+(?:\.\d+)?[万wW]?|分享|回复|赞|点赞|踩|收藏|前往西瓜视频回复评论)$/.test(word))continue;
     return false;
   }return true;
 };
 const fpIdentity=(n,wanted,requirePublic=true)=>{
   const show=requirePublic?fpPublic:()=>true,regular=rowIdentity(n,show,requirePublic);
   if(regular)return regular;
   if(!fpActor||args.identity_fresh!==true||!fpPlainBody(n,wanted,requirePublic))return null;
   const own=e=>e.closest(selector)===n;
   const avatars=Array.from(n.querySelectorAll('.comment-item-avatar')).filter(own),heads=Array.from(n.querySelectorAll('.comment-item-info-wrap')).filter(own);
   if(avatars.length!==1||heads.length!==1||!show(avatars[0])||!show(heads[0])||avatars[0].matches('a')||avatars[0].querySelector('a')||
     [avatars[0],...avatars[0].querySelectorAll('*')].some(e=>e.hasAttribute('href')||e.hasAttribute('xlink:href')))return null;
   const names=Array.from(heads[0].querySelectorAll('a')).filter(own);
   if(names.length!==2||names.some(a=>!show(a)||!a.hasAttribute('href')||!a.getAttribute('href').trim()||!a.querySelector('[data-click-from="title"]')||!a.innerText.trim()||a.innerText.trim().startsWith('@'))||
      profile(names[0])!==args.receiver||profile(names[1])!==args.sender)return null;
   const parts=Array.from(names[0].parentElement.children),arrow=parts[1];
   if(names[1].parentElement!==names[0].parentElement||parts.length!==3||parts[0]!==names[0]||parts[2]!==names[1]||arrow.tagName!=='DIV'||arrow.children.length||arrow.innerText.trim())return null;
   const s=getComputedStyle(arrow);
   if(s.borderLeftStyle!=='solid'||parseFloat(s.borderLeftWidth)<=0||parseFloat(s.borderTopWidth)<=0||parseFloat(s.borderBottomWidth)<=0||parseFloat(s.borderRightWidth)!==0)return null;
   if(requirePublic&&(!fpPublic(arrow)||s.borderLeftColor==='transparent'||/rgba\([^)]*,\s*0(?:\.0+)?\s*\)$/.test(s.borderLeftColor)))return null;
   return {author:args.receiver,target:args.sender,authorLink:names[0],semantic:true,noavatar:true};
 };
"""

# Reuse only the audited root/row/body/expander observation implementation,
# without inventing choice IDs, asking a library parser or minting Choice proof.
_ASK_LINE="const asks=descendants.filter(n=>visible(n)&&owners(n).size===1&&owners(n).has(args.receiver)&&ownText(n,args.ask_text));"
if CHOICE_THREAD_JS.count(_ASK_LINE)!=1:raise RuntimeError('followup_dom_dependency_changed')
FOLLOWUP_THREAD_JS=CHOICE_THREAD_JS.replace(_ASK_LINE,FOLLOWUP_HELPERS_JS+r"""
 if(!fpActor)return {ok:false,reason_code:'followup_current_receiver_unverified',counts};
 const priorBodies=descendants.filter(n=>fpPlainBody(n,args.ask_text,false));
 if(priorBodies.length>1)return {ok:false,reason_code:'followup_prior_reply_not_unique',counts};
 const asks=priorBodies.filter(n=>fpPublic(n)&&fpIdentity(n,args.ask_text)?.author===args.receiver&&
   (!fpIdentity(n,args.ask_text)?.target||fpIdentity(n,args.ask_text)?.target===args.sender));
""",1)
# A real A body may itself say "展开讲讲" or another UI-looking word. The
# semantic header/body/time/stats exclusions prove structure; an intent-like
# word list must never drop an otherwise verified ordinary follow-up.
_JUNK_LINES=[line for line in FOLLOWUP_THREAD_JS.splitlines() if line.strip().startswith('const junk=')]
if len(_JUNK_LINES)!=1:raise RuntimeError('followup_dom_dependency_changed')
FOLLOWUP_THREAD_JS=FOLLOWUP_THREAD_JS.replace(_JUNK_LINES[0],' const junk=s=>false;',1)
FOLLOWUP_EXPAND_HANDLE_JS=CHOICE_EXPAND_HANDLE_JS.replace(CHOICE_THREAD_JS,FOLLOWUP_THREAD_JS,1)

FOLLOWUP_REPLY_STATE_JS=r"""args=>{
 const thread=("""+FOLLOWUP_THREAD_JS+r""")(args);if(!thread.ok)return thread;
 if(thread.counts.readback_more_count||thread.counts.readback_unsupported_more_count)return {ok:false,reason_code:'followup_thread_incomplete'};
 const selector='[data-e2e="comment-item"]',normal=s=>(s||'').replace(/\s+/gu,''),roots=Array.from(document.querySelectorAll(selector));
 const rendered=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;},visible=e=>{const r=e.getBoundingClientRect();return rendered(e)&&r.bottom>0&&r.top<innerHeight;};
 const profile=a=>{try{const u=new URL(a.getAttribute('href'),location.href);return u.protocol==='https:'&&u.hostname==='www.douyin.com'&&!u.username&&!u.password&&!u.port&&/^\/user\/[^/?#]+$/.test(u.pathname)&&u.pathname!=='/user/self'?u.origin+u.pathname:'';}catch{return '';}};
 const rowIdentity=("""+CHOICE_ROW_IDENTITY_JS+r""");
 """+FOLLOWUP_HELPERS_JS+r"""
 const matches=thread.details.filter(d=>normal(d.command_text)===normal(args.selection_text)&&d.native_comment_id===args.native_comment_id);
 if(matches.length!==1)return {ok:false,reason_code:'followup_candidate_not_unique'};
 const detail=matches[0],selection=roots[detail.comment_index],root=roots[thread.root_index];
 if((args.expected_direct_parent&&!detail.evidence.direct_reply_to_ask)||
   (args.expected_prior_native!=null&&detail.evidence.ask_native_comment_id!==args.expected_prior_native))return {ok:false,reason_code:'followup_parent_changed'};
 const all=Array.from(root.querySelectorAll(selector)).filter(n=>fpPlainBody(n,args.reply,false));
 const b=all.filter(n=>fpIdentity(n,args.reply,false)?.author===args.receiver);
 const matched=b.filter(n=>fpPublic(n)&&fpIdentity(n,args.reply)?.author===args.receiver&&
   (!fpIdentity(n,args.reply)?.target||fpIdentity(n,args.reply)?.target===args.sender)&&
   (n.parentElement.closest(selector)===selection||n.parentElement.closest(selector)===root));
 const spans=Array.from(selection.querySelectorAll('span')),replySpans=spans.map((e,i)=>({e,i})).filter(({e})=>fpPublic(e)&&e.closest(selector)===selection&&e.innerText.trim()==='回复');
 const editors=Array.from(document.querySelectorAll('[contenteditable="true"][role="combobox"]')).filter(visible),editor=editors.length===1?editors[0]:null,composer=editor?editor.closest('.comment-input-container'):null;
 const expected=normal('回复@'+detail.sender_display_name+':'+detail.command_text);
 const headers=composer?Array.from(composer.querySelectorAll('*')).filter(e=>fpPublic(e)&&normal(e.innerText)===expected&&!Array.from(e.children).some(c=>fpPublic(c)&&normal(c.innerText)===expected)):[];
 const inline=!!editor&&editor.closest(selector)===selection,modal=!!editor&&!editor.closest(selector)&&fpActor&&headers.length===1;
 const scope=inline?selection:modal?composer:selection;
 const sends=Array.from(scope.querySelectorAll('"""+REPLY_SEND_SELECTOR+r"""')).filter(visible);
 return {ok:true,detail,root_index:detail.comment_index,evidence_root_index:thread.root_index,current_receiver_verified:fpActor,
  matching_reply_count:all.length===1?matched.length:0,existing_matching_reply_count:all.length,
  reply_readback_quality:matched.length===1&&matched[0].parentElement.closest(selector)===root?'weak_unique_root':null,
  reply_span_index:replySpans.length===1?replySpans[0].i:null,composer_unique:editors.length===1,composer_owned:inline||modal,
  composer_scope:modal?'modal':'inline',composer_text:editor?editor.innerText:null,send_control_count:sends.length};
}"""


def _normal(s):return re.sub(r'\s+','',s)


def _reason(code):
    return code.replace('choice_','followup_',1) if type(code) is str and re.fullmatch('[a-z_]{1,100}',code) else 'followup_unconfirmed'


def _diagnostic(receiver,value):
    previous=getattr(receiver,'last_followup_diagnostic',None)
    if isinstance(previous,dict) and 'lookup_counts' in previous and 'lookup_counts' not in value:
        value['lookup_counts']=previous['lookup_counts']
    receiver.last_followup_diagnostic=value
    receiver.last_followup_diagnostics=value


def _read(receiver,args):
    _choice_checkpoint(receiver)
    current=deepcopy(args);current['observed_at_utc']=datetime.now(timezone.utc).isoformat()
    current.update(identity_fresh=0<=time.monotonic()-getattr(receiver,'_reply_identity_checked_at',-10000)<=120,
        expected_public_number=receiver.expected_number)
    value=receiver.page.evaluate(FOLLOWUP_THREAD_JS,current)
    counts=value.get('counts',{}) if isinstance(value,dict) else {}
    if not isinstance(counts,dict):counts={}
    names={'original_root_count':'root_count','ask_count':'prior_reply_count','selection_count':'followup_candidate_count',
        'choice_sender_row_count':'sender_row_count','choice_receiver_target_row_count':'reply_to_B_count',
        'choice_body_candidate_count':'body_candidate_count','readback_more_count':'more_count',
        'readback_unsupported_more_count':'unsupported_more_count','readback_loaded_row_count':'loaded_row_count',
        'detail_ok':'detail_ok','sender_match_count':'sender_match_count','command_match_count':'command_match_count',
        'visible_comment_count':'visible_comment_count','video_id_count':'video_id_count'}
    safe={names[k]:v for k,v in counts.items() if k in names and type(v) is int and 0<=v<=1000000}
    _diagnostic(receiver,{'stage':'followup_dom_read','reason_code':_reason(value.get('reason_code','ok')) if isinstance(value,dict) else 'followup_structure_unknown',
        'counts':safe})
    if not isinstance(value,dict) or value.get('ok') is not True:
        raise ReceiverDetailError('原线程追问尚未唯一核对',{'reason_code':_reason(value.get('reason_code')) if isinstance(value,dict) else 'followup_unconfirmed'})
    return value


def _expanded(receiver,args):
    value=_read(receiver,args)
    for attempt in range(3):
        counts=value.get('counts',{});more=counts.get('readback_more_count',0)
        if counts.get('readback_unsupported_more_count') or more>1:
            raise ReceiverDetailError('原线程仍有未核对内容',{'reason_code':'followup_expander_unsupported'})
        if not more:return value
        if attempt==2 or value.get('expand_index') is None:
            raise ReceiverDetailError('原线程有界读取未完成',{'reason_code':'followup_expansion_bound'})
        params={'args':deepcopy(args),'kind':value['expand_kind'],'signature':value['expand_signature']}
        params['args'].update(identity_fresh=0<=time.monotonic()-getattr(receiver,'_reply_identity_checked_at',-10000)<=120,
            expected_public_number=receiver.expected_number)
        _choice_checkpoint(receiver);handle=receiver.page.evaluate_handle(FOLLOWUP_EXPAND_HANDLE_JS,params)
        try:
            node=handle.as_element();_choice_checkpoint(receiver)
            if node is None or not node.evaluate('(element,params)=>('+FOLLOWUP_EXPAND_HANDLE_JS+')(params)===element',params):
                raise ReceiverDetailError('追问展开控件改变',{'reason_code':'followup_expander_changed'})
            node.click(timeout=8000)
        finally:handle.dispose()
        previous=counts.get('readback_loaded_row_count',0);deadline=time.monotonic()+4
        while True:
            _choice_checkpoint(receiver);receiver.page.wait_for_timeout(200);value=_read(receiver,args)
            if value.get('counts',{}).get('readback_loaded_row_count',0)>previous:break
            if time.monotonic()>=deadline:
                raise ReceiverDetailError('追问展开没有进展',{'reason_code':'followup_expansion_no_progress'})
    return value


def _args(anchor,candidate):
    root=anchor['root_envelope'];text=anchor['prior_reply_text']
    if (type(text) is not str or not 1<=len(text)<=8000
            or any(type(anchor.get(k)) is not str or not re.fullmatch(r'[a-f0-9]{32}',anchor[k]) for k in ('root_task_id','root_agent_task_id'))
            or root.get('source_mode')!='live_ui' or root.get('evidence',{}).get('real_mention') is not True
            or hashlib.sha256(text.encode()).hexdigest()!=anchor['prior_reply_sha256']
            or any(candidate.get(k)!=root.get(k) for k in ('sender_profile_url','receiver_profile_url','video_id'))
            or (candidate.get('native_comment_id') is not None and
                (type(candidate['native_comment_id']) is not str or not re.fullmatch(r'[1-9][0-9]{4,29}',candidate['native_comment_id'])))
            or type(candidate.get('content')) is not str or not 1<=len(candidate['content'])<=4000):
        raise ValueError('followup_anchor_scope_changed')
    return {'sender':public_profile(root['sender_profile_url']),'receiver':public_profile(root['receiver_profile_url']),
        'video':root['video_id'],'root_text':root['comment_text'],'root_native':root['native_comment_id'],
        'root_reference_id':root['reference_id'],'ask_text':text,'readback_pagination':True,
        'allowed_selections':[unicodedata.normalize('NFKC',_normal(candidate['content'])).lower()]}


def _detail(anchor,args,raw):
    old=raw['evidence'];direct=old['direct_reply_to_ask'] is True
    proof={'source_kind':'conversation_followup','evidence_version':1,'query_only':True,
        'instance_id':anchor.get('instance_id'),'real_mention':False,'sender_verified':True,'receiver_verified':True,
        'video_verified':True,'current_receiver_verified':True,'unique_count':1,'root_chain_verified':True,
        'prior_reply_visible_unique':True,'reply_to_receiver_verified':True,'direct_reply_to_prior':direct,
        'parent_scope':'direct_prior_reply' if direct else 'unique_followup_in_verified_root',
        'sender_profile_url':args['sender'],'receiver_profile_url':args['receiver'],'video_id':args['video'],
        'root_task_id':anchor['root_task_id'],'root_agent_task_id':anchor['root_agent_task_id'],
        'root_reference_id':args['root_reference_id'],'root_comment_text':args['root_text'],
        'root_native_comment_id':old['root_native_comment_id'],'prior_reply_native_comment_id':old['ask_native_comment_id'],
        'prior_reply_text':anchor['prior_reply_text'],'prior_reply_sha256':anchor['prior_reply_sha256'],
        'followup_reference_id':'pending:derive-from-actual-followup','followup_text':raw['command_text'],
        'native_comment_id':raw['native_comment_id'],'observed_at_utc':old['observed_at_utc']}
    return {**{k:raw[k] for k in ('ok','sender_profile_url','receiver_profile_url','video_id','command_text',
        'native_comment_id','source_url','sender_display_name','comment_index')},'evidence':proof}


def _read_followup_reply(receiver,anchor,candidate,*,keep_open=True):
    anchor,candidate=deepcopy(anchor),deepcopy(candidate);args=_args(anchor,candidate)
    _diagnostic(receiver,{'stage':'followup_root_open','reason_code':'checking','counts':{}})
    target={'sender_profile_url':args['sender'],'receiver_profile_url':args['receiver'],'video_id':args['video'],
        'comment_text':args['root_text'],'reference_id':args['root_reference_id'],'native_comment_id':args['root_native']}
    _choice_checkpoint(receiver);receiver.open_original_from_notifications(target,max_candidates=3)
    lookup=getattr(receiver,'last_original_lookup',{})
    if type(lookup) is dict:
        receiver.last_followup_diagnostic['lookup_counts']={k:v for k,v in lookup.items()
            if k in {'candidate_count','checked_count','match_count','final_rechecks','final_verified','unreadable_count','lookup_stage_index'}
            and type(v) is int and 0<=v<=1000000}
    value=_expanded(receiver,args)
    if value.get('ask_missing') or len(value.get('details',[]))!=1:
        raise ReceiverDetailError('追问或既有回执未唯一核对',{'reason_code':'followup_candidate_not_unique'})
    raw=value['details'][0]
    if (_normal(raw['command_text'])!=_normal(candidate['content'])
            or raw['native_comment_id'] not in (None,candidate.get('native_comment_id'))):
        raise ReceiverDetailError('追问正文或身份改变',{'reason_code':'followup_candidate_changed'})
    _choice_checkpoint(receiver)
    result=_detail(anchor,args,raw)
    receiver.last_followup_diagnostic['reason_code']='matched'
    return result


def read_followup_reply(receiver,anchor,candidate,*,keep_open=True):
    try:
        with _readback_viewport(receiver):
            return _read_followup_reply(receiver,anchor,candidate,keep_open=keep_open)
    except ReceiverInterventionRequired:
        value=getattr(receiver,'last_followup_diagnostic',{})
        value.update(reason_code='followup_intervention');_diagnostic(receiver,value)
        raise
    except Exception as error:
        value=getattr(receiver,'last_followup_diagnostic',{})
        if not isinstance(value,dict):value={}
        code=getattr(error,'diagnostics',{}).get('reason_code') if isinstance(getattr(error,'diagnostics',None),dict) else None
        value.update(reason_code=_reason(code),exception_type=type(error).__name__ if type(error).__name__ in
            {'ReceiverDetailError','ReceiverPageError','ValueError','TypeError','KeyError','TimeoutError'} else 'local_error')
        _diagnostic(receiver,value)
        if _choice_should_stop(receiver):
            value['reason_code']='followup_stopped'
        if isinstance(error,(ReceiverDetailError,ReceiverPageError)):raise
        raise ReceiverDetailError('原线程追问读取尚未核对',{'reason_code':'followup_unconfirmed'}) from None


def _target_args(target):
    proof=target.get('clarification_evidence') or target.get('followup_evidence')
    env={k:target[k] for k in ('command_id','reference_id','reference_quality','native_comment_id',
        'sender_profile_url','receiver_profile_url','video_id','comment_text','source_mode')}
    env['evidence']=proof
    if target.get('clarification_evidence') is not None:
        from app.mention_agent.clarification_evidence import validate_clarification_envelope
        validate_clarification_envelope(env)
    else:
        validate_followup_envelope(env)
    root={'sender_profile_url':target['sender_profile_url'],'receiver_profile_url':target['receiver_profile_url'],
        'video_id':target['video_id'],'comment_text':proof['root_comment_text'],'reference_id':proof['root_reference_id'],
        'native_comment_id':proof['root_native_comment_id'],'source_mode':'live_ui','evidence':{'real_mention':True}}
    anchor={'root_task_id':proof['root_task_id'],'root_agent_task_id':proof['root_agent_task_id'],
        'root_envelope':root,'prior_reply_text':proof['prior_reply_text'],'prior_reply_sha256':proof['prior_reply_sha256'],
        'instance_id':proof['instance_id']}
    candidate={k:target[k] for k in ('sender_profile_url','receiver_profile_url','video_id','native_comment_id')}
    candidate['content']=target['comment_text'];args=_args(anchor,candidate)
    args.update(selection_text=target['comment_text'],native_comment_id=target['native_comment_id'],
        expected_direct_parent=proof['direct_reply_to_prior'],expected_prior_native=proof['prior_reply_native_comment_id'])
    return anchor,candidate,args


def followup_reply_state(receiver,target,text):
    with _readback_viewport(receiver):
        return _followup_reply_state(receiver,target,text)


def _followup_reply_state(receiver,target,text):
    from app.mention_agent.publisher import ReplyPreflightError
    _,_,args=_target_args(target);_choice_checkpoint(receiver)
    args.update(reply=text,identity_fresh=0<=time.monotonic()-getattr(receiver,'_reply_identity_checked_at',-10000)<=120,
        expected_public_number=receiver.expected_number,observed_at_utc=datetime.now(timezone.utc).isoformat())
    value=receiver.page.evaluate(FOLLOWUP_REPLY_STATE_JS,args)
    if not isinstance(value,dict) or value.get('ok') is not True:raise ReplyPreflightError(_reason(value.get('reason_code')) if isinstance(value,dict) else 'followup_unconfirmed')
    if value.get('current_receiver_verified') is not True:raise ReplyPreflightError('current_actor_not_verified')
    return value


def prepare_followup_reply(receiver,target,text):
    with _readback_viewport(receiver):
        return _prepare_followup_reply(receiver,target,text)


def _prepare_followup_reply(receiver,target,text):
    from app.mention_agent.publisher import ReplyPreflightError
    anchor,candidate,args=_target_args(target)
    detail=read_followup_reply(receiver,anchor,candidate,keep_open=True)
    def rebuild(actual):
        binding={'binding_id':target['binding_id'],'instance_id':target['instance_id'],
            'sender_profile_url':target['sender_profile_url'],'receiver_profile_url':target['receiver_profile_url'],'active':True}
        if target.get('clarification_evidence') is not None:
            from app.mention_agent.clarification_evidence import rebuild_clarification_envelope
            return rebuild_clarification_envelope(actual,binding,target['clarification_evidence'])
        return make_followup_envelope(actual,binding)
    fresh=rebuild(detail)
    if fresh['reference_id']!=target['reference_id'] or fresh['native_comment_id']!=target['native_comment_id']:
        raise ReplyPreflightError('followup_frozen_target_changed')
    value=followup_reply_state(receiver,target,text)
    if value['existing_matching_reply_count']:raise ReplyPreflightError('existing_matching_reply')
    if value['reply_span_index'] is None:raise ReplyPreflightError('reply_control_not_unique')
    receiver.page.locator('[data-e2e="comment-item"]').nth(value['root_index']).locator('span').nth(value['reply_span_index']).click(timeout=8000)
    value=followup_reply_state(receiver,target,text)
    if not value['composer_unique'] or not value['composer_owned'] or value['composer_text'].strip():
        raise ReplyPreflightError('composer_parent_not_verified')
    receiver.page.locator('[contenteditable="true"][role="combobox"]').and_(receiver.page.locator(':visible')).fill(text,timeout=8000)
    value=followup_reply_state(receiver,target,text);receiver._validate_reply_draft(value,text)
    # The reservation validator must receive the last ACTUAL DOM proof,
    # including changed parent/native quality. A frozen old proof is not its
    # own fresh verification, even if text/reference stayed the same.
    actual=_detail(anchor,args,value['detail'])
    fresh=rebuild(actual)
    return {k:fresh[k] for k in ('sender_profile_url','receiver_profile_url','video_id','comment_text','reference_id','native_comment_id')}|fresh['evidence']
