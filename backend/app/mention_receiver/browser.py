"""Normal-page receiver and explicitly enabled bounded original-thread replies.

This deliberately uses its own persistent browser profile. It reads the recent
notification panel, not a complete historical message stream. DOM changes are
reported as errors rather than silently treated as an empty inbox.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

SELF_URL = "https://www.douyin.com/user/self?from_tab_name=main&showTab=like"

# Read only rendered DOM. The hashed CSS class names are intentionally unused.
NOTICE_JS = r"""() => {
  const visible = el => { const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight; };
  // Narrow only a positively identified notification widget. The explicit
  // listDlgTest-container marker is emitted by the observed public NoticeEntry
  // component; an alternative widget needs a real expanded-control ARIA link.
  // Text or floating positioning alone is not ownership. Unknown layouts keep
  // the conservative legacy whole-page scan.
  const mains = Array.from(document.querySelectorAll('h1,main,[role="main"]')).filter(visible);
  const controls = Array.from(document.querySelectorAll('button,[role="button"]')).filter(el =>
    visible(el) && el.getAttribute('aria-expanded') === 'true' &&
    ((el.getAttribute('aria-label') || '').trim() === '通知' || (el.innerText || '').trim() === '通知'));
  const tokens = value => (value || '').trim().split(/\s+/).filter(Boolean);
  const uniqueId = el => el.id && Array.from(document.querySelectorAll('[id]')).filter(n => n.id === el.id).length === 1;
  const linked = panel => {
    if (!uniqueId(panel) || !['dialog','region','menu'].includes(panel.getAttribute('role'))) return false;
    const parents = controls.filter(control =>
      tokens(control.getAttribute('aria-controls')).includes(panel.id) ||
      (uniqueId(control) && tokens(panel.getAttribute('aria-labelledby')).includes(control.id)));
    return parents.length === 1;
  };
  const titles = Array.from(document.querySelectorAll('span,p,div,h1,h2,h3')).filter(el =>
    visible(el) && (el.innerText || '').trim() === '互动消息' &&
    !Array.from(el.children).some(child => visible(child) && (child.innerText || '').trim() === '互动消息'));
  const floatingBoundary = root => {
    for (let node=root,depth=0;node&&node!==document.body&&node!==document.documentElement&&depth<7;node=node.parentElement,depth++) {
      if(visible(node)&&/^(absolute|fixed)$/.test(getComputedStyle(node).position)&&!mains.some(main=>node.contains(main)))return true;
    }
    return false;
  };
  const nativePanels = Array.from(document.querySelectorAll('[data-e2e="listDlgTest-container"]')).filter(panel =>
    visible(panel) && !mains.some(main=>panel.contains(main)) && floatingBoundary(panel) &&
    titles.some(title=>panel.contains(title)));
  const panels = new Set(nativePanels);
  for (const title of titles) {
    if(nativePanels.some(panel=>panel.contains(title)))continue;
    for (let panel = title, depth = 0; panel && panel !== document.body && panel !== document.documentElement && depth < 7; panel = panel.parentElement, depth++) {
      if (visible(panel) && /^(absolute|fixed)$/.test(getComputedStyle(panel).position) &&
          !mains.some(main => panel.contains(main)) && linked(panel)) { panels.add(panel); break; }
    }
  }
  // Nested wrappers of one widget are one owner; distinct notification widgets
  // are not resolved by DOM order or by whichever empty marker renders first.
  const outerPanels = Array.from(panels).filter(panel=>!Array.from(panels).some(other=>other!==panel&&other.contains(panel)));
  const ambiguous = nativePanels.length>1 || outerPanels.length>1;
  const scope = !ambiguous && outerPanels.length === 1 ? outerPanels[0] : document;
  const inside = el => scope === document || scope.contains(el);
  const profile = href => { try { const u = new URL(href, location.href);
    if (u.hostname !== 'www.douyin.com' || !/^\/user\/[^/?#]+$/.test(u.pathname)) return '';
    return u.origin + u.pathname; } catch { return ''; } };
  const items = [];
  Array.from(document.querySelectorAll('pre')).forEach((pre, index) => {
    if (!visible(pre) || !inside(pre)) return;
    let row = pre;
    let fallback = null;
    for (let depth = 0; row && depth < 7; depth++, row = row.parentElement) {
      if (!inside(row)) break;
      if (row !== pre && row.querySelectorAll('pre').length !== 1) break;
      const text = row.innerText || '';
      const sender = Array.from(row.querySelectorAll('a[href]')).find(a =>
        a.innerText.trim() && profile(a.getAttribute('href')));
      const cover = Array.from(row.querySelectorAll('img[src]')).find(i =>
        !i.closest('a[href*="/user/"]') && visible(i));
      if (text.includes('在评论中提到了你') && sender) {
        let coverPath = '';
        try { coverPath = new URL(cover.getAttribute('src'), location.href).pathname; } catch {}
        fallback = {pre_index: index, source_profile_url: profile(sender.getAttribute('href')),
          sender_display_name: sender.innerText.trim(), exact_text: pre.innerText,
          semantic_fingerprint: coverPath};
        if (coverPath) break;
      }
    }
    if (fallback) items.push(fallback);
  });
  const mentionNodes = Array.from(document.querySelectorAll('span,p,div')).filter(el =>
    visible(el) && inside(el) && Array.from(el.childNodes).some(n => n.nodeType === 3 && (n.textContent || '').includes('在评论中提到了你')));
  const emptyNodes = Array.from(document.querySelectorAll('span,p,div')).filter(el =>
    visible(el) && inside(el) && /^(暂时没有更多了|暂无.*消息)$/.test((el.innerText || '').trim()));
  const potentialMentions = Array.from(document.querySelectorAll('pre')).filter(el=>visible(el)&&inside(el)&&/^@/.test((el.innerText||'').trim()));
  return {items, visible_mention_count: mentionNodes.length, potential_mention_count:potentialMentions.length,
    known_end_or_empty: emptyNodes.length > 0, notification_scope_ambiguous: ambiguous};
}"""

# Return the snapshot that satisfied readiness, rather than waiting on a title
# and then taking a second snapshot in the middle of a render transition.
NOTICE_READY_JS = """() => {
  const value = (""" + NOTICE_JS + """)();
  if (value.notification_scope_ambiguous) return false;
  if (value.visible_mention_count > value.items.length || value.potential_mention_count > value.items.length) return false;
  // Text can render before the cover. Never persist a temporary empty card
  // identity that changes as soon as the image element receives its source.
  if (value.items.some(item => !item.semantic_fingerprint.trim())) return false;
  return value.items.length || value.known_end_or_empty ? value : false;
}"""

# This is deliberately stricter than merely sharing an app-root ancestor.
# Unknown/static/global layouts have no scrolling fallback.
NOTICE_HISTORY_OWNER_JS = r"""el => {
  const visible=n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight;};
  const titles=Array.from(document.querySelectorAll('span,p,div,h1,h2,h3')).filter(n=>
    visible(n)&&(n.innerText||'').trim()==='互动消息'&&
    !Array.from(n.children).some(c=>visible(c)&&(c.innerText||'').trim()==='互动消息'));
  if(titles.length!==1)return false;
  const mains=Array.from(document.querySelectorAll('h1,main,[role="main"]')).filter(visible);
  for(let owner=el,depth=0;owner&&owner!==document.body&&owner!==document.documentElement&&depth<7;depth++,owner=owner.parentElement){
    if(visible(owner)&&/^(absolute|fixed)$/.test(getComputedStyle(owner).position)&&owner.contains(titles[0])&&
        !mains.some(main=>owner.contains(main)))return true;
  }
  return false;
}"""

# Only the normal notification pane may be scrolled. Find its nearest actual
# scroll ancestor from the already parsed visible notification cards; never
# fall back to document/body or guess a hashed class/cover/video relation.
NOTICE_HISTORY_CONTAINER_JS = """() => {
  const state = (""" + NOTICE_JS + """)();
  const visible = el => {const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight;};
  const owned=(""" + NOTICE_HISTORY_OWNER_JS + """);
  if(!state.items.length)return null;
  const pres=Array.from(document.querySelectorAll('pre')),found=new Set();
  for(const item of state.items){
    let p=pres[item.pre_index],matched=false;if(!p||!visible(p))return null;
    for(let depth=0;p&&p!==document.body&&p!==document.documentElement&&depth<12;depth++,p=p.parentElement){
      const style=getComputedStyle(p);
      if(/^(auto|scroll)$/.test(style.overflowY)&&p.clientHeight>0&&p.scrollHeight>p.clientHeight+2){
        if(!owned(p))return null;found.add(p);matched=true;break;
      }
    }
    if(!matched)return null;
  }
  return found.size===1?Array.from(found)[0]:null;
}"""

NOTICE_HISTORY_SCROLL_JS = r"""(el,args) => {
  if(!el||!document.contains(el)||el===document.body||el===document.documentElement)return null;
  if(! (""" + NOTICE_HISTORY_OWNER_JS + r""")(el))return null;
  const r=el.getBoundingClientRect(),style=getComputedStyle(el);
  if(r.width<=0||r.height<=0||! /^(auto|scroll)$/.test(style.overflowY)||el.clientHeight<=0)return null;
  if(args.action==='seek'){
    if(!Number.isFinite(args.top)||args.top<0)return null;el.scrollTop=args.top;
  }else if(args.action==='next'){el.scrollTop+=Math.max(1,Math.floor(el.clientHeight*.85));}
  else if(args.action!=='state')return null;
  return {top:el.scrollTop,at_end:el.scrollTop+el.clientHeight>=el.scrollHeight-2};
}"""

DETAIL_JS = r"""({sender, wanted, receiver}) => {
  const normal = s => (s || '').replace(/\s+/gu, '').trim();
  const profile = href => { try { const u = new URL(href, location.href);
    return u.protocol === 'https:' && u.hostname === 'www.douyin.com' &&
      !u.username && !u.password && !u.port && /^\/user\/[^/?#]+$/.test(u.pathname) &&
      u.pathname !== '/user/self' ? u.origin + u.pathname : ''; } catch { return ''; } };
  const visible = el => { const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight; };
  const commentSelector = '[data-e2e="comment-item"]';
  const matches = [];
  let senderMatches = 0, recipientMatches = 0;
  const comments = Array.from(document.querySelectorAll(commentSelector)).filter(visible);
  for (const item of comments) {
    // A root comment must not borrow an author's or @ link from a nested reply.
    const links = Array.from(item.querySelectorAll('a[href]')).filter(a =>
      visible(a) && a.closest(commentSelector) === item);
    const owners = links.filter(a => a.innerText.trim() && !a.innerText.trim().startsWith('@') &&
      profile(a.getAttribute('href')));
    const ownerProfiles = new Set(owners.map(a => profile(a.getAttribute('href'))));
    if (ownerProfiles.size !== 1 || !ownerProfiles.has(sender)) continue;
    const owner = owners[0];
    senderMatches++;
    const matchedContainers = new Set();
    for (const mention of links.filter(a => a.innerText.trim().startsWith('@') &&
        profile(a.getAttribute('href')) === receiver)) {
      recipientMatches++;
      let p = mention;
      for (let i = 0; p && p !== item && i < 12; i++, p = p.parentElement) {
        if (p.closest(commentSelector) !== item || p.querySelector(commentSelector)) break;
        if (visible(p) && normal(p.innerText) === normal(wanted)) {
          if (!matchedContainers.has(p)) {
            matchedContainers.add(p);
            // Only explicitly named public DOM attributes qualify. Never read
            // framework state, arbitrary data-id fields, or a hidden API.
            const nativeIds = new Set(['data-comment-id', 'data-e2e-comment-id']
              .map(name => item.getAttribute(name)).filter(value => /^\d{5,30}$/.test(value || '')));
            matches.push({command_text: p.innerText.trim(), sender_display_name: owner.innerText.trim(),
              sender_profile_url: profile(owner.getAttribute('href')),
              receiver_profile_url: profile(mention.getAttribute('href')),
              native_comment_id: nativeIds.size === 1 ? Array.from(nativeIds)[0] : null,
              native_comment_id_conflict: nativeIds.size > 1});
          }
          break;
        }
      }
    }
  }
  const strongIds = new Set(), captionIds = new Set();
  const descriptions = Array.from(document.querySelectorAll('[data-e2e="video-desc"]')).filter(visible);
  const infos = Array.from(document.querySelectorAll('[data-e2e="detail-video-info"],[data-e2e="video-info"]')).filter(visible);
  for (const info of infos) {
    const id = info.getAttribute('data-e2e-aweme-id');
    if (/^\d{5,30}$/.test(id || '')) strongIds.add(id);
  }
  for (const active of Array.from(document.querySelectorAll('[data-e2e="feed-active-video"]')).filter(visible)) {
    const id = active.getAttribute('data-e2e-vid');
    if (/^\d{5,30}$/.test(id || '')) strongIds.add(id);
    const match = /(?:^|\s)video_(\d{5,30})(?:\s|$)/.exec(active.getAttribute('class') || '');
    if (match) strongIds.add(match[1]);
  }
  for (const player of Array.from(document.querySelectorAll('[data-e2e="player-container"]')).filter(visible)) {
    const match = /(?:^|\s)video_(\d{5,30})(?:\s|$)/.exec(player.getAttribute('class') || '');
    if (match) strongIds.add(match[1]);
  }
  const routeId = /^\/video\/(\d{5,30})\/?$/.exec(location.pathname);
  if (location.hostname === 'www.douyin.com' && routeId) strongIds.add(routeId[1]);
  for (const desc of descriptions) for (const a of desc.querySelectorAll('a[href]')) {
    try { const u = new URL(a.getAttribute('href'), location.href); const id = u.searchParams.get('aweme_id');
      if (u.hostname === 'www.douyin.com' && /^\d{5,30}$/.test(id || '')) captionIds.add(id); } catch {}
  }
  const ids = strongIds.size ? strongIds : captionIds;
  const conflict = strongIds.size > 1 || (strongIds.size === 1 && Array.from(captionIds).some(id=>!strongIds.has(id)));
  const diagnostics = {visible_comment_count:comments.length, sender_match_count:senderMatches,
    recipient_match_count:recipientMatches, command_match_count:matches.length,
    visible_video_ids:Array.from(ids), info_count:infos.length, description_count:descriptions.length, id_conflict:conflict};
  if (matches.length !== 1) return {ok:false,reason:'未找到唯一的同发送者、同接收者、同正文评论',diagnostics};
  if (matches[0].native_comment_id_conflict)
    return {ok:false,reason:'原评论公开编号冲突，保留待核对',diagnostics};
  if (ids.size !== 1 || conflict)
    return {ok:false,reason:'可见视频缺少唯一的视频编号，保留待重试',diagnostics};
  const videoId = Array.from(ids)[0];
  const title = descriptions.length === 1 ? descriptions[0].innerText.trim()
    : infos.length === 1 ? (infos[0].querySelector('span') || infos[0]).innerText.trim() : '';
  const evidence = {sender_profile_url:matches[0].sender_profile_url,
    receiver_profile_url:matches[0].receiver_profile_url, command_text:matches[0].command_text,
    native_comment_id:matches[0].native_comment_id, unique_count:matches.length, video_id:videoId,
    real_mention:true, sender_verified:true, video_verified:true};
  return {ok: true, ...matches[0], video_id: videoId,
    video_title: title, diagnostics, evidence, unique_count:matches.length,
    real_mention:true, sender_verified:true, video_verified:true,
    source_url: 'https://www.douyin.com/video/' + videoId};
}"""

DETAIL_READY_JS = """args => {const value = (""" + DETAIL_JS + """)(args); return value.ok ? value : false;}"""

THREAD_REFERENCE_JS = r"""args => {
  const selector='[data-e2e="comment-item"]';
  const normal=s=>(s||'').replace(/\s+/gu,'');
  const visible=n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight;};
  const profile=a=>{try{const u=new URL(a.getAttribute('href'),location.href);return (
    u.protocol==='https:'&&u.hostname==='www.douyin.com'&&!u.username&&!u.password&&!u.port&&
    /^\/user\/[^/?#]+$/.test(u.pathname)&&u.pathname!=='/user/self'?u.origin+u.pathname:'');}catch{return '';}};
  const ownedLinks=n=>Array.from(n.querySelectorAll('a[href]')).filter(a=>a.closest(selector)===n&&visible(a));
  const owner=n=>new Set(ownedLinks(n).filter(a=>a.innerText.trim()&&!a.innerText.trim().startsWith('@')).map(profile).filter(Boolean));
  const bodyTexts=n=>new Set(Array.from(n.querySelectorAll('span,p,div')).filter(e=>e.closest(selector)===n&&
    !e.querySelector(selector)&&visible(e)).map(e=>normal(e.innerText)));
  const comments=Array.from(document.querySelectorAll(selector)).filter(visible);
  const current=comments.filter(n=>{const owners=owner(n);return owners.size===1&&owners.has(args.sender)&&
    ownedLinks(n).some(a=>a.innerText.trim().startsWith('@')&&profile(a)===args.receiver)&&bodyTexts(n).has(normal(args.text));});
  if(current.length!==1)return {quality:'independent',root_reference_id:args.reference_id};
  let root=current[0],ancestor=root.parentElement?.closest(selector);
  while(ancestor){root=ancestor;ancestor=root.parentElement?.closest(selector);}
  if(root===current[0])return {quality:'current_comment',root_reference_id:args.reference_id};
  const owners=owner(root);
  if(owners.size!==1||!owners.has(args.sender)||!ownedLinks(root).some(a=>
    a.innerText.trim().startsWith('@')&&profile(a)===args.receiver))return {quality:'independent',root_reference_id:args.reference_id};
  const texts=bodyTexts(root),matches=args.roots.filter(r=>texts.has(normal(r.comment_text)));
  const ids=new Set(matches.map(r=>r.reference_id));
  return ids.size===1?{quality:'verified_ancestor',root_reference_id:Array.from(ids)[0]}:
    {quality:'independent',root_reference_id:args.reference_id};
}"""

# This selector was observed in the normal direct-video inline composer. It
# deliberately requires both the public arrow shape and colour; changed markup
# pauses instead of guessing the last icon or pressing Enter.
REPLY_SEND_SELECTOR = '.comment-input-inner-container span:has(> svg > path[fill="#FE2C55"][d^="M17.5 30C23.851"])'

REPLY_STATE_JS = r"""args => {
  const normal = s => (s || '').replace(/\s+/gu, '');
  const rendered = e => {const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
  const visible = e => {const r=e.getBoundingClientRect();return rendered(e)&&r.bottom>0&&r.top<innerHeight;};
  const profile = href => {try {const u=new URL(href,location.href);return u.protocol==='https:'&&u.hostname==='www.douyin.com'&&
    !u.username&&!u.password&&!u.port&&/^\/user\/[^/?#]+$/.test(u.pathname)&&u.pathname!=='/user/self'?u.origin+u.pathname:'';}catch{return '';}};
  const selector='[data-e2e="comment-item"]', roots=Array.from(document.querySelectorAll(selector));
  const ownLinks=root=>Array.from(root.querySelectorAll('a[href]')).filter(a=>a.closest(selector)===root);
  const owners=(root,requireRendered=true)=>new Set(ownLinks(root).filter(a=>(!requireRendered||rendered(a))&&a.innerText.trim()&&!a.innerText.trim().startsWith('@'))
    .map(a=>profile(a.getAttribute('href'))).filter(Boolean));
  const candidates=roots.filter(root=>visible(root)&&owners(root).size===1&&owners(root).has(args.sender)&&
    ownLinks(root).some(a=>a.innerText.trim().startsWith('@')&&profile(a.getAttribute('href'))===args.receiver&&
      (function(){let p=a;for(let i=0;p&&p!==root&&i<12;i++,p=p.parentElement){
        if(p.closest(selector)!==root||p.querySelector(selector))break;
        if(normal(p.innerText)===normal(args.wanted))return true;}return false;})()));
  const detail=(""" + DETAIL_JS + r""")({sender:args.sender,receiver:args.receiver,wanted:args.wanted});
  if(!detail.ok||candidates.length!==1)return {ok:false,reason_code:'original_comment_not_unique'};
  const root=candidates[0];
  if(detail.video_id!==args.video||detail.native_comment_id!==args.native_comment_id)
    return {ok:false,reason_code:'original_target_changed'};
  // The current actor is the public avatar link of the main comment input,
  // never a matching B author link somewhere in the discussion.
  const actorInputs=Array.from(document.querySelectorAll('#comment-input-container')).filter(rendered);
  const actors=new Set(actorInputs.flatMap(e=>Array.from(e.querySelectorAll('a[href]')).filter(a=>rendered(a)&&a.querySelector('img'))
    .map(a=>profile(a.getAttribute('href')))).filter(Boolean));
  const directActor=actorInputs.length===1&&actors.size===1&&actors.has(args.receiver);
  const profileNumbers=new Set(Array.from((document.body.innerText||'').matchAll(/抖音号[：:\s]*(\d+)/g)).map(m=>m[1]));
  const modalSelf=location.protocol==='https:'&&location.hostname==='www.douyin.com'&&
    location.pathname==='/user/self'&&new URL(location.href).searchParams.get('modal_id')===args.video;
  // The observed notification modal has no avatar in its composer. It remains
  // over the freshly verified B self page: require that same public number,
  // fresh operator identity proof and no contradictory comment actor link.
  const modalActor=actors.size===0&&modalSelf&&args.identity_fresh===true&&
    typeof args.expected_public_number==='string'&&profileNumbers.size===1&&profileNumbers.has(args.expected_public_number);
  const currentActor=directActor||modalActor;
  const children=Array.from(root.querySelectorAll(selector)).filter(child=>child.parentElement.closest(selector)===root);
  const matching=children.filter(child=>visible(child)&&owners(child).size===1&&owners(child).has(args.receiver)&&
    Array.from(child.querySelectorAll('div,p,span')).some(e=>visible(e)&&e.closest(selector)===child&&!e.querySelector(selector)&&normal(e.innerText)===normal(args.reply)));
  const existing=children.filter(child=>owners(child,false).size===1&&owners(child,false).has(args.receiver)&&
    Array.from(child.querySelectorAll('div,p,span')).some(e=>e.closest(selector)===child&&normal(e.innerText)===normal(args.reply)));
  const spans=Array.from(root.querySelectorAll('span'));
  const replySpans=spans.map((e,i)=>({e,i})).filter(({e})=>visible(e)&&e.closest(selector)===root&&e.innerText.trim()==='回复');
  const editors=Array.from(document.querySelectorAll('[contenteditable="true"][role="combobox"]')).filter(visible);
  const editor=editors.length===1?editors[0]:null;
  const composer=editor?editor.closest('.comment-input-container'):null;
  const expectedHeader=normal('回复@'+detail.sender_display_name+':'+args.wanted);
  const parentHeaders=composer?Array.from(composer.querySelectorAll('*')).filter(e=>visible(e)&&
    normal(e.innerText)===expectedHeader&&!Array.from(e.children).some(c=>visible(c)&&normal(c.innerText)===expectedHeader)):[];
  const inlineOwned=!!editor&&editor.closest(selector)===root;
  const modalOwned=!!editor&&!editor.closest(selector)&&modalActor&&parentHeaders.length===1;
  const sendScope=inlineOwned?root:modalOwned?composer:root;
  const sends=Array.from(sendScope.querySelectorAll('""" + REPLY_SEND_SELECTOR + r"""')).filter(visible);
  return {ok:true,detail,root_index:roots.indexOf(root),current_receiver_verified:currentActor,
    matching_reply_count:matching.length,existing_matching_reply_count:existing.length,
    reply_span_index:replySpans.length===1?replySpans[0].i:null,
    composer_unique:editors.length===1,composer_owned:inlineOwned||modalOwned,
    composer_scope:modalOwned?'modal':'inline',parent_header_count:parentHeaders.length,
    composer_text:editor?editor.innerText:null,send_control_count:sends.length};
}"""


class ReceiverPageError(RuntimeError):
    """A non-sensitive, user-facing page-state error."""


class ReceiverInterventionRequired(ReceiverPageError):
    """Stop all input while the user handles verification or wrong identity."""


class ReceiverDetailError(ReceiverPageError):
    def __init__(self, message: str, diagnostics: dict):
        super().__init__(message)
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class ReceiverIdentity:
    public_number: str
    display_name: str


def canonical_profile(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.hostname != "www.douyin.com"
            or parts.username or parts.password or parts.port not in (None, 443)
            or not re.fullmatch(r"/user/[^/?#]+", parts.path) or parts.path == "/user/self"):
        raise ValueError("助手账号链接须为抖音公开个人主页链接")
    return f"https://www.douyin.com{parts.path}"


def isolated_profile(storage: Path, protected: Path) -> Path:
    """Reject symlinks/overlap, including a customised favourites profile."""
    storage = storage.resolve()
    target = storage / "mention_receiver" / "browser_b"
    resolved = target.resolve()
    if resolved != target or not resolved.is_relative_to(storage):
        raise ValueError("助手浏览器目录存在重定向，已停止")
    original = protected.resolve()
    if resolved == original or resolved.is_relative_to(original) or original.is_relative_to(resolved):
        raise ValueError("助手浏览器与原收藏浏览器目录重叠，已停止")
    return resolved


def browser_options(storage: Path) -> dict:
    for pattern in ("chromium-*/chrome-win/chrome.exe", "chromium-*/chrome-win64/chrome.exe"):
        matches = sorted((storage / "playwright_browsers").glob(pattern), reverse=True)
        if matches:
            return {"executable_path": str(matches[0])}
    # Installed Edge is the existing project's fallback; never install browsers.
    return {"channel": "msedge"}


class NormalPageReceiver:
    def __init__(self, page, expected_number: str, expected_profile_url: str, *, open_strategy="hover"):
        if open_strategy not in {"hover", "click"}:
            raise ValueError("未知的通知展开方式")
        self.page = page
        self.expected_number = expected_number
        self.expected_profile_url = canonical_profile(expected_profile_url)
        self.stage = "核对助手主页"
        self.open_strategy = open_strategy
        self.open_method = "none"
        self.open_attempts = 0
        self.open_retries = 0
        self.last_original_lookup = {}

    def begin_cycle(self):
        self.open_method = "none"
        self.open_attempts = 0
        self.open_retries = 0

    def _visible_text(self, text, *, exact=True):
        # Intersect before .first: a hidden template must not hide a later live node.
        return self.page.get_by_text(text, exact=exact).and_(self.page.locator(":visible"))

    def _notification_entry(self):
        # The observed parent is data-e2e="something-button", the event target.
        return self._visible_text("通知").first.locator("..")

    def identity(self) -> ReceiverIdentity | None:
        self.stage = "核对助手主页"
        self._check_challenge()
        if urlsplit(self.page.url).path != "/user/self":
            return None
        values = self._visible_text(re.compile(r"抖音号[：:\s]*\d+"), exact=False).all_text_contents()
        numbers = {m.group(1) for v in values for m in re.finditer(r"抖音号[：:\s]*(\d+)", v)}
        if not numbers:
            return None
        if numbers != {self.expected_number}:
            raise ReceiverInterventionRequired("登录账号与指定助手 B 不符，请本人核对；接收已暂停")
        heading = self.page.get_by_role("heading", level=1).and_(self.page.locator(":visible"))
        if heading.count() != 1:
            raise ReceiverPageError("助手主页结构发生变化，接收已暂停")
        return ReceiverIdentity(self.expected_number, heading.inner_text().strip())

    def _check_challenge(self):
        # Never interact with verification/authentication dialogs.
        for text in ("请完成下列验证", "拖动滑块", "请依次点击", "身份验证", "访问过于频繁",
                     "短信验证", "安全验证", "请输入验证码", "发送验证码", "完成验证"):
            if self._visible_text(text, exact=False).count():
                raise ReceiverInterventionRequired("网页要求人工验证或访问受限，已暂停自动操作，请在助手窗口处理")
        for text in ("扫码登录", "验证码登录", "手机号登录", "账号密码登录", "登录后抖音更懂你", "登录其他账号"):
            if self._visible_text(text, exact=False).count():
                raise ReceiverInterventionRequired("需要本人在独立助手窗口登录 B，自动操作已暂停")

    def check_intervention(self):
        self._check_challenge()

    def conversation_reference(self, envelope, trusted_roots):
        """Match only a previously recorded root in this currently visible thread.

        The caller supplies roots already isolated by user/device/library/video.
        Missing/ambiguous ancestry starts an independent context, never a guess
        based on the last question asked by this account somewhere else.
        """
        self._check_challenge()
        if (not isinstance(trusted_roots, list) or len(trusted_roots) > 24
                or any(not isinstance(root, dict) or set(root) != {"reference_id", "comment_text"}
                       or not isinstance(root["reference_id"], str) or not isinstance(root["comment_text"], str)
                       for root in trusted_roots)):
            raise ValueError("invalid_thread_roots")
        value = self.page.evaluate(THREAD_REFERENCE_JS, {"sender":envelope["sender_profile_url"],
            "receiver":envelope["receiver_profile_url"], "text":envelope["comment_text"],
            "reference_id":envelope["reference_id"], "roots":trusted_roots})
        allowed = {envelope["reference_id"], *(root["reference_id"] for root in trusted_roots)}
        if (not isinstance(value, dict) or set(value) != {"quality", "root_reference_id"}
                or value["quality"] not in {"current_comment", "verified_ancestor", "independent"}
                or value["root_reference_id"] not in allowed):
            raise ReceiverPageError("原评论线程范围尚未核对，保留为独立问题")
        return value

    def notifications(self) -> list[dict]:
        from playwright.sync_api import TimeoutError as PageTimeout
        self._check_challenge()
        self.stage = "展开通知入口"
        panel_title = self._visible_text("互动消息").first
        attempt_limit = 2 if self.open_strategy == "hover" else 1
        for attempt in range(attempt_limit):
            if panel_title.is_visible():
                break
            # Navigation can retain a pointer already over the header. Move to
            # the verified, non-interactive profile heading before re-entering.
            self.stage = "重置通知悬停位置"
            if self.open_strategy == "hover":
                self.page.get_by_role("heading", level=1).and_(self.page.locator(":visible")).first.hover(timeout=8000)
            self.stage = "展开通知入口"
            # One trigger per attempt. A delayed hover popup must never receive
            # a second click that can close it. Other variants choose click
            # explicitly at startup rather than guessing midway through a wait.
            self.open_method = self.open_strategy
            self.open_attempts += 1
            if self.open_strategy == "hover":
                self._notification_entry().hover(timeout=8000)
            else:
                self._notification_entry().click(timeout=8000)
            self.stage = "等待通知面板"
            try:
                panel_title.wait_for(timeout=12000)
                break
            except PageTimeout:
                self._check_challenge()
                if attempt + 1 >= attempt_limit:
                    raise
                # Startup hydration can miss the first pointer entry. Recheck
                # identity and retrigger the same idempotent hover, never click.
                if self.identity() is None:
                    raise ReceiverPageError("助手主页尚未就绪，保留进度等待下一轮")
                self.open_retries += 1
        self.stage = "等待通知内容完整"
        ready = self.page.wait_for_function(NOTICE_READY_JS, timeout=12000)
        try:
            result = ready.json_value()
        finally:
            ready.dispose()
        self.stage = "提取可见通知"
        if result["visible_mention_count"] > len(result["items"]):
            raise ReceiverPageError("看到了提及提醒，但网页结构未能完整解析；接收已暂停")
        if not result["items"] and not result["known_end_or_empty"]:
            raise ReceiverPageError("尚未确认通知列表已加载，不能判断是否有新提醒")
        if any(not item.get("semantic_fingerprint", "").strip() for item in result["items"]):
            raise ReceiverPageError("提醒封面尚未就绪，暂不保存不完整候选")
        return result["items"]

    def ui_state(self) -> dict:
        """Non-sensitive state for cycle metrics, without changing page focus."""
        state = self.page.evaluate("""() => ({visibility_state:document.visibilityState, focused:document.hasFocus()})""")
        state["panel_visible"] = self._visible_text("互动消息").first.is_visible()
        state["page_count"] = len(self.page.context.pages)
        entry = self._visible_text("通知").first
        state["entry_hit"] = entry.evaluate("""el => {const r=el.getBoundingClientRect();
            const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
            return !!hit && el.parentElement.contains(hit);}""") if entry.is_visible() else None
        return state

    def diagnostics(self) -> dict:
        """Public UI structure and bounded DM test evidence; no auth or URLs."""
        self._check_challenge()
        labels = ("通知", "互动消息", "全部消息", "消息", "评论", "评论和@", "评论与@", "全部通知", "暂无消息", "暂时没有更多了", "加载中", "加载失败", "刷新重试", "保存登录信息", "下次再说", "知道了")
        result = {"stage": self.stage, "visible_labels": {text: self._visible_text(text).count() for text in labels}}
        result["dom_structure"] = self.page.evaluate(r"""() => {
          const rect = el => {const r=el.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height};};
          const visible = el => {const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight;};
          const marker = /DM-[A-Z0-9-]{4,60}/;
          const nodes = Array.from(document.querySelectorAll('pre,span,p,div')).filter(el=>
            visible(el) && marker.test(el.innerText || '') && !Array.from(el.children).some(c=>marker.test(c.innerText || ''))).slice(0,2);
          return {viewport:{width:innerWidth,height:innerHeight},pre_nodes:Array.from(document.querySelectorAll('pre')).slice(0,20).map(el=>({rect:rect(el),visible:visible(el),text_length:(el.innerText||'').length})),
            test_marker_nodes:nodes.map(el=>{const parents=[];let p=el;for(let i=0;p&&i<7;i++,p=p.parentElement){const t=p.innerText||'';
              parents.push({tag:p.tagName,e2e:p.getAttribute('data-e2e'),rect:rect(p),text_length:t.length,markers:Array.from(new Set(t.match(/DM-[A-Z0-9-]{4,60}/g)||[])).slice(0,5)});}return parents;})};
        }""")
        entry = self._visible_text("通知").first
        if entry.is_visible():
            result["entry_structure"] = entry.evaluate("""el => {
              const out = []; let p = el;
              const er = el.getBoundingClientRect();
              const hit = document.elementFromPoint(er.x+er.width/2,er.y+er.height/2);
              for (let i=0; p && i<4; i++, p=p.parentElement) {
                const r=p.getBoundingClientRect();
                out.push({tag:p.tagName, role:p.getAttribute('role'), e2e:p.getAttribute('data-e2e'),
                  rect:{x:r.x,y:r.y,width:r.width,height:r.height}});
              } return {ancestors:out,hit:hit?{tag:hit.tagName,role:hit.getAttribute('role'),e2e:hit.getAttribute('data-e2e'),cls:hit.className,id:hit.id,inside_button:el.parentElement.contains(hit)}:null};
            }""")
        return result

    def detail(self, notice: dict, *, keep_open=False) -> dict:
        from playwright.sync_api import TimeoutError as PageTimeout
        self._check_challenge()
        self.stage = "打开提醒对应评论"
        # Revalidate the live row immediately before clicking a possibly shifted index.
        current = self.page.evaluate(NOTICE_JS)["items"]
        same = [v for v in current if all(v.get(k) == notice.get(k) for k in
                ("source_profile_url", "exact_text", "semantic_fingerprint"))]
        if len(same) != 1:
            raise ReceiverPageError("提醒位置或内容已改变，保留待重试")
        self.page.locator("pre").nth(same[0]["pre_index"]).click(timeout=8000)
        try:
            self.stage = "等待原评论与视频信息完整"
            payload = {"sender":notice["source_profile_url"],"wanted":notice["exact_text"],"receiver":self.expected_profile_url}
            try:
                ready = self.page.wait_for_function(DETAIL_READY_JS, arg=payload, timeout=15000)
                try:
                    return ready.json_value()
                finally:
                    ready.dispose()
            except PageTimeout:
                self._check_challenge()
                detail = self.page.evaluate(DETAIL_JS, payload)
                if detail.get("ok"):
                    return detail
                raise ReceiverDetailError(detail.get("reason", "原评论关联不完整"), detail.get("diagnostics", {})) from None
        finally:
            # Read first; never send Escape to an authentication or challenge dialog.
            self._check_challenge()
            if not keep_open:
                self.page.keyboard.press("Escape")

    def _reply_state(self, target, text):
        from app.mention_agent.publisher import ReplyPreflightError
        if target.get('followup_evidence') is not None:
            from .followup_browser import followup_reply_state
            return followup_reply_state(self, target, text)
        if target.get('choice_evidence') is not None:
            from .choice_browser import choice_reply_state
            return choice_reply_state(self, target, text)
        self._check_challenge()
        value = self.page.evaluate(REPLY_STATE_JS, {
            "sender": target["sender_profile_url"], "receiver": target["receiver_profile_url"],
            "wanted": target["comment_text"], "video": target["video_id"],
            "native_comment_id": target["native_comment_id"], "reply": text,
            "identity_fresh": 0 <= time.monotonic() - getattr(self, '_reply_identity_checked_at', -10000) <= 120,
            "expected_public_number": self.expected_number})
        if not value.get("ok"):
            raise ReplyPreflightError(value.get("reason_code", "original_target_changed"))
        if value.get("current_receiver_verified") is not True:
            raise ReplyPreflightError("current_actor_not_verified")
        return value

    def open_original_from_notifications(self, target: dict, *, max_candidates=3,
                                         native_candidate_correlation=False) -> dict:
        """Open one frozen original, without acquiring or filling a reply editor.

        At most three distinct visible cards are inspected. A card's cover is
        only a way to reopen that card, never evidence of a video ID. Every
        candidate must be readable, and the sole full match is reopened and
        checked again before its notification thread is left open.
        """
        if type(native_candidate_correlation) is not bool:
            raise ValueError('invalid_native_correlation_mode')
        if type(max_candidates) is not int or not 1 <= max_candidates <= 3:
            raise ValueError('invalid_original_lookup_limit')
        counts = dict(candidate_limit=max_candidates, candidate_count=0, checked_count=0,
                      match_count=0, unreadable_count=0, final_rechecks=0, final_verified=0,
                      snapshot_changes=0, page_open_count=0, lookup_stage_index=0, reason_code='checking')
        self.last_original_lookup = counts
        self._reply_identity_checked_at = -10000

        # Last attempted boundary only, never proof that the stage succeeded:
        # 0 target, 1 SELF navigation, 2 identity, 3 visible notices, 4 history
        # handle, 5 history scroll, 6 history hydration/read, 7 card position,
        # 8 candidate detail, 9 final detail, 10 matched. No page text/URL or
        # provider exception message is included in these fixed diagnostics.

        def reject(code):
            counts['reason_code'] = code
            raise ReceiverDetailError('原评论尚未完成唯一目标核对', dict(counts))

        def checkpoint():
            callback = getattr(self, 'choice_should_stop', None)
            def stopped():
                if callback is None:
                    return False
                try:
                    return not callable(callback) or bool(callback())
                except Exception:
                    return True
            if stopped():
                counts['reason_code'] = 'original_lookup_stopped'
                raise ReceiverPageError('原评论定位已停止')
            self.check_intervention()
            if stopped():
                counts['reason_code'] = 'original_lookup_stopped'
                raise ReceiverPageError('原评论定位已停止')

        required = {'sender_profile_url', 'receiver_profile_url', 'video_id',
                    'comment_text', 'native_comment_id', 'reference_id'}
        if (not isinstance(target, dict) or not required <= target.keys()
                or not isinstance(target['comment_text'], str) or not target['comment_text'].strip()
                or len(target['comment_text']) > 4000
                or not isinstance(target['reference_id'], str) or not 1 <= len(target['reference_id']) <= 256
                or not isinstance(target['video_id'], str) or not re.fullmatch(r'\d{5,30}', target['video_id'])
                or (target['native_comment_id'] is not None and (not isinstance(target['native_comment_id'], str)
                    or not re.fullmatch(r'\d{5,30}', target['native_comment_id'])))):
            reject('invalid_original_target')
        # Keep these immutable scalar fields fixed across all browser waits;
        # callbacks/caller-owned task dictionaries cannot retarget this lookup.
        target = {key: target[key] for key in required}
        if native_candidate_correlation and target['native_comment_id'] is None:
            reject('native_correlation_requires_source_id')
        try:
            sender = canonical_profile(target['sender_profile_url'])
            receiver = canonical_profile(target['receiver_profile_url'])
        except (ValueError, TypeError, AttributeError):
            reject('invalid_original_target')
        if receiver != self.expected_profile_url:
            reject('receiver_binding_mismatch')
        normal = lambda text: re.sub(r'\s+', '', text)
        history_enabled = False

        def matching(notices):
            if not isinstance(notices, list) or any(not isinstance(row, dict) for row in notices):
                reject('original_notifications_unavailable')
            return [row for row in notices if row.get('source_profile_url') == sender
                    and isinstance(row.get('exact_text'), str)
                    and normal(row['exact_text']) == normal(target['comment_text'])]

        def card_key(row):
            fingerprint = row.get('semantic_fingerprint', '')
            if not isinstance(fingerprint, str) or not fingerprint.strip():
                reject('original_notifications_unavailable')
            return (row['source_profile_url'], row['exact_text'], fingerprint)

        def history_snapshot(desired_key):
            """Three screens maximum, then seek/revalidate a visible card.

            The element handle lives only in this fresh SELF snapshot. Page
            positions are private locators, never part of target/evidence.
            """
            checkpoint()
            counts['lookup_stage_index'] = 4
            raw_handle = self.page.evaluate_handle(NOTICE_HISTORY_CONTAINER_JS)
            try:
                pane = raw_handle.as_element()
                if pane is None:
                    reject('original_notifications_unavailable')

                def scroll(action, top=None):
                    checkpoint()
                    counts['lookup_stage_index'] = 5
                    result = pane.evaluate(NOTICE_HISTORY_SCROLL_JS, {'action':action,'top':top})
                    checkpoint()
                    if (not isinstance(result, dict) or type(result.get('top')) not in (int,float)
                            or not 0 <= result['top'] <= 10000000 or type(result.get('at_end')) is not bool):
                        reject('original_notifications_unavailable')
                    return result

                def read_page():
                    # Allow an ordinary lazy-loaded pane to hydrate. Every
                    # bounded wait checks stop/challenge; no network shortcuts.
                    counts['lookup_stage_index'] = 6
                    for _ in range(5):
                        checkpoint(); self.page.wait_for_timeout(200)
                    checkpoint()
                    return self.notifications()

                position = scroll('seek', 0)
                notices = read_page()
                keyed, positions, content_positions = {}, {}, {}
                previous = None
                for screen in range(4):
                    # Lazy loading may grow scrollHeight after the scroll, so
                    # do not treat the old bottom measurement as a final end.
                    position = scroll('state')
                    same = matching(notices)
                    page_keys = set()
                    for row in same:
                        key = card_key(row)
                        if key in page_keys:
                            reject('original_notification_not_unique')
                        page_keys.add(key)
                        counts['lookup_stage_index'] = 7
                        absolute_top = pane.evaluate("""(el,index) => {
                          const pre=Array.from(document.querySelectorAll('pre'))[index];
                          return pre&&el.contains(pre)?pre.getBoundingClientRect().top-el.getBoundingClientRect().top+el.scrollTop:null;
                        }""", row.get('pre_index'))
                        if type(absolute_top) not in (int,float) or not -10000000 <= absolute_top <= 10000000:
                            reject('original_notifications_unavailable')
                        if key in content_positions and abs(content_positions[key]-absolute_top)>2:
                            reject('original_notification_not_unique')
                        content_positions[key] = absolute_top
                        keyed[key] = row
                        positions.setdefault(key, position['top'])
                    if len(keyed) > max_candidates:
                        reject('original_notification_candidates_exceed_limit')
                    signature = tuple(sorted(card_key(row) for row in notices))
                    if screen == 3 or position['at_end'] or signature == previous:
                        break
                    previous = signature
                    before_top = position['top']
                    position = scroll('next')
                    counts['history_scrolls'] = counts.get('history_scrolls', 0) + 1
                    notices = read_page()
                    if position['top'] <= before_top:
                        # No progress must not trigger repeated scroll attempts.
                        break
                if desired_key is not None:
                    if desired_key not in positions:
                        reject('original_notification_changed')
                    scroll('seek', positions[desired_key])
                    visible = read_page()
                    fresh = matching(visible)
                    if any(card_key(row) not in keyed for row in fresh):
                        reject('original_notification_changed')
                    live = [row for row in fresh if card_key(row) == desired_key]
                    if len(live) != 1:
                        reject('original_notification_changed')
                    keyed[desired_key] = live[0]
                return keyed
            finally:
                raw_handle.dispose()

        def snapshot(expected_keys=None, desired_key=None):
            nonlocal history_enabled
            checkpoint()
            self.stage = '通过主页重新核对原评论通知'
            counts['lookup_stage_index'] = 1
            self.page.goto(SELF_URL, wait_until='domcontentloaded', timeout=30000)
            counts['page_open_count'] += 1
            deadline = time.monotonic() + 15
            while True:
                checkpoint()
                counts['lookup_stage_index'] = 2
                if self.identity() is not None:
                    break
                if time.monotonic() >= deadline:
                    counts['reason_code'] = 'original_identity_not_ready'
                    raise ReceiverInterventionRequired('未能核对当前助手B，原评论定位已暂停')
                self.page.wait_for_timeout(250)
            self._reply_identity_checked_at = time.monotonic()
            checkpoint()
            counts['lookup_stage_index'] = 3
            notices = self.notifications()
            same = matching(notices)
            if expected_keys is None and not same:
                history_enabled = True
            if history_enabled:
                keyed = history_snapshot(desired_key)
                same = list(keyed.values())
            else:
                keyed = {}
                for row in same:
                    key = card_key(row)
                    if key in keyed:
                        reject('original_notification_not_unique')
                    keyed[key] = row
            counts['candidate_count'] = len(same)
            if not same:
                reject('original_notification_not_found')
            if len(same) > max_candidates:
                reject('original_notification_candidates_exceed_limit')
            if expected_keys is not None and set(keyed) != expected_keys:
                counts['snapshot_changes'] += 1
                reject('original_notification_changed')
            return keyed

        def inspect(row, *, final=False):
            checkpoint()
            counts['lookup_stage_index'] = 9 if final else 8
            try:
                value = self.detail(row, keep_open=True)
            except ReceiverInterventionRequired:
                raise
            except Exception:
                counts['unreadable_count'] += 1
                reject('original_candidate_unreadable')
            if not final:
                counts['checked_count'] += 1
            checkpoint()
            # detail() already requires one real @ root. These checks retain
            # that contract when comparing multiple opened notification cards.
            if (not isinstance(value, dict)
                    or value.get('sender_profile_url') != sender
                    or value.get('receiver_profile_url') != receiver
                    or not isinstance(value.get('command_text'), str)
                    or normal(value['command_text']) != normal(target['comment_text'])
                    or not isinstance(value.get('video_id'), str)
                    or not re.fullmatch(r'\d{5,30}', value['video_id'])
                    or 'native_comment_id' not in value
                    or (value['native_comment_id'] is not None and (not isinstance(value['native_comment_id'], str)
                        or not re.fullmatch(r'\d{5,30}', value['native_comment_id'])))
                    or value.get('native_comment_id_conflict')
                    or ('ok' in value and value['ok'] is not True)
                    or ('unique_count' in value and (type(value['unique_count']) is not int or value['unique_count'] != 1))
                    or any(key in value and value[key] is not True
                           for key in ('real_mention', 'sender_verified', 'video_verified'))):
                counts['unreadable_count'] += 1
                reject('original_candidate_unreadable')
            return value

        def full_match(value):
            return (value['video_id'] == target['video_id']
                    and (value['native_comment_id'] == target['native_comment_id']
                         or (native_candidate_correlation and value['native_comment_id'] is None)))

        try:
            current = snapshot()
            keys = list(current)
            frozen_keys = set(keys)
            matches = []
            matched_native_ids = {}
            same_video_keys = []
            for index, key in enumerate(keys):
                if index or history_enabled:
                    current = snapshot(frozen_keys, key)
                value = inspect(current[key])
                if value['video_id'] == target['video_id']:
                    same_video_keys.append(key)
                if full_match(value):
                    matches.append(key)
                    matched_native_ids[key] = value['native_comment_id']
                    counts['match_count'] = len(matches)
            if len(matches) != 1:
                reject('original_target_not_unique')
            if (native_candidate_correlation and matched_native_ids[matches[0]] is None
                    and len(same_video_keys) != 1):
                # A different explicitly numbered comment does not turn an
                # unnumbered sibling into positive native-ID proof. Semantic
                # fallback requires one same-video, same-author/body UI card.
                reject('original_native_semantic_ambiguous')
            # Reopen by a freshly revalidated card, not its old pre_index or
            # the last inspected video's current page. No arbitrary video URL.
            current = snapshot(frozen_keys, matches[0])
            counts['final_rechecks'] += 1
            final = inspect(current[matches[0]], final=True)
            if (not full_match(final)
                    or final['native_comment_id'] != matched_native_ids[matches[0]]):
                reject('original_target_changed')
            counts['final_verified'] = 1
            counts['lookup_stage_index'] = 10
            counts['reason_code'] = 'matched'
            if native_candidate_correlation:
                # This is an explicit correlation between a real API event and
                # a freshly, uniquely verified public UI comment. Missing DOM
                # IDs stay missing; they never become native-ID proof or alter
                # the envelope/publisher's frozen UI target. The native lane
                # separately fences semantic duplicates and records provenance.
                final = dict(final)
                final['native_correlation'] = {
                    'source_native_comment_id': target['native_comment_id'],
                    'observed_native_comment_id': final['native_comment_id'],
                    'quality': 'native_equal' if final['native_comment_id'] is not None else 'semantic_only',
                }
                final['native_ui_notice'] = {key: current[matches[0]][key] for key in (
                    'source_profile_url', 'exact_text', 'semantic_fingerprint')}
            return final
        except ReceiverInterventionRequired:
            if counts['reason_code'] == 'checking':
                counts['reason_code'] = 'intervention_required'
            raise
        except ReceiverPageError:
            if counts['reason_code'] == 'checking':
                counts['reason_code'] = 'original_lookup_unavailable'
            raise
        except Exception:
            counts['reason_code'] = 'original_lookup_unavailable'
            raise ReceiverDetailError('原评论页面暂未完成核对', dict(counts)) from None

    def prepare_reply(self, target: dict, text: str) -> dict:
        """Fresh public identity, exact parent and inline draft; never submits."""
        from app.mention_agent.publisher import ReplyPreflightError
        if target.get('followup_evidence') is not None:
            return self.prepare_followup_reply(target, text)
        if target.get('choice_evidence') is not None:
            from .choice_browser import prepare_choice_reply
            return prepare_choice_reply(self, target, text)
        try:
            self.open_original_from_notifications(target)
        except ReceiverDetailError as error:
            raise ReplyPreflightError(error.diagnostics.get('reason_code', 'original_target_changed')) from None
        # Keep notification targeting: a popular video's default first page
        # may not contain this new zero-like comment at all.
        original = self.page.get_by_text(target["comment_text"], exact=True).and_(self.page.locator(":visible"))
        original.wait_for(timeout=20000)
        if original.count() != 1:
            raise ReplyPreflightError("original_comment_not_unique")
        original.scroll_into_view_if_needed(timeout=8000)
        value = self._reply_state(target, text)
        if value["existing_matching_reply_count"]:
            raise ReplyPreflightError("existing_matching_reply")
        if value["reply_span_index"] is None:
            raise ReplyPreflightError("reply_control_not_unique")
        root = self.page.locator('[data-e2e="comment-item"]').nth(value["root_index"])
        self.stage = "打开原评论自己的回复框"
        root.locator("span").nth(value["reply_span_index"]).click(timeout=8000)
        value = self._reply_state(target, text)
        if not value["composer_unique"] or not value["composer_owned"]:
            raise ReplyPreflightError("composer_parent_not_verified")
        if value["composer_text"].strip():
            raise ReplyPreflightError("existing_composer_draft")
        self.stage = "填写一次待回复正文"
        editor = self.page.locator('[contenteditable="true"][role="combobox"]').and_(self.page.locator(":visible"))
        # fill keeps multiline text literal. Enter is never used as a sender.
        editor.fill(text, timeout=8000)
        value = self._reply_state(target, text)
        self._validate_reply_draft(value, text)
        return {key: target[key] for key in ("sender_profile_url", "receiver_profile_url", "video_id",
            "comment_text", "reference_id", "native_comment_id")} | {
            "unique_count": 1, "real_mention": True, "current_receiver_verified": True,
            "observed_at_utc": datetime.now(timezone.utc).isoformat()}

    def read_followup_reply(self, anchor, candidate, *, keep_open=True):
        from .followup_browser import read_followup_reply
        return read_followup_reply(self, anchor, candidate, keep_open=keep_open)

    def prepare_followup_reply(self, target, text):
        from .followup_browser import prepare_followup_reply
        return prepare_followup_reply(self, target, text)

    @staticmethod
    def _validate_reply_draft(value, text):
        from app.mention_agent.publisher import ReplyPreflightError
        if value["existing_matching_reply_count"]:
            raise ReplyPreflightError("existing_matching_reply")
        if (not value["composer_unique"] or not value["composer_owned"]
                or value["composer_text"].replace("\r\n", "\n").strip() != text.strip()
                or value["send_control_count"] != 1):
            raise ReplyPreflightError("draft_or_send_control_not_verified")

    def clear_prepared_reply(self, target, text):
        """Clear only this exact, freshly verified prepare-only draft."""
        self._check_challenge()
        value = self._reply_state(target, text)
        self._validate_reply_draft(value, text)
        editor = self.page.locator('[contenteditable="true"][role="combobox"]').and_(self.page.locator(':visible'))
        if editor.count() != 1:
            from app.mention_agent.publisher import ReplyPreflightError
            raise ReplyPreflightError('composer_parent_not_verified')
        editor.fill('', timeout=8000)
        self._check_challenge()

    def submit_reply(self, target: dict, text: str, *, before_submit) -> dict:
        """Exactly one click invocation after durable reservation and last gate."""
        value = self._reply_state(target, text)
        self._validate_reply_draft(value, text)
        if value.get("composer_scope") == "modal":
            editor = self.page.locator('[contenteditable="true"][role="combobox"]').and_(self.page.locator(':visible'))
            container = self.page.locator('.comment-input-container').filter(has=editor)
        else:
            container = self.page.locator('[data-e2e="comment-item"]').nth(value["root_index"])
        control = container.locator(REPLY_SEND_SELECTOR).and_(self.page.locator(":visible"))
        if control.count() != 1:
            from app.mention_agent.publisher import ReplyPreflightError
            raise ReplyPreflightError("send_control_not_unique")
        self._check_challenge()
        before_submit()
        self.stage = "仅提交一次原评论回复"
        # Any exception from this call is an uncertain outcome. No retry, Enter,
        # fallback coordinate, reload or second click exists in this method.
        control.click(timeout=8000)
        self.stage = "只读核对B原评论回读"
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self._check_challenge()
            try:
                value = self._reply_state(target, text)
            except Exception as error:
                if isinstance(error, ReceiverInterventionRequired):
                    raise
                return {"outcome": "delivery_unknown", "exact_body_in_original_thread": False}
            if value["matching_reply_count"] == 1 and value["existing_matching_reply_count"] == 1:
                result = {"outcome": "b_view_visible", "exact_body_in_original_thread": True}
                quality = value.get('reply_readback_quality')
                if isinstance(quality, str) and quality in ('weak_unique_root', 'direct_selected_parent'):
                    result['reply_readback_quality'] = quality
                return result
            if value["matching_reply_count"] > 1 or value["existing_matching_reply_count"] > 1:
                return {"outcome": "delivery_unknown", "exact_body_in_original_thread": False}
            self.page.wait_for_timeout(250)
        return {"outcome": "delivery_unknown", "exact_body_in_original_thread": False}

    def save_reply_evidence(self, target, text, path):
        self._check_challenge()
        value = self._reply_state(target, text)
        if value["matching_reply_count"] == 1:
            index = value.get('evidence_root_index', value['root_index']) if target.get('followup_evidence') is not None else value['root_index']
            self.page.locator('[data-e2e="comment-item"]').nth(index).screenshot(
                path=str(path), timeout=5000)

    def read_choice_replies(self, session, allowed_selections, *, keep_open=False,candidate_text=None):
        """One known pending root; this is not a general reply-notification inbox."""
        from .choice_browser import read_choice_replies
        options={'candidate_text':candidate_text} if candidate_text is not None else {}
        return read_choice_replies(self, session, allowed_selections, keep_open=keep_open,**options)

    def observe_current_choice_replies(self, session, allowed_selections,*,candidate_text=None):
        """Exact current DOM only, without reopening a different notification."""
        from .choice_browser import observe_current_choice_replies
        options={'candidate_text':candidate_text} if candidate_text is not None else {}
        return observe_current_choice_replies(self, session, allowed_selections,**options)
