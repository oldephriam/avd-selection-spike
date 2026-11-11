import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

from src.campaign.sandbox_adapter import (
    CONFIRMATION_PHRASE,
    AttemptState,
    ConsentLedger,
    DispatchBlocked,
    HmacReferenceMapper,
    InMemoryMockProvider,
    MockProviderOutcome,
    RateLimitPolicy,
    RateLimitExceeded,
    SandboxDispatchLoop,
)


class HmacReferenceMapperTests(unittest.TestCase):
    def setUp(self):
        self.mapper = HmacReferenceMapper(
            consent_key_id="consent-v1",
            consent_key=b"c" * 32,
            suppression_key_id="suppress-v1",
            suppression_key=b"s" * 32,
        )

    def test_references_are_stable_scoped_and_separated(self):
        first = self.mapper.for_test_recipient("pilot-a", "+1 208 555 0101")
        same = self.mapper.for_test_recipient("pilot-a", "+12085550101")
        other_campaign = self.mapper.for_test_recipient("pilot-b", "+12085550101")

        self.assertEqual(first.campaign_subject_ref, same.campaign_subject_ref)
        self.assertNotEqual(first.campaign_subject_ref, other_campaign.campaign_subject_ref)
        self.assertEqual(first.suppression_ref, other_campaign.suppression_ref)
        self.assertNotEqual(first.campaign_subject_ref, first.suppression_ref)
        self.assertNotIn("12085550101", first.campaign_subject_ref)
        self.assertTrue(first.campaign_subject_ref.startswith("hmac-sha256:consent-v1:"))
        self.assertTrue(first.suppression_ref.startswith("hmac-sha256:suppress-v1:"))

    def test_mapper_rejects_non_synthetic_recipient(self):
        with self.assertRaises(ValueError):
            self.mapper.for_test_recipient("pilot-a", "+1 208 555 0201")

    def test_key_ids_must_distinguish_separate_purpose_keys(self):
        with self.assertRaises(ValueError):
            HmacReferenceMapper(
                consent_key_id="same-id", consent_key=b"c" * 32,
                suppression_key_id="same-id", suppression_key=b"s" * 32,
            )


class ConsentLedgerTests(unittest.TestCase):
    def setUp(self):
        self.mapper = HmacReferenceMapper(
            consent_key_id="consent-v1",
            consent_key=b"c" * 32,
            suppression_key_id="suppress-v1",
            suppression_key=b"s" * 32,
        )
        self.ledger = ConsentLedger(self.mapper)

    def test_explicit_consent_is_required_and_global_opt_out_overrides_grant(self):
        recipient = "+1 208 555 0101"
        disclosure_hash = "a" * 64
        with self.assertRaises(ValueError):
            self.ledger.record_grant(
                "pilot-a", recipient, disclosure_hash, affirmative=False,
                captured_at=datetime.now(timezone.utc), source_ref="fixture"
            )

        grant = self.ledger.record_grant(
            "pilot-a", recipient, disclosure_hash, affirmative=True,
            captured_at=datetime.now(timezone.utc), source_ref="fixture"
        )
        self.assertTrue(self.ledger.is_eligible("pilot-a", recipient))
        self.assertEqual(grant.campaign_subject_ref,
                         self.mapper.for_test_recipient("pilot-a", recipient).campaign_subject_ref)

        self.ledger.record_opt_out(recipient, occurred_at=datetime.now(timezone.utc), source="operator")
        self.assertFalse(self.ledger.is_eligible("pilot-a", recipient))
        self.assertFalse(self.ledger.is_eligible("pilot-b", recipient))

    def test_eligibility_is_derived_from_verified_ledger_not_cached_grants(self):
        recipient = "+1 208 555 0101"
        self.ledger.record_grant(
            "pilot-a", recipient, "a" * 64, affirmative=True,
            captured_at=datetime.now(timezone.utc), source_ref="fixture"
        )
        self.ledger._events.clear()

        self.assertFalse(self.ledger.is_eligible("pilot-a", recipient))

    def test_future_dated_consent_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.record_grant(
                "pilot-a", "+1 208 555 0101", "a" * 64, affirmative=True,
                captured_at=datetime.now(timezone.utc) + timedelta(days=1),
                source_ref="fixture-consent-future"
            )

    def test_campaign_withdrawal_revokes_only_that_campaign_grant(self):
        recipient = "+1 208 555 0101"
        for campaign in ("pilot-a", "pilot-b"):
            self.ledger.record_grant(
                campaign, recipient, "a" * 64, affirmative=True,
                captured_at=datetime.now(timezone.utc), source_ref=f"fixture-{campaign}"
            )
        self.ledger.withdraw_consent(
            "pilot-a", recipient, occurred_at=datetime.now(timezone.utc),
            source_ref="fixture-withdrawal-a"
        )

        self.assertFalse(self.ledger.is_eligible("pilot-a", recipient))
        self.assertTrue(self.ledger.is_eligible("pilot-b", recipient))

    def test_backdated_grant_cannot_revive_withdrawn_campaign_consent(self):
        recipient = "+1 208 555 0101"
        now = datetime.now(timezone.utc)
        self.ledger.record_grant(
            "pilot-a", recipient, "a" * 64, affirmative=True,
            captured_at=now - timedelta(days=2), source_ref="fixture-grant-old"
        )
        self.ledger.withdraw_consent(
            "pilot-a", recipient, occurred_at=now - timedelta(days=1),
            source_ref="fixture-withdrawal-new"
        )

        with self.assertRaises(ValueError):
            self.ledger.record_grant(
                "pilot-a", recipient, "b" * 64, affirmative=True,
                captured_at=now - timedelta(hours=36), source_ref="fixture-grant-backdated"
            )
        self.assertFalse(self.ledger.is_eligible("pilot-a", recipient))


class SandboxDispatchLoopTests(unittest.TestCase):
    def setUp(self):
        self.mapper = HmacReferenceMapper(
            consent_key_id="consent-v1",
            consent_key=b"c" * 32,
            suppression_key_id="suppress-v1",
            suppression_key=b"s" * 32,
        )
        self.provider = InMemoryMockProvider()
        self.loop = SandboxDispatchLoop(
            self.mapper,
            RateLimitPolicy(
                max_campaign_attempts=3,
                campaign_window_seconds=3600,
                max_recipient_attempts=1,
                recipient_window_seconds=86400,
            ),
            provider=self.provider,
        )
        self.recipient = "+1 208 555 0101"
        self.loop.consent_ledger.record_grant(
            "pilot-a", self.recipient, "a" * 64, affirmative=True,
            captured_at=datetime.now(timezone.utc), source_ref="fixture-consent-1"
        )
        self.loop.kill_switch.enable(actor="operator-a", second_approver="operator-b")

    def test_preview_is_blocked_while_kill_switch_is_disabled(self):
        self.loop.kill_switch.disable(actor="operator-a", reason="test-disable")
        with self.assertRaises(DispatchBlocked):
            self.loop.create_preview(
                "pilot-a", self.recipient, "Synthetic Ada", "Must not preview while disabled.",
                snapshot_id="snapshot-1", roster_index=0
            )

    def test_per_recipient_preview_confirmation_and_mock_attempt_are_single_use(self):
        preview = self.loop.create_preview(
            "pilot-a", self.recipient, "Synthetic Ada", "Hello Ada, sandbox only.",
            snapshot_id="snapshot-1", roster_index=0
        )
        self.assertEqual(preview.recipient, "+12085550101")
        with self.assertRaises(DispatchBlocked):
            self.loop.confirm(preview, "CONFIRM ALL", operator_id="operator-a")

        confirmation = self.loop.confirm(
            preview, CONFIRMATION_PHRASE, operator_id="operator-a"
        )
        attempt = self.loop.dispatch(confirmation)

        self.assertEqual(attempt.state, AttemptState.ACCEPTED_BY_PROVIDER)
        self.assertEqual(len(self.provider.submissions), 1)
        request = self.provider.submissions[0]
        self.assertNotIn("12085550101", repr(request))
        self.assertNotIn("Hello Ada", repr(request))
        with self.assertRaises(DispatchBlocked):
            self.loop.dispatch(confirmation)
        self.assertEqual(len(self.provider.submissions), 1)

    def _preview_and_confirm(self, message="Hello Ada, sandbox only."):
        preview = self.loop.create_preview(
            "pilot-a", self.recipient, "Synthetic Ada", message,
            snapshot_id="snapshot-1", roster_index=0
        )
        return self.loop.confirm(preview, CONFIRMATION_PHRASE, operator_id="operator-a")

    def test_opt_out_after_confirmation_blocks_mock_submission(self):
        confirmation = self._preview_and_confirm()
        self.loop.consent_ledger.record_opt_out(
            self.recipient, occurred_at=datetime.now(timezone.utc), source="operator"
        )

        with self.assertRaises(DispatchBlocked):
            self.loop.dispatch(confirmation)
        self.assertEqual(self.provider.submissions, ())

    def test_kill_generation_invalidates_confirmation(self):
        confirmation = self._preview_and_confirm()
        self.loop.kill_switch.disable(actor="operator-a", reason="test-stop")

        with self.assertRaises(DispatchBlocked):
            self.loop.dispatch(confirmation)
        self.assertEqual(self.provider.submissions, ())

    def test_kill_generation_invalidates_confirmation_after_reenable(self):
        confirmation = self._preview_and_confirm()
        self.loop.kill_switch.disable(actor="operator-a", reason="test-stop-reenable")
        self.loop.kill_switch.enable(actor="operator-b", second_approver="operator-c")

        with self.assertRaises(DispatchBlocked):
            self.loop.dispatch(confirmation)
        self.assertEqual(self.provider.submissions, ())

    def test_disable_reenable_invalidates_unconfirmed_preview(self):
        preview = self.loop.create_preview(
            "pilot-a", self.recipient, "Synthetic Ada", "Old preview content.",
            snapshot_id="snapshot-1", roster_index=0
        )
        self.loop.kill_switch.disable(actor="operator-a", reason="test-stop-preview")
        self.loop.kill_switch.enable(actor="operator-b", second_approver="operator-c")

        with self.assertRaises(DispatchBlocked):
            self.loop.confirm(preview, CONFIRMATION_PHRASE, operator_id="operator-a")
        self.assertEqual(self.provider.submissions, ())

    def test_ambiguous_outcome_is_unknown_and_never_retried(self):
        self.provider.queue_outcome(MockProviderOutcome.AMBIGUOUS)
        confirmation = self._preview_and_confirm()

        attempt = self.loop.dispatch(confirmation)

        self.assertEqual(attempt.state, AttemptState.UNKNOWN)
        self.assertEqual(
            [event.state for event in self.loop.attempt_history],
            [AttemptState.ATTEMPT_STARTED, AttemptState.UNKNOWN],
        )
        with self.assertRaises(DispatchBlocked):
            self.loop.dispatch(confirmation)
        with self.assertRaises(DispatchBlocked):
            self.loop.dispatch(self._preview_and_confirm("A new message is not a retry."))
        self.assertEqual(len(self.provider.submissions), 1)

    def test_unknown_attempt_stays_blocked_after_rate_window_expires(self):
        start = datetime.now(timezone.utc)
        current = [start]
        provider = InMemoryMockProvider()
        provider.queue_outcome(MockProviderOutcome.AMBIGUOUS)
        loop = SandboxDispatchLoop(
            self.mapper,
            RateLimitPolicy(4, 1, 4, 1),
            provider=provider,
            clock=lambda: current[0],
        )
        loop.consent_ledger.record_grant(
            "pilot-a", self.recipient, "a" * 64, affirmative=True,
            captured_at=start, source_ref="fixture-consent-clock"
        )
        loop.kill_switch.enable(actor="operator-a", second_approver="operator-b")
        preview = loop.create_preview(
            "pilot-a", self.recipient, "Synthetic Ada", "Same reviewed message.",
            snapshot_id="snapshot-1", roster_index=0
        )
        confirmation = loop.confirm(preview, CONFIRMATION_PHRASE, operator_id="operator-a")
        self.assertEqual(loop.dispatch(confirmation).state, AttemptState.UNKNOWN)

        current[0] += timedelta(seconds=2)
        new_preview = loop.create_preview(
            "pilot-a", self.recipient, "Synthetic Ada", "Same reviewed message.",
            snapshot_id="snapshot-1", roster_index=0
        )
        new_confirmation = loop.confirm(
            new_preview, CONFIRMATION_PHRASE, operator_id="operator-a"
        )
        with self.assertRaises(DispatchBlocked):
            loop.dispatch(new_confirmation)
        self.assertEqual(len(provider.submissions), 1)

    def test_accepted_message_cannot_be_submitted_again_after_rate_window(self):
        start = datetime.now(timezone.utc)
        current = [start]
        provider = InMemoryMockProvider()
        loop = SandboxDispatchLoop(
            self.mapper, RateLimitPolicy(4, 1, 4, 1), provider=provider,
            clock=lambda: current[0]
        )
        loop.consent_ledger.record_grant(
            "pilot-a", self.recipient, "a" * 64, affirmative=True,
            captured_at=start, source_ref="fixture-consent-accepted"
        )
        loop.kill_switch.enable(actor="operator-a", second_approver="operator-b")
        message = "Same accepted content."
        preview = loop.create_preview(
            "pilot-a", self.recipient, "Synthetic Ada", message,
            snapshot_id="snapshot-1", roster_index=0
        )
        first_confirmation = loop.confirm(preview, CONFIRMATION_PHRASE, operator_id="operator-a")
        self.assertEqual(loop.dispatch(first_confirmation).state, AttemptState.ACCEPTED_BY_PROVIDER)

        current[0] += timedelta(seconds=2)
        next_preview = loop.create_preview(
            "pilot-a", self.recipient, "Synthetic Ada", message,
            snapshot_id="snapshot-1", roster_index=0
        )
        next_confirmation = loop.confirm(next_preview, CONFIRMATION_PHRASE, operator_id="operator-a")
        with self.assertRaises(DispatchBlocked):
            loop.dispatch(next_confirmation)
        self.assertEqual(len(provider.submissions), 1)

    def test_definitive_nonacceptance_allows_only_new_linked_attempt(self):
        provider = InMemoryMockProvider()
        provider.queue_outcome(MockProviderOutcome.REJECT)
        loop = SandboxDispatchLoop(
            self.mapper, RateLimitPolicy(3, 3600, 2, 86400), provider=provider
        )
        loop.consent_ledger.record_grant(
            "pilot-a", self.recipient, "a" * 64, affirmative=True,
            captured_at=datetime.now(timezone.utc), source_ref="fixture-consent-retry"
        )
        loop.kill_switch.enable(actor="operator-a", second_approver="operator-b")
        message = "Only retry after definite nonacceptance."
        preview = loop.create_preview(
            "pilot-a", self.recipient, "Synthetic Ada", message,
            snapshot_id="snapshot-1", roster_index=0
        )
        first = loop.dispatch(loop.confirm(preview, CONFIRMATION_PHRASE, operator_id="operator-a"))
        next_preview = loop.create_preview(
            "pilot-a", self.recipient, "Synthetic Ada", message,
            snapshot_id="snapshot-1", roster_index=0
        )
        second = loop.dispatch(
            loop.confirm(next_preview, CONFIRMATION_PHRASE, operator_id="operator-a")
        )

        self.assertEqual(first.state, AttemptState.DEFINITIVELY_REJECTED)
        self.assertEqual(second.state, AttemptState.ACCEPTED_BY_PROVIDER)
        self.assertEqual(second.linked_prior_attempt_id, first.attempt_id)
        self.assertEqual(len(provider.submissions), 2)

    def test_definitive_mock_rejection_is_recorded_without_automatic_retry(self):
        self.provider.queue_outcome(MockProviderOutcome.REJECT)
        first_attempt = self.loop.dispatch(self._preview_and_confirm())

        self.assertEqual(first_attempt.state, AttemptState.DEFINITIVELY_REJECTED)
        with self.assertRaises(RateLimitExceeded):
            self.loop.dispatch(self._preview_and_confirm("A second attempt is not automatic."))
        self.assertEqual(len(self.provider.submissions), 1)

    def test_unknown_attempt_requires_evidence_to_reconcile_and_is_not_retried(self):
        self.provider.queue_outcome(MockProviderOutcome.AMBIGUOUS)
        confirmation = self._preview_and_confirm()
        attempt = self.loop.dispatch(confirmation)

        with self.assertRaises(DispatchBlocked):
            self.loop.reconcile_unknown(
                attempt.attempt_id, accepted_by_provider=False,
                evidence_ref="provider-query-7", operator_id="operator-c", operator_attested=False
            )
        reconciled = self.loop.reconcile_unknown(
            attempt.attempt_id, accepted_by_provider=False,
            evidence_ref="provider-query-7", operator_id="operator-c", operator_attested=True
        )

        self.assertEqual(reconciled.state, AttemptState.RECONCILED_NOT_ACCEPTED)
        self.assertEqual(reconciled.reconciliation_evidence_ref, "provider-query-7")
        with self.assertRaises(DispatchBlocked):
            self.loop.dispatch(confirmation)
        self.assertEqual(len(self.provider.submissions), 1)

    def test_kill_switch_starts_disabled_and_enable_requires_two_operators(self):
        loop = SandboxDispatchLoop(
            self.mapper,
            RateLimitPolicy(2, 3600, 1, 86400),
            provider=InMemoryMockProvider(),
        )
        loop.consent_ledger.record_grant(
            "pilot-a", self.recipient, "a" * 64, affirmative=True,
            captured_at=datetime.now(timezone.utc), source_ref="fixture-consent-2"
        )
        self.assertFalse(loop.kill_switch.enabled)
        with self.assertRaises(DispatchBlocked):
            loop.create_preview(
                "pilot-a", self.recipient, "Synthetic Ada", "Sandbox only.",
                snapshot_id="snapshot-1", roster_index=0
            )
        with self.assertRaises(ValueError):
            loop.kill_switch.enable(actor="operator-a", second_approver="operator-a")
        loop.kill_switch.enable(actor="operator-a", second_approver="operator-b")
        preview = loop.create_preview(
            "pilot-a", self.recipient, "Synthetic Ada", "Sandbox only.",
            snapshot_id="snapshot-1", roster_index=0
        )
        loop.confirm(preview, CONFIRMATION_PHRASE, operator_id="operator-a")
        self.assertEqual(loop.provider.submissions, ())

    def test_kill_switch_audits_each_generation_change(self):
        self.loop.kill_switch.disable(actor="operator-a", reason="pause-test")
        events = self.loop.kill_switch.events

        self.assertEqual([event.generation for event in events], [1, 2])
        self.assertEqual([event.action for event in events], ["ENABLE", "DISABLE"])
        self.assertEqual(events[0].second_approver, "operator-b")
        self.assertEqual(events[1].reason, "pause-test")

    def test_dispatcher_refuses_non_mock_provider_and_provider_replacement(self):
        class ProviderLike:
            def submit_mock(self, request):
                raise AssertionError("must never be called")

        with self.assertRaises(TypeError):
            SandboxDispatchLoop(
                self.mapper,
                RateLimitPolicy(2, 3600, 1, 86400),
                provider=ProviderLike(),  # type: ignore[arg-type]
            )
        with self.assertRaises(AttributeError):
            setattr(self.loop, "provider", ProviderLike())
        with self.assertRaises(AttributeError):
            setattr(self.loop, "rate_policy", RateLimitPolicy(100, 3600, 100, 86400))
        with self.assertRaises(AttributeError):
            setattr(self.loop, "consent_ledger", object())

    def test_concurrent_confirmations_cannot_bypass_recipient_rate_limit(self):
        first = self._preview_and_confirm("First approved note.")
        second = self._preview_and_confirm("Second approved note.")
        barrier = threading.Barrier(3)

        def attempt(confirmation):
            barrier.wait()
            try:
                self.loop.dispatch(confirmation)
                return "accepted"
            except RateLimitExceeded:
                return "limited"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(attempt, item) for item in (first, second)]
            barrier.wait()
            outcomes = [future.result() for future in results]

        self.assertCountEqual(outcomes, ["accepted", "limited"])
        self.assertEqual(len(self.provider.submissions), 1)


if __name__ == "__main__":
    unittest.main()
