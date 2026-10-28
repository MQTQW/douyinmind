"""Bounded dual-channel native intake. Scheduling remains in the existing app."""
from __future__ import annotations

import re
import time
import logging

from .native_events import (NativeEventError, canonical_profile, project_page,
    project_ui_review_hints, project_semantic_messages, notice_identifier_diagnostic)
from .native_source_store import NativeSourceStore, NativeStoreError
from .native_transport import NativeTransportError, decimal_id, project_native_transport_facts

_LOG = logging.getLogger(__name__)


class NativeReceiverError(RuntimeError):
    def __init__(self, code, *, transport_facts=None):
        self._transport_facts = tuple(project_native_transport_facts(transport_facts).items())
        self.code = code if type(code) is str and re.fullmatch(r"[a-z_]{1,80}", code) else "native_receiver_error"
        super().__init__(self.code)

    @property
    def transport_facts(self):
        return dict(self._transport_facts)


class NativeReceiver:
    """The owner thread explicitly calls poll; construction never loads auth.

    Initial scans in BOTH channels are permanent observations.  Only notices
    newer than the persisted post-baseline arm fence may enter the UI lane;
    missing native timestamps stay historical.  No native record is dispatchable.
    """
    def __init__(self, transport, store: NativeSourceStore, *, receiver_uid,
                 receiver_sec_uid, profile_allowed, allow_live=False, clock=None,
                 visibility_diagnostic_probe=None, visibility_review_hint_sink=None,
                 semantic_message_sink=None):
        self.transport, self.store = transport, store
        self.receiver_uid = decimal_id(receiver_uid)
        self.receiver_profile_url = canonical_profile(receiver_sec_uid)
        self._receiver_sec_uid = receiver_sec_uid
        if (store.receiver_uid != self.receiver_uid or store.receiver_profile_url != self.receiver_profile_url
                or not callable(profile_allowed) or type(allow_live) is not bool
                or (visibility_review_hint_sink is not None and not callable(visibility_review_hint_sink))
                or (semantic_message_sink is not None and not callable(semantic_message_sink))):
            raise NativeReceiverError("native_receiver_configuration_mismatch")
        self._allowed = profile_allowed
        self._allow_live = allow_live
        self._clock = clock or time.time
        self._next_channel = 9
        self._last_code = "not_started"
        self.visibility_diagnostic_probe = visibility_diagnostic_probe
        self.last_visibility_diagnostics = []
        self.visibility_review_hint_sink = visibility_review_hint_sink
        self.last_visibility_review_sink_error_code = None
        self.semantic_message_sink = semantic_message_sink
        self.last_semantic_message_sink_error_code = None

    def poll(self, max_pages=2):
        if type(max_pages) is not int or not 1 <= max_pages <= 8:
            raise NativeReceiverError("invalid_poll_limit")
        self.last_visibility_diagnostics = []
        self.last_visibility_review_sink_error_code = None
        self.last_semantic_message_sink_error_code = None
        def observe_visibility(value):
            if len(self.last_visibility_diagnostics) < 16:
                self.last_visibility_diagnostics.append(value)
        candidates = []
        try:
            if self.transport.verify_identity() != self.receiver_uid:
                raise NativeReceiverError("session_identity_mismatch")
            groups = self.transport.get_groups()
            if type(groups) is not dict or set(groups) != {9, 7}:
                raise NativeReceiverError("group_schema_unknown")
            # Fair alternating pages; unfinished channel cursors live in SQLite.
            for _ in range(max_pages):
                if self.store.pending_count() >= 100:
                    self._last_code = "native_backpressure"
                    break
                channel = self._next_channel
                self._next_channel = 7 if channel == 9 else 9
                context = self.store.prepare_channel(channel, groups[channel])
                page = self.transport.read_page(groups[channel], context["cursor"])
                projection = project_page(page, channel, receiver_uid=self.receiver_uid,
                                          receiver_sec_uid=self._receiver_sec_uid,
                                          profile_allowed=self._allowed,
                                          diagnostic_probe=self.visibility_diagnostic_probe,
                                          diagnostic_observer=observe_visibility)
                # A response is not committed if the actual session changed.
                if self.transport.verify_identity() != self.receiver_uid:
                    raise NativeReceiverError("session_identity_mismatch")
                self.store.set_verified()
                candidates.extend(self.store.record_page(
                    context, projection, next_cursor=(page["min_time"], page["max_time"]),
                    has_more=page["has_more"], allow_live=self._allow_live))
                self.store.arm_if_ready(self._allow_live)
                if self.visibility_review_hint_sink is not None or self.semantic_message_sink is not None:
                    # Normal page+skip commit and the actual post-read B UID
                    # GET precede this separate ledger write. The two stores
                    # are not an atomic transaction; a failure never falls
                    # back to UI or turns a skipped fact into a candidate.
                    hints = project_ui_review_hints(page, channel,
                        receiver_uid=self.receiver_uid, receiver_sec_uid=self._receiver_sec_uid,
                        profile_allowed=self._allowed)
                    if hints and self.visibility_review_hint_sink is not None:
                        try:
                            self.visibility_review_hint_sink(hints, page_fingerprint=projection['fingerprint'])
                        except Exception:
                            self.last_visibility_review_sink_error_code = 'visibility_review_hint_sink_unavailable'
                    if self.semantic_message_sink is not None:
                        # Meaning is a separate, non-executable inbox. The
                        # strict projection contains only registered authors;
                        # unknown visibility is not invented as public proof.
                        messages = project_semantic_messages(page,channel,receiver_uid=self.receiver_uid,
                            receiver_sec_uid=self._receiver_sec_uid,profile_allowed=self._allowed)
                        if messages:
                            try:
                                for offset in range(0,len(messages),16):
                                    self.semantic_message_sink(messages[offset:offset+16],
                                        page_fingerprint=projection['fingerprint'])
                            except Exception:
                                self.last_semantic_message_sink_error_code = 'semantic_message_sink_unavailable'
            else:
                self._last_code = "ok"
            # Empty/backpressured successful rounds still verify identity.
            self.store.set_verified()
            if self._last_code not in ("native_backpressure", "ok"):
                self._last_code = "ok"
            return candidates
        except (NativeTransportError, NativeEventError, NativeStoreError, NativeReceiverError) as error:
            self._last_code = error.code
            try:
                self.store.pause(error.code)
            except NativeStoreError:
                pass
            diagnostic = notice_identifier_diagnostic(error)
            if diagnostic is not None:
                try:
                    _LOG.warning('Native notice identifier rejected: field_role=%s value_kind=%s problem=%s',
                                 diagnostic['field_role'], diagnostic['value_kind'], diagnostic['problem'])
                except Exception:
                    pass  # Diagnostics never replace the original rejection.
                try:
                    self.store.record_notice_identifier_diagnostic(diagnostic)
                except Exception:
                    pass  # Local diagnostic storage cannot grant or recover intake.
            raise NativeReceiverError(error.code, transport_facts=getattr(error, "transport_facts", None)) from None
        except Exception:
            self._last_code = "native_receiver_error"
            try:
                self.store.pause(self._last_code)
            except Exception:
                pass
            raise NativeReceiverError(self._last_code) from None

    poll_once = poll

    def reverify_identity_checkpoint(self):
        """One strict identity GET, independent of notice pagination.

        The owner lane decides whether its still-live unplanned claim has only
        aged past receipt freshness. This does not admit it, extend its lease,
        recapture browser credentials, read groups/pages or advance a cursor.
        """
        try:
            if self.transport.verify_identity() != self.receiver_uid:
                raise NativeReceiverError("session_identity_mismatch")
            self.store.set_verified()
            self._last_code = "ok"
            return True
        except (NativeTransportError, NativeStoreError, NativeReceiverError) as error:
            self._last_code = error.code
            try:
                self.store.pause(error.code)
            except Exception:
                pass
            raise NativeReceiverError(error.code, transport_facts=getattr(error, "transport_facts", None)) from None
        except Exception:
            self._last_code = "native_receiver_error"
            try:
                self.store.pause(self._last_code)
            except Exception:
                pass
            raise NativeReceiverError(self._last_code) from None

    def safe_status(self):
        return {"code": self._last_code, **self.store.safe_status()}
