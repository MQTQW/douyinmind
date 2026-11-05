"""Metadata refresh is never permission to erase saved local material."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.entities import (
    FavoriteCollection, FavoriteVideo, KnowledgeBase, KnowledgeBaseVideo,
    KnowledgeNoteGeneration, VideoCache,
)
from app.services import douyin_collector as collector_module
from app.services import favorites_service as favorites_module
from app.services.douyin_collector import (
    DouyinCollector, FavoriteScrapeSnapshot, FavoriteScrapedCollection, FavoriteScrapedVideo,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def existing(db, *, status="done", library=False):
    collection = FavoriteCollection(platform_collection_id="original", title="original", video_count=1)
    cache = VideoCache(platform_item_id="1001", title="saved title", status=status,
                       transcript_text="saved original words", visual_analysis_text="saved visual words")
    db.add_all([collection, cache]); db.flush()
    db.add(FavoriteVideo(collection_id=collection.id, platform_item_id="1001", title="saved title"))
    if library:
        base = KnowledgeBase(name="saved library", name_key="saved library")
        db.add(base); db.flush()
        db.add(KnowledgeBaseVideo(knowledge_base_id=base.id, platform_item_id="1001"))
    db.commit()
    return collection, cache


@pytest.mark.parametrize("status", ["done", "pending", "failed", "downloading", "transcribing", "embedding", "deleting"])
@pytest.mark.parametrize("library", [False, True])
def test_complete_unfavorite_retains_every_local_cache_status(db, status, library):
    collection, cache = existing(db, status=status, library=library)
    snapshot = FavoriteScrapeSnapshot(
        collections=[FavoriteScrapedCollection("original", "remote title", 0)], complete=True)
    service = favorites_module.FavoritesService()
    collections = service._sync_collections(db, snapshot)
    assert service._sync_videos_and_cache(db, snapshot, collections) == (0, 1)
    db.commit(); db.expire_all()
    saved = db.get(VideoCache, cache.id)
    assert saved is not None
    assert (saved.status, saved.transcript_text, saved.visual_analysis_text) == (
        status, "saved original words", "saved visual words")
    assert db.scalars(select(FavoriteVideo)).all() == []
    assert db.get(FavoriteCollection, collection.id).title == "remote title"
    assert len(db.scalars(select(KnowledgeBaseVideo)).all()) == int(library)


def test_unfavorite_preserves_saved_note_content_and_media(db, tmp_path):
    existing(db)
    note = KnowledgeNoteGeneration(note_id="synthetic-note", platform_item_id="1001",
        cache_key="a" * 64, source_fingerprint="b" * 64, configuration_hash="c" * 64,
        source_snapshot='{"text":"saved original words"}', markdown="# Saved note",
        markdown_sha256="d" * 64)
    db.add(note); db.flush()
    note.state, note.active_key = "done", None
    db.commit()
    media = tmp_path / "1001.mp4"
    media.write_bytes(b"saved synthetic media")
    snapshot = FavoriteScrapeSnapshot(collections=[FavoriteScrapedCollection("original", "remote", 0)], complete=True)
    service = favorites_module.FavoritesService()
    service._sync_videos_and_cache(db, snapshot, service._sync_collections(db, snapshot))
    db.commit(); db.expire_all()
    assert db.get(KnowledgeNoteGeneration, "synthetic-note").markdown == "# Saved note"
    assert db.get(KnowledgeNoteGeneration, "synthetic-note").source_snapshot == '{"text":"saved original words"}'
    assert media.read_bytes() == b"saved synthetic media"
    assert db.scalar(select(VideoCache.transcript_text)) == "saved original words"


@pytest.mark.parametrize("marker", [False, None, "true", 1, "missing"])
@pytest.mark.parametrize("entry", ["public", "collections", "videos"])
def test_unconfirmed_snapshot_cannot_mutate_metadata_or_material(db, monkeypatch, marker, entry):
    collection, cache = existing(db)
    attributes = {"collections": [], "videos": []}
    if marker != "missing":
        attributes["complete"] = marker
    snapshot = SimpleNamespace(**attributes)
    service = favorites_module.FavoritesService()
    async def fetch(**kwargs):
        return snapshot
    monkeypatch.setattr(favorites_module.collector, "fetch_snapshot", fetch)
    with pytest.raises(RuntimeError, match="完整确认"):
        if entry == "public":
            asyncio.run(service.sync_from_douyin(db))
        elif entry == "collections":
            service._sync_collections(db, snapshot)
        else:
            service._sync_videos_and_cache(db, snapshot, {"original": collection})
    assert not db.new and not db.dirty and not db.deleted
    db.commit(); db.expire_all()
    assert db.get(FavoriteCollection, collection.id).is_active
    assert len(db.scalars(select(FavoriteVideo)).all()) == 1
    assert db.get(VideoCache, cache.id).transcript_text == "saved original words"


def test_complete_refresh_adds_remote_metadata_without_removing_original_material(db, monkeypatch):
    _, original = existing(db)
    snapshot = FavoriteScrapeSnapshot(
        collections=[FavoriteScrapedCollection("original", "remote", 1)],
        videos=[FavoriteScrapedVideo("2002", "https://www.douyin.com/video/2002", "new video", "author", 17, {"original"})],
        complete=True)
    async def fetch(**kwargs):
        return snapshot
    monkeypatch.setattr(favorites_module.collector, "fetch_snapshot", fetch)
    result = asyncio.run(favorites_module.FavoritesService().sync_from_douyin(db))
    assert result == {"collections_total": 1, "videos_total": 1, "added_videos": 1, "removed_videos": 1}
    assert db.get(VideoCache, original.id).transcript_text == "saved original words"
    assert db.scalar(select(VideoCache.status).where(VideoCache.platform_item_id == "2002")) == "pending"
    assert db.scalars(select(FavoriteVideo.platform_item_id)).all() == ["2002"]


class FakePage:
    def __init__(self, result):
        self.result = result
    def goto(self, *args, **kwargs):
        pass
    def wait_for_function(self, *args, **kwargs):
        pass
    def evaluate(self, script):
        return self.result


@pytest.mark.parametrize("result", [
    {"ok": True, "collections": [], "itemsByCollection": {}},
    {"ok": True, "complete": False, "collections": [], "itemsByCollection": {}},
    {"ok": False, "complete": False, "error": "video_status"},
    {"ok": True, "complete": True, "collections": None, "itemsByCollection": {}},
    {"ok": True, "complete": True, "collections": [None], "itemsByCollection": {}},
    {"ok": True, "complete": True, "collections": [{"collectionFolderId": ""}], "itemsByCollection": {}},
    {"ok": True, "complete": True, "collections": [{"collectionFolderId": "original"}], "itemsByCollection": {}},
    {"ok": True, "complete": True, "collections": [{"collectionFolderId": "original"}], "itemsByCollection": {"original": [None]}},
    {"ok": True, "complete": True, "collections": [{"collectionFolderId": "original"}], "itemsByCollection": {"original": [{"awemeId": "bad-id"}]}},
])
def test_collector_rejects_unknown_or_invalid_snapshot_before_publishing(monkeypatch, result):
    monkeypatch.setattr(collector_module.time, "sleep", lambda _: None)
    collector = DouyinCollector()
    with pytest.raises(RuntimeError, match="完整|编号"):
        collector._fetch_in_context(FakePage(result))
    assert collector._snapshot is None


def test_collector_marks_only_validated_snapshot_complete(monkeypatch):
    monkeypatch.setattr(collector_module.time, "sleep", lambda _: None)
    result = {"ok": True, "complete": True,
        "collections": [{"collectionFolderId": "original", "collectionFolderName": "remote", "videoTotal": 1}],
        "itemsByCollection": {"original": [{"awemeId": "2002", "title": "new video", "durationMs": 17000}]}}
    snapshot = DouyinCollector()._fetch_in_context(FakePage(result))
    assert snapshot.complete is True
    assert snapshot.videos[0].platform_item_id == "2002"
    assert snapshot.videos[0].duration == 17
    assert snapshot.videos[0].collection_ids == {"original"}


@pytest.mark.parametrize("snapshot", [FavoriteScrapeSnapshot(), SimpleNamespace(collections=[], videos=[])])
def test_legacy_cached_snapshot_is_not_reused_as_complete(snapshot):
    collector = DouyinCollector()
    collector._snapshot = snapshot
    with pytest.raises(RuntimeError, match="完整确认"):
        asyncio.run(collector.fetch_snapshot())


def test_valid_complete_cached_snapshot_is_reused():
    collector = DouyinCollector()
    collector._snapshot = FavoriteScrapeSnapshot(complete=True)
    assert asyncio.run(collector.fetch_snapshot()) is collector._snapshot
