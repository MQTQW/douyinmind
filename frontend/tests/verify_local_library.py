"""Production UI checks against local-library-preview.mjs only, fresh browser.

All API responses and SSE text are synthetic. No app database, platform session,
model credentials or real backend is opened. Non-preview requests are aborted.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlencode, urlparse

from playwright.sync_api import expect, sync_playwright


def run(out, base):
    out.mkdir(parents=True, exist_ok=True)
    checks, errors, blocked = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, locale='zh-CN')
        context.set_default_timeout(12000)

        def route(request):
            if urlparse(request.request.url).netloc == urlparse(base).netloc:
                request.continue_()
            else:
                blocked.append(request.request.url)
                request.abort()

        context.route('**/*', route)
        page = context.new_page()
        page.clock.install()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def state(**values):
            return context.request.get(base + '/__test/state?' + urlencode({key: str(value).lower() for key, value in values.items()})).json()

        def ask(text):
            page.get_by_label('输入问题', exact=True).fill(text)
            page.get_by_role('button', name='发送问题', exact=True).click()
            expect(page.get_by_label('输入问题', exact=True)).to_be_enabled()
            return [call['body'] for call in state()['calls'] if call['path'] == '/api/chat/ask/stream'][-1]

        state(loggedIn=False, listFailure=False, progressFailure=False, activeTask=False, clearCalls=True)
        page.goto(base, wait_until='networkidle')
        page.get_by_role('button', name='打开本地知识库', exact=True).click()
        expect(page.get_by_role('button', name='视频资料', exact=True)).to_be_visible()
        page.locator('.library-open').filter(has_text='学习笔记').click()
        expect(page.locator('.workspace-title')).to_have_text('学习笔记（合成验证）')
        page.get_by_role('button', name='视频资料', exact=True).click()
        expect(page.get_by_label('视频资料列表')).to_be_visible()
        page.locator('.resource-title').first.click()
        expect(page.get_by_role('dialog')).to_contain_text('合成文字稿：先核对原文')
        page.get_by_label('关闭识别结果').click()
        page.get_by_role('button', name='对话', exact=True).click()
        body = ask('合成验证：资料里如何整理笔记？')
        assert body['chat_only'] is False and body['knowledge_base_id'] == 7
        assert not any(call['path'].startswith('/api/favorites/') for call in state()['calls'])
        checks.append('disconnected_local_library_read_transcript_and_scoped_answer')

        page.get_by_role('button', name='纯 AI 新对话', exact=True).click()
        expect(page.locator('.workspace-title')).to_have_text('纯 AI 对话')
        before_unsupported = len([call for call in state()['calls'] if call['path'] == '/api/chat/ask/stream'])
        page.route('**/api/chat/capabilities', lambda request: request.fulfill(status=404, content_type='application/json', body='{}'))
        page.get_by_label('输入问题', exact=True).fill('合成验证：旧后台不得收到这条纯AI问题')
        page.get_by_role('button', name='发送问题', exact=True).click()
        expect(page.locator('.chat-room')).to_contain_text('本次没有发送问题或读取本地资料')
        assert len([call for call in state()['calls'] if call['path'] == '/api/chat/ask/stream']) == before_unsupported
        page.unroute('**/api/chat/capabilities')
        checks.append('old_backend_capability_failure_blocks_AI_POST_in_the_real_UI')
        body = ask('合成验证：不读取资料的普通问题')
        assert body['chat_only'] is True and body['collection_id'] is None and 'knowledge_base_id' not in body
        assert any(call['path'] == '/api/chat/capabilities' for call in state()['calls'])
        checks.append('explicit_AI_mode_prechecks_capability_and_has_no_library_scope')

        page.locator('.history-open').filter(has_text='学习资料里的整理步骤').click()
        expect(page.get_by_label('输入问题', exact=True)).to_be_enabled()
        body = ask('合成验证：继续原来的资料对话')
        assert body['session_id'] == 81 and body['chat_only'] is False and body['knowledge_base_id'] == 7
        page.locator('.history-open').filter(has_text='不使用本地资料的对话').click()
        expect(page.get_by_label('输入问题', exact=True)).to_be_enabled()
        body = ask('合成验证：继续原来的纯AI对话')
        assert body['session_id'] == 82 and body['chat_only'] is True and 'knowledge_base_id' not in body
        checks.append('history_restores_saved_AI_or_library_mode')

        state(loggedIn=True, clearCalls=True)
        page.reload(wait_until='networkidle')
        page.locator('.library-open').filter(has_text='学习笔记').click()
        page.locator('.history-open').filter(has_text='学习资料里的整理步骤').click()
        expect(page.get_by_label('输入问题', exact=True)).to_be_enabled()
        state(loggedIn=False)
        page.clock.fast_forward(31000)
        expect(page.get_by_role('button', name='连接收藏', exact=True)).to_be_visible()
        expect(page.locator('.workspace-title')).to_have_text('学习笔记（合成验证）')
        expect(page.locator('.chat-room')).to_contain_text('这是合成历史回答')
        body = ask('合成验证：登录过期后继续当前会话')
        assert body['session_id'] == 81 and body['chat_only'] is False and body['knowledge_base_id'] == 7
        checks.append('login_expiry_preserves_open_session_scope_and_answer')

        for width, height, label in [(1440, 1000, 'desktop'), (768, 1024, 'tablet'), (390, 844, 'mobile')]:
            page.set_viewport_size({'width': width, 'height': height})
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            page.screenshot(path=str(out / f'{label}-local-disconnected.png'), full_page=True)
        checks.append('local_library_view_at_1440_768_390_without_horizontal_overflow')

        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.locator('.task-shortcut').filter(has_text='处理进度').click()
        expect(page.get_by_text('已读取 100 / 205', exact=False)).to_be_visible()
        assert page.locator('.resource-row').count() == 0
        page.get_by_role('button', name='载入更多视频', exact=True).click()
        expect(page.get_by_text('已读取 200 / 205', exact=False)).to_be_visible()
        expect(page.locator('.resource-row')).to_contain_text(['合成验证视频 151'])
        state(listFailure=True)
        page.get_by_role('button', name='刷新状态', exact=True).click()
        expect(page.get_by_role('alert')).to_contain_text('视频状态暂时未能更新')
        expect(page.locator('.resource-row')).to_contain_text(['合成验证视频 151'])
        state(listFailure=False)
        page.get_by_role('button', name='重新加载', exact=True).click()
        expect(page.get_by_text('已读取 200 / 205', exact=False)).to_be_visible()
        page.get_by_role('button', name='载入更多视频', exact=True).click()
        expect(page.get_by_text('已读取 205 / 205', exact=False)).to_be_visible()
        assert page.locator('.resource-row').count() == 2
        assert page.get_by_role('button', name='载入更多视频', exact=True).count() == 0
        checks.append('progress_loads_past_100_and_error_retry_preserves_loaded_history')
        page.screenshot(path=str(out / 'progress-all-pages.png'), full_page=True)

        state(activeTask=True, progressFailure=True)
        page.get_by_role('button', name='刷新状态', exact=True).click()
        expect(page.get_by_role('alert')).to_contain_text('任务进度暂时无法更新')
        state(progressFailure=False)
        page.get_by_role('button', name='重试读取进度', exact=True).click()
        expect(page.get_by_text('正在处理合成验证视频 13', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='重试读取进度', exact=True)).not_to_be_visible()
        checks.append('active_job_progress_network_failure_is_visible_and_retry_is_readonly')
        page.set_viewport_size({'width': 390, 'height': 844})
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.screenshot(path=str(out / 'mobile-progress.png'), full_page=True)
        all_calls = state()['calls']
        assert not any(call['path'] in {'/api/knowledge/sync', '/api/favorites/sync'} for call in all_calls)
        context.close()

        touch = browser.new_context(viewport={'width': 390, 'height': 844}, locale='zh-CN', is_mobile=True, has_touch=True, device_scale_factor=2)
        touch.route('**/*', route)
        touch.request.get(base + '/__test/state?loggedIn=false&activeTask=false')
        phone = touch.new_page()
        phone.on('pageerror', lambda error: errors.append(str(error)))
        phone.goto(base, wait_until='networkidle')
        phone.get_by_role('button', name='打开本地知识库', exact=True).tap()
        phone.get_by_role('button', name='资料库', exact=True).tap()
        phone.locator('.library-open').filter(has_text='园艺资料').tap()
        expect(phone.locator('.workspace-title')).to_have_text('园艺资料（合成验证）')
        phone.get_by_role('button', name='视频资料', exact=True).tap()
        phone.locator('.resource-title').first.tap()
        expect(phone.get_by_role('dialog')).to_contain_text('合成文字稿：先核对原文')
        phone.get_by_label('关闭识别结果').tap()
        assert not phone.evaluate('document.documentElement.scrollWidth > innerWidth')
        phone.screenshot(path=str(out / 'mobile-touch-local-materials.png'), full_page=True)
        checks.append('emulated_touch_sidebar_library_and_transcript_navigation')
        touch.close()
        browser.close()
    assert not errors, errors
    assert not blocked, blocked
    report = {'synthetic_only': True, 'checks': checks, 'runtime_errors': errors, 'external_requests': blocked,
        'browser': 'Edge Chromium headless; viewport and touch emulation, no physical-device or soft-keyboard claim',
        'model_calls': 0, 'platform_calls': 0, 'knowledge_import_calls': 0}
    (out / 'browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checks': len(checks), 'runtime_errors': len(errors), 'report': str(out / 'browser-report.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--base', default='http://127.0.0.1:5199')
    arguments = parser.parse_args()
    if urlparse(arguments.base).hostname != '127.0.0.1':
        parser.error('only the isolated loopback preview is permitted')
    run(arguments.out, arguments.base)
