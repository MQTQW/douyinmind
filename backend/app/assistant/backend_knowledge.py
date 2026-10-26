"""Helper-side evidence transport; the helper never opens the Chroma client."""
from __future__ import annotations

import json
import re
from threading import Lock
import urllib.error
import urllib.request

from app.mention_agent.knowledge import LocalKnowledgeTools


EVIDENCE_URL = "http://127.0.0.1:8000/api/assistant/evidence/query"
MAX_REQUEST_BYTES = 12 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_QUESTION_CHARS = 2000
HTTP_TIMEOUT_SECONDS = 45
EXECUTION_CLAIM_HEADER = "X-DouyinMind-Execution-Claim"
_VIDEO = re.compile(r"[0-9]{5,30}\Z")
_TASK = re.compile(r"[a-f0-9]{32}\Z")
_CLAIM = re.compile(r"[a-f0-9]{48}\Z")


def valid_execution_claim(value):
    return isinstance(value, str) and _CLAIM.fullmatch(value) is not None


class BackendEvidenceError(RuntimeError):
    """Fixed errors only; never expose request URLs, bodies or provider errors."""


class NoEvidenceRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BackendEvidenceError("backend_evidence_redirect_rejected")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def decode_json(raw):
    def nonfinite(_):
        raise ValueError("nonfinite_json")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=nonfinite)


def normalized_evidence_result(value, knowledge_base_id, *, allowed_video_ids=None, query_scope='library'):
    """Bound and validate the evidence contract without executing any URL."""
    if (not isinstance(value, dict) or set(value) - {
            "status", "knowledge_base_id", "knowledge_base_name", "evidence", "retrieval_mode", "query_scope"}
            or not isinstance(query_scope,str) or query_scope not in {'library','all_local'}
            or 'knowledge_base_id' not in value
            or (query_scope=='library' and (type(knowledge_base_id) is not int or knowledge_base_id<=0
                or type(value.get('knowledge_base_id')) is not int))
            or (query_scope=='all_local' and (knowledge_base_id is not None or value.get('knowledge_base_id') is not None
                or value.get('query_scope')!='all_local'))
            or value.get('query_scope','library') != query_scope
            or value["knowledge_base_id"] != knowledge_base_id
            or value.get("status") not in {"ok", "empty", "no_evidence"}
            or not isinstance(value.get("knowledge_base_name"), str)
            or not value["knowledge_base_name"].strip() or len(value["knowledge_base_name"]) > 128
            or not isinstance(value.get("evidence"), list) or len(value["evidence"]) > 8
            or value.get("retrieval_mode") not in {"hybrid", "semantic", "lexical_fallback", "empty_scope", "unknown"}):
        raise BackendEvidenceError("backend_evidence_invalid_response")
    evidence, total = [], 0
    for item in value["evidence"]:
        if (not isinstance(item, dict) or set(item) - {"source_id", "platform_item_id", "chunk_id", "title", "url", "text", "source_type", "time_range"}
                or any(not isinstance(item.get(key), str) or not item[key].strip()
                       for key in ("source_id", "platform_item_id", "chunk_id", "title", "url", "text"))
                or not _VIDEO.fullmatch(item["platform_item_id"])
                or len(item["source_id"]) > 128 or len(item["chunk_id"]) > 256 or len(item["title"]) > 512
                or len(item["text"]) > 64000
                or item["url"] != "https://www.douyin.com/video/" + item["platform_item_id"]
                or ("source_type" in item and item["source_type"] not in {"speech", "visual", "transcript", "metadata"})
                or ("time_range" in item and (not isinstance(item["time_range"], str)
                    or not re.fullmatch(r"\d{2,3}:\d{2}(?::\d{2})?\s*[–—~\-]\s*\d{2,3}:\d{2}(?::\d{2})?", item["time_range"])))):
            raise BackendEvidenceError("backend_evidence_invalid_response")
        if allowed_video_ids is not None and item["platform_item_id"] not in allowed_video_ids:
            continue
        if item["text"].lstrip().startswith(("视频标题：", "视频标题:")) or item["text"].strip() == item["title"].strip():
            continue
        total += len(item["text"])
        if total > 64000:
            raise BackendEvidenceError("backend_evidence_response_too_large")
        evidence.append(dict(item))
    result = dict(value)
    if result["status"] != "ok" and evidence:
        raise BackendEvidenceError("backend_evidence_invalid_response")
    result["evidence"] = evidence
    if not evidence and result["status"] == "ok":
        result["status"] = "no_evidence"
    return result


class BackendKnowledgeTools:
    def __init__(self, database, knowledge_base_id, task_id, focus_video_id=None, *, execution_token,
                 query_scope='library'):
        if not isinstance(task_id, str) or not _TASK.fullmatch(task_id):
            raise ValueError("invalid_evidence_task_id")
        if focus_video_id is not None and (not isinstance(focus_video_id, str) or not _VIDEO.fullmatch(focus_video_id)):
            raise ValueError("invalid_evidence_focus")
        if not valid_execution_claim(execution_token):
            raise ValueError("invalid_evidence_execution_claim")
        # describe() uses only the established SQL-only library snapshot path.
        scope_options = {'query_scope':query_scope} if query_scope != 'library' else {}
        self._description = LocalKnowledgeTools(database, knowledge_base_id, **scope_options)
        self.knowledge_base_id, self.task_id = knowledge_base_id, task_id
        self.query_scope = query_scope
        self.focus_video_id = focus_video_id
        self._execution_token = execution_token
        self._source_ids = {}
        self._source_lock = Lock()

    def describe(self):
        return self._description.describe()

    def query(self, question):
        return self._query(question)

    def query_with_focus(self, question, *, focus='auto'):
        if not isinstance(focus, str) or focus not in {'auto', 'current_video', 'authorized_scope'}:
            raise ValueError('invalid_evidence_focus_mode')
        return self._query(question, focus=focus, explicit_focus=True)

    def _query(self, question, *, focus='auto', explicit_focus=False):
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
            raise ValueError("invalid_evidence_question")
        body = {"task_id": self.task_id, "question": question.strip()}
        if explicit_focus:
            body['focus'] = focus
        if focus == 'auto' and self.focus_video_id is not None:
            body["focus_video_id"] = self.focus_video_id
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_REQUEST_BYTES:
            raise BackendEvidenceError("backend_evidence_request_too_large")
        request = urllib.request.Request(EVIDENCE_URL, data=raw, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json", "Origin": "http://127.0.0.1:8000",
            EXECUTION_CLAIM_HEADER: self._execution_token})
        # Deliberately ignore HTTP_PROXY/HTTPS_PROXY and the global opener.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoEvidenceRedirect())
        try:
            with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                if response.getcode() != 200 or response.geturl() != EVIDENCE_URL:
                    raise BackendEvidenceError("backend_evidence_invalid_http_response")
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise BackendEvidenceError("backend_evidence_invalid_http_response")
                raw_response = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw_response) > MAX_RESPONSE_BYTES:
                    raise BackendEvidenceError("backend_evidence_response_too_large")
            result = normalized_evidence_result(decode_json(raw_response), self.knowledge_base_id,
                allowed_video_ids={self.focus_video_id} if focus == 'auto' and self.focus_video_id else None,
                query_scope=self.query_scope)
        except BackendEvidenceError:
            raise
        except Exception:
            # One attempt only. A timeout never triggers an automatic retry.
            raise BackendEvidenceError("backend_evidence_unavailable") from None
        # Server tools may be request-local. Preserve stable source labels for
        # this helper conversation rather than reusing 资料1 for another chunk.
        with self._source_lock:
            for item in result["evidence"]:
                key = (item["platform_item_id"], item["chunk_id"])
                if key not in self._source_ids:
                    self._source_ids[key] = f"资料{len(self._source_ids) + 1}"
                item["source_id"] = self._source_ids[key]
        return result
