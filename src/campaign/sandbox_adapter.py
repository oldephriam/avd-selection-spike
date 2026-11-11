from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import json
import re
import threading
import unicodedata
import uuid
from collections.abc import Callable
from typing import Protocol

from .roster import normalize_us_number


_SYNTHETIC_TEST_NUMBER = re.compile(r"\+120855501[0-9]{2}\Z")
_KEY_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_CAMPAIGN_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_SOURCE_REF = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_REASON_CODE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


@dataclass(frozen=True, slots=True)
class RecipientReferences:
    campaign_subject_ref: str
    suppression_ref: str


class HmacReferenceMapper:
    """Purpose-separated HMAC references for synthetic sandbox recipients."""

    def __init__(
        self,
        *,
        consent_key_id: str,
        consent_key: bytes,
        suppression_key_id: str,
        suppression_key: bytes,
    ) -> None:
        if (
            not isinstance(consent_key_id, str)
            or not isinstance(suppression_key_id, str)
            or not _KEY_ID.fullmatch(consent_key_id)
            or not _KEY_ID.fullmatch(suppression_key_id)
        ):
            raise ValueError("key IDs must be 1-64 safe ASCII characters")
        if consent_key_id == suppression_key_id:
            raise ValueError("consent and suppression key IDs must be distinct")
        if not isinstance(consent_key, bytes) or len(consent_key) < 32:
            raise ValueError("consent HMAC key must contain at least 32 bytes")
        if not isinstance(suppression_key, bytes) or len(suppression_key) < 32:
            raise ValueError("suppression HMAC key must contain at least 32 bytes")
        if hmac.compare_digest(consent_key, suppression_key):
            raise ValueError("consent and suppression HMAC keys must be distinct")
        self._consent_key_id = consent_key_id
        self._consent_key = consent_key
        self._suppression_key_id = suppression_key_id
        self._suppression_key = suppression_key

    def for_test_recipient(self, campaign_id: str, recipient: str) -> RecipientReferences:
        if not isinstance(campaign_id, str) or not _CAMPAIGN_ID.fullmatch(campaign_id):
            raise ValueError("campaign ID must be 1-128 safe ASCII characters")
        if not isinstance(recipient, str):
            raise ValueError("recipient must be a string")
        canonical = normalize_us_number(recipient)
        if _SYNTHETIC_TEST_NUMBER.fullmatch(canonical) is None:
            raise ValueError("sandbox adapter accepts synthetic test numbers only")
        encoded_number = canonical.encode("ascii")
        campaign_message = (
            b"sms-consent:v1\0" + campaign_id.encode("ascii") + b"\0" + encoded_number
        )
        suppression_message = b"sms-global-suppression:v1\0" + encoded_number
        consent_digest = hmac.new(self._consent_key, campaign_message, hashlib.sha256).hexdigest()
        suppression_digest = hmac.new(
            self._suppression_key, suppression_message, hashlib.sha256
        ).hexdigest()
        return RecipientReferences(
            campaign_subject_ref=f"hmac-sha256:{self._consent_key_id}:{consent_digest}",
            suppression_ref=f"hmac-sha256:{self._suppression_key_id}:{suppression_digest}",
        )


@dataclass(frozen=True, slots=True)
class ConsentLedgerEvent:
    event_id: str
    event_type: str
    campaign_id: str | None
    campaign_subject_ref: str | None
    suppression_ref: str | None
    occurred_at: str
    source_ref: str
    consent_text_sha256: str | None
    previous_hash: str
    event_hash: str


class ConsentLedger:
    """Process-local, hash-chained ledger for synthetic sandbox identities only."""

    GENESIS = "0" * 64
    _HASH = re.compile(r"[0-9a-f]{64}\Z")
    _OPT_OUT_SOURCES = frozenset({"operator", "inbound_keyword", "provider_notice"})

    def __init__(
        self,
        references: HmacReferenceMapper,
        *,
        lock: threading.RLock | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._references = references
        self._lock = lock or threading.RLock()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._events: list[ConsentLedgerEvent] = []

    @property
    def events(self) -> tuple[ConsentLedgerEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def _utc(self, value: datetime) -> str:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware")
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("consent ledger clock must return a timezone-aware datetime")
        utc_value = value.astimezone(timezone.utc)
        if utc_value > now.astimezone(timezone.utc):
            raise ValueError("event timestamp cannot be in the future")
        return utc_value.isoformat()

    @staticmethod
    def _canonical(record: dict[str, object]) -> bytes:
        return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")

    def _append(
        self,
        *,
        event_type: str,
        campaign_id: str | None,
        campaign_subject_ref: str | None,
        suppression_ref: str | None,
        occurred_at: str,
        source_ref: str,
        consent_text_sha256: str | None,
    ) -> ConsentLedgerEvent:
        previous = self._events[-1].event_hash if self._events else self.GENESIS
        event_id = str(uuid.uuid4())
        record: dict[str, object] = {
            "event_id": event_id,
            "event_type": event_type,
            "campaign_id": campaign_id,
            "campaign_subject_ref": campaign_subject_ref,
            "suppression_ref": suppression_ref,
            "occurred_at": occurred_at,
            "source_ref": source_ref,
            "consent_text_sha256": consent_text_sha256,
            "previous_hash": previous,
        }
        event_hash = hashlib.sha256(self._canonical(record)).hexdigest()
        event = ConsentLedgerEvent(
            event_id=event_id,
            event_type=event_type,
            campaign_id=campaign_id,
            campaign_subject_ref=campaign_subject_ref,
            suppression_ref=suppression_ref,
            occurred_at=occurred_at,
            source_ref=source_ref,
            consent_text_sha256=consent_text_sha256,
            previous_hash=previous,
            event_hash=event_hash,
        )
        self._events.append(event)
        return event

    def _reject_out_of_order_consent(
        self, campaign_id: str, campaign_subject_ref: str, occurred_at: str
    ) -> None:
        new_time = datetime.fromisoformat(occurred_at)
        for event in reversed(self._events):
            if (
                event.event_type in {"GRANT", "WITHDRAWAL"}
                and event.campaign_id == campaign_id
                and event.campaign_subject_ref == campaign_subject_ref
            ):
                previous_time = datetime.fromisoformat(event.occurred_at)
                if new_time <= previous_time:
                    raise ValueError("consent events must be recorded in strictly increasing time order")
                return

    def verify_integrity(self) -> bool:
        with self._lock:
            previous = self.GENESIS
            seen: set[str] = set()
            for event in self._events:
                if event.event_id in seen or event.previous_hash != previous:
                    return False
                record = {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "campaign_id": event.campaign_id,
                    "campaign_subject_ref": event.campaign_subject_ref,
                    "suppression_ref": event.suppression_ref,
                    "occurred_at": event.occurred_at,
                    "source_ref": event.source_ref,
                    "consent_text_sha256": event.consent_text_sha256,
                    "previous_hash": event.previous_hash,
                }
                if not hmac.compare_digest(hashlib.sha256(self._canonical(record)).hexdigest(), event.event_hash):
                    return False
                seen.add(event.event_id)
                previous = event.event_hash
            return True

    def record_grant(
        self,
        campaign_id: str,
        recipient: str,
        consent_text_sha256: str,
        *,
        affirmative: bool,
        captured_at: datetime,
        source_ref: str,
    ) -> ConsentLedgerEvent:
        if affirmative is not True:
            raise ValueError("explicit affirmative consent is required")
        if not isinstance(consent_text_sha256, str) or not self._HASH.fullmatch(consent_text_sha256):
            raise ValueError("consent disclosure must be identified by a SHA-256 hex digest")
        if not isinstance(source_ref, str) or not _SOURCE_REF.fullmatch(source_ref):
            raise ValueError("consent source must be an opaque safe reference")
        occurred_at = self._utc(captured_at)
        refs = self._references.for_test_recipient(campaign_id, recipient)
        with self._lock:
            if not self.verify_integrity():
                raise RuntimeError("consent ledger integrity check failed")
            self._reject_out_of_order_consent(campaign_id, refs.campaign_subject_ref, occurred_at)
            event = self._append(
                event_type="GRANT",
                campaign_id=campaign_id,
                campaign_subject_ref=refs.campaign_subject_ref,
                suppression_ref=None,
                occurred_at=occurred_at,
                source_ref=source_ref.strip(),
                consent_text_sha256=consent_text_sha256,
            )
            return event

    def record_opt_out(self, recipient: str, *, occurred_at: datetime, source: str) -> ConsentLedgerEvent:
        if not isinstance(source, str) or source not in self._OPT_OUT_SOURCES:
            raise ValueError("unsupported opt-out source")
        timestamp = self._utc(occurred_at)
        refs = self._references.for_test_recipient("global-suppression", recipient)
        with self._lock:
            if not self.verify_integrity():
                raise RuntimeError("consent ledger integrity check failed")
            event = self._append(
                event_type="OPT_OUT",
                campaign_id=None,
                campaign_subject_ref=None,
                suppression_ref=refs.suppression_ref,
                occurred_at=timestamp,
                source_ref=source,
                consent_text_sha256=None,
            )
            return event

    def withdraw_consent(
        self,
        campaign_id: str,
        recipient: str,
        *,
        occurred_at: datetime,
        source_ref: str,
    ) -> ConsentLedgerEvent:
        if not isinstance(source_ref, str) or not _SOURCE_REF.fullmatch(source_ref):
            raise ValueError("withdrawal source must be an opaque safe reference")
        timestamp = self._utc(occurred_at)
        refs = self._references.for_test_recipient(campaign_id, recipient)
        with self._lock:
            if not self.verify_integrity():
                raise RuntimeError("consent ledger integrity check failed")
            self._reject_out_of_order_consent(campaign_id, refs.campaign_subject_ref, timestamp)
            return self._append(
                event_type="WITHDRAWAL",
                campaign_id=campaign_id,
                campaign_subject_ref=refs.campaign_subject_ref,
                suppression_ref=None,
                occurred_at=timestamp,
                source_ref=source_ref,
                consent_text_sha256=None,
            )

    def is_eligible(self, campaign_id: str, recipient: str) -> bool:
        return self.current_grant(campaign_id, recipient) is not None

    def current_grant(self, campaign_id: str, recipient: str) -> ConsentLedgerEvent | None:
        refs = self._references.for_test_recipient(campaign_id, recipient)
        with self._lock:
            if not self.verify_integrity():
                raise RuntimeError("consent ledger integrity check failed")
            suppressed = any(
                event.event_type == "OPT_OUT" and event.suppression_ref == refs.suppression_ref
                for event in self._events
            )
            if suppressed:
                return None
            for event in reversed(self._events):
                if (
                    event.event_type in {"GRANT", "WITHDRAWAL"}
                    and event.campaign_id == campaign_id
                    and event.campaign_subject_ref == refs.campaign_subject_ref
                ):
                    return event if event.event_type == "GRANT" else None
            return None


CONFIRMATION_PHRASE = "CONFIRM ONE TEXT"


class DispatchBlocked(RuntimeError):
    pass


class RateLimitExceeded(DispatchBlocked):
    pass


class AttemptState(str, Enum):
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    ACCEPTED_BY_PROVIDER = "ACCEPTED_BY_PROVIDER"
    DEFINITIVELY_REJECTED = "DEFINITIVELY_REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILED_ACCEPTED = "RECONCILED_ACCEPTED"
    RECONCILED_NOT_ACCEPTED = "RECONCILED_NOT_ACCEPTED"


class MockProviderOutcome(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    AMBIGUOUS = "AMBIGUOUS"


class MockProviderRejected(RuntimeError):
    pass


class MockProviderAmbiguous(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    max_campaign_attempts: int
    campaign_window_seconds: int
    max_recipient_attempts: int
    recipient_window_seconds: int
    policy_version: str = "sandbox-v1"

    def __post_init__(self) -> None:
        if type(self.max_campaign_attempts) is not int or self.max_campaign_attempts < 1:
            raise ValueError("campaign attempt limit must be a positive integer")
        if type(self.max_recipient_attempts) is not int or self.max_recipient_attempts < 1:
            raise ValueError("recipient attempt limit must be a positive integer")
        if type(self.campaign_window_seconds) is not int or self.campaign_window_seconds < 1:
            raise ValueError("campaign rate window must be positive seconds")
        if type(self.recipient_window_seconds) is not int or self.recipient_window_seconds < 1:
            raise ValueError("recipient rate window must be positive seconds")
        if not isinstance(self.policy_version, str) or not _SOURCE_REF.fullmatch(self.policy_version):
            raise ValueError("rate policy version must be a safe opaque identifier")


@dataclass(frozen=True, slots=True)
class Preview:
    preview_id: str
    campaign_id: str
    recipient: str
    display_name: str
    message: str
    campaign_subject_ref: str
    payload_sha256: str
    consent_event_id: str
    consent_event_hash: str
    snapshot_id: str
    roster_index: int
    created_at: datetime
    expires_at: datetime
    kill_generation: int


@dataclass(frozen=True, slots=True)
class Confirmation:
    confirmation_id: str
    preview_id: str
    campaign_id: str
    campaign_subject_ref: str
    payload_sha256: str
    consent_event_id: str
    consent_event_hash: str
    snapshot_id: str
    roster_index: int
    operator_id: str
    confirmed_at: datetime
    expires_at: datetime
    kill_generation: int


@dataclass(frozen=True, slots=True)
class MockProviderRequest:
    attempt_id: str
    campaign_id: str
    campaign_subject_ref: str
    payload_sha256: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class MockProviderReceipt:
    provider_reference: str


class MockProviderPort(Protocol):
    @property
    def submissions(self) -> tuple[MockProviderRequest, ...]: ...

    def submit_mock(self, request: MockProviderRequest) -> MockProviderReceipt: ...


class InMemoryMockProvider:
    """Deterministic in-process mock. It has no network or message-delivery code."""

    def __init__(self) -> None:
        self._submissions: list[MockProviderRequest] = []
        self._outcomes: list[MockProviderOutcome] = []
        self._lock = threading.Lock()

    @property
    def submissions(self) -> tuple[MockProviderRequest, ...]:
        with self._lock:
            return tuple(self._submissions)

    def queue_outcome(self, outcome: MockProviderOutcome) -> None:
        with self._lock:
            self._outcomes.append(MockProviderOutcome(outcome))

    def submit_mock(self, request: MockProviderRequest) -> MockProviderReceipt:
        with self._lock:
            self._submissions.append(request)
            outcome = self._outcomes.pop(0) if self._outcomes else MockProviderOutcome.ACCEPT
        if outcome is MockProviderOutcome.REJECT:
            raise MockProviderRejected("mock provider definitively rejected the request")
        if outcome is MockProviderOutcome.AMBIGUOUS:
            raise MockProviderAmbiguous("mock provider acknowledgment was lost")
        return MockProviderReceipt(provider_reference=f"mock-{request.attempt_id}")


class KillSwitch:
    """Default-disabled in-memory control with a generation fence."""

    def __init__(self, lock: threading.RLock, clock: Callable[[], datetime]) -> None:
        self._lock = lock
        self._clock = clock
        self._enabled = False
        self._generation = 0
        self._events: list[KillSwitchEvent] = []

    @property
    def events(self) -> tuple[KillSwitchEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def _event_time(self) -> datetime:
        occurred_at = self._clock()
        if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise RuntimeError("kill-switch clock must be timezone-aware")
        return occurred_at.astimezone(timezone.utc)

    def _record(
        self, action: str, generation: int, actor: str,
        second_approver: str | None, reason: str, occurred_at: datetime
    ) -> None:
        self._events.append(KillSwitchEvent(
            generation=generation,
            action=action,
            occurred_at=occurred_at,
            actor=actor,
            second_approver=second_approver,
            reason=reason,
        ))

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def enable(self, *, actor: str, second_approver: str) -> None:
        if (
            not isinstance(actor, str) or not _SOURCE_REF.fullmatch(actor)
            or not isinstance(second_approver, str) or not _SOURCE_REF.fullmatch(second_approver)
            or actor == second_approver
        ):
            raise ValueError("enabling requires two distinct named approvers")
        with self._lock:
            if self._enabled:
                raise ValueError("dispatch is already enabled")
            occurred_at = self._event_time()
            generation = self._generation + 1
            self._record("ENABLE", generation, actor, second_approver, "two-person approval", occurred_at)
            self._generation = generation
            self._enabled = True

    def disable(self, *, actor: str, reason: str) -> None:
        if (
            not isinstance(actor, str) or not _SOURCE_REF.fullmatch(actor)
            or not isinstance(reason, str) or not _REASON_CODE.fullmatch(reason)
        ):
            raise ValueError("disabling requires an actor and controlled reason code")
        with self._lock:
            occurred_at = self._event_time()
            generation = self._generation + 1
            self._record("DISABLE", generation, actor, None, reason, occurred_at)
            self._generation = generation
            self._enabled = False


@dataclass(frozen=True, slots=True)
class KillSwitchEvent:
    generation: int
    action: str
    occurred_at: datetime
    actor: str
    second_approver: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_id: str
    campaign_id: str
    campaign_subject_ref: str
    payload_sha256: str
    state: AttemptState
    attempt_started_at: datetime
    confirmation_id: str
    consent_event_id: str
    consent_event_hash: str
    snapshot_id: str
    roster_index: int
    kill_generation: int
    policy_version: str
    linked_prior_attempt_id: str | None = None
    resolved_at: datetime | None = None
    provider_reference: str | None = None
    reconciliation_evidence_ref: str | None = None
    reconciled_by: str | None = None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class SandboxDispatchLoop:
    """Fail-closed, process-local dispatcher that accepts only the built-in mock."""

    def __init__(
        self,
        references: HmacReferenceMapper,
        rate_policy: RateLimitPolicy,
        *,
        provider: MockProviderPort | None = None,
        clock: Callable[[], datetime] = _now_utc,
        confirmation_ttl_seconds: int = 600,
    ) -> None:
        selected_provider = provider if provider is not None else InMemoryMockProvider()
        if type(selected_provider) is not InMemoryMockProvider:
            raise TypeError("sandbox loop accepts only InMemoryMockProvider")
        if type(confirmation_ttl_seconds) is not int or confirmation_ttl_seconds < 1:
            raise ValueError("confirmation TTL must be positive seconds")
        self._lock = threading.RLock()
        self._clock = clock
        self._references = references
        self._consent_ledger = ConsentLedger(references, lock=self._lock, clock=clock)
        self._kill_switch = KillSwitch(self._lock, self._clock)
        self._rate_policy = rate_policy
        self._provider = selected_provider
        self._confirmation_ttl = timedelta(seconds=confirmation_ttl_seconds)
        self._previews: dict[str, Preview] = {}
        self._confirmations: dict[str, Confirmation] = {}
        self._confirmed_previews: set[str] = set()
        self._used_confirmations: set[str] = set()
        self._rate_uses: list[tuple[str, str, datetime]] = []
        self._attempts: dict[str, AttemptRecord] = {}
        self._attempt_history: list[AttemptRecord] = []

    @property
    def attempts(self) -> tuple[AttemptRecord, ...]:
        with self._lock:
            return tuple(self._attempts.values())

    @property
    def attempt_history(self) -> tuple[AttemptRecord, ...]:
        with self._lock:
            return tuple(self._attempt_history)

    @property
    def provider(self) -> MockProviderPort:
        return self._provider

    @property
    def consent_ledger(self) -> ConsentLedger:
        return self._consent_ledger

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    @property
    def rate_policy(self) -> RateLimitPolicy:
        return self._rate_policy

    @staticmethod
    def _safe_display(value: str, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required")
        if any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in value):
            raise ValueError(f"{label} contains unsafe formatting characters")
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("sandbox clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def create_preview(
        self,
        campaign_id: str,
        recipient: str,
        display_name: str,
        message: str,
        *,
        snapshot_id: str,
        roster_index: int,
    ) -> Preview:
        display_name = self._safe_display(display_name, "display name")
        message = self._safe_display(message, "message")
        if not isinstance(snapshot_id, str) or not _SOURCE_REF.fullmatch(snapshot_id):
            raise ValueError("snapshot ID is required")
        if type(roster_index) is not int or roster_index < 0:
            raise ValueError("roster index must be a nonnegative integer")
        canonical_recipient = normalize_us_number(recipient)
        refs = self._references.for_test_recipient(campaign_id, canonical_recipient)
        now = self._now()
        with self._lock:
            if not self.kill_switch.enabled:
                raise DispatchBlocked("kill switch is disabled; preview is not allowed")
            grant = self.consent_ledger.current_grant(campaign_id, canonical_recipient)
            if grant is None:
                raise DispatchBlocked("recipient lacks current consent or is suppressed")
            preview = Preview(
                preview_id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                recipient=canonical_recipient,
                display_name=display_name,
                message=message,
                campaign_subject_ref=refs.campaign_subject_ref,
                payload_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
                consent_event_id=grant.event_id,
                consent_event_hash=grant.event_hash,
                snapshot_id=snapshot_id,
                roster_index=roster_index,
                created_at=now,
                expires_at=now + self._confirmation_ttl,
                kill_generation=self.kill_switch.generation,
            )
            self._previews[preview.preview_id] = preview
            return preview

    def confirm(self, preview: Preview, phrase: str, *, operator_id: str) -> Confirmation:
        with self._lock:
            issued = self._previews.get(preview.preview_id) if isinstance(preview, Preview) else None
            if issued is None or issued != preview:
                raise DispatchBlocked("preview is unknown or altered")
            if preview.preview_id in self._confirmed_previews:
                raise DispatchBlocked("preview already has a confirmation")
            now = self._now()
            if now >= preview.expires_at:
                raise DispatchBlocked("preview expired")
            if phrase != CONFIRMATION_PHRASE:
                raise DispatchBlocked("exact per-recipient confirmation phrase required")
            if not isinstance(operator_id, str) or not _SOURCE_REF.fullmatch(operator_id):
                raise DispatchBlocked("operator identity is required")
            if not self.kill_switch.enabled or self.kill_switch.generation != preview.kill_generation:
                raise DispatchBlocked("kill switch is disabled or preview generation is stale")
            grant = self.consent_ledger.current_grant(preview.campaign_id, preview.recipient)
            if grant is None or (grant.event_id, grant.event_hash) != (
                preview.consent_event_id, preview.consent_event_hash
            ):
                raise DispatchBlocked("consent changed or recipient became suppressed")
            confirmation = Confirmation(
                confirmation_id=str(uuid.uuid4()),
                preview_id=preview.preview_id,
                campaign_id=preview.campaign_id,
                campaign_subject_ref=preview.campaign_subject_ref,
                payload_sha256=preview.payload_sha256,
                consent_event_id=preview.consent_event_id,
                consent_event_hash=preview.consent_event_hash,
                snapshot_id=preview.snapshot_id,
                roster_index=preview.roster_index,
                operator_id=operator_id.strip(),
                confirmed_at=now,
                expires_at=preview.expires_at,
                kill_generation=preview.kill_generation,
            )
            self._confirmations[confirmation.confirmation_id] = confirmation
            self._confirmed_previews.add(preview.preview_id)
            return confirmation

    def _reserve_rate(self, campaign_id: str, subject_ref: str, now: datetime) -> None:
        campaign_window = timedelta(seconds=self._rate_policy.campaign_window_seconds)
        recipient_window = timedelta(seconds=self._rate_policy.recipient_window_seconds)
        active = [(c, s, at) for c, s, at in self._rate_uses if now - at < max(campaign_window, recipient_window)]
        self._rate_uses = active
        campaign_uses = [at for c, _, at in active if c == campaign_id and now - at < campaign_window]
        recipient_uses = [at for c, s, at in active if c == campaign_id and s == subject_ref and now - at < recipient_window]
        if len(campaign_uses) >= self._rate_policy.max_campaign_attempts:
            raise RateLimitExceeded("campaign rate limit reached")
        if len(recipient_uses) >= self._rate_policy.max_recipient_attempts:
            raise RateLimitExceeded("recipient rate limit reached")
        self._rate_uses.append((campaign_id, subject_ref, now))

    def _check_prior_attempts(self, confirmation: Confirmation) -> str | None:
        related = [
            attempt for attempt in self._attempts.values()
            if attempt.campaign_id == confirmation.campaign_id
            and attempt.campaign_subject_ref == confirmation.campaign_subject_ref
        ]
        if any(attempt.state in {AttemptState.ATTEMPT_STARTED, AttemptState.UNKNOWN} for attempt in related):
            raise DispatchBlocked("prior attempt has an unresolved outcome; no further attempt is allowed")
        duplicate_accepted = any(
            attempt.payload_sha256 == confirmation.payload_sha256
            and attempt.state in {AttemptState.ACCEPTED_BY_PROVIDER, AttemptState.RECONCILED_ACCEPTED}
            for attempt in related
        )
        if duplicate_accepted:
            raise DispatchBlocked("same message was already accepted by the provider")
        retryable = [
            attempt for attempt in related
            if attempt.payload_sha256 == confirmation.payload_sha256
            and attempt.state in {
                AttemptState.DEFINITIVELY_REJECTED,
                AttemptState.RECONCILED_NOT_ACCEPTED,
            }
        ]
        return retryable[-1].attempt_id if retryable else None

    def dispatch(self, confirmation: Confirmation) -> AttemptRecord:
        with self._lock:
            issued = self._confirmations.get(confirmation.confirmation_id) if isinstance(confirmation, Confirmation) else None
            if issued is None or issued != confirmation:
                raise DispatchBlocked("confirmation is unknown or altered")
            if confirmation.confirmation_id in self._used_confirmations:
                raise DispatchBlocked("confirmation already consumed")
            self._used_confirmations.add(confirmation.confirmation_id)
            now = self._now()
            if now >= confirmation.expires_at:
                raise DispatchBlocked("confirmation expired")
            if not self.kill_switch.enabled or self.kill_switch.generation != confirmation.kill_generation:
                raise DispatchBlocked("kill switch is disabled or confirmation generation is stale")
            preview = self._previews[confirmation.preview_id]
            grant = self.consent_ledger.current_grant(confirmation.campaign_id, preview.recipient)
            if grant is None or (grant.event_id, grant.event_hash) != (
                confirmation.consent_event_id, confirmation.consent_event_hash
            ):
                raise DispatchBlocked("consent changed or recipient became suppressed")
            if (
                preview.payload_sha256 != confirmation.payload_sha256
                or preview.campaign_subject_ref != confirmation.campaign_subject_ref
                or preview.snapshot_id != confirmation.snapshot_id
                or preview.roster_index != confirmation.roster_index
            ):
                raise DispatchBlocked("confirmation no longer matches the reviewed preview")
            linked_prior_attempt_id = self._check_prior_attempts(confirmation)
            self._reserve_rate(confirmation.campaign_id, confirmation.campaign_subject_ref, now)
            attempt_id = str(uuid.uuid4())
            attempt = AttemptRecord(
                attempt_id=attempt_id,
                campaign_id=confirmation.campaign_id,
                campaign_subject_ref=confirmation.campaign_subject_ref,
                payload_sha256=confirmation.payload_sha256,
                state=AttemptState.ATTEMPT_STARTED,
                attempt_started_at=now,
                confirmation_id=confirmation.confirmation_id,
                consent_event_id=confirmation.consent_event_id,
                consent_event_hash=confirmation.consent_event_hash,
                snapshot_id=confirmation.snapshot_id,
                roster_index=confirmation.roster_index,
                kill_generation=confirmation.kill_generation,
                policy_version=self._rate_policy.policy_version,
                linked_prior_attempt_id=linked_prior_attempt_id,
            )
            self._attempts[attempt_id] = attempt
            self._attempt_history.append(attempt)
            request = MockProviderRequest(
                attempt_id=attempt_id,
                campaign_id=confirmation.campaign_id,
                campaign_subject_ref=confirmation.campaign_subject_ref,
                payload_sha256=confirmation.payload_sha256,
                idempotency_key=attempt_id,
            )

        # ATTEMPT_STARTED is committed before this mock call; a kill after admission
        # cannot recall an already in-flight request.
        try:
            receipt = self._provider.submit_mock(request)
            state = AttemptState.ACCEPTED_BY_PROVIDER
            provider_reference = receipt.provider_reference
        except MockProviderRejected:
            state = AttemptState.DEFINITIVELY_REJECTED
            provider_reference = None
        except MockProviderAmbiguous:
            state = AttemptState.UNKNOWN
            provider_reference = None
        except Exception:
            state = AttemptState.UNKNOWN
            provider_reference = None
        try:
            resolved_at = self._now()
        except RuntimeError:
            state = AttemptState.UNKNOWN
            provider_reference = None
            resolved_at = attempt.attempt_started_at
        resolved = replace(attempt, state=state, resolved_at=resolved_at, provider_reference=provider_reference)
        with self._lock:
            self._attempts[attempt_id] = resolved
            self._attempt_history.append(resolved)
        return resolved

    def reconcile_unknown(
        self,
        attempt_id: str,
        *,
        accepted_by_provider: bool,
        evidence_ref: str,
        operator_id: str,
        operator_attested: bool,
    ) -> AttemptRecord:
        if type(accepted_by_provider) is not bool or operator_attested is not True:
            raise DispatchBlocked("reconciliation requires explicit operator attestation")
        if not isinstance(evidence_ref, str) or not _SOURCE_REF.fullmatch(evidence_ref):
            raise DispatchBlocked("opaque reconciliation evidence reference is required")
        if not isinstance(operator_id, str) or not _SOURCE_REF.fullmatch(operator_id):
            raise DispatchBlocked("authenticated operator reference is required")
        with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is None or attempt.state is not AttemptState.UNKNOWN:
                raise DispatchBlocked("only an unknown attempt can be reconciled")
            state = (
                AttemptState.RECONCILED_ACCEPTED
                if accepted_by_provider
                else AttemptState.RECONCILED_NOT_ACCEPTED
            )
            reconciled = replace(
                attempt,
                state=state,
                resolved_at=self._now(),
                reconciliation_evidence_ref=evidence_ref,
                reconciled_by=operator_id,
            )
            self._attempts[attempt_id] = reconciled
            self._attempt_history.append(reconciled)
            return reconciled
