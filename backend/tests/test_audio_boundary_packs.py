from copy import deepcopy
import json
from threading import Barrier, Lock
import wave

import pytest

from app.core.config import settings
from app.services.audio_boundary import propose_boundary_splice
from app.services.audio_boundary_batch import plan_boundary_batches, prepare_audio_pack, parse_batch_response
from app.services.audio_boundary_batch import has_acoustic_uncertainty
from app.services.audio_boundary_packs import review_boundary_packs
from app.services.omni_service import OmniService, OmniServiceError

LEFT = '这里有足够长且唯一的前文用于完整核对人口从一亿增长到'
RIGHT = '点三亿当时正值人口爆炸时期更多原文继续作为右侧可靠定位'


def part(text, start, end):
    return {'text': text, 'start_seconds': start, 'end_seconds': end}


def test_numeric_duplicate_needs_two_agreeing_windows_and_keeps_value():
    left, right = [part(LEFT+'四。', 0, 10)], [part('四'+RIGHT, 10, 20)]
    heard = [part(LEFT+'四'+RIGHT, 5, 15)]
    assert not propose_boundary_splice(left, right, heard)['accepted']
    fixed = propose_boundary_splice(left, right, heard, confirmation_parts=[part(LEFT+'四'+RIGHT, 6, 14)])
    assert fixed['accepted'] and fixed['reason'] == 'two_window_confirmed_boundary_duplicate'
    assert '四点三亿' in fixed['after_parts'][0]['text']
    assert left[0]['text'].endswith('四。')
    assert not propose_boundary_splice(left, right, heard,
        confirmation_parts=[part(LEFT+'四四'+RIGHT, 6, 14)])['accepted']
    assert not propose_boundary_splice(left, right, heard,
        confirmation_parts=[part(LEFT+'四点四亿'+RIGHT[4:], 6, 14)])['accepted']


def test_confirmation_does_not_allow_a_different_number_or_unit():
    left,right=[part(LEFT+'四。',0,10)],[part('四'+RIGHT,10,20)]
    for changed in (LEFT+'五'+RIGHT,LEFT+'四点四亿'+RIGHT,LEFT+'四'+RIGHT.replace('亿','万')):
        heard=[part(changed,5,15)]
        assert not propose_boundary_splice(left,right,heard,confirmation_parts=heard)['accepted']


def test_duplicate_han_before_unchanged_ascii_can_be_confirmed():
    a='已有前文必须完整保留而此处谈到的维生'
    b=' E 等营养素在原文中继续说明相关内容与条件'
    left,right=[part(a+'素。',0,10)],[part('素'+b,10,20)]
    heard=[part(a+'素'+b,5,15)]
    assert not propose_boundary_splice(left,right,heard)['accepted']
    assert propose_boundary_splice(left,right,heard,confirmation_parts=heard)['accepted']


def test_default_analyze_entry_reaches_batch_review_with_no_live_model(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from app.services import omni_service as module
    from app.services.audio_media import AudioSegment
    from app.services.omni_service import AudioAnalysis
    assert type(settings).model_fields['audio_boundary_batch_enabled'].default is True
    source=tmp_path/'input.wav';make_source(source,seconds=120)
    monkeypatch.setattr(settings,'audio_cache_dir',str(tmp_path/'cache'))
    monkeypatch.setattr(settings,'qwen_omni_api_key','offline-fake')
    monkeypatch.setattr(settings,'audio_boundary_review_enabled',True)
    monkeypatch.setattr(settings,'audio_boundary_batch_enabled',True)
    monkeypatch.setattr(settings,'video_segment_seconds',60)
    @contextmanager
    def prepare(path):yield 120,iter([AudioSegment(source,0,60),AudioSegment(source,60,60)])
    monkeypatch.setattr(module,'prepare_audio_segments',prepare)
    service=OmniService();calls=[]
    monkeypatch.setattr(service,'_analyze_audio_segment',lambda segment:(AudioAnalysis(
        transcript_segments=[part('独立原文内容保留',0,60)]),'saved root'))
    def pack(segment,layout):
        calls.append(len(layout));return json.dumps({'clips':[{'clip_id':m['clip_id'],'text':'不同原话无法匹配','warnings':[]} for m in layout]})
    monkeypatch.setattr(service,'_recognize_audio_pack',pack)
    result=service.analyze_audio(source)
    assert calls==[1] and result.payload['boundary_review']['primary_windows']==1
    assert result.payload['audio_processing']['total_recognition_attempts']==3


@pytest.mark.parametrize('letter', ['不', '年', 'A', '３'])
def test_confirmation_cannot_bypass_negation_units_or_ascii_number_guards(letter):
    a='原声中的前文足够长而且每个字都应当保留'
    b='后文也必须逐字不变不能产生别的事实和结论'
    left,right=[part(a+letter+'。',0,10)],[part(letter+b,10,20)]
    heard=[part(a+letter+b,5,15)]
    assert not propose_boundary_splice(left,right,heard,confirmation_parts=heard)['accepted']


def make_source(path, seconds=780, rate=100):
    pcm=b''.join((i%32000).to_bytes(2,'little',signed=True) for i in range(seconds*rate))
    with wave.open(str(path),'wb') as out:
        out.setparams((1,2,rate,0,'NONE','not compressed'));out.writeframes(pcm)
    return pcm,rate


def make_roots():
    return [{'start_seconds':i*60,'parts':[part('这是一段必须全部保留的原始讲话内容接着谈到山',i*60,(i+1)*60)]}
            if i==0 else {'start_seconds':i*60,'parts':[part('山坡与其他原声内容完整地保留其中还有不同的话题',i*60,(i+1)*60)]}
            for i in range(13)]


def test_plan_covers_every_boundary_and_confirmation_has_different_source_span():
    roots=make_roots(); plan=plan_boundary_batches(roots)
    assert [w['boundary_seconds'] for b in plan if b['stage']=='primary' for w in b['windows']]==list(range(60,721,60))
    assert len(plan)==3 and plan[-1]['stage']=='confirmation' and plan[-1]['radius']==4
    bigger=deepcopy(roots)
    for i in range(13,31):bigger.append({'start_seconds':i*60,'parts':[part('保留新的讲话内容',i*60,(i+1)*60)]})
    p=plan_boundary_batches(bigger)
    cuts=[w['boundary_seconds'] for b in p if b['stage']=='primary' for w in b['windows']]
    assert len(cuts)<=12 and 60 in cuts and 1800 in cuts and len(cuts)==len(set(cuts))


def test_pack_pcm_ranges_and_silence_are_exact(tmp_path,monkeypatch):
    source=tmp_path/'audio.wav';pcm,rate=make_source(source)
    monkeypatch.setattr(settings,'audio_cache_dir',str(tmp_path/'cache'))
    windows=plan_boundary_batches(make_roots())[0]['windows']
    with prepare_audio_pack(source,windows,5,780) as (segment,layout):
        with wave.open(str(segment.path),'rb') as inp: packed=inp.readframes(inp.getnframes())
        assert segment.duration==65
        for meta in layout:
            start,end=round(meta['packed_start_seconds']*rate)*2,round(meta['packed_end_seconds']*rate)*2
            assert packed[start:end]==pcm[meta['source_frame_start']*2:meta['source_frame_end']*2]
        assert packed[10*rate*2:11*rate*2]==b'\0'*(rate*2)


@pytest.mark.parametrize('clips', [[], [{'clip_id':True,'text':'话','warnings':[]}],
    [{'clip_id':0,'text':'话','warnings':[]},{'clip_id':0,'text':'重复ID','warnings':[]}],
    [{'clip_id':1,'text':'错ID','warnings':[]}], [{'clip_id':0,'text':3,'warnings':[]}]])
def test_invalid_batch_never_becomes_a_valid_window(clips):
    with pytest.raises(OmniServiceError):
        parse_batch_response(json.dumps({'clips':clips}),[{'clip_id':0,'duration_seconds':10}],OmniService())


def test_batch_uses_real_window_envelope_not_invented_utterance_times():
    result=parse_batch_response(json.dumps({'clips':[{'clip_id':0,'text':'整段原话','warnings':[]}]}),
        [{'clip_id':0,'duration_seconds':10}],OmniService())
    assert result[0]['transcript_segments']==[part('整段原话',0,10)]


@pytest.mark.parametrize('warning', ['听不清', '片段开始处听不清', '片段中间可能有文字缺失',
    '片段开始处可能有文字缺失，数字无法确认', '音轨有噪音', '未知警告'])
def test_uncertainty_is_not_confused_with_a_known_cut_window_edge(warning):
    assert has_acoustic_uncertainty([warning],[part('这里有原话',0,10)])


def test_known_cropped_edges_do_not_override_unclear_speech_or_other_warning():
    edge='片段开始处可能有文字缺失'
    assert not has_acoustic_uncertainty([edge,'片段结尾处可能有文字缺失'],[part('可辨认的中心原话',0,10)])
    assert has_acoustic_uncertainty([edge],[part('[听不清]',0,10)])
    assert has_acoustic_uncertainty([edge,'中心数字不确定'],[part('原话',0,10)])


def test_duplicate_json_keys_are_rejected_before_any_clip_is_used():
    raw='{"clips":[{"clip_id":9,"clip_id":0,"text":"原话","warnings":[]}]}'
    with pytest.raises(OmniServiceError):
        parse_batch_response(raw,[{'clip_id':0,'duration_seconds':10}],OmniService())


def test_packs_execute_concurrently_and_cache_is_content_bound(tmp_path,monkeypatch):
    source=tmp_path/'audio.wav';make_source(source)
    monkeypatch.setattr(settings,'audio_cache_dir',str(tmp_path/'cache'))
    monkeypatch.setattr(settings,'audio_transcription_concurrency',3)
    roots=make_roots();barrier=Barrier(3);lock=Lock();calls=[]
    service=OmniService()
    def fake(segment,layout):
        with lock:calls.append(segment.duration)
        barrier.wait(timeout=5)
        return json.dumps({'clips':[{'clip_id':m['clip_id'],'text':'未能可靠对应原稿的独立听写','warnings':[]}
                                   for m in layout]})
    monkeypatch.setattr(service,'_recognize_audio_pack',fake)
    original=deepcopy(roots)
    speech,audit=review_boundary_packs(service,source,780,roots)
    assert len(calls)==3 and audit['recognition_attempts']==3 and audit['primary_windows']==12
    assert speech==[p for r in roots for p in r['parts']] and roots==original
    _,cached=review_boundary_packs(service,source,780,roots)
    assert len(calls)==3 and cached['recognition_attempts']==0 and cached['reused_packs']==3
    roots[0]['parts'][0]['text']='原稿开头确实改变'+roots[0]['parts'][0]['text']
    review_boundary_packs(service,source,780,roots)
    assert len(calls)==6


@pytest.mark.parametrize('warnings,accepted', [(['听不清'],False), (['片段开始处可能有文字缺失'],True)])
def test_real_controller_preserves_uncertainty_and_requires_both_numeric_hearings(tmp_path,monkeypatch,warnings,accepted):
    source=tmp_path/'input.wav';make_source(source,seconds=40)
    monkeypatch.setattr(settings,'audio_cache_dir',str(tmp_path/'cache'))
    roots=[{'start_seconds':0,'parts':[part(LEFT+'四。',0,20)]},
           {'start_seconds':20,'parts':[part('四'+RIGHT,20,40)]}]
    service=OmniService()
    monkeypatch.setattr(service,'_recognize_audio_pack',lambda seg,layout:json.dumps({'clips':[
        {'clip_id':m['clip_id'],'text':LEFT+'四'+RIGHT,'warnings':warnings} for m in layout]}))
    _,audit=review_boundary_packs(service,source,40,roots)
    assert audit['accepted_windows']==int(accepted)
    assert audit['recognition_attempts']==2
    assert not hasattr(service._request_context,'boundary_deadline')


def test_failed_confirmation_cannot_unlock_numeric_deletion(tmp_path,monkeypatch):
    source=tmp_path/'input.wav';make_source(source,seconds=40)
    monkeypatch.setattr(settings,'audio_cache_dir',str(tmp_path/'cache'))
    roots=[{'start_seconds':0,'parts':[part(LEFT+'四。',0,20)]},
           {'start_seconds':20,'parts':[part('四'+RIGHT,20,40)]}]
    service=OmniService()
    def recognize(segment,layout):
        if segment.duration==8:raise OmniServiceError('模拟超时',code='boundary_review_timeout',raw_response='partial')
        return json.dumps({'clips':[{'clip_id':m['clip_id'],'text':LEFT+'四'+RIGHT,'warnings':[]} for m in layout]})
    monkeypatch.setattr(service,'_recognize_audio_pack',recognize)
    speech,audit=review_boundary_packs(service,source,40,roots)
    assert speech==[p for r in roots for p in r['parts']]
    assert audit['accepted_windows']==0 and audit['failed_recognition_attempts']==1
    assert any(p.get('raw_response')=='partial' for p in audit['pack_results'])


def test_stage_deadline_after_cache_read_never_starts_model(tmp_path,monkeypatch):
    from app.services import audio_boundary_packs as module
    source=tmp_path/'input.wav';make_source(source)
    monkeypatch.setattr(settings,'audio_cache_dir',str(tmp_path/'cache'))
    clock=[0.0];monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    def cache_read(*args):clock[0]=60;return None
    monkeypatch.setattr(module.PackCache,'get',cache_read)
    service=OmniService()
    monkeypatch.setattr(service,'_recognize_audio_pack',lambda *args:pytest.fail('No request admitted after deadline'))
    speech,audit=review_boundary_packs(service,source,780,make_roots())
    assert audit['recognition_attempts']==0 and audit['accepted_windows']==0


def test_cache_write_failure_keeps_paid_raw_and_original(tmp_path,monkeypatch):
    from app.services import audio_boundary_packs as module
    source=tmp_path/'input.wav';make_source(source)
    monkeypatch.setattr(settings,'audio_cache_dir',str(tmp_path/'cache'))
    def fail(*args):raise OSError('unit-only write failure')
    monkeypatch.setattr(module.PackCache,'put',fail)
    service=OmniService()
    monkeypatch.setattr(service,'_recognize_audio_pack',lambda seg,layout:json.dumps({'clips':[
        {'clip_id':m['clip_id'],'text':'已付费返回的完整原话必须保留','warnings':[]} for m in layout]}))
    roots=make_roots();speech,audit=review_boundary_packs(service,source,780,roots)
    assert speech==[p for r in roots for p in r['parts']]
    assert audit['accepted_windows']==0 and audit['recognition_attempts']==3
    assert all('完整原话' in json.loads(p['raw_response'])['clips'][0]['text'] for p in audit['pack_results'])
