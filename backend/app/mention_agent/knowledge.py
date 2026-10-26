"""Evidence-only access to one trusted library or the owner's all-local scope.

The application SQLite database is opened with ``mode=ro`` and a private
SQLAlchemy session.  No chat history, chat write, ingestion or second answer
generation is used.  Production retrieval still uses the existing query
embedding provider and Chroma index: it can make a paid embedding request, and
Chroma's first initialization may create internal persistent files/collections.
This module does not promise that every file on the computer remains untouched.

The caller must bind this local instance/library to the authenticated owner.
An existing library ID is not itself a multi-user authorization credential.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
from threading import Lock
from typing import Iterator

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.services.knowledge_base_service import KnowledgeBaseNotFound
from app.services.retrieval_context import select_context_hits


class LocalKnowledgeTools:
    """Return bounded source excerpts, never generate or publish an answer.

    ``source_id`` identifies a distinct (video, chunk) pair. Labels are assigned
    in first-seen order and remain stable for this tool instance, including
    repeated tool calls in one model conversation. They are not durable IDs.
    """

    MAX_EVIDENCE = 8

    def __init__(self, database: Path, knowledge_base_id: int | None, *, rag=None, focus_video_id=None,
                 query_scope="library") -> None:
        if (not isinstance(query_scope,str) or query_scope not in {'library','all_local'}
                or (query_scope == 'all_local' and knowledge_base_id is not None)
                or (query_scope == 'library' and (type(knowledge_base_id) is not int or knowledge_base_id <= 0))
                ):
            raise ValueError("请选择一个有效的知识库。")
        self.database = Path(database).expanduser().resolve()
        self.knowledge_base_id = knowledge_base_id
        self.query_scope = query_scope
        self._rag = rag
        self._source_ids: dict[tuple[str, str], str] = {}
        self._source_lock = Lock()
        if focus_video_id is not None and (not isinstance(focus_video_id, str) or not re.fullmatch(r"\d{5,30}", focus_video_id)):
            raise ValueError("当前视频范围无效")
        self.focus_video_id = focus_video_id

    @contextmanager
    def _read_session(self) -> Iterator[Session]:
        def connect():
            connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
            connection.execute("PRAGMA query_only=ON")
            return connection

        engine = create_engine("sqlite://", creator=connect, poolclass=NullPool)
        try:
            with Session(engine, autoflush=False) as db:
                yield db
        finally:
            engine.dispose()

    def _snapshot(self, db: Session) -> tuple[dict, set[str]]:
        if self.query_scope == 'all_local':
            # Matches the page's saved-material inventory, including done
            # videos not associated with any KnowledgeBaseVideo row. The caller
            # must prove this instance belongs to this owner before using it.
            scope = set(db.scalars(select(VideoCache.platform_item_id).where(VideoCache.status == 'done')).all())
            return {'id':None,'name':'全部入库资料','total_count':len(scope),'ready_count':len(scope),
                    'query_scope':'all_local'}, scope
        base = db.get(KnowledgeBase, self.knowledge_base_id)
        if base is None:
            raise KnowledgeBaseNotFound("这个知识库不存在，请刷新后重新选择。")
        total = db.scalar(
            select(func.count()).select_from(KnowledgeBaseVideo).where(
                KnowledgeBaseVideo.knowledge_base_id == self.knowledge_base_id
            )
        )
        scope = set(db.scalars(
            select(KnowledgeBaseVideo.platform_item_id).join(
                VideoCache, VideoCache.platform_item_id == KnowledgeBaseVideo.platform_item_id
            ).where(
                KnowledgeBaseVideo.knowledge_base_id == self.knowledge_base_id,
                VideoCache.status == "done",
            )
        ).all())
        return {
            "id": base.id,
            "name": base.name,
            "total_count": int(total or 0),
            "ready_count": len(scope),
        }, scope

    def describe(self) -> dict:
        """Describe only the fixed library, without listing other library names."""
        with self._read_session() as db:
            description, _ = self._snapshot(db)
        return description

    @staticmethod
    def _scoped_hits(hits, scope: set[str]) -> list[dict]:
        return [dict(hit) for hit in hits if isinstance(hit, dict)
                and isinstance(hit.get("platform_item_id"), str)
                and hit["platform_item_id"] in scope
                and isinstance(hit.get("chunk_id"), str) and hit["chunk_id"].strip()
                and isinstance(hit.get("text"), str) and hit["text"].strip()
                and isinstance(hit.get("title"), str)]

    def _label(self, video_id: str, chunk_id: str) -> str:
        with self._source_lock:
            identity = (video_id, chunk_id)
            if identity not in self._source_ids:
                self._source_ids[identity] = f"资料{len(self._source_ids) + 1}"
            return self._source_ids[identity]

    def query(self, question: str) -> dict:
        """Retrieve scoped original evidence; errors remain errors, not empty hits."""
        if not isinstance(question, str) or not question.strip():
            raise ValueError("请输入要在这个知识库中查找的问题。")
        with self._read_session() as db:
            description, scope = self._snapshot(db)
        if self.focus_video_id is not None:
            scope.intersection_update({self.focus_video_id})
        result = {
            "status": "empty",
            "knowledge_base_id": self.knowledge_base_id,
            "knowledge_base_name": description["name"],
            "evidence": [],
            "retrieval_mode": "empty_scope",
        }
        if self.query_scope == 'all_local':
            result['query_scope'] = 'all_local'
        # Never turn an empty library into None (the existing global-scope flag).
        if not scope:
            return result
        if settings.rag_prompt_max_context_chars <= 0:
            raise ValueError("知识库工具的正文长度上限必须大于零。")

        from app.services.rag_service import RagService

        if self._rag is None:
            self._rag = RagService()
        hits = self._rag._dense_retrieve(question.strip(), scope)
        mode = getattr(hits, "mode", "semantic")
        scoped = self._scoped_hits(hits, scope)
        selected = self._rag._context_hits(scoped, scope)
        # Recheck after the injected/reused retriever. Preserve the existing
        # source-text, deduplication and sentence-boundary budget rules too.
        selected = select_context_hits(
            self._scoped_hits(selected, scope),
            max_count=min(self.MAX_EVIDENCE, settings.rag_context_count),
            max_chars=settings.rag_prompt_max_context_chars,
            format_blocks=RagService._format_context_blocks,
            has_source_text=RagService._has_source_text,
            scope_ids=scope,
        )
        evidence = []
        for hit in selected:
            video_id, chunk_id = hit["platform_item_id"], hit["chunk_id"]
            item = {
                "source_id": self._label(video_id, chunk_id),
                "platform_item_id": video_id,
                "chunk_id": chunk_id,
                "title": hit.get("title", ""),
                "text": hit["text"],
                "url": f"https://www.douyin.com/video/{video_id}",
            }
            # Only expose timestamps actually present in stored source text.
            # They locate an excerpt, rather than inventing a precise citation.
            times = re.findall(r"(?<!\d)(\d{2,3}:\d{2}(?::\d{2})?)\s*[–—~\-]\s*(\d{2,3}:\d{2}(?::\d{2})?)(?!\d)", hit["text"])
            if times:
                item["time_range"] = times[0][0] + "–" + times[-1][1]
            source_type = hit.get("source_type")
            if isinstance(source_type, str) and source_type:
                item["source_type"] = source_type
            elif hit["text"].startswith("语音文字稿"):
                item["source_type"] = "speech"
            elif hit["text"].startswith("画面分析"):
                item["source_type"] = "visual"
            evidence.append(item)
        result.update(status="ok" if evidence else "no_evidence", evidence=evidence,
                      retrieval_mode=mode if isinstance(mode, str) else "semantic")
        return result
