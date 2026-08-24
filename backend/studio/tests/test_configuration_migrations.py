from __future__ import annotations

import hashlib
import json

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class _MigrationTestCase(TransactionTestCase):
    migrate_from: str
    migrate_to: str

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        old_apps = executor.loader.project_state([
            ("studio", self.migrate_from),
        ]).apps
        self.set_up_before_migration(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
        self.apps = executor.loader.project_state([
            ("studio", self.migrate_to),
        ]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def set_up_before_migration(self, apps):
        raise NotImplementedError


class ConfigureProductionCapabilitiesMigrationTests(_MigrationTestCase):
    migrate_from = "0045_generatedartifact_comparison_parameters"
    migrate_to = "0046_configure_production_capabilities"

    def set_up_before_migration(self, apps):
        ConfigRevision = apps.get_model("studio", "ConfigRevision")
        self.failed_revision_id = ConfigRevision.objects.create(
            scope_type="system",
            revision=999,
            status="published",
            activation_state="failed",
            values_sha256="0" * 64,
            activation_error="historical failed boot",
        ).id

    def test_historical_failed_revision_does_not_pollute_stable_active_config(self):
        ConfigDefinition = self.apps.get_model("studio", "ConfigDefinition")
        ConfigRevision = self.apps.get_model("studio", "ConfigRevision")
        target_keys = {
            "production.default_resolution_width",
            "production.default_resolution_height",
            "production.default_concurrency",
            "production.video_shot_min_duration_ms",
            "production.video_shot_max_duration_ms",
            "production.script_auto_split_trigger_chars",
            "production.script_auto_split_target_chars",
            "production.task_timeout_seconds",
            "production.recommended_max_pending",
            "production.recommended_max_active_per_user",
            "production.prompt_normalization_deployment_id",
        }

        self.assertTrue(ConfigRevision.objects.filter(
            pk=self.failed_revision_id,
            activation_state="failed",
        ).exists())
        self.assertEqual(
            set(ConfigDefinition.objects.filter(
                key__in=target_keys,
                needs_review=True,
            ).values_list("key", flat=True)),
            set(),
        )


class GenericResolutionBindingMigrationTests(_MigrationTestCase):
    migrate_from = "0046_configure_production_capabilities"
    migrate_to = "0047_require_generic_resolution_bindings"

    def set_up_before_migration(self, apps):
        User = apps.get_model("auth", "User")
        WorkflowTemplate = apps.get_model("studio", "WorkflowTemplate")
        WorkflowTemplateVersion = apps.get_model("studio", "WorkflowTemplateVersion")
        user = User.objects.create(username="migration-publisher")
        template = WorkflowTemplate.objects.create(
            name="Generic migration fixture",
            slug="generic-migration-fixture",
            version=1,
            provider="comfyui",
            output_kind="image",
            definition={},
            input_schema={},
            is_system=True,
            is_active=True,
        )
        published_at = timezone.now()
        published = WorkflowTemplateVersion.objects.create(
            template=template,
            version=1,
            compiler_id="generic.comfy",
            compiler_version=1,
            status="published",
            validation_state="passed",
            validation_message="historical validate passed",
            smoke_test_state="passed",
            smoke_test_message="historical smoke passed",
            validation_evidence={"validated": True},
            validation_evidence_sha256="1" * 64,
            smoke_test_evidence={"smoked": True},
            smoke_test_evidence_sha256="2" * 64,
            is_current=True,
            published_at=published_at,
            published_by_id=user.id,
        )
        validated = WorkflowTemplateVersion.objects.create(
            template=template,
            version=2,
            compiler_id="generic.comfy",
            compiler_version=1,
            status="validated",
            validation_state="passed",
            validation_message="draft validation",
            smoke_test_state="passed",
            smoke_test_message="draft smoke",
            validation_evidence={"validated": True},
            validation_evidence_sha256="3" * 64,
            smoke_test_evidence={"smoked": True},
            smoke_test_evidence_sha256="4" * 64,
            is_current=False,
        )
        self.published_id = published.id
        self.validated_id = validated.id
        self.publisher_id = user.id
        self.published_at = published_at

    def test_published_history_is_retired_without_rewriting_audit_evidence(self):
        WorkflowTemplateVersion = self.apps.get_model(
            "studio", "WorkflowTemplateVersion",
        )
        published = WorkflowTemplateVersion.objects.get(pk=self.published_id)
        self.assertEqual(published.status, "retired")
        self.assertFalse(published.is_current)
        self.assertEqual(published.published_by_id, self.publisher_id)
        self.assertEqual(published.published_at, self.published_at)
        self.assertEqual(published.validation_state, "passed")
        self.assertEqual(published.smoke_test_state, "passed")
        self.assertEqual(published.validation_evidence, {"validated": True})
        self.assertEqual(published.validation_evidence_sha256, "1" * 64)
        self.assertEqual(published.smoke_test_evidence, {"smoked": True})
        self.assertEqual(published.smoke_test_evidence_sha256, "2" * 64)

    def test_unpublished_validated_version_returns_to_draft_and_invalidates_evidence(self):
        WorkflowTemplateVersion = self.apps.get_model(
            "studio", "WorkflowTemplateVersion",
        )
        validated = WorkflowTemplateVersion.objects.get(pk=self.validated_id)
        self.assertEqual(validated.status, "draft")
        self.assertEqual(validated.validation_state, "not_run")
        self.assertEqual(validated.smoke_test_state, "not_run")
        self.assertEqual(validated.validation_evidence, {})
        self.assertEqual(validated.validation_evidence_sha256, "")
        self.assertEqual(validated.smoke_test_evidence, {})
        self.assertEqual(validated.smoke_test_evidence_sha256, "")


class ExactPromptBindingCapabilityMigrationTests(_MigrationTestCase):
    migrate_from = "0060_prompt_normalization_resource_reference"
    migrate_to = "0061_deactivate_invalid_exact_prompt_profile_bindings"

    @staticmethod
    def _contract(value):
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return len(payload), hashlib.sha256(payload).hexdigest()

    def set_up_before_migration(self, apps):
        Provider = apps.get_model("studio", "ProviderProfile")
        Model = apps.get_model("studio", "ModelProfileVersion")
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        driver_size, driver_sha256 = self._contract({})
        provider = Provider.objects.create(
            key="migration-capability-provider",
            name="Migration capability provider",
            driver_type="deepseek_api",
            modalities="text",
            operations="chat_completion",
            driver_config={},
            driver_config_schema_version=1,
            driver_config_size_bytes=driver_size,
            driver_config_sha256=driver_sha256,
        )

        def model(key, capabilities):
            specification = {
                "schema_version": 1,
                "capabilities": capabilities,
            }
            size, sha256 = self._contract(specification)
            return Model.objects.create(
                profile_key=key,
                version=1,
                provider=provider,
                model_identifier="deepseek-migration-test",
                modality="text",
                specification=specification,
                specification_schema_version=1,
                specification_size_bytes=size,
                specification_sha256=sha256,
                status="published",
            )

        invalid_model = model(
            "migration-capability-invalid",
            ["script.generate", "prompt.normalize"],
        )
        valid_model = model(
            "migration-capability-valid",
            ["script.structure"],
        )
        structure_profile = Profile.objects.create(
            profile_key="migration.deepseek.structure",
            version=1,
            name="Migration structure profile",
            model_family="deepseek",
            operation="script.structure",
            status="published",
            review_state="ready",
        )
        self.invalid_binding_id = Binding.objects.create(
            key="migration.deepseek.structure.invalid",
            name="Invalid exact binding",
            operation="script.structure",
            model_family="deepseek",
            model_profile_version=invalid_model,
            prompt_profile_version=structure_profile,
            is_active=True,
        ).id
        self.valid_binding_id = Binding.objects.create(
            key="migration.deepseek.structure.valid",
            name="Valid exact binding",
            operation="script.structure",
            model_family="deepseek",
            model_profile_version=valid_model,
            prompt_profile_version=structure_profile,
            is_active=True,
        ).id
        self.family_binding_id = Binding.objects.create(
            key="migration.deepseek.structure.family",
            name="Family binding",
            operation="script.structure",
            model_family="deepseek",
            prompt_profile_version=structure_profile,
            is_active=True,
        ).id

    def test_only_invalid_active_exact_binding_is_deactivated_and_audited(self):
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        AuditEvent = self.apps.get_model("studio", "PlatformLifecycleAuditEvent")

        self.assertFalse(Binding.objects.get(pk=self.invalid_binding_id).is_active)
        self.assertTrue(Binding.objects.get(pk=self.valid_binding_id).is_active)
        self.assertTrue(Binding.objects.get(pk=self.family_binding_id).is_active)
        audit = AuditEvent.objects.get(
            object_type="prompt_profile_binding",
            object_id=self.invalid_binding_id,
            action="deactivate",
        )
        self.assertEqual(audit.reason_code, "exact_model_capability_missing")
        self.assertTrue(audit.before_summary["is_active"])
        self.assertFalse(audit.after_summary["is_active"])
