"""Read-only build/freshness diagnostics; never expose exception text or locals."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re

BACKEND = Path(__file__).resolve().parents[2]


def source_version():
    digest = hashlib.sha256()
    count = 0
    try:
        for package in ('assistant', 'mention_agent', 'mention_receiver'):
            for path in sorted((BACKEND / 'app' / package).glob('*.py')):
                relative = path.relative_to(BACKEND).as_posix()
                digest.update(relative.encode('utf-8') + b'\0')
                digest.update(hashlib.sha256(path.read_bytes()).digest())
                count += 1
    except OSError:
        return None
    return digest.hexdigest()[:16] if count else None


def capture_build(role):
    if role not in {'backend', 'helper'}:
        raise ValueError('invalid_runtime_role')
    return {'role': role, 'source_version': source_version(), 'pid': os.getpid(),
            'captured_at': datetime.now(timezone.utc).isoformat()}


# Captured once by each newly loaded process. Reading disk later is deliberately
# a separate value: editing a file never upgrades this stored startup snapshot.
BACKEND_BUILD = capture_build('backend')


def _age(value, now):
    try:
        if not isinstance(value, str): return None
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None: return None
        return round((now - parsed).total_seconds(), 1)
    except (ValueError, OverflowError, TypeError):
        return None


def _build(value, disk):
    if not isinstance(value, dict):
        return {'source_version': None, 'matches_disk': None}
    version = value.get('source_version')
    if not isinstance(version, str) or not re.fullmatch('[a-f0-9]{16}', version):
        return {'source_version': None, 'matches_disk': None}
    return {'source_version': version, 'matches_disk': version == disk if disk else None,
            'captured_at': value.get('captured_at') if isinstance(value.get('captured_at'), str) else None}


def diagnostics(control, counts, *, now=None, backend_build=None, pending_rechecks=0):
    now = now or datetime.now(timezone.utc)
    disk = source_version()
    backend = _build(BACKEND_BUILD if backend_build is None else backend_build, disk)
    helper = _build(control.get('runtime_build'), disk)
    heartbeat_age = _age(control.get('heartbeat_at'), now)
    capture_age = _age(control.get('last_capture_at'), now)
    notices = []
    if control.get('last_capture_unavailable_details', 0) or pending_rechecks:
        notices.append({'code': 'original_detail_unavailable', 'message': '最近一轮有提醒未能核对原评论，已暂缓后重查；这些提醒不计作已收件，也不会阻挡其余可核对指令。'})
    if backend['matches_disk'] is False:
        notices.append({'code': 'backend_version_changed', 'message': '后台启动版本与磁盘不同，请正常重启项目后核对；保存的数据不会因此重置。'})
    if control.get('process_alive') and helper['matches_disk'] is False:
        notices.append({'code': 'helper_version_changed', 'message': '助手仍在运行旧版本。请先正常停止助手，再开启以加载更新。'})
    if control.get('process_alive') and helper['source_version'] is None:
        notices.append({'code': 'helper_version_unrecorded', 'message': '这次助手启动未记录源码版本，不能据此确认已加载更新。'})
    if control.get('state') == 'running':
        if heartbeat_age is None or heartbeat_age < -2 or heartbeat_age > 180:
            notices.append({'code': 'heartbeat_needs_review', 'message': '助手运行报告缺失或已过期，请核对窗口；不会自动重启或重发。'})
        if capture_age is None or capture_age < -2 or capture_age > 180:
            notices.append({'code': 'capture_needs_review', 'message': '近期没有完成通知核对。页面可能仍在处理，请核对助手窗口和原任务。'})
    unknown = sum(counts.get(key, 0) for key in ('delivery_unknown', 'verification_required', 'needs_reconcile'))
    return {'disk_source_version': disk, 'backend_build': backend, 'helper_build': helper,
            'heartbeat_age_seconds': heartbeat_age, 'capture_age_seconds': capture_age,
            'notices': notices,
            'intake_scope': {'poll_seconds': 45, 'visible_candidates_per_batch': 3, 'all_notifications_confirmed': False},
            'unverified_notice_deferrals': pending_rechecks,
            'delivery': {'b_view_visible': counts.get('b_view_visible', 0), 'requires_review': unknown,
                         'recipient_visibility': 'not_automatically_verified'},
            'version_scope': 'source_fingerprint_at_process_start_not_a_guarantee_of_all_loaded_dependencies'}


SAFE_FAILURE_TYPES = frozenset({'ToolExecutionFailure', 'BackendEvidenceError', 'TimeoutError',
    'HTTPError', 'URLError', 'ConnectionError', 'APIConnectionError', 'APITimeoutError',
    'RateLimitError', 'AuthenticationError', 'PermissionDeniedError', 'InternalServerError',
    'IncompleteModelResponse', 'AgentStoreError', 'QueueError', 'StorageGuardError',
    'OperationalError', 'DatabaseError', 'ValueError', 'TypeError', 'KeyError', 'ModelBudgetError'})


def safe_failure_chain(error):
    """Categories only: no str(error), URL, filename, message, input or traceback."""
    result, seen = [], set()
    while error is not None and id(error) not in seen and len(result) < 4:
        seen.add(id(error))
        name = type(error).__name__
        result.append(name if name in SAFE_FAILURE_TYPES else 'OtherError')
        error = error.__cause__ if error.__cause__ is not None else error.__context__
    return result
