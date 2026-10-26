"""Local opt-in wiring for API intake and fresh public-page execution evidence.

No web request can select an account, credential path, transport or target.
An enabled but unavailable native source never silently falls back to UI intake.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from app.mention_agent.contracts import public_profile


class NativeConfigurationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _owned(path, *, required=False):
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink():
        raise NativeConfigurationError('native_configuration_redirected')
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise NativeConfigurationError('native_configuration_not_regular')
    if required and not path.is_file():
        raise NativeConfigurationError('native_configuration_missing')
    return path


def load_native_configuration(state_dir, receiver_profile_url):
    path = _owned(Path(state_dir) / 'native-receiver.json')
    required = _owned(Path(state_dir) / 'native-receiver-required.json')
    if not path.exists():
        native_root = Path(state_dir) / 'native-receiver'
        prior_state = (native_root / 'sources.sqlite3', native_root / 'sources.sqlite3.owner.json')
        if required.exists() or any(item.exists() or item.is_symlink() for item in prior_state):
            raise NativeConfigurationError('native_configuration_required')
        return None
    if path.stat().st_size > 8192:
        raise NativeConfigurationError('native_configuration_too_large')
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        config = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        allowed = {'version', 'enabled', 'receiver_uid', 'receiver_sec_uid',
                   'receiver_profile_url', 'session_source'}
        if not isinstance(config, dict) or set(config) != allowed:
            raise ValueError()
        if type(config['version']) is not int or config['version'] != 1 or type(config['enabled']) is not bool:
            raise ValueError()
        uid, sec = config['receiver_uid'], config['receiver_sec_uid']
        if (not isinstance(uid, str) or not re.fullmatch(r'[0-9]{5,30}', uid)
                or not isinstance(sec, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', sec)):
            raise ValueError()
        expected = 'https://www.douyin.com/user/' + sec
        if (config['receiver_profile_url'] != expected
                or public_profile(receiver_profile_url) != expected
                or public_profile(config['receiver_profile_url']) != expected):
            raise ValueError()
        if config['session_source'] not in {'owned_capsule', 'authorized_existing_capsule_v1'}:
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError, OSError):
        raise NativeConfigurationError('native_configuration_invalid') from None
    return config if config['enabled'] else None


def _visibility_review_options(state_dir):
    """An explicit local opt-in collects inert hints, never authorizes a task."""
    try:
        path=_owned(Path(state_dir)/'native-visibility-review.json')
        if not path.is_file() or path.stat().st_size>256:return {}
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:raise ValueError()
                result[key]=value
            return result
        value=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if (type(value) is not dict or set(value) not in ({'version','enabled'},
                {'version','enabled','automatic_new_hints'}) or type(value['version']) is not int
                or value['version']!=1 or type(value['enabled']) is not bool
                or ('automatic_new_hints' in value and type(value['automatic_new_hints']) is not bool)):
            return {}
        return value
    except Exception:
        return {}


def visibility_review_enabled(state_dir):
    return _visibility_review_options(state_dir).get('enabled') is True


def automatic_visibility_review_enabled(state_dir):
    value=_visibility_review_options(state_dir)
    return value.get('enabled') is True and value.get('automatic_new_hints') is True


def build_intake_lane(adapter, registry, queue, conversation, state_dir):
    from .browser_lane import BrowserLane
    browser_lane = BrowserLane(adapter, registry, queue, conversation)
    config = load_native_configuration(state_dir, adapter.expected_profile_url)
    if config is None:
        return browser_lane
    from .native_lane import NativeLane
    from .native_receiver import NativeReceiver
    from .native_source_store import NativeSourceStore
    from .native_transport import NativeTransport
    from .native_session import NativeSessionVault
    from .native_lifecycle import NativeSessionLifecycle, SameContextCapture, visible_draft_is_empty

    root = Path(state_dir) / 'native-receiver'
    # This capsule was provisioned by the same application's explicitly
    # authorized native-receiver test. Reading it is a normal DPAPI load; no
    # browser key/cookie DB, copy, plaintext export or historical ledger import.
    legacy = None
    if config['session_source'] == 'authorized_existing_capsule_v1':
        storage = Path(__file__).resolve().parents[2] / 'app' / 'storage'
        legacy = storage / 'continuous_testing' / '2026-10-19' / 'nonbrowser-readonly-receiver-2026-10-23' / 'private' / 'b-session.dpapi'
    owned_vault = NativeSessionVault(config['receiver_uid'], private_root=root / 'private')
    legacy_loader = (NativeSessionVault(config['receiver_uid'], private_root=root / 'private',
        legacy_capsule_path=legacy).load if legacy is not None else None)

    capture = SameContextCapture(adapter, config['receiver_profile_url'], config['receiver_uid'],
        draft_is_empty=lambda: visible_draft_is_empty(adapter))
    lifecycle = NativeSessionLifecycle(expected_uid=config['receiver_uid'], owned_vault=owned_vault,
        context_capture=capture, legacy_loader=legacy_loader, require_seal=True)
    store = NativeSourceStore(root / 'sources.sqlite3', config['receiver_uid'],
                               config['receiver_profile_url'], create=False)
    transport = NativeTransport(lifecycle.provider, config['receiver_uid'])

    def profile_allowed(profile):
        user = registry.find_user(profile, config['receiver_profile_url'])
        return bool(user and user['active'] and user['verification_status'] == 'verified')

    from .native_visibility_diagnostics import load_visibility_probe
    probe=load_visibility_probe(state_dir)
    diagnostic_options={'visibility_diagnostic_probe':probe} if probe is not None else {}
    hint_store=None
    if visibility_review_enabled(state_dir):
        from .native_visibility_hint_store import NativeVisibilityHintStore
        hint_path=root/'visibility-hints.sqlite3'
        hint_store=NativeVisibilityHintStore(hint_path,config['receiver_uid'],config['receiver_profile_url'],
            source_store=store,create=not hint_path.exists())
        if automatic_visibility_review_enabled(state_dir):hint_store.enable_new_hint_review()
        diagnostic_options['visibility_review_hint_sink']=hint_store.record_hints
    receiver = NativeReceiver(transport, store, receiver_uid=config['receiver_uid'],
        receiver_sec_uid=config['receiver_sec_uid'], profile_allowed=profile_allowed, allow_live=True,
        **diagnostic_options)
    lane=NativeLane(browser_lane, receiver, store, session_lifecycle=lifecycle)
    lane.visibility_hint_store=hint_store
    return lane
