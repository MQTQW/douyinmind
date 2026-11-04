"""Bounded source-only token preparation for scope-preserving retrieval.

Only compact source term postings/lengths are retained. Queries, answers,
histories, source dictionaries and mutable result objects are never cached.
The caller supplies the authoritative corpus on every call. A failed or
over-budget preparation returns None so the original tokenizer remains usable.
"""
from __future__ import annotations

from array import array
from collections import Counter, OrderedDict
import hashlib
import sys
import threading


DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_MAX_CHUNK_CHARS = 20000
_ENTRY_BASE_BYTES = 4096
_TOKEN_ENTRY_BYTES = 512
_NEGATIVE_ENTRY_BYTES = 1024
_SCALAR_FIELDS = ("platform_item_id", "chunk_id", "text", "title")


def _put_string(digest, value):
    digest.update(len(value).to_bytes(8, "big"))
    for offset in range(0, len(value), 4096):
        digest.update(value[offset:offset + 4096].encode("utf-8", errors="surrogatepass"))


class _HardBudgetRefusal(Exception):
    pass


class _PreparedIndex:
    __slots__ = ("postings", "lengths", "bytes")

    def __init__(self, postings, lengths, retained_bytes):
        self.postings = postings
        self.lengths = lengths
        self.bytes = retained_bytes

    def features(self, identities, query_keys):
        # Match the original per-document Counter insertion order exactly:
        # each document receives its nonzero terms in query_keys iteration order.
        term_counts = {key: Counter() for key in identities}
        lengths = {key: self.lengths[index] for index, key in enumerate(identities)}
        frequencies = Counter()
        for term in query_keys:
            posting = self.postings.get(term)
            if posting is None:
                continue
            indices, counts = posting
            frequencies[term] = len(indices)
            for index, count in zip(indices, counts):
                term_counts[identities[index]][term] = count
        return term_counts, frequencies, lengths


class PreparedLexicalCache:
    """One cache per ranker module; serialized preparation, bounded live storage.

    Conservative accounting includes retained arrays, dictionary/string overhead,
    current source snapshots, array reallocation slack and a per-chunk temporary
    tokenizer workspace. Admission/eviction happen before a cold allocation.
    Ordinary ranking's own per-query dictionaries are not retained by this cache.
    """
    def __init__(self, max_bytes=DEFAULT_MAX_BYTES, *, max_entries=8, max_rejections=64):
        if (type(max_bytes) is not int or max_bytes < 0
                or type(max_entries) is not int or max_entries < 1
                or type(max_rejections) is not int or not 1 <= max_rejections <= 64):
            raise ValueError("invalid_prepared_lexical_budget")
        self._max_bytes = max_bytes
        self.max_entries = max_entries
        self.max_rejections = max_rejections
        self._entries = OrderedDict()
        self._negative = OrderedDict()
        self._retained_bytes = 0
        self._negative_bytes = 0
        self._prepare_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._hits = self._misses = self._builds = self._fallbacks = self._evictions = 0
        self._peak_reserved_bytes = 0
        self._negative_hits = 0

    @property
    def max_bytes(self):
        return self._max_bytes

    @max_bytes.setter
    def max_bytes(self, value):
        if type(value) is not int or value < 0:
            raise ValueError("invalid_prepared_lexical_budget")
        with self._prepare_lock, self._state_lock:
            if value != self._max_bytes:
                self._entries.clear()
                self._negative.clear()
                self._retained_bytes = self._negative_bytes = 0
                self._peak_reserved_bytes = 0
                self._max_bytes = value

    def stats(self):
        with self._state_lock:
            return {"max_bytes": self.max_bytes, "retained_bytes": self._retained_bytes + self._negative_bytes,
                "prepared_index_bytes": self._retained_bytes, "negative_entries": len(self._negative),
                "negative_hits": self._negative_hits,
                "peak_reserved_bytes": self._peak_reserved_bytes, "entries": len(self._entries),
                "hits": self._hits, "misses": self._misses, "builds": self._builds,
                "fallbacks": self._fallbacks, "evictions": self._evictions}

    def clear(self):
        with self._prepare_lock, self._state_lock:
            self._entries.clear()
            self._negative.clear()
            self._retained_bytes = self._negative_bytes = 0

    def _reserve(self, build_and_workspace_bytes):
        with self._state_lock:
            if build_and_workspace_bytes > self.max_bytes:
                return False
            while (self._entries or self._negative) and self._retained_bytes + self._negative_bytes + build_and_workspace_bytes > self.max_bytes:
                if self._entries:
                    _, entry = self._entries.popitem(last=False)
                    self._retained_bytes -= entry.bytes
                else:
                    self._negative.popitem(last=False)
                    self._negative_bytes -= _NEGATIVE_ENTRY_BYTES
                self._evictions += 1
            self._peak_reserved_bytes = max(self._peak_reserved_bytes,
                self._retained_bytes + self._negative_bytes + build_and_workspace_bytes)
            return True

    def _remember_refusal(self, key, workspace):
        if not self._reserve(workspace + _NEGATIVE_ENTRY_BYTES):
            return
        with self._state_lock:
            while len(self._negative) >= self.max_rejections:
                self._negative.popitem(last=False)
                self._negative_bytes -= _NEGATIVE_ENTRY_BYTES
            self._negative[(key, self.max_bytes)] = None
            self._negative_bytes += _NEGATIVE_ENTRY_BYTES

    def _fallback(self):
        with self._state_lock:
            self._fallbacks += 1
        return None

    def _key_and_snapshot(self, documents, items):
        # Cache only ordinary scalar source data. Unusual caller objects still
        # work through the unchanged original retrieval path.
        longest, total_chars = 0, 0
        for hit in documents:
            if type(hit) is not dict:
                return None
            for field in _SCALAR_FIELDS:
                value = hit.get(field)
                if value is not None and type(value) is not str:
                    return None
                if isinstance(value, str):
                    longest = max(longest, len(value))
                    total_chars += len(value)
        hard_limit = longest > _MAX_CHUNK_CHARS or total_chars > self.max_bytes * 2
        workspace = 65536 + (0 if hard_limit else len(items) * 128)
        if not self._reserve(workspace):
            return None
        # Immutable strings are snapshotted before tokenizing, so a caller's
        # later dictionary replacement cannot poison an entry under an old key.
        snapshot = None if hard_limit else tuple((key, str(hit.get("text") or "")) for key, hit in items.items())
        digest = hashlib.sha256(b"DouyinMind prepared lexical sources v2\0")
        for hit in documents:
            digest.update(b"D")
            for field in _SCALAR_FIELDS:
                if field not in hit:
                    digest.update(b"M")
                elif hit[field] is None:
                    digest.update(b"N")
                else:
                    digest.update(b"S")
                    _put_string(digest, hit[field])
        source_items = snapshot if snapshot is not None else ((key, str(hit.get("text") or "")) for key, hit in items.items())
        for identity, text in source_items:
            digest.update(b"P")
            _put_string(digest, identity[0])
            _put_string(digest, identity[1])
            _put_string(digest, text)
        return digest.digest(), snapshot, workspace, hard_limit

    def _build(self, snapshot, tokenizer, workspace):
        postings, lengths = {}, array("Q")
        if array("I").itemsize != 4 or lengths.itemsize != 8:
            return None
        pairs = vocabulary_bytes = 0
        count = len(snapshot)
        # Account for the largest single posting/length buffer's reallocating
        # copy, in addition to the conservative retained storage estimate.
        reallocation_slack = count * 8 + 1024
        for document_index, (_, text) in enumerate(snapshot):
            retained = _ENTRY_BASE_BYTES + vocabulary_bytes + pairs * 9 + len(lengths) * 9
            scratch = 16384 + len(text) * 512
            if not self._reserve(retained + workspace + reallocation_slack + scratch):
                raise _HardBudgetRefusal()
            terms = tokenizer(text)
            if not isinstance(terms, Counter):
                return None
            length = max(1, sum(terms.values()))
            if length > 0xffffffffffffffff:
                return None
            added_vocabulary = sum(sys.getsizeof(term) + _TOKEN_ENTRY_BYTES
                                   for term in terms if term not in postings)
            next_bytes = (_ENTRY_BASE_BYTES + vocabulary_bytes + added_vocabulary
                          + (pairs + len(terms)) * 9 + (len(lengths) + 1) * 9)
            if not self._reserve(next_bytes + workspace + reallocation_slack + scratch):
                raise _HardBudgetRefusal()
            lengths.append(length)
            for term, frequency in terms.items():
                if type(term) is not str or type(frequency) is not int or not 1 <= frequency <= 0xffffffff:
                    return None
                posting = postings.get(term)
                if posting is None:
                    posting = (array("I"), array("I"))
                    postings[term] = posting
                posting[0].append(document_index)
                posting[1].append(frequency)
            pairs += len(terms)
            vocabulary_bytes += added_vocabulary
            # Do not keep the previous Counter alive while the next is built.
            del terms
        retained = _ENTRY_BASE_BYTES + vocabulary_bytes + pairs * 9 + len(lengths) * 9
        if not self._reserve(retained + workspace):
            raise _HardBudgetRefusal()
        return _PreparedIndex(postings, lengths, retained)

    def features(self, documents, items, query_keys, tokenizer):
        if not self.max_bytes or sys.implementation.name != "cpython":
            return self._fallback()
        try:
            # Preparing source data is serial: many cold requests cannot each
            # allocate a budget-sized index. No DB, provider or answer work is
            # performed while this local CPU mutex is held.
            with self._prepare_lock:
                keyed = self._key_and_snapshot(documents, items)
                if keyed is None:
                    return self._fallback()
                key, snapshot, workspace, hard_limit = keyed
                with self._state_lock:
                    refused = (key, self.max_bytes) in self._negative
                    if refused:
                        self._negative.move_to_end((key, self.max_bytes))
                        self._negative_hits += 1
                    index = self._entries.get(key) if not refused else None
                    if index is not None:
                        self._entries.move_to_end(key)
                        self._hits += 1
                    else:
                        self._misses += 1
                if refused:
                    return self._fallback()
                if hard_limit:
                    self._remember_refusal(key, workspace)
                    return self._fallback()
                if index is None:
                    budget_refused = False
                    try:
                        index = self._build(snapshot, tokenizer, workspace)
                    except _HardBudgetRefusal:
                        budget_refused = True
                        index = None
                    # The failed build's traceback/temporary arrays have been
                    # released before retaining a refusal fingerprint.
                    if budget_refused:
                        self._remember_refusal(key, workspace)
                    if index is None:
                        return self._fallback()
                    with self._state_lock:
                        while len(self._entries) >= self.max_entries:
                            _, old = self._entries.popitem(last=False)
                            self._retained_bytes -= old.bytes
                            self._evictions += 1
                        self._entries[key] = index
                        self._retained_bytes += index.bytes
                        self._builds += 1
                # Only fresh, query-local dictionaries leave this method. Their
                # identities come from the current authoritative scope, never
                # from a cached source or an earlier user's result.
                return index.features(tuple(key for key, _ in snapshot), query_keys)
        except Exception:
            # A cache issue must not become a new retrieval business failure.
            return self._fallback()
