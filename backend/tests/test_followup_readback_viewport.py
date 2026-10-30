"""Followup readback viewport: intercepted DOM reads and fake-only submission."""
from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from app.mention_receiver import followup_browser as followup
from app.mention_receiver import choice_browser as choice
from app.mention_receiver.browser import NormalPageReceiver, ReceiverDetailError, ReceiverInterventionRequired, ReceiverPageError
from tests.test_conversation_followup_browser import setup, browser, surface, QUESTION, ANSWER
from tests.test_conversation_followup_evidence import binding as proof_binding, detail as proof_detail
from app.mention_agent.followup_evidence import make_followup_envelope


def spread(surface, monkeypatch):
    receiver, anchor, candidate = setup(surface, monkeypatch)
    surface.page.locator('#selection').evaluate(
        "e=>{const top=e.getBoundingClientRect().top;e.style.marginTop=Math.max(0,1000-top)+'px';}")
    surface.page.set_viewport_size({'width': 1200, 'height': 800})
    surface.page.evaluate("window.inputCount=0;document.addEventListener('input',()=>window.inputCount++)")
    return receiver, anchor, candidate


def untouched(surface):
    assert surface.page.viewport_size == {'width': 1200, 'height': 800}
    assert surface.page.evaluate('window.sendCount') == surface.page.evaluate('window.openCount') == surface.page.evaluate('window.inputCount') == 0
    assert surface.page.locator('#editor').inner_text() == ''


def test_trusted1600_reads_whole_followup_then_restores800_without_editor_or_send(surface, monkeypatch):
    receiver, anchor, candidate = spread(surface, monkeypatch)
    with pytest.raises(ReceiverDetailError):
        receiver.read_followup_reply(anchor, candidate, keep_open=True)
    untouched(surface)
    receiver.readback_viewport_height = 1600
    value = receiver.read_followup_reply(anchor, candidate, keep_open=True)
    assert value['command_text'] == QUESTION and value['evidence']['root_chain_verified'] is True
    assert receiver.last_choice_readback_viewport_diagnostics['restored'] == 1
    untouched(surface)


def test1600_does_not_admit_bad_or_duplicate_root_and_always_restores(surface, monkeypatch):
    for problem in ('bad_author_header', 'duplicate_root'):
        receiver, anchor, candidate = spread(surface, monkeypatch)
        receiver.readback_viewport_height = 1600
        if problem == 'bad_author_header':
            surface.page.locator('#root .comment-item-info-wrap').first.evaluate(
                "e=>e.querySelector('[data-click-from=\"title\"]').removeAttribute('data-click-from')")
        else:
            surface.page.locator('#root').evaluate("e=>{const duplicate=e.cloneNode(true);duplicate.style.position='absolute';duplicate.style.top='120px';duplicate.style.left='4px';duplicate.style.width='1192px';e.after(duplicate);}")
        with pytest.raises(ReceiverDetailError):
            receiver.read_followup_reply(anchor, candidate, keep_open=True)
        untouched(surface)


def test_followup_checkpoint_exception_after_resize_restores800(surface, monkeypatch):
    receiver, anchor, candidate = spread(surface, monkeypatch)
    receiver.readback_viewport_height = 1600
    challenge = ReceiverInterventionRequired('synthetic checkpoint challenge')
    def check():
        if surface.page.viewport_size['height'] == 1600:
            raise challenge
    receiver.check_intervention = check
    with pytest.raises(ReceiverInterventionRequired) as raised:
        receiver.read_followup_reply(anchor, candidate, keep_open=True)
    assert raised.value is challenge
    untouched(surface)


class FakeLocator:
    def __init__(self, page, index=None):
        self.page, self.index = page, index
    def nth(self, index):
        return FakeLocator(self.page, index)
    def locator(self, *args, **kwargs):
        return FakeLocator(self.page, self.index)
    def and_(self, other):
        return self
    def count(self):
        return 1
    def click(self, **kwargs):
        self.page.events.append(('click', self.page.viewport_size['height']))
        self.page.clicks += 1
        if self.page.click_error:
            raise RuntimeError('synthetic click uncertainty')
    def fill(self, text, **kwargs):
        self.page.events.append(('fill', self.page.viewport_size['height']))
        self.page.text = text
    def screenshot(self, **kwargs):
        self.page.events.append(('screenshot', self.page.viewport_size['height'], self.index))
        if self.page.screenshot_error:
            raise RuntimeError('synthetic screenshot failure')
        return b''


class FakePage:
    def __init__(self, target, mode):
        self.viewport_size = {'width': 1200, 'height': 800}
        self.changes, self.events = [], []
        self.target, self.mode = target, mode
        self.text = '' if mode == 'prepare' else ANSWER
        self.clicks = 0
        self.click_error = self.screenshot_error = False
    def set_viewport_size(self, value):
        self.changes.append(dict(value))
        self.viewport_size = dict(value)
    def locator(self, *args, **kwargs):
        return FakeLocator(self)
    def evaluate(self, script, args):
        self.events.append(('state', self.viewport_size['height']))
        proof = self.target['followup_evidence']
        raw = {'ok':True, 'sender_profile_url':self.target['sender_profile_url'],
            'receiver_profile_url':self.target['receiver_profile_url'],'video_id':self.target['video_id'],
            'command_text':self.target['comment_text'],'native_comment_id':self.target['native_comment_id'],
            'source_url':'https://www.douyin.com/video/'+self.target['video_id'],
            'sender_display_name':'合成A','comment_index':2,'evidence':{
                'direct_reply_to_ask':proof['direct_reply_to_prior'],
                'root_native_comment_id':proof['root_native_comment_id'],
                'ask_native_comment_id':proof['prior_reply_native_comment_id'],
                'observed_at_utc':proof['observed_at_utc']}}
        found = self.mode == 'evidence' or self.mode == 'submit' and self.clicks == 1
        return {'ok':True,'current_receiver_verified':True,'detail':raw,
            'root_index':2,'evidence_root_index':0,'matching_reply_count':int(found),
            'existing_matching_reply_count':int(found),'reply_span_index':3,
            'composer_unique':True,'composer_owned':True,'composer_scope':'inline',
            'composer_text':self.text,'send_control_count':1}


def fake_receiver(mode):
    detail = proof_detail()
    binding = proof_binding()
    envelope = make_followup_envelope(detail, binding)
    target = {key:value for key,value in envelope.items() if key != 'evidence'}
    target.update(followup_evidence=envelope['evidence'],binding_id=binding.binding_id,
        instance_id=binding.instance_id,knowledge_base_id=None,query_scope='all_local')
    receiver = NormalPageReceiver.__new__(NormalPageReceiver)
    receiver.page = FakePage(target, mode)
    receiver.readback_viewport_height = 1600
    receiver.choice_should_stop = lambda: False
    receiver.check_intervention = lambda: None
    receiver._check_challenge = lambda: receiver.page.events.append(('challenge', receiver.page.viewport_size['height']))
    receiver._reply_identity_checked_at = time.monotonic()
    receiver.expected_number = '1234567'
    return receiver, target, detail


def assert_fake_span(receiver):
    assert receiver.page.viewport_size == {'width':1200,'height':800}
    assert receiver.page.changes == [{'width':1200,'height':1600},{'width':1200,'height':800}]
    assert all(event[1] == 1600 for event in receiver.page.events)


def test_nested_prepare_and_state_expand_only_once_and_restore(monkeypatch):
    receiver, target, detail = fake_receiver('prepare')
    def read(current, *args, **kwargs):
        current.page.events.append(('read', current.page.viewport_size['height']))
        return deepcopy(detail)
    # Keep all public viewport/target guards; substitute only the inner DOM read.
    monkeypatch.setattr(followup, '_read_followup_reply', read)
    verification = followup.prepare_followup_reply(receiver, target, ANSWER)
    assert verification['query_only'] is True and verification['real_mention'] is False
    assert verification['reference_id'] == target['reference_id']
    assert receiver.page.clicks == 1  # Fake editor-opening click, never a DOM send.
    assert [event[0] for event in receiver.page.events].count('fill') == 1
    assert_fake_span(receiver)


def test_fake_submit_and_screenshot_keep_state_click_and_capture_inside1600(tmp_path):
    for mode in ('submit', 'evidence'):
        receiver, target, detail = fake_receiver(mode)
        gates = []
        if mode == 'submit':
            result = receiver.submit_reply(target, ANSWER,
                before_submit=lambda: gates.append(receiver.page.viewport_size['height']))
            assert result == {'outcome':'b_view_visible','exact_body_in_original_thread':True}
            assert gates == [1600] and receiver.page.clicks == 1
            assert [event[0] for event in receiver.page.events].count('state') == 2
        else:
            path = tmp_path/'synthetic-not-written.png'
            receiver.save_reply_evidence(target, ANSWER, path)
            assert ('screenshot',1600,0) in receiver.page.events
            assert receiver.page.clicks == 0 and not path.exists()
        assert_fake_span(receiver)


def test_fake_click_or_screenshot_exception_restores800_without_second_click(tmp_path):
    for mode in ('submit', 'evidence'):
        receiver, target, detail = fake_receiver(mode)
        gates = []
        receiver.page.click_error = mode == 'submit'
        receiver.page.screenshot_error = mode == 'evidence'
        with pytest.raises(RuntimeError, match='synthetic'):
            if mode == 'submit':
                receiver.submit_reply(target, ANSWER,
                    before_submit=lambda: gates.append(receiver.page.viewport_size['height']))
            else:
                receiver.save_reply_evidence(target, ANSWER, tmp_path/'never-written.png')
        assert receiver.page.clicks == (1 if mode == 'submit' else 0)
        assert gates == ([1600] if mode == 'submit' else [])
        assert_fake_span(receiver)
    # A restore error after a completed one-click/readback must propagate, never
    # retry a successful/uncertain submission to recover layout.
    receiver, target, detail = fake_receiver('submit')
    gates = []
    resize = receiver.page.set_viewport_size
    def fail_restore(value):
        if value['height'] == 800:
            receiver.page.changes.append(dict(value))
            raise RuntimeError('synthetic viewport restore failure')
        resize(value)
    receiver.page.set_viewport_size = fail_restore
    with pytest.raises(ReceiverPageError):
        receiver.submit_reply(target, ANSWER,
            before_submit=lambda: gates.append(receiver.page.viewport_size['height']))
    assert gates == [1600] and receiver.page.clicks == 1
    assert receiver.page.changes == [{'width':1200,'height':1600},{'width':1200,'height':800}]
    assert all(event[1] == 1600 for event in receiver.page.events)
