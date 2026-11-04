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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import VideoCache
from app.db.session import session_factory
from app.services.chroma_service import get_chroma_service
from app.services.llm_service import embedding_client
from app.services.video_media import download_video
from app.services.media_service import download_audio
from app.services.knowledge_base_service import knowledge_base_service
from app.services.worker import WorkerBusyError, worker
from app.services.ingestion_jobs import job_metadata, task_identity, record_kind
from app.services.ingestion_metrics import IngestionRun

logger = logging.getLogger(__name__)


class _OmniFacade:
    """Read-only knowledge APIs do not initialize optional recognition SDKs."""
    @staticmethod
    def check_configuration(*args, **kwargs):
        from app.services.omni_service import omni_service as actual
        return actual.check_configuration(*args, **kwargs)

    @staticmethod
    def analyze_audio(*args, **kwargs):
        from app.services.omni_service import omni_service as actual
        return actual.analyze_audio(*args, **kwargs)

    @staticmethod
    def analyze_video(*args, **kwargs):
        from app.services.omni_service import omni_service as actual
        return actual.analyze_video(*args, **kwargs)


omni_service = _OmniFacade()


class KnowledgeRecoveryError(ValueError):
    def __init__(self, code, message, *, reset_count=0, needs_review=False, task_id=None):
        self.code, self.message = code, message
        self.reset_count, self.needs_review, self.task_id = reset_count, needs_review, task_id
        super().__init__(message)


def recovery_task_id(task_id, request_id):
    task_identity(task_id)
    request_id = str(uuid.UUID(str(request_id)))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'DouyinMind.ingestion.retry.v1:' + task_id + ':' + request_id))


def retry_metadata(record):
    try:
        value = job_metadata(record.get('metadata'))
        if (record_kind(record) != 'ingestion'
                or record.get('status') not in {'failed', 'interrupted', 'needs_review'}
                or record.get('persistence_error') is True
                or not value.get('platform_item_ids')
                or not {'knowledge_base_id', 'analysis_mode'} <= value.keys()):
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise KnowledgeRecoveryError('INGESTION_RETRY_NOT_ALLOWED',
            '这项任务没有可安全重新提交的已记录范围。请核对原记录后从收藏夹重新选择。') from None


@contextmanager
def _business_boundary(db, guard):
    with guard:
        try:
            yield
        except BaseException:
            db.rollback()
            raise


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
        *, _recovery_of: str | None = None, _task_id: str | None = None,
    ) -> dict:
        """Coordinate this request's DB transaction with explicit recovery.

        The reservation ends only after commit/rollback; no worker mutex is
        held while waiting for SQLite. Membership commit and queue publication
        are still separate resources: a failed publisher does not undo a
        previously committed library assignment or auto-submit another job.
        """
        with worker.submission_guard():
            try:
                return self._start_sync_reserved(db, selected_ids, knowledge_base_id,
                    recovery_of=_recovery_of, task_id=_task_id)
            except BaseException:
                db.rollback()
                raise

    def _start_sync_reserved(
        self, db: Session, selected_ids: list[str] | None = None,
        knowledge_base_id: int | None = None,
        *, recovery_of: str | None = None, task_id: str | None = None,
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
                raise KnowledgeRecoveryError('INGESTION_EMPTY_SELECTION', '请至少选择一个视频导入知识库。')
        if selected_ids is None:
            # Keep the original all-pending behavior; failed videos require an
            # explicit selection or the existing reset-failed action.
            pending_videos = db.scalars(
                select(VideoCache).where(VideoCache.status == "pending")
                .order_by(VideoCache.id)
            ).all()
        else:
            if not selected_ids:
                raise KnowledgeRecoveryError('INGESTION_EMPTY_SELECTION', '请至少选择一个待入库视频。')
            if any(not item_id.strip() for item_id in selected_ids):
                raise ValueError("所选视频编号无效，请重新选择。")
            ids = list(dict.fromkeys(selected_ids))
            selected_videos = db.scalars(
                select(VideoCache).where(VideoCache.platform_item_id.in_(ids)).execution_options(populate_existing=True)
            ).all()
            by_id = {video.platform_item_id: video for video in selected_videos}
            if any(item_id not in by_id for item_id in ids):
                raise ValueError("部分所选视频已不存在，请刷新收藏夹后重新选择。")
            if any(by_id[item_id].status == 'deleting' for item_id in ids):
                raise ValueError('所选视频有删除待完成的资料，请先核对原删除操作。')
            pending_videos = [by_id[item_id] for item_id in ids
                              if by_id[item_id].status in ("pending", "failed")]
            skipped_count = len(ids) - len(pending_videos)

        # Configuration/preflight failures must not leave partial memberships.
        # A later queue-publication failure cannot undo an already committed
        # assignment; it must not auto-submit another model task.
        if pending_videos:
            active_reader = getattr(worker, 'active_video_tasks', None)
            active = active_reader() if callable(active_reader) else {}
            if any(video.platform_item_id in active for video in pending_videos):
                raise WorkerBusyError('所选视频已有正在处理或排队的任务，请刷新原任务后再操作。')
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

        task_id = task_id or str(uuid.uuid4())
        metadata = {'platform_item_ids': pending_ids, 'knowledge_base_id': knowledge_base_id,
                    'analysis_mode': settings.knowledge_analysis_mode}
        if recovery_of is not None: metadata['recovery_of'] = task_identity(recovery_of)
        try:
            worker.submit(task_id, self._run_sync, pending_ids, job_metadata=metadata)
        except Exception:
            raise KnowledgeRecoveryError('INGESTION_PUBLICATION_UNCONFIRMED',
                '当前资料和归类状态已保留，但未确认新入库任务提交。请刷新任务与视频状态核对，不要重复提交。',
                needs_review=True, task_id=task_id) from None

        return {
            "task_id": task_id,
            "pending_count": len(pending_videos),
            "skipped_count": skipped_count,
            **assignment,
            "message": f"已提交入库任务，共 {len(pending_videos)} 个视频",
        }

    @staticmethod
    def public_task(record):
        kind = record_kind(record)
        if kind == 'notes':
            raise KnowledgeRecoveryError('INGESTION_TASK_KIND_MISMATCH', '这项任务不是入库任务，请从原笔记任务查看。')
        keys = ('task_id', 'kind', 'status', 'stage', 'progress', 'total', 'message', 'metadata', 'reason_code',
                'current_item_id', 'created_at', 'updated_at', 'started_at', 'finished_at',
                'needs_review', 'usage_state', 'shutdown_requested_at', 'shutdown_timed_out', 'persistence_error')
        value = {key: record[key] for key in keys if key in record}
        value['kind'] = kind
        try:
            value['metadata'] = job_metadata(record.get('metadata'))
            retry_metadata(record)
            value['can_retry'] = True
        except (ValueError, TypeError, AttributeError):
            value['can_retry'] = False
            try: value['metadata'] = job_metadata(record.get('metadata'))
            except (ValueError, TypeError): value['metadata'] = {}
        value['usage_state'] = 'not_recorded'
        fixed = {'failed': '本次处理未完成，已保留原任务与已保存资料。',
                 'interrupted': '处理已中断，请核对原资料与调用结果后再明确重新提交。',
                 'needs_review': '任务需要核对，未自动重新执行。'}
        if value.get('status') in fixed: value['message'] = fixed[value['status']]
        if record.get('persistence_error') is True:
            value.update(can_retry=False, message='任务记录暂未确认，请保留原记录并核对服务状态。')
        return value

    def list_tasks(self, limit=50, offset=0):
        return {'items': [self.public_task(item) for item in worker.list_tasks(limit=limit, offset=offset, kind='ingestion')],
                'total': worker.count_tasks(kind='ingestion'), 'limit': limit, 'offset': offset}

    def recovery_result(self, task_id, request_id):
        identity = recovery_task_id(task_id, request_id)
        original = worker.get_progress(task_id)
        if original is not None and record_kind(original) != 'ingestion':
            raise KnowledgeRecoveryError('INGESTION_TASK_KIND_MISMATCH', '这项任务不是入库任务，未重新提交。')
        record = worker.get_progress(identity)
        if record is None:
            return {'found': False, 'task_id': identity, 'recovery_of': task_id,
                    'message': '尚未查到这次重新提交的任务记录，请核对视频状态；当前没有再次提交。'}
        if record_kind(record) != 'ingestion' or record.get('metadata', {}).get('recovery_of') != task_id:
            raise KnowledgeRecoveryError('INGESTION_RETRY_ID_CONFLICT', '这次提交标识与原任务不一致，未继续处理。')
        return {'found': True, 'task_id': identity, 'recovery_of': task_id, 'task': self.public_task(record),
                'message': '已找到这次提交的原任务记录，没有再次执行。'}

    def retry_sync(self, db: Session, task_id: str, request_id: str):
        existing = self.recovery_result(task_id, request_id)
        if existing['found']: return {**existing, 'duplicate': True}
        reset_count = 0
        reset_committed = False
        try:
            with _business_boundary(db, worker.maintenance_guard()):
                existing = self.recovery_result(task_id, request_id)
                if existing['found']: return {**existing, 'duplicate': True}
                old = worker.get_progress(task_id)
                if old is None:
                    raise KnowledgeRecoveryError('INGESTION_TASK_MISSING', '原任务记录不存在，未重新提交。')
                metadata = retry_metadata(old)
                ids, base_id = metadata['platform_item_ids'], metadata['knowledge_base_id']
                if base_id is not None:
                    try: knowledge_base_service.require_base(db, base_id)
                    except ValueError:
                        raise KnowledgeRecoveryError('INGESTION_LIBRARY_UNAVAILABLE',
                            '原任务的目标知识库已不可用，未重新提交。请保留原记录并重新选择目标。') from None
                rows = list(db.scalars(select(VideoCache).where(VideoCache.platform_item_id.in_(ids))
                    .execution_options(populate_existing=True)))
                by_id = {row.platform_item_id: row for row in rows}
                if any(identity not in by_id for identity in ids):
                    raise KnowledgeRecoveryError('INGESTION_TARGET_MISSING', '原任务中的部分视频已不存在，未重新提交。')
                if any(row.status == 'deleting' for row in rows):
                    raise KnowledgeRecoveryError('INGESTION_TARGET_DELETING', '原任务中有删除待完成的资料，请先核对原删除操作。')
                if any(row.status not in {'done', 'pending', 'failed', 'downloading', 'transcribing', 'embedding'} for row in rows):
                    raise KnowledgeRecoveryError('INGESTION_TARGET_UNCONFIRMED', '原任务中的视频状态需要核对，未重新提交。')
                for row in rows:
                    if row.status in {'downloading', 'transcribing', 'embedding'}:
                        row.status = 'pending'
                        reset_count += 1
                try:
                    db.commit()
                    reset_committed = True
                except Exception:
                    raise KnowledgeRecoveryError('INGESTION_RETRY_RESET_UNCONFIRMED',
                        '视频状态重置结果尚未确认，尚未提交新任务。请保留原记录并刷新核对。',
                        needs_review=True, task_id=existing['task_id']) from None
            result = self.start_sync(db, ids, base_id, _recovery_of=task_id, _task_id=existing['task_id'])
            if result['task_id'] is None:
                with _business_boundary(db, worker.submission_guard()):
                    current = dict(db.execute(select(VideoCache.platform_item_id, VideoCache.status)
                        .where(VideoCache.platform_item_id.in_(ids))).all())
                    if len(current) != len(ids) or any(current.get(identity) != 'done' for identity in ids):
                        raise KnowledgeRecoveryError('INGESTION_RETRY_ALREADY_ACTIVE',
                            '原视频已由其他处理接手或状态发生变化，本次没有另建处理任务。请先核对现有任务。')
                    db.commit()
                    # A zero-work record is an idempotency receipt. It does not
                    # run recognition or erase unknown service usage.
                    worker.submit(existing['task_id'], self._run_sync, [], job_metadata={
                        'platform_item_ids': [], 'knowledge_base_id': base_id,
                        'analysis_mode': settings.knowledge_analysis_mode, 'recovery_of': task_id})
                    result['task_id'] = existing['task_id']
            return {**result, 'recovery_of': task_id, 'reset_count': reset_count, 'duplicate': False,
                    'message': '已登记新的处理任务；原记录和未知费用保留，已完成资料会复用。'}
        except BaseException as error:
            if not isinstance(error, Exception): raise
            # A racing request may have registered this exact deterministic ID.
            observed = self.recovery_result(task_id, request_id)
            if observed['found']: return {**observed, 'duplicate': True, 'reset_count': reset_count}
            if reset_committed and reset_count:
                raise KnowledgeRecoveryError('INGESTION_RETRY_PARTIAL_RESET',
                    '这批视频的中断状态已重置，但未确认新任务提交。请保留原记录并刷新核对，不要直接换新请求重试。',
                    reset_count=reset_count, needs_review=True, task_id=existing['task_id']) from None
            if isinstance(error, (KnowledgeRecoveryError, WorkerBusyError)): raise
            raise KnowledgeRecoveryError('INGESTION_RETRY_UNAVAILABLE',
                '这次重新提交未完成，请先核对原任务、知识库和服务状态。原记录已保留。') from None

    def _run_sync(
        self, platform_item_ids: list[str], task_id: str = "",
    ) -> None:
        if not platform_item_ids: return
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
            if cache.status == 'deleting':
                failures += 1
                continue

            # Two libraries can request the same pending video before a queued
            # task starts. If an earlier task has finished it, reuse its result.
            if cache.status == "done":
                self._report_progress(task_id, idx + 1, total, f"复用已入库资料: {cache.title[:30]}",
                    stage='reusing', item_id=platform_item_id)
                continue

            run = IngestionRun(platform_item_id, cache.title, task_id, settings.knowledge_analysis_mode)
            try:
                # 构建视频 URL
                video_url = f"https://www.douyin.com/video/{platform_item_id}"

                audio_only = settings.knowledge_analysis_mode == "audio"
                # The audio release avoids visual downloads/encoding and visual calls.
                cache.status = "downloading"
                db.commit()
                self._report_progress(
                    task_id, idx, total,
                    f"{'下载音频' if audio_only else '下载完整视频'}: {cache.title[:30]}...",
                    stage='downloading', item_id=platform_item_id,
                )
                run.start_stage("download")
                media_path = (download_audio if audio_only else download_video)(video_url, platform_item_id)
                run.end_stage()

                # Step 2: Preserve the full speech transcript and visual observations.
                cache.status = "transcribing"
                db.commit()
                def analysis_progress(part: int, count: int) -> None:
                    self._report_progress(task_id, idx, total,
                        f"{'完整语音转写，已完成片段' if audio_only else '声音与画面识别'} {part}/{count}: {cache.title[:30]}...",
                        stage='transcribing', item_id=platform_item_id)
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
                self._report_progress(
                    task_id, idx, total,
                    f"向量化入库: {cache.title[:30]}...",
                    stage='embedding', item_id=platform_item_id,
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
                self._report_progress(task_id, idx + 1, total, f"已完成: {cache.title[:30]}",
                    stage='saving', item_id=platform_item_id)

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

    @staticmethod
    def _report_progress(task_id, progress, total, message, *, stage, item_id):
        kwargs = {'stage': stage}
        active_reader = getattr(worker, 'active_video_tasks', None)
        declared = active_reader() if callable(active_reader) else {}
        # Legacy/native jobs may not declare business IDs. Do not invent a
        # current-item assertion for them merely because they share this code.
        if declared.get(item_id) == task_id: kwargs['item_id'] = item_id
        worker.update_progress(task_id, progress, total, message, **kwargs)

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
        deleting = db.scalar(select(func.count()).select_from(VideoCache).where(VideoCache.status == 'deleting')) or 0

        # ChromaDB 统计
        chroma = get_chroma_service()
        chroma_chunks = chroma.count()
        chroma_videos = chroma.get_video_count()
        activity_reader = getattr(worker, 'activity_snapshot', None)
        if callable(activity_reader):
            activity = activity_reader()
        else:
            # Legacy doubles/owners cannot prove the absence of queued work.
            activity = {'active_task_id': worker.get_current_task(), 'active_task_kind': None,
                        'maintenance_busy': True, 'activity_known': False}
        legacy_current = (None if activity['active_task_kind'] == 'notes' else activity['active_task_id'])

        return {
            "analysis_mode": settings.knowledge_analysis_mode,
            "audio_transcription_concurrency": settings.audio_transcription_concurrency,
            "current_task_id": legacy_current,
            **activity,
            "video_cache": {
                "pending": int(pending),
                "downloading": int(downloading),
                "transcribing": int(transcribing),
                "embedding": int(embedding),
                "done": int(done),
                "failed": int(failed),
                "deleting": int(deleting),
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
        value = worker.get_progress(task_id)
        if value is not None and record_kind(value) == 'notes':
            raise KnowledgeRecoveryError('INGESTION_TASK_KIND_MISMATCH', '这项任务不是入库任务，请从原笔记任务查看。')
        return value


    def delete_video(self, db: Session, platform_item_id: str) -> dict:
        """Caller holds maintenance exclusion across every deletion phase."""
        from app.services.material_deletion import delete_material
        return delete_material(db, platform_item_id, chroma=get_chroma_service(),
            audio_dir=Path(settings.audio_cache_dir), video_dir=Path(settings.video_cache_dir))

    def resume_deletion(self, db: Session, operation_id: str):
        from app.models.entities import KnowledgeDeletionOperation
        from app.services.material_deletion import DeletionIncomplete, delete_material, describe_operation
        operation = db.scalar(select(KnowledgeDeletionOperation).where(
            KnowledgeDeletionOperation.operation_id == operation_id).execution_options(populate_existing=True))
        if operation is None: raise DeletionIncomplete(operation_id, 'operation_not_found')
        if operation.state == 'done':
            # An old completed operation is only a receipt. Do not instantiate
            # the index or delete a later re-import of the same video ID.
            return {**describe_operation(operation), 'status': 'pending', 'already_completed': True}
        return delete_material(db, operation.platform_item_id, chroma=get_chroma_service(),
            audio_dir=Path(settings.audio_cache_dir), video_dir=Path(settings.video_cache_dir),
            expected_operation_id=operation_id)


# 全局单例
knowledge_service = KnowledgeService()
