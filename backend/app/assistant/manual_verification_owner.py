"""Product owner-thread relay for a HUMAN's bounded authentication UI.

No credential values are inspected, persisted or returned. Inputs require a
fresh unique visible authentication region; a comment/page alone is read-only.
"""
from __future__ import annotations

import math
import os
import re
import threading
from urllib.parse import urlsplit

from app.mention_receiver.browser import SELF_URL


_AUTH_REGION_FIND = r"""
  const visible=e=>{
    const r=e.getBoundingClientRect();
    if(r.width<=0||r.height<=0||r.right<=0||r.bottom<=0||r.x>=innerWidth||r.y>=innerHeight)return false;
    for(let a=e;a;a=a.parentElement){const s=getComputedStyle(a);
      if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||parseFloat(s.opacity||'1')<=0)return false;}
    return true;
  };
  const diag=()=>'iframe='+[...document.querySelectorAll('iframe')].filter(visible).length
    +',dialog='+document.querySelectorAll('[role="dialog"],[aria-modal="true"]').length
    +',captcha='+document.querySelectorAll('[id*="captcha"],[class*="captcha"]').length
    +',verify='+document.querySelectorAll('[id*="verify"],[class*="verify"]').length;
  const authText=/请完成下列验证|拖动滑块|请依次点击|身份验证|短信验证|安全验证|请输入验证码|发送验证码|接收短信验证码|发送短信验证|扫码登录|验证码登录|手机号登录|账号密码登录|登录抖音/;
  const strongAuth=/身份验证|安全验证|短信验证|接收短信验证码|发送短信验证|请完成下列验证/;
  const candidates=[...document.querySelectorAll('[role="dialog"],[aria-modal="true"],[id*="captcha"],[class*="captcha"],[class*="login-panel"],[class*="loginModal"],[id*="verify"],[class*="verify"]')].filter(e=>{
    if(['HTML','BODY','MAIN'].includes(e.tagName)||!visible(e)||!authText.test(e.innerText||''))return false;
    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    if(r.x<0||r.y<0||r.right>innerWidth||r.bottom>innerHeight||(r.width>=innerWidth*.95&&r.height>=innerHeight*.95))return false;
    if(e.getAttribute('role')!=='dialog'&&e.getAttribute('aria-modal')!=='true'&&!['fixed','absolute'].includes(s.position))return false;
    if([...e.querySelectorAll('main,[role="main"]')].some(visible))return false;
    if([...e.querySelectorAll('h1,[role="heading"][aria-level="1"]')].some(h=>visible(h)&&!authText.test(h.innerText||'')))return false;
    if([...e.querySelectorAll('.comment-input-inner-container,[data-e2e="comment-input"],textarea[placeholder*="评论"],textarea[placeholder*="私信"],[contenteditable="true"][class*="comment"]')].some(visible))return false;
    // Identity-verification choice panels ("receive code" / "send SMS") have
    // only buttons; interactive frames and slider/point captchas cover theirs.
    const text=e.innerText||'';
    if(!e.querySelector('input,iframe,canvas,button,[role="button"]')
        &&!/拖动滑块|请依次点击/.test(text)&&!strongAuth.test(text))return false;
    const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
    return !!hit&&(hit===e||e.contains(hit));
  });
  let roots=candidates.filter(e=>!candidates.some(o=>o!==e&&o.contains(e)));
  if(roots.length===0){
    // Some verification modals are a single centered cross-origin frame whose
    // parent carries no text; bind to that lone large centered frame only.
    const frames=[...document.querySelectorAll('iframe')].filter(visible).filter(e=>{
      const r=e.getBoundingClientRect();
      return r.width>=200&&r.height>=150&&r.x>=0&&r.y>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;
    });
    if(frames.length===1){
      const e=frames[0],r=e.getBoundingClientRect(),cx=r.x+r.width/2,cy=r.y+r.height/2;
      const centered=Math.abs(cx-innerWidth/2)<=innerWidth*.25&&Math.abs(cy-innerHeight/2)<=innerHeight*.25;
      const large=r.width*r.height>=innerWidth*innerHeight*.05;
      const hit=document.elementFromPoint(cx,cy);
      if(centered&&large&&hit===e)roots=[e];
    }
  }
"""

AUTH_REGION_SCRIPT = "() => {" + _AUTH_REGION_FIND + r"""
  if(roots.length!==1)return {known:false,hint:diag()};
  const region=roots[0],r=region.getBoundingClientRect(),a=document.activeElement;
  let focused=false;
  if(a&&region.contains(a)&&visible(a)){
    const f=a.getBoundingClientRect(),hit=document.elementFromPoint(f.x+f.width/2,f.y+f.height/2);
    focused=!!hit&&(hit===a||a.contains(hit));
  }
  return {known:true,hint:null,x:r.x,y:r.y,width:r.width,height:r.height,focused,active_tag:a&&a.tagName,
    viewport_width:innerWidth,viewport_height:innerHeight};
}"""

AUTH_HIT_SCRIPT = "p => {" + _AUTH_REGION_FIND + r"""
  const hit=document.elementFromPoint(p.x,p.y);
  return roots.length===1&&!!hit&&(hit===roots[0]||roots[0].contains(hit));
}"""

_KEYS = frozenset({'Tab','Shift+Tab','Backspace','Delete','Enter','Escape',
                  'ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'})


def verify_registered_receiver(adapter, lane):
    """Explicit finish/Start identity check only, never an input or poll loop."""
    from .native_lifecycle import visible_draft_is_empty
    adapter.check_intervention()
    if visible_draft_is_empty(adapter) is not True:
        raise ValueError('verification_existing_draft')
    adapter.page.goto(SELF_URL,wait_until='domcontentloaded',timeout=20000)
    adapter.check_intervention()
    adapter.page.get_by_text(re.compile(r'抖音号[：:\s]*\d+'),exact=False).and_(
        adapter.page.locator(':visible')).first.wait_for(state='visible',timeout=8000)
    if adapter.identity() is None:
        raise ValueError('verification_identity_unconfirmed')
    lifecycle=getattr(lane,'session_lifecycle',None)
    receiver=getattr(lane,'native_receiver',None)
    if lifecycle is None or receiver is None:
        raise ValueError('verification_identity_unconfirmed')
    lifecycle.resume_after_explicit_start()
    with lifecycle.cycle(reset_failures_on_success=False):
        if receiver.reverify_identity_checkpoint() is not True:
            raise ValueError('verification_identity_unconfirmed')
    return lane._public_native_session_status()


class ManualVerificationOwner:
    def __init__(self, adapter, lane, control, queue, run_id, *, operation_current=None, worker_inflight=None):
        self.adapter,self.lane,self.control,self.queue,self.run_id=adapter,lane,control,queue,run_id
        self.page,self.context=adapter.page,adapter.page.context
        self._thread,self._pid=threading.get_ident(),os.getpid()
        if operation_current is not None and not callable(operation_current):
            raise ValueError('verification_owner_required')
        self._operation_current=operation_current
        self._worker_inflight=worker_inflight or (lambda:False)

    def _guard(self):
        if threading.get_ident()!=self._thread or os.getpid()!=self._pid:
            raise ValueError('verification_owner_required')
        if self._operation_current is not None and self._operation_current() is not True:
            raise ValueError('verification_operation_expired')
        current=self.control.snapshot()
        if (current.get('run_id')!=self.run_id or current.get('desired_state')!='paused'
                or current.get('state')!='paused' or not current.get('process_alive')
                or current.get('worker_state')!='idle'
                or current.get('receiver_state') not in {'verification_required','login_required'}
                or self.adapter.page is not self.page or self.page.context is not self.context
                or self.page.is_closed()):
            raise ValueError('verification_not_ready')
        url=urlsplit(self.page.url)
        if url.scheme!='https' or url.hostname!='www.douyin.com' or url.username or url.password or url.port not in (None,443):
            raise ValueError('verification_page_not_allowed')
        counts=self.queue.counts()
        if self._worker_inflight() is True or any(counts.get(key,0) for key in ('executing','publishing')):
            raise ValueError('verification_worker_busy')
        if self._operation_current is not None and self._operation_current() is not True:
            raise ValueError('verification_operation_expired')

    def allowed(self):
        current=self.control.snapshot()
        counts=self.queue.counts()
        return {**{k:current.get(k) for k in ('state','desired_state','run_id','process_alive','receiver_state','worker_state')},
                'publication_busy':bool(counts.get('publishing')),
                'queue_worker_busy':bool(counts.get('executing') or self._worker_inflight() is True)}

    def _validate_region(self, proof, *, focused=False):
        if (type(proof) is not dict or proof.get('known') is not True
                or any(type(proof.get(k)) not in (int,float) or not math.isfinite(proof[k])
                       for k in ('x','y','width','height','viewport_width','viewport_height'))
                or proof['width']<=0 or proof['height']<=0
                or (focused and proof.get('focused') is not True)):
            raise ValueError('verification_region_unknown')
        return proof

    def _region(self, *, focused=False):
        return self._validate_region(self.page.evaluate(AUTH_REGION_SCRIPT), focused=focused)

    def _point(self, payload):
        if type(payload) is not dict or set(payload)!={'x','y'}:
            raise ValueError('verification_input_invalid')
        proof=self._region()
        x,y=payload['x'],payload['y']
        if (type(x) not in (int,float) or type(y) not in (int,float)
                or not math.isfinite(x) or not math.isfinite(y)
                or not 0<=x<proof['viewport_width'] or not 0<=y<proof['viewport_height']
                or not proof['x']<=x<proof['x']+proof['width']
                or not proof['y']<=y<proof['y']+proof['height']
                or self.page.evaluate(AUTH_HIT_SCRIPT,{'x':x,'y':y}) is not True):
            raise ValueError('verification_input_outside_region')
        return x,y

    def __call__(self, action, payload):
        self._guard()
        if action=='screenshot':
            size=self.page.viewport_size
            if type(size) is not dict or not 1<=size.get('width',0)<=1920 or not 1<=size.get('height',0)<=1200:
                raise ValueError('verification_frame_unavailable')
            proof=self.page.evaluate(AUTH_REGION_SCRIPT)
            known=False
            hint=None
            if type(proof) is dict:
                raw=proof.get('hint')
                hint=raw if type(raw) is str and re.fullmatch(r'[a-z0-9=,]{0,120}',raw) else None
                if proof.get('known') is True:
                    try:
                        self._validate_region(proof);known=True
                    except ValueError:
                        known=False
            return {'ok':True,'frame':self.page.screenshot(type='png',scale='css',timeout=5000),
                    'width':size['width'],'height':size['height'],
                    'input_enabled':known,'auth_region_known':known,
                    'region_hint':None if known else hint}
        if action in {'begin','cancel'}:
            return {'ok':True}
        if action=='finish':
            status=verify_registered_receiver(self.adapter,self.lane)
            self.control.heartbeat(self.run_id,native_session_status=status)
            self._guard()
            return {'ok':True,'verified':True}
        if action=='click':
            x,y=self._point(payload);self._guard();self.page.mouse.click(x,y)
        elif action=='drag':
            points=payload.get('points') if type(payload) is dict and set(payload)=={'points'} else None
            if type(points) is not list or not 2<=len(points)<=80:
                raise ValueError('verification_input_invalid')
            # Validate every HUMAN-recorded point before mouse-down; no solver
            # or invented path, and recheck the live region on every move.
            previous=-1
            for point in points:
                if (type(point) is not dict or set(point)!={'x','y','t'}
                        or type(point['t']) is not int or not 0<=point['t']<=10000 or point['t']<previous):
                    raise ValueError('verification_input_invalid')
                self._point({'x':point['x'],'y':point['y']});previous=point['t']
            if points[0]['t']!=0 or points[-1]['t']<1:
                raise ValueError('verification_input_invalid')
            first=self._point({'x':points[0]['x'],'y':points[0]['y']})
            self._guard();self.page.mouse.move(*first)
            self._guard();self.page.mouse.down()
            try:
                previous=0
                for point in points[1:]:
                    self._guard()
                    self.page.wait_for_timeout(point['t']-previous)
                    self._guard()
                    target=self._point({'x':point['x'],'y':point['y']})
                    self._guard();self.page.mouse.move(*target)
                    previous=point['t']
            finally:self.page.mouse.up()
        elif action=='key':
            if type(payload) is not dict or set(payload)!={'key'} or payload['key'] not in _KEYS:
                raise ValueError('verification_input_invalid')
            self._region(focused=True);self._guard();self.page.keyboard.press(payload['key'])
        elif action=='text':
            text=payload.get('text') if type(payload) is dict and set(payload)=={'text'} else None
            if type(text) is not str or not 1<=len(text)<=128 or any(ord(c)<32 or ord(c)==127 for c in text):
                raise ValueError('verification_input_invalid')
            proof=self._region(focused=True)
            if proof.get('active_tag') not in {'INPUT','IFRAME'}:
                raise ValueError('verification_region_unknown')
            self._guard()
            self.page.keyboard.insert_text(text)
        else:
            raise ValueError('verification_input_invalid')
        # No follow-up Enter/click, no text echo, no model or task mutation.
        return {'ok':True}
