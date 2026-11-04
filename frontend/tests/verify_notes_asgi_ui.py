"""Production notes UI -> real frozen ASGI/SQL/Worker -> synthetic LLM only.

No actual app lifespan, credentials, user database, index, media or profile is
opened. Public snapshot code is verified/copied to a fresh TEMP before imports.
Browser non-preview requests are aborted; API requests use in-process loopback.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse
import uuid


VIDEO = 'synthetic-notes-ui'
KEY = 'douyinmind.notes.requests.v1'
WATCHED = ('app/services/notes_service.py', 'app/services/notes_store.py', 'app/api/routes/notes.py',
           'app/models/entities.py', 'app/services/worker.py', 'app/services/ingestion_jobs.py',
           'app/services/material_deletion.py', 'app/services/knowledge_service.py')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def owned_temporary(snapshot, temp_base):
    with tempfile.TemporaryDirectory(prefix='dm-notes-asgi-', dir=temp_base) as directory:
        try:
            yield directory
        finally:
            # Windows must leave the owned TEMP tree before rmdir, including
            # failures during imports/setup before the Worker exists.
            os.chdir(snapshot)


def run(snapshot, out, base):
    snapshot, out = snapshot.resolve(), out.resolve()
    metadata = json.loads((snapshot / 'snapshot.json').read_text(encoding='utf-8'))
    if metadata.get('private_data_copied') is not False:
        raise ValueError('verified public-only snapshot required')
    files = metadata['public_files']
    source_hashes = {name: sha(snapshot / 'backend' / name) for name in WATCHED}
    out.mkdir(parents=True, exist_ok=True)
    temp_base = Path('C:/Users/Administrator/AppData/Local/Temp').resolve()
    with owned_temporary(snapshot, temp_base) as directory:
        temporary = Path(directory).resolve()
        assert temporary.parent == temp_base and temporary.name.startswith('dm-notes-asgi-')
        code = temporary / 'public-code' / 'backend'
        for relative, evidence in files.items():
            rel = Path(relative)
            if rel.is_absolute() or '..' in rel.parts or rel.suffix != '.py' or rel.parts[0] not in {'app', 'tests', 'scripts'}:
                raise ValueError('non-public source entry')
            original, destination = snapshot / 'backend' / rel, code / rel
            if original.resolve() != original or original.stat().st_nlink != 1:
                raise ValueError('redirected source entry')
            raw = original.read_bytes()
            if hashlib.sha256(raw).hexdigest() != evidence['sha256']:
                raise ValueError('snapshot source fingerprint changed')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        copied_before = {name: sha(code / name) for name in files}
        for name in tuple(os.environ):
            if re.search(r'(?i)(api_?key|token|secret|password|credential|cookie|session|auth|bearer|proxy|base_url|endpoint)', name) or name.upper().endswith(('_URL', '_URI')):
                os.environ.pop(name, None)
        os.environ['DATABASE_URL'] = 'sqlite:///' + (temporary / 'unused-default.sqlite3').as_posix()
        os.environ['INGESTION_JOB_DB_PATH'] = str(temporary / 'unused-default-jobs.sqlite3')
        for name in ('CHROMA_PERSIST_DIR', 'AUDIO_CACHE_DIR', 'VIDEO_CACHE_DIR', 'PLAYWRIGHT_USER_DATA_DIR', 'INGESTION_METRICS_DIR'):
            os.environ[name] = str(temporary / name.lower())
        os.chdir(temporary)
        sys.path.insert(0, str(code))

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session, sessionmaker
        from playwright.sync_api import expect, sync_playwright
        from app.api.routes import notes, knowledge, knowledge_bases
        from app.db.base import Base
        from app.models.entities import (KnowledgeBase, KnowledgeBaseVideo, VideoCache,
            KnowledgeNoteGeneration as Generation, KnowledgeNoteStep as Step)
        from app.services import notes_service as notes_module, knowledge_service as knowledge_module
        from app.services.notes_service import NotesService
        from app.services.worker import Worker
        assert Path(notes_module.__file__).resolve().is_relative_to(code)
        engine = create_engine('sqlite:///' + (temporary / 'notes-business.sqlite3').as_posix(),
                               connect_args={'check_same_thread': False})
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        worker = Worker(temporary / 'notes-jobs.sqlite3')
        effects = {'real_recognition': 0, 'real_download': 0, 'real_embedding': 0, 'real_ingestion': 0}
        def forbidden(kind):
            def call(*args, **kwargs):
                effects[kind] += 1
                raise AssertionError('forbidden non-note provider path: ' + kind)
            return call
        knowledge_module.omni_service.check_configuration = forbidden('real_recognition')
        knowledge_module.omni_service.analyze_audio = knowledge_module.omni_service.analyze_video = forbidden('real_recognition')
        knowledge_module.download_audio = knowledge_module.download_video = forbidden('real_download')
        knowledge_module.embedding_client.embed_texts = forbidden('real_embedding')
        knowledge_module.KnowledgeService._run_sync = forbidden('real_ingestion')
        knowledge_module.worker = knowledge.worker = knowledge_bases.worker = worker
        audio, video = temporary / 'audio', temporary / 'video'
        (audio / 'source_media').mkdir(parents=True); video.mkdir()
        knowledge_module.settings.audio_cache_dir, knowledge_module.settings.video_cache_dir = str(audio), str(video)

        class Index:
            def __init__(self): self.calls, self.records = [], {VIDEO}
            def delete_by_video(self, identity): self.calls.append(identity); self.records.discard(identity)
            def count(self): return len(self.records)
            def get_video_count(self): return len(self.records)
        index = Index(); knowledge_module.get_chroma_service = lambda: index

        class Model:
            def __init__(self):
                self.calls, self.fail_at, self.hold = 0, None, True
                self.entered, self.release = threading.Event(), threading.Event()
            def configuration_identity(self):
                return deepcopy({'model': '合成验证模型', 'endpoint_hash': 'e' * 64,
                                 'credential_hash': 'c' * 64, 'generation_parameters_hash': 'g' * 64})
            def chat(self, *, system_prompt, user_prompt, temperature, max_tokens, observation_sink):
                self.calls += 1
                current = self.calls
                if self.hold:
                    self.entered.set()
                    if not self.release.wait(30): raise TimeoutError('synthetic test barrier timeout')
                observation_sink({'call_id': uuid.uuid4().hex, 'request_attempts': 1,
                    'usage_complete': False, 'unknown_outcome': self.fail_at == current,
                    'known_usage': {}, 'elapsed_seconds': .01})
                if self.fail_at == current: raise TimeoutError('SYNTHETIC_PRIVATE_PROVIDER_FAILURE')
                return f'### 内容摘要\n合成整理步骤 {current}，仅用于临时接口验证。'
        model = Model()
        service = NotesService(session_factory=factory, worker=worker, llm=model, ready=lambda: True)
        notes.notes_service = notes_module.notes_service = service
        with factory() as db:
            library = KnowledgeBase(name='笔记接口验证库（合成）', name_key='notes-interface-fixture')
            db.add(library); db.flush(); base_id = library.id
            db.add(VideoCache(platform_item_id=VIDEO, title='先核对原文，再整理笔记（合成）', status='done',
                transcript_text='合成已保存原文版本一，先核对原文，再整理要点。', analysis_json='{}',
                analysis_model='synthetic-recognition', processed_at=datetime.now(timezone.utc)))
            db.flush(); db.add(KnowledgeBaseVideo(knowledge_base_id=base_id, platform_item_id=VIDEO)); db.commit()
        app = FastAPI()
        for route in (notes, knowledge, knowledge_bases): app.include_router(route.router, prefix='/api')
        def business():
            with factory() as db: yield db
        for route in (notes, knowledge, knowledge_bases): app.dependency_overrides[route.get_db] = business
        @app.get('/api/auth/douyin/login/status')
        def login(): return {'status': 'logged_out', 'message': '合成账户观察，未连接抖音'}
        @app.get('/api/chat/sessions')
        def sessions(): return {'success': True, 'items': [], 'total': 0}
        checks, calls, errors, blocked, downloads = [], [], [], [], []

        def idle():
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if not worker.activity_snapshot()['maintenance_busy']: return
                time.sleep(.01)
            raise AssertionError('temporary fake-model worker did not become idle')

        try:
            worker.start()
            with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 52001),
                    headers={'origin': 'http://127.0.0.1:5173', 'content-type': 'application/json'}) as client:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel='msedge', headless=True)
                    context = browser.new_context(viewport={'width': 1440, 'height': 1000}, locale='zh-CN', accept_downloads=True)
                    context.set_default_timeout(16000)
                    uncertain = {'post': False, 'lookup': False}
                    def intercept(route):
                        request = route.request; parsed = urlparse(request.url)
                        if parsed.netloc != urlparse(base).netloc:
                            blocked.append(request.url); route.abort(); return
                        if not parsed.path.startswith('/api/'):
                            route.continue_(); return
                        raw = request.post_data
                        calls.append({'method': request.method, 'path': parsed.path,
                                      'body': json.loads(raw) if raw else None})
                        response = client.request(request.method, parsed.path + ('?' + parsed.query if parsed.query else ''), content=raw.encode() if raw else None)
                        if parsed.path.startswith('/api/notes/requests/') and uncertain['lookup']:
                            route.fulfill(status=503, content_type='application/json', body='{"code":"NOTES_READ_UNAVAILABLE"}'); return
                        if parsed.path == '/api/notes/videos/' + VIDEO and request.method == 'POST' and uncertain['post']:
                            uncertain['post'] = False
                            route.fulfill(status=503, content_type='application/json', body='{"code":"NOTES_SUBMISSION_UNCONFIRMED","needs_review":true}'); return
                        headers = {key: value for key, value in response.headers.items()
                                   if key.lower() not in {'content-length', 'content-encoding', 'transfer-encoding'}}
                        route.fulfill(status=response.status_code, headers=headers, body=response.content)
                    context.route('**/*', intercept)
                    page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))

                    def open_panel(navigate=False):
                        if navigate:
                            page.goto(base, wait_until='networkidle')
                            local = page.get_by_role('button', name='打开本地知识库', exact=True)
                            if local.count(): local.click()
                            page.locator('.library-open').filter(has_text='笔记接口验证库').click()
                            page.get_by_role('button', name='视频资料', exact=True).click()
                        page.get_by_role('button', name='更多操作：先核对原文，再整理笔记（合成）', exact=True).click()
                        page.get_by_role('button', name='AI 笔记：生成与下载', exact=True).click()
                        expect(page.get_by_role('heading', name='AI 笔记', exact=True)).to_be_visible()

                    def note_posts(): return [call for call in calls if call['method'] == 'POST' and call['path'].startswith('/api/notes/videos/')]
                    def download(link):
                        with page.expect_download() as event: link.click()
                        data = Path(event.value.path()).read_bytes(); downloads.append(hashlib.sha256(data).hexdigest()); return data
                    def refresh(): page.get_by_role('button', name='刷新笔记', exact=True).click()
                    def confirm_generation():
                        # Keep Playwright's routing pump alive through the fresh
                        # capability GET and POST; a Python Event.wait directly
                        # after click would block those browser callbacks.
                        with page.expect_response(lambda response: response.request.method == 'POST'
                                and urlparse(response.url).path == '/api/notes/videos/' + VIDEO):
                            page.get_by_role('button', name='确认并生成', exact=True).click()

                    open_panel(True)
                    page.get_by_role('button', name='生成并保存笔记', exact=True).click()
                    page.get_by_role('button', name='暂不生成', exact=True).click()
                    assert note_posts() == [] and model.calls == 0
                    page.get_by_role('button', name='生成并保存笔记', exact=True).click()
                    confirm_generation()
                    assert model.entered.wait(4)
                    first_id = note_posts()[0]['body']['request_id']
                    activity = client.get('/api/knowledge/stats').json()
                    assert activity['active_task_kind'] == 'notes' and activity['maintenance_busy'] and activity['current_task_id'] is None
                    page.get_by_role('button', name='返回资料', exact=True).click()
                    expect(page.locator('.materials-bulk-feedback')).to_contain_text('暂不能修改资料')
                    page.get_by_role('button', name='更多操作：先核对原文，再整理笔记（合成）', exact=True).click()
                    expect(page.get_by_role('button', name='删除全部入库资料', exact=True)).to_be_disabled()
                    expect(page.get_by_role('button', name='导出原文与时间', exact=True)).to_be_enabled()
                    page.get_by_role('button', name='AI 笔记：生成与下载', exact=True).click()
                    model.hold = False; model.release.set()
                    expect(page.get_by_role('link', name='下载笔记', exact=True)).to_be_visible()
                    idle()
                    first = download(page.get_by_role('link', name='下载笔记', exact=True))
                    assert '合成已保存原文版本一'.encode() in first and model.calls == 1
                    checks.append('explicit_UI_confirmation_real_notes_worker_and_global_busy_read_access')

                    history = client.get('/api/notes/videos/' + VIDEO).json()
                    cached_id = str(uuid.uuid4())
                    cached = client.post('/api/notes/videos/' + VIDEO, json={'request_id': cached_id, 'confirm': True,
                        'expected_source_fingerprint': history['source']['fingerprint']}).json()
                    assert cached['cached'] and cached['item']['expected_call_count'] == 0
                    assert cached['item']['note_id'] == history['current_note']['note_id'] and model.calls == 1
                    refresh(); expect(page.locator('.notes-panel')).to_contain_text('下方用量属于原笔记')
                    assert client.get(cached['item']['download_url']).content == first and model.calls == 1
                    checks.append('cached_request_uses_same_fixed_artifact_and_no_new_fake_model_call')

                    with factory() as db:
                        cache = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == VIDEO))
                        cache.transcript_text = '合成长文版本二：' + '甲' * 16000
                        cache.processed_at = datetime.now(timezone.utc); db.commit()
                    refresh(); expect(page.locator('.notes-panel')).to_contain_text('基于此前保存的识别版本')
                    assert download(page.get_by_role('link', name='下载笔记', exact=True).first) == first and model.calls == 1
                    model.fail_at = 3; uncertain.update(post=True, lookup=True)
                    page.get_by_role('button', name='确认后重新生成', exact=True).first.click()
                    confirm_generation()
                    expect(page.locator('.notes-panel')).to_contain_text('请求编号')
                    idle(); assert model.calls == 3
                    unknown_id = note_posts()[-1]['body']['request_id']; before_reload = len(note_posts())
                    page.reload(wait_until='networkidle')
                    local = page.get_by_role('button', name='打开本地知识库', exact=True)
                    if local.count(): local.click()
                    page.locator('.library-open').filter(has_text='笔记接口验证库').click()
                    page.get_by_role('button', name='视频资料', exact=True).click(); open_panel()
                    expect(page.locator('.notes-panel')).to_contain_text(unknown_id)
                    assert len(note_posts()) == before_reload and model.calls == 3
                    uncertain['lookup'] = False
                    page.get_by_role('button', name='核查生成结果', exact=True).click()
                    expect(page.locator('.notes-panel')).to_contain_text('没有自动重试')
                    assert model.calls == 3
                    unknown_record = page.locator('.notes-record').filter(has_text='结果需要核对')
                    unknown_record.get_by_role('button', name='确认后重新生成', exact=True).click()
                    page.get_by_role('button', name='暂不生成', exact=True).click()
                    assert len(note_posts()) == before_reload
                    model.fail_at = None
                    unknown_record.get_by_role('button', name='确认后重新生成', exact=True).click()
                    confirm_generation()
                    expect(page.locator('.notes-panel')).to_contain_text('已复用 1 步')
                    idle(); assert model.calls == 5
                    new_id = note_posts()[-1]['body']['request_id']
                    assert new_id != unknown_id and note_posts()[-1]['body']['recovery_of'] == unknown_id
                    completed = client.get('/api/notes/requests/' + new_id).json()['item']
                    assert completed['state'] == 'done' and completed['reused_step_count'] == 1
                    assert client.get('/api/notes/requests/' + unknown_id).json()['item']['state'] == 'needs_review'
                    assert 'SYNTHETIC_PRIVATE_PROVIDER_FAILURE' not in page.locator('body').inner_text()
                    checks.append('stale_bytes_then_unknown_ack_reload_GET_only_explicit_redo_reuses_successful_step')

                    owned = [audio / f'{VIDEO}.wav', audio / f'{VIDEO}.wav.analysis.json',
                             audio / f'{VIDEO}.wav.boundary-analysis.json', audio / f'{VIDEO}.wav.boundary-packs.json',
                             audio / 'source_media' / f'{VIDEO}.mp4', video / f'{VIDEO}.mp4']
                    for path in owned: path.write_bytes(b'synthetic original media copy')
                    deleted = client.delete('/api/knowledge/videos/' + VIDEO).json()
                    assert deleted['success'] and all(not path.exists() for path in owned) and model.calls == 5
                    old_operation = deleted['operation_id']
                    with factory() as db:
                        assert all(row.state == 'deleted' and not row.source_snapshot and not row.markdown for row in db.scalars(select(Generation)))
                        assert all(not row.output for row in db.scalars(select(Step)))
                    refresh(); expect(page.locator('.notes-panel')).to_contain_text('资料已删除')
                    expect(page.get_by_role('link', name='下载笔记', exact=True)).to_have_count(0)
                    assert client.get(cached['item']['download_url']).status_code == 409
                    with factory() as db:
                        cache = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == VIDEO))
                        cache.status, cache.transcript_text, cache.processed_at = 'done', '合成重入库版本三的新原文。', datetime.now(timezone.utc)
                        db.add(KnowledgeBaseVideo(knowledge_base_id=base_id, platform_item_id=VIDEO)); db.commit()
                    index.records.add(VIDEO)
                    for path in owned: path.write_bytes(b'new synthetic import')
                    refresh(); expect(page.locator('.notes-panel')).to_contain_text('旧删除记录保留')
                    before_new = len(note_posts())
                    page.get_by_role('button', name='生成并保存笔记', exact=True).click()
                    assert len(note_posts()) == before_new and model.calls == 5
                    confirm_generation()
                    expect(page.get_by_role('link', name='下载笔记', exact=True)).to_have_count(1)
                    idle(); assert model.calls == 6
                    reimport_id = note_posts()[-1]['body']['request_id']
                    assert reimport_id not in {first_id, cached_id, unknown_id, new_id}
                    assert 'redo' not in note_posts()[-1]['body'] and 'recovery_of' not in note_posts()[-1]['body']
                    reimported = client.get('/api/notes/requests/' + reimport_id).json()['item']
                    assert reimported['reused_step_count'] == 0 and reimported['state'] == 'done'
                    fresh_bytes = download(page.get_by_role('link', name='下载笔记', exact=True))
                    assert '合成重入库版本三的新原文'.encode() in fresh_bytes
                    receipt = client.post('/api/knowledge/deletions/' + old_operation + '/resume', json={'confirm': True}).json()
                    assert receipt['already_completed'] and index.calls == [VIDEO]
                    assert all(path.read_bytes() == b'new synthetic import' for path in owned)
                    assert client.get(reimported['download_url']).content == fresh_bytes and model.calls == 6
                    checks.append('real_global_delete_erases_notes_and_media_reimport_needs_new_UUID_old_operation_is_receipt_only')

                    for label, width, height in [('desktop', 1440, 1000), ('tablet', 768, 1024), ('mobile', 390, 844)]:
                        page.set_viewport_size({'width': width, 'height': height})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                        page.screenshot(path=str(out / f'notes-real-asgi-{label}.png'), full_page=True)
                    checks.append('real_ASGI_history_at_1440_768_390_without_overflow')
                    context.close(); browser.close()
            assert not errors and not blocked and not any(effects.values())
            idle()
            stopped = worker.stop(timeout=5)
            assert stopped['stopped'] and stopped.get('state_recorded', True)
            assert source_hashes == {name: sha(snapshot / 'backend' / name) for name in WATCHED}
            assert copied_before == {name: sha(code / name) for name in files}
            report = {'passed': len(checks), 'checks': checks, 'synthetic_only': True,
                'real_ASGI_routes_SQL_and_Worker': True, 'fake_model_calls': model.calls,
                'real_provider_calls': 0, 'forbidden_provider_effects': effects,
                'page_errors': errors, 'external_requests': blocked, 'worker_stop': stopped,
                'snapshot': str(snapshot), 'snapshot_metadata_sha256': sha(snapshot / 'snapshot.json'),
                'critical_source_sha256': source_hashes, 'copied_public_files': len(files),
                'source_and_copy_stable': True, 'downloads_sha256': downloads,
                'api_calls': calls, 'app_lifespan_started': False,
                'scope': 'Fresh Edge against production assets; in-process ASGI trusted loopback peer/Origin, TEMP SQL/media/Worker and injected fake LLM. No actual profiles/providers/production writes or physical-device claim.'}
            (out / 'notes-asgi-browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'passed': len(checks), 'fake_model_calls': model.calls, 'report': str(out / 'notes-asgi-browser-report.json')}))
        finally:
            model.release.set(); worker.stop(timeout=5)
            engine.dispose()
            os.chdir(snapshot)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base', default='http://127.0.0.1:5199')
    args = parser.parse_args()
    run(args.source_snapshot, args.output, args.base)
