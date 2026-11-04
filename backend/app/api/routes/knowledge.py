"""
知识库路由模块

提供收藏夹一键入库、入库进度查询、知识库统计、视频内容导出等接口。
"""
import logging
import json
from typing import Literal
from uuid import UUID
from datetime import timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.entities import FavoriteVideo, VideoCache, KnowledgeDeletionOperation
from app.services.knowledge_service import KnowledgeRecoveryError, knowledge_service
from app.services.worker import WorkerBusyError, worker
from app.services.ingestion_metrics import latest_ingestion_run
from app.services.material_deletion import DeletionIncomplete, describe_operation
from app.assistant.routes import local_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["知识库"])


@router.get("/videos")
def list_knowledge_videos(
    status: Literal["done", "all"] = "done",
    page: int = Query(1, ge=1),
    size: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """List saved material independently of current Douyin favorites."""
    conditions = [VideoCache.status == "done"] if status == "done" else []
    total = db.scalar(select(func.count()).select_from(VideoCache).where(*conditions)) or 0
    rows = db.execute(
        select(VideoCache, FavoriteVideo)
        .outerjoin(FavoriteVideo, FavoriteVideo.platform_item_id == VideoCache.platform_item_id)
        .where(*conditions)
        .order_by(VideoCache.updated_at.desc(), VideoCache.id.desc())
        .offset((page - 1) * size).limit(size)
    ).all()
    return {"success": True, "total": total, "items": [
        {"id": cache.id, "collection_id": str(favorite.collection_id) if favorite else "",
         "platform_item_id": cache.platform_item_id, "title": cache.title,
         "author": favorite.author if favorite else "",
         "duration": int(cache.analysis_duration_seconds or (favorite.duration if favorite else 0)),
         "url": f"https://www.douyin.com/video/{cache.platform_item_id}", "status": cache.status}
        for cache, favorite in rows
    ]}


class SyncRequest(BaseModel):
    # Omission/null keeps the existing all-pending action. An explicit empty
    # selection must never become an all-video job.
    selected_ids: list[str] | None = None
    knowledge_base_id: int | None = Field(default=None, gt=0)


class RetryTaskRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm_reprocess: StrictBool
    request_id: UUID


class ResumeDeletionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm: StrictBool


def recovery_failure(error):
    return {'success': False, 'code': error.code, 'message': error.message,
            'needs_review': error.needs_review, 'reset_count': error.reset_count, 'task_id': error.task_id}


def deletion_failure(error):
    return {'success': False, 'code': 'DELETION_INCOMPLETE', 'operation_id': error.operation_id,
            'reason_code': error.reason, 'status': 'deleting', 'message': error.message}

@router.post("/sync")
async def sync_knowledge(body: SyncRequest, db: Session = Depends(get_db)):
    """
    触发知识库入库任务

    默认提交全部 pending 视频；指定 selected_ids 时仅提交其中的
    pending/failed 视频。空选择与未知编号不会提交任务。

    :param body: 请求体，可指定 selected_ids 限定入库范围
    :param db: 数据库会话
    :return: 任务 ID 和待处理数量
    """
    try:
        result = knowledge_service.start_sync(db, body.selected_ids, body.knowledge_base_id)
    except KnowledgeRecoveryError as error:
        return recovery_failure(error)
    except WorkerBusyError:
        return {'success': False, 'code': 'INGESTION_BUSY', 'message': '有资料正在处理、排队或恢复，请先刷新原任务后再操作。'}
    except ValueError:
        return {'success': False, 'code': 'INGESTION_SELECTION_INVALID', 'message': '所选视频或目标知识库暂不可用，请刷新并核对范围后再提交。'}
    except Exception:
        return {'success': False, 'code': 'INGESTION_SUBMISSION_UNAVAILABLE', 'message': '入库任务尚未确认提交，请核对服务配置与原记录后重试。'}
    return {"success": True, **result}


@router.get("/sync/{task_id}")
async def get_sync_progress(task_id: str):
    """
    查询入库任务进度

    :param task_id: 任务 ID（由 /sync 返回）
    :return: 任务状态和进度
    """
    try:
        progress = knowledge_service.get_progress(task_id)
    except KnowledgeRecoveryError as error:
        return recovery_failure(error)
    except Exception:
        return {'success': False, 'code': 'INGESTION_PROGRESS_UNAVAILABLE', 'message': '任务状态暂时无法读取，请保留原记录并稍后刷新。'}
    if progress is None:
        return {
            "success": False,
            "message": f"任务不存在: {task_id}",
        }
    return {"success": True, **knowledge_service.public_task(progress)}


@router.get('/tasks')
def list_ingestion_tasks(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    try:
        return {'success': True, **knowledge_service.list_tasks(limit=limit, offset=offset)}
    except Exception:
        return {'success': False, 'code': 'INGESTION_HISTORY_UNAVAILABLE',
                'message': '入库任务记录暂时无法读取，原任务未被重新执行。请稍后刷新。'}


@router.get('/tasks/{task_id}/retry-result', dependencies=[Depends(local_request)])
def get_ingestion_retry_result(task_id: str, request_id: UUID):
    try:
        return {'success': True, **knowledge_service.recovery_result(task_id, request_id)}
    except KnowledgeRecoveryError as error:
        return recovery_failure(error)
    except Exception:
        return {'success': False, 'code': 'INGESTION_RETRY_LOOKUP_UNAVAILABLE',
                'message': '这次提交的结果暂不可读。请保留原请求，当前没有再次提交。'}


@router.post('/tasks/{task_id}/retry', dependencies=[Depends(local_request)])
def retry_ingestion_task(task_id: str, body: RetryTaskRequest, db: Session = Depends(get_db)):
    if body.confirm_reprocess is not True:
        return {'success': False, 'code': 'INGESTION_CONFIRMATION_REQUIRED', 'message': '请先确认重新处理可能再次产生费用。'}
    try:
        return {'success': True, **knowledge_service.retry_sync(db, task_id, body.request_id)}
    except KnowledgeRecoveryError as error:
        return recovery_failure(error)
    except WorkerBusyError:
        return {'success': False, 'code': 'INGESTION_BUSY', 'message': '有任务正在处理或排队，尚未重新提交。请先核对现有任务。'}
    except Exception:
        return {'success': False, 'code': 'INGESTION_RETRY_UNAVAILABLE',
                'message': '这次重新提交的结果尚未确认。请保留原请求并先核查任务记录。', 'needs_review': True}


def operation_view(operation):
    def stamp(value):
        return value.replace(tzinfo=timezone.utc).isoformat() if value is not None else None
    return {**describe_operation(operation), 'created_at': stamp(operation.created_at),
            'updated_at': stamp(operation.updated_at), 'can_resume': operation.active_key == 1}


@router.get('/deletions')
def list_deletions(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   active_only: bool = False, db: Session = Depends(get_db)):
    try:
        conditions = [KnowledgeDeletionOperation.active_key == 1] if active_only else []
        total = db.scalar(select(func.count()).select_from(KnowledgeDeletionOperation).where(*conditions)) or 0
        rows = db.scalars(select(KnowledgeDeletionOperation).where(*conditions)
            .order_by(KnowledgeDeletionOperation.updated_at.desc(), KnowledgeDeletionOperation.operation_id.desc())
            .offset(offset).limit(limit))
        return {'success': True, 'items': [operation_view(operation) for operation in rows],
                'total': total, 'limit': limit, 'offset': offset}
    except Exception:
        db.rollback()
        return {'success': False, 'code': 'DELETION_HISTORY_UNAVAILABLE', 'message': '删除记录暂时无法读取，请保留原资料并稍后刷新。'}


@router.post('/deletions/{operation_id}/resume', dependencies=[Depends(local_request)])
def resume_deletion(operation_id: UUID, body: ResumeDeletionRequest, db: Session = Depends(get_db)):
    if body.confirm is not True:
        return {'success': False, 'code': 'DELETION_CONFIRMATION_REQUIRED', 'message': '请先确认继续完成这一次删除。'}
    try:
        identity = str(operation_id)
        old = db.get(KnowledgeDeletionOperation, identity)
        if old is not None and old.state == 'done':
            return {'success': True, **knowledge_service.resume_deletion(db, identity)}
        with worker.maintenance_guard():
            try:
                result = knowledge_service.resume_deletion(db, identity)
            except BaseException:
                db.rollback()
                raise
        return {'success': True, **result}
    except DeletionIncomplete as error:
        return deletion_failure(error)
    except WorkerBusyError:
        db.rollback()
        return {'success': False, 'code': 'DELETION_BUSY', 'message': '有资料正在处理或排队，尚未继续删除。请等待任务结束后再核对。'}
    except Exception:
        return {'success': False, 'code': 'DELETION_RESUME_UNAVAILABLE', 'message': '删除结果尚未确认，请保留原操作记录并刷新核对。'}


@router.post("/reset-failed")
def reset_failed_videos(db: Session = Depends(get_db)):
    """
    将所有失败/卡住的视频重置为 pending

    处理场景：
    - 入库过程被中断（刷新页面、服务重启）
    - 视频转写失败需要重试

    :param db: 数据库会话
    :return: 重置数量
    """
    from sqlalchemy import update as sql_update
    reset_statuses = ["failed", "downloading", "transcribing", "embedding"]
    try:
        with worker.maintenance_guard():
            try:
                result = db.execute(
                    sql_update(VideoCache)
                    .where(VideoCache.status.in_(reset_statuses))
                    .values(status="pending", error_message="")
                )
                db.commit()
                count = result.rowcount
            except BaseException:
                db.rollback()
                raise
    except WorkerBusyError as error:
        db.rollback()
        return {"success": False, "message": str(error)}
    return {"success": True, "reset_count": count}


@router.delete("/videos/{platform_item_id}")
async def delete_video(platform_item_id: str, db: Session = Depends(get_db)):
    """
    删除已入库视频

    清理 ChromaDB 向量、重置为 pending 状态、删除音频缓存。
    不允许删除正在入库中的视频。

    :param platform_item_id: 抖音视频 ID
    :param db: 数据库会话
    :return: 删除结果
    """
    try:
        with worker.maintenance_guard():
            try:
                result = knowledge_service.delete_video(db, platform_item_id)
            except BaseException:
                db.rollback()
                raise
        return {"success": True, **result}
    except DeletionIncomplete as error:
        return deletion_failure(error)
    except WorkerBusyError:
        db.rollback()
        return {"success": False, "message": "有资料正在处理或排队，请等待入库任务结束后再删除。"}
    except ValueError as exc:
        return {"success": False, "code": "DELETION_SELECTION_INVALID", "message": "这份资料当前不能删除，请刷新核对原视频与处理状态。"}
    except Exception:
        return {'success': False, 'code': 'DELETION_UNAVAILABLE', 'message': '删除结果尚未确认，请保留原资料并刷新核对。'}


@router.get("/stats")
async def get_knowledge_stats(db: Session = Depends(get_db)):
    """
    获取知识库统计信息

    :param db: 数据库会话
    :return: VideoCache 各状态数量 + ChromaDB 存储统计
    """
    stats = knowledge_service.get_stats(db)
    return {"success": True, **stats, "capabilities": {"guarded_video_delete": True}}


@router.get("/videos/{platform_item_id}/content")
async def get_video_content(platform_item_id: str, db: Session = Depends(get_db)):
    cache = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == platform_item_id))
    if cache is None:
        return {"success": False, "message": "未找到这个视频，请先同步收藏夹。"}
    try:
        analysis = json.loads(cache.analysis_json or "{}")
        if not isinstance(analysis, dict):
            analysis = {}
    except (ValueError, TypeError):
        analysis = {}
    return {"success": True, "platform_item_id": cache.platform_item_id,
            "title": cache.title, "status": cache.status,
            "transcript_text": cache.transcript_text,
            "visual_analysis_text": cache.visual_analysis_text,
            "analysis_model": cache.analysis_model,
            "analysis_mode": analysis.get("analysis_mode", "audio_visual" if cache.visual_analysis_text else "audio"),
            "duration_seconds": cache.analysis_duration_seconds,
            "transcript_segments": analysis.get("transcript_segments", []),
            "visual_segments": analysis.get("visual_segments", []),
            "warnings": analysis.get("warnings", []), "error_message": cache.error_message,
            "audio_processing": analysis.get("audio_processing"),
            "processing_metrics": latest_ingestion_run(platform_item_id)}


@router.get("/export/{platform_item_id}", response_class=PlainTextResponse)
async def export_video_markdown(
    platform_item_id: str,
    mode: str = Query("original", pattern="^(original|ai)$"),
    db: Session = Depends(get_db),
):
    """
    导出视频内容为 Markdown

    两种模式：
    - original：原始 ASR 转写全文 + 元信息
    - ai：AI 结构化整理（摘要/观点/提纲/建议）+ 原始转写

    :param platform_item_id: 抖音视频 ID
    :param mode: 导出模式（original / ai）
    :param db: 数据库会话
    :return: Markdown 文本（Content-Type: text/plain; charset=utf-8）
    """
    cache = db.execute(
        select(VideoCache).where(VideoCache.platform_item_id == platform_item_id)
    ).scalar_one_or_none()

    if cache is None:
        return f"# 导出失败\n\n视频 `{platform_item_id}` 未找到，请先同步收藏夹。\n"
    if cache.status == 'deleting' and mode == 'ai':
        return JSONResponse(status_code=409, content={'success': False, 'code': 'DELETION_PENDING',
            'message': '这份资料的删除尚未完成，暂不调用 AI 整理。可导出原文核对，或先查看原删除记录。'})

    try:
        if mode == "original":
            from app.services.markdown_export import export_original
            md = export_original(cache)
        else:
            # Compatibility download only: paid generation is an explicit
            # UUID-bearing POST under /notes, never a GET side effect.
            from app.services.notes_service import NotesError, notes_service
            try:
                md = notes_service.latest_artifact(db, platform_item_id)
            except NotesError as error:
                return JSONResponse(status_code=error.status, content={
                    'success': False, 'code': error.code, 'message': error.message})
    except ValueError as exc:
        return f"# 导出失败\n\n{exc}\n"

    from fastapi.responses import Response
    return Response(
        content=md,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={platform_item_id}.md"
        },
    )
