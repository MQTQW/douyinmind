"""Read recorded query evidence and register a first, operator-authored candidate.

This module cannot claim, resume, complete, ingest, query a model or publish.
The paused browser owner independently grants the eventual first-draft permit.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat
import unicodedata
import urllib.request

from app.mention_agent.contracts import Binding, canonical_json, validate_envelope
from app.mention_agent.engine import AgentEngine, _literal_quote_support, _PUBLIC_QUOTATIONS
from app.mention_agent.answer_requirements import parse_answer_requirements, validate_quotes
from app.mention_agent.knowledge import LocalKnowledgeTools
from app.mention_agent.store import AgentStore
from app.storage_guard import verified_readonly_connection
from .backend_knowledge import decode_json
from .reply_style import normalize_reply_prefix, reply_body_budget

TASK = re.compile(r'[a-f0-9]{32}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
COMPONENT = 'DouyinMind.operator-query-drafts.v1'
MAX_CANDIDATE_BYTES = 65536
MAX_BUNDLE_BYTES = 512 * 1024
_REQUEST_FIELDS = {'text', 'source_ids', 'expected_snapshot_digest', 'expected_evidence_digest'}


class OperatorDraftError(ValueError):
    def __init__(self, code, status_code=409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def _json(value):
    return canonical_json(value).encode('utf-8')


def _sha(value):
    return hashlib.sha256(_json(value)).hexdigest()


def operator_snapshot_digest(queue_task, agent_snapshot, authorization, binding,
                             reply_style_key, prefix, run_id, receiver_profile_url):
    """Shared with the owner: all arguments are frozen observations, no effects."""
    binding = binding.to_dict() if isinstance(binding, Binding) else Binding.from_dict(binding).to_dict()
    return _sha({'queue_task': queue_task, 'agent_snapshot': agent_snapshot,
        'authorization': authorization, 'binding': binding, 'reply_style_key': reply_style_key,
        'prefix': prefix, 'run_id': run_id, 'receiver_profile_url': receiver_profile_url})


def existing_agent_readonly(path):
    """Open only the existing component; the regular constructor can create it."""
    agent = AgentStore.__new__(AgentStore)
    agent.path, agent.protected_paths = Path(os.path.abspath(os.fspath(path))), ()
    agent._guard_path()
    agent._verify_existing_readonly()
    return agent


def _prefix_readonly(agent, key):
    with agent._file_lock():
        agent._guard()
        with verified_readonly_connection(agent.path, agent._owned, guard=agent._guard) as db:
            return agent._reply_prefix(db, key)


def _default_documents(video_ids):
    # This exceptional read must not initialize a client or recreate a lost
    # collection. The normal backend owns and initializes its existing index.
    from app.services import chroma_service
    service = chroma_service._chroma_service
    if service is None:
        raise OperatorDraftError('operator_draft_index_not_loaded')
    return service.get_documents(video_ids)


def validate_operator_proposal(proposal, evidence, prefix, effective_max_chars, *, comment_text='', library_name=''):
    """Pure shared owner/server validation; no file flag grants draft authority."""
    try:
        requirement = parse_answer_requirements(comment_text)
        expected_max = reply_body_budget(280 if requirement is not None else 320, prefix)
        if (normalize_reply_prefix(prefix) != prefix or type(effective_max_chars) is not int
                or effective_max_chars != expected_max or requirement is not None and requirement.issue
                or type(proposal) is not dict or set(proposal) != {'kind', 'text', 'source_ids', 'sources'}
                or proposal.get('kind') != 'answer' or type(proposal.get('source_ids')) is not list
                or not 1 <= len(proposal['source_ids']) <= 8
                or any(type(key) is not str for key in proposal['source_ids'])
                or len(set(proposal['source_ids'])) != len(proposal['source_ids'])
                or type(evidence) is not list or not 1 <= len(evidence) <= 64
                or any(type(item) is not dict or type(item.get('source_id')) is not str
                    or type(item.get('text')) is not str or not item['text'].strip()
                    or hashlib.sha256(item['text'].encode('utf-8')).hexdigest() != item.get('text_sha256')
                    for item in evidence)):
            raise ValueError()
        by_id = {item['source_id']: item for item in evidence}
        ids = proposal['source_ids']
        if len(by_id) != len(evidence) or not set(ids) <= by_id.keys():
            raise ValueError()
        sources = [{key: by_id[source][key] for key in
            ('source_id', 'platform_item_id', 'chunk_id', 'title', 'url', 'text_sha256')} for source in ids]
        if proposal['sources'] != sources or any(
                type(source['platform_item_id']) is not str or not re.fullmatch(r'[0-9]{5,30}', source['platform_item_id'])
                or type(source['chunk_id']) is not str or not source['chunk_id'].strip()
                or type(source['title']) is not str or not source['title'].strip() or len(source['title']) > 512
                or source['url'] != 'https://www.douyin.com/video/' + source['platform_item_id'] for source in sources):
            raise ValueError()
        validator = AgentEngine(None, None, reply_prefix=prefix)
        text = validator._public_text(proposal['text'], maximum=effective_max_chars, check_write_claims=False)
        mentioned = {'资料' + number for number in re.findall(r'资料\s*(\d+)', unicodedata.normalize('NFKC', text))}
        if not mentioned <= set(ids):
            raise ValueError()
        text = validator._public_text(re.sub(r'\[来源\s*[:：][^\]]*\]', '', text),
            maximum=effective_max_chars, check_write_claims=False)
        provided = {key: [by_id[key]['text']] for key in ids}
        labels = (library_name,) if library_name else ()
        if requirement is not None:
            quotes = []
            for match in _PUBLIC_QUOTATIONS.finditer(text):
                excerpt = next(value for value in match.groups() if value is not None)
                normalized = ' '.join(excerpt.split())
                matches = [key for key in ids if normalized and normalized in ' '.join(by_id[key]['text'].split())]
                if matches and len(''.join(excerpt.split())) >= 6 and sum(c.isalnum() for c in excerpt) >= 4:
                    quotes.append({'source_id': matches[0], 'excerpt': excerpt})
            validate_quotes(requirement, quotes, text, ids, provided, non_evidence_labels=labels)
        spans = []
        _literal_quote_support(text, ids, provided, labels=labels, attributed_spans=spans)
        validator._check_write_claims(text, spans)
        return {'kind': 'answer', 'text': text, 'source_ids': list(ids), 'sources': sources}
    except Exception:
        raise OperatorDraftError('operator_draft_reply_invalid', 422) from None


class OperatorDraftService:
    def __init__(self, queue, registry, database, agent, state_dir, *, control,
                 receiver_provider, source_loader=None, membership_reader=None, prefix_reader=None):
        self.queue, self.registry, self.database, self.agent = queue, registry, database, agent
        self.state_dir = Path(os.path.abspath(os.fspath(state_dir)))
        self.control, self.receiver_provider = control, receiver_provider
        self.source_loader = source_loader or _default_documents
        self.membership_reader = membership_reader or self._membership
        self.prefix_reader = prefix_reader or (lambda key: _prefix_readonly(self.agent, key))
        self.directory = self.state_dir / 'operator-query-drafts'

    def _membership(self, binding):
        tools = LocalKnowledgeTools(self.database, binding.knowledge_base_id, query_scope=binding.query_scope)
        with tools._read_session() as db:
            return tools._snapshot(db)

    def _scope(self, task_id):
        if type(task_id) is not str or not TASK.fullmatch(task_id):
            raise OperatorDraftError('operator_draft_task_invalid', 422)
        try:
            task = self.queue.get_task(task_id)
            if (type(task) is not dict or task.get('kind') != 'query' or task.get('state') != 'needs_review'
                    or task.get('reason_code') != 'model_round_budget_exceeded'
                    or task.get('publication_token') is not None
                    or type(task.get('publish_attempts', 0)) is not int or task.get('publish_attempts', 0) != 0
                    or type(task.get('agent_task_id')) is not str or not TASK.fullmatch(task['agent_task_id'])):
                raise OperatorDraftError('operator_draft_task_not_eligible')
            binding = Binding.from_dict(task['binding'])
            validate_envelope(task['envelope'], binding)
            if not binding.active or not binding.public_reply_allowed or task['envelope']['source_mode'] != 'live_ui':
                raise OperatorDraftError('operator_draft_scope_invalid', 403)
            authorization = task['authorization']
            current = self.registry.authorize_snapshot(authorization, action='query')
            user = self.registry.get_user(task['user_id'])
            device = self.registry.get_device(task['device_id'])
            if (current != binding or task.get('knowledge_base_id') != binding.knowledge_base_id
                    or type(authorization) is not dict
                    or task['user_id'] != authorization.get('user_id') or task['device_id'] != authorization.get('device_id')
                    or not user or user.get('active') is not True or user.get('verification_status') != 'verified'
                    or user.get('sender_profile_url') != binding.sender_profile_url
                    or user.get('receiver_profile_url') != binding.receiver_profile_url
                    or not device or device.get('user_id') != task['user_id']
                    or device.get('instance_id') != binding.instance_id or device.get('is_local') is not True
                    or device.get('enabled') is not True or device.get('revoked') is not False
                    or device.get('verification_status') != 'verified'):
                raise OperatorDraftError('operator_draft_scope_not_authorized', 403)
            control = self.control.snapshot()
            receiver = self.receiver_provider()
            if (type(control.get('run_id')) is not str or not TASK.fullmatch(control['run_id'])
                    or type(control.get('pid')) is not int or control['pid'] <= 0
                    or control.get('process_alive') is not True
                    or control.get('state') not in {'running', 'paused', 'pausing'}
                    or control.get('desired_state') not in {'running', 'paused'}
                    or receiver.get('receiver_profile_url') != binding.receiver_profile_url):
                raise OperatorDraftError('operator_draft_owner_unavailable')
            agent_snapshot = self.agent.operator_query_draft_snapshot(task['agent_task_id'])
            if (agent_snapshot['binding'] != binding.to_dict() or agent_snapshot['envelope'] != task['envelope']
                    or agent_snapshot['task_id'] != task['agent_task_id']):
                raise OperatorDraftError('operator_draft_agent_scope_changed')
            key = 'assistant:' + task_id
            prefix = self.prefix_reader(key)
            if (prefix is None or normalize_reply_prefix(prefix) != prefix
                    or agent_snapshot.get('agent_prefix') not in (None, prefix)):
                raise OperatorDraftError('operator_draft_frozen_style_required')
            requirement = parse_answer_requirements(task['envelope']['comment_text'])
            if requirement is not None and requirement.issue:
                raise OperatorDraftError('operator_draft_quote_requirement_unsupported')
            maximum = reply_body_budget(280 if requirement is not None else 320, prefix)
            description, allowed = self.membership_reader(binding)
            video = task['envelope']['video_id']
            if video not in allowed:
                raise OperatorDraftError('operator_draft_video_not_ready', 403)
            digest = operator_snapshot_digest(task, agent_snapshot, authorization, binding, key,
                prefix, control['run_id'], receiver['receiver_profile_url'])
            return {'task': deepcopy(task), 'agent': agent_snapshot, 'binding': binding,
                'description': description, 'allowed': set(allowed), 'snapshot_digest': digest,
                'prefix': prefix, 'reply_style_key': key, 'effective_max_chars': maximum,
                'run_id': control['run_id'], 'requirement': requirement}
        except OperatorDraftError:
            raise
        except Exception:
            raise OperatorDraftError('operator_draft_scope_unavailable') from None

    @staticmethod
    def _same_scope(before, after):
        if (before['snapshot_digest'] != after['snapshot_digest']
                or before['description'] != after['description'] or before['allowed'] != after['allowed']):
            raise OperatorDraftError('operator_draft_scope_changed', 403)

    def _evidence(self, task_id):
        scope = self._scope(task_id)
        video = scope['task']['envelope']['video_id']
        refs = scope['agent']['recorded_refs']
        if (type(refs) is not list or not 1 <= len(refs) <= 64
                or any(ref.get('platform_item_id') != video for ref in refs)
                or len({ref['source_id'] for ref in refs}) != len(refs)
                or len({ref['chunk_id'] for ref in refs}) != len(refs)):
            raise OperatorDraftError('operator_draft_recorded_scope_invalid')
        try:
            documents = self.source_loader({video})
            if (type(documents) is not list or not 1 <= len(documents) <= 10000
                    or any(type(doc) is not dict or doc.get('platform_item_id') != video
                        or type(doc.get('chunk_id')) is not str or not doc['chunk_id']
                        or type(doc.get('text')) is not str or type(doc.get('title')) is not str for doc in documents)
                    or sum(len(doc['text']) for doc in documents) > 16 * 1024 * 1024
                    or len({doc['chunk_id'] for doc in documents}) != len(documents)):
                raise OperatorDraftError('operator_draft_documents_invalid')
            by_id = {doc['chunk_id']: doc for doc in documents}
            if any(ref['chunk_id'] not in by_id for ref in refs):
                raise OperatorDraftError('operator_draft_original_missing')
            # These are the existing pure context transforms, never RagService().
            from app.core.config import settings
            from app.services.rag_service import RagService
            from app.services.retrieval_context import expand_hit_context, select_context_hits
            raw = [dict(by_id[ref['chunk_id']]) for ref in refs]
            expanded = expand_hit_context(raw, documents, max_extra_chars=settings.retrieval_neighbor_chars)
            variants = [raw, expanded]
            for hits in (raw, expanded):
                variants.append(select_context_hits(hits,
                    max_count=min(8, settings.rag_context_count), max_chars=settings.rag_prompt_max_context_chars,
                    format_blocks=RagService._format_context_blocks, has_source_text=RagService._has_source_text,
                    scope_ids={video}))
            evidence = []
            for ref in refs:
                matches = {}
                for variant in variants:
                    for hit in variant:
                        if (hit['chunk_id'] == ref['chunk_id'] and hit['text'].strip()
                                and RagService._has_source_text(hit)
                                and hashlib.sha256(hit['text'].encode('utf-8')).hexdigest() == ref['text_sha256']):
                            matches[hit['text']] = hit
                if len(matches) != 1:
                    raise OperatorDraftError('operator_draft_original_hash_mismatch')
                hit = next(iter(matches.values()))
                if not hit['title'].strip() or len(hit['title']) > 512:
                    raise OperatorDraftError('operator_draft_source_metadata_invalid')
                item = {**ref, 'title': hit['title'], 'url': 'https://www.douyin.com/video/' + video,
                        'text': hit['text']}
                if 'expanded_from_chunk_ids' in hit:
                    item['expanded_from_chunk_ids'] = list(hit['expanded_from_chunk_ids'])
                evidence.append(item)
        except OperatorDraftError:
            raise
        except Exception:
            raise OperatorDraftError('operator_draft_original_unavailable') from None
        self._same_scope(scope, self._scope(task_id))
        result = {'task_id': task_id, 'agent_task_id': scope['agent']['task_id'],
            'snapshot_digest': scope['snapshot_digest'], 'evidence_digest': _sha(evidence), 'evidence': evidence,
            'reply_style_key': scope['reply_style_key'], 'prefix': scope['prefix'],
            'effective_max_chars': scope['effective_max_chars'], 'run_id': scope['run_id']}
        if len(_json(result)) > MAX_BUNDLE_BYTES:
            raise OperatorDraftError('operator_draft_evidence_too_large')
        return result, scope

    def evidence(self, task_id):
        return self._evidence(task_id)[0]

    @staticmethod
    def _body(body):
        if (type(body) is not dict or set(body) != _REQUEST_FIELDS
                or type(body.get('text')) is not str or len(body['text']) > 600
                or type(body.get('source_ids')) is not list or not 1 <= len(body['source_ids']) <= 8
                or any(type(key) is not str for key in body['source_ids'])
                or len(set(body['source_ids'])) != len(body['source_ids'])
                or any(type(body.get(key)) is not str or not SHA.fullmatch(body[key])
                    for key in ('expected_snapshot_digest', 'expected_evidence_digest'))):
            raise OperatorDraftError('operator_draft_request_invalid', 422)
        return deepcopy(body)

    def _proposal(self, body, result, scope):
        if (body['expected_snapshot_digest'] != result['snapshot_digest']
                or body['expected_evidence_digest'] != result['evidence_digest']):
            raise OperatorDraftError('operator_draft_expected_snapshot_changed')
        evidence = {item['source_id']: item for item in result['evidence']}
        ids = body['source_ids']
        if not set(ids) <= evidence.keys():
            raise OperatorDraftError('operator_draft_source_ids_invalid', 422)
        sources = [{key: evidence[source][key] for key in
            ('source_id', 'platform_item_id', 'chunk_id', 'title', 'url', 'text_sha256')} for source in ids]
        return validate_operator_proposal({'kind': 'answer', 'text': body['text'], 'source_ids': ids,
            'sources': sources}, result['evidence'], result['prefix'], result['effective_max_chars'],
            comment_text=scope['task']['envelope']['comment_text'], library_name=scope['description']['name'])

    @staticmethod
    def _directory_guard(path):
        if not path.is_absolute() or path.resolve() != path:
            raise OperatorDraftError('operator_draft_path_unsafe')
        for ancestor in (path, *path.parents):
            info = ancestor.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
                raise OperatorDraftError('operator_draft_path_unsafe')

    @staticmethod
    def _regular(path):
        info = path.lstat()
        if (path.resolve() != path or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not 0 < info.st_size <= MAX_CANDIDATE_BYTES
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise OperatorDraftError('operator_draft_file_unsafe')
        return info

    def _read_file(self, path):
        self._directory_guard(self.directory)
        before = self._regular(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        try:
            opened = os.fstat(fd)
            raw = os.read(fd, MAX_CANDIDATE_BYTES + 1)
            after = self._regular(path)
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if identity(before) != identity(opened) or identity(opened) != identity(after) or len(raw) != opened.st_size:
                raise OperatorDraftError('operator_draft_file_changed')
            return decode_json(raw)
        finally:
            os.close(fd)

    def _write_file(self, path, value):
        self._directory_guard(self.directory)
        raw = _json(value)
        if not 0 < len(raw) <= MAX_CANDIDATE_BYTES:
            raise OperatorDraftError('operator_draft_candidate_too_large')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
                     | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OperatorDraftError('operator_draft_file_unsafe')
            remaining = memoryview(raw)
            while remaining:
                written = os.write(fd, remaining)
                if not written:
                    raise OperatorDraftError('operator_draft_write_incomplete')
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if self._read_file(path) != value:
            raise OperatorDraftError('operator_draft_file_changed')

    def _owned_directory(self, *, create=False):
        self._directory_guard(self.state_dir)
        state = self.state_dir.stat()
        marker = {'component': COMPONENT, 'version': 1,
                  'state_identity': {'device': state.st_dev, 'inode': state.st_ino}}
        if create:
            try:
                self.directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                self._write_file(self.directory / '_owner.json', marker)
        self._directory_guard(self.directory)
        if self._read_file(self.directory / '_owner.json') != marker:
            raise OperatorDraftError('operator_draft_directory_not_owned')

    def register(self, task_id, body):
        body = self._body(body)
        result, scope = self._evidence(task_id)
        proposal = self._proposal(body, result, scope)
        # Fence validation time too, before the only allowed write (candidate).
        fresh, _ = self._evidence(task_id)
        if fresh != result:
            raise OperatorDraftError('operator_draft_evidence_changed')
        candidate = {'component': COMPONENT, 'version': 1, 'task_id': task_id,
            'agent_task_id': result['agent_task_id'], 'run_id': result['run_id'],
            'snapshot_digest': result['snapshot_digest'], 'evidence_digest': result['evidence_digest'],
            'reply_style_key': result['reply_style_key'], 'prefix': result['prefix'],
            'request': body, 'proposal': proposal}
        try:
            self._owned_directory(create=True)
            path = self.directory / (task_id + '.json')
            try:
                self._write_file(path, candidate)
            except FileExistsError:
                if self._read_file(path) != candidate:
                    raise OperatorDraftError('operator_draft_candidate_already_registered')
            return self.bundle(task_id)
        except OperatorDraftError:
            raise
        except Exception:
            raise OperatorDraftError('operator_draft_registration_unavailable') from None

    def bundle(self, task_id):
        result, scope = self._evidence(task_id)
        try:
            self._owned_directory()
            candidate = self._read_file(self.directory / (task_id + '.json'))
            if (type(candidate) is not dict or set(candidate) != {'component', 'version', 'task_id',
                    'agent_task_id', 'run_id', 'snapshot_digest', 'evidence_digest', 'reply_style_key',
                    'prefix', 'request', 'proposal'} or candidate.get('component') != COMPONENT
                    or candidate.get('version') != 1
                    or any(candidate.get(key) != result[key] for key in ('task_id', 'agent_task_id', 'run_id',
                        'snapshot_digest', 'evidence_digest', 'reply_style_key', 'prefix'))):
                raise OperatorDraftError('operator_draft_candidate_scope_changed')
            proposal = self._proposal(self._body(candidate['request']), result, scope)
            if proposal != candidate['proposal']:
                raise OperatorDraftError('operator_draft_candidate_invalid')
            fresh, _ = self._evidence(task_id)
            if fresh != result:
                raise OperatorDraftError('operator_draft_evidence_changed')
            return {**result, 'proposal': proposal, 'candidate_sha256': _sha(candidate)}
        except OperatorDraftError:
            raise
        except Exception:
            raise OperatorDraftError('operator_draft_candidate_unavailable') from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OperatorDraftError('operator_draft_transport_redirect')


def fetch_operator_draft_bundle(task_id):
    """One bounded fixed-loopback GET. No retries, redirects, model or proxies."""
    if type(task_id) is not str or not TASK.fullmatch(task_id):
        raise OperatorDraftError('operator_draft_task_invalid')
    url = 'http://127.0.0.1:8000/api/assistant/operator-query-draft/' + task_id + '/bundle'
    request = urllib.request.Request(url, headers={'Origin': 'http://127.0.0.1:8000', 'Accept': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            if (response.getcode() != 200 or response.geturl() != url
                    or response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json'):
                raise OperatorDraftError('operator_draft_transport_invalid')
            raw = response.read(MAX_BUNDLE_BYTES + 1)
            if len(raw) > MAX_BUNDLE_BYTES:
                raise OperatorDraftError('operator_draft_transport_too_large')
        result = decode_json(raw)
        if (type(result) is not dict or result.get('task_id') != task_id
                or any(type(result.get(key)) is not str or not SHA.fullmatch(result[key])
                    for key in ('snapshot_digest', 'evidence_digest', 'candidate_sha256'))):
            raise OperatorDraftError('operator_draft_transport_invalid')
        return result
    except OperatorDraftError:
        raise
    except Exception:
        raise OperatorDraftError('operator_draft_transport_unavailable') from None
