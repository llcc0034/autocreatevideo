"""Isolated configuration/probe tests; never probe real GPU or models."""
from datetime import timedelta
import importlib
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from studio.models import ConfigDefinition, ConfigRevision, HostCapabilitySnapshot, RuntimeHost, RuntimeHostProbeAttempt
from studio.services.configuration import create_config_revision, publish_config_revision, resolve_configuration
from studio.services.runtime_host_probe_queue import enqueue_due_runtime_host_probes, enqueue_runtime_host_probe, claim_next_runtime_host_probe
from studio.services.runtime_host_probe_settings import host_probe_settings
from studio.services.runtime_platform import probe_runtime_host


class HostProbeDuePolicyTests(SimpleTestCase):
    def test_manual_also_disables_due_only_maintenance_command_without_snapshot(self):
        from studio.services.runtime_host_probe_queue import _capability_snapshot_refresh_is_due
        with patch("studio.services.runtime_host_probe_queue.host_probe_settings", return_value=("manual", timedelta(days=36500))):
            self.assertFalse(_capability_snapshot_refresh_is_due(None, now=timezone.now()))

    def test_automatic_missing_or_undeclared_expiry_requests_real_probe(self):
        from studio.services.runtime_host_probe_queue import _capability_snapshot_refresh_is_due
        with patch("studio.services.runtime_host_probe_queue.host_probe_settings", return_value=("automatic", timedelta(days=30))):
            self.assertTrue(_capability_snapshot_refresh_is_due(None, now=timezone.now()))
            self.assertTrue(_capability_snapshot_refresh_is_due(SimpleNamespace(expires_at=None), now=timezone.now()))


class HostProbeSettingsTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(username="probe-policy", password="test-password")
        self.host = RuntimeHost.objects.create(key="probe-policy-host", name="Isolated host", status="active")

    def configure(self, **values):
        revision = create_config_revision(scope_type="system", values=values, created_by=self.admin)
        return publish_config_revision(revision, published_by=self.admin)

    def probe(self, errors=None):
        with patch("studio.services.runtime_platform._host_probe_is_local", return_value=True), \
                patch("studio.services.runtime_platform._gpu_probe", return_value=([], None)), \
                patch("studio.services.runtime_platform._memory_total_bytes", return_value=1024), \
                patch("studio.services.runtime_platform.shutil.disk_usage", return_value=SimpleNamespace(total=100, free=90)), \
                patch("studio.services.runtime_platform._comfy_probe", return_value=({"status": "not_configured"}, [], errors or [])):
            return probe_runtime_host(self.host)

    def test_defaults_migration_replay_and_provider_ttl_boundary_unchanged(self):
        self.assertEqual(host_probe_settings(), ("automatic", timedelta(days=30)))
        values = resolve_configuration().values
        self.assertEqual(values["security.runtime.max_capability_ttl_seconds"], 86400)
        self.assertEqual(values["runtime.host_probe_manual_validity_days"], 36500)
        self.assertNotIn("security_ceiling_key", ConfigDefinition.objects.get(key="runtime.capability_ttl_seconds").value_schema)
        snapshot = self.probe()
        self.assertEqual(snapshot.expires_at - snapshot.captured_at, timedelta(days=30))
        self.configure(**{"runtime.host_probe_mode": "manual", "runtime.host_probe_manual_validity_days": 30000})
        before = list(ConfigRevision.objects.values_list("pk", "values_sha256"))
        importlib.import_module("studio.migrations.0238_host_probe_mode_and_validity").forward(apps, SimpleNamespace(connection=connection))
        self.assertEqual(before, list(ConfigRevision.objects.values_list("pk", "values_sha256")))
        self.assertEqual(host_probe_settings(), ("manual", timedelta(days=30000)))

    def test_manual_disables_scheduled_even_without_snapshot_and_manual_still_deduplicates(self):
        self.configure(**{"runtime.host_probe_mode": "manual"})
        self.assertEqual(enqueue_due_runtime_host_probes(), 0)
        self.assertEqual(enqueue_runtime_host_probe(self.host, trigger="scheduled"), (None, False))
        self.assertEqual(enqueue_runtime_host_probe(self.host, due_only=True), (None, False))
        attempt, created = enqueue_runtime_host_probe(self.host, created_by=self.admin)
        self.assertTrue(created)
        self.assertEqual(enqueue_runtime_host_probe(self.host, created_by=self.admin), (attempt, False))
        self.assertEqual(claim_next_runtime_host_probe("isolated-policy-worker").id, attempt.id)
        # A mode switch never cancels or replaces already accepted work.
        self.configure(**{"runtime.host_probe_mode": "automatic"})
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "running")

    def test_manual_persists_long_expiry_and_real_failure_is_not_hidden(self):
        self.configure(**{"runtime.host_probe_mode": "manual"})
        snapshot = self.probe(errors=["comfyui_probe_failed:fixture"])
        self.assertEqual(snapshot.expires_at - snapshot.captured_at, timedelta(days=36500))
        self.assertEqual(snapshot.probe_status, "partial")
        self.assertIn("comfyui_probe_failed:fixture", snapshot.details["probe_errors"])
        self.assertEqual(snapshot.details["runtime_configuration"]["host_probe_mode"], "manual")
        self.assertEqual(snapshot.details["runtime_configuration"]["host_probe_validity_seconds"], 36500 * 86400)

    def test_configuration_changes_do_not_rewrite_existing_snapshot(self):
        original = self.probe()
        before = (original.expires_at, original.details_sha256)
        self.configure(**{"runtime.host_probe_mode": "manual", "runtime.host_probe_manual_validity_days": 5000})
        manual = self.probe()
        self.assertEqual(manual.expires_at - manual.captured_at, timedelta(days=5000))
        original.refresh_from_db()
        self.assertEqual(before, (original.expires_at, original.details_sha256))
        self.configure(**{"runtime.host_probe_mode": "automatic", "runtime.capability_ttl_seconds": 86400})
        automatic = self.probe()
        self.assertEqual(automatic.expires_at - automatic.captured_at, timedelta(days=1))
        manual.refresh_from_db()
        self.assertEqual(manual.expires_at - manual.captured_at, timedelta(days=5000))

    def test_switch_to_auto_refreshes_old_manual_snapshot_at_auto_interval(self):
        now = timezone.now()
        snapshot = HostCapabilitySnapshot.objects.create(host=self.host, revision=1, probe_status="succeeded",
            captured_at=now - timedelta(days=31), expires_at=now + timedelta(days=36469))
        attempt, created = enqueue_runtime_host_probe(self.host, trigger="scheduled", due_only=True)
        self.assertTrue(created)
        self.assertEqual(attempt.trigger, "scheduled")
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.expires_at, now + timedelta(days=36469))

    def test_invalid_modes_and_validities_cannot_publish(self):
        for key, value in [("runtime.host_probe_mode", "off"), ("runtime.capability_ttl_seconds", 0),
                ("runtime.host_probe_manual_validity_days", 0), ("runtime.host_probe_manual_validity_days", 36501)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                self.configure(**{key: value})

    def test_disabled_host_remains_blocked_in_manual_mode(self):
        self.configure(**{"runtime.host_probe_mode": "manual"})
        self.host.status = "disabled"
        self.host.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            enqueue_runtime_host_probe(self.host, created_by=self.admin)
        self.assertFalse(RuntimeHostProbeAttempt.objects.filter(host=self.host).exists())
