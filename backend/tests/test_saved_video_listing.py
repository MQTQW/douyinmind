"""Saved-material navigation must include retained videos outside favorites."""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.entities import VideoCache


def test_saved_listing_includes_retained_material_and_paginates_without_transcripts():
    engine = create_engine('sqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            VideoCache(platform_item_id='retained', title='不在收藏夹中的已保存资料', status='done', transcript_text='PRIVATE_FULL_TRANSCRIPT', analysis_duration_seconds=731),
            VideoCache(platform_item_id='done-other', title='另一份资料', status='done'),
            VideoCache(platform_item_id='failed-one', title='失败的视频', status='failed'),
        ])
        db.commit()
        app.dependency_overrides[get_db] = lambda: db
        try:
            client = TestClient(app)
            first = client.get('/api/knowledge/videos?status=done&page=1&size=1').json()
            second = client.get('/api/knowledge/videos?status=done&page=2&size=1').json()
            assert first['total'] == second['total'] == 2
            items = first['items'] + second['items']
            assert {item['platform_item_id'] for item in items} == {'retained', 'done-other'}
            retained = next(item for item in items if item['platform_item_id'] == 'retained')
            assert retained['duration'] == 731
            assert 'PRIVATE_FULL_TRANSCRIPT' not in str(items)
            assert client.get('/api/knowledge/videos?status=all').json()['total'] == 3
            assert client.get('/api/knowledge/videos?page=0').status_code == 422
            assert client.get('/api/knowledge/videos?size=101').status_code == 422
            assert client.get('/api/knowledge/videos?status=unknown').status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)
    engine.dispose()
