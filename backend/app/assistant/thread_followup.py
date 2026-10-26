"""A normal reply in an already verified public conversation is a new query.

Native parent IDs locate a possible anchor. Only a fresh browser observation
may prove the root, the assistant's published receipt and the new user's reply.
This module never imports a video or publishes a comment.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re


def _normal(text):
    return re.sub(r'\s+', '', text)


class ThreadFollowupCoordinator:
    def __init__(self, browser_lane, bridge):
        self.browser_lane, self.bridge = browser_lane, bridge

    def anchors(self, candidate):
        values = self.bridge.followup_anchors(deepcopy(candidate))
        if not isinstance(values, list) or len(values) > 3:
            return []
        for anchor in values:
            if not isinstance(anchor, dict):
                return []
            root = anchor.get('root_envelope', {})
            if (not isinstance(root, dict)
                    or any(root.get(key) != candidate.get(key) for key in
                        ('sender_profile_url', 'receiver_profile_url', 'video_id'))
                    or root.get('evidence', {}).get('real_mention') is not True
                    or root.get('evidence', {}).get('source_kind') is not None
                    or any(type(anchor.get(key)) is not str or not re.fullmatch(r'[a-f0-9]{32}', anchor[key])
                        for key in ('root_task_id', 'root_agent_task_id'))
                    or type(anchor.get('prior_reply_text')) is not str
                    or hashlib.sha256(anchor['prior_reply_text'].encode()).hexdigest()
                        != anchor.get('prior_reply_sha256')):
                return []
        return values

    def observe(self, candidate, *, checkpoint, read):
        """Fresh proof only; also usable for an explicit paused-owner READ."""
        from app.mention_agent.followup_evidence import make_followup_envelope
        lane = self.browser_lane
        if (candidate.get('source_kind') != 'native_reply_notice_candidate'
                or lane.pending or not checkpoint()):
            return None, None, None, 'native_reply_parent_unresolved'
        anchors = self.anchors(candidate)
        # A hint is never sufficient to choose between otherwise valid roots.
        if len(anchors) != 1:
            return None, None, None, 'native_reply_parent_unresolved'
        anchor = anchors[0]
        detail = read(anchor, deepcopy(candidate))
        if not isinstance(detail, dict) or not checkpoint():
            return None, None, None, 'native_followup_ui_unverified'
        preliminary = lane.registry.intake_binding(candidate['sender_profile_url'],
            candidate['receiver_profile_url'])
        if preliminary is None:
            return None, None, None, 'native_scope_changed'
        envelope = make_followup_envelope(detail, preliminary)
        proof = envelope['evidence']
        try:
            stamp = datetime.fromisoformat(proof['observed_at_utc'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None, None, None, 'native_followup_ui_unverified'
        if not -5 <= age <= 120:
            return None, None, None, 'native_followup_ui_unverified'
        if (any(envelope.get(key) != candidate.get(source) for key, source in (
                ('sender_profile_url', 'sender_profile_url'), ('receiver_profile_url', 'receiver_profile_url'),
                ('video_id', 'video_id')))
                or _normal(envelope['comment_text']) != _normal(candidate['content'])
                or envelope['native_comment_id'] not in (None, candidate['native_comment_id'])
                or proof.get('root_task_id') != anchor['root_task_id']
                or proof.get('root_agent_task_id') != anchor['root_agent_task_id']
                or proof.get('root_reference_id') != anchor['root_envelope']['reference_id']
                or proof.get('prior_reply_sha256') != anchor['prior_reply_sha256']):
            return None, None, None, 'native_followup_ui_unverified'
        if (not checkpoint() or self.bridge.followup_anchor_current(
                anchor['root_task_id'], anchor['prior_reply_sha256']) is not True):
            return None, None, None, 'native_scope_changed'
        return detail, envelope, anchor, None

    def prepare(self, claim, *, checkpoint, read):
        from app.mention_agent.followup_evidence import make_followup_envelope
        lane = self.browser_lane
        if not callable(lane.semantic_resolution_provider):
            return [], None, None, 'native_reply_parent_unresolved'
        detail, envelope, anchor, failure = self.observe(claim['candidate'], checkpoint=checkpoint, read=read)
        if envelope is None:
            return [], None, None, failure

        def authorized():
            return (checkpoint() and self.bridge.followup_anchor_current(
                anchor['root_task_id'], anchor['prior_reply_sha256']) is True)

        if not authorized():
            return [], None, None, 'native_scope_changed'
        # Ordinary replies are not AT messages. They are deliberately planned
        # once AFTER the above actual UI proof; never borrowed from the AT inbox.
        resolution = lane.semantic_resolution_provider(envelope,
            source_checkpoint=authorized)
        if resolution is None or lane.should_stop():
            return [], None, None, None
        if (resolution.action not in {'query', 'clarify'} or resolution.binding is None
                or resolution.authorization is None
                or lane.registry.authorize_snapshot(resolution.authorization) != resolution.binding
                or not authorized()):
            return [], None, None, 'native_scope_changed'
        envelope = make_followup_envelope(detail, resolution.binding)
        thread = {'quality': 'verified_ancestor',
            'root_reference_id': anchor['root_envelope']['reference_id']}
        # There is no actual AT-notification fingerprint for this UI observation.
        # Do not manufacture one merely to fit the old notice ledger.
        lane.pending[envelope['reference_id']] = {'thread': thread, 'fingerprint': None}
        observed = envelope['native_comment_id']
        candidate = claim['candidate']
        correlation = {'source_native_comment_id': candidate['native_comment_id'],
            'observed_native_comment_id': observed,
            'quality': 'native_equal' if observed == candidate['native_comment_id'] else 'semantic_only'}
        return [{'binding': resolution.binding, 'envelope': envelope,
            'video_title': detail.get('video_title', '')[:512], 'kind': resolution.action,
            'notice_fingerprint': None, 'user_id': resolution.user_id,
            'device_id': resolution.device_id, 'authorization': resolution.authorization}], thread, correlation, None

    def authority_current(self, task):
        proof = task.get('envelope', {}).get('evidence', {})
        if proof.get('source_kind') != 'conversation_followup':
            return True
        try:
            return (task.get('kind') in {'query', 'clarify'} and
                self.bridge.followup_anchor_current(proof['root_task_id'],
                    proof['prior_reply_sha256']) is True)
        except Exception:
            return False
