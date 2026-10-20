"""
知识库路由模块

提供收藏夹一键入库、入库进度查询、知识库统计、视频内容导出等接口。
"""
import logging
import json
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.entities import FavoriteVideo, VideoCache
from app.services.knowledge_service import knowledge_service
from app.services.markdown_export import export_ai_organized, export_original
from app.services.worker import WorkerBusyError, worker
from app.services.ingestion_metrics import latest_ingestion_run

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
    except (RuntimeError, ValueError) as exc:
        db.rollback()
        return {"success": False, "message": str(exc)}
    return {"success": True, **result}


@router.get("/sync/{task_id}")
async def get_sync_progress(task_id: str):
    """
    查询入库任务进度

    :param task_id: 任务 ID（由 /sync 返回）
    :return: 任务状态和进度
    """
    progress = knowledge_service.get_progress(task_id)
    if progress is None:
        return {
            "success": False,
            "message": f"任务不存在: {task_id}",
        }
    return {"success": True, **progress}


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
        result = knowledge_service.delete_video(db, platform_item_id)
        return {"success": True, **result}
    except ValueError as exc:
        return {"success": False, "message": str(exc)}


@router.get("/stats")
async def get_knowledge_stats(db: Session = Depends(get_db)):
    """
    获取知识库统计信息

    :param db: 数据库会话
    :return: VideoCache 各状态数量 + ChromaDB 存储统计
    """
    stats = knowledge_service.get_stats(db)
    return {"success": True, **stats}


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

    try:
        if mode == "original":
            md = export_original(cache)
        else:
            md = export_ai_organized(cache)
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
