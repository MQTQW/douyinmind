"""Run selected synthetic pytest files in an immutable public-code copy.

This is an audit-hook guard for trusted tests, not an OS sandbox for malicious
Python. No product module or Store is imported by this harness. The source and
the shared dependency environment remain read-only; all test writes go to the
fresh temporary root or this run's private report directory.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import sysconfig
import tempfile
import traceback
from urllib.parse import unquote, urlsplit, parse_qs
import uuid

EXCLUDED = {
    "storage", "exports", "__pycache__", ".venv", "node_modules", "dist",
    "cache", ".pytest_cache", ".git", "logs", "tmp", "temp",
}
REPARSE = 0x400
# Explicitly published tools only. Other backend/scripts files are local
# experiments and may reference private acceptance data. Never copy those.
PUBLIC_SCRIPTS = frozenset({
    '__init__.py', 'evaluate_retrieval.py', 'evaluate_answers.py',
    'evaluate_conversations.py', 'render_retrieval_report.py',
    'render_answer_report.py', 'long_video_test.py', 'observe_ingestion.py',
    'audit_materials.py', 'benchmark_retrieval.py',
})


def within(path, root):
    return path == root or root in path.parents


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def dump_new(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=True, indent=2, sort_keys=True)
        stream.write("\n")


def safe_directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or path.resolve() != path
            or getattr(info, "st_file_attributes", 0) & REPARSE):
        raise ValueError("unsafe_source_directory")


def safe_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or path.resolve() != path
            or getattr(info, "st_file_attributes", 0) & REPARSE):
        raise ValueError("unsafe_source_file")


def public_rel(path):
    return (len(path.parts) >= 2 and path.parts[0] in {"app", "tests", "scripts"}
            and path.suffix == ".py" and not path.is_absolute()
            and not (path.parts[0] == 'tests' and path.name.startswith('test_journal'))
            and (path.parts[0] != 'scripts' or (len(path.parts) == 2 and path.name in PUBLIC_SCRIPTS))
            and not any(part in EXCLUDED or part.startswith(".") for part in path.parts))


def inventory(backend):
    safe_directory(backend)
    found = []
    for prefix in ("app", "tests", "scripts"):
        top = backend / prefix
        if prefix == 'scripts' and not top.exists():
            continue
        safe_directory(top)
        for current, directories, files in os.walk(top, followlinks=False):
            current = Path(current)
            safe_directory(current)
            kept = []
            for name in directories:
                if name in EXCLUDED or name.startswith("."):
                    continue
                safe_directory(current / name)
                kept.append(name)
            directories[:] = sorted(kept)
            for name in sorted(files):
                if not name.endswith(".py") or name.startswith("."):
                    continue
                absolute = current / name
                relative = absolute.relative_to(backend)
                if not public_rel(relative):
                    if prefix == 'scripts' or (prefix == 'tests' and name.startswith('test_journal')):
                        continue
                    raise ValueError("non_public_source_path")
                safe_file(absolute)
                found.append(relative)
    if not found or len(found) != len(set(found)):
        raise ValueError("empty_or_duplicate_inventory")
    return sorted(found)


def manifest(base, paths):
    result = {}
    for relative in paths:
        target = base / relative
        safe_file(target)
        raw = target.read_bytes()
        result[relative.as_posix()] = {"sha256": sha(raw), "bytes": len(raw)}
    return result


def selected_tests(values, backend, paths):
    selected = []
    known = {path.as_posix() for path in paths}
    for value in values:
        name, *nodes = value.replace("\\", "/").split("::")
        if name.startswith("backend/"):
            name = name[len("backend/"):]
        if "/" not in name:
            name = "tests/" + name
        relative = Path(name)
        if (name not in known or not public_rel(relative)
                or relative.parts[0] != "tests"
                or not relative.name.startswith("test_")
                or any(not node or "\0" in node or "/" in node or "\\" in node for node in nodes)):
            raise ValueError("invalid_or_missing_selected_test:" + name)
        safe_file(backend / relative)
        normalized = relative.as_posix()
        if nodes:
            normalized += "::" + "::".join(nodes)
        if normalized in selected:
            raise ValueError("duplicate_selected_test")
        selected.append(normalized)
    return selected


def filesystem_path(value):
    if isinstance(value, int):
        return value
    if value is None:
        return Path.cwd()
    text = os.fsdecode(value)
    if "\0" in text:
        raise ValueError("nul_path")
    return Path(text).resolve()


def sqlite_path(value):
    text = os.fsdecode(value)
    if text == ":memory:":
        return None
    if "\0" in text:
        raise ValueError("nul_sqlite_path")
    if text.startswith("file:"):
        uri = urlsplit(text)
        if uri.netloc not in ("", "localhost"):
            raise ValueError("remote_sqlite_uri")
        query = parse_qs(uri.query)
        path = unquote(uri.path)
        if path == ":memory:":
            return None
        if query.get("mode") == ["memory"] and re.fullmatch(r"[A-Za-z0-9_-]+", path):
            return None
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return filesystem_path(path)
    return filesystem_path(text)


class Guard:
    def __init__(self, work, report, runtime, source_backend, *, allow_stdlib_socketpair=False,
                 allow_fixture_hardlinks=False):
        self.work, self.report = work, report
        self.runtime, self.source_backend = runtime, source_backend
        self.script = Path(__file__).resolve()
        self.verify = False
        self.denials = []
        self.denial_details = []
        self._stdlib_socketpair_code = None
        self._stdlib_socket_type = None
        self._stdlib_socketpair_globals = None
        self._stdlib_socket_frames = set()
        self.stdlib_socketpair_events = 0
        self.allow_fixture_hardlinks = allow_fixture_hardlinks
        self.fixture_hardlink_events = 0
        if allow_stdlib_socketpair:
            import socket
            expected = Path(sysconfig.get_path('stdlib')).resolve() / 'socket.py'
            safe_file(expected)
            function = socket.socketpair
            code = getattr(function, '__code__', None)
            if code is None or Path(code.co_filename).resolve() != expected:
                raise RuntimeError('stdlib_socketpair_implementation_unverified')
            self._stdlib_socketpair_code = code
            self._stdlib_socket_type = socket.socket
            self._stdlib_socketpair_globals = function.__globals__
            self._native_getsockname = socket.SocketType.getsockname
            self._socket_families = (socket.AF_INET, socket.AF_INET6)
            self._socket_stream_type = socket.SOCK_STREAM
            for method in (socket.socket.__init__, socket.socket.accept):
                method_code = getattr(method, '__code__', None)
                if method_code is not None:
                    if Path(method_code.co_filename).resolve() != expected:
                        raise RuntimeError('stdlib_socket_method_unverified')
                    self._stdlib_socket_frames.add(method_code)

    def stdlib_socketpair_event(self, event, args):
        """Only CPython's own private pair, never an application connection."""
        if self._stdlib_socketpair_code is None or event not in {'socket.__new__','socket.bind','socket.connect'}:
            return False
        frame = sys._getframe(1)
        if frame.f_code is not Guard.__call__.__code__:
            return False
        frame = frame.f_back
        while frame is not None and frame.f_code is not self._stdlib_socketpair_code:
            if frame.f_code not in self._stdlib_socket_frames:
                return False  # An application callback cannot borrow the scope.
            frame = frame.f_back
        if (frame is None or frame.f_globals is not self._stdlib_socketpair_globals
                or frame.f_globals.get('socket') is not self._stdlib_socket_type):
            return False
        if event == 'socket.__new__':
            allowed = (len(args) == 4 and type(args[0]) is self._stdlib_socket_type
                       and args[1] in self._socket_families
                       and args[2] == self._socket_stream_type and args[3] == 0)
        else:
            sock, address = args
            values = frame.f_locals
            if (type(sock) is not self._stdlib_socket_type or type(address) is not tuple
                    or len(address) < 2 or address[0] not in {'127.0.0.1','::1'}):
                return False
            if event == 'socket.bind':
                allowed = sock is values.get('lsock') and address[1] == 0
            else:
                listener = values.get('lsock')
                allowed = (sock is values.get('csock') and type(listener) is self._stdlib_socket_type
                           and address[:2] == self._native_getsockname(listener)[:2] and address[1] > 0)
        if allowed:
            self.stdlib_socketpair_events += 1
        return allowed

    def deny(self, event, arguments=()):
        self.denials.append(event)
        if len(self.denial_details) < 64:
            # Do not use linecache/traceback extraction inside the audit hook:
            # reading source lines can cause another denied filesystem event.
            frame, stack = sys._getframe(1), []
            while frame is not None and len(stack) < 12:
                stack.append({"file": frame.f_code.co_filename, "line": frame.f_lineno,
                              "function": frame.f_code.co_name})
                frame = frame.f_back
            detail = {"event": event, "stack": stack}
            if event in {"file_read", "file_write"} and len(arguments) == 3:
                path, mode, flags = arguments
                if type(path) is str:
                    detail["path"] = path[:2048]
                if mode is None or type(mode) is str:
                    detail["mode"] = mode
                if type(flags) is int:
                    detail["flags"] = flags
            self.denial_details.append(detail)
        raise PermissionError("isolated_guard_denied:" + event)

    def allowed_source(self, path, directory=False):
        try:
            relative = path.relative_to(self.source_backend)
        except ValueError:
            return False
        if any(part in EXCLUDED or part.startswith(".") for part in relative.parts):
            return False
        if directory:
            return not relative.parts or relative.parts[0] in ("app", "tests", "scripts")
        return public_rel(relative)

    def allowed(self, value, write=False, directory=False):
        try:
            path = filesystem_path(value)
        except (TypeError, ValueError, OSError):
            return False
        if isinstance(path, int):
            return True
        # Runtime libraries may themselves live inside the source's .venv.
        # That does not authorize reading source credentials or data storage.
        source_root = self.source_backend.parent
        if within(path, source_root):
            relative = path.relative_to(source_root)
            if (any(part in {"storage", "exports", "playwright_user_data"}
                    or part == ".env" or part.startswith(".env.")
                    for part in relative.parts)
                    or path.name in {"Cookies", "Cookies-journal", "Local State"}
                    or path.name.startswith("Login Data")):
                return False
        if write:
            return any(within(path, root) for root in (self.work / "t", self.work / "tmp", self.report))
        return (within(path, self.work) or within(path, self.report)
                or path == self.script
                or any(within(path, root) for root in self.runtime)
                or self.verify and self.allowed_source(path, directory=directory))

    def __call__(self, event, args):
        if event.startswith('socket.') and self.stdlib_socketpair_event(event, args):
            return
        if ((event.startswith("socket.") and event != "socket.gethostname")
                or event in {
                    "subprocess.Popen", "os.system", "os.posix_spawn", "os.posix_spawnp",
                    "os.fork", "os.forkpty", "os.exec", "os.spawn", "os.startfile",
                    "os.startfile/2", "ctypes.dlopen", "ctypes.dlsym",
                    "ctypes.dlsym/handle", "ctypes.call_function", "_winapi.CreateProcess",
                }):
            self.deny(event, args)
        if event == "open":
            path, mode, flags = args
            write = bool(isinstance(mode, str) and any(c in mode for c in "wax+"))
            write |= bool(isinstance(flags, int) and flags & (
                os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC))
            if not self.allowed(path, write):
                self.deny("file_write" if write else "file_read", args)
        if event in {
            "os.mkdir", "os.rmdir", "os.remove", "os.unlink", "os.chmod", "os.chown",
            "os.truncate", "os.utime", "shutil.rmtree",
        } and not self.allowed(args[0], True):
            self.deny("file_mutation")
        if event in {"os.rename", "os.replace"} and any(not self.allowed(p, True) for p in args[:2]):
            self.deny("file_mutation")
        if event in {"os.listdir", "os.scandir"} and not self.allowed(args[0], directory=True):
            self.deny("directory_read")
        if event == 'os.link' and self.allow_fixture_hardlinks:
            # Both endpoints must already be writable test-owned locations.
            # Runtime/source/private files remain read-only and cannot be
            # linked into a writable fixture. Symbolic links stay prohibited.
            try:
                source, target = (Path(os.path.abspath(os.fspath(value))) for value in args[:2])
                allowed = (len(args) == 4
                    and all(value is None or type(value) is int and value == -1 for value in args[2:])
                    and self.allowed(source, True) and self.allowed(target, True)
                    and source.resolve() == source and source.parent.resolve() == source.parent
                    and target.resolve() == target and target.parent.resolve() == target.parent
                    and stat.S_ISREG(source.lstat().st_mode) and not source.is_symlink()
                    and not target.exists() and not target.is_symlink())
            except (TypeError, ValueError, OSError):
                allowed = False
            if allowed:
                self.fixture_hardlink_events += 1
                return
        if event in {"os.link", "os.symlink"}:
            self.deny("file_link")
        if event == "sqlite3.connect":
            try:
                database = sqlite_path(args[0])
            except (TypeError, ValueError, OSError):
                self.deny("sqlite_connect")
            else:
                if database is not None and not self.allowed(database, True):
                    self.deny("sqlite_connect")


def preload_stdlib_ctypes(guard):
    """Initialize only CPython's ctypes module, before the strict test hook.

    A narrow bootstrap hook already applies the same file/network/process
    policy. The native exceptions match CPython's initialization: on Windows,
    load kernel32 and resolve GetLastError; on POSIX, open the current-process
    handle. No application module, DPAPI library or DPAPI operation is
    imported/called. The strict hook subsequently denies all new FFI loads
    and symbol resolutions, including application attempts to reuse kernel32.
    """
    expected = Path(sysconfig.get_path("stdlib")).resolve() / "ctypes/__init__.py"
    safe_file(expected)
    if "ctypes" in sys.modules:
        raise RuntimeError("stdlib_ctypes_unexpected_preload")
    if any(name == "app" or name.startswith("app.") or name == "tests"
           or name.startswith("tests.") for name in sys.modules):
        raise RuntimeError("application_imported_before_guard")
    active, events = [True], []

    def stdlib_frame():
        frame = sys._getframe(2)
        while frame is not None:
            # uv may spell the base interpreter through its version alias.
            # Match the same canonical stdlib file, as the socketpair boundary
            # does; the strict post-bootstrap FFI policy is unchanged.
            if (frame.f_code.co_filename == str(expected)
                    or Path(frame.f_code.co_filename).resolve() == expected):
                return True
            frame = frame.f_back
        return False

    def bootstrap(event, args):
        if not active[0]:
            return
        if event == "ctypes.dlopen":
            expected_load = ("kernel32",) if os.name == "nt" else (None,)
            if args != expected_load or not stdlib_frame():
                guard.deny("stdlib_bootstrap_dll_rejected", args)
            events.append({"event": event, "library": expected_load[0]})
            return
        if event == "ctypes.dlsym":
            if (os.name != "nt" or len(args) != 2 or args[1] != "GetLastError"
                    or getattr(args[0], "_name", None) != "kernel32"
                    or not stdlib_frame()):
                guard.deny("stdlib_bootstrap_symbol_rejected", args)
            events.append({"event": event, "library": "kernel32", "symbol": "GetLastError"})
            return
        guard(event, args)

    sys.addaudithook(bootstrap)
    try:
        module = importlib.import_module("ctypes")
        if Path(module.__file__).resolve() != expected:
            raise RuntimeError("stdlib_ctypes_path_mismatch")
        expected_events = ([{"event": "ctypes.dlopen", "library": "kernel32"},
                            {"event": "ctypes.dlsym", "library": "kernel32", "symbol": "GetLastError"}]
                           if os.name == "nt" else [{"event": "ctypes.dlopen", "library": None}])
        if events != expected_events:
            raise RuntimeError("stdlib_ctypes_bootstrap_events_unexpected")
        if any(name == "app" or name.startswith("app.") or name == "tests"
               or name.startswith("tests.") for name in sys.modules):
            raise RuntimeError("application_imported_during_bootstrap")
        return {"path": str(expected), "sha256": sha(expected.read_bytes()),
                "events": events, "application_modules_imported": False,
                "dpapi_library_loaded": False, "phase": "before_strict_test_hook"}
    finally:
        active[0] = False


def sanitize_environment(temporary):
    removed = []
    for key in tuple(os.environ):
        if (re.search(r"(?i)(api_?key|token|secret|password|credential|cookie|session|auth|bearer|proxy|base_url|endpoint)", key)
                or key.upper().endswith(("_URL", "_URI"))
                or key.upper().startswith("PYTHON")):
            os.environ.pop(key, None)
            removed.append(key)
    os.environ.update(
        TEMP=str(temporary), TMP=str(temporary),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1", PY_COLORS="0",
        DATABASE_URL="sqlite:///" + (temporary / "runtime.sqlite3").as_posix(),
        INGESTION_JOB_DB_PATH=str(temporary / "ingestion-jobs.sqlite3"),
    )
    for key in ("CHROMA_PERSIST_DIR", "AUDIO_CACHE_DIR", "VIDEO_CACHE_DIR",
                "PLAYWRIGHT_USER_DATA_DIR", "PLAYWRIGHT_BROWSERS_PATH", "INGESTION_METRICS_DIR"):
        os.environ[key] = str(temporary / key.lower())
    tempfile.tempdir, sys.dont_write_bytecode = str(temporary), True
    return len(removed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--tests", required=True, nargs="+")
    parser.add_argument("--label", required=True)
    parser.add_argument('--allow-stdlib-socketpair', action='store_true',
                        help='Allow only CPython socketpair internals for ASGI event loops; app networking remains denied')
    parser.add_argument('--allow-fixture-hardlinks', action='store_true',
                        help='Allow hardlinks only between writable test-owned temporary files; no source/runtime/symlinks')
    parser.add_argument('--disable-optional-httpx-cli', action='store_true',
                        help='Disable only httpx._main optional console entry point; keep actual HTTP client/SDK and native guard intact')
    parser.add_argument("--output", required=True, type=Path,
                        help="Existing/new private report directory outside the source checkout")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.label):
        parser.error("label must be 1-64 lowercase letters, numbers, '_' or '-'")
    source = Path(os.path.abspath(args.source))
    if source.resolve() != source:
        parser.error("source cannot traverse redirected paths")
    safe_directory(source)
    backend = source / "backend"
    report_root = Path(os.path.abspath(args.output))
    if report_root.resolve() != report_root or within(report_root, source):
        parser.error("output must be a non-redirected private directory outside source")
    sites = tuple(dict.fromkeys(Path(sysconfig.get_path(key)).resolve()
                               for key in ("purelib", "platlib")))
    for site in sites:
        safe_directory(site)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    report_root.mkdir(parents=True, exist_ok=True)
    report = report_root / (args.label + "-" + run_id)
    report.mkdir()
    # Do not let an accidentally configured TEMP point tests into the source.
    temporary_base = next((Path(os.path.abspath(value)).resolve()
        for value in tempfile._candidate_tempdir_list()
        if Path(value).is_dir()
        and not within(Path(os.path.abspath(value)).resolve(), source)), None)
    if temporary_base is None:
        parser.error("no independent temporary directory is available outside source")
    work = Path(tempfile.mkdtemp(prefix="dm-orchestration-guard-", dir=temporary_base)).resolve()
    copy, base, temporary = work / "c", work / "t", work / "tmp"
    copy.mkdir(); temporary.mkdir()
    result = {
        "run_id": run_id, "label": args.label, "source": str(source),
        "copy_root": str(copy), "basetemp": str(base), "temporary": str(temporary),
        "runner_sha256": sha(Path(__file__).read_bytes()),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pytest_exit_code": None, "validation_passed": False,
        "snapshot_tests_passed": False, "guard_denials": [],
        "guard_scope": "Python audit hooks for trusted synthetic tests; not an OS malicious-code sandbox",
        "pytest_optional_current_symlink_disabled": True,
        "pytest_optional_windows_colorama_workaround_disabled": True,
        "dependency_sites": [str(site) for site in sites],
        "interpreter": {"executable": sys.executable, "implementation": sys.implementation.name,
                        "version": list(sys.version_info[:3])},
        "private_source_data_copied": False,
        "optional_httpx_cli_disabled": args.disable_optional_httpx_cli,
        "optional_httpx_cli_tested": False,
        "negative_canaries_passed": 0,
    }
    guard = None
    try:
        paths = inventory(backend)
        selections = selected_tests(args.tests, backend, paths)
        result.update(selected_tests=selections, source_file_count=len(paths))
        before = manifest(backend, paths)
        dump_new(report / "source-before.json", before)
        for path in paths:
            destination = copy / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((backend / path).read_bytes())
        paths_after_copy = inventory(backend)
        after_copy = manifest(backend, paths_after_copy)
        copy_before = manifest(copy, inventory(copy))
        dump_new(report / "source-after-copy.json", after_copy)
        dump_new(report / "copy-before-run.json", copy_before)
        result["source_stable_during_copy"] = before == after_copy == copy_before
        if not result["source_stable_during_copy"]:
            raise RuntimeError("source_changed_during_copy")
        os.chdir(copy)
        result["sensitive_environment_names_removed_count"] = sanitize_environment(temporary)
        runtime = tuple(dict.fromkeys((Path(sys.base_prefix).resolve(), *sites,
            Path(sysconfig.get_path("stdlib")).resolve(),
            Path(sysconfig.get_path("platstdlib")).resolve())))
        kept = [p for p in sys.path if p and any(
            within(Path(p).resolve(), root) for root in runtime)]
        sys.path[:] = list(dict.fromkeys([str(copy), *(str(site) for site in sites), *kept]))
        configuration = work / "pytest.ini"
        configuration.write_text("[pytest]\n", encoding="utf-8")
        guard = Guard(work, report, runtime, backend,
                      allow_stdlib_socketpair=args.allow_stdlib_socketpair,
                      allow_fixture_hardlinks=args.allow_fixture_hardlinks)
        result["stdlib_ctypes_bootstrap"] = preload_stdlib_ctypes(guard)
        sys.addaudithook(guard)
        canaries = [
            ("socket.connect", (None, ("127.0.0.1", 1))),
            ("socket.bind", (None, ("127.0.0.1", 0))),
            ("subprocess.Popen", ("not-launched", [], None, None)),
            ("os.symlink", ("not-linked", str(temporary / "not-created-link"), -1)),
            ("open", (str(source / "backend/.env"), "r", 0)),
            ("open", (str(source / "backend/app/storage/not-written.db"), "w", os.O_WRONLY)),
            ("sqlite3.connect", ((source / "backend/app/storage/not-opened.db").as_uri() + "?mode=ro",)),
            ("ctypes.dlopen", ("crypt32",)),
            ("ctypes.dlsym", (None, "OpenProcess")),
            ("ctypes.dlsym/handle", (0, "OpenProcess")),
            ("ctypes.call_function", (0, ())),
            ("_winapi.CreateProcess", ("not-launched",)),
        ]
        if args.allow_stdlib_socketpair:
            # No application may allocate/bind/connect even on loopback. The
            # stdlib-only positive probe below is a separate verified boundary.
            import socket
            canaries.extend([
                ('socket.__new__', (None, socket.AF_INET, socket.SOCK_STREAM, 0)),
                ('socket.bind', (None, ('127.0.0.1', 0))),
                ('socket.connect', (None, ('203.0.113.1', 443))),
            ])
        if args.allow_fixture_hardlinks:
            dirfd_source = temporary/'fixture-dirfd-canary-source'
            dirfd_source.write_bytes(b'test-only-dirfd-canary')
            canaries.extend([
                ('os.link', (str(backend/'app/main.py'), str(temporary/'not-created-link'), -1, -1)),
                ('os.link', (str(temporary/'not-created-source'), str(backend/'app/not-created-link'), -1, -1)),
                ('os.link', (str(dirfd_source), str(temporary/'not-created-fd-source-link'), 123, -1)),
                ('os.link', (str(dirfd_source), str(temporary/'not-created-fd-destination-link'), -1, 123)),
            ])
        for event, arguments in canaries:
            try:
                sys.audit(event, *arguments)
            except PermissionError:
                result["negative_canaries_passed"] += 1
            else:
                raise RuntimeError("guard_negative_canary_failed:" + event)
        dump_new(report / "negative-canaries.json",
                 {"passed": result["negative_canaries_passed"], "events": [e for e, _ in canaries],
                  "method": "sys.audit invokes the actual guard without touching production paths"})
        result["negative_canaries_expected"] = len(canaries)
        result["original_seven_negative_canaries_passed"] = result["negative_canaries_passed"] >= 7
        result["post_bootstrap_ffi_negative_canaries_passed"] = result["negative_canaries_passed"] == len(canaries)
        result['stdlib_socketpair_allowed'] = args.allow_stdlib_socketpair
        result['fixture_hardlinks_allowed'] = args.allow_fixture_hardlinks
        if args.allow_fixture_hardlinks:
            dirfd_source.unlink()
            first_path, second_path = temporary/'fixture-link-probe', temporary/'fixture-link-probe-copy'
            first_path.write_bytes(b'test-fixture-only')
            os.link(first_path, second_path)
            if not os.path.samefile(first_path, second_path) or second_path.read_bytes() != b'test-fixture-only':
                raise RuntimeError('fixture_hardlink_probe_failed')
            second_path.unlink(); first_path.unlink()
            result['fixture_hardlink_positive_probe_passed'] = True
        if args.allow_stdlib_socketpair:
            first, second = socket.socketpair()
            try:
                first.settimeout(2); second.settimeout(2)
                first.sendall(b'isolated-internal-pair')
                if second.recv(64) != b'isolated-internal-pair':
                    raise RuntimeError('stdlib_socketpair_probe_failed')
                result['stdlib_socketpair_positive_probe_passed'] = True
            finally:
                first.close(); second.close()
        guard.denials.clear()
        guard.denial_details.clear()
        if args.disable_optional_httpx_cli:
            # HTTPX catches this ImportError and keeps its real HTTP client,
            # transports and API. Its optional CLI would otherwise initialize
            # Click's native Windows console at import time. No application
            # module or HTTP method is replaced; this is not a CLI test.
            if 'httpx' in sys.modules or 'httpx._main' in sys.modules:
                raise RuntimeError('httpx_imported_before_optional_cli_boundary')
            sys.modules['httpx._main'] = None
        with (report / "terminal.txt").open("x", encoding="utf-8") as terminal:
            with contextlib.redirect_stdout(terminal), contextlib.redirect_stderr(terminal):
                try:
                    import pytest
                    from _pytest import pathlib as pytest_paths
                    from _pytest import capture as pytest_capture
                    from _pytest import tmpdir as pytest_tmpdir
                    pytest_paths._force_symlink = lambda *unused, **ignored: None
                    # Pytest 9 imports this helper into tmpdir at module load;
                    # disable both optional "current" aliases, never the
                    # application's symlink syscall or its guard.
                    if hasattr(pytest_tmpdir, '_force_symlink'):
                        pytest_tmpdir._force_symlink = pytest_paths._force_symlink
                    # File-only, no-color, capture-disabled output needs no
                    # optional Windows console attachment. Keep the native DLL
                    # guard intact for every application/test ctypes attempt.
                    pytest_capture._colorama_workaround = lambda: None
                    targets = [str(copy / item.split("::")[0]) + (
                        "::" + "::".join(item.split("::")[1:]) if "::" in item else "")
                        for item in selections]
                    result["pytest_exit_code"] = int(pytest.main([
                        "-q", "-s", "-p", "no:cacheprovider",
                        "--log-file", str(temporary / "pytest.log"),
                        "-c", str(configuration), "--rootdir", str(copy),
                        "--confcutdir", str(copy), "--basetemp", str(base), *targets,
                    ]))
                except BaseException:
                    traceback.print_exc()
        guard.verify = True
        after = manifest(backend, inventory(backend))
        copied_after = manifest(copy, inventory(copy))
        dump_new(report / "source-after-run.json", after)
        dump_new(report / "copy-after-run.json", copied_after)
        result.update(
            source_stable_after_run=before == after,
            copy_stable_after_run=copy_before == copied_after,
            guard_denials=list(guard.denials),
            guard_denial_details=list(guard.denial_details),
            runner_stable_after_run=result["runner_sha256"] == sha(Path(__file__).read_bytes()),
            stdlib_socketpair_allowed_events=guard.stdlib_socketpair_events,
            fixture_hardlink_allowed_events=guard.fixture_hardlink_events,
        )
        terminal_text = (report / "terminal.txt").read_text(encoding="utf-8")
        summaries = re.findall(r"^.*\b\d+ (?:passed|failed|error|errors|skipped)\b.*$", terminal_text, re.M)
        result["original_summary"] = summaries[-1] if summaries else None
        result["snapshot_tests_passed"] = (
            result["pytest_exit_code"] == 0 and result["copy_stable_after_run"]
            and not result["guard_denials"] and result["negative_canaries_passed"] == len(canaries))
        result["validation_passed"] = (
            result["snapshot_tests_passed"] and result["source_stable_after_run"]
            and result["runner_stable_after_run"])
    except BaseException:
        result["failure_type"] = sys.exc_info()[0].__name__
        with (report / "runner-error.txt").open("x", encoding="utf-8") as stream:
            traceback.print_exc(file=stream)
        if guard is not None:
            result["guard_denials"] = list(guard.denials)
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    dump_new(report / "report.json", result)
    print(json.dumps({
        "report": str(report / "report.json"), "validation_passed": result["validation_passed"],
        "snapshot_tests_passed": result["snapshot_tests_passed"],
        "pytest_exit_code": result["pytest_exit_code"], "original_summary": result.get("original_summary"),
        "negative_canaries_passed": result["negative_canaries_passed"],
        "guard_denials": list(dict.fromkeys(result["guard_denials"])),
        "guard_denial_count": len(result["guard_denials"]),
    }, ensure_ascii=True))
    return 0 if result["validation_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
