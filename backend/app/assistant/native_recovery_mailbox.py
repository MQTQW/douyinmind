"""Fixed, short-lived owner-thread review intents; no browser or model APIs.

Only the current paused registered run is scanned. Claim artifacts are permanent
evidence: a crashed/unknown claim is never replayed automatically.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

_RUN = re.compile(r'[a-f0-9]{32}\Z')
_SOURCE = re.compile(r'native:[a-f0-9]{64}\Z')
_HINT = re.compile(r'native-hint:[a-f0-9]{64}\Z')
_TASK = re.compile(r'assistant-task:[a-f0-9]{32}\Z')
_DRAFT = re.compile(r'assistant-draft:[a-f0-9]{32}:[a-f0-9]{64}\Z')
_REQUEST = re.compile(r'([a-f0-9]{32})\.json\Z')
OPERATIONS = frozenset({'review_legacy_prequeue', 'resume_interrupted_prequeue','request_visibility_ui_review',
                        'inspect_visibility_hint','recover_visibility_hint','inspect_reply','request_followup_ui_review',
                        'inspect_thread_followup','review_unexecuted_followup','finalize_query_draft'})
_MARKER = 'DouyinMind.native-review.v1'
_BOOLS = ('reviewed', 'eligible', 'queued_for_fresh_ui', 'historical_preserved', 'dispatchable')
_HANDLER_CODES = frozenset({'native_recovery_not_safe', 'native_recovery_scope_unavailable',
    'native_recovery_effects_unknown', 'native_recovery_stopped', 'native_recovery_identity_unverified',
    'native_legacy_recovery_incomplete', 'native_recovery_effects_present'})
MAX_BYTES = 1024


def _source_for_operation(source_id,operation):
    if type(source_id) is not str or type(operation) is not str or operation not in OPERATIONS:return False
    if operation=='inspect_reply':return bool(_TASK.fullmatch(source_id))
    if operation=='finalize_query_draft':return bool(_DRAFT.fullmatch(source_id))
    return bool((_HINT if operation in {'request_visibility_ui_review','inspect_visibility_hint','recover_visibility_hint'} else _SOURCE).fullmatch(source_id))


class NativeRecoveryMailboxError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _pairs(rows):
    value = {}
    for key, item in rows:
        if key in value:
            raise NativeRecoveryMailboxError('native_recovery_request_invalid')
        value[key] = item
    return value


def _json(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except Exception:
        raise NativeRecoveryMailboxError('native_recovery_request_invalid') from None


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def _identity(info):
    return info.st_dev, info.st_ino


class NativeRecoveryMailbox:
    def __init__(self, state_dir, run_id, control, *, clock=None):
        if type(run_id) is not str or not _RUN.fullmatch(run_id) or not callable(getattr(control, 'snapshot', None)):
            raise NativeRecoveryMailboxError('native_recovery_owner_invalid')
        self.state_dir = Path(state_dir).absolute()
        self.run_id, self.control = run_id, control
        self._clock = clock or time.time
        self._thread, self._pid = threading.get_ident(), os.getpid()
        self._check_dir(self.state_dir)
        self.path = self.state_dir / 'runs' / self.run_id / 'native-review'

    def __repr__(self):
        return '<NativeRecoveryMailbox fixed-current-run>'

    @staticmethod
    def _check_dir(path):
        if not path.is_absolute() or path.resolve() != path:
            raise NativeRecoveryMailboxError('native_recovery_path_unsafe')
        try:
            info = path.lstat()
        except OSError:
            raise NativeRecoveryMailboxError('native_recovery_path_unavailable') from None
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise NativeRecoveryMailboxError('native_recovery_path_unsafe')

    @staticmethod
    def _regular(path):
        try:
            info = path.lstat()
        except OSError:
            raise NativeRecoveryMailboxError('native_recovery_file_unavailable') from None
        if (path.resolve() != path or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
                or not 0 < info.st_size <= MAX_BYTES):
            raise NativeRecoveryMailboxError('native_recovery_file_unsafe')
        return info

    def _read(self, path):
        self._check_directory_chain()
        before = self._regular(path)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
            try:
                opened = os.fstat(descriptor)
                if _identity(before) != _identity(opened) or opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode):
                    raise NativeRecoveryMailboxError('native_recovery_file_unsafe')
                raw = os.read(descriptor, MAX_BYTES + 1)
                after = self._regular(path)
                if len(raw) != opened.st_size or len(raw) > MAX_BYTES or _identity(after) != _identity(opened):
                    raise NativeRecoveryMailboxError('native_recovery_file_unsafe')
            finally:
                os.close(descriptor)
        except NativeRecoveryMailboxError:
            raise
        except OSError:
            raise NativeRecoveryMailboxError('native_recovery_file_unavailable') from None
        return _json(raw)

    def _write_exclusive(self, path, value):
        self._check_directory_chain()
        raw = _encoded(value)
        if not 0 < len(raw) <= MAX_BYTES:
            raise NativeRecoveryMailboxError('native_recovery_result_invalid')
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
                                 | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise NativeRecoveryMailboxError('native_recovery_file_unsafe')
                view = memoryview(raw)
                while view:
                    size = os.write(descriptor, view)
                    if size <= 0:
                        raise NativeRecoveryMailboxError('native_recovery_write_failed')
                    view = view[size:]
                os.fsync(descriptor)
                current = self._regular(path)
                if _identity(current) != _identity(opened):
                    raise NativeRecoveryMailboxError('native_recovery_file_unsafe')
            finally:
                os.close(descriptor)
        except NativeRecoveryMailboxError:
            raise
        except OSError:
            raise NativeRecoveryMailboxError('native_recovery_write_failed') from None

    def _check_directory_chain(self):
        self._check_dir(self.state_dir)
        for path in (self.state_dir / 'runs', self.state_dir / 'runs' / self.run_id, self.path):
            self._check_dir(path)

    def _directory(self, *, create=False):
        if not create and not self.path.exists():
            return False
        created = False
        self._check_dir(self.state_dir)
        for path in (self.state_dir / 'runs', self.state_dir / 'runs' / self.run_id, self.path):
            if not path.exists():
                if not create:
                    return False
                try:
                    path.mkdir()
                    if path == self.path:
                        created = True
                except OSError:
                    raise NativeRecoveryMailboxError('native_recovery_path_unavailable') from None
            self._check_dir(path)
        marker = self.path / '.owner.json'
        expected = {'owner': _MARKER, 'run_id': self.run_id}
        if created:
            self._write_exclusive(marker, expected)
        else:
            if not marker.exists():
                raise NativeRecoveryMailboxError('native_recovery_owner_marker_missing')
            if self._read(marker) != expected:
                raise NativeRecoveryMailboxError('native_recovery_foreign_mailbox')
        return True

    def _guard(self, *, owner=False):
        current = self.control.snapshot()
        valid = (type(current) is dict and current.get('run_id') == self.run_id
                 and current.get('state') == 'paused' and current.get('desired_state') == 'paused'
                 and current.get('process_alive') is True and current.get('worker_state') == 'idle')
        if owner:
            if threading.get_ident() != self._thread or os.getpid() != self._pid:
                raise NativeRecoveryMailboxError('native_recovery_owner_thread_required')
            valid = valid and type(current.get('pid')) is int and current['pid'] == os.getpid()
        return valid

    @staticmethod
    def _safe(request_id, source_id, status, code, fields=None):
        result = {'request_id': request_id, 'source_id': source_id, 'status': status, 'code': code,
                  'no_model': True, 'no_import': True, 'no_send': True, 'dispatchable': False}
        if fields:
            for key in _BOOLS:
                if key in fields:
                    if type(fields[key]) is not bool or key == 'dispatchable' and fields[key] is not False:
                        raise NativeRecoveryMailboxError('native_recovery_result_invalid')
                    result[key] = fields[key]
        return result

    def submit(self, source_id, operation):
        if not _source_for_operation(source_id,operation):
            raise NativeRecoveryMailboxError('native_recovery_request_invalid')
        if not self._guard():
            raise NativeRecoveryMailboxError('native_recovery_not_paused')
        self._directory(create=True)
        request_id = uuid.uuid4().hex
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise NativeRecoveryMailboxError('native_recovery_clock_invalid')
        value = {'version': 1, 'request_id': request_id, 'source_id': source_id, 'operation': operation,
                 'run_id': self.run_id, 'created_at': now, 'expires_at': now + 300}
        temporary, target = self.path / (request_id + '.tmp'), self.path / (request_id + '.json')
        self._write_exclusive(temporary, value)
        if not self._guard():
            # Preserve the unpublished intent; never sweep another run's files.
            raise NativeRecoveryMailboxError('native_recovery_owner_changed')
        self._rename_exclusive(temporary, target)
        return self._safe(request_id, source_id, 'queued', 'native_recovery_requested')

    def _rename_exclusive(self, source, target):
        self._check_directory_chain()
        before = self._regular(source)
        if target.exists() or target.is_symlink():
            raise NativeRecoveryMailboxError('native_recovery_claim_conflict')
        # Windows os.rename is atomic and never replaces an existing target.
        # The product host is Windows; other hosts fail rather than weaken this.
        if os.name != 'nt':
            raise NativeRecoveryMailboxError('native_recovery_windows_required')
        try:
            os.rename(source, target)
        except OSError:
            raise NativeRecoveryMailboxError('native_recovery_claim_conflict') from None
        after = self._regular(target)
        if _identity(before) != _identity(after):
            raise NativeRecoveryMailboxError('native_recovery_file_unsafe')

    def _validate(self, value, request_id):
        required = {'version','request_id','source_id','operation','run_id','created_at','expires_at'}
        if (type(value) is not dict or set(value) != required or type(value['version']) is not int or value['version'] != 1
                or value['request_id'] != request_id or value['run_id'] != self.run_id
                or not _source_for_operation(value['source_id'],value['operation'])):
            raise NativeRecoveryMailboxError('native_recovery_request_invalid')
        created, expires, now = value['created_at'], value['expires_at'], self._clock()
        if (type(created) not in (int,float) or type(expires) not in (int,float)
                or not math.isfinite(created) or not math.isfinite(expires)
                or not 0 < created <= now or not 0 < expires-created <= 300):
            raise NativeRecoveryMailboxError('native_recovery_request_invalid')
        if now >= expires:
            raise NativeRecoveryMailboxError('native_recovery_request_expired')
        return value

    def process_one(self, handler):
        if not callable(handler):
            raise NativeRecoveryMailboxError('native_recovery_handler_invalid')
        if not self._guard(owner=True) or not self._directory():
            return None
        names = sorted(path.name for path in self.path.iterdir() if _REQUEST.fullmatch(path.name))
        if len(names) > 32:
            raise NativeRecoveryMailboxError('native_recovery_backlog_limit')
        for name in names:
            request_id = _REQUEST.fullmatch(name)[1]
            original, claimed = self.path / name, self.path / (request_id + '.claimed')
            result_path = self.path / (request_id + '.result')
            if claimed.exists() or result_path.exists():
                continue  # Never replay any known/unknown prior claim.
            self._rename_exclusive(original, claimed)
            source_id = None
            try:
                value = self._validate(self._read(claimed), request_id)
                source_id = value['source_id']
                if not self._guard(owner=True):
                    result = self._safe(request_id, source_id, 'rejected', 'native_recovery_owner_changed')
                else:
                    # Fresh TTL/run check after potentially slow control read.
                    self._validate(value, request_id)
                    returned = handler(value['operation'], source_id)
                    if type(returned) is not dict or returned.get('ok') is not True:
                        code = returned.get('code') if type(returned) is dict else None
                        code = code if code in _HANDLER_CODES else 'native_recovery_not_safe'
                        result = self._safe(request_id, source_id, 'rejected', code)
                    else:
                        code = ('assistant_reply_inspected' if value['operation'] == 'inspect_reply' else
                            'native_visibility_recovery_requested' if value['operation'] == 'recover_visibility_hint' else
                            'native_recovery_inspected' if value['operation'] == 'inspect_visibility_hint' else
                            'native_recovery_reviewed' if value['operation'] == 'review_legacy_prequeue' else
                            'operator_query_draft_finalized' if value['operation'] == 'finalize_query_draft' else
                            'native_recovery_resume_requested')
                        result = self._safe(request_id, source_id, 'done', code, returned)
            except NativeRecoveryMailboxError as error:
                code = error.code if error.code in {'native_recovery_request_invalid','native_recovery_request_expired','native_recovery_result_invalid'} else 'native_recovery_handler_failed'
                result = self._safe(request_id, source_id, 'rejected', code)
            except Exception:
                result = self._safe(request_id, source_id, 'failed', 'native_recovery_handler_failed')
            self._write_exclusive(result_path, result)
            return result
        return None
