"""Short-lived loopback viewer; browser work is drained by the owning thread.

No command files, screenshots on disk, navigation, JS, CDP or credential APIs.
The HTTP thread only validates and queues bounded user actions in memory.
"""
from __future__ import annotations

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import secrets
import threading
import time

from .manual_verification_page import PAGE, SCRIPT

MAX_BODY = 4096
MAX_FRAME = 4 * 1024 * 1024
KEYS = frozenset({'Tab', 'Shift+Tab', 'Enter', 'Escape', 'Backspace', 'Delete',
                  'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'})
_RUN = re.compile(r'[a-f0-9]{32}\Z')
_NONCE = re.compile(r'[a-f0-9]{64}\Z')


class ManualVerificationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(data):
    return json.dumps(data, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ManualVerificationError('invalid_request')
        result[key] = value
    return result


class ManualVerificationServer:
    """Construct and drain on the runtime's browser-owner thread only.

    allowed() must return a fresh trusted control snapshot (never HTTP input).
    handler(action, payload) executes only from process_one(). finish must return
    {ok: True, verified: True}; this module never resumes publishing or a queue.
    """
    def __init__(self, owner_run_id, *, allowed, port=8767, process_guard=None,
                 clock=None, session_seconds=300, frame_seconds=20):
        if type(owner_run_id) is not str or not _RUN.fullmatch(owner_run_id) or not callable(allowed):
            raise ManualVerificationError('invalid_manual_owner')
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ManualVerificationError('invalid_manual_port')
        if type(session_seconds) is not int or not 30 <= session_seconds <= 300:
            raise ManualVerificationError('invalid_manual_ttl')
        if type(frame_seconds) is not int or not 1 <= frame_seconds <= 30:
            raise ManualVerificationError('invalid_frame_ttl')
        self.owner_run_id = owner_run_id
        self.port = port
        self.origin = 'http://127.0.0.1:' + str(port)
        self._pid, self._owner_thread = os.getpid(), threading.get_ident()
        self._allowed, self._guard = allowed, process_guard
        self._clock = clock or time.monotonic
        self._session_seconds, self._frame_seconds = session_seconds, frame_seconds
        self._lock = threading.RLock()
        self._pending = deque()
        self._inflight = None
        self._nonces = {}
        self._session = self._session_nonce = None
        self._owner_finalization_pending = False
        self._finalization_epoch = 0
        self._expires = 0.0
        self._state = 'idle'
        self._reason = None
        self._frame = None
        self._frame_at = 0.0
        self._width = self._height = 0
        self._input_enabled = self._auth_region_known = False
        self._frame_generation = 0
        self._server = self._thread = None
        self._closed = False

    def __repr__(self):
        return '<ManualVerificationServer loopback memory-only>'

    def _owner(self):
        if os.getpid() != self._pid or threading.get_ident() != self._owner_thread:
            raise ManualVerificationError('manual_owner_thread_required')

    def _clear_frame(self):
        self._frame = None
        self._width = self._height = 0
        self._frame_at = 0.0
        self._input_enabled = self._auth_region_known = False

    def _end(self, state, reason=None):
        self._pending.clear()  # Includes any unconsumed text; never persisted.
        self._clear_frame()
        self._session = self._session_nonce = None
        self._expires = 0.0
        self._state, self._reason = state, reason

    def _expire(self):
        now = self._clock()
        self._nonces = {key: expiry for key, expiry in self._nonces.items() if expiry > now}
        if self._session and now >= self._expires:
            self._end('expired')
        if self._frame is not None and now - self._frame_at >= self._frame_seconds:
            self._clear_frame()

    @property
    def session_active(self):
        with self._lock:
            self._expire()
            return self._session is not None

    @property
    def owner_finalization_pending(self):
        """A begin needs owner pause acknowledgement even after cancel/expiry."""
        with self._lock:
            self._expire()
            return self._owner_finalization_pending

    @property
    def finalization_epoch(self):
        with self._lock:
            return self._finalization_epoch

    def acknowledge_finalization(self, expected_epoch):
        """Owner CAS after forcing pause; never consume a newer begin intent."""
        self._owner()
        if type(expected_epoch) is not int or expected_epoch < 1:
            return False
        try:
            if not self._trusted_allowed():
                return False
        except Exception:
            return False
        with self._lock:
            self._expire()
            if (expected_epoch != self._finalization_epoch or self._session is not None
                    or self._pending or self._inflight is not None):
                return False
            self._owner_finalization_pending = False
            return True

    def safe_status(self):
        with self._lock:
            self._expire()
            return {'state': self._state, 'reason_code': self._reason,
                    'active': self._session is not None, 'pending': bool(self._pending),
                    'frame_available': self._frame is not None,
                    'frame_generation': self._frame_generation,
                    'input_enabled': self._input_enabled,
                    'auth_region_known': self._auth_region_known}

    def operation_current(self):
        """Current operation lease, checked by owner before/after browser waits.

        This does not replace the owner's fresh process/control/auth-DOM guard.
        Input also requires the exact displayed frame and enabled auth region.
        """
        with self._lock:
            self._expire()
            if self._closed or self._inflight is None or self._inflight[1] != self._session:
                return False
            if self._inflight[0] in {'click', 'drag', 'key', 'text'}:
                return bool(self._inflight[2] == self._frame_generation and self._frame is not None
                            and self._input_enabled and self._auth_region_known)
            return True

    def input_lease_current(self):
        """Compatibility helper for input-only checks; no extra authority."""
        with self._lock:
            return bool(self.operation_current() and self._inflight is not None
                        and self._inflight[0] in {'click', 'drag', 'key', 'text'})

    def _trusted_allowed(self):
        if os.getpid() != self._pid:
            return False
        if self._guard is not None and self._guard() is not True:
            return False
        value = self._allowed()
        return (type(value) is dict and value.get('run_id') == self.owner_run_id
                and value.get('process_alive') is True and value.get('state') == 'paused'
                and value.get('receiver_state') in {'verification_required', 'login_required'}
                and value.get('worker_state') == 'idle'
                and value.get('publication_busy') is False
                and value.get('queue_worker_busy') is False)

    def process_one(self, handler):
        """Run at most one command on owner thread. Return fixed public status."""
        self._owner()
        if not callable(handler):
            raise ManualVerificationError('invalid_manual_handler')
        with self._lock:
            self._expire()
            if self._closed or not self._pending:
                return None
            action, payload, session, frame_generation = self._pending.popleft()
            if session != self._session:
                return None
            self._state = 'processing'
            self._inflight = (action, session, frame_generation)
        try:
            if action == 'cancel':
                with self._lock:
                    self._end('cancelled')
                return self.safe_status()
            if not self._trusted_allowed():
                with self._lock:
                    self._end('verification_not_ready', 'owner_unavailable')
                return self.safe_status()
            # The trusted control read can block. Recheck memory lease and
            # exact displayed frame immediately before executing browser input.
            with self._lock:
                self._expire()
                if self._closed or session != self._session:
                    return self.safe_status()
                if action in {'click', 'drag', 'key', 'text'} and (self._frame is None
                        or self._frame_generation != frame_generation
                        or not self._input_enabled or not self._auth_region_known):
                    self._end('verification_not_ready', 'page_unavailable')
                    return self.safe_status()
            result = handler(action, payload)
            # Check owner/scheduler scope again after browser work. finish still
            # cannot resume normal execution; the caller keeps its control pause.
            if not self._trusted_allowed():
                with self._lock:
                    self._end('verification_not_ready', 'owner_unavailable')
                return self.safe_status()
            with self._lock:
                self._expire()
                if self._closed or session != self._session:
                    return self.safe_status()
                if type(result) is not dict or result.get('ok') is not True:
                    raise ManualVerificationError('action_failed')
                if action == 'screenshot':
                    frame = result.get('frame')
                    width, height = result.get('width'), result.get('height')
                    if (type(frame) is not bytes or not 8 <= len(frame) <= MAX_FRAME
                            or not frame.startswith(b'\x89PNG\r\n\x1a\n')
                            or type(width) is not int or type(height) is not int
                            or not 1 <= width <= 4096 or not 1 <= height <= 4096):
                        raise ManualVerificationError('page_unavailable')
                    if (type(result.get('input_enabled', False)) is not bool
                            or type(result.get('auth_region_known', False)) is not bool):
                        raise ManualVerificationError('page_unavailable')
                    self._frame, self._width, self._height = frame, width, height
                    self._input_enabled = result.get('input_enabled', False)
                    self._auth_region_known = result.get('auth_region_known', False)
                    self._frame_at = self._clock()
                    self._frame_generation += 1
                elif action == 'finish':
                    if result.get('verified') is not True:
                        self._state, self._reason = 'active', 'verification_failed'
                        return self.safe_status()
                    self._end('completed')
                    return self.safe_status()
                self._state, self._reason = 'active', None
        except Exception as exc:
            # Do not log/serialize exception messages: they could contain text.
            reason = exc.code if type(exc) is ManualVerificationError and exc.code in {'page_unavailable', 'action_failed'} else 'action_failed'
            with self._lock:
                self._end('verification_failed', reason)
        finally:
            # Python strings cannot promise physical memory zeroization. The
            # reference is dropped and no application log/file/response sees it.
            if type(payload) is dict:
                payload.clear()
            with self._lock:
                self._inflight = None
        return self.safe_status()

    def _nonce_valid(self, nonce):
        return (type(nonce) is str and bool(_NONCE.fullmatch(nonce))
                and self._nonces.get(nonce, 0) > self._clock())

    def request(self, method, path, headers, body=b'', *, client_host='127.0.0.1'):
        """Pure request boundary, also used by socket-free isolated tests.

        Return (HTTP status, fixed response headers, bytes). No request text or
        arbitrary error is returned. headers must already reject duplicates.
        """
        with self._lock:
            self._expire()
            fixed = {'Cache-Control': 'no-store', 'Pragma': 'no-cache',
                     'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
                     'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}
            def answer(status, value):
                return status, {**fixed, 'Content-Type': 'application/json; charset=utf-8'}, _json(value)
            if self._closed:
                return answer(503, {'code': 'manual_closed'})
            if (client_host != '127.0.0.1' or type(headers) is not dict
                    or headers.get('host') != '127.0.0.1:' + str(self.port)):
                return answer(403, {'code': 'manual_origin_required'})
            origin = headers.get('origin')
            if origin is not None and origin != self.origin:
                return answer(403, {'code': 'manual_origin_required'})
            if method == 'GET' and path == '/':
                # SOP hides this nonce from another origin; CSP forbids framing.
                if headers.get('sec-fetch-dest') in {'iframe', 'frame', 'object', 'embed'}:
                    return answer(403, {'code': 'manual_origin_required'})
                nonce = secrets.token_hex(32)
                if len(self._nonces) >= 8:
                    expendable = next((n for n in self._nonces if n != self._session_nonce), None)
                    if expendable is not None:
                        self._nonces.pop(expendable, None)
                self._nonces[nonce] = self._clock() + 600
                page = PAGE.replace('__MANUAL_NONCE__', nonce).encode('utf-8')
                return 200, {**fixed, 'Content-Type': 'text/html; charset=utf-8'}, page
            if method == 'GET' and path == '/viewer.js':
                return 200, {**fixed, 'Content-Type': 'text/javascript; charset=utf-8'}, SCRIPT.encode('utf-8')
            nonce = headers.get('x-douyinmind-manual')
            if not self._nonce_valid(nonce):
                return answer(403, {'code': 'manual_nonce_required'})
            if self._session and nonce != self._session_nonce:
                return answer(409, {'code': 'manual_session_owned'})
            if method == 'GET' and path == '/api/status':
                return answer(200, {**self.safe_status(), 'session_id': self._session})
            if method == 'GET' and path == '/api/frame':
                if not self._session or self._frame is None:
                    return answer(404, {'code': 'manual_frame_unavailable'})
                return 200, {**fixed, 'Content-Type': 'image/png',
                             'X-DouyinMind-Frame-Generation': str(self._frame_generation)}, self._frame
            if method != 'POST' or path not in {'/api/begin', '/api/action'}:
                return answer(405, {'code': 'manual_method_not_allowed'})
            if origin != self.origin:
                return answer(403, {'code': 'manual_origin_required'})
            if (headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json'
                    or type(body) is not bytes or len(body) > MAX_BODY):
                return answer(400, {'code': 'manual_invalid_request'})
            try:
                data = json.loads(body, object_pairs_hook=_pairs,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if type(data) is not dict:
                    raise ValueError()
                if path == '/api/begin':
                    if data or self._session:
                        return answer(409, {'code': 'manual_session_busy'})
                    self._session, self._session_nonce = secrets.token_hex(16), nonce
                    self._expires = self._clock() + self._session_seconds
                    self._finalization_epoch += 1
                    self._owner_finalization_pending = True
                    action, payload, command_generation = 'begin', {}, None
                else:
                    base_keys = {'session_id', 'action', 'payload'}
                    if set(data) not in (base_keys, base_keys | {'frame_generation'}) or data['session_id'] != self._session or not self._session:
                        raise ValueError()
                    action, payload = data['action'], data['payload']
                    command_generation = None
                    if type(action) is not str or type(payload) is not dict:
                        raise ValueError()
                    if action in {'click', 'drag', 'key', 'text'}:
                        command_generation = data.get('frame_generation')
                        if (type(command_generation) is not int or command_generation != self._frame_generation
                                or self._frame is None or not self._input_enabled or not self._auth_region_known):
                            raise ValueError()
                    elif set(data) != base_keys:
                        raise ValueError()
                    if action in {'screenshot', 'finish', 'cancel'}:
                        if payload:
                            raise ValueError()
                    elif action == 'click':
                        if (set(payload) != {'x', 'y'} or type(payload['x']) is not int or type(payload['y']) is not int
                                or self._frame is None or not 0 <= payload['x'] < self._width or not 0 <= payload['y'] < self._height):
                            raise ValueError()
                    elif action == 'drag':
                        if set(payload) != {'points'} or type(payload['points']) is not list or not 2 <= len(payload['points']) <= 80 or self._frame is None:
                            raise ValueError()
                        previous = -1
                        for point in payload['points']:
                            if (type(point) is not dict or set(point) != {'x', 'y', 't'}
                                    or type(point['x']) is not int or type(point['y']) is not int or type(point['t']) is not int
                                    or not 0 <= point['x'] < self._width or not 0 <= point['y'] < self._height
                                    or not previous <= point['t'] <= 10000):
                                raise ValueError()
                            previous = point['t']
                        if payload['points'][0]['t'] != 0 or payload['points'][-1]['t'] < 1:
                            raise ValueError()
                    elif action == 'key':
                        if set(payload) != {'key'} or payload['key'] not in KEYS:
                            raise ValueError()
                    elif action == 'text':
                        if (set(payload) != {'text'} or type(payload['text']) is not str or not 1 <= len(payload['text']) <= 256
                                or any(ord(char) < 32 or ord(char) == 127 for char in payload['text'])):
                            raise ValueError()
                    else:
                        raise ValueError()
                if self._pending or self._state == 'processing':
                    if action == 'cancel':
                        self._end('cancelled')
                        return answer(200, {'accepted': True})
                    return answer(409, {'code': 'manual_action_pending'})
                self._pending.append((action, payload, self._session, command_generation))
                self._state, self._reason = 'queued', None
                return answer(202, {'accepted': True, 'session_id': self._session})
            except Exception:
                return answer(400, {'code': 'manual_invalid_request'})

    def start(self):
        """Bind this fixed loopback port or fail; never find/kill another owner."""
        self._owner()
        if self._server is not None or self._closed:
            raise ManualVerificationError('manual_server_already_used')
        manager = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'
            def setup(self):
                super().setup()
                self.connection.settimeout(2)  # Bound header reads as well as body.
            def log_message(self, *_args):
                pass
            def _serve(self):
                try:
                    self.connection.settimeout(2)
                    names = [name.lower() for name in self.headers.keys()]
                    if len(names) != len(set(names)) or len(names) > 30 or self.headers.get('Transfer-Encoding') is not None:
                        raise ValueError()
                    headers = {key.lower(): value for key, value in self.headers.items()}
                    raw_length = self.headers.get('Content-Length', '0')
                    if not re.fullmatch(r'[0-9]{1,5}', raw_length):
                        raise ValueError()
                    length = int(raw_length)
                    if length > MAX_BODY:
                        raise ValueError()
                    body = self.rfile.read(length) if length else b''
                    if len(body) != length:
                        raise ValueError()
                    status, headers_out, content = manager.request(self.command, self.path, headers, body,
                                                                   client_host=self.client_address[0])
                except Exception:
                    status, headers_out, content = 400, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}, b'{"code":"manual_invalid_request"}'
                self.send_response(status)
                for key, value in headers_out.items():
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Connection', 'close')
                self.end_headers()
                try:
                    self.wfile.write(content)
                except OSError:
                    pass
            do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = _serve
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False
            request_queue_size = 4
            def __init__(self, *args, **kwargs):
                self._slots = threading.BoundedSemaphore(4)
                super().__init__(*args, **kwargs)
            def process_request(self, request, client_address):
                if not self._slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except Exception:
                    self._slots.release()
                    raise
            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self._slots.release()
            def handle_error(self, *_args):
                pass  # Never write request/exception text to process logs.
        try:
            self._server = Server(('127.0.0.1', self.port), Handler)
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            name='DouyinMindManualViewer', daemon=True)
            self._thread.start()
        except Exception:
            if self._server is not None:
                self._server.server_close()
            self._server = None
            raise ManualVerificationError('manual_port_unavailable') from None
        return self.origin + '/'

    def close(self):
        self._owner()
        with self._lock:
            self._closed = True
            self._end('cancelled')
            self._nonces.clear()
            server, thread = self._server, self._thread
            self._server = self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
