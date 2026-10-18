"""Historical-state cleanup on the isolated test database, never real data."""
from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from studio.tests.test_configuration_migrations import _MigrationTestCase


class RetiredPromptBindingsMigrationTests(_MigrationTestCase):
    migrate_from = "0166_execution_recommendation_configuration"
    migrate_to = "0167_remove_retired_prompt_bindings"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        self.old_apps = apps
        provider = apps.get_model("studio", "ProviderProfile").objects.create(
            key="retired-binding-provider", name="Shared provider", driver_type="openai_compatible")
        model = apps.get_model("studio", "ModelProfileVersion").objects.create(
            profile_key="retired-binding-model", version=1, provider=provider,
            model_identifier="test-model", modality="text", status="draft")
        self.deployment = apps.get_model("studio", "ModelDeployment").objects.create(
            key="retired-binding-deployment", name="Shared deployment", profile_version=model)
        prompt = apps.get_model("studio", "PromptProfileVersion").objects.create(
            profile_key="retired-binding-profile", version=1, name="Shared strategy",
            model_family="test", operation="prompt.normalize")
        self.binding = apps.get_model("studio", "PromptProfileBinding").objects.create(
            key="retired-binding", name="Retired selection", model_profile_version=model,
            prompt_profile_version=prompt, model_family="test", operation="prompt.normalize")
        instance = apps.get_model("studio", "ModelExecutionInstance").objects.create(
            key="retained-exact-instance", name="Current instance")
        version = apps.get_model("studio", "ModelExecutionInstanceVersion").objects.create(
            instance=instance, version=1, deployment=self.deployment, operation="prompt.normalize",
            prompt_mode="profile", prompt_profile_version=prompt)
        audit = apps.get_model("studio", "PlatformLifecycleAuditEvent")
        self.retired_event = audit.objects.create(action="update", object_type="prompt_profile_binding",
            object_id=self.binding.pk, reason_code="isolated_fixture")
        self.shared_event = audit.objects.create(action="create", object_type="model_execution_instance_version",
            object_id=version.pk, reason_code="isolated_fixture")
        self.shared_rows = [(row._meta.object_name, row.pk, type(row).objects.filter(pk=row.pk).values().get())
            for row in (provider, model, self.deployment, prompt, instance, version, self.shared_event)]

    def migrate(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
        self.apps = executor.loader.project_state([("studio", self.migrate_to)]).apps

    def assert_shared_unchanged(self):
        for name, pk, before in self.shared_rows:
            with self.subTest(model=name):
                self.assertEqual(self.apps.get_model("studio", name).objects.filter(pk=pk).values().get(), before)

    def test_drop_retired_configuration_and_only_its_audit_without_conversion(self):
        self.migrate()
        self.assertNotIn("studio_promptprofilebinding", connection.introspection.table_names())
        with self.assertRaises(LookupError):
            self.apps.get_model("studio", "PromptProfileBinding")
        self.assertFalse(self.apps.get_model("studio", "PlatformLifecycleAuditEvent").objects.filter(pk=self.retired_event.pk).exists())
        self.assert_shared_unchanged()
        for name, field in (("ModelDeploymentSmokeAttempt", "prompt_profile_binding"),
                ("ModelDeploymentCapabilitySnapshot", "prompt_profile_binding"), ("GenerationAttempt", "prompt_profile_binding_key")):
            self.assertNotIn(field, {row.name for row in self.apps.get_model("studio", name)._meta.fields})

    def test_old_evidence_blocks_before_any_destructive_change(self):
        snapshot = self.old_apps.get_model("studio", "ModelDeploymentCapabilitySnapshot").objects.create(
            deployment=self.deployment, revision=1, prompt_profile_binding=self.binding,
            probe_kind="smoke", status="failed", expires_at=timezone.now() + timedelta(hours=1))
        with self.assertRaisesMessage(RuntimeError, "先按R7精确清理旧证据"):
            self.migrate()
        self.assertTrue(type(snapshot).objects.filter(pk=snapshot.pk, prompt_profile_binding=self.binding).exists())
        self.assertTrue(type(self.binding).objects.filter(pk=self.binding.pk).exists())
        self.assertTrue(type(self.retired_event).objects.filter(pk=self.retired_event.pk).exists())

    def test_reversing_schema_does_not_restore_discarded_binding_or_audit(self):
        self.migrate()
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        restored = executor.loader.project_state([("studio", self.migrate_from)]).apps
        self.assertFalse(restored.get_model("studio", "PromptProfileBinding").objects.exists())
        self.assertFalse(restored.get_model("studio", "PlatformLifecycleAuditEvent").objects.filter(pk=self.retired_event.pk).exists())
        self.migrate()
        self.assert_shared_unchanged()
