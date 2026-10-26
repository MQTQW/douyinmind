"""Only temporary queue, fake Popen and in-memory ASGI. No browser/account."""
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant.background_mode import (product_browser_headless, background_receiver_message,
                                           background_finished_message)
from app.assistant.control import AssistantControl
from app.assistant.queue import AssistantQueue, QueueError
from app.assistant import routes


class BackgroundPolicyTests(unittest.TestCase):
    def test_default_mode_is_usable_by_product_launcher_without_process_creation(self):
        calls=[]
        class FakeChromium:
            def launch_persistent_context(self,profile,**options):
                calls.append((profile,options)); return object()
        with patch('subprocess.Popen',side_effect=AssertionError('real process forbidden')):
            self.assertIs(product_browser_headless(),True)
            FakeChromium().launch_persistent_context('synthetic-owned-B-profile',
                headless=product_browser_headless(mode='background'),viewport={'width':1100,'height':800})
        self.assertIs(calls[0][1]['headless'],True)

    def test_visible_modes_boolean_or_http_payload_never_become_launch_options(self):
        for value in (False,True,None,'headed','headful','visible','BACKGROUND','',
                      {'headless':False},['background'],b'background'):
            with self.subTest(value_type=type(value).__name__),self.assertRaisesRegex(ValueError,'^assistant_background_mode_required$'):
                product_browser_headless(mode=value)

    def test_environment_cannot_silently_override_daily_background_policy(self):
        with patch.dict('os.environ',{'HEADLESS':'false','DOUYIN_HEADLESS':'0','ASSISTANT_BROWSER_MODE':'headed'}):
            self.assertIs(product_browser_headless(),True)

    def test_human_messages_do_not_promise_visible_window_or_automatic_verification(self):
        for state in ('verification_required','login_required'):
            value=background_receiver_message(state)
            self.assertIn('后台抖音会话',value); self.assertIn('本人',value)
            self.assertIn('等待人工处理',value)
            for forbidden in ('助手窗口','自动完成','已验证','自动打开'):
                self.assertNotIn(forbidden,value)
        with self.assertRaisesRegex(ValueError,'^invalid_receiver_state$'):
            background_receiver_message('SYNTHETIC_SECRET_DO_NOT_RENDER')

    def test_close_reason_compatibility_and_no_raw_reason_in_user_message(self):
        self.assertEqual(background_finished_message('assistant_window_closed'),
                         background_finished_message('assistant_session_closed'))
        self.assertIn('会话已关闭',background_finished_message('assistant_window_closed'))
        self.assertIn('未能启动',background_finished_message('runtime_start_failed'))
        self.assertNotIn('SYNTHETIC_SECRET',background_finished_message('SYNTHETIC_SECRET'))


class BackgroundControlTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='assistant-background-fixture-')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.queue=AssistantQueue(self.root/'queue.sqlite3')
        self.spawns=[]; self.alive=set()
        def popen(command,**kwargs):
            pid=98000+len(self.spawns); self.alive.add(pid)
            self.spawns.append((command,kwargs)); return SimpleNamespace(pid=pid)
        self.control=AssistantControl(self.queue,popen_factory=popen,process_probe=lambda pid:pid in self.alive)

    def running(self):
        state=self.control.start(); self.control.register(state['run_id'],state['pid'])
        return state['run_id']

    def test_start_keeps_fixed_registered_command_and_only_one_fake_owner(self):
        first=self.control.start(); second=self.control.start()
        self.assertEqual(first['run_id'],second['run_id']); self.assertEqual(len(self.spawns),1)
        command,options=self.spawns[0]
        self.assertEqual(command,[sys.executable,'-m','app.assistant','--state-dir',str(self.root),'--run-registered'])
        self.assertIs(options['shell'],False); self.assertEqual(options['stdout'],subprocess.DEVNULL)
        self.assertEqual(options['creationflags'],getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(first['receiver_state'],'starting')
        self.assertIn('后台',first['message']); self.assertIn('尚未就绪',first['message'])

    def test_explicit_resume_keeps_human_health_until_fresh_page_check(self):
        run=self.running()
        self.control.set_receiver_health(run,'verification_required','human_intervention_required')
        self.control.pause(); self.control.heartbeat(run)
        waiting=self.control.snapshot()
        self.assertEqual(waiting['desired_state'],'paused')
        self.assertEqual(waiting['receiver_state'],'verification_required')
        resumed=self.control.start()
        self.assertEqual(resumed['run_id'],run); self.assertEqual(len(self.spawns),1)
        self.assertEqual(resumed['receiver_state'],'verification_required')
        self.assertIn('等待人工处理',resumed['message'])
        self.control.set_receiver_health(run,'ready')
        self.assertEqual(self.control.snapshot()['message'],'后台抖音会话已就绪。')

    def test_page_closed_finish_cannot_leave_stale_ready_message(self):
        run=self.running(); self.control.set_receiver_health(run,'ready')
        closed=self.control.finish(run,'assistant_window_closed')
        self.assertEqual((closed['state'],closed['desired_state'],closed['pid']),('stopped','stopped',None))
        self.assertEqual(closed['reason_code'],'assistant_window_closed')
        self.assertEqual(closed['receiver_state'],'page_unavailable')
        self.assertIn('后台抖音会话已关闭',closed['message']); self.assertNotIn('已就绪',closed['message'])

    def test_stop_remains_cooperative_with_the_same_run_stop_file(self):
        run=self.running(); pid=self.control.snapshot()['pid']
        stopped=self.control.stop()
        self.assertEqual(stopped['state'],'stopping'); self.assertIn('正在停止',stopped['message'])
        self.assertTrue(self.control.stop_path(run).is_file()); self.assertIn(pid,self.alive)
        self.assertTrue(self.control.should_stop(run)); self.assertEqual(len(self.spawns),1)
        with self.assertRaisesRegex(QueueError,'runtime_stopping'): self.control.start()
        finished=self.control.finish(run)
        self.assertEqual(finished['state'],'stopped'); self.assertIn('已停止',finished['message'])

    def test_invalid_health_input_is_fixed_and_does_not_mutate_control(self):
        run=self.running(); before=self.queue.get_control()
        for state in ('unknown',[],True):
            with self.subTest(state_type=type(state).__name__),self.assertRaisesRegex(QueueError,'invalid_receiver_state'):
                self.control.set_receiver_health(run,state)
        self.assertEqual(self.queue.get_control(),before)

    def test_http_start_still_rejects_browser_profile_or_headed_override(self):
        app=FastAPI(); app.include_router(routes.router,prefix='/api')
        service=routes.AssistantAPI(self.queue,self.control)
        app.dependency_overrides[routes.get_service]=lambda:service
        with TestClient(app,base_url='http://127.0.0.1:8000',client=('127.0.0.1',50000)) as client:
            for body in ({'headless':False},{'browser_mode':'headed'},{'profile':'arbitrary-profile'}):
                with self.subTest(field=next(iter(body))):
                    self.assertEqual(client.post('/api/assistant/start',json=body).status_code,422)
            self.assertEqual(self.spawns,[])
            self.assertEqual(client.post('/api/assistant/start',json={}).status_code,200)
        self.assertEqual(len(self.spawns),1)


if __name__=='__main__':
    unittest.main()
