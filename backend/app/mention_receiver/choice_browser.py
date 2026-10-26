"""Bounded normal-page observation/preparation for one frozen library choice.

No submission method lives here. Persistent choice freshness/authorization and
the durable publisher gate remain the caller's responsibility. Only rendered
public DOM and the existing notification adapter are used.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from app.mention_agent.contracts import Binding, validate_envelope
from .browser import DETAIL_JS, REPLY_SEND_SELECTOR, SELF_URL, ReceiverPageError, ReceiverDetailError, ReceiverInterventionRequired


CHOICE_OBSERVATION_BASE = Path(__file__).resolve().parents[1] / 'storage' / 'assistant'
CHOICE_EXPAND_SELECTOR = 'button,[data-e2e="video-comment-more"]'
CHOICE_EXPAND_WAIT_SECONDS = 4.0
CHOICE_READBACK_MAX_EXPANSIONS = 2
_READBACK_LAYOUT_COUNTS = frozenset({
    'readback_native_button_count','readback_legacy_control_count','readback_span_control_count',
    'readback_root_owned_more_count','readback_parent_reply_container_count',
    'readback_parent_owned_by_root_count','readback_reply_container_ancestor_count',
    'readback_direct_root_more_count','readback_grandparent_root_more_count',
    'readback_inside_native_button_count','readback_contains_native_button_count',
    'readback_label_plain_more_count','readback_label_numeric_replies_count',
    'readback_label_more_replies_count','readback_label_all_replies_count','readback_label_bare_replies_count'})
_READBACK_MATCH_COUNTS = frozenset({
    'readback_current_receiver_verified','readback_b_author_row_count','readback_b_public_author_row_count',
    'readback_own_text_equal_count','readback_b_exact_text_equal_count','readback_b_visible_body_match_count',
    'readback_b_direct_selected_parent_count','readback_b_flat_root_parent_count',
    'readback_b_to_sender_header_count','readback_b_plain_body_match_count',
    'readback_root_public_count','readback_selection_public_count','readback_ask_public_count',
    'readback_root_semantic_sender_count','readback_selection_semantic_sender_count',
    'readback_ask_semantic_receiver_count','readback_selection_to_receiver_count'})
_READBACK_DURABLE_COUNTS = frozenset({'matching_reply_count','existing_matching_reply_count',
    'strict_matching_reply_count','weak_matching_reply_count','verified_root_count',
    'verified_selection_count','selected_source_count'})
_READBACK_EQUAL_AUTHOR_COUNTS = frozenset({
    'readback_equal_identity_missing_count','readback_equal_identity_B_count','readback_equal_identity_other_count',
    'readback_equal_avatar_region_count','readback_equal_header_region_count',
    'readback_equal_avatar_link_count','readback_equal_title_link_count',
    'readback_equal_avatar_B_count','readback_equal_avatar_A_count','readback_equal_avatar_self_count',
    'readback_equal_avatar_uncanonical_count','readback_equal_first_title_B_count',
    'readback_equal_first_title_A_count','readback_equal_first_title_self_count',
    'readback_equal_first_title_uncanonical_count','readback_equal_avatar_title_same_canonical_count',
    'readback_equal_avatar_title_both_self_count','readback_equal_target_title_A_count',
    'readback_equal_B_mention_count','readback_equal_parent_root_count','readback_equal_parent_selection_count',
    'readback_equal_parent_ask_count','readback_equal_parent_other_count','readback_equal_public_row_count',
    'readback_equal_body_interval_count','readback_equal_header_three_parts_count',
    'readback_equal_header_arrow_shape_count','readback_equal_header_arrow_public_count',
    'readback_equal_avatar_any_anchor_count','readback_equal_avatar_href_element_count',
    'readback_equal_row_css_public_count','readback_noavatar_identity_count','readback_noavatar_public_count',
    'readback_scroll_candidate_count'})
_READBACK_LOCAL_COUNTS = frozenset({'readback_scroll_count'})
_OBSERVATION_COMPONENT = 'DouyinMind.choice_observation.v1'
_OBSERVATION_STAGES = frozenset({'read_begin','root_open','root_identity','root_notifications','root_detail',
    'thread_read','expand','expand_wait','read_finished','failure','readback'})
_OBSERVATION_REASONS = frozenset({'started','ok','no_selection','selection_observed','expansion_started',
    'choice_receiver_mismatch','choice_identity_not_ready','choice_root_notification_not_unique',
    'choice_original_root_detail_unavailable','choice_original_root_changed','choice_unexpected_page_origin',
    'choice_video_route_changed','choice_original_root_not_unique','choice_visible_window_too_large',
    'choice_prompt_not_unique','choice_prompt_missing','choice_selection_native_id_conflict',
    'choice_prompt_native_id_conflict','choice_selection_not_unique','choice_structure_unknown',
    'receiver_detail_error','receiver_page_error','intervention_required','unclassified_choice_failure',
    'choice_run_stopped','choice_expander_changed','expansion_wait_complete','expansion_wait_timeout',
    # The shared opener already emits only these fixed codes. Preserve them
    # instead of hiding a root lookup failure behind receiver_detail_error.
    'invalid_original_target','native_correlation_requires_source_id','receiver_binding_mismatch',
    'original_lookup_stopped','original_identity_not_ready','original_notifications_unavailable',
    'original_notification_not_found','original_notification_candidates_exceed_limit',
    'original_notification_not_unique','original_notification_changed','original_candidate_unreadable',
    'original_target_not_unique','original_target_changed',
    'original_native_semantic_ambiguous','original_lookup_unavailable',
    'choice_readback_complete','choice_readback_bound_exhausted','choice_readback_no_progress',
    'choice_readback_expander_ambiguous','choice_readback_expander_unsupported',
    'choice_readback_reply_not_visible','choice_readback_duplicate_reply','choice_readback_unavailable'})
_OBSERVATION_COUNTS = frozenset({'notice_count','root_notice_match_count','visible_comment_count',
    'sender_match_count','command_match_count','video_id_count','id_conflict','detail_ok',
    'original_root_count','descendant_count','ask_count','selection_count','expand_control_count',
    'choice_sender_row_count','choice_single_body_row_count','choice_allowed_body_row_count',
    'choice_receiver_target_row_count','expand_attempt','expand_available','ask_missing',
    'wait_read_count','wait_elapsed_ms','choice_metadata_block_count','choice_body_candidate_count',
    'candidate_limit','candidate_count','checked_count','match_count','unreadable_count',
    'final_rechecks','final_verified','snapshot_changes','page_open_count','lookup_stage_index',
    'readback_expansion_count','readback_progress_count','readback_more_count',
    'readback_unsupported_more_count','readback_loaded_row_count','readback_bound_exhausted'}) | _READBACK_LAYOUT_COUNTS | _READBACK_MATCH_COUNTS | _READBACK_DURABLE_COUNTS | _READBACK_EQUAL_AUTHOR_COUNTS | _READBACK_LOCAL_COUNTS


class _ChoiceRunStopped(ReceiverPageError):
    pass


def _choice_should_stop(receiver):
    callback = getattr(receiver, 'choice_should_stop', None)
    if callback is None:
        return False  # Legacy standalone adapters have no control callback.
    try:
        return not callable(callback) or bool(callback())
    except Exception:
        return True


def _choice_checkpoint(receiver):
    if _choice_should_stop(receiver):
        raise _ChoiceRunStopped('选库回看已停止')
    receiver.check_intervention()
    if _choice_should_stop(receiver):
        raise _ChoiceRunStopped('选库回看已停止')


def _observation_regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.resolve() != path
            or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
        raise ValueError('unsafe_observation_file')
    return info


def _choice_observation(args, stage, reason_code='ok', **counts):
    """Opt-in, bounded append-only fixed-code diagnostics; never affects work.

    No browser state/text, URL, token or exception message reaches this writer.
    Missing/invalid opt-in and any diagnostic I/O error leave business behavior
    unchanged. Existing files need our exact header before any append.
    """
    try:
        choice = args.get('choice_id')
        if (stage not in _OBSERVATION_STAGES or not isinstance(choice, str)
                or not re.fullmatch(r'[a-f0-9]{32,64}', choice)):
            return
        base = CHOICE_OBSERVATION_BASE
        if not base.is_absolute() or base.resolve() != base:
            return
        for directory in (base, *base.parents):
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
                return
        opt_in = base / 'choice-observation-enabled.json'
        before = _observation_regular(opt_in)
        if not 1 <= before.st_size <= 256:
            return
        with opt_in.open('rb') as source:
            opened = os.fstat(source.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                return
            enabled = json.loads(source.read(257))
        if enabled != {'choice_id': choice}:
            return
        folder = base / 'choice-observations'
        folder.mkdir(exist_ok=True)
        info = folder.lstat()
        if (not stat.S_ISDIR(info.st_mode) or folder.resolve() != folder or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            return
        path = folder / (choice + '.jsonl')
        header = (json.dumps({'component': _OBSERVATION_COMPONENT, 'choice_id': choice},
                            sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
        safe_counts = {key: value for key, value in counts.items()
                       if key in _OBSERVATION_COUNTS and type(value) is int and 0 <= value <= 1000000}
        event = {'at_utc': datetime.now(timezone.utc).isoformat(), 'stage': stage,
                 'reason_code': reason_code if reason_code in _OBSERVATION_REASONS else 'unclassified_choice_failure',
                 'counts': safe_counts}
        content = (json.dumps(event, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
        descriptor = None
        try:
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
                expected = os.fstat(descriptor)
                new = True
            except FileExistsError:
                expected = _observation_regular(path)
                descriptor = os.open(path, os.O_RDWR | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
                new = False
            actual = os.fstat(descriptor)
            current = _observation_regular(path)
            if ((expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino)
                    or (current.st_dev, current.st_ino) != (actual.st_dev, actual.st_ino)
                    or actual.st_nlink != 1 or not stat.S_ISREG(actual.st_mode) or actual.st_size > 131072):
                return
            if not new and os.read(descriptor, len(header)) != header:
                return
            os.write(descriptor, (header if new else b'') + content)
            os.fsync(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    except Exception:
        return  # Optional observations never authorize, reject or retry work.


def _failure_observation(args, error):
    reason = ('choice_run_stopped' if isinstance(error, _ChoiceRunStopped) else
              'intervention_required' if isinstance(error, ReceiverInterventionRequired) else
              'receiver_detail_error' if isinstance(error, ReceiverDetailError) else
              'receiver_page_error' if isinstance(error, ReceiverPageError) else 'unclassified_choice_failure')
    diagnostics = getattr(error, 'diagnostics', {})
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    fixed = diagnostics.get('reason_code')
    if isinstance(fixed, str) and fixed in _OBSERVATION_REASONS:
        reason = fixed
    counts = {name: diagnostics[name] for name in _OBSERVATION_COUNTS if name in diagnostics}
    videos = diagnostics.get('visible_video_ids')
    if isinstance(videos, list):
        counts['video_id_count'] = len(videos)
    _choice_observation(args, 'failure', reason, **counts)


# A real public reply row may say A [CSS triangle] B, without an @ chip.
# Only the semantic avatar/header agree on its author; body links never do.
# The function is shared by selection intake and conservative reply readback.
CHOICE_ROW_IDENTITY_JS = r"""(n,shown,requirePublicArrow=true)=>{
 const local=e=>e.closest(selector)===n, own=(css)=>Array.from(n.querySelectorAll(css)).filter(e=>local(e)&&shown(e));
 const avatarNodes=own('.comment-item-avatar'),headers=own('.comment-item-info-wrap');
 const hasSemantic=Array.from(n.querySelectorAll('.comment-item-avatar,.comment-item-info-wrap')).some(local);
 if(hasSemantic){
   if(avatarNodes.length!==1||headers.length!==1)return null;
   const avatars=Array.from(avatarNodes[0].querySelectorAll('a[href]')).filter(a=>local(a)&&shown(a)&&a.querySelector('img')&&profile(a));
   const names=Array.from(headers[0].querySelectorAll('a[href]')).filter(a=>local(a)&&shown(a)&&profile(a)&&
     a.innerText.trim()&&!a.innerText.trim().startsWith('@')&&a.querySelector('[data-click-from="title"]'));
   if(avatars.length!==1||![1,2].includes(names.length)||profile(avatars[0])!==profile(names[0]))return null;
   let target=null;
   if(names.length===2){
     const author=names[0],to=names[1],group=author.parentElement;
     const parts=Array.from(group.children).filter(shown);
     if(to.parentElement!==group||parts.length!==3||parts[0]!==author||parts[2]!==to)return null;
     const arrow=parts[1],style=getComputedStyle(arrow);
     if(arrow.tagName!=='DIV'||arrow.children.length||arrow.innerText.trim()||
       style.borderLeftStyle!=='solid'||parseFloat(style.borderLeftWidth)<=0||
       parseFloat(style.borderTopWidth)<=0||parseFloat(style.borderBottomWidth)<=0||
       parseFloat(style.borderRightWidth)!==0||profile(author)===profile(to))return null;
     if(requirePublicArrow&&(style.visibility!=='visible'||parseFloat(style.opacity)===0||
       style.borderLeftColor==='transparent'||/rgba\([^)]*,\s*0(?:\.0+)?\s*\)$/.test(style.borderLeftColor)))return null;
     target=profile(to);
   }
   return {author:profile(names[0]),authorLink:names[0],target,semantic:true};
 }
 const names=Array.from(n.querySelectorAll('a[href]')).filter(a=>local(a)&&shown(a)&&profile(a)&&
   a.innerText.trim()&&!a.innerText.trim().startsWith('@'));
 const authors=new Set(names.map(profile));
 return authors.size===1?{author:profile(names[0]),authorLink:names[0],target:null,semantic:false}:null;
}"""


CHOICE_THREAD_JS = r"""args => {
 const here=new URL(location.href),route=/^\/video\/(\d{5,30})\/?$/.exec(here.pathname);
 if(here.protocol!=='https:'||here.hostname!=='www.douyin.com'||here.username||here.password||here.port||
    (here.pathname!=='/user/self'&&!route))return {ok:false,reason_code:'choice_unexpected_page_origin'};
 const modal=here.searchParams.get('modal_id');
 if((route&&route[1]!==args.video)||(modal!==null&&modal!==args.video))
   return {ok:false,reason_code:'choice_video_route_changed'};
 const selector='[data-e2e="comment-item"]', normal=s=>(s||'').replace(/\s+/gu,''),
   key=s=>normal(s).normalize('NFKC').toLowerCase();
 const rendered=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
 const visible=e=>{const r=e.getBoundingClientRect();return rendered(e)&&r.bottom>0&&r.top<innerHeight;};
 const profile=a=>{try{const u=new URL(a.getAttribute('href'),location.href);return u.protocol==='https:'&&
   u.hostname==='www.douyin.com'&&!u.username&&!u.password&&!u.port&&/^\/user\/[^/?#]+$/.test(u.pathname)&&
   u.pathname!=='/user/self'?u.origin+u.pathname:'';}catch{return '';}};
 const roots=Array.from(document.querySelectorAll(selector));
 const links=n=>Array.from(n.querySelectorAll('a[href]')).filter(a=>a.closest(selector)===n&&visible(a));
 const rowIdentity=(""" + CHOICE_ROW_IDENTITY_JS + r""");
 const ownerLinks=n=>{const value=rowIdentity(n,visible);return value?[value.authorLink]:[];};
 const owners=n=>new Set(ownerLinks(n).map(profile));
 const ownText=(n,wanted)=>Array.from(n.querySelectorAll('div,p,span')).some(e=>visible(e)&&
   e.closest(selector)===n&&!e.querySelector(selector)&&normal(e.innerText)===normal(wanted));
 const original=roots.filter(n=>visible(n)&&owners(n).size===1&&owners(n).has(args.sender)&&
   links(n).some(a=>a.innerText.trim().startsWith('@')&&profile(a)===args.receiver&&
     (function(){let p=a;for(let i=0;p&&p!==n&&i<12;i++,p=p.parentElement){
       if(p.closest(selector)!==n||p.querySelector(selector))break;
       if(visible(p)&&normal(p.innerText)===normal(args.root_text))return true;}return false;})()));
 const detail=(""" + DETAIL_JS + r""")({sender:args.sender,receiver:args.receiver,wanted:args.root_text});
 const counts={visible_comment_count:roots.filter(visible).length,original_root_count:original.length,detail_ok:detail.ok?1:0,
   sender_match_count:detail.diagnostics?.sender_match_count,command_match_count:detail.diagnostics?.command_match_count,
   video_id_count:detail.diagnostics?.visible_video_ids?.length};
 if(!detail.ok||original.length!==1)return {ok:false,reason_code:'choice_original_root_not_unique',counts};
 if(detail.video_id!==args.video||detail.native_comment_id!==args.root_native)
   return {ok:false,reason_code:'choice_original_root_changed',counts};
 const root=original[0], descendants=roots.filter(n=>n!==root&&root.contains(n));
 counts.descendant_count=descendants.length;
 if(descendants.length>500)return {ok:false,reason_code:'choice_visible_window_too_large',counts};
 const more=Array.from(document.querySelectorAll('""" + CHOICE_EXPAND_SELECTOR + r"""'));
 const expandText=/^(?:展开|查看)(?:(?:全部|更多)|(?:\d+(?:\.\d+)?[万wW]?条))?回复$/;
 // Readback is a separate, stricter purpose. A known but unsupported/ambiguous
 // More stays incomplete evidence; it is never ignored to claim uniqueness.
 const readback=args.readback_pagination===true;
 const publicControl=e=>{
   if(!rendered(e))return false;
   for(let p=e;p&&p.nodeType===1;p=p.parentElement){const s=getComputedStyle(p);
     if(s.visibility!=='visible'||s.display==='none'||parseFloat(s.opacity)===0)return false;}
   return true;
 };
 const knownMore=readback?more.filter(e=>publicControl(e)&&root.contains(e)&&
   (expandText.test(normal(e.innerText))||normal(e.innerText)==='展开更多')):[];
 const initialNumeric=e=>descendants.length===0&&e.tagName==='BUTTON'&&e.closest(selector)===root&&
   e.parentElement?.closest(selector)===root&&e.parentElement.parentElement===root&&
   /^(?:展开|查看)\d+(?:\.\d+)?[万wW]?条回复$/.test(normal(e.innerText));
 const expand=readback?knownMore.filter(e=>e.tagName==='BUTTON'&&e.closest(selector)===root&&
   ((e.parentElement?.matches('.replyContainer')&&e.parentElement.closest(selector)===root)||initialNumeric(e))):
   more.filter(e=>visible(e)&&e.closest(selector)===root&&expandText.test(normal(e.innerText)));
 const expandIndex=expand.length===1?more.indexOf(expand[0]):null;
 const expandKind=expand.length===1?(expand[0].tagName==='BUTTON'?'native_button':'legacy_e2e'):null;
 const expandSignature=expand.length===1?expandKind+':'+normal(expand[0].innerText):null;
 counts.expand_control_count=expand.length;
 if(readback)Object.assign(counts,{readback_more_count:knownMore.length,
   readback_unsupported_more_count:knownMore.length-expand.length,readback_loaded_row_count:descendants.length});
 if(readback){
   const count=predicate=>knownMore.filter(predicate).length;
   // Mechanical, fixed-enum counts only. No tag/label/class/DOM text leaves
   // the page. These observations cannot relax the expander's scope gate.
   Object.assign(counts,{
     readback_native_button_count:count(e=>e.tagName==='BUTTON'),
     readback_legacy_control_count:count(e=>e.tagName!=='BUTTON'&&e.matches('[data-e2e="video-comment-more"]')),
     readback_span_control_count:count(e=>e.tagName==='SPAN'),
     readback_root_owned_more_count:count(e=>e.closest(selector)===root),
     readback_parent_reply_container_count:count(e=>e.parentElement?.matches('.replyContainer')),
     readback_parent_owned_by_root_count:count(e=>e.parentElement?.closest(selector)===root),
     readback_reply_container_ancestor_count:count(e=>!!e.closest('.replyContainer')),
     readback_direct_root_more_count:count(e=>e.parentElement===root),
     readback_grandparent_root_more_count:count(e=>e.parentElement?.parentElement===root),
     readback_inside_native_button_count:count(e=>e.tagName!=='BUTTON'&&!!e.closest('button')),
     readback_contains_native_button_count:count(e=>!!e.querySelector('button')),
     readback_label_plain_more_count:count(e=>normal(e.innerText)==='展开更多'),
     readback_label_numeric_replies_count:count(e=>/^(?:展开|查看)\d+(?:\.\d+)?[万wW]?条回复$/.test(normal(e.innerText))),
     readback_label_more_replies_count:count(e=>/^(?:展开|查看)更多回复$/.test(normal(e.innerText))),
     readback_label_all_replies_count:count(e=>/^(?:展开|查看)全部回复$/.test(normal(e.innerText))),
     readback_label_bare_replies_count:count(e=>/^(?:展开|查看)回复$/.test(normal(e.innerText)))
   });
 }
 const asks=descendants.filter(n=>visible(n)&&owners(n).size===1&&owners(n).has(args.receiver)&&ownText(n,args.ask_text));
 counts.ask_count=asks.length;
 if(asks.length>1)return {ok:false,reason_code:'choice_prompt_not_unique',counts};
 if(!asks.length)return {ok:true,ask_missing:true,details:[],expand_index:expandIndex,expand_kind:expandKind,
   expand_signature:expandSignature,root_index:roots.indexOf(root),counts};
 const ask=asks[0], allowed=new Set(args.allowed_selections||[]), observed=[];
 Object.assign(counts,{choice_sender_row_count:0,choice_single_body_row_count:0,
   choice_allowed_body_row_count:0,choice_receiver_target_row_count:0,
   choice_metadata_block_count:0,choice_body_candidate_count:0});
 const native=n=>{const ids=new Set(['data-comment-id','data-e2e-comment-id'].map(k=>n.getAttribute(k))
   .filter(v=>/^\d{5,30}$/.test(v||'')));return {value:ids.size===1?Array.from(ids)[0]:null,conflict:ids.size>1};};
 const timeRegion=item=>{
   const own=e=>e.closest(selector)===item;
   const headers=Array.from(item.querySelectorAll('.comment-item-info-wrap')).filter(e=>own(e)&&visible(e));
   const stats=Array.from(item.querySelectorAll('.comment-item-stats-container')).filter(e=>own(e)&&visible(e));
   if(headers.length!==1||stats.length!==1)return null;
   const header=headers[0],group=header.parentElement;
   let block=stats[0];
   while(block.parentElement&&block.parentElement!==group&&own(block))block=block.parentElement;
   if(block.parentElement!==group||!own(block))return null;
   const metadata=block.previousElementSibling,body=metadata?.previousElementSibling;
   // Observed public layout: semantic header, actual body, standalone time/IP
   // text, own stats. Do not remove time-like words from inside the body or
   // ignore an extra independent body merely because another matches a name.
   if(!metadata||!body||body===header||body.previousElementSibling!==header||
      metadata.tagName!=='DIV'||!own(metadata)||!visible(metadata)||!visible(body)||
      !normal(body.innerText)||metadata.querySelector('a,button,svg,img,time,[contenteditable]'))return null;
   const stamp=/^(?:刚刚|\d+(?:秒|分钟|小时|天|周|个月|年)前|\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?)(?:·[\p{L}]{1,32})?$/u;
   return stamp.test(normal(metadata.innerText))?metadata:null;
 };
 const excluded=(e,metadata)=>{
   if(metadata?.contains(e))return true;
   // A broad content wrapper can also contain its sibling header/stats. Do
   // not reject a genuine body just because an ancestor contains their SVGs.
   if(e.querySelector('svg,button,[role="button"],time'))return true;
   for(let p=e;p&&p!==e.closest(selector);p=p.parentElement){
     if(p.matches('.comment-item-avatar,.comment-item-info-wrap,.comment-item-stats-container'))return true;
     if(p.matches('button,[role="button"],svg,time,a[href]')&&!(p.matches('a[href]')&&
       (p.innerText||'').trim().startsWith('@')&&profile(p)))return true;
     if(p.querySelector('svg,button,[role="button"]')&&/^\s*\d+(?:\.\d+)?[万wW]?\s*$/.test(p.innerText||''))return true;
     if(p===e&&Array.from(p.querySelectorAll('a[href]')).some(a=>a.closest(selector)===e.closest(selector)&&
       visible(a)&&profile(a)&&a.innerText.trim()&&!a.innerText.trim().startsWith('@')))return true;
   }return false;
 };
 const junk=s=>/^(回复|收起|展开.*|查看.*回复|共\d+条回复|\d+(?:秒|分钟|小时|天|周|个月|年)前|\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?|置顶|作者|删除)$/.test(s);
 for(const item of descendants){
   if(!visible(item)||owners(item).size!==1||!owners(item).has(args.sender))continue;
   counts.choice_sender_row_count++;
   const identity=rowIdentity(item,visible);
   const metadata=timeRegion(item);
   if(metadata)counts.choice_metadata_block_count++;
   const direct=item.parentElement?.closest(selector)===ask;
   if(direct&&identity.semantic&&identity.target&&identity.target!==args.receiver)continue;
   const receiverLabels=links(item).filter(a=>a.innerText.trim().startsWith('@')&&profile(a)===args.receiver)
     .map(a=>a.innerText.trim());
   const prefixLabels=receiverLabels.filter(label=>Array.from(item.querySelectorAll('div,p,span')).some(e=>
     visible(e)&&e.closest(selector)===item&&!e.querySelector(selector)&&
     ['回复'+normal(label),'回复'+normal(label)+':','回复'+normal(label)+'：'].includes(normal(e.innerText))));
   let nodes=Array.from(item.querySelectorAll('div,p,span')).filter(e=>visible(e)&&e.closest(selector)===item&&
     !e.querySelector(selector)&&!excluded(e,metadata)&&normal(e.innerText)&&!junk(normal(e.innerText)));
   // Keep maximal safe body containers, never a leaf like-count inside one.
   nodes=nodes.filter(e=>!nodes.some(parent=>parent!==e&&parent.contains(e)));
   nodes=nodes.filter(e=>!prefixLabels.some(label=>['回复'+normal(label),'回复'+normal(label)+':','回复'+normal(label)+'：'].includes(normal(e.innerText))));
   counts.choice_body_candidate_count+=nodes.length;
   // Two independent text blocks (e.g. unrelated body plus a bare like 1) are
   // ambiguous, even when one happens to equal an allowed numeric option.
   if(nodes.length!==1)continue;
   counts.choice_single_body_row_count++;
   const body=nodes[0].innerText.trim();let value=body,label='';
   if(/^[0-9]+$/.test(key(body))&&[nodes[0].previousElementSibling,nodes[0].nextElementSibling].some(e=>e&&
     (e.matches('svg,button,[role="button"]')||e.querySelector('svg'))))continue;
   if(!allowed.has(key(value))){
     const possible=receiverLabels.filter(l=>{
       const n=normal(body),p='回复'+normal(l),direct=normal(l);
       return n.startsWith(p+':')||n.startsWith(p+'：')||n.startsWith(direct);});
     if(possible.length!==1)continue;
     label=possible[0];const n=normal(body),p='回复'+normal(label);
     value=n.startsWith(p+':')||n.startsWith(p+'：')?n.slice(p.length+1):n.slice(normal(label).length).replace(/^[:：]/u,'');
     if(!allowed.has(key(value)))continue;
   }
   counts.choice_allowed_body_row_count++;
   const canonicalReplyToB=identity.semantic?identity.target===args.receiver:!!label||prefixLabels.length===1;
   if(!direct&&!canonicalReplyToB)continue;
   counts.choice_receiver_target_row_count++;
   const id=native(item);if(id.conflict)return {ok:false,reason_code:'choice_selection_native_id_conflict',counts};
   const askId=native(ask);if(askId.conflict)return {ok:false,reason_code:'choice_prompt_native_id_conflict',counts};
   const evidence={source_kind:'library_choice_reply',evidence_version:1,choice_id:args.choice_id,instance_id:args.instance_id,
     real_mention:false, // A rendered reply-to label is not a new user @ mention.
     sender_verified:true,receiver_verified:true,video_verified:true,unique_count:1,
     direct_reply_to_ask:direct,root_chain_verified:true,
     parent_scope:direct?'verified_ask_parent':'unique_pending_choice_in_verified_root',
     sender_profile_url:args.sender,receiver_profile_url:args.receiver,video_id:args.video,
     root_reference_id:args.root_reference_id,root_comment_text:args.root_text,ask_ref:args.ask_ref,
     ask_reply_ref:args.ask_reply_ref,ask_text:args.ask_text,selection_reference_id:'pending:derive-from-public-selection',
     selection_text:body,observed_at_utc:args.observed_at_utc,unique_pending_choice:true,
     ask_visible_unique:true,receiver_reply_target_verified:direct||canonicalReplyToB,
     native_comment_id:id.value,root_native_comment_id:detail.native_comment_id,ask_native_comment_id:askId.value};
   if(label){evidence.selection_value=value;evidence.receiver_reply_label=label;}
   observed.push({ok:true,unique_count:1,sender_profile_url:args.sender,receiver_profile_url:args.receiver,
     sender_display_name:ownerLinks(item)[0].innerText.trim(),video_id:args.video,
     source_url:'https://www.douyin.com/video/'+args.video,command_text:body,native_comment_id:id.value,
     native_comment_id_conflict:false,evidence,comment_index:roots.indexOf(item)});
 }
 counts.selection_count=observed.length;
 if(observed.length>1)return {ok:false,reason_code:'choice_selection_not_unique',counts};
 return {ok:true,root_index:roots.indexOf(root),ask_index:roots.indexOf(ask),detail,details:observed,
   expand_index:expandIndex,expand_kind:expandKind,expand_signature:expandSignature,ask_missing:false,counts};
}"""


CHOICE_EXPAND_HANDLE_JS = r"""params => {
 const state=(""" + CHOICE_THREAD_JS + r""")(params.args);
 if(!state.ok||(params.args.readback_pagination!==true&&state.details.length)||state.expand_index===null||state.expand_kind!==params.kind||
    state.expand_signature!==params.signature)return null;
 const selector='[data-e2e="comment-item"]',root=document.querySelectorAll(selector)[state.root_index];
 const node=document.querySelectorAll('""" + CHOICE_EXPAND_SELECTOR + r"""')[state.expand_index];
 return node&&node.closest(selector)===root?node:null;
}"""


CHOICE_REPLY_STATE_JS = r"""args => {
 const thread=(""" + CHOICE_THREAD_JS + r""")(args);
 if(!thread.ok)return thread;
 const normal=s=>(s||'').replace(/\s+/gu,''),selector='[data-e2e="comment-item"]';
 const rendered=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
 const visible=e=>{const r=e.getBoundingClientRect();return rendered(e)&&r.bottom>0&&r.top<innerHeight;};
 const profile=a=>{try{const u=new URL(a.getAttribute('href'),location.href);return u.protocol==='https:'&&
   u.hostname==='www.douyin.com'&&!u.username&&!u.password&&!u.port&&/^\/user\/[^/?#]+$/.test(u.pathname)&&
   u.pathname!=='/user/self'?u.origin+u.pathname:'';}catch{return '';}};
 const matches=thread.details.filter(d=>normal(d.command_text)===normal(args.selection_text)&&d.native_comment_id===args.native_comment_id);
 if(matches.length!==1)return {ok:false,reason_code:'choice_selection_not_unique'};
 const detail=matches[0],roots=Array.from(document.querySelectorAll(selector)),selection=roots[detail.comment_index],root=roots[thread.root_index];
 if((args.expected_direct_parent===true&&!detail.evidence.direct_reply_to_ask)||
    (args.expected_ask_native!=null&&detail.evidence.ask_native_comment_id!==args.expected_ask_native))
   return {ok:false,reason_code:'choice_frozen_parent_changed'};
 const actorInputs=Array.from(document.querySelectorAll('#comment-input-container')).filter(rendered);
 const actors=new Set(actorInputs.flatMap(e=>Array.from(e.querySelectorAll('a[href]')).filter(a=>rendered(a)&&a.querySelector('img')).map(profile)).filter(Boolean));
 const directActor=actorInputs.length===1&&actors.size===1&&actors.has(args.receiver);
 const numbers=new Set(Array.from((document.body.innerText||'').matchAll(/抖音号[：:\s]*(\d+)/g)).map(m=>m[1]));
 const modalActor=actors.size===0&&location.protocol==='https:'&&location.hostname==='www.douyin.com'&&location.pathname==='/user/self'&&
   new URL(location.href).searchParams.get('modal_id')===args.video&&args.identity_fresh===true&&numbers.size===1&&numbers.has(args.expected_public_number);
 const rowIdentity=(""" + CHOICE_ROW_IDENTITY_JS + r""");
 const owns=(n,requireRendered=true)=>{const value=rowIdentity(n,requireRendered?rendered:()=>true,requireRendered);
   return new Set(value?[value.author]:[]);};
 const bodyMatch=(n,requireVisible)=>Array.from(n.querySelectorAll('div,p,span')).some(e=>e.closest(selector)===n&&
   (!requireVisible||(visible(e)&&!e.querySelector(selector)))&&normal(e.innerText)===normal(args.reply));
 const exactReadback=s=>(s||'').replace(/\r\n?/g,'\n').trim();
 const exactOwnReply=n=>Array.from(n.querySelectorAll('div,p,span')).some(e=>e.closest(selector)===n&&
   !e.querySelector(selector)&&exactReadback(e.innerText)===exactReadback(args.reply));
 const exactReplyRows=args.readback_pagination===true?Array.from(root.querySelectorAll(selector)).filter(exactOwnReply):[];
 const cssPublic=e=>{
   if(!rendered(e))return false;
   for(let p=e;p&&p.nodeType===1;p=p.parentElement){const s=getComputedStyle(p);
     if(s.visibility!=='visible'||s.display==='none'||parseFloat(s.opacity)===0)return false;}
   return true;
 };
 const readbackIdentity=(n,shown,requirePublicArrow=true)=>{
   const original=rowIdentity(n,shown,requirePublicArrow);
   if(original)return original;
   if(args.readback_pagination!==true||args.identity_fresh!==true||!(directActor||modalActor)||
      exactReplyRows.length!==1||exactReplyRows[0]!==n)return null;
   const local=e=>e.closest(selector)===n;
   const avatars=Array.from(n.querySelectorAll('.comment-item-avatar')).filter(local),
     heads=Array.from(n.querySelectorAll('.comment-item-info-wrap')).filter(local);
   if(avatars.length!==1||heads.length!==1||!shown(avatars[0])||!shown(heads[0])||
      avatars[0].matches('a')||avatars[0].querySelector('a')||
      [avatars[0],...avatars[0].querySelectorAll('*')].some(e=>e.hasAttribute('href')||e.hasAttribute('xlink:href')))return null;
   const names=Array.from(heads[0].querySelectorAll('a')).filter(local);
   if(names.length!==2||names.some(a=>!shown(a)||!a.hasAttribute('href')||!a.getAttribute('href').trim()||
      !a.querySelector('[data-click-from="title"]')||!a.innerText.trim()||a.innerText.trim().startsWith('@'))||
      profile(names[0])!==args.receiver||profile(names[1])!==args.sender)return null;
   const parts=Array.from(names[0].parentElement.children),arrow=parts[1];
   if(names[1].parentElement!==names[0].parentElement||parts.length!==3||parts[0]!==names[0]||parts[2]!==names[1]||
      arrow.tagName!=='DIV'||arrow.children.length||arrow.innerText.trim())return null;
   const s=getComputedStyle(arrow);
   if(s.borderLeftStyle!=='solid'||parseFloat(s.borderLeftWidth)<=0||parseFloat(s.borderTopWidth)<=0||
      parseFloat(s.borderBottomWidth)<=0||parseFloat(s.borderRightWidth)!==0)return null;
   if(requirePublicArrow&&(s.visibility!=='visible'||parseFloat(s.opacity)===0||s.borderLeftColor==='transparent'||
      /rgba\([^)]*,\s*0(?:\.0+)?\s*\)$/.test(s.borderLeftColor)))return null;
   return {author:args.receiver,authorLink:names[0],target:args.sender,semantic:true,readback_noavatar:true};
 };
 const readbackOwns=n=>{const id=readbackIdentity(n,()=>true,false);return new Set(id?[id.author]:[]);};
 // A same-body B reply anywhere inside this frozen root is a conservative
 // no-send barrier. Only direct ancestry under the selected A reply is exact
 // enough for this module's positive readback claim.
 const bReplies=Array.from(root.querySelectorAll(selector)).filter(n=>readbackOwns(n).size===1&&readbackOwns(n).has(args.receiver)&&bodyMatch(n,false));
 const matching=bReplies.filter(n=>visible(n)&&owns(n).size===1&&owns(n).has(args.receiver)&&
   n.parentElement.closest(selector)===selection&&bodyMatch(n,true)&&
   (!rowIdentity(n,rendered)?.target||rowIdentity(n,rendered).target===args.sender));
 const publicVisible=e=>{
   if(!visible(e))return false;
   for(let p=e;p&&p.nodeType===1;p=p.parentElement){const s=getComputedStyle(p);
     if(s.visibility!=='visible'||s.display==='none'||parseFloat(s.opacity)===0)return false;}
   return true;
 };
 const exact=s=>(s||'').replace(/\r\n?/g,'\n').trim();
 const ask=roots[thread.ask_index];
 // Flat headers identify people, not a native parent comment. Count every
 // plausible competing A -> B source, including hidden/other-choice rows.
 const aSources=Array.from(root.querySelectorAll(selector)).filter(n=>{
   const id=rowIdentity(n,()=>true,false);
   return id?.author===args.sender&&((id.semantic&&id.target===args.receiver)||n.parentElement.closest(selector)===ask||
     Array.from(n.querySelectorAll('div,p,span')).some(e=>e.closest(selector)===n&&normal(e.innerText)===normal(args.selection_text)));
 });
 const ownPlainBody=n=>{
   const local=e=>e.closest(selector)===n;
   const headers=Array.from(n.querySelectorAll('.comment-item-info-wrap')).filter(e=>local(e)&&publicVisible(e));
   const stats=Array.from(n.querySelectorAll('.comment-item-stats-container')).filter(e=>local(e)&&publicVisible(e));
   if(headers.length!==1||stats.length!==1)return false;
   const header=headers[0],group=header.parentElement,body=header.nextElementSibling;
   let statsBlock=stats[0];
   while(statsBlock.parentElement&&statsBlock.parentElement!==group&&local(statsBlock))statsBlock=statsBlock.parentElement;
   if(!body||statsBlock.parentElement!==group||!local(body)||!publicVisible(body)||body.querySelector(
     selector+',.comment-item-info-wrap,.comment-item-avatar,.comment-item-stats-container,a,button,svg,img,time,[contenteditable]')||
     exact(body.innerText)!==exact(args.reply))return false;
   let metadata=null;
   const stamp=/^(?:刚刚|\d+(?:秒|分钟|小时|天|周|个月|年)前|\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?)(?:·[\p{L}]{1,32})?$/u;
   if(body.nextElementSibling!==statsBlock){
     metadata=body.nextElementSibling;
     if(!metadata||metadata.nextElementSibling!==statsBlock||metadata.tagName!=='DIV'||
       !publicVisible(metadata)||metadata.querySelector('a,button,svg,img,time,[contenteditable],'+selector)||
       !stamp.test(normal(metadata.innerText)))return false;
   }
   // Exact body cannot be established by matching one convenient interval
   // while silently ignoring another own prose block before/after it.
   const allowed=[body,metadata,...Array.from(n.querySelectorAll('.comment-item-avatar')).filter(local)].filter(Boolean);
   const names=Array.from(header.querySelectorAll('a[href]')).filter(a=>local(a)&&publicVisible(a)&&
     a.querySelector('[data-click-from="title"]')&&[args.sender,args.receiver].includes(profile(a)));
   const walker=document.createTreeWalker(n,NodeFilter.SHOW_TEXT);
   while(walker.nextNode()){
     const text=walker.currentNode,parent=text.parentElement;
     if(!normal(text.nodeValue)||!parent||!local(parent)||!publicVisible(parent))continue;
     if(allowed.some(region=>region===parent||region.contains(parent)))continue;
     const word=normal(text.nodeValue);
     if(header.contains(parent)){
       if(names.some(a=>a===parent||a.contains(parent)))continue;
       const menu=parent.closest('[data-e2e="video-comment-more"]'),badge=parent.closest('.comment-item-tag');
       if(menu&&header.contains(menu)&&/^(?:\.{3}|…{1,2})$/.test(word))continue;
       if(badge&&header.contains(badge)&&/^(?:作者|互相关注|好友|置顶)$/.test(word))continue;
     }
     if(stats[0].contains(parent)&&(/^(?:\d+(?:\.\d+)?[万wW]?|分享|回复|赞|点赞|踩|收藏|前往西瓜视频回复评论)$/.test(word)))continue;
     return false;
   }
   return true;
 };
 const rootIdentity=rowIdentity(root,publicVisible),selectedIdentity=rowIdentity(selection,publicVisible),askIdentity=ask?rowIdentity(ask,publicVisible):null;
 const weak=(matching.length===0&&bReplies.length===1&&aSources.length===1&&publicVisible(root)&&publicVisible(selection)&&
   rootIdentity?.semantic&&rootIdentity.author===args.sender&&selectedIdentity?.semantic&&selectedIdentity.author===args.sender&&
   (!selectedIdentity.target||selectedIdentity.target===args.receiver)&&askIdentity?.semantic&&askIdentity.author===args.receiver)
   ?bReplies.filter(n=>{
      const id=readbackIdentity(n,publicVisible);
      return publicVisible(n)&&id?.semantic&&id.author===args.receiver&&id.target===args.sender&&
        n.parentElement.closest(selector)===root&&ownPlainBody(n);
    }):[];
 const allMatching=matching.concat(weak);
 const quality=allMatching.length===1?(matching.length===1?'direct_selected_parent':'weak_unique_root'):'unconfirmed';
 const noavatarRows=bReplies.filter(n=>readbackIdentity(n,()=>true,false)?.readback_noavatar===true);
 const scrollCandidates=(args.readback_pagination===true&&aSources.length===1&&bReplies.length===1)?noavatarRows.filter(n=>
   n.parentElement.closest(selector)===root&&cssPublic(n)&&readbackIdentity(n,rendered)?.readback_noavatar===true):[];
 // Diagnostic comparisons are local booleans/counts, never source text,
 // hashes of private content, identities or changes to the matching gates.
 const diagnosticRows=args.readback_pagination===true?Array.from(root.querySelectorAll(selector)):[];
 const exactOwnText=n=>Array.from(n.querySelectorAll('div,p,span')).some(e=>e.closest(selector)===n&&
   !e.querySelector(selector)&&exact(e.innerText)===exact(args.reply));
 const diagnosticB=diagnosticRows.filter(n=>owns(n,false).size===1&&owns(n,false).has(args.receiver));
 const readbackMatch=args.readback_pagination===true?{
   readback_current_receiver_verified:directActor||modalActor?1:0,
   readback_b_author_row_count:diagnosticB.length,
   readback_b_public_author_row_count:diagnosticB.filter(n=>publicVisible(n)&&rowIdentity(n,publicVisible)?.author===args.receiver).length,
   readback_own_text_equal_count:diagnosticRows.filter(exactOwnText).length,
   readback_b_exact_text_equal_count:diagnosticB.filter(exactOwnText).length,
   readback_b_visible_body_match_count:bReplies.filter(n=>publicVisible(n)&&bodyMatch(n,true)).length,
   readback_b_direct_selected_parent_count:bReplies.filter(n=>n.parentElement.closest(selector)===selection).length,
   readback_b_flat_root_parent_count:bReplies.filter(n=>n.parentElement.closest(selector)===root).length,
   readback_b_to_sender_header_count:bReplies.filter(n=>{const id=rowIdentity(n,publicVisible);return id?.semantic&&id.author===args.receiver&&id.target===args.sender;}).length,
   readback_b_plain_body_match_count:bReplies.filter(ownPlainBody).length,
   readback_root_public_count:publicVisible(root)?1:0,
   readback_selection_public_count:publicVisible(selection)?1:0,
   readback_ask_public_count:ask&&publicVisible(ask)?1:0,
   readback_root_semantic_sender_count:rootIdentity?.semantic&&rootIdentity.author===args.sender?1:0,
   readback_selection_semantic_sender_count:selectedIdentity?.semantic&&selectedIdentity.author===args.sender?1:0,
   readback_ask_semantic_receiver_count:askIdentity?.semantic&&askIdentity.author===args.receiver?1:0,
   readback_selection_to_receiver_count:!selectedIdentity?.target||selectedIdentity.target===args.receiver?1:0
 }:{};
 if(args.readback_pagination===true)Object.assign(readbackMatch,{readback_noavatar_identity_count:noavatarRows.length,
   readback_noavatar_public_count:noavatarRows.filter(n=>readbackIdentity(n,publicVisible)?.readback_noavatar===true).length,
   readback_scroll_candidate_count:scrollCandidates.length});
 const equalAuthor={};
 if(args.readback_pagination===true){
   const fields=['identity_missing','identity_B','identity_other','avatar_region','header_region','avatar_link',
     'title_link','avatar_B','avatar_A','avatar_self','avatar_uncanonical','first_title_B','first_title_A',
     'first_title_self','first_title_uncanonical','avatar_title_same_canonical','avatar_title_both_self',
     'target_title_A','B_mention','parent_root','parent_selection','parent_ask','parent_other',
     'public_row','body_interval','header_three_parts','header_arrow_shape','header_arrow_public',
     'avatar_any_anchor','avatar_href_element','row_css_public'];
   for(const name of fields)equalAuthor['readback_equal_'+name+'_count']=0;
   const add=(name,value=1)=>{equalAuthor['readback_equal_'+name+'_count']+=value;};
   const diagnosticProfile=a=>a.hasAttribute('href')&&a.getAttribute('href').trim()?profile(a):'';
   const selfLink=a=>{try{if(!a.hasAttribute('href')||!a.getAttribute('href').trim())return false;
     const u=new URL(a.getAttribute('href'),location.href);return u.protocol==='https:'&&
     u.hostname==='www.douyin.com'&&!u.username&&!u.password&&!u.port&&/^\/user\/self\/?$/.test(u.pathname);}catch{return false;}};
   for(const row of diagnosticRows.filter(exactOwnText)){
     const local=e=>e.closest(selector)===row;
     const avatars=Array.from(row.querySelectorAll('.comment-item-avatar')).filter(local),
       heads=Array.from(row.querySelectorAll('.comment-item-info-wrap')).filter(local);
     const av=avatars.flatMap(region=>Array.from(region.querySelectorAll('a')).filter(a=>local(a)&&a.querySelector('img')));
     const names=heads.flatMap(region=>Array.from(region.querySelectorAll('a')).filter(a=>local(a)&&
       a.innerText.trim()&&!a.innerText.trim().startsWith('@')&&a.querySelector('[data-click-from="title"]')));
     const id=rowIdentity(row,()=>true,false);
     add(!id?'identity_missing':id.author===args.receiver?'identity_B':'identity_other');
     add('avatar_region',avatars.length);add('header_region',heads.length);add('avatar_link',av.length);add('title_link',names.length);
     add('avatar_any_anchor',avatars.reduce((sum,region)=>sum+(region.matches('a')?1:0)+region.querySelectorAll('a').length,0));
     add('avatar_href_element',avatars.reduce((sum,region)=>sum+[region,...region.querySelectorAll('*')].filter(e=>e.hasAttribute('href')||e.hasAttribute('xlink:href')).length,0));
     for(const a of av){if(diagnosticProfile(a)===args.receiver)add('avatar_B');if(diagnosticProfile(a)===args.sender)add('avatar_A');
       if(selfLink(a))add('avatar_self');if(!diagnosticProfile(a)&&!selfLink(a))add('avatar_uncanonical');}
     const first=names[0];
     if(first){if(diagnosticProfile(first)===args.receiver)add('first_title_B');if(diagnosticProfile(first)===args.sender)add('first_title_A');
       if(selfLink(first))add('first_title_self');if(!diagnosticProfile(first)&&!selfLink(first))add('first_title_uncanonical');}
     if(av.length===1&&first&&diagnosticProfile(av[0])&&diagnosticProfile(av[0])===diagnosticProfile(first))add('avatar_title_same_canonical');
     if(av.length===1&&first&&selfLink(av[0])&&selfLink(first))add('avatar_title_both_self');
     add('target_title_A',names.slice(1).filter(a=>diagnosticProfile(a)===args.sender).length);
     add('B_mention',Array.from(row.querySelectorAll('a[href]')).filter(a=>local(a)&&a.innerText.trim().startsWith('@')&&profile(a)===args.receiver).length);
     const parent=row.parentElement?.closest(selector);
     add(parent===root?'parent_root':parent===selection?'parent_selection':parent===ask?'parent_ask':'parent_other');
     if(publicVisible(row))add('public_row');
     if(cssPublic(row))add('row_css_public');
     if(heads.length===1){const body=heads[0].nextElementSibling;
       if(body&&local(body)&&!body.querySelector(selector)&&exact(body.innerText)===exact(args.reply))add('body_interval');}
     if(names.length===2&&names[1].parentElement===first.parentElement){
       const parts=Array.from(first.parentElement.children).filter(rendered),arrow=parts[1];
       if(parts.length===3&&parts[0]===first&&parts[2]===names[1]){
         add('header_three_parts');const s=getComputedStyle(arrow);
         if(arrow.tagName==='DIV'&&!arrow.children.length&&!arrow.innerText.trim()&&s.borderLeftStyle==='solid'&&
            parseFloat(s.borderLeftWidth)>0&&parseFloat(s.borderTopWidth)>0&&parseFloat(s.borderBottomWidth)>0&&parseFloat(s.borderRightWidth)===0){
           add('header_arrow_shape');if(publicVisible(arrow)&&s.borderLeftColor!=='transparent'&&
             !/rgba\([^)]*,\s*0(?:\.0+)?\s*\)$/.test(s.borderLeftColor))add('header_arrow_public');
         }
       }
     }
   }
 }
 const spans=Array.from(selection.querySelectorAll('span')),replySpans=spans.map((e,i)=>({e,i})).filter(({e})=>
   visible(e)&&e.closest(selector)===selection&&e.innerText.trim()==='回复');
 const editors=Array.from(document.querySelectorAll('[contenteditable="true"][role="combobox"]')).filter(visible),editor=editors.length===1?editors[0]:null;
 const composer=editor?editor.closest('.comment-input-container'):null;
 const expected=normal('回复@'+detail.sender_display_name+':'+detail.command_text);
 const headers=composer?Array.from(composer.querySelectorAll('*')).filter(e=>visible(e)&&normal(e.innerText)===expected&&
   !Array.from(e.children).some(c=>visible(c)&&normal(c.innerText)===expected)):[];
 const inlineOwned=!!editor&&editor.closest(selector)===selection,modalOwned=!!editor&&!editor.closest(selector)&&modalActor&&headers.length===1;
 const scope=inlineOwned?selection:modalOwned?composer:selection;
 const sends=Array.from(scope.querySelectorAll('""" + REPLY_SEND_SELECTOR + r"""')).filter(visible);
 return {ok:true,detail,root_index:detail.comment_index,current_receiver_verified:directActor||modalActor,
   matching_reply_count:allMatching.length,existing_matching_reply_count:bReplies.length,
   strict_matching_reply_count:matching.length,weak_matching_reply_count:weak.length,
   verified_root_count:thread.counts?.original_root_count,verified_selection_count:matches.length,
   selected_source_count:aSources.length,reply_readback_quality:quality,
   reply_span_index:replySpans.length===1?replySpans[0].i:null,composer_unique:editors.length===1,
   composer_owned:inlineOwned||modalOwned,composer_scope:modalOwned?'modal':'inline',parent_header_count:headers.length,
   composer_text:editor?editor.innerText:null,send_control_count:sends.length,
   ...(args.readback_pagination===true?{readback_expand_index:thread.expand_index,
      readback_expand_kind:thread.expand_kind,readback_expand_signature:thread.expand_signature,
      readback_more_count:thread.counts.readback_more_count,
      readback_unsupported_more_count:thread.counts.readback_unsupported_more_count,
      readback_loaded_row_count:thread.counts.readback_loaded_row_count,
      ...Object.fromEntries(Object.entries(thread.counts).filter(([name])=>name.startsWith('readback_'))),
      ...equalAuthor,...readbackMatch,
      readback_scroll_candidate_index:scrollCandidates.length===1?roots.indexOf(scrollCandidates[0]):null}: {})};
}"""


CHOICE_READBACK_EXPAND_HANDLE_JS = r"""params => {
 const state=(""" + CHOICE_REPLY_STATE_JS + r""")(params.args);
 if(state.ok&&state.current_receiver_verified===true)return (""" + CHOICE_EXPAND_HANDLE_JS + r""")(params);
 // The first page can keep an empty, Root-owned replyContainer or expose a
 // numeric native control one wrapper below the exact original Root. This
 // read-only opening still needs current B identity; later pages need the
 // complete selected-parent proof. No partly loaded thread uses this path.
 const thread=(""" + CHOICE_THREAD_JS + r""")(params.args),selector='[data-e2e="comment-item"]';
 if(!thread.ok||thread.details.length!==0||thread.counts.descendant_count!==0||thread.counts.ask_count!==0)return null;
 const node=(""" + CHOICE_EXPAND_HANDLE_JS + r""")(params);
 const root=document.querySelectorAll(selector)[thread.root_index];
 if(!node||node.tagName!=='BUTTON'||node.parentElement?.closest(selector)!==root||
    !(node.parentElement.matches('.replyContainer')||node.parentElement.parentElement===root)||
    !/^(?:展开|查看)\d+(?:\.\d+)?[万wW]?条回复$/.test((node.innerText||'').replace(/\s+/gu,'')))return null;
 const rendered=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
 const profile=a=>{try{const u=new URL(a.getAttribute('href'),location.href);return u.protocol==='https:'&&
   u.hostname==='www.douyin.com'&&!u.username&&!u.password&&!u.port&&/^\/user\/[^/?#]+$/.test(u.pathname)&&
   u.pathname!=='/user/self'?u.origin+u.pathname:'';}catch{return '';}};
 const inputs=Array.from(document.querySelectorAll('#comment-input-container')).filter(rendered);
 const actors=new Set(inputs.flatMap(e=>Array.from(e.querySelectorAll('a[href]')).filter(a=>rendered(a)&&a.querySelector('img')).map(profile)).filter(Boolean));
 const direct=inputs.length===1&&actors.size===1&&actors.has(params.args.receiver);
 const numbers=new Set(Array.from((document.body.innerText||'').matchAll(/抖音号[：:\s]*(\d+)/g)).map(m=>m[1]));
 const modal=actors.size===0&&location.protocol==='https:'&&location.hostname==='www.douyin.com'&&location.pathname==='/user/self'&&
   new URL(location.href).searchParams.get('modal_id')===params.args.video&&params.args.identity_fresh===true&&
   numbers.size===1&&numbers.has(params.args.expected_public_number);
 return direct||modal?node:null;
}"""


CHOICE_READBACK_SCROLL_HANDLE_JS = r"""args => {
 const state=(""" + CHOICE_REPLY_STATE_JS + r""")(args);
 if(!state.ok||state.current_receiver_verified!==true||state.readback_more_count!==0||
    state.readback_scroll_candidate_count!==1||state.existing_matching_reply_count!==1||
    state.selected_source_count!==1||!Number.isInteger(state.readback_scroll_candidate_index))return null;
 return document.querySelectorAll('[data-e2e="comment-item"]')[state.readback_scroll_candidate_index]||null;
}"""


def _normal(value):
    return re.sub(r'\s+','',value)


def _session_args(session, allowed_selections, *, candidate_text=None):
    if not isinstance(session,dict):raise ValueError('invalid_choice_session')
    binding=Binding.from_dict(session['root_binding'])
    envelope=validate_envelope(session['root_envelope'],binding)
    if envelope['evidence'].get('source_kind') is not None:raise ValueError('choice_requires_original_mention_root')
    if (not isinstance(session.get('choice_id'),str) or not re.fullmatch(r'[a-f0-9]{32,64}',session['choice_id'])
            or any(not isinstance(session.get(key),str) or not session[key].strip() or len(session[key])>maximum
                   for key,maximum in (('ask_ref',256),('ask_text',320)))):
        raise ValueError('invalid_frozen_choice_prompt')
    from app.mention_agent.choice_evidence import choice_ask_reference
    ask_reply_ref=choice_ask_reference(root_reference_id=envelope['reference_id'],ask_ref=session['ask_ref'],
        ask_text=session['ask_text'],receiver_profile_url=binding.receiver_profile_url,video_id=envelope['video_id'])
    if session.get('ask_reply_ref') not in (None,ask_reply_ref):raise ValueError('choice_frozen_ask_reference_conflict')
    if candidate_text is None:
        forms=list(allowed_selections)
        if len(forms)>16000 or any(not isinstance(value,str) or not value or len(value)>512 for value in forms):
            raise ValueError('invalid_choice_selection_forms')
    else:
        from app.assistant.choice_intent import normalized_choice
        if (type(candidate_text) is not str or not candidate_text.strip() or len(candidate_text)>4000
                or any(ord(c)<32 and c not in '\r\n\t' for c in candidate_text)):
            raise ValueError('invalid_choice_candidate_text')
        # The trusted trigger identifies text, never a library or a DOM row.
        # Existing author/Root/ask/full-body/metadata/unique-row gates remain.
        forms=[normalized_choice(candidate_text).lower()]
    return {'sender':binding.sender_profile_url,'receiver':binding.receiver_profile_url,'instance_id':binding.instance_id,
        'root_text':envelope['comment_text'],'root_native':envelope['native_comment_id'],'video':envelope['video_id'],
        'root_reference_id':envelope['reference_id'],'choice_id':session['choice_id'],'ask_ref':session['ask_ref'],
        'ask_reply_ref':ask_reply_ref,'ask_text':session['ask_text'],'allowed_selections':forms,
        'observed_at_utc':datetime.now(timezone.utc).isoformat()}


def _open_root(receiver,args):
    _choice_observation(args,'root_open','started')
    if args['receiver']!=receiver.expected_profile_url:raise ReceiverDetailError('选库助手账号不一致',{'reason_code':'choice_receiver_mismatch'})
    _choice_checkpoint(receiver)
    opener=getattr(receiver,'open_original_from_notifications',None)
    if callable(opener):
        target={'sender_profile_url':args['sender'],'receiver_profile_url':args['receiver'],'video_id':args['video'],
                'comment_text':args['root_text'],'reference_id':args['root_reference_id'],'native_comment_id':args['root_native']}
        # The shared live adapter freezes/rechecks the full target. An error
        # from it never falls back to picking a same-looking notification.
        detail=opener(target,max_candidates=3)
        _choice_checkpoint(receiver)
        if (not isinstance(detail,dict) or detail.get('ok') is not True or type(detail.get('unique_count')) is not int
                or detail['unique_count']!=1 or any(detail.get(key)!=args[want] for key,want in (
                ('sender_profile_url','sender'),('receiver_profile_url','receiver'),('video_id','video'),('native_comment_id','root_native')))
                or _normal(detail.get('command_text',''))!=_normal(args['root_text'])):
            raise ReceiverDetailError('原保存评论范围已改变',{'reason_code':'choice_original_root_changed'})
        _choice_observation(args,'root_detail')
        return
    from .browser import NormalPageReceiver
    if isinstance(receiver,NormalPageReceiver):
        raise ReceiverDetailError('严格原评论读取能力未就绪',{'reason_code':'choice_original_root_detail_unavailable'})
    # Legacy non-live fake adapters used in isolated tests only. Actual normal
    # receivers must use the shared bounded full-target lookup above.
    receiver.page.goto(SELF_URL,wait_until='domcontentloaded',timeout=30000)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        _choice_checkpoint(receiver)
        if receiver.identity() is not None:break
        receiver.page.wait_for_timeout(250)
    else:
        _choice_observation(args,'root_identity','choice_identity_not_ready')
        raise ReceiverInterventionRequired('未能核对当前助手B，选库回看已暂停')
    _choice_observation(args,'root_identity')
    receiver._reply_identity_checked_at=time.monotonic()
    notices=receiver.notifications()
    same=[notice for notice in notices if notice.get('source_profile_url')==args['sender']
          and _normal(notice.get('exact_text',''))==_normal(args['root_text'])]
    _choice_observation(args,'root_notifications','ok' if len(same)==1 else 'choice_root_notification_not_unique',
                        notice_count=len(notices),root_notice_match_count=len(same))
    if len(same)!=1:raise ReceiverDetailError('未找到唯一原保存提醒',{'reason_code':'choice_root_notification_not_unique'})
    try:
        _choice_observation(args,'root_detail','started')
        detail=receiver.detail(same[0],keep_open=True)
    except ReceiverInterventionRequired:
        raise
    except ReceiverDetailError:
        raise
    except ReceiverPageError:
        _choice_checkpoint(receiver)
        raise ReceiverDetailError('原保存评论尚未完成核对',{'reason_code':'choice_original_root_detail_unavailable'}) from None
    if (any(detail.get(key)!=args[want] for key,want in (('sender_profile_url','sender'),('receiver_profile_url','receiver'),
            ('video_id','video'),('native_comment_id','root_native')))
            or _normal(detail.get('command_text',''))!=_normal(args['root_text'])):
        raise ReceiverDetailError('原保存评论范围已改变',{'reason_code':'choice_original_root_changed'})
    _choice_observation(args,'root_detail')


def _read_thread(receiver,args):
    _choice_observation(args,'thread_read','started')
    _choice_checkpoint(receiver)
    # Navigation/expansion may take longer than the evidence freshness window.
    # Timestamp this actual DOM read, never the earlier session preparation or
    # the later result return; leave all frozen scope/selection fields intact.
    read_args=deepcopy(args)
    read_args['observed_at_utc']=datetime.now(timezone.utc).isoformat()
    value=receiver.page.evaluate(CHOICE_THREAD_JS,read_args)
    counts=value.get('counts',{}) if isinstance(value,dict) else {}
    if not isinstance(counts,dict):counts={}
    counts={key:counts[key] for key in _OBSERVATION_COUNTS if key in counts}
    _choice_observation(args,'thread_read',value.get('reason_code','ok') if isinstance(value,dict) else 'choice_structure_unknown',**counts)
    if not isinstance(value,dict) or value.get('ok') is not True:
        raise ReceiverDetailError('原评论或选库提示尚未唯一核对',{'reason_code':value.get('reason_code','choice_structure_unknown') if isinstance(value,dict) else 'choice_structure_unknown'})
    return value


def _wait_for_choice_expansion(receiver, args):
    """Poll only the same verified root after one click; never clicks itself."""
    started = time.monotonic()
    deadline = started + CHOICE_EXPAND_WAIT_SECONDS
    reads = 0
    while True:
        _choice_checkpoint(receiver)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            receiver.page.wait_for_timeout(min(200, max(1, int(remaining * 1000))))
        _choice_checkpoint(receiver)
        value = _read_thread(receiver, args)
        reads += 1
        if value['details'] or time.monotonic() >= deadline:
            _choice_observation(args, 'expand_wait', 'expansion_wait_complete' if value['details'] else 'expansion_wait_timeout',
                wait_read_count=reads, wait_elapsed_ms=max(0, int((time.monotonic()-started)*1000)))
            return value


def _expanded_choice_thread(receiver, args, *, expansion_budget=None):
    value = _read_thread(receiver, args)
    clicked_native = set()
    for attempt in range(3):
        if (value['details'] or attempt == 2 or value['expand_index'] is None or
                (expansion_budget is not None and expansion_budget['clicks']>=CHOICE_READBACK_MAX_EXPANSIONS)):
            return value
        signature = value.get('expand_signature')
        if value.get('expand_kind') == 'native_button' and signature in clicked_native:
            return value  # A loading/unchanged native button is never re-clicked.
        _choice_checkpoint(receiver)
        _choice_observation(args, 'expand', 'expansion_started', expand_attempt=attempt+1, expand_available=1)
        if value.get('expand_kind') == 'native_button':
            clicked_native.add(signature)
        _click_choice_expansion(receiver, args, value,readback=args.get('readback_pagination') is True)
        if expansion_budget is not None:
            expansion_budget['clicks']+=1
        _choice_checkpoint(receiver)
        value = _wait_for_choice_expansion(receiver, args)
    return value


def _click_choice_expansion(receiver, args, value, *, readback=False):
    """Hold the freshly verified element, never re-resolve an old global index."""
    _choice_checkpoint(receiver)
    params = {'args': args, 'kind': value.get('expand_kind'), 'signature': value.get('expand_signature')}
    script = CHOICE_READBACK_EXPAND_HANDLE_JS if readback else CHOICE_EXPAND_HANDLE_JS
    handle = receiver.page.evaluate_handle(script, params)
    try:
        element = handle.as_element()
        _choice_checkpoint(receiver)
        if element is None or not element.evaluate('(element,params)=>(' + script + ')(params)===element', params):
            raise ReceiverDetailError('原评论展开控件已改变', {'reason_code': 'choice_expander_changed'})
        _choice_checkpoint(receiver)
        element.click(timeout=8000)
    finally:
        try:
            handle.dispose()
        except Exception:
            pass


def _record_ask_proof(receiver,args):
    receiver.last_choice_ask_proof={key:args[key] for key in ('ask_ref','ask_reply_ref','ask_text')}|{
        'source_kind':'library_choice_ask','exact_body_in_original_thread':True,
        'ask_author_profile_url':args['receiver'],'receiver_verified':True,'unique_count':1,
        'reference_quality':'verified_ui','native_comment_id':None,
        'sender_profile_url':args['sender'],'receiver_profile_url':args['receiver'],'video_id':args['video'],
        'root_reference_id':args['root_reference_id'],'root_comment_text':args['root_text'],
        'observed_at_utc':datetime.now(timezone.utc).isoformat()}


def observe_current_choice_replies(receiver,session,allowed_selections,*,candidate_text=None):
    """Read the already-open normal modal only, for exact persisted-source dedup.

    No navigation, panel expansion, clicks or keyboard input. The caller still
    compares the derived envelope with the persistent selected choice record.
    """
    receiver.last_choice_ask_proof=None
    args=_session_args(session,allowed_selections,candidate_text=candidate_text)
    value=_read_thread(receiver,args)
    if value.get('ask_missing'):
        raise ReceiverDetailError('原选库提示未能唯一回读',{'reason_code':'choice_prompt_missing'})
    _record_ask_proof(receiver,args)
    return value['details']


def read_choice_replies(receiver,session,allowed_selections,*,keep_open=False,candidate_text=None):
    """One waiting session only; two bounded reply expansions, no notification fabrication."""
    receiver.last_choice_ask_proof=None
    if session.get('state') not in {'waiting_library','awaiting_ask','ask_unknown','ask_held','selected','handoff_claimed'}:raise ValueError('choice_session_not_waiting')
    args=_session_args(session,allowed_selections,candidate_text=candidate_text)
    _choice_observation(args,'read_begin','started')
    try:
        _open_root(receiver,args)
        value=_expanded_choice_thread(receiver,args)
        if not value.get('ask_missing'):
            _record_ask_proof(receiver,args)
        if value['details']:
            _choice_observation(args,'read_finished','selection_observed',selection_count=len(value['details']))
            return value['details']
        if value.get('ask_missing'):raise ReceiverDetailError('原选库提示未能唯一回读',{'reason_code':'choice_prompt_missing'})
        _choice_observation(args,'read_finished','no_selection',selection_count=0,ask_missing=0)
        return []
    except BaseException as error:
        _failure_observation(args,error)
        receiver.last_choice_ask_proof=None
        raise
    finally:
        if not _choice_should_stop(receiver):
            receiver.check_intervention()
            if not keep_open and not _choice_should_stop(receiver):receiver.page.keyboard.press('Escape')


def _target_args(target):
    proof=target.get('choice_evidence')
    if not isinstance(proof,dict):raise ValueError('choice_reply_target_evidence_required')
    if (proof.get('source_kind')!='library_choice_reply'
            or any(target.get(key)!=proof.get(proof_key) for key,proof_key in (
                ('sender_profile_url','sender_profile_url'),('receiver_profile_url','receiver_profile_url'),
                ('video_id','video_id'),('comment_text','selection_text'),('reference_id','selection_reference_id')))
            or proof.get('native_comment_id')!=target.get('native_comment_id')):
        raise ValueError('choice_reply_target_scope_conflict')
    from app.assistant.choice_intent import normalized_choice
    # The root Binding need not be the selected library Binding. Only identity
    # and the frozen root fields are used here; real grants are checked outside.
    args={key:proof[source] for key,source in (('sender','sender_profile_url'),('receiver','receiver_profile_url'),
        ('instance_id','instance_id'),('root_text','root_comment_text'),('root_reference_id','root_reference_id'),
        ('choice_id','choice_id'),('ask_ref','ask_ref'),('ask_reply_ref','ask_reply_ref'),('ask_text','ask_text'),('video','video_id'))}
    args.update(root_native=proof.get('root_native_comment_id'),selection_text=target['comment_text'],
                native_comment_id=target['native_comment_id'],allowed_selections=[
                    normalized_choice(proof.get('selection_value',proof['selection_text']))],
                expected_direct_parent=proof['direct_reply_to_ask'],expected_ask_native=proof.get('ask_native_comment_id'),
                observed_at_utc=datetime.now(timezone.utc).isoformat())
    return args


def _reply_state_args(receiver,target,text, *, readback_pagination=False):
    args=_target_args(target)
    args.update(reply=text,identity_fresh=0<=time.monotonic()-getattr(receiver,'_reply_identity_checked_at',-10000)<=120,
                expected_public_number=receiver.expected_number)
    if readback_pagination:
        args['readback_pagination']=True
    return args


def choice_reply_state(receiver,target,text, *, readback_pagination=False):
    from app.mention_agent.publisher import ReplyPreflightError
    _choice_checkpoint(receiver)
    args=_reply_state_args(receiver,target,text,readback_pagination=readback_pagination)
    value=receiver.page.evaluate(CHOICE_REPLY_STATE_JS,args)
    if not value.get('ok'):raise ReplyPreflightError(value.get('reason_code','choice_original_root_changed'))
    if value.get('current_receiver_verified') is not True:raise ReplyPreflightError('current_actor_not_verified')
    return value


def prepare_choice_reply(receiver,target,text):
    """Reopen frozen root, find exact A selection, fill only; never sends."""
    from app.mention_agent.publisher import ReplyPreflightError
    args=_target_args(target)
    _open_root(receiver,args)
    _expanded_choice_thread(receiver,args)
    value=choice_reply_state(receiver,target,text)
    if value['existing_matching_reply_count']:raise ReplyPreflightError('existing_matching_reply')
    if value['reply_span_index'] is None:raise ReplyPreflightError('reply_control_not_unique')
    node=receiver.page.locator('[data-e2e="comment-item"]').nth(value['root_index'])
    node.locator('span').nth(value['reply_span_index']).click(timeout=8000)
    value=choice_reply_state(receiver,target,text)
    if not value['composer_unique'] or not value['composer_owned']:raise ReplyPreflightError('composer_parent_not_verified')
    if value['composer_text'].strip():raise ReplyPreflightError('existing_composer_draft')
    receiver.page.locator('[contenteditable="true"][role="combobox"]').and_(receiver.page.locator(':visible')).fill(text,timeout=8000)
    value=choice_reply_state(receiver,target,text)
    receiver._validate_reply_draft(value,text)
    return _frozen_choice_verification(target,value)


def _frozen_choice_verification(target,value):
    from app.mention_agent.publisher import ReplyPreflightError
    if value.get('current_receiver_verified') is not True:
        raise ReplyPreflightError('current_actor_not_verified')
    fresh=value['detail']['evidence'];proof=dict(target['choice_evidence'])
    mutable={'observed_at_utc','selection_reference_id','diagnostics','ask_native_comment_id','root_native_comment_id'}
    for key,old in proof.items():
        if key in mutable or key in {'direct_reply_to_ask','parent_scope'}:continue
        if key=='selection_text' and _normal(old)==_normal(fresh.get(key,'')):continue
        if fresh.get(key)!=old:raise ReplyPreflightError('choice_frozen_parent_changed')
    if proof['direct_reply_to_ask'] and not fresh['direct_reply_to_ask']:
        raise ReplyPreflightError('choice_frozen_parent_changed')
    for key in ('ask_native_comment_id','root_native_comment_id'):
        if proof.get(key) is not None and proof[key]!=fresh.get(key):raise ReplyPreflightError('choice_frozen_parent_changed')
    proof['observed_at_utc']=datetime.now(timezone.utc).isoformat()
    return {key:target[key] for key in ('sender_profile_url','receiver_profile_url','video_id','comment_text','reference_id','native_comment_id')}|proof|{
        'unique_count':1,'real_mention':proof['real_mention'],'current_receiver_verified':True,
        'observed_at_utc':proof['observed_at_utc']}


_REPLY_READBACK_COUNTS = _READBACK_DURABLE_COUNTS | frozenset({'readback_expansion_count','readback_progress_count',
    'readback_more_count','readback_unsupported_more_count','readback_loaded_row_count','readback_bound_exhausted'}) | _READBACK_LAYOUT_COUNTS
_REPLY_READBACK_COUNTS |= _READBACK_MATCH_COUNTS
_REPLY_READBACK_COUNTS |= _READBACK_EQUAL_AUTHOR_COUNTS
_REPLY_READBACK_COUNTS |= _READBACK_LOCAL_COUNTS


def _expanded_readback_state(receiver,target,text,result):
    """Read-only, same-root pages; known unread scope can never confirm.

    A repeat label needs actual loaded-row progress. Two fresh guarded native
    clicks maximum; no compose, submit, cleanup key, or send-barrier change.
    """
    budget={'clicks':0}
    initial_args=_reply_state_args(receiver,target,text,readback_pagination=True)
    initial=_expanded_choice_thread(receiver,initial_args,expansion_budget=budget)
    clicks=budget['clicks'];progress=0
    result['counts'].update(readback_expansion_count=clicks,readback_progress_count=progress,readback_bound_exhausted=0)
    for key in _READBACK_LAYOUT_COUNTS | {'readback_more_count','readback_unsupported_more_count','readback_loaded_row_count'}:
        if type(initial.get('counts',{}).get(key)) is int:
            result['counts'][key]=initial['counts'][key]
    if initial.get('counts',{}).get('readback_unsupported_more_count',0):
        result['readback_reason_code']='choice_readback_expander_unsupported'
        return None
    if not initial['details'] and initial.get('counts',{}).get('readback_more_count',0):
        result['readback_reason_code']='choice_readback_bound_exhausted' if clicks>=CHOICE_READBACK_MAX_EXPANSIONS else 'choice_readback_no_progress'
        result['counts']['readback_bound_exhausted']=int(clicks>=CHOICE_READBACK_MAX_EXPANSIONS)
        return None
    while True:
        value=choice_reply_state(receiver,target,text,readback_pagination=True)
        result['counts'].update({key:value[key] for key in _REPLY_READBACK_COUNTS
                                if type(value.get(key)) is int and 0<=value[key]<=1000000})
        result['counts'].update(readback_expansion_count=clicks,readback_progress_count=progress,readback_bound_exhausted=0)
        if value.get('readback_unsupported_more_count',0):
            result['readback_reason_code']='choice_readback_expander_unsupported'
            return None
        if value.get('readback_more_count')==0:
            return value
        if value.get('readback_more_count')!=1 or value.get('readback_expand_index') is None:
            result['readback_reason_code']='choice_readback_expander_ambiguous'
            return None
        if clicks>=CHOICE_READBACK_MAX_EXPANSIONS:
            result['readback_reason_code']='choice_readback_bound_exhausted'
            result['counts']['readback_bound_exhausted']=1
            return None
        before=value['readback_loaded_row_count']
        live_args=_reply_state_args(receiver,target,text,readback_pagination=True)
        _click_choice_expansion(receiver,live_args,{
            'expand_kind':value['readback_expand_kind'],'expand_signature':value['readback_expand_signature']},readback=True)
        clicks+=1
        result['counts']['readback_expansion_count']=clicks
        deadline=time.monotonic()+CHOICE_EXPAND_WAIT_SECONDS
        while True:
            _choice_checkpoint(receiver)
            receiver.page.wait_for_timeout(200)
            current=choice_reply_state(receiver,target,text,readback_pagination=True)
            if current.get('readback_loaded_row_count',0)>before:
                progress+=1
                break
            if time.monotonic()>=deadline:
                result['readback_reason_code']='choice_readback_no_progress'
                result['counts'].update({key:current[key] for key in _REPLY_READBACK_COUNTS
                                        if type(current.get(key)) is int and 0<=current[key]<=1000000})
                result['counts'].update(readback_expansion_count=clicks,readback_progress_count=progress)
                return None


def _finish_readback(receiver,args,result):
    """Keep the durable Store's exact contract; extra diagnostics are local.

    The fixed receiver property and optional observation log are not evidence
    credentials and cannot authorize a Store/Queue visibility projection.
    """
    reason=result.pop('readback_reason_code')
    diagnostics={key:value for key,value in result['counts'].items()
                 if key in _REPLY_READBACK_COUNTS
                 and type(value) is int and 0<=value<=1000000}
    receiver.last_choice_readback_diagnostics={'reason_code':reason,'counts':diagnostics}
    _choice_observation(args,'readback',reason,**diagnostics)
    result['counts']={key:value for key,value in result['counts'].items()
                      if key in _READBACK_DURABLE_COUNTS}
    return result


def _scroll_readback_candidate(receiver,target,text):
    """At most one exact frozen candidate; only scrolling, never composing."""
    _choice_checkpoint(receiver)
    args=_reply_state_args(receiver,target,text,readback_pagination=True)
    handle=receiver.page.evaluate_handle(CHOICE_READBACK_SCROLL_HANDLE_JS,args)
    try:
        element=handle.as_element()
        _choice_checkpoint(receiver)
        if element is None or not element.evaluate('(element,args)=>('+CHOICE_READBACK_SCROLL_HANDLE_JS+')(args)===element',args):
            raise ReceiverDetailError('原回执显示位置已改变',{'reason_code':'choice_structure_unknown'})
        _choice_checkpoint(receiver)
        element.scroll_into_view_if_needed(timeout=3000)
        _choice_checkpoint(receiver)
    finally:
        try:handle.dispose()
        except Exception:pass


def _observe_existing_choice_reply(receiver,target,text):
    """Read a previously submitted reply; never prepare, fill, clear or send.

    Callers still need a durable original reservation/gate and matching frozen
    authority before persisting a confirmation. This observation cannot grant
    a publication, remove a send barrier, or prove an earlier B/A view.
    """
    from app.mention_agent.publisher import ReplyPreflightError
    if not isinstance(text,str) or not text.strip() or len(text)>280:
        raise ValueError('invalid_reply_text')
    target=deepcopy(target)
    args=_target_args(target)
    result={'outcome':'delivery_unknown','exact_body_in_original_thread':False,'verification':None,
            'reply_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'observed_at_utc':datetime.now(timezone.utc).isoformat(),'counts':{'readback_scroll_count':0},'readback_quality':'unconfirmed',
            'readback_reason_code':'choice_readback_unavailable'}
    try:
        _open_root(receiver,args)
        value=_expanded_readback_state(receiver,target,text,result)
        if value is None:
            return _finish_readback(receiver,args,result)
        deadline=time.monotonic()+CHOICE_EXPAND_WAIT_SECONDS
        scrolled=False
        while True:
            _choice_checkpoint(receiver)
            result['counts'].update({key:value[key] for key in _REPLY_READBACK_COUNTS
                                    if type(value.get(key)) is int and 0<=value[key]<=1000000})
            result['observed_at_utc']=datetime.now(timezone.utc).isoformat()
            if value.get('readback_more_count')!=0:
                result['readback_reason_code']='choice_readback_bound_exhausted'
                return _finish_readback(receiver,args,result)
            if (not scrolled and value.get('matching_reply_count')==0
                    and value.get('readback_scroll_candidate_count')==1
                    and value.get('readback_noavatar_public_count')==0):
                _scroll_readback_candidate(receiver,target,text)
                scrolled=True
                result['counts']['readback_scroll_count']=1
                value=choice_reply_state(receiver,target,text,readback_pagination=True)
                continue
            if value.get('matching_reply_count')==1 and value.get('existing_matching_reply_count')==1:
                verification=_frozen_choice_verification(target,value)
                result.update(outcome='b_view_visible',exact_body_in_original_thread=True,
                    verification=verification,observed_at_utc=verification['observed_at_utc'],
                    readback_quality=value['reply_readback_quality'],readback_reason_code='choice_readback_complete')
                return _finish_readback(receiver,args,result)
            if value.get('existing_matching_reply_count',0)>1 or time.monotonic()>=deadline:
                result['readback_reason_code']='choice_readback_duplicate_reply' if value.get('existing_matching_reply_count',0)>1 else 'choice_readback_reply_not_visible'
                return _finish_readback(receiver,args,result)
            _choice_checkpoint(receiver)
            receiver.page.wait_for_timeout(200)
            value=choice_reply_state(receiver,target,text,readback_pagination=True)
    except (ReceiverInterventionRequired,_ChoiceRunStopped):
        raise
    except (ReplyPreflightError,ReceiverPageError):
        _choice_checkpoint(receiver)
        return _finish_readback(receiver,args,result)
    except Exception:
        _choice_checkpoint(receiver)
        return _finish_readback(receiver,args,result)


@contextmanager
def _readback_viewport(receiver):
    """Trusted headless-owner opt-in, readback only, always restore layout.

    No desktop/window controls or identity/visibility rule changes. The owner
    factory may set exactly1600; absent opt-in retains the existing viewport.
    """
    diagnostic={'configured':0,'expanded':0,'restored':0,'restore_failed':0}
    receiver.last_choice_readback_viewport_diagnostics=diagnostic
    configured=getattr(receiver,'readback_viewport_height',None)
    if type(configured) is not int or configured!=1600:
        yield
        return
    diagnostic['configured']=1
    _choice_checkpoint(receiver)
    original=deepcopy(receiver.page.viewport_size)
    if (not isinstance(original,dict) or set(original)!={'width','height'}
            or any(type(original.get(k)) is not int or not 1<=original[k]<=16384 for k in ('width','height'))):
        raise ReceiverPageError('只读回执页面尺寸尚未核对')
    if original['height']>=configured:
        yield
        return
    changed=False;primary=None
    try:
        # Mark before calling: a failed setter may have applied the layout.
        changed=True
        receiver.page.set_viewport_size({'width':original['width'],'height':configured})
        diagnostic['expanded']=1
        _choice_checkpoint(receiver)
        yield
    except BaseException as error:
        primary=error
        raise
    finally:
        if changed:
            try:
                receiver.page.set_viewport_size(original)
                diagnostic['restored']=1
            except Exception:
                diagnostic['restore_failed']=1
                if primary is None:
                    raise ReceiverPageError('只读回执页面尺寸未能恢复') from None


def observe_existing_choice_reply(receiver,target,text):
    """Read a frozen reply under an optional bounded, temporary viewport."""
    if not isinstance(text,str) or not text.strip() or len(text)>280:
        raise ValueError('invalid_reply_text')
    _target_args(deepcopy(target))  # No layout action for an invalid target.
    with _readback_viewport(receiver):
        return _observe_existing_choice_reply(receiver,target,text)
