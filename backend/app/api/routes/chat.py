"""
对话路由模块

提供 RAG 问答（非流式 + SSE 流式）和会话管理接口。
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.db.session import get_db, session_factory
from app.models.entities import ChatSession
from app.services.rag_service import rag_service
from app.services.chat_errors import logged_chat_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["对话"])


class AskRequest(BaseModel):
    """
    问答请求体

    :field query: 用户问题
    :field session_id: 会话 ID（可选，不传则自动创建新会话）
    :field collection_id: 限定检索的收藏夹 ID（可选，"all" 或空表示全库检索）
    """
    query: str = Field(min_length=1, max_length=2000)
    session_id: int | None = Field(default=None, gt=0)
    collection_id: str | None = None
    knowledge_base_id: int | None = Field(default=None, gt=0)

    @field_validator("query", mode="before")
    @classmethod
    def strip_query(cls, value):
        return value.strip() if isinstance(value, str) else value


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=128)

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, value):
        return value.strip() if isinstance(value, str) else value


# ------------------------------------------------------------------
# 非流式问答
# ------------------------------------------------------------------

@router.post("/ask")
def chat_ask(
    body: AskRequest, db: Session = Depends(get_db)
):
    """
    非流式 RAG 问答

    提交问题后等待完整回答返回。

    :param body: 问答请求
    :param db: 数据库会话
    :return: 完整回答 + 来源信息 + 会话 ID
    """
    if body.session_id is not None and db.get(ChatSession, body.session_id) is None:
        raise HTTPException(status_code=404, detail="该历史对话已不存在，请新建对话。")
    try:
        kwargs = {"knowledge_base_id": body.knowledge_base_id} if body.knowledge_base_id is not None else {}
        result = rag_service.answer(db, body.query, body.session_id, body.collection_id, **kwargs)
        return {"success": True, **result}
    except Exception as exc:
        return {
            "success": False,
            **logged_chat_error(logger, exc),
        }


# ------------------------------------------------------------------
# SSE 流式问答
# ------------------------------------------------------------------

@router.post("/ask/stream")
async def chat_ask_stream(
    body: AskRequest,
):
    """
    SSE 流式 RAG 问答

    事件类型：
    - sources: 检索到的视频来源列表
    - delta:   LLM 逐 token 输出
    - meta:    最终回答和元信息（answer, session_id, route_type, latency_ms, sources, trace）
    - done:    流结束标记
    - error:   错误信息

    :param body: 问答请求
    :return: SSE 事件流
    """

    # A sync iterator runs in Starlette's thread pool so blocking LLM calls
    # do not block the event loop or delay delivery of streamed chunks.
    def event_stream():
        try:
            # Keep this session alive for the entire response, independent of
            # request dependency cleanup and FastAPI version differences.
            with session_factory() as db:
                kwargs = {"knowledge_base_id": body.knowledge_base_id} if body.knowledge_base_id is not None else {}
                for event_name, payload in rag_service.answer_stream(
                    db, body.query, body.session_id, body.collection_id, **kwargs
                ):
                    data = json.dumps(payload, ensure_ascii=False)
                    yield f"event: {event_name}\ndata: {data}\n\n"
        except Exception as exc:
            error_data = json.dumps(
                logged_chat_error(logger, exc), ensure_ascii=False
            )
            yield f"event: error\ndata: {error_data}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------
# 会话管理
# ------------------------------------------------------------------

@router.get("/sessions")
async def list_sessions(
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    获取对话会话列表

    按最后消息时间降序排列。

    :param limit: 最大返回数量
    :param db: 数据库会话
    :return: 会话列表
    """
    items = rag_service.list_sessions(db, limit=limit)
    return {"success": True, "items": items, "total": len(items)}


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: int, db: Session = Depends(get_db)
):
    """
    获取指定会话的消息历史

    :param session_id: 会话 ID
    :param db: 数据库会话
    :return: 消息列表
    """
    messages = rag_service.get_messages(db, session_id)
    if messages is None:
        raise HTTPException(status_code=404, detail="该历史对话已不存在。")
    return {
        "success": True,
        "session_id": session_id,
        "title": db.get(ChatSession, session_id).title,
        "knowledge_base_id": db.get(ChatSession, session_id).knowledge_base_id,
        "items": messages,
    }


@router.patch("/sessions/{session_id}")
async def rename_session(
    session_id: int, body: RenameSessionRequest, db: Session = Depends(get_db)
):
    """更改历史对话标题，保留所有消息。"""
    item = rag_service.rename_session(db, session_id, body.title)
    if item is None:
        raise HTTPException(status_code=404, detail="该历史对话已不存在。")
    return {"success": True, "session_id": item["id"], "title": item["title"]}


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: int, db: Session = Depends(get_db)
):
    """
    删除指定会话及其所有消息

    :param session_id: 会话 ID
    :param db: 数据库会话
    :return: 是否删除成功
    """
    ok = rag_service.delete_session(db, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="该历史对话已不存在。")
    return {"success": ok, "session_id": session_id}
