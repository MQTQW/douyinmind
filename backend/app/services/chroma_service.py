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
from copy import deepcopy
import hashlib
import json
import math
from numbers import Real
import re
import sys
import threading
import time
from typing import Iterable

from app.core.config import settings

logger = logging.getLogger(__name__)

# ChromaDB 相关配置常量
COLLECTION_NAME = "douyinrag_videos"
DISTANCE_METRIC = "cosine"
_MUTATION_LOCK = threading.RLock()


class IndexAuditUnavailable(RuntimeError):
    REASONS = frozenset({'index_read_unavailable', 'index_metadata_invalid',
                         'index_snapshot_changed', 'index_scan_limit', 'index_text_read_unavailable'})

    def __init__(self, reason='index_read_unavailable'):
        self.reason = reason if reason in self.REASONS else 'index_read_unavailable'
        super().__init__('索引核查未能完整读取，未把未知内容或读取失败记为零。')


def _content_role(text, title):
    """New-writer evidence marker only; never infer body from chunk ordinal."""
    body = text.strip()
    header = re.match(r'^(?:语音文字稿(?:（来自视频声音）)?|画面分析(?:（来自视频画面）)?)[：:]\s*\n([\s\S]*)$', body)
    if header:
        speech = re.sub(r'\[?\d{1,3}:\d{2}(?::\d{2})?(?:\.\d+)?[–—~\-\d:. ]*\]?', '', header[1])
        return 'source' if any(character.isalnum() for character in speech) else 'unverified'
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if lines and all(line.startswith(('视频标题：', '视频标题:', '作者：', '作者:')) for line in lines):
        return 'metadata'
    for prefix in ('视频标题：', '视频标题:'):
        if body.startswith(prefix):
            declared, separator, author = body[len(prefix):].rpartition('\n作者：')
            if not separator:
                declared = body[len(prefix):]
            if declared.strip() == title.strip() and (not separator or '\n' not in author.strip()):
                return 'metadata'
    return 'unverified'


def _vectors(values, expected):
    if not isinstance(values, (list, tuple)) or len(values) != expected:
        raise ValueError('invalid_vector_batch')
    normalized, dimension = [], None
    for vector in values:
        try:
            if isinstance(vector, (str, bytes, dict)):
                raise ValueError()
            row = list(vector)
            if not row or any(not isinstance(value, Real) or isinstance(value, bool)
                              or not math.isfinite(value) for value in row):
                raise ValueError()
            if dimension is not None and len(row) != dimension:
                raise ValueError()
            dimension = len(row)
            normalized.append([float(value) for value in row])
        except (TypeError, ValueError, OverflowError):
            raise ValueError('invalid_vector_batch') from None
    return normalized


def _snapshot(collection, video_id, *, ids=None):
    arguments = {'include': ['metadatas', 'documents', 'embeddings']}
    if ids is None:
        arguments['where'] = {'platform_item_id': video_id}
    else:
        arguments['ids'] = ids
    value = collection.get(**arguments)
    identifiers = list(value.get('ids')) if value.get('ids') is not None else []
    if not identifiers:
        if any(value.get(key) is not None and len(value[key]) for key in ('metadatas', 'documents', 'embeddings')):
            raise ValueError('invalid_vector_snapshot')
        return {}
    metadata = list(value.get('metadatas')) if value.get('metadatas') is not None else []
    documents = list(value.get('documents')) if value.get('documents') is not None else []
    embeddings = list(value.get('embeddings')) if value.get('embeddings') is not None else []
    if (len(set(identifiers)) != len(identifiers) or any(type(key) is not str or not key for key in identifiers)
            or ids is not None and not set(identifiers).issubset(ids)
            or len(metadata) != len(identifiers) or len(documents) != len(identifiers)
            or any(type(item) is not dict or item.get('platform_item_id') != video_id for item in metadata)
            or any(type(document) is not str for document in documents)):
        raise ValueError('invalid_vector_snapshot')
    embeddings = _vectors(embeddings, len(identifiers))
    return {key: {'metadata': deepcopy(meta), 'document': document, 'embedding': vector}
            for key, meta, document, vector in zip(identifiers, metadata, documents, embeddings)}


def _write_snapshot(collection, rows):
    if rows:
        collection.upsert(ids=list(rows), embeddings=[row['embedding'] for row in rows.values()],
                          metadatas=[row['metadata'] for row in rows.values()],
                          documents=[row['document'] for row in rows.values()])


def _same_snapshot(actual, expected):
    if set(actual) != set(expected):
        return False
    for key, row in expected.items():
        got = actual[key]
        if got['metadata'] != row['metadata'] or got['document'] != row['document']:
            return False
        if len(got['embedding']) != len(row['embedding']) or not all(
                math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-8)
                for a, b in zip(got['embedding'], row['embedding'])):
            return False
    return True


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
        from chromadb.config import Settings as ChromaSettings

        self._client = chromadb.PersistentClient(
            path=settings.chroma_persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": DISTANCE_METRIC},
            embedding_function=None,
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

        先校验并保存旧快照，再写新块；只有写入成功才删除旧的多余块。
        失败时尝试恢复旧快照，无法确认补偿结果时明确抛错。

        :param platform_item_id: 抖音视频 ID
        :param title: 视频标题
        :param chunks: 文本块列表
        :param embeddings: 对应的向量列表（与 chunks 一一对应）
        :return: chunk ID 列表
        """
        if (type(platform_item_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', platform_item_id)
                or type(title) is not str or not isinstance(chunks, list)
                or any(type(text) is not str or not text.strip() for text in chunks)):
            raise ValueError('invalid_video_chunks')
        vectors = _vectors(embeddings, len(chunks))
        if not chunks:
            return []
        with _MUTATION_LOCK:
            now_ts = int(time.time())
            chunk_ids = [f'{platform_item_id}:{index}' for index in range(len(chunks))]
            replacement = {key: {'embedding': vectors[index], 'document': chunks[index],
                'metadata': {'chunk_id': key, 'platform_item_id': platform_item_id,
                             'chunk_index': index, 'title': title[:500], 'created_at': now_ts,
                             'dm_content_role_version': 1, 'dm_content_role': _content_role(chunks[index], title)}}
                for index, key in enumerate(chunk_ids)}
            try:
                previous = _snapshot(self._collection, platform_item_id)
                # Even a corrupt foreign row cannot be overwritten through a
                # generated ID collision and then hidden by compensation.
                _snapshot(self._collection, platform_item_id, ids=chunk_ids)
                if previous and len(next(iter(previous.values()))['embedding']) != len(vectors[0]):
                    raise ValueError('vector_dimension_changed')
            except Exception:
                raise RuntimeError('无法完整读取旧索引快照，未写入或删除任何向量。') from None
            try:
                _write_snapshot(self._collection, replacement)
                stale = sorted(set(previous) - set(replacement))
                if stale:
                    self._collection.delete(ids=stale)
                if not _same_snapshot(_snapshot(self._collection, platform_item_id), replacement):
                    raise ValueError('vector_write_not_confirmed')
            except Exception:
                try:
                    _write_snapshot(self._collection, previous)
                    added = sorted(set(replacement) - set(previous))
                    if added:
                        self._collection.delete(ids=added)
                    if not _same_snapshot(_snapshot(self._collection, platform_item_id), previous):
                        raise ValueError('vector_restore_not_confirmed')
                except Exception:
                    raise RuntimeError('向量更新失败，旧索引恢复结果无法确认；原文保留，未标记入库成功。') from None
                raise RuntimeError('向量更新失败，已核对恢复旧索引；原文保留，未标记入库成功。') from None
            logger.info('向量入库完成: %s, %d chunks', platform_item_id, len(chunks))
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
        with _MUTATION_LOCK:
            self._collection.delete(where={"platform_item_id": platform_item_id})

    def list_indexed_video_counts(self, page_size=500, *, verify_legacy_text=False, max_records=1_000_000):
        """Read a bounded metadata census without embeddings or model calls.

        Legacy ordinals are not content evidence. Only an explicit opt-in reads
        local documents to classify legacy text with the existing source rule.
        Hashes cover the observed identities/roles (and opted-in text hashes),
        not a cross-storage atomic snapshot or a claim of answer correctness.
        """
        if (type(page_size) is not int or not 1 <= page_size <= 1000
                or type(verify_legacy_text) is not bool or type(max_records) is not int or max_records < 1):
            raise ValueError('invalid_index_audit_options')
        try:
            before = self._collection.count()
            if type(before) is not int or before < 0:
                raise IndexAuditUnavailable('index_metadata_invalid')
            if before > max_records:
                raise IndexAuditUnavailable('index_scan_limit')
            counts, seen, records = {}, set(), []
            offset = 0
            include = ['metadatas', 'documents'] if verify_legacy_text else ['metadatas']
            while offset < before:
                result = self._collection.get(limit=page_size, offset=offset, include=include)
                identifiers, metadata = result.get('ids'), result.get('metadatas')
                if (not isinstance(identifiers, list) or not isinstance(metadata, list)
                        or not identifiers or len(identifiers) > page_size or len(metadata) != len(identifiers)):
                    raise IndexAuditUnavailable('index_metadata_invalid')
                documents = result.get('documents') if verify_legacy_text else None
                if verify_legacy_text and (not isinstance(documents, list) or len(documents) != len(identifiers)
                                           or any(type(document) is not str for document in documents)):
                    raise IndexAuditUnavailable('index_text_read_unavailable')
                for index, (identifier, meta) in enumerate(zip(identifiers, metadata)):
                    if type(identifier) is not str or not identifier or len(identifier) > 512 or type(meta) is not dict:
                        raise IndexAuditUnavailable('index_metadata_invalid')
                    if identifier in seen:
                        raise IndexAuditUnavailable('index_snapshot_changed')
                    seen.add(identifier)
                    video_id = meta.get('platform_item_id')
                    if type(video_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', video_id):
                        raise IndexAuditUnavailable('index_metadata_invalid')
                    role = meta.get('dm_content_role') if type(meta.get('dm_content_role_version')) is int and meta.get('dm_content_role_version') == 1 else 'unverified'
                    if role not in {'source', 'metadata'}:
                        role = 'unverified'
                    if role == 'unverified' and verify_legacy_text:
                        from app.services.retrieval_fusion import _source_text
                        role = 'source' if _source_text({'text': documents[index], 'title': meta.get('title', '')}) else 'metadata'
                    row = counts.setdefault(video_id, {'platform_item_id': video_id, 'total_chunks': 0,
                        'verified_content_chunks': 0, 'metadata_chunks': 0, 'unverified_chunks': 0})
                    row['total_chunks'] += 1
                    row[{'source': 'verified_content_chunks', 'metadata': 'metadata_chunks', 'unverified': 'unverified_chunks'}[role]] += 1
                    text_hash = hashlib.sha256(documents[index].encode()).hexdigest() if verify_legacy_text else None
                    records.append((identifier, video_id, role, text_hash))
                offset += len(identifiers)
            after = self._collection.count()
            if type(after) is not int or before != after or offset != before:
                raise IndexAuditUnavailable('index_snapshot_changed')
            rows = []
            for key in sorted(counts):
                row = counts[key]
                row['content_verified'] = row['unverified_chunks'] == 0
                row['content_chunks'] = row['verified_content_chunks'] if row['content_verified'] else None
                rows.append(row)
            return {'read_complete': True, 'content_verified': all(row['content_verified'] for row in rows),
                    'counts': rows, 'index_records': before,
                    'index_hash': hashlib.sha256(json.dumps(sorted(records), separators=(',', ':')).encode()).hexdigest(),
                    'verification_mode': 'local_legacy_text' if verify_legacy_text else 'metadata_only',
                    'source_kind': 'live_index_metadata'}
        except IndexAuditUnavailable:
            raise
        except Exception:
            raise IndexAuditUnavailable() from None

    def delete_videos(self, platform_item_ids: Iterable[str]) -> None:
        """
        批量删除多个视频的向量数据

        :param platform_item_ids: 抖音视频 ID 列表
        """
        ids = [item for item in platform_item_ids if item]
        if not ids:
            return
        with _MUTATION_LOCK:
            self._collection.delete(where={"platform_item_id": {"$in": ids}})

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
