"""Shared evidence focus, independent of query authorization or execution."""
import re

from app.mention_agent.followup_evidence import validate_followup_envelope


_CURRENT_VIDEO=r'(?:这个|当前|这条|本条|本|该)\s*视频'
_OTHER_SCOPE=(r'(?:不要|不用|不必|别|不是|并非).{0,8}'+_CURRENT_VIDEO
              +r'|对比|比较|另一个视频|其他视频|其它视频')


def default_current_video(envelope):
    """A verified ordinary followup may omit its subject; AT rules stay intact.

    The return value is only a default filter. Callers retain explicit scope
    choices and must separately authorize the actual query and video access.
    """
    if type(envelope) is not dict:return None
    body=envelope.get('comment_text');video=envelope.get('video_id')
    if type(body) is not str or type(video) is not str or not re.fullmatch(r'[0-9]{5,30}',video):return None
    proof=envelope.get('evidence')
    followup=type(proof) is dict and proof.get('source_kind')=='conversation_followup'
    if followup:
        try:validate_followup_envelope(envelope)
        except (ValueError,TypeError,KeyError):return None
    if re.search(_OTHER_SCOPE,body):return None
    return video if followup or re.search(_CURRENT_VIDEO,body) else None
