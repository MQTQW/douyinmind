"""Library classification and preservation run only against isolated SQLite."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.knowledge_bases import router
from app.db.base import Base
from app.db.migrations import ensure_chat_knowledge_base_column
from app.db.session import get_db
from app.models.entities import FavoriteCollection, FavoriteVideo, KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.services.douyin_collector import FavoriteScrapeSnapshot, FavoriteScrapedCollection
from app.services.favorites_service import FavoritesService
from app.services.knowledge_base_service import knowledge_base_service as service


@pytest.fixture
def context():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([
            VideoCache(platform_item_id="ready", title="完整文字稿", status="done", transcript_text="保留全部原话"),
            VideoCache(platform_item_id="pending", title="尚未分析", status="pending"),
            VideoCache(platform_item_id="failed", title="需要重试", status="failed"),
        ])
        db.commit()
        app = FastAPI()
        app.include_router(router, prefix="/api")
        def test_db():
            yield db
        app.dependency_overrides[get_db] = test_db
        with TestClient(app) as client:
            yield SimpleNamespace(db=db, client=client, engine=engine)
    engine.dispose()


def test_create_and_rename_validate_names_with_friendly_errors(context):
    client = context.client
    response = client.post("/api/knowledge-bases", json={"name": "  技术资料  "})
    assert response.status_code == 201
    item = response.json()["item"]
    assert item["name"] == "技术资料"
    assert item["video_count"] == item["ready_count"] == 0
    assert client.post("/api/knowledge-bases", json={"name": "技术资料"}).status_code == 409
    assert client.post("/api/knowledge-bases", json={"name": " \n "}).status_code == 400
    assert client.post("/api/knowledge-bases", json={"name": "字" * 129}).status_code == 400
    english = client.post("/api/knowledge-bases", json={"name": "React"}).json()["item"]
    assert client.post("/api/knowledge-bases", json={"name": "react"}).status_code == 409
    assert client.patch(f"/api/knowledge-bases/{item['id']}", json={"name": "react"}).status_code == 409
    renamed = client.patch(f"/api/knowledge-bases/{item['id']}", json={"name": "开发教程"}).json()
    assert renamed["item"]["name"] == "开发教程"
    assert client.patch("/api/knowledge-bases/9999", json={"name": "缺失"}).status_code == 404
    assert len(client.get("/api/knowledge-bases").json()["items"]) == 2
    assert english["id"] != item["id"]


def test_membership_is_many_to_many_idempotent_and_keeps_full_result(context):
    db = context.db
    first = service.create(db, "学习资料")["id"]
    second = service.create(db, "开发资料")["id"]
    db.commit()
    result = service.assign_videos(db, first, ["ready", "pending", "ready"])
    assert result == {"added_count": 2, "existing_count": 0, "selected_ids": ["ready", "pending"]}
    result = service.assign_videos(db, first, ["ready", "failed"])
    assert result["added_count"] == result["existing_count"] == 1
    assert service.assign_videos(db, second, ["ready"])["added_count"] == 1
    db.commit()
    stats = {item["id"]: item for item in service.list(db)}
    assert (stats[first]["video_count"], stats[first]["ready_count"]) == (3, 1)
    assert (stats[second]["video_count"], stats[second]["ready_count"]) == (1, 1)
    assert service.list_video_ids(db, first, done_only=True) == ["ready"]
    ready = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == "ready"))
    assert ready.status == "done"
    assert ready.transcript_text == "保留全部原话"
    assert len(ready.knowledge_base_links) == 2


@pytest.mark.parametrize("selected", [[], ["ready", "unknown"], [" "], ["ready", ""]])
def test_invalid_selection_is_atomic(context, selected):
    db = context.db
    base_id = service.create(db, "空库")["id"]
    db.commit()
    with pytest.raises(ValueError):
        service.assign_videos(db, base_id, selected)
    assert service.list_video_ids(db, base_id) == []


def test_assignment_leaves_commit_to_caller(context):
    db = context.db
    base_id = service.create(db, "可撤回")["id"]
    db.commit()
    service.assign_videos(db, base_id, ["ready"])
    db.rollback()
    assert service.list_video_ids(db, base_id) == []


def test_video_listing_paginates_and_remove_only_affects_chosen_library(context):
    db = context.db
    first = service.create(db, "甲")["id"]
    second = service.create(db, "乙")["id"]
    service.assign_videos(db, first, ["ready", "pending", "failed"])
    service.assign_videos(db, second, ["ready"])
    db.commit()
    client = context.client
    result = client.get(f"/api/knowledge-bases/{first}/videos?page=1&size=2").json()
    assert result["total"] == 3 and len(result["items"]) == 2
    tail = client.get(f"/api/knowledge-bases/{first}/videos?page=2&size=2").json()
    assert len(tail["items"]) == 1
    assert {item["platform_item_id"] for item in result["items"] + tail["items"]} == {"ready", "pending", "failed"}
    assert tail["items"][0]["collection_id"] == f"knowledge-base:{first}"
    assert client.get("/api/knowledge-bases/9999/videos").status_code == 404
    assert client.delete(f"/api/knowledge-bases/{first}/videos/ready").json()["removed_count"] == 1
    assert client.delete(f"/api/knowledge-bases/{first}/videos/ready").json()["removed_count"] == 0
    assert service.list_video_ids(db, second) == ["ready"]
    assert db.scalar(select(VideoCache.transcript_text).where(VideoCache.platform_item_id == "ready")) == "保留全部原话"


def test_orm_video_delete_cleans_membership(context):
    db = context.db
    base_id = service.create(db, "甲")["id"]
    service.assign_videos(db, base_id, ["ready"])
    db.commit()
    db.delete(db.scalar(select(VideoCache).where(VideoCache.platform_item_id == "ready")))
    db.commit()
    assert db.scalars(select(KnowledgeBaseVideo)).all() == []
    assert db.get(KnowledgeBase, base_id) is not None


def test_unfavorite_sync_preserves_named_library_transcript(context):
    db = context.db
    collection = FavoriteCollection(platform_collection_id="original", title="收藏", video_count=2)
    db.add(collection)
    db.flush()
    db.add_all([FavoriteVideo(collection_id=collection.id, platform_item_id=item_id, title=item_id)
                for item_id in ["ready", "pending"]])
    base_id = service.create(db, "长期保存")["id"]
    service.assign_videos(db, base_id, ["ready"])
    db.commit()
    favorites = FavoritesService()
    snapshot = FavoriteScrapeSnapshot(collections=[FavoriteScrapedCollection("original", "收藏", 0)])
    collections = favorites._sync_collections(db, snapshot)
    favorites._sync_videos_and_cache(db, snapshot, collections)
    db.commit()
    assert db.scalars(select(FavoriteVideo)).all() == []
    assert db.scalar(select(VideoCache).where(VideoCache.platform_item_id == "pending")) is None
    saved = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == "ready"))
    assert saved.status == "done" and saved.transcript_text == "保留全部原话"
    videos, total = service.list_videos(db, base_id)
    assert total == 1 and videos[0]["title"] == "完整文字稿"


def test_existing_chat_migration_is_idempotent_and_preserves_history():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY, title VARCHAR(128))"))
        connection.execute(text("INSERT INTO chat_sessions (id, title) VALUES (1, '原有对话')"))
        connection.execute(text("CREATE TABLE knowledge_bases (id INTEGER PRIMARY KEY)"))
    ensure_chat_knowledge_base_column(engine)
    ensure_chat_knowledge_base_column(engine)
    columns = [column["name"] for column in inspect(engine).get_columns("chat_sessions")]
    assert columns.count("knowledge_base_id") == 1
    with engine.connect() as connection:
        assert connection.execute(text("SELECT title, knowledge_base_id FROM chat_sessions")).one() == ("原有对话", None)
    engine.dispose()


def _add_collection(db, collection_id="folder", title="  开发资料  ", active=True, with_video=True):
    collection = FavoriteCollection(platform_collection_id=collection_id, title=title,
                                    is_active=active, video_count=99)
    db.add(collection)
    db.flush()
    if with_video:
        db.add(FavoriteVideo(collection_id=collection.id, platform_item_id=f"folder-{collection_id}",
                             title="文件夹视频"))
    db.commit()
    return collection


def test_collection_default_name_is_stored_title_and_repeated_creation_reuses(context):
    folder = _add_collection(context.db)
    response = context.client.post("/api/knowledge-bases/from-collection", json={"collection_id": "folder"})
    assert response.status_code == 200
    result = response.json()
    assert result["item"]["name"] == "开发资料"
    assert result["reused"] is False
    assert result["item"]["video_count"] == 0  # Resolving a name does not import anything.
    repeat = context.client.post("/api/knowledge-bases/from-collection", json={"collection_id": "folder"}).json()
    assert repeat["reused"] is True and repeat["item"]["id"] == result["item"]["id"]
    assert context.db.get(FavoriteCollection, folder.id).title == "  开发资料  "
    assert context.db.scalars(select(KnowledgeBaseVideo)).all() == []


def test_collection_name_reuses_casefold_match_without_renaming_existing(context):
    _add_collection(context.db, title="  REACT  ")
    existing = service.create(context.db, "React")
    service.assign_videos(context.db, existing["id"], ["ready"])
    context.db.commit()
    result = context.client.post("/api/knowledge-bases/from-collection", json={"collection_id": "folder"}).json()
    assert result["reused"] is True
    assert result["item"]["id"] == existing["id"]
    assert result["item"]["name"] == "React"
    assert result["item"]["video_count"] == result["item"]["ready_count"] == 1


@pytest.mark.parametrize("kind, expected", [("missing", 404), ("inactive", 404), ("empty", 400),
                                           ("empty-all", 400), ("blank-title", 400)])
def test_collection_invalid_or_empty_never_creates_library(context, kind, expected):
    collection_id = "all" if kind == "empty-all" else "folder"
    if kind not in {"missing", "empty-all"}:
        _add_collection(context.db, active=kind != "inactive", with_video=kind != "empty",
                        title="  " if kind == "blank-title" else "开发资料")
    response = context.client.post("/api/knowledge-bases/from-collection", json={"collection_id": collection_id})
    assert response.status_code == expected
    assert context.db.scalars(select(KnowledgeBase)).all() == []


def test_virtual_all_collection_gets_all_favorites_name(context):
    _add_collection(context.db)
    result = context.client.post("/api/knowledge-bases/from-collection", json={"collection_id": "all"}).json()
    assert result["item"]["name"] == "全部收藏" and result["reused"] is False
    assert result["item"]["video_count"] == 0


def test_long_collection_title_has_stable_default_without_changing_folder(context):
    title = "知识" * 100
    folder = _add_collection(context.db, title=title)
    first, reused = service.from_collection(context.db, "folder")
    context.db.commit()
    second, reused_second = service.from_collection(context.db, "folder")
    assert first["name"] == title[:128] and len(first["name"]) == 128
    assert reused is False and reused_second is True and first["id"] == second["id"]
    assert context.db.get(FavoriteCollection, folder.id).title == title


def test_collection_library_creation_leaves_commit_to_caller(context):
    _add_collection(context.db)
    service.from_collection(context.db, "folder")
    context.db.rollback()
    assert context.db.scalars(select(KnowledgeBase)).all() == []


def test_collection_concurrent_name_creation_reuses_winner(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent-name.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _add_collection(db, title="开发资料")
        original_flush = db.flush
        competed = False

        def competing_flush(*args, **kwargs):
            nonlocal competed
            if not competed and any(isinstance(item, KnowledgeBase) for item in db.new):
                competed = True
                with Session(engine) as competing_db:
                    service.create(competing_db, "开发资料")
                    competing_db.commit()
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(db, "flush", competing_flush)
        item, reused = service.from_collection(db, "folder")
        assert competed and reused is True and item["name"] == "开发资料"
        # The uniqueness failure was contained; this same transaction is usable.
        assert len(db.scalars(select(KnowledgeBase)).all()) == 1
        db.commit()
    engine.dispose()
