"""Named knowledge bases classify shared video results without repeating recognition."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, delete, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.entities import FavoriteCollection, FavoriteVideo, KnowledgeBase, KnowledgeBaseVideo, VideoCache


class KnowledgeBaseNotFound(ValueError):
    pass


class DuplicateKnowledgeBaseName(ValueError):
    pass


class FavoriteCollectionNotFound(ValueError):
    pass


class KnowledgeBaseService:
    def require_base(self, db: Session, knowledge_base_id: int) -> KnowledgeBase:
        if type(knowledge_base_id) is not int or knowledge_base_id <= 0:
            raise KnowledgeBaseNotFound("这个知识库不存在，请刷新后重新选择。")
        item = db.scalar(select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id, KnowledgeBase.deleted_at.is_(None),
        ).execution_options(populate_existing=True))
        if item is None:
            raise KnowledgeBaseNotFound("这个知识库不存在，请刷新后重新选择。")
        return item

    @staticmethod
    def _normalize_name(name: str) -> tuple[str, str]:
        name = name.strip()
        if not name:
            raise ValueError("请输入知识库名称。")
        if len(name) > 128:
            raise ValueError("知识库名称最多 128 个字。")
        if "\x00" in name:
            raise ValueError("知识库名称包含无效字符，请重新填写。")
        return name, name.casefold()

    def create(self, db: Session, name: str) -> dict:
        name, name_key = self._normalize_name(name)
        if db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name_key == name_key, KnowledgeBase.deleted_at.is_(None))):
            raise DuplicateKnowledgeBaseName("已存在同名知识库，请换个名称。")
        item = KnowledgeBase(name=name, name_key=name_key)
        db.add(item)
        db.flush()
        return self._to_dict(item, 0, 0)

    def from_collection(self, db: Session, collection_id: str) -> tuple[dict, bool]:
        """Resolve the stored folder name; creation does not assign or analyze videos."""
        if collection_id == "all":
            name = "全部收藏"
            video_query = select(FavoriteVideo.id).where(FavoriteVideo.is_active.is_(True))
        else:
            collection = db.scalar(select(FavoriteCollection).where(
                FavoriteCollection.platform_collection_id == collection_id,
                FavoriteCollection.is_active.is_(True),
            ))
            if collection is None:
                raise FavoriteCollectionNotFound("这个收藏夹不存在，请刷新后重新选择。")
            name = collection.title
            video_query = select(FavoriteVideo.id).where(
                FavoriteVideo.collection_id == collection.id,
                FavoriteVideo.is_active.is_(True),
            )
        if db.scalar(video_query.limit(1)) is None:
            raise ValueError("这个收藏夹没有可导入的视频，请先同步收藏夹。")
        # Folder titles may exceed the library name limit. The same stored title
        # always yields the same default name, without altering the source folder.
        name, name_key = self._normalize_name(name.strip()[:128])
        existing_id = db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name_key == name_key, KnowledgeBase.deleted_at.is_(None)))
        if existing_id is not None:
            return self.get(db, existing_id), True
        connection = db.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            # SQLite's legacy driver does not start a transaction for SELECT.
            # Begin the outer one so releasing our savepoint cannot commit a
            # new library before the caller explicitly commits.
            connection.exec_driver_sql("BEGIN")
        try:
            # Keep the caller's transaction usable if another request wins the
            # unique-name race between the read above and our insert.
            with db.begin_nested():
                item = KnowledgeBase(name=name, name_key=name_key)
                db.add(item)
                db.flush()
            return self._to_dict(item, 0, 0), False
        except IntegrityError:
            existing_id = db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name_key == name_key, KnowledgeBase.deleted_at.is_(None)))
            if existing_id is None:
                raise
            return self.get(db, existing_id), True

    def rename(self, db: Session, knowledge_base_id: int, name: str) -> dict:
        item = self.require_base(db, knowledge_base_id)
        name, name_key = self._normalize_name(name)
        duplicate = db.scalar(select(KnowledgeBase.id).where(
            KnowledgeBase.name_key == name_key, KnowledgeBase.id != item.id,
            KnowledgeBase.deleted_at.is_(None),
        ))
        if duplicate:
            raise DuplicateKnowledgeBaseName("已存在同名知识库，请换个名称。")
        item.name = name
        item.name_key = name_key
        db.flush()
        return self.get(db, item.id)

    def list(self, db: Session) -> list[dict]:
        rows = db.execute(
            select(KnowledgeBase, func.count(KnowledgeBaseVideo.platform_item_id),
                   func.sum(case((VideoCache.status == "done", 1), else_=0)))
            .outerjoin(KnowledgeBaseVideo, KnowledgeBaseVideo.knowledge_base_id == KnowledgeBase.id)
            .outerjoin(VideoCache, VideoCache.platform_item_id == KnowledgeBaseVideo.platform_item_id)
            .where(KnowledgeBase.deleted_at.is_(None))
            .group_by(KnowledgeBase.id)
            .order_by(desc(KnowledgeBase.updated_at), desc(KnowledgeBase.id))
        ).all()
        return [self._to_dict(item, int(total), int(ready or 0)) for item, total, ready in rows]

    def get(self, db: Session, knowledge_base_id: int) -> dict:
        self.require_base(db, knowledge_base_id)
        return next(item for item in self.list(db) if item["id"] == knowledge_base_id)

    def assign_videos(self, db: Session, knowledge_base_id: int, selected_ids: list[str]) -> dict:
        """Validate the complete selection before writing; caller owns the transaction."""
        item = self.require_base(db, knowledge_base_id)
        if not selected_ids:
            raise ValueError("请至少选择一个视频。")
        if any(not isinstance(item_id, str) or not item_id.strip() for item_id in selected_ids):
            raise ValueError("所选视频编号无效，请重新选择。")
        selected_ids = list(dict.fromkeys(selected_ids))
        found = set(db.scalars(select(VideoCache.platform_item_id).where(
            VideoCache.platform_item_id.in_(selected_ids)
        )).all())
        if any(item_id not in found for item_id in selected_ids):
            raise ValueError("部分所选视频已不存在，请刷新后重新选择。")
        existing = set(db.scalars(select(KnowledgeBaseVideo.platform_item_id).where(
            KnowledgeBaseVideo.knowledge_base_id == knowledge_base_id,
            KnowledgeBaseVideo.platform_item_id.in_(selected_ids),
        )).all())
        new_ids = [item_id for item_id in selected_ids if item_id not in existing]
        db.add_all([KnowledgeBaseVideo(knowledge_base_id=knowledge_base_id, platform_item_id=item_id)
                    for item_id in new_ids])
        if new_ids:
            item.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.flush()
        return {"added_count": len(new_ids), "existing_count": len(existing), "selected_ids": selected_ids}

    def list_video_ids(self, db: Session, knowledge_base_id: int, done_only: bool = False) -> list[str]:
        self.require_base(db, knowledge_base_id)
        query = select(KnowledgeBaseVideo.platform_item_id).join(
            VideoCache, VideoCache.platform_item_id == KnowledgeBaseVideo.platform_item_id
        ).where(KnowledgeBaseVideo.knowledge_base_id == knowledge_base_id)
        if done_only:
            query = query.where(VideoCache.status == "done")
        return list(db.scalars(query.order_by(KnowledgeBaseVideo.added_at, VideoCache.id)).all())

    def list_videos(self, db: Session, knowledge_base_id: int, page: int = 1, size: int = 20) -> tuple[list[dict], int]:
        self.require_base(db, knowledge_base_id)
        total = int(db.scalar(select(func.count()).select_from(KnowledgeBaseVideo)
                              .where(KnowledgeBaseVideo.knowledge_base_id == knowledge_base_id)) or 0)
        rows = db.execute(
            select(VideoCache, FavoriteVideo)
            .join(KnowledgeBaseVideo, KnowledgeBaseVideo.platform_item_id == VideoCache.platform_item_id)
            .outerjoin(FavoriteVideo, FavoriteVideo.platform_item_id == VideoCache.platform_item_id)
            .where(KnowledgeBaseVideo.knowledge_base_id == knowledge_base_id)
            .order_by(desc(KnowledgeBaseVideo.added_at), desc(VideoCache.id))
            .offset((page - 1) * size).limit(size)
        ).all()
        return [{
            "id": cache.id,
            "collection_id": f"knowledge-base:{knowledge_base_id}",
            "platform_item_id": cache.platform_item_id,
            "url": favorite.video_url if favorite else f"https://www.douyin.com/video/{cache.platform_item_id}",
            "title": cache.title,
            "author": favorite.author if favorite else "",
            "duration": favorite.duration if favorite else round(cache.analysis_duration_seconds or 0),
            "status": cache.status,
        } for cache, favorite in rows], total

    def remove_video(self, db: Session, knowledge_base_id: int, platform_item_id: str) -> int:
        item = self.require_base(db, knowledge_base_id)
        result = db.execute(delete(KnowledgeBaseVideo).where(
            KnowledgeBaseVideo.knowledge_base_id == knowledge_base_id,
            KnowledgeBaseVideo.platform_item_id == platform_item_id,
        ))
        if result.rowcount:
            item.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.flush()
        return result.rowcount

    def delete(self, db: Session, knowledge_base_id: int) -> dict:
        """Caller holds worker maintenance_guard through commit/rollback.

        Keep the row, its ID and original name for history/authorization references.
        Only membership is removed; shared video results and other state are untouched.
        """
        item = self.require_base(db, knowledge_base_id)
        removed = db.execute(delete(KnowledgeBaseVideo).where(
            KnowledgeBaseVideo.knowledge_base_id == knowledge_base_id,
        ))
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        item.deleted_at = now
        item.updated_at = now
        # NUL is rejected in user names, so this reserved namespace cannot collide.
        # Keeping tombstone rows also prevents SQLite reusing the former max ID.
        item.name_key = f"\x00deleted:{item.id}"
        db.flush()
        return {"deleted_id": item.id, "removed_count": int(removed.rowcount)}

    @staticmethod
    def _to_dict(item: KnowledgeBase, total: int, ready: int) -> dict:
        return {"id": item.id, "name": item.name, "video_count": total, "ready_count": ready,
                "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat()}


knowledge_base_service = KnowledgeBaseService()
