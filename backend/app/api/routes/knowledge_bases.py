"""Manage user-created knowledge bases; removal affects membership only."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.knowledge_base_service import (
    DuplicateKnowledgeBaseName, FavoriteCollectionNotFound, KnowledgeBaseNotFound, knowledge_base_service,
)

router = APIRouter(prefix="/knowledge-bases", tags=["自建知识库"])


class KnowledgeBaseName(BaseModel):
    name: str


class CollectionKnowledgeBase(BaseModel):
    collection_id: str


def _error(exc: ValueError) -> HTTPException:
    code = 404 if isinstance(exc, (KnowledgeBaseNotFound, FavoriteCollectionNotFound)) else 409 if isinstance(exc, DuplicateKnowledgeBaseName) else 400
    return HTTPException(status_code=code, detail=str(exc))


@router.get("")
async def list_knowledge_bases(db: Session = Depends(get_db)):
    items = knowledge_base_service.list(db)
    return {"success": True, "items": items, "total": len(items)}


@router.post("", status_code=201)
async def create_knowledge_base(body: KnowledgeBaseName, db: Session = Depends(get_db)):
    try:
        item = knowledge_base_service.create(db, body.name)
        db.commit()
        return {"success": True, "item": item}
    except ValueError as exc:
        raise _error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="已存在同名知识库，请换个名称。") from exc


@router.post("/from-collection")
async def create_collection_knowledge_base(body: CollectionKnowledgeBase, db: Session = Depends(get_db)):
    try:
        item, reused = knowledge_base_service.from_collection(db, body.collection_id)
        db.commit()
        return {"success": True, "item": item, "reused": reused}
    except ValueError as exc:
        db.rollback()
        raise _error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="知识库创建冲突，请重试。") from exc


@router.patch("/{knowledge_base_id}")
async def rename_knowledge_base(knowledge_base_id: int, body: KnowledgeBaseName, db: Session = Depends(get_db)):
    try:
        item = knowledge_base_service.rename(db, knowledge_base_id, body.name)
        db.commit()
        return {"success": True, "item": item}
    except ValueError as exc:
        raise _error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="已存在同名知识库，请换个名称。") from exc


@router.get("/{knowledge_base_id}/videos")
async def list_knowledge_base_videos(knowledge_base_id: int, page: int = Query(1, ge=1),
                                   size: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    try:
        items, total = knowledge_base_service.list_videos(db, knowledge_base_id, page, size)
    except ValueError as exc:
        raise _error(exc) from exc
    return {"success": True, "items": items, "total": total, "page": page, "size": size}


@router.delete("/{knowledge_base_id}/videos/{platform_item_id}")
async def remove_knowledge_base_video(knowledge_base_id: int, platform_item_id: str,
                                      db: Session = Depends(get_db)):
    try:
        count = knowledge_base_service.remove_video(db, knowledge_base_id, platform_item_id)
        db.commit()
    except ValueError as exc:
        raise _error(exc) from exc
    return {"success": True, "removed_count": count}
