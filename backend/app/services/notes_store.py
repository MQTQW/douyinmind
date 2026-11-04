"""Durable note UUIDs and per-call barriers in the business SQLite database.

This module performs no network calls and never restarts an unknown step. All
content lives here so material deletion can erase it in the same transaction.
"""
from datetime import datetime, timezone
import hashlib
import json
import uuid

from sqlalchemy import case, select, update

from app.models.entities import KnowledgeNoteGeneration as Generation, KnowledgeNoteRequest as Request, KnowledgeNoteStep as Step


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NotesPersistenceError(RuntimeError):
    def __init__(self):
        super().__init__('notes_persistence_unconfirmed')


def rollback(db):
    try:
        db.rollback()
    except Exception:
        try:
            db.invalidate()
        except Exception:
            db.close()


class NotesStore:
    def __init__(self, db):
        self.db = db

    def commit(self):
        try:
            self.db.commit()
        except Exception:
            rollback(self.db)
            raise NotesPersistenceError() from None

    def request(self, identity):
        return self.db.get(Request, identity, populate_existing=True)

    def generation(self, identity):
        return self.db.get(Generation, identity, populate_existing=True)

    def steps(self, identity):
        return list(self.db.scalars(select(Step).where(Step.note_id == identity)
            .order_by(Step.ordinal).execution_options(populate_existing=True)))

    def register(self, *, request_id, video_id, intent_hash, snapshot, source_fingerprint,
                 cache_key, configuration_hash, model_label, specs, recovery_of=None, redo=False):
        """Registration commits before queueing; a failed queue never frees its UUID."""
        existing = self.request(request_id)
        if existing is not None:
            if existing.platform_item_id != video_id or existing.intent_hash != intent_hash:
                raise ValueError('notes_request_conflict')
            return existing, True
        cached = None if redo else self.db.scalar(select(Generation).where(
            Generation.platform_item_id == video_id, Generation.cache_key == cache_key,
            Generation.state == 'done', Generation.markdown != '')
            .order_by(Generation.created_at.desc(), Generation.note_id.desc()).limit(1))
        if cached is not None:
            if digest(cached.markdown) != cached.markdown_sha256:
                raise ValueError('notes_artifact_unconfirmed')
            generation, task_id = cached, None
        else:
            generation = Generation(note_id=str(uuid.uuid4()), platform_item_id=video_id,
                cache_key=cache_key, source_fingerprint=source_fingerprint, configuration_hash=configuration_hash,
                model_label=model_label, source_snapshot=canonical(snapshot), state='queued', active_key=1,
                expected_calls=len(specs), revision=1)
            self.db.add(generation)
            self.db.flush()
            for ordinal, kind, fingerprint in specs:
                step = Step(step_id=str(uuid.uuid4()), note_id=generation.note_id, ordinal=ordinal,
                    kind=kind, input_fingerprint=fingerprint, state='pending', revision=1)
                self.db.add(step)
                if kind != 'merge':
                    self._reuse(step, cache_key)
            task_id = 'notes-' + uuid.UUID(request_id).hex
        request = Request(request_id=request_id, platform_item_id=video_id, note_id=generation.note_id,
                          intent_hash=intent_hash, task_id=task_id, recovery_of=recovery_of, cached=cached is not None)
        self.db.add(request)
        self.commit()
        return request, False

    def _reuse(self, step, cache_key):
        prior = self.db.scalars(select(Step).join(Generation, Step.note_id == Generation.note_id).where(
            Generation.cache_key == cache_key, Generation.state != 'deleted',
            Step.note_id != step.note_id, Step.kind == step.kind,
            Step.input_fingerprint == step.input_fingerprint, Step.state == 'succeeded', Step.output != '')
            .order_by(Step.finished_at.desc(), Step.step_id.desc()).limit(100))
        for old in prior:
            if digest(old.output) == old.output_sha256:
                step.state, step.output, step.output_sha256 = 'succeeded', old.output, old.output_sha256
                step.reused_from, step.finished_at = old.step_id, now()
                # Historical usage is retained on its original step, never
                # counted as new requests/zero cost on this generation.
                return True
        return False

    def begin_generation(self, note_id):
        result = self.db.execute(update(Generation).where(Generation.note_id == note_id,
            Generation.state == 'queued', Generation.active_key == 1).values(
                state='running', revision=Generation.revision + 1, updated_at=now()))
        if result.rowcount != 1:
            rollback(self.db)
            return False
        self.commit()
        return True

    def bind_merge(self, step_id, fingerprint, cache_key):
        step = self.db.get(Step, step_id, populate_existing=True)
        if step is None or step.state != 'pending':
            raise ValueError('notes_step_not_pending')
        step.input_fingerprint = fingerprint
        self._reuse(step, cache_key)
        self.commit()
        return self.db.get(Step, step_id, populate_existing=True)

    def begin_step(self, step_id):
        step = self.db.get(Step, step_id, populate_existing=True)
        if step is None or step.state != 'pending':
            raise ValueError('notes_step_not_pending')
        generation = self.generation(step.note_id)
        if generation is None or generation.state != 'running' or generation.active_key != 1:
            raise ValueError('notes_generation_not_running')
        result = self.db.execute(update(Step).where(Step.step_id == step_id, Step.state == 'pending',
            Step.revision == step.revision).values(state='started', started_at=now(), revision=step.revision + 1))
        if result.rowcount != 1:
            rollback(self.db)
            raise ValueError('notes_step_conflict')
        self.commit()  # No provider call may precede this durable barrier.

    def save_success(self, step_id, output, usage):
        step = self.db.get(Step, step_id, populate_existing=True)
        if step is None or step.state != 'started':
            raise ValueError('notes_step_not_started')
        generation = self.generation(step.note_id)
        if generation is None or generation.state != 'running':
            raise ValueError('notes_generation_not_running')
        fingerprint = digest(output)
        result = self.db.execute(update(Step).where(Step.step_id == step_id,
            Step.state == 'started', Step.revision == step.revision).values(state='succeeded',
                output=output, output_sha256=fingerprint, call_id=usage.get('call_id'),
                usage_json=canonical(usage), finished_at=now(), revision=step.revision + 1))
        if result.rowcount != 1:
            rollback(self.db)
            raise ValueError('notes_step_conflict')
        try:
            self.commit()
        except NotesPersistenceError:
            # Lost acknowledgement is not permission to call again. Only the
            # exact durable success proof can accept the returned content.
            confirmed = self.db.get(Step, step_id, populate_existing=True)
            if confirmed is None or confirmed.state != 'succeeded' or confirmed.output_sha256 != fingerprint:
                raise

    def review(self, note_id, reason, usage=None):
        """Best-effort terminal projection; never clears a started barrier."""
        rollback(self.db)
        generation = self.generation(note_id)
        if generation is None or generation.state in {'done', 'deleted'}:
            return
        for step in self.steps(note_id):
            if step.state == 'started':
                step.state, step.finished_at, step.revision = 'unknown', now(), step.revision + 1
                if usage:
                    step.usage_json = canonical(usage)
                    step.call_id = usage.get('call_id')
        generation.state, generation.active_key = 'needs_review', None
        generation.reason_code, generation.updated_at = reason, now()
        generation.revision += 1
        self.commit()

    def publish(self, note_id, markdown):
        generation = self.generation(note_id)
        steps = self.steps(note_id)
        if (generation is None or generation.state != 'running' or not steps
                or any(step.state != 'succeeded' or digest(step.output) != step.output_sha256 for step in steps)):
            raise ValueError('notes_publication_unconfirmed')
        fingerprint = digest(markdown)
        changed = self.db.execute(update(Generation).where(Generation.note_id == note_id,
            Generation.state == 'running', Generation.active_key == 1, Generation.revision == generation.revision)
            .values(state='done', active_key=None, markdown=markdown, markdown_sha256=fingerprint,
                    reason_code='', updated_at=now(), revision=generation.revision + 1))
        if changed.rowcount != 1:
            rollback(self.db)
            raise ValueError('notes_publication_conflict')
        try:
            self.commit()
        except NotesPersistenceError:
            confirmed = self.generation(note_id)
            if confirmed is None or confirmed.state != 'done' or confirmed.markdown_sha256 != fingerprint:
                raise


def recover_incomplete_notes(db):
    """Startup-only projection. This never enqueues a callable or pays a call."""
    store = NotesStore(db)
    active = list(db.scalars(select(Generation.note_id).where(Generation.state.in_(['queued', 'running']))))
    for identity in active:
        store.review(identity, 'interrupted')
    return len(active)


def erase_material_notes(db, platform_item_id):
    """Caller owns the deletion's final SQL transaction; no independent commit."""
    identities = select(Generation.note_id).where(Generation.platform_item_id == platform_item_id)
    db.execute(update(Step).where(Step.note_id.in_(identities)).values(
        state=case((Step.state.in_(['started', 'unknown']), 'unknown'), else_='deleted'),
        output='', output_sha256='', revision=Step.revision + 1))
    db.execute(update(Generation).where(Generation.platform_item_id == platform_item_id).values(
        state='deleted', active_key=None, source_snapshot='', markdown='', markdown_sha256='',
        reason_code='deleted', updated_at=now(), revision=Generation.revision + 1))
