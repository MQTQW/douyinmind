from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import wave
import subprocess
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.services import ingestion_metrics, knowledge_service as knowledge_module
from app.services.ingestion_metrics import IngestionRun, list_ingestion_runs
from app.services.long_video_audit import audit_transcript, listening_windows
from app.services.omni_service import VideoAnalysis
from scripts.long_video_test import compile_exam, extract_windows, initialize, inspect_videos, listening_source
from scripts.observe_ingestion import update_observations


def test_stage_clock_is_durable_and_does_not_include_old_attempts(tmp_path, monkeypatch):
    tick = [0.0]
    monkeypatch.setattr(ingestion_metrics.time, 'perf_counter', lambda: tick[0])
    run = IngestionRun('123', '本地长视频', 'task', 'audio')
    tick[0] = 1; run.start_stage('download')
    tick[0] = 4; run.end_stage()
    tick[0] = 5; run.start_stage('recognition')
    tick[0] = 25; run.end_stage()
    tick[0] = 26; run.start_stage('indexing')
    tick[0] = 28; run.end_stage()
    tick[0] = 30; run.finish('done')
    saved = list_ingestion_runs('123')[0]
    assert saved['total_processing_seconds'] == 30
    assert [saved['stages'][name]['seconds'] for name in ('download','recognition','indexing')] == [3,20,2]
    assert saved['queue_wait_seconds'] is None
    run.finish('failed', error_type='must_not_overwrite_success')
    assert list_ingestion_runs('123')[0]['status'] == 'done'


def test_failure_retains_partial_stage_and_separate_retry(monkeypatch):
    tick=[0]
    monkeypatch.setattr(ingestion_metrics.time, 'perf_counter', lambda: tick[0])
    failed = IngestionRun('123', 'video', 'first','audio')
    failed.start_stage('recognition'); tick[0]=7
    failed.finish('failed',error_type='ValueError')
    tick[0]=100
    retry=IngestionRun('123','video','second','audio')
    tick[0]=102;retry.finish('done')
    runs=list_ingestion_runs('123')
    assert len(runs)==2
    assert sorted(r['total_processing_seconds'] for r in runs)==[2,7]
    assert next(r for r in runs if r['status']=='failed')['stages']['recognition']['status']=='failed'
    assert len({r['run_id'] for r in runs})==2


def test_timing_write_failure_does_not_fail_processing(tmp_path,monkeypatch):
    blocked=tmp_path/'blocked';blocked.write_text('file',encoding='utf-8')
    monkeypatch.setattr(settings,'ingestion_metrics_dir',str(blocked))
    run=IngestionRun('123','video','','audio');run.start_stage('download');run.finish('done')
    assert run.data['status']=='done'
    assert list_ingestion_runs('../outside')==[]


def test_malformed_timing_record_is_not_exposed_to_the_page(tmp_path):
    run=IngestionRun('123','video','','audio');run.finish('done')
    broken=run.path.parent/'broken.json'
    broken.write_text(json.dumps({'schema_version':1,'video_id':'123','started_at':'zzz'}),encoding='utf-8')
    assert len(list_ingestion_runs('123'))==1


def test_video_content_exposes_timings_and_legacy_unknown_separately():
    from app.api.routes.knowledge import get_video_content
    engine=create_engine('sqlite:///:memory:');Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([VideoCache(platform_item_id='new',title='new',status='done'),
                    VideoCache(platform_item_id='old',title='old',status='done')]);db.commit()
        run=IngestionRun('new','new','','audio');run.start_stage('download');run.finish('done')
        new=asyncio.run(get_video_content('new',db));old=asyncio.run(get_video_content('old',db))
        assert new['processing_metrics']['status']=='done'
        assert new['processing_metrics']['stages']['download']['seconds']>=0
        assert old['processing_metrics'] is None
    engine.dispose()


def transcript_fixture():
    return {'analysis_mode':'audio','transcript_segments':[
        {'start_seconds':0,'end_seconds':10,'text':'开头确实有说话。'},
        {'start_seconds':65,'end_seconds':75,'text':'结尾还有一句。'}],
        'raw_results':[{'start_seconds':0,'duration_seconds':60,'audio_response':'first'},
                       {'start_seconds':60,'duration_seconds':60,'audio_response':'last'}]}


def test_request_coverage_does_not_claim_silent_gaps_are_omissions():
    result=audit_transcript(120,transcript_fixture(),'开头确实有说话。\n结尾还有一句。')
    assert result['structural_status']=='passed'
    assert result['request_coverage']['ratio']==1
    assert result['speech_gaps_to_listen']
    assert '回听' in result['omission_verdict']
    assert result['automatic_checks_only'] is True


def test_missing_tail_audio_and_invalid_speech_are_detected():
    payload=transcript_fixture();payload['raw_results'].pop()
    payload['transcript_segments'].append({'start_seconds':122,'end_seconds':123,'text':'不应接受'})
    result=audit_transcript(120,payload,'开头确实有说话。\n结尾还有一句。')
    assert result['structural_status']=='needs_check'
    assert result['request_coverage']['ratio']==.5
    assert result['request_coverage']['uncovered_ranges']==[{'start_seconds':60,'end_seconds':120}]
    assert any('时间' in issue for issue in result['issues'])


def test_legacy_data_and_unknown_duration_never_report_zero_or_full_coverage():
    assert audit_transcript(120,{},'')['request_coverage'] is None
    assert audit_transcript(0,{},'')['structural_status']=='unavailable'
    assert listening_windows(0)==[]


def test_long_video_windows_cover_ends_and_every_boundary():
    windows=listening_windows(1800,60)
    assert len([w for w in windows if w['kind']=='分段交界'])==29
    assert windows[0]['start_seconds']==0
    assert next(w for w in windows if w['kind']=='结尾')['end_seconds']==1800
    assert all(0<=w['start_seconds']<w['end_seconds']<=1800 for w in windows)


def test_identical_long_adjacent_text_is_only_a_listening_clue():
    p=transcript_fixture();text='这是一段比较长的重复讲话，需要回听再确认是否真的重复。'
    p['transcript_segments']=[{'start_seconds':0,'end_seconds':5,'text':text},
                              {'start_seconds':5,'end_seconds':10,'text':text}]
    result=audit_transcript(120,p,text+'\n'+text)
    assert result['structural_status']=='passed'
    assert len(result['repeated_text_to_listen'])==1


def test_plan_freezes_old_ids_and_does_not_fabricate_questions(tmp_path):
    old={'video_id':'old','title':'旧视频','status':'done','duration_seconds':600,
         'transcript_sha256':'abc','knowledge_bases':[],'transcript_text':'','analysis':{}}
    initialize(tmp_path,[old])
    new={**old,'video_id':'new','title':'<script>alert(1)</script>'}
    result,folder=inspect_videos(tmp_path,[old,new])
    assert [v['video_id'] for v in result['videos']]==['new']
    assert result['model_calls']==result['chat_writes']==0
    sheet=json.loads((folder/'question-worksheet.json').read_text(encoding='utf-8'))
    assert len(sheet['cases'])==8
    assert all(not c['question'] and not c['expected_answer'] and c['status']=='pending_annotation' for c in sheet['cases'])
    assert '<script>alert(1)</script>' not in (folder/'长视频检查报告.html').read_text(encoding='utf-8')
    with pytest.raises(ValueError,match='存在'):
        initialize(tmp_path,[old])
    assert result['videos'][0]['latest_processing'] is None


def test_synced_pending_video_is_not_lost_when_import_finishes_after_preparation(tmp_path):
    pending={'video_id':'known-pending','title':'已同步但未入库','status':'pending','duration_seconds':0,
             'transcript_sha256':'empty','knowledge_bases':[],'transcript_text':'','analysis':{}}
    initialize(tmp_path,[pending])
    done={**pending,'status':'done','duration_seconds':900,'transcript_sha256':'changed'}
    report,_=inspect_videos(tmp_path,[done])
    assert [v['video_id'] for v in report['videos']]==['known-pending']


def test_selected_current_batch_can_include_videos_completed_during_setup(tmp_path):
    row={'video_id':'already','title':'在准备时已完成','status':'done','duration_seconds':900,
         'transcript_sha256':'stable','knowledge_bases':[],'transcript_text':'','analysis':{}}
    initialize(tmp_path,[row])
    (tmp_path/'selected-targets.json').write_text(json.dumps({'video_ids':['already']}),encoding='utf-8')
    report,_=inspect_videos(tmp_path,[row])
    assert len(report['videos'])==1


def test_polling_timings_are_marked_approximate_and_unknown_starts_not_invented():
    state={'videos':{}}
    update_observations(state,[{'video_id':'early','status':'transcribing'},
                               {'video_id':'next','status':'pending'},
                               {'video_id':'old','status':'done'}],0,'start')
    update_observations(state,[{'video_id':'next','status':'downloading'}],2,'t2')
    update_observations(state,[{'video_id':'next','status':'transcribing'}],8,'t8')
    update_observations(state,[{'video_id':'early','status':'done'},
                               {'video_id':'next','status':'done'}],20,'t20')
    assert state['videos']['early']['total_observed_seconds'] is None
    assert state['videos']['early']['stage_observations'][0]['partial_start'] is True
    assert state['videos']['next']['total_observed_seconds']==18
    assert state['videos']['next']['stage_observations'][0]['observed_seconds']==6
    assert state['videos']['old']['total_observed_seconds'] is None


def test_real_pcm_clips_are_exact_source_samples(tmp_path):
    source=tmp_path/'source.wav'
    payload=b'\x01\x02'*16000*3
    with wave.open(str(source),'wb') as f:
        f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(payload)
    clips=extract_windows(source,[{'kind':'交界','start_seconds':.5,'end_seconds':1.5}],tmp_path/'clips')
    with wave.open(str(tmp_path/'clips'/clips[0]['file'])) as f:
        assert f.getnframes()==16000
        assert f.readframes(16000)==payload[16000:48000]
    assert clips[0]['actual_seconds']==1


def test_earlier_local_video_can_supply_soundtrack_without_network(tmp_path,monkeypatch):
    from app.services.media_service import _resolve_ffmpeg_path,MediaPipelineError
    try:ffmpeg=_resolve_ffmpeg_path()
    except MediaPipelineError:pytest.skip('FFmpeg unavailable')
    from app.services import media_service
    monkeypatch.setattr(media_service,'_resolve_ffmpeg_path',lambda:ffmpeg)
    audio_dir=tmp_path/'audio';media=audio_dir/'source_media';media.mkdir(parents=True)
    monkeypatch.setattr(settings,'audio_cache_dir',str(audio_dir))
    source=media/'123.mp4'
    generated=subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-f','lavfi',
        '-i','sine=frequency=440:sample_rate=16000','-t','2','-c:a','aac',str(source)],capture_output=True,timeout=30)
    assert generated.returncode==0
    wav=listening_source('123',tmp_path/'inspection')
    with wave.open(str(wav)) as f:
        assert f.getframerate()==16000 and f.getnchannels()==1
        assert f.getnframes()/16000==pytest.approx(2,abs=.1)
    assert listening_source('123',tmp_path/'inspection')==wav
    with pytest.raises(ValueError,match='不存在'):listening_source('missing',tmp_path/'inspection')


def test_compile_requires_audio_verification_scope_and_valid_current_quotes(tmp_path):
    engine=create_engine('sqlite:///:memory:');Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(KnowledgeBase(id=1,name='长视频',name_key='长视频'))
        db.add(VideoCache(platform_item_id='1',title='video',status='done',transcript_text='真实的一句原话。',
            analysis_json=json.dumps({'transcript_segments':[{'start_seconds':1,'end_seconds':3,'text':'真实的一句原话。'}]})))
        db.flush();db.add(KnowledgeBaseVideo(knowledge_base_id=1,platform_item_id='1'));db.commit()
        case={'id':'L1','question':'说了什么？','expected_answer':'真实的一句原话。','scope':{'knowledge_base_id':1,'label':'长视频'},
              'answerable':True,'expected_route':'vector','category':'开头事实','status':'ready','source_audio_verified':False,
              'evidence':[{'video_id':'1','title':'video','start_seconds':1,'end_seconds':3,'quote':'真实的一句原话。'}]}
        with pytest.raises(ValueError,match='原声'):
            compile_exam(db,{'cases':[case]},tmp_path/'no.json')
        case['source_audio_verified']=True
        result=compile_exam(db,{'cases':[case]},tmp_path/'exam.json')
        assert result['status_counts']=={'validated':1}
        wrong=deepcopy(case);wrong['id']='L2';wrong['evidence'][0]['quote']='稿子里并没有的话。'
        with pytest.raises(ValueError,match='校验'):
            compile_exam(db,{'cases':[wrong]},tmp_path/'bad.json')
    engine.dispose()


@pytest.mark.parametrize('fail_indexing',[False,True])
def test_pipeline_records_real_stage_boundaries_and_keeps_long_test_audio(tmp_path,monkeypatch,fail_indexing):
    engine=create_engine('sqlite:///:memory:');Base.metadata.create_all(engine)
    monkeypatch.setattr(settings,'knowledge_analysis_mode','audio')
    monkeypatch.setattr(settings,'long_video_test_keep_audio',True)
    source=tmp_path/'long.wav';source.write_bytes(b'unit audio')
    result=VideoAnalysis('完整文字','',{'model':'test-model','duration_seconds':600,
        'transcript_segments':[{'start_seconds':0,'end_seconds':3,'text':'完整文字'}],
        'visual_segments':[],'warnings':[],'raw_results':[],'audio_processing':{'reused_root_segments':0}})
    monkeypatch.setattr(knowledge_module,'download_audio',lambda *a:source)
    monkeypatch.setattr(knowledge_module.omni_service,'analyze_audio',lambda *a:result)
    def embed(chunks):
        if fail_indexing:raise RuntimeError('offline indexing failure')
        return [[.1,.2] for _ in chunks]
    monkeypatch.setattr(knowledge_module.embedding_client,'embed_texts',embed)
    monkeypatch.setattr(knowledge_module,'get_chroma_service',lambda:SimpleNamespace(upsert_video_chunks=lambda **kw:None))
    with Session(engine) as db:
        cache=VideoCache(platform_item_id='long-test',title='long',status='pending');db.add(cache);db.commit()
        if fail_indexing:
            with pytest.raises(RuntimeError):knowledge_module.KnowledgeService()._process_videos(db,['long-test'])
        else:knowledge_module.KnowledgeService()._process_videos(db,['long-test'])
        runs=list_ingestion_runs('long-test');assert len(runs)==1
        assert runs[0]['status']==('failed' if fail_indexing else 'done')
        assert set(runs[0]['stages'])=={'download','recognition','indexing'}
        assert all(s['seconds'] is not None for s in runs[0]['stages'].values())
        assert source.exists()
        assert cache.transcript_text=='完整文字'
        if not fail_indexing:assert runs[0]['details']['audio_retained_for_test'] is True
    engine.dispose()
