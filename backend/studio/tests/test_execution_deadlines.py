from datetime import timedelta
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.utils import timezone

from studio.services.execution_deadlines import (
    expired_kind,
    initial_deadlines,
    meaningful_deadline_updates,
)


class ExecutionDeadlineTests(SimpleTestCase):
    def test_hard_deadline_wins_and_is_never_recomputed(self):
        started = timezone.now()
        state = initial_deadlines(
            idle_seconds=30, hard_seconds=60, now=started, marker="phase:claimed",
        )
        subject = SimpleNamespace(**state.__dict__)
        updates = meaningful_deadline_updates(
            subject, marker="node:2", idle_seconds=30, now=started + timedelta(seconds=50),
        )
        self.assertEqual(updates["idle_deadline_at"], state.hard_deadline_at)
        self.assertEqual(subject.hard_deadline_at, state.hard_deadline_at)
        subject.idle_deadline_at = updates["idle_deadline_at"]
        self.assertEqual(expired_kind(subject, now=started + timedelta(seconds=60)), "hard")

    def test_repeated_marker_heartbeat_and_running_do_not_renew_idle(self):
        started = timezone.now()
        state = initial_deadlines(
            idle_seconds=30, hard_seconds=300, now=started, marker="status:running",
        )
        subject = SimpleNamespace(**state.__dict__)
        self.assertEqual(
            meaningful_deadline_updates(
                subject,
                marker="status:running",
                idle_seconds=30,
                now=started + timedelta(seconds=10),
            ),
            {},
        )
        self.assertEqual(subject.idle_deadline_at, state.idle_deadline_at)

    def test_strict_node_or_progress_marker_renews_idle(self):
        started = timezone.now()
        state = initial_deadlines(
            idle_seconds=30, hard_seconds=300, now=started, marker="node:1",
        )
        subject = SimpleNamespace(**state.__dict__)
        updates = meaningful_deadline_updates(
            subject,
            marker="node:2",
            idle_seconds=30,
            now=started + timedelta(seconds=20),
        )
        self.assertEqual(updates["idle_deadline_at"], started + timedelta(seconds=50))
        self.assertEqual(updates["progress_marker"], "node:2")

    def test_idle_cannot_exceed_hard(self):
        with self.assertRaises(ValidationError):
            initial_deadlines(idle_seconds=61, hard_seconds=60)
