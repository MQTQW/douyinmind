"""
知识库入库服务模块

编排视频入库流水线：
1. 从 VideoCache 获取 pending 状态的视频
2. 默认提取音频 → 千问完整转写 → 保存结果 → 可搜索资料入库
3. 更新 VideoCache 状态
"""
from __future__ import annotations

import logging
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, desc, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import KnowledgeBaseVideo, VideoCache
from app.db.session import session_factory
from app.services.chroma_service import get_chroma_service
from app.services.llm_service import embedding_client
from app.services.video_media import download_video
from app.services.media_service import download_audio
from app.services.omni_service import omni_service
from app.services.knowledge_base_service import knowledge_base_service
from app.services.worker import worker
from app.services.ingestion_metrics import IngestionRun

logger = logging.getLogger(__name__)


class KnowledgeService:
    """
    知识库入库服务

    负责：
    1. 触发入库任务
    2. 逐视频执行入库流水线
    3. 提供知识库统计
    """

    def start_sync(
        self, db: Session, selected_ids: list[str] | None = None,
        knowledge_base_id: int | None = None,
    ) -> dict:
        """Coordinate this request's DB transaction with explicit recovery.

        The reservation ends only after commit/rollback; no worker mutex is
        held while waiting for SQLite. Membership commit and queue publication
        are still separate resources: a failed publisher does not undo a
        previously committed library assignment or auto-submit another job.
        """
        with worker.submission_guard():
            try:
                return self._start_sync_reserved(db, selected_ids, knowledge_base_id)
            except BaseException:
                db.rollback()
                raise

    def _start_sync_reserved(
        self, db: Session, selected_ids: list[str] | None = None,
        knowledge_base_id: int | None = None,
    ) -> dict:
        """
        启动入库任务

        默认查询 pending 视频；明确选择时允许 pending/failed 视频。
        空选择、未知编号不提交，重复编号只处理一次。

        :param db: 数据库会话
        :param selected_ids: 可选，限定入库的视频 ID 列表
        :return: {"task_id": ..., "pending_count": ...}
        """
        skipped_count = 0
        if knowledge_base_id is not None:
            knowledge_base_service.require_base(db, knowledge_base_id)
            # Importing into a named destination must have an explicit selection.
            # A missing list must never classify every synchronized favorite.
            if not selected_ids:
                raise ValueError("请至少选择一个视频导入知识库。")
        if selected_ids is None:
            # Keep the original all-pending behavior; failed videos require an
            # explicit selection or the existing reset-failed action.
            pending_videos = db.scalars(
                select(VideoCache).where(VideoCache.status == "pending")
                .order_by(VideoCache.id)
            ).all()
        else:
            if not selected_ids:
                raise ValueError("请至少选择一个待入库视频。")
            if any(not item_id.strip() for item_id in selected_ids):
                raise ValueError("所选视频编号无效，请重新选择。")
            ids = list(dict.fromkeys(selected_ids))
            selected_videos = db.scalars(
                select(VideoCache).where(VideoCache.platform_item_id.in_(ids))
            ).all()
            by_id = {video.platform_item_id: video for video in selected_videos}
            if any(item_id not in by_id for item_id in ids):
                raise ValueError("部分所选视频已不存在，请刷新收藏夹后重新选择。")
            pending_videos = [by_id[item_id] for item_id in ids
                              if by_id[item_id].status in ("pending", "failed")]
            skipped_count = len(ids) - len(pending_videos)

        # Configuration/preflight failures must not leave partial memberships.
        # A later queue-publication failure cannot undo an already committed
        # assignment; it must not auto-submit another model task.
        if pending_videos:
            omni_service.check_configuration()

        assignment = {}
        if knowledge_base_id is not None:
            assignment = knowledge_base_service.assign_videos(db, knowledge_base_id, ids)
            assignment = {
                "knowledge_base_id": knowledge_base_id,
                "assigned_count": assignment["added_count"],
                "existing_count": assignment["existing_count"],
            }

        # End even read-only transactions before publishing or returning. API
        # dependency cleanup runs later and cannot own this protocol boundary.
        pending_ids = [video.platform_item_id for video in pending_videos]
        db.commit()

        if not pending_videos:
            return {
                "task_id": None,
                "pending_count": 0,
                "skipped_count": skipped_count,
                **assignment,
                "message": (f"已加入知识库 {assignment['assigned_count']} 个视频，"
                            f"{assignment['existing_count']} 个视频已在该库中；"
                            "已完成的视频复用文字稿，处理中的视频完成后会自动显示。"
                            if knowledge_base_id is not None else
                            "所选视频已入库或正在处理中，无需重复提交。"
                            if selected_ids is not None
                            else "没有待入库的视频，请先同步收藏夹"),
            }

        task_id = str(uuid.uuid4())[:8]
        worker.submit(
            task_id,
            self._run_sync,
            pending_ids,
        )

        return {
            "task_id": task_id,
            "pending_count": len(pending_videos),
            "skipped_count": skipped_count,
            **assignment,
            "message": f"已提交入库任务，共 {len(pending_videos)} 个视频",
        }

    def _run_sync(
        self, platform_item_ids: list[str], task_id: str = "",
    ) -> None:
        # A long-running job must own its session, rather than reuse the API's session.
        with session_factory() as db:
            self._process_videos(db, platform_item_ids, task_id)

    def _process_videos(
        self,
        db: Session,
        platform_item_ids: list[str],
        task_id: str = "",
    ) -> None:
        """
        执行入库流水线（在后台线程中运行）

        逐个处理待入库视频：
        pending → downloading → transcribing → embedding → done

        :param db: 数据库会话
        :param platform_item_ids: 待处理的视频 ID 列表
        :param task_id: 任务 ID（由 worker 回调传入）
        """
        total = len(platform_item_ids)
        chroma = get_chroma_service()
        failures = 0

        for idx, platform_item_id in enumerate(platform_item_ids):
            # 获取视频缓存记录
            cache = db.execute(
                select(VideoCache).where(
                    VideoCache.platform_item_id == platform_item_id
                )
            ).scalar_one_or_none()

            if cache is None:
                failures += 1
                logger.warning("VideoCache 记录不存在: %s", platform_item_id)
                continue

            # Two libraries can request the same pending video before a queued
            # task starts. If an earlier task has finished it, reuse its result.
            if cache.status == "done":
                worker.update_progress(task_id, idx + 1, total, f"复用已入库资料: {cache.title[:30]}")
                continue

            run = IngestionRun(platform_item_id, cache.title, task_id, settings.knowledge_analysis_mode)
            try:
                # 构建视频 URL
                video_url = f"https://www.douyin.com/video/{platform_item_id}"

                audio_only = settings.knowledge_analysis_mode == "audio"
                # The audio release avoids visual downloads/encoding and visual calls.
                cache.status = "downloading"
                db.commit()
                worker.update_progress(
                    task_id, idx, total,
                    f"{'下载音频' if audio_only else '下载完整视频'}: {cache.title[:30]}...",
                )
                run.start_stage("download")
                media_path = (download_audio if audio_only else download_video)(video_url, platform_item_id)
                run.end_stage()

                # Step 2: Preserve the full speech transcript and visual observations.
                cache.status = "transcribing"
                db.commit()
                def analysis_progress(part: int, count: int) -> None:
                    worker.update_progress(task_id, idx, total,
                        f"{'完整语音转写，已完成片段' if audio_only else '声音与画面识别'} {part}/{count}: {cache.title[:30]}...")
                run.start_stage("recognition")
                result = (omni_service.analyze_audio if audio_only else omni_service.analyze_video)(media_path, analysis_progress)
                run.end_stage()
                # Changing the release mode must not erase visuals saved by an earlier run.
                if audio_only and cache.visual_analysis_text:
                    try:
                        previous = json.loads(cache.analysis_json or "{}")
                    except (ValueError, TypeError):
                        previous = {}
                    if not isinstance(previous, dict):
                        previous = {}
                    result.visual_analysis_text = cache.visual_analysis_text
                    result.payload["visual_segments"] = previous.get("visual_segments", [])
                    result.payload["retained_visual_analysis"] = True
                    previous_visual_result = previous.get("previous_visual_result")
                    result.payload["previous_visual_result"] = previous_visual_result if isinstance(previous_visual_result, dict) else {
                        "model": cache.analysis_model, "raw_results": previous.get("raw_results", [])}
                    result.payload["duration_seconds"] = max(result.payload["duration_seconds"], cache.analysis_duration_seconds or 0)
                cache.transcript_text = result.transcript_text
                cache.visual_analysis_text = result.visual_analysis_text
                cache.analysis_json = json.dumps(result.payload, ensure_ascii=False)
                cache.analysis_model = result.payload["model"]
                cache.analysis_duration_seconds = result.payload["duration_seconds"]
                cache.error_message = ""
                # Retain recognition results even if later indexing fails.
                db.commit()

                # Step 3: Index both source types; silent videos can have useful visuals.
                run.start_stage("indexing")
                chunks = result.knowledge_chunks()
                if not chunks:
                    logger.warning(
                        "声音和画面分析为空: %s", platform_item_id
                    )
                    cache.status = "failed"
                    raise RuntimeError("音频没有可识别的人声，暂无文字稿可入库。" if audio_only else "没有获得可入库的声音或画面内容")

                # 追加元信息文档（标题 + 作者搜索召回优化）
                from app.models.entities import FavoriteVideo as FV
                fv = db.query(FV).filter(
                    FV.platform_item_id == platform_item_id
                ).first()
                meta_chunk = f"视频标题：{cache.title}"
                if fv and fv.author:
                    meta_chunk += f"\n作者：{fv.author}"
                chunks = [meta_chunk] + chunks

                # Step 4: Embedding + 存入 ChromaDB
                cache.status = "embedding"
                db.commit()
                worker.update_progress(
                    task_id, idx, total,
                    f"向量化入库: {cache.title[:30]}...",
                )
                embeddings = embedding_client.embed_texts(chunks)
                chroma.upsert_video_chunks(
                    platform_item_id=platform_item_id,
                    title=cache.title,
                    chunks=chunks,
                    embeddings=embeddings,
                )

                # Step 5: 标记完成
                cache.status = "done"
                cache.processed_at = datetime.now(timezone.utc)
                db.commit()
                run.end_stage()
                keep_audio = (settings.long_video_test_keep_audio and audio_only
                              and 360 <= cache.analysis_duration_seconds <= 1860)
                run.finish("done", details={"duration_seconds": cache.analysis_duration_seconds,
                           "transcript_characters": len(cache.transcript_text), "indexed_chunks": len(chunks),
                           "audio_retained_for_test": keep_audio,
                           "audio_processing": result.payload.get("audio_processing")})
                worker.update_progress(task_id, idx + 1, total, f"已完成: {cache.title[:30]}")

                logger.info(
                    "入库成功 (%d/%d): %s (%d chunks)",
                    idx + 1,
                    total,
                    platform_item_id,
                    len(chunks),
                )

                # Results and raw model responses are already retained in the database.
                try:
                    if not keep_audio:
                        media_path.unlink(missing_ok=True)
                except Exception:
                    pass

            except Exception as exc:
                run.finish("failed", error_type=type(exc).__name__)
                failures += 1
                db.rollback()
                logger.exception(
                    "入库失败 (%d/%d): %s", idx + 1, total, platform_item_id
                )
                cache.status = "failed"
                cache.error_message = str(exc)[:1000]
                db.commit()

        if failures:
            raise RuntimeError(f"入库结束：{total - failures} 个成功，{failures} 个失败。识别成功的结果已保存，可查看后重试。")

    def get_stats(self, db: Session) -> dict:
        """
        获取知识库统计信息

        :param db: 数据库会话
        :return: 统计数据
        """
        # 各状态视频数量
        pending = db.scalar(
            select(func.count()).select_from(VideoCache).where(
                VideoCache.status == "pending"
            )
        ) or 0
        done = db.scalar(
            select(func.count()).select_from(VideoCache).where(
                VideoCache.status == "done"
            )
        ) or 0
        failed = db.scalar(
            select(func.count()).select_from(VideoCache).where(
                VideoCache.status == "failed"
            )
        ) or 0
        downloading = db.scalar(
            select(func.count()).select_from(VideoCache).where(
                VideoCache.status == "downloading"
            )
        ) or 0
        transcribing = db.scalar(
            select(func.count()).select_from(VideoCache).where(
                VideoCache.status == "transcribing"
            )
        ) or 0
        embedding = db.scalar(select(func.count()).select_from(VideoCache).where(VideoCache.status == "embedding")) or 0

        # ChromaDB 统计
        chroma = get_chroma_service()
        chroma_chunks = chroma.count()
        chroma_videos = chroma.get_video_count()

        return {
            "analysis_mode": settings.knowledge_analysis_mode,
            "audio_transcription_concurrency": settings.audio_transcription_concurrency,
            "current_task_id": worker.get_current_task(),
            "video_cache": {
                "pending": int(pending),
                "downloading": int(downloading),
                "transcribing": int(transcribing),
                "embedding": int(embedding),
                "done": int(done),
                "failed": int(failed),
            },
            "chromadb": {
                "total_chunks": chroma_chunks,
                "total_videos": chroma_videos,
            },
        }

    def get_progress(self, task_id: str) -> dict | None:
        """
        查询入库任务进度

        :param task_id: 任务 ID
        :return: 进度信息
        """
        return worker.get_progress(task_id)


    def delete_video(self, db: Session, platform_item_id: str) -> dict:
        """
        删除已入库视频

        清理 ChromaDB 向量、重置 VideoCache 状态、删除音频缓存。

        :param db: 数据库会话
        :param platform_item_id: 抖音视频 ID
        :return: 删除结果
        :raises ValueError: 视频不存在或正在入库中
        """
        cache = db.query(VideoCache).filter(
            VideoCache.platform_item_id == platform_item_id
        ).first()

        if cache is None:
            raise ValueError(f"视频 {platform_item_id} 不存在")
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]+", platform_item_id):
            raise ValueError("视频编号无效")
        if cache.status in ("downloading", "transcribing", "embedding"):
            raise ValueError("视频正在入库中，无法删除")

        # 1. 删除 ChromaDB 向量
        chroma = get_chroma_service()
        chroma.delete_by_video(platform_item_id)

        # 2. 重置 VideoCache
        # This action deletes the shared transcript everywhere. Removing from a
        # single library uses the dedicated membership endpoint instead.
        db.execute(delete(KnowledgeBaseVideo).where(
            KnowledgeBaseVideo.platform_item_id == platform_item_id))
        cache.status = "pending"
        cache.transcript_text = ""
        cache.visual_analysis_text = ""
        cache.analysis_json = "{}"
        cache.analysis_model = ""
        cache.analysis_duration_seconds = 0
        cache.error_message = ""
        cache.processed_at = None

        # 3. 清理音频缓存
        audio_dir = Path(settings.audio_cache_dir)
        for ext in (".mp3", ".wav", ".m4a", ".webm", ".opus"):
            audio_file = audio_dir / f"{platform_item_id}{ext}"
            if audio_file.exists():
                audio_file.unlink(missing_ok=True)
            audio_file.with_name(audio_file.name + ".analysis.json").unlink(missing_ok=True)
        for ext in (".mp4", ".mkv", ".webm", ".mov"):
            video_file = Path(settings.video_cache_dir) / f"{platform_item_id}{ext}"
            video_file.unlink(missing_ok=True)

        db.commit()
        logger.info("视频已删除: %s", platform_item_id)
        return {"platform_item_id": platform_item_id, "status": "pending"}


# 全局单例
knowledge_service = KnowledgeService()
