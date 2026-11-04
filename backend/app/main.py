"""
FastAPI 应用入口

启动命令：
    uv run --project backend uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --app-dir backend
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.base import Base
from app.db.session import engine
from app.db.migrations import ensure_chat_knowledge_base_column, ensure_chat_mode_column, ensure_knowledge_base_tombstones, ensure_video_analysis_columns
from app.core.runtime_identity import capture_runtime_identity, RuntimeIdentityError


try:
    _RUNTIME_IDENTITY = capture_runtime_identity()
except RuntimeIdentityError:
    _RUNTIME_IDENTITY = {}  # An unidentified server must never be silently reused.


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 应用生命周期管理

    启动时：
    - 初始化日志系统
    - 创建数据库表
    - 创建存储目录

    关闭时：
    - 释放数据库连接
    """
    # === 启动 ===
    setup_logging()
    logger.info("正在启动 DouyinRAG 后端服务...")

    # 创建存储目录
    storage_dirs = [
        settings.chroma_persist_dir,
        settings.audio_cache_dir,
        settings.video_cache_dir,
        settings.playwright_user_data_dir,
        settings.playwright_browsers_path,
        "logs",
    ]
    for dir_path in storage_dirs:
        os.makedirs(dir_path, exist_ok=True)
    logger.info("存储目录初始化完成")

    # 创建数据库表（同步引擎）
    Base.metadata.create_all(bind=engine)
    ensure_video_analysis_columns(engine)
    ensure_chat_knowledge_base_column(engine)
    ensure_chat_mode_column(engine)
    ensure_knowledge_base_tombstones(engine)
    logger.info("数据库表初始化完成")

    # 启动后台 worker
    from app.services.worker import worker
    from app.db.session import session_factory
    from app.services.notes_store import recover_incomplete_notes
    worker.start()
    # Only the confirmed cross-process worker owner may quarantine old notes.
    # The HTTP app has not yielded and the worker never reconstructs callbacks.
    try:
        with worker.maintenance_guard(), session_factory() as notes_db:
            recover_incomplete_notes(notes_db)
    except BaseException:
        worker.stop(timeout=30.0)
        raise
    logger.info("后台工作线程已启动")

    try:
        yield
    finally:
        # Keep the event loop free while the current synchronous job finishes.
        # A timeout is not completion and cannot authorize a replay on restart.
        from asyncio import to_thread
        logger.info("正在关闭 DouyinRAG 后端服务...")
        try:
            stopped = await to_thread(worker.stop, timeout=30.0)
        except Exception:
            logger.warning("入库线程的退出结果未确认，保留任务与业务数据库连接，请核对原记录。")
        else:
            confirmed = (isinstance(stopped, dict) and stopped.get("stopped") is True
                         and stopped.get("timed_out") is not True)
            if not confirmed:
                logger.warning("入库线程尚未确认停止，保留任务与业务数据库连接；处理结果仍需核对。")
            else:
                if stopped.get("state_recorded") is False:
                    logger.warning("入库线程已停止，但任务状态写入未确认；保留原记录供后续核对。")
                try:
                    engine.dispose()
                except Exception:
                    logger.warning("入库线程已停止，但数据库连接释放结果未确认，请核对本机状态。")
                else:
                    logger.info("入库线程已停止，数据库连接已释放")


# 创建 FastAPI 应用
app = FastAPI(
    title="DouyinRAG",
    description="抖音收藏夹 RAG 知识库 API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS 中间件（允许前端开发服务器跨域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(api_router)


@app.get("/")
async def root():
    """
    根路径健康检查

    :return: 服务状态信息
    """
    return {
        "status": "ok" if _RUNTIME_IDENTITY else "identity_unavailable",
        "service": "DouyinRAG",
        "version": "0.1.0",
        **_RUNTIME_IDENTITY,
    }
