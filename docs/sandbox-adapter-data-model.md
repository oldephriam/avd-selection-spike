# Sandbox adapter: consent ledger and safety-state design

**Status:** design plus a process-local in-memory mock implementation only. No provider is selected, no network transport is implemented, and no real dispatch is enabled. Keep this work separate from the synthetic-only prototype.

## Boundary and defaults

The sandbox adapter is a separate package/entry point that can use only a provider's documented sandbox environment and test destinations. It must not accept production credentials or production endpoints. The existing fake recorder remains the default. The adapter is not a reason to weaken the core synthetic-recipient gate or add a live transport to the offline prototype.

A dispatch attempt is allowed only after all of these checks succeed together at the last responsible moment: exact preview confirmation, current consent, no opt-out, valid evidence, rate-limit reservation, and `dispatch_enabled`. Missing or unreadable state denies the attempt. A transport timeout after an attempt starts is `UNKNOWN/NEEDS_REVIEW`, never an automatic retry.

## Identity and protected data

Normalize a volunteer's number to strict E.164 before lookup; reject ambiguous or invalid inputs rather than guessing. Do not use an unsalted hash of a phone number: its small search space makes enumeration practical. Use purpose- and scope-separated keyed references:

```text
campaign_subject_ref = "hmac-sha256:<key-id>:" + hex(HMAC-SHA-256(
    K_consent[key-id], "sms-consent:v1\0" || campaign_id || "\0" || canonical_e164))

suppression_ref = "hmac-sha256:<key-id>:" + hex(HMAC-SHA-256(
    K_suppression[key-id], "sms-global-suppression:v1\0" || canonical_e164))
```

- `campaign_subject_ref` is scoped to one campaign; `suppression_ref` is deliberately stable across campaigns so a global SMS opt-out can be enforced. The latter reveals equality across that suppression scope, so treat it as sensitive pseudonymous data, not anonymized data. Do not emit it in general analytics or logs.
- `<key-id>` identifies the managed key version; the `v1` domain tag identifies the input/schema purpose. Keep `K_consent` and `K_suppression` in a managed secret/key store, never in source, a roster, or application logs. Use separate keys and domain prefixes for each purpose.
- Apply HMAC to the exact UTF-8 bytes of the validated canonical E.164 value (and the specified scope fields); use one canonical encoding everywhere. Use constant-time comparison when verifying MACs supplied across a trust boundary; ordinary indexed database equality on opaque references is not a MAC verification.
- Store the number, if operationally required, separately as authenticated ciphertext in a restricted identity vault. Keep the key separate from the ciphertext store; record key version and encryption metadata.
- A vault row should have a random opaque `vault_record_id`, authenticated ciphertext, encryption-key ID, and a restricted index of `(purpose, HMAC key-id, reference)` values. Keep this mapping out of analytics. Use a fixed, validated campaign-ID encoding and unambiguous field framing in the HMAC input; never concatenate unchecked variable-length fields.
- Define a reviewed key-rotation and lookup-migration process before deployment. Do not silently change the HMAC key: it changes every reference and can break suppression matching. Keep key IDs with references; migrate by deriving new references from the protected identity vault or retain old-key lookup until a complete reconciliation proves every existing suppression is still matched. Test legacy-key matches before retiring any key.
- Do not put full numbers, raw inbound message bodies, or full message text in the audit ledger. A human preview may show the resolved recipient and exact message; audit events should record references and hashes.

## Append-only consent and suppression events

Use versioned event ledgers rather than overwriting booleans or conflating campaign consent with global suppression. Persist an event before considering its state effective; make append and state projection transactional or enforce a single writer. Canonicalize fields as UTF-8 JSON with sorted keys, compact separators, normalized UTC timestamps, and no floating-point values; exclude the `event_hash` field itself and include the prior hash. Hash chaining provides tamper evidence, not immutability. If legal-grade retention is required, separately approve an access-controlled/WORM archive and retention schedule.

A consent event can use this logical schema:

```text
ConsentEvent {
  schema_version: integer
  event_id: UUID
  campaign_subject_ref: campaign-scoped HMAC reference
  event_type: GRANT | WITHDRAWAL
  channel: SMS
  campaign_id: stable identifier
  purpose: short, approved purpose code
  consent_text_version: stable identifier
  consent_text_sha256: hash of the exact disclosure shown
  affirmative_action: explicit action captured (not inferred from roster membership)
  captured_at: RFC3339 UTC timestamp
  capture_method: approved method identifier
  source_ref: protected evidence reference, not a raw URL or free-form note
  evidence_sha256: optional hash of protected evidence
  recorded_by: authenticated operator/system identity
  previous_event_hash: prior ledger hash
  event_hash: hash of canonicalized event fields plus previous hash
}
```

Keep global SMS suppression in a separate event stream so routine consent events do not create a cross-campaign join key:

```text
SuppressionEvent {
  schema_version: integer
  event_id: UUID
  suppression_ref: global-scope HMAC reference
  channel: SMS
  action: BLOCK
  source: inbound_keyword | operator | provider_notice
  normalized_keyword: optional approved keyword, never the raw message body
  received_at: RFC3339 UTC timestamp
  source_event_ref: protected source/evidence reference
  recorded_by: authenticated operator/system identity
  previous_event_hash
  event_hash
}
```

An SMS opt-out creates a durable global suppression event (and, if needed, a campaign-consent withdrawal); persist it before acknowledging the opt-out. If persistence or webhook verification fails, fail closed and hold new attempts. Suppression overrides every grant in every campaign for the defined channel/scope. Do not provide a casual re-subscribe or delete path; if re-consent is ever allowed, require a separately reviewed event and proof, and preserve the prior opt-out history. Do not claim that an event's presence independently proves the person understood or authorized the disclosure; verify the capture process and evidence.

Project current consent and suppression from the event history, but retain the source events. Reject malformed, duplicate, out-of-order, or unverifiable records and fail closed. The mock ledger rejects non-increasing `GRANT`/`WITHDRAWAL` times for the same campaign subject; inbound event ingestion in a future durable system must separately preserve received/recorded time so delayed opt-out events still suppress immediately. Minimize retention of the protected evidence and recipient vault according to an approved schedule.

## Atomic rate-limit records

Set actual numerical limits in a signed pilot charter, not as guessed code defaults. Start with at most one individually approved outbound message per volunteer for the initial pilot; any follow-up requires a new, separately reviewed scope.

Represent limits as policy plus an append-only attempt/reservation log:

```text
RatePolicy {
  policy_version
  campaign_id
  global_max_attempts: integer
  global_window_seconds: integer
  per_recipient_max_attempts: integer
  per_recipient_window_seconds: integer
  per_recipient_cooldown_seconds: integer
  effective_from: RFC3339 UTC timestamp
}

AttemptReservation {
  reservation_id: UUID
  idempotency_key: random opaque ID, unique and stable for this exact approved job
  campaign_id
  campaign_subject_ref
  preview_hash
  reserved_at: RFC3339 UTC timestamp
  attempt_started_at: optional RFC3339 UTC timestamp
  resolved_at: optional RFC3339 UTC timestamp
  state: RESERVED | ATTEMPT_STARTED | ACCEPTED_BY_PROVIDER | DEFINITIVELY_REJECTED | UNKNOWN | CANCELLED_BEFORE_ATTEMPT | RECONCILED_ACCEPTED | RECONCILED_NOT_ACCEPTED
  policy_version
  provider_reference: optional opaque provider ID
  outcome_evidence_ref: optional protected evidence reference
  linked_prior_attempt_id: optional ID for a separately approved new attempt
}
```

Use a transactional store with a unique idempotency constraint and a rolling-window check (or another reviewed limiter with equivalent boundary behavior). The dispatch authority must atomically expose current consent/suppression projections, kill generation, confirmation consumption, and rate state; if events are ingested elsewhere, update the dispatch projection through a durable, fail-closed process. First consume the confirmation and reserve capacity in a transaction. Immediately before the provider call, run a second transaction that rechecks consent and suppression, compares the current kill generation and policy, and changes `RESERVED` to `ATTEMPT_STARTED`. Commit that transition before calling the provider. If the checks fail, mark `CANCELLED_BEFORE_ATTEMPT` and make no call. Concurrent confirms must not both pass. Count `ATTEMPT_STARTED`, `ACCEPTED_BY_PROVIDER`, `DEFINITIVELY_REJECTED`, `UNKNOWN`, and both reconciliation outcomes against attempt limits; release a reservation only when durable state proves it never reached `ATTEMPT_STARTED`. If the limiter/control/consent authority is unavailable, block dispatch.

Define transitions explicitly: `RESERVED -> ATTEMPT_STARTED | CANCELLED_BEFORE_ATTEMPT`; `ATTEMPT_STARTED -> ACCEPTED_BY_PROVIDER | DEFINITIVELY_REJECTED | UNKNOWN`; and `UNKNOWN -> RECONCILED_ACCEPTED | RECONCILED_NOT_ACCEPTED` only with provider evidence or documented human review. Provider acceptance is not delivery; delivery receipts, if supported, are separate evidence. A crash or timeout after `ATTEMPT_STARTED` becomes `UNKNOWN`, even if the crash may have occurred before the network call. On restart, never resend an `ATTEMPT_STARTED` or `UNKNOWN` record automatically. The executable mock additionally blocks any further attempt for the same campaign subject while an earlier attempt is unresolved, even after the rate window expires, and blocks resubmission of identical content once accepted. A reconciled acceptance or unresolved unknown must not be resent; only a documented definitive non-acceptance, including an evidence-backed `RECONCILED_NOT_ACCEPTED`, can permit a new job with a fresh preview and fresh confirmation. Keep the old attempt and link the new attempt for audit. Provider idempotency support may reduce duplicates only if its exact semantics are verified; it does not justify automatic retry. No database transaction can make an external provider call exactly-once; this design chooses possible false `UNKNOWN` states over duplicate sends.

## Master kill switch

Store kill state durably and audit every transition:

```text
DispatchControlEvent {
  event_id: UUID
  scope: GLOBAL | CAMPAIGN
  campaign_id: optional stable identifier
  action: DISABLE | ENABLE
  generation: monotonically increasing integer
  changed_at: RFC3339 UTC timestamp
  changed_by: authenticated operator
  second_approver: required for ENABLE
  reason_code: controlled value
}
```

The effective default is `DISABLED`; missing, stale, corrupt, or unavailable control state means disabled. Check it before showing a confirmation and again atomically with the rate-limit reservation before every attempt. Store the kill generation and rate reservation in the same transactional authority, or use a reviewed fencing/compare-and-swap protocol; a check across independent stores is not atomic. Bind each confirmation to the current generation. Any `DISABLE` increments the generation, invalidating all outstanding confirmations, and stops new queued attempts. Re-enabling requires deliberate, audited two-person approval; it must not revive old confirmations. An attempt already admitted or in flight may still reach the provider; the switch cannot recall it. Record that as an incident/unknown outcome, not as cancelled.

## Per-message confirmation binding

Keep the current per-recipient model. A confirmation record should bind:

```text
Confirmation {
  confirmation_id: single-use random identifier
  campaign_id
  campaign_subject_ref
  consent_event_id and consent_event_hash
  preview_hash: hash of exact rendered message and recipient binding
  roster_snapshot_hash or approved volunteer-record version
  kill_generation
  expires_at: RFC3339 UTC timestamp
  confirmed_by and confirmed_at
  consumed_at: optional timestamp
}
```

No group-level approval. At consumption, verify the exact recipient and message hash, freshness, consent, suppression, policy, and kill generation; consume the confirmation and reserve the attempt atomically. Store only references/hashes in audit records, not the message body.

## Sandbox acceptance gates

Before using even provider sandbox credentials, test and document: opt-out before preview and after confirmation; malformed consent/ledger data; duplicate confirmation and concurrent attempts; rate-window boundaries; kill switch before preview and immediately before attempt; key-version mismatch; provider timeout/unknown result; restart recovery; and proof that no production endpoint or credential can be selected. The adapter should log decisions without exposing phone numbers or message content.

This document does not authorize a live pilot. A later live-test proposal needs a separate explicit review of consent evidence, privacy/security controls, provider terms, opt-out/STOP handling, retention, rate policy, and a human-approved scope. The existing offline prototype remains unchanged and remains the only runnable sending-related flow until that review is complete.

## Current code status and limits

`src/campaign/sandbox_adapter.py` provides `HmacReferenceMapper`, a process-local hash-chained `ConsentLedger`, `InMemoryMockProvider`, a preview/confirmation gate, a rolling-window rate limiter, kill-switch generations, and an in-memory attempt state flow. `InMemoryMockProvider` receives only the campaign reference and payload hash; it has no network or SMS capability. The loop accepts exactly that provider class and rejects provider replacement through its public property.

This is an executable simulation, **not** a provider sandbox integration or a pilot-ready ledger. Consent, opt-outs, kill state, rate reservations, attempts, and mock results disappear on process exit; the in-memory lock is not cross-process atomicity or durable transaction storage. HMAC keys are injected bytes for tests, not KMS-managed keys. Operator IDs and `operator_attested` reconciliation are caller assertions, not authentication or proof. No real volunteer numbers or consent data belong in this implementation. Any external provider, durable store, real identity vault, or live endpoint requires separate design review and approval; do not replace the mock through an adapter or configuration switch.

`src/campaign/sandbox_pilot_cli.py` is a runnable synthetic-only wrapper. Its local JSON roster supports `volunteers add/list/remove`; IDs, names, and numbers are validated, names must start with `Synthetic `, duplicate JSON keys and unsupported schemas fail closed, and the existing mapper rejects numbers outside the synthetic test range. The registry contains plaintext synthetic fixture names/numbers, so use it only for synthetic data. `dry-run` creates an explicitly synthetic fixture consent event, requires two distinct approver IDs to enable the in-memory kill switch, prints the full recipient/message preview, and waits for the exact one-message confirmation before invoking `InMemoryMockProvider`. It has no live mode, provider configuration, or credentials. The approver/reviewer IDs are assertions only, not authenticated identities, and neither the fixture consent nor this command is suitable for a live-pilot gate.

Run from the repository root:

```text
python -m src.campaign.sandbox_pilot_cli volunteers add --registry sandbox-volunteers.json --id ada --name "Synthetic Ada" --phone "+1 208 555 0101"
python -m src.campaign.sandbox_pilot_cli volunteers list --registry sandbox-volunteers.json
python -m src.campaign.sandbox_pilot_cli dry-run --registry sandbox-volunteers.json --volunteer-id ada --campaign-id fixture-campaign --message "Synthetic sandbox notice." --approver-one operator-a --approver-two operator-b --reviewer operator-a
```

The last command requires a real interactive terminal and entry of `CONFIRM ONE TEXT` after displaying the complete preview; piped input is refused. The Python `main()` function accepts an injectable input function only as a trusted test harness hook. The registry must live on a local fixed drive on Windows (UNC and mapped network drives are rejected, and existing reparse-point components are refused). This pathname check is not atomic against a concurrent local actor replacing path components and is not a security boundary; the registry is strictly synthetic test data. The command records at most one in-memory mock submission. Registry management persists only synthetic fixture contact rows; the consent, kill-switch, rate-limit, and attempt state is process-local.
