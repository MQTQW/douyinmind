"""
ChromaDB 向量库操作模块

提供向量存储的增删查改操作：
- upsert：批量插入/更新视频文本块的向量
- search：向量语义检索
- delete：按视频 ID 删除
- count：统计存储量
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Iterable

from app.core.config import settings

logger = logging.getLogger(__name__)

# ChromaDB 相关配置常量
COLLECTION_NAME = "douyinrag_videos"
DISTANCE_METRIC = "cosine"


class ChromaService:
    """
    ChromaDB 向量存储服务

    封装 ChromaDB PersistentClient 的主要操作。
    """

    def __init__(self) -> None:
        """
        初始化 ChromaDB 客户端和集合

        :raises RuntimeError: Python 3.14 不兼容
        """
        if sys.version_info >= (3, 14):
            raise RuntimeError(
                "ChromaDB 当前不兼容 Python 3.14，请使用 Python 3.12"
            )

        import chromadb

        self._client = chromadb.PersistentClient(
            path=settings.chroma_persist_dir
        )
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": DISTANCE_METRIC},
        )
        logger.info(
            "ChromaDB 初始化完成: %s (collection=%s, count=%d)",
            settings.chroma_persist_dir,
            COLLECTION_NAME,
            self._collection.count(),
        )

    def upsert_video_chunks(
        self,
        platform_item_id: str,
        title: str,
        chunks: list[str],
        embeddings: list[list[float]],
    ) -> list[str]:
        """
        将一个视频的所有文本块写入向量库

        先删除该视频已有的旧数据，再批量插入新数据。

        :param platform_item_id: 抖音视频 ID
        :param title: 视频标题
        :param chunks: 文本块列表
        :param embeddings: 对应的向量列表（与 chunks 一一对应）
        :return: chunk ID 列表
        """
        if not chunks:
            return []

        # 先删旧数据
        self.delete_by_video(platform_item_id)

        now_ts = int(time.time())
        chunk_ids = [
            f"{platform_item_id}:{idx}" for idx in range(len(chunks))
        ]
        metadatas = [
            {
                "chunk_id": chunk_ids[idx],
                "platform_item_id": platform_item_id,
                "chunk_index": idx,
                "title": title[:500],
                "created_at": now_ts,
            }
            for idx in range(len(chunks))
        ]

        self._collection.upsert(
            ids=chunk_ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=chunks,
        )

        logger.info(
            "向量入库完成: %s, %d chunks", platform_item_id, len(chunks)
        )
        return chunk_ids

    def search(
        self, query_vector: list[float], top_k: int = 20,
        use_mmr: bool = True, fetch_k: int = 32, lambda_mult: float = 0.55,
        allowed_video_ids: set[str] | None = None,
    ) -> list[dict]:
        """
        向量语义检索（支持 MMR 多样性）

        :param query_vector: 查询向量
        :param top_k: 返回结果数量
        :param use_mmr: 是否启用 MMR 多样性
        :param fetch_k: MMR 候选池大小
        :param lambda_mult: MMR 相关性权重
        :return: 检索结果列表
        """
        if allowed_video_ids is not None and not allowed_video_ids:
            return []
        count = self._collection.count()
        if not count:
            return []
        n_fetch = max(fetch_k, top_k * 3)
        query_kwargs = dict(
            query_embeddings=[query_vector],
            n_results=min(n_fetch, count),
            include=["metadatas", "distances", "documents"],
        )
        if allowed_video_ids is not None:
            query_kwargs["where"] = {"platform_item_id": {"$in": sorted(allowed_video_ids)}}
        result = self._collection.query(**query_kwargs)

        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]

        candidates: list[dict] = []
        for idx, metadata in enumerate(metadatas):
            if not metadata:
                continue
            chunk_id = ids[idx] if idx < len(ids) else metadata.get("chunk_id")
            platform_item_id = metadata.get("platform_item_id")
            if not chunk_id or not platform_item_id:
                continue
            score = 1.0 - float(distances[idx] if idx < len(distances) else 0.0)
            title = metadata.get("title", "")
            text = documents[idx] if idx < len(documents) else ""
            candidates.append({
                "chunk_id": str(chunk_id),
                "platform_item_id": str(platform_item_id),
                "title": str(title),
                "score": score,
                "text": str(text),
                "chunk_index": metadata.get("chunk_index"),
            })

        if not candidates or not use_mmr or len(candidates) <= top_k:
            return candidates[:top_k]

        # MMR 迭代选择
        selected: list[dict] = [max(candidates, key=lambda c: c["score"])]
        remaining = [c for c in candidates if c != selected[0]]

        for _ in range(1, top_k):
            if not remaining:
                break
            best_score = -float("inf")
            best_idx = 0
            for i, c in enumerate(remaining):
                relevance = c["score"]
                max_sim = 0.0
                for s in selected:
                    c_set = set(c["text"][:200])
                    s_set = set(s["text"][:200])
                    if c_set or s_set:
                        inter = len(c_set & s_set)
                        union = len(c_set | s_set)
                        max_sim = max(max_sim, inter / union if union > 0 else 0)
                mmr = lambda_mult * relevance - (1.0 - lambda_mult) * max_sim
                if mmr > best_score:
                    best_score = mmr
                    best_idx = i
            selected.append(remaining.pop(best_idx))

        return selected

    def get_documents(self, allowed_video_ids: set[str] | None = None) -> list[dict]:
        """Read original text for lexical retrieval and neighboring sentences.

        Apply the same scope inside the index read, not after retrieving global
        results. No embeddings are created or returned by this read-only path.
        """
        if allowed_video_ids is not None and not allowed_video_ids:
            return []
        documents = []
        offset = 0
        batch_size = 500
        while True:
            kwargs = {"include": ["metadatas", "documents"], "limit": batch_size, "offset": offset}
            if allowed_video_ids is not None:
                kwargs["where"] = {"platform_item_id": {"$in": sorted(allowed_video_ids)}}
            result = self._collection.get(**kwargs)
            identifiers = result.get("ids") or []
            metadatas = result.get("metadatas") or []
            texts = result.get("documents") or []
            for index, identifier in enumerate(identifiers):
                metadata = metadatas[index] if index < len(metadatas) else None
                if not metadata or not metadata.get("platform_item_id") or index >= len(texts):
                    continue
                video_id = str(metadata["platform_item_id"])
                if allowed_video_ids is not None and video_id not in allowed_video_ids:
                    continue
                documents.append({"chunk_id": str(identifier), "platform_item_id": video_id,
                                  "chunk_index": metadata.get("chunk_index"),
                                  "title": str(metadata.get("title", "")),
                                  "text": str(texts[index] or ""), "score": 0.0})
            if len(identifiers) < batch_size:
                break
            offset += len(identifiers)
        return documents

    def delete_by_video(self, platform_item_id: str) -> None:
        """
        删除指定视频的所有向量数据

        :param platform_item_id: 抖音视频 ID
        """
        self._collection.delete(
            where={"platform_item_id": platform_item_id}
        )

    def delete_videos(self, platform_item_ids: Iterable[str]) -> None:
        """
        批量删除多个视频的向量数据

        :param platform_item_ids: 抖音视频 ID 列表
        """
        ids = [item for item in platform_item_ids if item]
        if not ids:
            return
        self._collection.delete(
            where={"platform_item_id": {"$in": ids}}
        )

    def count(self) -> int:
        """
        获取向量库中存储的文本块总数

        :return: chunk 数量
        """
        return int(self._collection.count())

    def get_video_count(self) -> int:
        """
        获取向量库中已入库的视频数量（按 platform_item_id 去重）

        :return: 唯一视频数量
        """
        result = self._collection.get(include=["metadatas"])
        ids_set = set()
        for meta in result.get("metadatas", []):
            if meta and "platform_item_id" in meta:
                ids_set.add(meta["platform_item_id"])
        return len(ids_set)


# 全局单例
_chroma_service: ChromaService | None = None


def get_chroma_service() -> ChromaService:
    """
    获取 ChromaService 全局单例

    :return: ChromaService 实例
    """
    global _chroma_service
    if _chroma_service is None:
        _chroma_service = ChromaService()
    return _chroma_service
