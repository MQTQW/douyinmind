"""A ready cache plus metadata alone is not searchable body evidence."""
import pytest
from sqlalchemy import select

from app.models.entities import VideoCache
from tests.test_mention_ingestion_service import context, VIDEO


@pytest.mark.parametrize('document', [
    {'text': '合成标题', 'title': '合成标题'},
    {'text': '作者：合成作者', 'title': '合成标题'},
    {'text': '作者: synthetic author', 'title': 'Synthetic title'},
])
def test_metadata_without_a_zero_identifier_still_cannot_prove_indexed_content(context, document):
    with context.sessions() as db:
        db.add(VideoCache(platform_item_id=VIDEO, title='preserved title', status='done', transcript_text='preserved transcript'))
        db.commit()
    context.documents[VIDEO] = [{'platform_item_id': VIDEO, 'chunk_id': 'legacy-metadata', **document}]
    result = context.service.submit(context.payload)
    assert result['indexed_content_chunks'] == 0
    assert result['state'] == 'unknown'
    assert context.worker.jobs == context.calls == []
    with context.sessions() as db:
        cache = db.scalar(select(VideoCache))
        assert cache.title == 'preserved title' and cache.transcript_text == 'preserved transcript' and cache.status == 'done'


def test_a_real_paragraph_after_an_author_line_still_proves_content_without_asr(context):
    with context.sessions() as db:
        db.add(VideoCache(platform_item_id=VIDEO, title='preserved title', status='done', transcript_text='preserved transcript'))
        db.commit()
    context.documents[VIDEO] = [{'platform_item_id': VIDEO, 'chunk_id': 'legacy-body', 'title': '合成标题',
                                'text': '作者：合成作者\n这是已保存的合成正文，说明资料归类的方法。'}]
    result = context.service.submit(context.payload)
    assert result['state'] == 'succeeded' and result['indexed_content_chunks'] == 1
    assert context.worker.jobs == context.calls == []
