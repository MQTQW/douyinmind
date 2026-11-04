"""Probe the opt-in stdlib private-pair boundary without any app/network call."""
import importlib.util
import json
from pathlib import Path
import socket
import sys
import sysconfig
import tempfile


def main():
    runner = Path(__file__).resolve().with_name('portable_guarded_tests.py')
    spec = importlib.util.spec_from_file_location('pair_guard_under_test', runner)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    work = Path(tempfile.mkdtemp(prefix='dm-pair-boundary-')).resolve()
    report = work/'report'; report.mkdir()
    (work/'t').mkdir(); (work/'tmp').mkdir()
    source = work/'source/backend'; source.mkdir(parents=True)
    runtime = tuple(dict.fromkeys((Path(sys.base_prefix).resolve(),
                    Path(sysconfig.get_path('purelib')).resolve(),
                    Path(sysconfig.get_path('platlib')).resolve())))
    guard = module.Guard(work, report, runtime, source, allow_stdlib_socketpair=True)
    module.preload_stdlib_ctypes(guard)
    # Unconnected fixture allocated before the hook; it is never connected or
    # used for traffic, only supplied to a synthetic allocation audit event.
    fixture_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    sys.addaudithook(guard)
    tests = []

    class UnexpectedConnection(RuntimeError):
        pass

    forged_probe = [False]

    def fail_safe(event, args):
        # If the boundary regresses, stop BEFORE an OS connect can happen.
        if forged_probe[0] and event == 'socket.connect' and args[1][:2] == ('127.0.0.1', 1):
            raise UnexpectedConnection('guard_did_not_reject_forged_private_pair')

    sys.addaudithook(fail_safe)

    def expect_denial(name, action, expected):
        before = len(guard.denials)
        returned = None
        try:
            returned = action()
        except PermissionError as error:
            if str(error) != 'isolated_guard_denied:' + expected:
                raise
            if guard.denials[before:] != [expected]:
                raise RuntimeError('unexpected_boundary_denial_count')
            tests.append({'name': name, 'passed': True, 'denial': expected})
        else:
            if isinstance(returned, tuple):
                for endpoint in returned:
                    endpoint.close()
            raise RuntimeError('boundary_probe_not_denied:' + name)

    for address in (('127.0.0.1', 8000), ('203.0.113.1', 443)):
        expect_denial('ordinary_connect_' + str(address[1]),
                      lambda address=address: sys.audit('socket.connect', None, address), 'socket.connect')
    original_class = socket.socket
    original_getsockname = socket.socket.getsockname
    own_getsockname = 'getsockname' in socket.socket.__dict__

    def constructor_callback(*args, **kwargs):
        return original_class(*args, **kwargs)

    socket.socket = constructor_callback
    try:
        expect_denial('replaced_constructor_inside_pair', socket.socketpair, 'socket.__new__')
    finally:
        socket.socket = original_class

    def profile_callback(frame, event, arg):
        if event == 'call' and frame.f_code is guard._stdlib_socketpair_code:
            sys.audit('socket.__new__', fixture_socket, socket.AF_INET, socket.SOCK_STREAM, 0)

    sys.setprofile(profile_callback)
    try:
        expect_denial('application_profile_frame_inside_pair', socket.socketpair, 'socket.__new__')
    finally:
        sys.setprofile(None)
        fixture_socket.close()

    def application_callback(instance):
        probe = original_class(socket.AF_INET, socket.SOCK_STREAM, 0)
        probe.close()
        return original_getsockname(instance)

    socket.socket.getsockname = application_callback
    try:
        expect_denial('application_frame_inside_pair', socket.socketpair, 'socket.__new__')
    finally:
        if own_getsockname:
            socket.socket.getsockname = original_getsockname
        else:
            del socket.socket.getsockname

    socket.socket.getsockname = lambda instance: ('127.0.0.1', 1)
    forged_probe[0] = True
    try:
        expect_denial('forged_listener_address', socket.socketpair, 'socket.connect')
    finally:
        forged_probe[0] = False
        if own_getsockname:
            socket.socket.getsockname = original_getsockname
        else:
            del socket.socket.getsockname

    first, second = socket.socketpair()
    try:
        first.settimeout(2); second.settimeout(2)
        first.sendall(b'private-pair-only')
        assert second.recv(64) == b'private-pair-only'
        tests.append({'name': 'original_stdlib_private_pair', 'passed': True})
    finally:
        first.close(); second.close()
    result = {'passed': len(tests), 'tests': tests, 'external_connections_attempted': False,
              'private_data_read': False, 'allowed_stdlib_events': guard.stdlib_socketpair_events}
    (report/'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'passed': len(tests), 'report': str(report/'result.json')}))


if __name__ == '__main__':
    main()
