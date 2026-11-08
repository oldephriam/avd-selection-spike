from __future__ import annotations

import tempfile
import unittest
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from campaign.event_log import DuplicateEventError, EventLog
from campaign.fake_driver import FakeDriver, LostAcknowledgment, SimulatedCrash
from campaign.models import Job, ResolutionEvidence, RunStatus
from campaign.roster import RosterError, load_roster
from campaign.runner import Runner


class FaultInjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.csv_path = self.root / "roster.csv"
        self.csv_path.write_text(
            "name,phone\nAlice Example,+1 (208) 555-0101\nBob Example,208-555-0102\n",
            encoding="utf-8",
        )
        self.snapshot = load_roster(self.csv_path, campaign_id="test-campaign")
        self.log = EventLog(self.root / "events.jsonl")
        self.driver = FakeDriver()
        self.runner = Runner(self.snapshot, self.log, self.driver)
        self.job = Job.create(self.snapshot, 0, "Test message")
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def evidence(self, *, snapshot_id=None, index=0, number=None, when=None):
        contact = self.snapshot.contacts[index]
        return ResolutionEvidence(
            snapshot_id=snapshot_id or self.snapshot.snapshot_id,
            roster_index=index,
            recipient_key=number or contact.recipient_key,
            observed_at=when or self.now,
            source="fake-driver",
            evidence_id=f"evidence-{index}-{(when or self.now).timestamp()}",
        )

    def test_duplicate_identity_rejects_roster(self):
        self.csv_path.write_text(
            "name,phone\nAlice,+1 208 555 0101\nAlias,208-555-0101\n",
            encoding="utf-8",
        )
        with self.assertRaises(RosterError):
            load_roster(self.csv_path, campaign_id="duplicates")
        self.assertEqual(self.driver.attempts, [])

    def test_stale_mapping_fails_closed(self):
        stale = self.evidence(snapshot_id="old-snapshot")
        result = self.runner.run(self.job, stale)
        self.assertEqual(result, RunStatus.SKIPPED_UNMATCHED_THREAD)
        self.assertEqual(self.driver.attempts, [])

    def test_late_resolution_requires_fresh_reverification(self):
        self.assertEqual(self.runner.run(self.job, None), RunStatus.SKIPPED_UNVERIFIED)
        stale = self.evidence(when=self.now - timedelta(minutes=10))
        self.assertEqual(self.runner.on_late_resolution(self.job, stale), RunStatus.STALE_EVIDENCE_RETRY)
        self.assertEqual(self.driver.attempts, [])
        fresh = self.evidence(when=self.now + timedelta(seconds=1))
        self.assertEqual(self.runner.run(self.job, fresh), RunStatus.SENT)
        self.assertEqual(len(self.driver.attempts), 1)

    def test_lost_ack_transitions_to_needs_review_and_blocks_retry(self):
        self.driver.outcome = "lost_ack"
        with self.assertRaises(LostAcknowledgment):
            self.runner.run(self.job, self.evidence())
        self.assertEqual(self.log.latest_status(self.job.job_id), RunStatus.NEEDS_REVIEW)
        self.assertEqual(len(self.driver.attempts), 1)
        self.assertEqual(self.runner.run(self.job, self.evidence()), RunStatus.NEEDS_REVIEW)
        self.assertEqual(len(self.driver.attempts), 1, "uncertain dispatch must never auto-retry")

    def test_crash_restart_does_not_repeat_dispatch_attempt(self):
        self.driver.outcome = "crash_after_record"
        with self.assertRaises(SimulatedCrash):
            self.runner.run(self.job, self.evidence())
        restarted = Runner(self.snapshot, EventLog(self.root / "events.jsonl"), self.driver)
        self.assertEqual(restarted.run(self.job, self.evidence()), RunStatus.NEEDS_REVIEW)
        self.assertEqual(len(self.driver.attempts), 1)

    def test_wrong_recipient_evidence_blocks_dispatch(self):
        wrong = self.evidence(number=self.snapshot.contacts[1].recipient_key)
        self.assertEqual(self.runner.run(self.job, wrong), RunStatus.SKIPPED_UNMATCHED_THREAD)
        self.assertEqual(self.driver.attempts, [])

    def test_duplicate_event_id_is_rejected(self):
        event = self.log.append(self.job.job_id, RunStatus.PENDING, event_id="fixed-event")
        self.assertEqual(event.schema_version, 1)
        self.assertEqual(self.log.events()[0].schema_version, 1)
        with self.assertRaises(DuplicateEventError):
            self.log.append(self.job.job_id, RunStatus.PENDING, event_id="fixed-event")

    def test_missing_evidence_blocks_dispatch(self):
        self.assertEqual(self.runner.run(self.job, None), RunStatus.SKIPPED_UNVERIFIED)
        self.assertEqual(self.driver.attempts, [])


if __name__ == "__main__":
    unittest.main()
