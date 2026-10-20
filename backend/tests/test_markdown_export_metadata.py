import json

from app.models.entities import VideoCache
from app.services import markdown_export as module


def sample():
    return VideoCache(platform_item_id='sample-video', title='验收样例', transcript_text='先明确问题，再用自己的话复述。',
                      visual_analysis_text='', analysis_model='sample-model', analysis_json=json.dumps({
                          'transcript_segments':[{'start_seconds':1,'end_seconds':3,'text':'先明确问题，再用自己的话复述。'}],
                          'warnings':['一处声音需要复查'],
                      },ensure_ascii=False))


def test_ai_export_identifies_video_id_without_inventing_an_author_and_keeps_original(monkeypatch):
    calls=[]
    def answer(**kwargs):
        calls.append(kwargs)
        return '### 内容摘要\n示例整理结果'
    monkeypatch.setattr(module.llm_client,'chat',answer)
    video=sample()
    result=module.export_ai_organized(video)
    assert '> 视频编号：sample-video' in result
    assert '> 作者：sample-video' not in result
    assert video.transcript_text in result
    assert '[00:01–00:03]' in result
    assert '一处声音需要复查' in result
    assert len(calls)==1


def test_original_export_preserves_content_and_warnings_without_model_calls(monkeypatch):
    def forbidden(**kwargs):raise AssertionError('原文导出不能调用模型')
    monkeypatch.setattr(module.llm_client,'chat',forbidden)
    video=sample()
    result=module.export_original(video)
    assert video.transcript_text in result
    assert '> 视频编号：sample-video' in result
    assert '一处声音需要复查' in result
