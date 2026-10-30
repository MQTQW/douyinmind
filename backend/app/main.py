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
from app.db.migrations import ensure_chat_knowledge_base_column, ensure_knowledge_base_tombstones, ensure_video_analysis_columns


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
    ensure_knowledge_base_tombstones(engine)
    logger.info("数据库表初始化完成")

    # 启动后台 worker
    from app.services.worker import worker
    worker.start()
    logger.info("后台工作线程已启动")

    yield

    # === 关闭 ===
    logger.info("正在关闭 DouyinRAG 后端服务...")
    worker.stop()
    engine.dispose()
    logger.info("数据库连接已释放")


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
        "status": "ok",
        "service": "DouyinRAG",
        "version": "0.1.0",
    }
