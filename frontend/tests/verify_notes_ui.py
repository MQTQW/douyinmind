"""Fresh Edge production UI against the in-memory preview only; no providers.

No real app backend/storage, account, model or platform is accessed. Every
non-preview browser request is aborted. Test controls mutate synthetic data.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlparse

from playwright.sync_api import expect, sync_playwright


KEY = 'douyinmind.notes.requests.v1'


def run(out, base):
    out.mkdir(parents=True, exist_ok=True)
    checks, errors, blocked = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, locale='zh-CN', accept_downloads=True)
        context.set_default_timeout(15000)
        context.route('**/*', lambda route: route.continue_() if urlparse(route.request.url).netloc == urlparse(base).netloc
                      else (blocked.append(route.request.url), route.abort()))
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def state(**values):
            return context.request.get(base + '/__test/state?' + urlencode({key: str(value).lower() for key, value in values.items()})).json()

        def posts():
            return [call for call in state()['calls'] if call['method'] == 'POST' and call['path'].startswith('/api/notes/videos/')]

        def open_notes(navigate=False):
            if navigate:
                page.goto(base, wait_until='networkidle')
                local = page.get_by_role('button', name='打开本地知识库', exact=True)
                if local.count(): local.click()
                page.locator('.library-open').filter(has_text='学习笔记').click()
                page.get_by_role('button', name='视频资料', exact=True).click()
            row = page.get_by_label('视频资料列表', exact=True).locator('.resource-row').first
            row.locator('button[aria-label^="更多操作："]').click()
            page.get_by_role('button', name='AI 笔记：生成与下载', exact=True).click()
            expect(page.get_by_role('heading', name='AI 笔记', exact=True)).to_be_visible()

        state(loggedIn=False, notesSupported=False, notesReady=True, notesPostUnknown=False,
              notesLookupFailure=False, notesSourceChanged=False, notesDeleted=False,
              notesHistoryFailure=False, resetNotes=True, clearCalls=True)
        open_notes(True)
        expect(page.locator('.notes-panel')).to_contain_text('更新后台')
        assert posts() == []
        assert not any('mode=ai' in call['query'] for call in state()['calls'])
        expect(page.get_by_role('link', name='导出当前原文与时间', exact=True)).to_have_attribute('href', '/api/knowledge/export/7999999000000000000?mode=original')
        checks.append('old_backend_fail_closed_preserves_original_read_without_login')

        page.get_by_role('button', name='返回资料', exact=True).click()
        state(notesSupported=True)
        open_notes()
        generate = page.get_by_role('button', name='生成并保存笔记', exact=True)
        expect(generate).to_be_enabled()
        generate.click()
        dialog = page.get_by_role('dialog', name='确认生成 AI 笔记', exact=True)
        expect(dialog).to_contain_text('可能产生模型费用')
        expect(page.get_by_role('button', name='暂不生成', exact=True)).to_be_focused()
        page.keyboard.press('Shift+Tab'); page.keyboard.press('Shift+Tab')
        expect(page.get_by_role('button', name='确认并生成', exact=True)).to_be_focused()
        page.keyboard.press('Tab')
        expect(page.get_by_role('button', name='取消生成确认', exact=True)).to_be_focused()
        page.keyboard.press('Escape')
        expect(dialog).to_have_count(0)
        expect(generate).to_be_focused()
        assert posts() == [] and page.evaluate('(key) => localStorage.getItem(key)', KEY) is None
        checks.append('fee_confirmation_focus_trap_escape_cancel_has_no_uuid_or_POST')

        generate.click(); page.get_by_role('button', name='确认并生成', exact=True).click()
        expect(page.locator('.notes-panel')).to_contain_text('等待整理')
        assert len(posts()) == 1
        saved = page.evaluate('(key) => JSON.parse(localStorage.getItem(key))', KEY)
        first_id = posts()[0]['body']['request_id']
        assert saved['items'] == [{'videoId': '7999999000000000000', 'requestId': first_id}]
        assert posts()[0]['body']['expected_source_fingerprint'] == 'a' * 64
        state(completeNotes=True)
        page.get_by_role('button', name='核查生成结果', exact=True).click()
        download = page.get_by_role('link', name='下载笔记', exact=True)
        expect(download).to_be_visible()
        with page.expect_download() as event: download.click()
        original = Path(event.value.path()).read_bytes()
        assert '合成文字稿版本 a' in original.decode('utf-8') and len(posts()) == 1
        checks.append('stable_UUID_registered_then_ready_download_is_GET_only')

        state(notesSourceChanged=True)
        page.get_by_role('button', name='刷新笔记', exact=True).click()
        expect(page.locator('.notes-panel')).to_contain_text('基于此前保存的识别版本')
        with page.expect_download() as event: page.get_by_role('link', name='下载笔记', exact=True).click()
        assert Path(event.value.path()).read_bytes() == original and len(posts()) == 1
        checks.append('stale_completed_note_download_keeps_exact_old_bytes_without_generation')
        state(notesDeleted=True)
        page.get_by_role('button', name='刷新笔记', exact=True).click()
        expect(page.locator('.notes-panel')).to_contain_text('资料已删除')
        expect(page.get_by_role('link', name='下载笔记', exact=True)).to_have_count(0)
        expect(page.get_by_role('button', name='确认后重新生成', exact=True)).to_have_count(0)
        checks.append('deleted_note_has_no_download_or_redo')

        state(notesDeleted=False, notesSourceChanged=False)
        page.get_by_role('button', name='刷新笔记', exact=True).click()
        expect(page.locator('.notes-panel')).to_contain_text('旧删除记录保留')
        expect(page.locator('.notes-panel')).to_contain_text('资料已删除')
        page.get_by_role('button', name='生成并保存笔记', exact=True).click()
        assert len(posts()) == 1
        page.get_by_role('button', name='确认并生成', exact=True).click()
        expect(page.locator('.notes-panel')).to_contain_text('等待整理')
        assert len(posts()) == 2 and posts()[1]['body']['request_id'] != first_id
        assert 'redo' not in posts()[1]['body'] and 'recovery_of' not in posts()[1]['body']
        expect(page.locator('.notes-panel')).to_contain_text('资料已删除')
        checks.append('delete_reimport_keeps_tombstone_and_requires_new_confirmed_UUID')

        page.get_by_role('button', name='返回资料', exact=True).click()
        state(resetNotes=True, notesSourceChanged=False, notesDeleted=False, notesPostUnknown=True, notesLookupFailure=True, clearCalls=True)
        page.evaluate('(key) => localStorage.removeItem(key)', KEY)
        open_notes(); page.get_by_role('button', name='生成并保存笔记', exact=True).click()
        page.get_by_role('button', name='确认并生成', exact=True).click()
        expect(page.locator('.notes-panel')).to_contain_text('请求编号')
        assert len(posts()) == 1
        unknown_id = posts()[0]['body']['request_id']
        page.reload(wait_until='networkidle')
        # Login remains synthetic/disconnected; localStorage keeps the same UUID.
        local = page.get_by_role('button', name='打开本地知识库', exact=True)
        if local.count(): local.click()
        page.locator('.library-open').filter(has_text='学习笔记').click()
        page.get_by_role('button', name='视频资料', exact=True).click()
        open_notes()
        expect(page.locator('.notes-panel')).to_contain_text(unknown_id)
        assert len(posts()) == 1
        state(notesLookupFailure=False, notesPostUnknown=False)
        page.get_by_role('button', name='核查生成结果', exact=True).click()
        expect(page.locator('.notes-panel')).to_contain_text('没有自动重试')
        assert len(posts()) == 1
        page.get_by_role('button', name='确认后重新生成', exact=True).click()
        page.get_by_role('button', name='暂不生成', exact=True).click()
        assert len(posts()) == 1
        page.get_by_role('button', name='确认后重新生成', exact=True).click()
        page.get_by_role('button', name='确认并生成', exact=True).click()
        expect(page.locator('.notes-panel')).to_contain_text('等待整理')
        assert len(posts()) == 2 and posts()[1]['body']['request_id'] != unknown_id
        assert posts()[1]['body']['redo'] is True and posts()[1]['body']['recovery_of'] == unknown_id
        checks.append('unknown_survives_reload_GET_only_then_explicit_redo_uses_new_UUID')
        state(completeNotes=True)
        page.get_by_role('button', name='核查生成结果', exact=True).click()
        expect(page.get_by_role('link', name='下载笔记', exact=True)).to_have_count(1)
        screenshots = []
        for name, width in [('desktop', 1440), ('tablet', 768), ('mobile', 390)]:
            page.set_viewport_size({'width': width, 'height': 1000})
            page.screenshot(path=str(out / f'notes-records-{name}.png'), full_page=True)
            if name == 'desktop':
                html = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', page.content(), flags=re.I)
                html = html.replace('href="/assets/', f'href="{base}/assets/')
                (out / 'notes-rendered-production-snapshot.html').write_text(html, encoding='utf-8')
                context.request.post(base + '/__test/notes-rendered', data=html, headers={'Content-Type': 'text/html'})
            screenshots.append(f'notes-records-{name}.png')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.get_by_role('button', name='确认后重新生成', exact=True).click()
            expect(page.get_by_role('dialog')).to_be_visible()
            page.screenshot(path=str(out / f'notes-confirm-{name}.png'), full_page=True)
            screenshots.append(f'notes-confirm-{name}.png')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.keyboard.press('Escape')
        checks.append('responsive_records_and_confirmation_at_1440_768_390_no_horizontal_overflow')
        assert not errors and not blocked
        assert not any('mode=ai' in call['query'] for call in state()['calls'])
        result = {'checks': checks, 'passed': len(checks), 'page_errors': errors, 'external_requests_blocked': blocked,
                  'original_note_sha256': hashlib.sha256(original).hexdigest(), 'screenshots': screenshots,
                  'scope': 'Fresh headless Edge production UI plus in-memory synthetic API; no real providers/backend/storage. Desktop viewport emulation, not a real phone.'}
        (out / 'notes-browser-report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        browser.close()
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base', default='http://127.0.0.1:5199')
    args = parser.parse_args()
    run(args.output, args.base)
