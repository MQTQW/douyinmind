"""Production UI -> real recovery routes -> TEMP SQL/worker ledgers.

Only public Python code is imported from an explicitly frozen snapshot. The
browser receives synthetic account/session observations; recovery/deletion APIs
run through in-process ASGI. The worker never starts and all provider/media
entry points fail if called. No real app DB, B profile or key file is opened.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlparse


def run(snapshot, out, base):
    snapshot = snapshot.resolve()
    if not (snapshot / 'origin-public-manifest.json').is_file():
        raise ValueError('a frozen public-only source snapshot is required')
    out.mkdir(parents=True, exist_ok=True)
    watched = ['app/services/knowledge_service.py', 'app/api/routes/knowledge.py', 'app/services/ingestion_jobs.py']
    source_hashes = {name: hashlib.sha256((snapshot / 'backend' / name).read_bytes()).hexdigest() for name in watched}
    with tempfile.TemporaryDirectory(prefix='dm-recovery-browser-') as directory:
        temporary = Path(directory).resolve()
        assert temporary.parent == Path(tempfile.gettempdir()).resolve() and temporary.name.startswith('dm-recovery-browser-')
        for name in tuple(os.environ):
            if re.search(r'(?i)(api_?key|token|secret|password|credential|cookie|session|auth|bearer|proxy|base_url|endpoint)', name) or name.upper().endswith(('_URL', '_URI')):
                os.environ.pop(name, None)
        os.environ['DATABASE_URL'] = 'sqlite:///' + (temporary / 'default-unused.sqlite3').as_posix()
        os.environ['INGESTION_JOB_DB_PATH'] = str(temporary / 'default-unused-jobs.sqlite3')
        for name in ('CHROMA_PERSIST_DIR', 'AUDIO_CACHE_DIR', 'VIDEO_CACHE_DIR', 'PLAYWRIGHT_USER_DATA_DIR', 'INGESTION_METRICS_DIR'):
            os.environ[name] = str(temporary / name.lower())
        os.chdir(temporary)
        sys.path.insert(0, str(snapshot / 'backend'))

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session
        from playwright.sync_api import expect, sync_playwright
        from app.api.routes import knowledge, knowledge_bases
        from app.db.base import Base
        from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, KnowledgeDeletionOperation, VideoCache
        from app.services import knowledge_service as service
        from app.services.worker import Worker

        engine = create_engine('sqlite:///' + str(temporary / 'business.sqlite3'), connect_args={'check_same_thread': False})
        Base.metadata.create_all(engine)
        worker = Worker(temporary / 'jobs.sqlite3')
        effects = {'pipeline': 0, 'recognition': 0, 'download': 0, 'embedding': 0}
        def forbidden(kind):
            def call(*args, **kwargs):
                effects[kind] += 1
                raise AssertionError('forbidden synthetic UI effect: ' + kind)
            return call
        service.worker = knowledge.worker = knowledge_bases.worker = worker
        service.omni_service.check_configuration = lambda: None
        service.omni_service.analyze_audio = service.omni_service.analyze_video = forbidden('recognition')
        service.download_audio = service.download_video = forbidden('download')
        service.embedding_client.embed_texts = forbidden('embedding')
        service.KnowledgeService._run_sync = forbidden('pipeline')
        service.settings.audio_cache_dir = str(temporary / 'audio')
        service.settings.video_cache_dir = str(temporary / 'video')
        (temporary / 'audio').mkdir(); (temporary / 'video').mkdir()
        class Index:
            fail = True
            calls = []
            records = {'synthetic-done'}
            def delete_by_video(self, identity):
                self.calls.append(identity)
                if self.fail: raise RuntimeError('PRIVATE_SYNTHETIC_INDEX_ERROR')
                self.records.discard(identity)
            def count(self): return len(self.records)
            def get_video_count(self): return len(self.records)
        index = Index(); service.get_chroma_service = lambda: index
        with Session(engine) as db:
            library = KnowledgeBase(name='恢复验证资料库（合成）', name_key='recovery-ui')
            db.add(library); db.flush(); base_id = library.id
            for identity, status, title in [('synthetic-done', 'done', '合成已完成的资料'),
                    ('synthetic-failed', 'failed', '合成失败资料'), ('synthetic-interrupted', 'transcribing', '合成中断资料')]:
                db.add(VideoCache(platform_item_id=identity, title=title, status=status,
                    transcript_text='保留的合成原文 ' + identity, analysis_json='{}'))
            db.flush()
            db.add_all(KnowledgeBaseVideo(knowledge_base_id=base_id, platform_item_id=identity)
                for identity in ['synthetic-done', 'synthetic-failed', 'synthetic-interrupted'])
            db.commit()
        ledger = worker._get_ledger(create=True, owner=True)
        old_id = '11111111-1111-4111-8111-111111111111'
        ledger.register(old_id, worker._owner_token, {'platform_item_ids': ['synthetic-failed', 'synthetic-interrupted'],
            'knowledge_base_id': base_id, 'analysis_mode': 'audio'})
        worker._cache(old_id, ledger.finish(old_id, worker._owner_token, 'interrupted'))
        original_job = worker.get_progress(old_id)
        app = FastAPI(); app.include_router(knowledge.router, prefix='/api'); app.include_router(knowledge_bases.router, prefix='/api')
        def business():
            with Session(engine, expire_on_commit=False) as db: yield db
        app.dependency_overrides[knowledge.get_db] = business
        @app.get('/api/auth/douyin/login/status')
        def login(): return {'status': 'logged_in', 'message': '合成 ASGI 验证 · 未连接抖音'}
        @app.get('/api/favorites/collections')
        def collections(): return {'success': True, 'items': [], 'total': 0}
        @app.get('/api/chat/sessions')
        def sessions(): return {'success': True, 'items': [], 'total': 0}

        checks, calls, errors, blocked = [], [], [], []
        with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 52000),
                headers={'origin': 'http://127.0.0.1:5173', 'content-type': 'application/json'}) as client:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1000}, locale='zh-CN')
                context.set_default_timeout(12000)
                uncertain = {'retry': True}
                def route(request):
                    parsed = urlparse(request.request.url)
                    if parsed.netloc != urlparse(base).netloc:
                        blocked.append(request.request.url); request.abort(); return
                    if not parsed.path.startswith('/api/'):
                        request.continue_(); return
                    body = request.request.post_data
                    calls.append({'method': request.request.method, 'path': parsed.path, 'query': parsed.query,
                        'body': json.loads(body) if body else None})
                    response = client.request(request.request.method, parsed.path + ('?' + parsed.query if parsed.query else ''),
                        content=body.encode() if body else None)
                    if parsed.path.endswith('/retry') and uncertain['retry'] and response.json().get('success'):
                        uncertain['retry'] = False
                        request.fulfill(status=503, content_type='application/json', body='{"code":"SYNTHETIC_ACK_UNAVAILABLE"}')
                    else:
                        request.fulfill(status=response.status_code, content_type=response.headers.get('content-type', 'application/json'), body=response.content)
                context.route('**/*', route)
                page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base, wait_until='networkidle')
                page.locator('.library-open').filter(has_text='恢复验证资料库').click()
                page.get_by_role('button', name='视频资料', exact=True).click()
                page.get_by_role('button', name='更多操作：合成已完成的资料', exact=True).click()
                page.once('dialog', lambda dialog: dialog.dismiss())
                page.get_by_role('button', name='删除全部入库资料', exact=True).click()
                assert not [call for call in calls if call['method'] == 'DELETE']
                page.get_by_role('button', name='更多操作：合成已完成的资料', exact=True).click()
                page.once('dialog', lambda dialog: dialog.accept())
                page.get_by_role('button', name='删除全部入库资料', exact=True).click()
                expect(page.get_by_role('alert')).to_contain_text('删除尚未完成')
                assert 'PRIVATE_SYNTHETIC_INDEX_ERROR' not in page.locator('body').inner_text()
                with Session(engine) as db:
                    operation = db.scalar(select(KnowledgeDeletionOperation))
                    operation_id = operation.operation_id
                    assert db.scalar(select(VideoCache.status).where(VideoCache.platform_item_id == 'synthetic-done')) == 'deleting'
                page.get_by_role('button', name='查看待完成删除', exact=True).click()
                resume = page.get_by_role('button', name='继续原删除操作：' + operation_id, exact=True)
                expect(resume).to_be_visible()
                for width, height, label in [(1440, 1000, 'desktop'), (768, 1024, 'tablet'), (390, 844, 'mobile')]:
                    page.set_viewport_size({'width': width, 'height': height})
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                    page.screenshot(path=str(out / (label + '-real-asgi-pending-delete.png')), full_page=True)
                assert not [call for call in calls if call['path'].endswith('/resume')]
                checks.append('pending_delete_at_three_sizes_never_automatically_resumes')
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.once('dialog', lambda dialog: dialog.dismiss()); resume.click()
                assert not [call for call in calls if call['path'].endswith('/resume')]
                index.fail = False
                page.once('dialog', lambda dialog: dialog.accept()); resume.click()
                expect(page.get_by_text('当前没有需要继续的删除操作。', exact=True)).to_be_visible()
                with Session(engine) as db:
                    assert db.get(KnowledgeDeletionOperation, operation_id).state == 'done'
                    assert db.scalar(select(VideoCache.transcript_text).where(VideoCache.platform_item_id == 'synthetic-done')) == ''
                checks.append('browser_confirmation_real_ASGI_partial_delete_and_same_operation_resume')

                page.get_by_role('button', name='入库任务', exact=True).click()
                retry = page.get_by_role('button', name='重新提交任务：' + old_id, exact=True)
                expect(retry).to_be_visible()
                page.once('dialog', lambda dialog: dialog.dismiss()); retry.click()
                assert not [call for call in calls if call['path'].endswith('/retry')]
                page.once('dialog', lambda dialog: dialog.accept()); retry.click()
                expect(page.get_by_text('重新提交的网络结果尚未确认。', exact=False)).to_be_visible()
                retry_calls = [call for call in calls if call['path'].endswith('/retry')]
                assert len(retry_calls) == 1 and worker.count_tasks() == 2 and worker._queue.qsize() == 1
                request_id = retry_calls[0]['body']['request_id']
                stored = page.evaluate("sessionStorage.getItem('douyinmind.ingestion-retry-intents.v1')")
                assert request_id in stored and old_id in stored
                page.reload(wait_until='networkidle')
                page.locator('.task-shortcut').filter(has_text='处理进度').click()
                page.get_by_role('button', name='入库任务', exact=True).click()
                page.get_by_role('button', name='核查重新提交结果：' + old_id, exact=True).click()
                expect(page.get_by_text('已找到本次提交对应的任务。', exact=False)).to_be_visible()
                assert len([call for call in calls if call['path'].endswith('/retry')]) == 1
                lookup = [call for call in calls if call['path'].endswith('/retry-result')][-1]
                assert request_id in lookup['query'] and lookup['method'] == 'GET'
                assert worker.count_tasks() == 2 and worker._queue.qsize() == 1
                assert worker.get_progress(old_id) == original_job
                checks.append('real_persistent_job_unknown_ack_reload_same_UUID_readonly_reconciliation')
                for width, height, label in [(1440, 1000, 'desktop'), (768, 1024, 'tablet'), (390, 844, 'mobile')]:
                    page.set_viewport_size({'width': width, 'height': height})
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                    page.screenshot(path=str(out / (label + '-real-asgi-task-history.png')), full_page=True)
                checks.append('real_recovery_history_at_1440_768_390_without_horizontal_overflow')
                context.close(); browser.close()
        assert not errors and not blocked and not any(effects.values())
        assert not worker._thread.is_alive()
        assert worker.stop(timeout=0)['stopped']
        engine.dispose()
        assert source_hashes == {name: hashlib.sha256((snapshot / 'backend' / name).read_bytes()).hexdigest() for name in watched}
        report = {'synthetic_only': True, 'real_inprocess_ASGI_routes': True, 'real_temporary_SQL_and_worker_ledgers': True,
            'source_snapshot': str(snapshot), 'source_sha256': source_hashes, 'checks': checks, 'effects': effects,
            'runtime_errors': errors, 'external_requests': blocked, 'worker_thread_started': False,
            'api_peer': 'Isolated TestClient loopback peer with the trusted frontend Origin; foreign-Origin refusal is separately tested by the strict API suite.',
            'browser': 'Fresh headless Edge, production assets; no real user profile or physical-device claim'}
        (out / 'asgi-browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        # Windows cannot remove the process's current directory. Leave the
        # verified TEMP root before its context performs the checked cleanup.
        os.chdir(snapshot)
        print(json.dumps({'checks': len(checks), 'report': str(out / 'asgi-browser-report.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-snapshot', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--base', default='http://127.0.0.1:5199')
    arguments = parser.parse_args()
    if urlparse(arguments.base).hostname != '127.0.0.1': parser.error('only isolated loopback assets are permitted')
    run(arguments.source_snapshot, arguments.out.resolve(), arguments.base)
