"""
收藏夹同步服务模块

负责将从抖音抓取的快照数据与本地数据库进行差异对齐：
- 新增收藏夹 / 标记已删除的收藏夹为失效
- 新增视频 / 删除不再存在的视频关联
- 同步更新 VideoCache 表（增删视频时保持一致）
"""
from __future__ import annotations

import logging
from collections import OrderedDict

from sqlalchemy import delete, desc, func, select
from sqlalchemy.orm import Session

from app.models.entities import FavoriteCollection, FavoriteVideo, KnowledgeBaseVideo, VideoCache
from app.services.douyin_collector import (
    FavoriteScrapeSnapshot,
    collector,
)

logger = logging.getLogger(__name__)

# 虚拟收藏夹 ID，表示"全部收藏"
ALL_COLLECTION_ID = "all"
ALL_COLLECTION_TITLE = "全部收藏"


class FavoritesService:
    """
    收藏夹同步服务

    负责：
    1. 从抖音拉取最新收藏夹快照
    2. 与本地数据库差异对齐
    3. 提供收藏夹和视频的查询接口
    """

    async def sync_from_douyin(self, db: Session) -> dict:
        """
        从抖音同步收藏夹数据

        完整流程：
        1. 调用采集器获取最新快照
        2. 差异同步收藏夹表
        3. 差异同步视频表 + VideoCache 表
        4. 返回同步统计

        :param db: 数据库会话
        :return: 同步统计信息
        """
        snapshot = await collector.fetch_snapshot(
            max_collections=100, max_videos_per_collection=500
        )

        collections_by_platform = self._sync_collections(db, snapshot)
        added, removed = self._sync_videos_and_cache(
            db, snapshot, collections_by_platform
        )
        db.commit()

        total_collections = (
            db.scalar(
                select(func.count()).select_from(FavoriteCollection).where(
                    FavoriteCollection.is_active.is_(True)
                )
            )
            or 0
        )
        total_videos = (
            db.scalar(
                select(
                    func.count(func.distinct(FavoriteVideo.platform_item_id))
                )
                .select_from(FavoriteVideo)
                .where(FavoriteVideo.is_active.is_(True))
            )
            or 0
        )

        return {
            "collections_total": int(total_collections),
            "videos_total": int(total_videos),
            "added_videos": added,
            "removed_videos": removed,
        }

    def _sync_collections(
        self,
        db: Session,
        snapshot: FavoriteScrapeSnapshot,
    ) -> dict[str, FavoriteCollection]:
        """
        同步收藏夹表

        对比快照与数据库：
        - 新增的收藏夹 → 插入
        - 已有的收藏夹 → 更新标题/数量
        - 快照中不存在的 → 标记 is_active=False

        :param db: 数据库会话
        :param snapshot: 抖音抓取快照
        :return: {platform_collection_id: FavoriteCollection} 映射
        """
        existing = (
            db.execute(select(FavoriteCollection)).scalars().all()
        )
        existing_map = {
            row.platform_collection_id: row for row in existing
        }

        seen: set[str] = set()
        for col in snapshot.collections:
            seen.add(col.platform_collection_id)
            row = existing_map.get(col.platform_collection_id)

            if row is None:
                # 新增收藏夹
                row = FavoriteCollection(
                    platform_collection_id=col.platform_collection_id,
                    title=col.title,
                    video_count=col.video_count,
                    is_active=True,
                )
                db.add(row)
                existing_map[col.platform_collection_id] = row
            else:
                # 更新已有收藏夹
                row.title = col.title
                row.video_count = col.video_count
                row.is_active = True

        # 标记快照中不存在的收藏夹为失效
        for row in existing:
            if row.platform_collection_id not in seen:
                row.is_active = False

        db.flush()
        return existing_map

    def _sync_videos_and_cache(
        self,
        db: Session,
        snapshot: FavoriteScrapeSnapshot,
        collections_by_platform: dict[str, FavoriteCollection],
    ) -> tuple[int, int]:
        """
        同步视频表和 VideoCache 表

        1. 构建"期望状态"：(collection_id, platform_item_id) → 视频数据
        2. 对比现有数据，执行新增/更新/删除
        3. 同步更新 VideoCache（新增 pending 记录、删除孤儿记录）

        :param db: 数据库会话
        :param snapshot: 抖音抓取快照
        :param collections_by_platform: 收藏夹映射
        :return: (新增视频数, 删除视频数)
        """
        # 构建期望的关联关系和视频数据
        desired_pairs: dict[tuple[int, str], dict] = {}
        desired_video_payload: dict[str, dict] = {}

        for video in snapshot.videos:
            desired_video_payload[video.platform_item_id] = {
                "url": video.url,
                "title": video.title,
                "author": video.author,
                "duration": video.duration,
            }
            for collection_platform_id in video.collection_ids:
                collection = collections_by_platform.get(
                    collection_platform_id
                )
                if collection is None:
                    continue
                desired_pairs[(collection.id, video.platform_item_id)] = {
                    "collection_id": collection.id,
                    "platform_item_id": video.platform_item_id,
                    "url": video.url,
                    "title": video.title,
                    "author": video.author,
                    "duration": video.duration,
                }

        # 获取所有活跃收藏夹的视频
        active_collection_ids = [
            c.id
            for c in collections_by_platform.values()
            if c.is_active
        ]

        existing_rows = []
        if active_collection_ids:
            existing_rows = (
                db.execute(
                    select(FavoriteVideo).where(
                        FavoriteVideo.collection_id.in_(
                            active_collection_ids
                        )
                    )
                )
                .scalars()
                .all()
            )

        existing_map = {
            (row.collection_id, row.platform_item_id): row
            for row in existing_rows
        }
        existing_video_ids = {
            row.platform_item_id for row in existing_rows
        }
        desired_video_ids = set(desired_video_payload.keys())
        added_video_ids = desired_video_ids - existing_video_ids

        # 新增或更新视频关联
        for key, payload in desired_pairs.items():
            row = existing_map.get(key)
            if row is None:
                db.add(
                    FavoriteVideo(
                        collection_id=payload["collection_id"],
                        platform_item_id=payload["platform_item_id"],
                        video_url=payload["url"],
                        title=payload["title"],
                        author=payload["author"],
                        duration=payload["duration"] or 0,
                        is_active=True,
                    )
                )
                continue
            # 更新已有记录
            row.video_url = payload["url"]
            row.title = payload["title"]
            row.author = payload["author"]
            row.duration = payload["duration"] or 0
            row.is_active = True

        # 删除不再存在的关联
        removed_ids: set[str] = set()
        for key, row in existing_map.items():
            if key in desired_pairs:
                continue
            removed_ids.add(row.platform_item_id)
            db.delete(row)

        # 同步 VideoCache 表
        cache_rows = (
            db.execute(select(VideoCache)).scalars().all()
        )
        cache_map = {row.platform_item_id: row for row in cache_rows}

        for platform_item_id, payload in desired_video_payload.items():
            cache = cache_map.get(platform_item_id)
            if cache is None:
                db.add(
                    VideoCache(
                        platform_item_id=platform_item_id,
                        title=payload["title"],
                        status="pending",
                    )
                )
            else:
                cache.title = payload["title"]

        db.flush()

        # 删除孤儿的 VideoCache 记录（视频已不在任何收藏夹中）
        if removed_ids:
            still_used = {
                row[0]
                for row in db.execute(
                    select(FavoriteVideo.platform_item_id).where(
                        FavoriteVideo.platform_item_id.in_(removed_ids)
                    )
                ).all()
            }
            # Removing a Douyin favorite must not erase material in a named library.
            saved_in_library = set(db.scalars(
                select(KnowledgeBaseVideo.platform_item_id).where(
                    KnowledgeBaseVideo.platform_item_id.in_(removed_ids)
                )
            ).all())
            orphan_ids = sorted(removed_ids - still_used - saved_in_library)
            if orphan_ids:
                db.execute(
                    delete(VideoCache).where(
                        VideoCache.platform_item_id.in_(orphan_ids)
                    )
                )

        removed_count = len(existing_video_ids - desired_video_ids)
        return len(added_video_ids), removed_count

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def list_collections(self, db: Session) -> list[dict]:
        """
        获取收藏夹列表

        第一项固定为"全部收藏"（虚拟项），
        后续按视频数量降序排列。

        :param db: 数据库会话
        :return: 收藏夹列表
        """
        total = (
            db.scalar(
                select(
                    func.count(func.distinct(FavoriteVideo.platform_item_id))
                ).where(FavoriteVideo.is_active.is_(True))
            )
            or 0
        )

        rows = (
            db.execute(
                select(FavoriteCollection)
                .where(FavoriteCollection.is_active.is_(True))
                .order_by(
                    desc(FavoriteCollection.video_count),
                    desc(FavoriteCollection.updated_at),
                )
            )
            .scalars()
            .all()
        )

        result = [
            {
                "id": 0,
                "collection_id": ALL_COLLECTION_ID,
                "title": ALL_COLLECTION_TITLE,
                "video_count": int(total),
                "is_active": True,
            }
        ]
        for row in rows:
            result.append({
                "id": row.id,
                "collection_id": row.platform_collection_id,
                "title": row.title,
                "video_count": row.video_count,
                "is_active": row.is_active,
            })
        return result

    def list_collection_videos(
        self,
        db: Session,
        collection_id: str,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict], int]:
        """
        获取指定收藏夹的视频列表（分页）

        :param db: 数据库会话
        :param collection_id: 收藏夹 platform_collection_id（"all" 表示全部）
        :param page: 页码（从 1 开始）
        :param size: 每页数量
        :return: (视频列表, 总数量)
        """
        offset = (page - 1) * size

        if collection_id == ALL_COLLECTION_ID:
            # 全部收藏：去重（同一视频可能在多个收藏夹中）
            rows = (
                db.execute(
                    select(FavoriteVideo, VideoCache.status)
                    .join(
                        VideoCache,
                        VideoCache.platform_item_id
                        == FavoriteVideo.platform_item_id,
                        isouter=True,
                    )
                    .where(FavoriteVideo.is_active.is_(True))
                    .order_by(
                        desc(FavoriteVideo.updated_at),
                        desc(FavoriteVideo.id),
                    )
                )
                .all()
            )

            # 去重：同一 platform_item_id 只保留第一条
            dedup: OrderedDict[str, tuple] = OrderedDict()
            for row, status in rows:
                if row.platform_item_id in dedup:
                    continue
                dedup[row.platform_item_id] = (row, status or "pending")

            values = list(dedup.values())
            total = len(values)
            page_items = values[offset : offset + size]
            return (
                [
                    self._to_video_dict(row, status, ALL_COLLECTION_ID)
                    for row, status in page_items
                ],
                total,
            )

        # 指定收藏夹
        collection = (
            db.execute(
                select(FavoriteCollection).where(
                    FavoriteCollection.platform_collection_id
                    == collection_id,
                    FavoriteCollection.is_active.is_(True),
                )
            )
            .scalars()
            .one_or_none()
        )
        if collection is None:
            return [], 0

        total = (
            db.scalar(
                select(func.count())
                .select_from(FavoriteVideo)
                .where(
                    FavoriteVideo.collection_id == collection.id,
                    FavoriteVideo.is_active.is_(True),
                )
            )
            or 0
        )

        rows = (
            db.execute(
                select(FavoriteVideo, VideoCache.status)
                .join(
                    VideoCache,
                    VideoCache.platform_item_id
                    == FavoriteVideo.platform_item_id,
                    isouter=True,
                )
                .where(
                    FavoriteVideo.collection_id == collection.id,
                    FavoriteVideo.is_active.is_(True),
                )
                .order_by(
                    desc(FavoriteVideo.updated_at),
                    desc(FavoriteVideo.id),
                )
                .offset(offset)
                .limit(size)
            )
            .all()
        )

        return (
            [
                self._to_video_dict(
                    row, status or "pending", collection_id
                )
                for row, status in rows
            ],
            int(total),
        )

    @staticmethod
    def _to_video_dict(
        row: FavoriteVideo, status: str, collection_id: str
    ) -> dict:
        """
        将 FavoriteVideo ORM 对象转换为字典

        :param row: FavoriteVideo 对象
        :param status: VideoCache 处理状态
        :param collection_id: 所属收藏夹 ID
        :return: 视频信息字典
        """
        return {
            "id": row.id,
            "collection_id": collection_id,
            "platform_item_id": row.platform_item_id,
            "url": row.video_url,
            "title": row.title,
            "author": row.author,
            "duration": row.duration,
            "status": status,
        }


# 全局单例
favorites_service = FavoritesService()
