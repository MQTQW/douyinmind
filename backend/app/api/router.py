"""
API 路由聚合模块

将各子路由（auth / favorites / knowledge / chat）注册到统一前缀 /api 下。
"""
from fastapi import APIRouter

from app.api.routes import auth, chat, favorites, knowledge, knowledge_bases, mention_ingestion
from app.assistant.routes import router as assistant_router
from app.assistant.registry_routes import router as assistant_registry_router
from app.assistant.evidence_routes import router as assistant_evidence_router
from app.assistant.usage_routes import router as assistant_usage_router

api_router = APIRouter(prefix="/api")

api_router.include_router(auth.router)
api_router.include_router(favorites.router)
api_router.include_router(knowledge.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(chat.router)
api_router.include_router(mention_ingestion.router)
api_router.include_router(assistant_router)
api_router.include_router(assistant_registry_router)
api_router.include_router(assistant_evidence_router)
api_router.include_router(assistant_usage_router)
