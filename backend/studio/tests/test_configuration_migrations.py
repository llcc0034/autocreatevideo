from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import copy

from django.core.management import call_command
from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from studio.tests.transaction_fixtures import SeededTransactionTestCase


# These forward-only data changes are not reversible in production. Historical
# tests supply their own data; their setup needs only the older schema. Keep the
# exception list explicit so an unlisted irreversible migration fails closed.
_SCHEMA_FIXTURE_ONLY_DATA_MIGRATIONS = {
    ("studio", "0172_remove_project_default_conditioning_mode"),
    ("studio", "0175_configurable_style_categories"),
    ("studio", "0186_freeze_business_output_parser"),
    ("studio", "0188_freeze_content_source_reader"),
    # Test setup stages an older schema before constructing its own request
    # fixtures. It does not reconstruct archived requests or reverse 0194 data.
    ("studio", "0194_managed_execution_requests"),
}


class _FixtureDefinitionColumn(migrations.RemoveField):
    """Reconstruct discarded columns for historical schema fixtures only.

    0202 cannot recover old identity content. Isolated migration tests provide
    their own rows; empty text here is a staging value, never a data rollback.
    """
    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        field = to_state.apps.get_model(app_label, self.model_name)._meta.get_field(self.name)
        original = field.default
        if field.get_internal_type() in {"CharField", "TextField"}:
            field.default = ""
        try:
            super().database_backwards(app_label, schema_editor, from_state, to_state)
        finally:
            field.default = original


def _migrate_fixture_schema(targets):
    """Stage a historical test schema, never claim a production data rollback.

    Only setup/cleanup uses this helper. Each tested forward/reverse transition
    still uses a normal MigrationExecutor and the unmodified migration files.
    """
    if not getattr(connection, "_test_serialized_contents", None):
        raise RuntimeError("Historical schema fixtures require a serialized isolated test database")
    executor = MigrationExecutor(connection)
    replaced = []
    try:
        for migration, backwards in executor.migration_plan(targets):
            if backwards and (migration.app_label, migration.name) == ("studio", "0202_field_identity_only"):
                replaced.append((migration, migration.operations))
                migration.operations = [
                    _FixtureDefinitionColumn(model_name=operation.model_name, name=operation.name)
                    if type(operation) is migrations.RemoveField else operation
                    for operation in migration.operations
                ]
                continue
            if not backwards or (migration.app_label, migration.name) not in _SCHEMA_FIXTURE_ONLY_DATA_MIGRATIONS:
                continue
            operations = []
            for operation in migration.operations:
                if type(operation) is migrations.RunPython and not operation.reversible:
                    operation = copy(operation)
                    operation.reverse_code = migrations.RunPython.noop
                operations.append(operation)
            replaced.append((migration, migration.operations))
            migration.operations = operations
        executor.migrate(targets)
    finally:
        for migration, operations in replaced:
            migration.operations = operations
    return executor


class _MigrationTestCase(SeededTransactionTestCase):
    # Migration tests deliberately move the shared test database away from the
    # leaf schema.  Django flushes TransactionTestCase data after ``tearDown``;
    # restoring the serialized leaf data on the next case preserves data-
    # migration seeds that a flush cannot replay by itself.
    migrate_from: str
    migrate_to: str
    bootstrap_from: str = ""
    migrate_to_in_setup = True

    def setUp(self):
        super().setUp()
        self._leaf_sqlite_snapshot = None
        if connection.vendor == "sqlite":
            connection.ensure_connection()
            self._leaf_sqlite_snapshot = sqlite3.connect(":memory:")
            connection.connection.backup(self._leaf_sqlite_snapshot)
        # Cleanups also run when setUp or the test body fails, so no following
        # test observes a historical schema.
        self.addCleanup(self._restore_leaf_schema)
        if self.bootstrap_from:
            # Forward-only data migrations cannot be undone by schema rollback.
            # A requested historical bootstrap therefore starts with no studio
            # data/schema; otherwise newer configuration rows collide with the
            # original seed contracts while replaying the old migrations.
            call_command("flush", verbosity=0, interactive=False,
                database=connection.alias, reset_sequences=False,
                inhibit_post_migrate=True)
            _migrate_fixture_schema([("studio", None)])
            executor = MigrationExecutor(connection)
            executor.migrate([("studio", self.bootstrap_from)])
        executor = _migrate_fixture_schema([("studio", self.migrate_from)])
        old_apps = executor.loader.project_state([
            ("studio", self.migrate_from),
        ]).apps
        self.set_up_before_migration(old_apps)

        if self.migrate_to_in_setup:
            executor = MigrationExecutor(connection)
            executor.migrate([("studio", self.migrate_to)])
            self.apps = executor.loader.project_state([
                ("studio", self.migrate_to),
            ]).apps
        else:
            self.apps = old_apps

    def _restore_leaf_schema(self):
        if self._leaf_sqlite_snapshot is not None:
            snapshot = self._leaf_sqlite_snapshot
            self._leaf_sqlite_snapshot = None
            try:
                raw_connection = connection.connection
                if raw_connection.in_transaction:
                    raw_connection.rollback()
                snapshot.backup(raw_connection)
            finally:
                snapshot.close()
            return

        # Backends without SQLite's page-level backup use the conservative
        # path: discard every historical fixture before rebuilding the studio
        # migration graph.  This is intentionally slower, but no fixture data
        # is allowed to cross later fail-closed RunPython migrations.
        call_command(
            "flush",
            verbosity=0,
            interactive=False,
            database=connection.alias,
            reset_sequences=False,
            inhibit_post_migrate=True,
        )
        _migrate_fixture_schema([("studio", None)])
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def set_up_before_migration(self, apps):
        raise NotImplementedError


class ConfigureProductionCapabilitiesMigrationTests(_MigrationTestCase):
    migrate_from = "0045_generatedartifact_comparison_parameters"
    migrate_to = "0046_configure_production_capabilities"

    def set_up_before_migration(self, apps):
        ConfigRevision = apps.get_model("studio", "ConfigRevision")
        Definition = apps.get_model("studio", "ConfigDefinition")
        Value = apps.get_model("studio", "ConfigValue")
        # 0166 deliberately removes this value. Reconstruct the complete
        # historical fixture explicitly, without reversing a production purge
        # or teaching the current runtime to accept the removed key.
        key = "production.prompt_normalization_deployment_id"
        definition, _ = Definition.objects.get_or_create(key=key, defaults={
            "domain": "production", "value_type": "string", "value_schema": {},
        })
        active = ConfigRevision.objects.get(scope_type="system", activation_state="active")
        raw = json.dumps("", ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        Value.objects.get_or_create(revision=active, definition=definition, defaults={
            "value": "", "value_schema_version": 1, "value_size_bytes": len(raw),
            "value_sha256": hashlib.sha256(raw).hexdigest(),
        })
        values = {row.definition.key: row.value for row in active.values.select_related("definition")}
        active.values_sha256 = hashlib.sha256(json.dumps(values, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        active.save(update_fields=("values_sha256",))
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


class PromptFieldValidationRetryPolicyMigrationTests(_MigrationTestCase):
    migrate_from = "0069_h3_ref2va_official_prompt_sources"
    migrate_to = "0070_prompt_field_validation_retry_policy"

    def set_up_before_migration(self, apps):
        ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
        ConfigRevision = apps.get_model("studio", "ConfigRevision")
        ConfigValue = apps.get_model("studio", "ConfigValue")
        ConfigValue.objects.filter(
            definition__key="production.prompt_field_retry_limit",
        ).delete()
        ConfigDefinition.objects.filter(
            key="production.prompt_field_retry_limit",
        ).delete()
        preserved_value = {"kept": True}
        preserved_schema = {"type": "object"}
        preserved_value_bytes = json.dumps(
            preserved_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        preserved_schema_bytes = json.dumps(
            preserved_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        preserved_definition, _ = ConfigDefinition.objects.get_or_create(
            key="test.migration_preserved_value",
            defaults={
                "domain": "test",
                "value_type": "object",
                "description": "Migration fixture that must survive revision cloning.",
                "allowed_scopes": "system",
                "is_required": True,
                "is_active": True,
                "value_schema": preserved_schema,
                "value_schema_version": 1,
                "value_schema_size_bytes": len(preserved_schema_bytes),
                "value_schema_sha256": hashlib.sha256(preserved_schema_bytes).hexdigest(),
                "initial_value": preserved_value,
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": len(preserved_value_bytes),
                "initial_value_sha256": hashlib.sha256(preserved_value_bytes).hexdigest(),
            },
        )
        active = ConfigRevision.objects.filter(
            scope_type="system",
            status="published",
            activation_state="active",
        ).order_by("-revision").first()
        if active is None:
            next_revision = (
                ConfigRevision.objects.filter(scope_type="system")
                .order_by("-revision")
                .values_list("revision", flat=True)
                .first()
                or 0
            ) + 1
            active = ConfigRevision.objects.create(
                scope_type="system",
                revision=next_revision,
                status="published",
                activation_state="active",
                activated_at=timezone.now(),
                published_at=timezone.now(),
                values_sha256="0" * 64,
                change_summary="Prompt retry migration test baseline",
            )
        ConfigValue.objects.get_or_create(
            revision=active,
            definition=preserved_definition,
            defaults={
                "value": preserved_value,
                "value_schema_version": 1,
                "value_size_bytes": len(preserved_value_bytes),
                "value_sha256": hashlib.sha256(preserved_value_bytes).hexdigest(),
            },
        )
        self.previous_active_id = active.id
        self.previous_value_count = ConfigValue.objects.filter(
            revision=active,
        ).count()

    def test_definition_and_active_revision_are_created_without_losing_values(self):
        ConfigDefinition = self.apps.get_model("studio", "ConfigDefinition")
        ConfigRevision = self.apps.get_model("studio", "ConfigRevision")
        ConfigValue = self.apps.get_model("studio", "ConfigValue")
        definition = ConfigDefinition.objects.get(
            key="production.prompt_field_retry_limit",
        )
        self.assertEqual(definition.domain, "production")
        self.assertEqual(definition.value_type, "integer")
        self.assertEqual(definition.initial_value, 2)
        self.assertEqual(definition.value_schema, {"minimum": 0, "maximum": 5})
        self.assertEqual(definition.allowed_scopes, "system,project,run")
        self.assertFalse(definition.restart_required)

        previous = ConfigRevision.objects.get(pk=self.previous_active_id)
        active = ConfigRevision.objects.get(
            scope_type="system",
            activation_state="active",
        )
        self.assertEqual(previous.status, "superseded")
        self.assertEqual(active.based_on_revision_id, previous.id)
        self.assertEqual(
            ConfigValue.objects.filter(revision=active).count(),
            self.previous_value_count + 1,
        )
        preserved = ConfigValue.objects.get(
            revision=active,
            definition__key="test.migration_preserved_value",
        )
        self.assertEqual(preserved.value, {"kept": True})
        value = ConfigValue.objects.get(
            revision=active,
            definition=definition,
        )
        self.assertEqual(value.value, 2)
        expected = json.dumps(2, separators=(",", ":")).encode("utf-8")
        self.assertEqual(value.value_size_bytes, len(expected))
        self.assertEqual(value.value_sha256, hashlib.sha256(expected).hexdigest())


class H3Ref2VAPromptProfileV2MigrationTests(_MigrationTestCase):
    migrate_from = "0071_prompt_profile_field_generation_contract"
    migrate_to = "0072_upgrade_h3_ref2va_prompt_profile_v2"

    def set_up_before_migration(self, apps):
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Field = apps.get_model("studio", "PromptFieldDefinition")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        profile = Profile.objects.create(
            profile_key="minimax_h3.video.ref2va",
            version=4,
            name="H3 Ref2VA v4",
            model_family="minimax_h3",
            operation="video.generate",
            mode="Ref2VA",
            status="published",
            review_state="ready",
            contract_schema_version=1,
            contract_size_bytes=2,
            contract_sha256="a" * 64,
            published_at=timezone.now(),
        )
        specs = (
            ("subject_definitions", "request.h3_ref2va.subject_definitions", "label_block"),
            ("summary", "request.h3_ref2va.summary", "label_colon"),
            ("retention_analysis", "request.h3_ref2va.retention_analysis", "label_block"),
            ("detailed_description", "request.h3_ref2va.detailed_description", "label_block"),
            ("overall_soundscape", "request.h3_ref2va.overall_soundscape", "label_colon"),
            ("non_diegetic_music", "request.h3_ref2va.non_diegetic_music", "label_colon"),
        )
        for position, (field_key, selector, label_style) in enumerate(specs, start=1):
            definition, _created = Field.objects.get_or_create(
                key=field_key,
                defaults={
                    "label": field_key,
                    "category": "prompt",
                    "description": "Migration fixture field",
                    "impact": "Migration fixture impact",
                    "value_type": "long_text",
                    "default_value": None,
                    "value_options": [],
                    "is_required": False,
                    "is_active": True,
                    "display_order": position,
                },
            )
            Rule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector=selector,
                channel="positive_prompt",
                position=position,
                label_style=label_style,
                separator="newline",
                required=True,
                omit_if_empty=False,
                max_chars=7000,
                generation_source_selector=selector,
            )
        Binding.objects.create(
            key="minimax-h3-ref2va-official-v4",
            name="H3 Ref2VA v4",
            operation="video.generate",
            model_family="minimax_h3",
            mode="Ref2VA",
            prompt_profile_version=profile,
            priority=100,
            is_active=True,
        )

    def test_v5_is_published_with_configured_layer_two_rules_and_binding(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        profile = Profile.objects.get(
            profile_key="minimax_h3.video.ref2va",
            version=5,
        )
        self.assertEqual(profile.contract_schema_version, 2)
        self.assertEqual(profile.status, "published")
        self.assertEqual(
            profile.source_projection_key,
            "classified_reference_context_ir_v1",
        )
        rules = list(
            profile.field_rules.select_related("field_definition")
            .order_by("position", "id")
        )
        self.assertEqual(
            [rule.generation_mode for rule in rules],
            ["server_locked"] * 3 + ["model_generated"] * 3,
        )
        self.assertEqual(
            [rule.generation_output_semantics for rule in rules],
            ["final"] * 3 + ["contribution"] * 3,
        )
        self.assertEqual(
            [rule.generation_source_selector for rule in rules[3:]],
            ["shot.detailed_description", "shot.sound_effects", "shot.music"],
        )
        self.assertEqual(
            [(rule.generation_min, rule.generation_max) for rule in rules[3:]],
            [(140, 220), (1, 80), (1, 60)],
        )
        contract = {
            "schema_version": 2,
            "profile_key": profile.profile_key,
            "version": profile.version,
            "model_family": profile.model_family,
            "operation": profile.operation,
            "mode": profile.mode,
            "source_projection_key": profile.source_projection_key,
            "rules": [{
                "field_key": rule.field_definition.key,
                "field_value_type": rule.field_definition.value_type,
                "field_default_value": rule.field_definition.default_value,
                "field_value_options": rule.field_definition.value_options,
                "field_required": rule.field_definition.is_required,
                "source_selector": rule.source_selector,
                "channel": rule.channel,
                "position": rule.position,
                "label_style": rule.label_style,
                "separator": rule.separator,
                "required": rule.required,
                "omit_if_empty": rule.omit_if_empty,
                "max_chars": rule.max_chars,
                "generation_mode": rule.generation_mode,
                "generation_source_selector": rule.generation_source_selector,
                "generation_instruction": rule.generation_instruction,
                "generation_output_semantics": rule.generation_output_semantics,
                "generation_required": rule.generation_required,
                "language_policy": rule.language_policy,
                "length_unit": rule.length_unit,
                "generation_min": rule.generation_min,
                "generation_max": rule.generation_max,
                "generation_validator_keys": rule.generation_validator_keys,
            } for rule in rules],
        }
        encoded = json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(profile.contract_size_bytes, len(encoded))
        self.assertEqual(profile.contract_sha256, hashlib.sha256(encoded).hexdigest())
        self.assertFalse(Binding.objects.get(
            key="minimax-h3-ref2va-official-v4",
        ).is_active)
        self.assertTrue(Binding.objects.get(
            key="minimax-h3-ref2va-official-v5",
        ).is_active)


class PromptProfileChannelLimitsV3MigrationTests(_MigrationTestCase):
    migrate_from = "0075_generic_prompt_language_gate_config"
    migrate_to = "0076_prompt_profile_channel_limits_v3"

    @staticmethod
    def _contract(value):
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return len(encoded), hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _profile_contract(profile, rules, *, schema_version, channel_limits=None):
        contract = {
            "schema_version": schema_version,
            "profile_key": profile.profile_key,
            "version": profile.version,
            "model_family": profile.model_family,
            "operation": profile.operation,
            "mode": profile.mode,
            "source_projection_key": profile.source_projection_key,
            "rules": [{
                "field_key": rule.field_definition.key,
                "field_value_type": rule.field_definition.value_type,
                "field_default_value": rule.field_definition.default_value,
                "field_value_options": rule.field_definition.value_options,
                "field_required": rule.field_definition.is_required,
                "source_selector": rule.source_selector,
                "channel": rule.channel,
                "position": rule.position,
                "label_style": rule.label_style,
                "separator": rule.separator,
                "required": rule.required,
                "omit_if_empty": rule.omit_if_empty,
                "max_chars": rule.max_chars,
                "generation_mode": rule.generation_mode,
                "generation_source_selector": rule.generation_source_selector,
                "generation_instruction": rule.generation_instruction,
                "generation_output_semantics": rule.generation_output_semantics,
                "generation_required": rule.generation_required,
                "language_policy": rule.language_policy,
                "length_unit": rule.length_unit,
                "generation_min": rule.generation_min,
                "generation_max": rule.generation_max,
                "generation_validator_keys": rule.generation_validator_keys,
            } for rule in rules],
        }
        if schema_version >= 3:
            contract["channel_limits"] = channel_limits or {}
        return contract

    def set_up_before_migration(self, apps):
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Field = apps.get_model("studio", "PromptFieldDefinition")
        Binding = apps.get_model("studio", "PromptProfileBinding")

        profile = Profile.objects.create(
            profile_key="minimax_h3.video.ref2va",
            version=6,
            name="H3 Ref2VA frozen v6",
            model_family="minimax_h3",
            operation="video.generate",
            mode="Ref2VA",
            source_projection_key="classified_reference_context_ir_v1",
            status="published",
            review_state="ready",
            contract_schema_version=2,
            contract_size_bytes=2,
            contract_sha256="",
            published_at=timezone.now(),
        )
        specs = (
            (
                "subject_definitions",
                "request.h3_ref2va.subject_definitions",
                "label_block",
                "server_locked",
                "final",
                "none",
                None,
                None,
            ),
            (
                "summary",
                "request.h3_ref2va.summary",
                "label_colon",
                "server_locked",
                "final",
                "none",
                None,
                None,
            ),
            (
                "retention_analysis",
                "request.h3_ref2va.retention_analysis",
                "label_block",
                "server_locked",
                "final",
                "none",
                None,
                None,
            ),
            (
                "detailed_description",
                "request.h3_ref2va.detailed_description",
                "label_block",
                "model_generated",
                "contribution",
                "english_words",
                140,
                220,
            ),
            (
                "overall_soundscape",
                "request.h3_ref2va.overall_soundscape",
                "label_colon",
                "model_generated",
                "contribution",
                "characters",
                1,
                80,
            ),
            (
                "non_diegetic_music",
                "request.h3_ref2va.non_diegetic_music",
                "label_colon",
                "model_generated",
                "contribution",
                "characters",
                1,
                60,
            ),
        )
        generation_sources = {
            "detailed_description": "shot.detailed_description",
            "overall_soundscape": "shot.sound_effects",
            "non_diegetic_music": "shot.music",
        }
        for position, spec in enumerate(specs, start=1):
            (
                field_key,
                source_selector,
                label_style,
                generation_mode,
                output_semantics,
                length_unit,
                generation_min,
                generation_max,
            ) = spec
            definition, _created = Field.objects.get_or_create(
                key=field_key,
                defaults={
                    "label": field_key,
                    "category": "prompt",
                    "description": "Migration fixture field",
                    "impact": "Migration fixture impact",
                    "value_type": "long_text",
                    "default_value": None,
                    "value_options": [],
                    "is_required": False,
                    "is_active": True,
                    "display_order": position,
                },
            )
            Rule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector=source_selector,
                channel="positive_prompt",
                position=position,
                label_style=label_style,
                separator="newline",
                required=True,
                omit_if_empty=False,
                max_chars=7000,
                generation_mode=generation_mode,
                generation_source_selector=generation_sources.get(field_key, ""),
                generation_instruction=f"Generate {field_key}",
                generation_output_semantics=output_semantics,
                generation_required=generation_mode == "model_generated",
                language_policy="inherit",
                length_unit=length_unit,
                generation_min=generation_min,
                generation_max=generation_max,
                generation_validator_keys=[],
            )
        rules = list(
            Rule.objects.filter(profile_version_id=profile.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        size, sha256 = self._contract(self._profile_contract(
            profile,
            rules,
            schema_version=2,
        ))
        Profile.objects.filter(pk=profile.pk).update(
            contract_size_bytes=size,
            contract_sha256=sha256,
        )
        Binding.objects.create(
            key="minimax-h3-ref2va-official-v6",
            name="H3 Ref2VA frozen v6",
            operation="video.generate",
            model_family="minimax_h3",
            mode="Ref2VA",
            prompt_profile_version=profile,
            priority=100,
            is_active=True,
        )
        self.source_profile_id = profile.id
        self.source_contract_size = size
        self.source_contract_sha256 = sha256

    def test_v7_adds_channel_contract_without_rewriting_frozen_v6(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")

        source = Profile.objects.get(pk=self.source_profile_id)
        self.assertEqual(source.version, 6)
        self.assertEqual(source.contract_schema_version, 2)
        self.assertEqual(source.contract_size_bytes, self.source_contract_size)
        self.assertEqual(source.contract_sha256, self.source_contract_sha256)
        self.assertEqual(source.channel_limits, {})

        target = Profile.objects.get(
            profile_key="minimax_h3.video.ref2va",
            version=7,
        )
        self.assertEqual(target.status, "published")
        self.assertEqual(target.contract_schema_version, 3)
        self.assertEqual(target.channel_limits, {
            "positive_prompt": {
                "max_chars": 7000,
                "warn_at_chars": 6200,
            },
        })
        rules = list(
            target.field_rules.select_related("field_definition")
            .order_by("position", "id")
        )
        detail = next(
            rule for rule in rules
            if rule.field_definition.key == "detailed_description"
        )
        self.assertEqual(detail.length_unit, "english_words")
        self.assertEqual(detail.generation_min, 350)
        self.assertEqual(detail.generation_max, 500)
        size, sha256 = self._contract(self._profile_contract(
            target,
            rules,
            schema_version=3,
            channel_limits=target.channel_limits,
        ))
        self.assertEqual(target.contract_size_bytes, size)
        self.assertEqual(target.contract_sha256, sha256)
        self.assertFalse(Binding.objects.get(
            key="minimax-h3-ref2va-official-v6",
        ).is_active)
        self.assertTrue(Binding.objects.get(
            key="minimax-h3-ref2va-official-v7",
        ).is_active)


class PromptProfileStaticReferencePolicyV4MigrationTests(_MigrationTestCase):
    migrate_from = "0076_prompt_profile_channel_limits_v3"
    migrate_to = "0077_prompt_profile_static_reference_policy_v4"

    @staticmethod
    def _contract(value):
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return len(encoded), hashlib.sha256(encoded).hexdigest()

    def set_up_before_migration(self, apps):
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Field = apps.get_model("studio", "PromptFieldDefinition")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        profile = Profile.objects.create(
            profile_key="z-image.bible.reference",
            version=1,
            name="Z-Image Bible reference v1",
            model_family="z-image",
            operation="image.generate",
            mode="",
            source_projection_key="direct_v1",
            channel_limits={},
            status="published",
            review_state="ready",
            contract_schema_version=3,
            contract_size_bytes=2,
            contract_sha256="a" * 64,
            published_at=timezone.now(),
        )
        for position, (key, selector, channel) in enumerate((
            ("subject", "request.subject", "positive_prompt"),
            ("theme", "request.theme", "positive_prompt"),
            ("composition", "request.composition", "positive_prompt"),
            ("environment", "request.environment", "positive_prompt"),
            ("lighting", "request.lighting", "positive_prompt"),
            ("color_palette", "request.color_palette", "positive_prompt"),
            ("visual_style", "request.style", "positive_prompt"),
            ("negative_constraints", "request.negative_constraints", "negative_prompt"),
        ), start=1):
            definition, _created = Field.objects.get_or_create(
                key=key,
                defaults={
                    "label": key,
                    "category": "migration",
                    "description": "Static reference migration fixture",
                    "impact": "Static reference migration fixture",
                    "value_type": "long_text",
                    "default_value": None,
                    "value_options": [],
                    "is_required": False,
                    "is_active": True,
                    "display_order": position,
                },
            )
            Rule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector=selector,
                channel=channel,
                position=position,
                label_style="label_colon",
                separator="newline",
                required=position <= 3,
                omit_if_empty=True,
                max_chars=4000,
                generation_mode="passthrough",
                generation_source_selector=selector,
                generation_instruction="",
                generation_output_semantics="final",
                generation_required=False,
                language_policy="inherit",
                length_unit="none",
                generation_min=None,
                generation_max=None,
                generation_validator_keys=[],
            )
        Binding.objects.create(
            key="z-image-turbo-bible-v1",
            name="Z-Image Bible v1",
            operation="image.generate",
            model_family="z-image",
            mode="",
            prompt_profile_version=profile,
            priority=200,
            is_active=True,
        )
        h3 = Profile.objects.create(
            profile_key="minimax_h3.video.ref2va",
            version=7,
            name="H3 Ref2VA v7 migration fixture",
            model_family="minimax_h3",
            operation="video.generate",
            mode="Ref2VA",
            source_projection_key="classified_reference_context_ir_v1",
            channel_limits={
                "positive_prompt": {
                    "max_chars": 7000,
                    "warn_at_chars": 6200,
                },
            },
            status="published",
            review_state="ready",
            contract_schema_version=3,
            contract_size_bytes=2,
            contract_sha256="b" * 64,
            published_at=timezone.now(),
        )
        h3_specs = (
            (
                "subject_definitions",
                "request.h3_ref2va.subject_definitions",
                "label_block",
                "server_locked",
                "final",
                "none",
                None,
                None,
            ),
            (
                "summary",
                "request.h3_ref2va.summary",
                "label_colon",
                "server_locked",
                "final",
                "none",
                None,
                None,
            ),
            (
                "retention_analysis",
                "request.h3_ref2va.retention_analysis",
                "label_block",
                "server_locked",
                "final",
                "none",
                None,
                None,
            ),
            (
                "detailed_description",
                "request.h3_ref2va.detailed_description",
                "label_block",
                "model_generated",
                "contribution",
                "english_words",
                350,
                500,
            ),
            (
                "overall_soundscape",
                "request.h3_ref2va.overall_soundscape",
                "label_colon",
                "model_generated",
                "contribution",
                "characters",
                1,
                80,
            ),
            (
                "non_diegetic_music",
                "request.h3_ref2va.non_diegetic_music",
                "label_colon",
                "model_generated",
                "contribution",
                "characters",
                1,
                60,
            ),
        )
        for position, (
            key,
            selector,
            label_style,
            generation_mode,
            output_semantics,
            length_unit,
            generation_min,
            generation_max,
        ) in enumerate(h3_specs, start=1):
            definition, _created = Field.objects.get_or_create(
                key=key,
                defaults={
                    "label": key,
                    "category": "migration",
                    "description": "H3 migration fixture",
                    "impact": "H3 migration fixture",
                    "value_type": "long_text",
                    "default_value": None,
                    "value_options": [],
                    "is_required": False,
                    "is_active": True,
                    "display_order": position,
                },
            )
            Rule.objects.create(
                profile_version=h3,
                field_definition=definition,
                source_selector=selector,
                channel="positive_prompt",
                position=position,
                label_style=label_style,
                separator="newline",
                required=True,
                omit_if_empty=False,
                max_chars=7000,
                generation_mode=generation_mode,
                generation_source_selector=selector,
                generation_instruction=(
                    f"Generate {key}" if generation_mode == "model_generated" else ""
                ),
                generation_output_semantics=output_semantics,
                generation_required=generation_mode == "model_generated",
                language_policy="inherit",
                length_unit=length_unit,
                generation_min=generation_min,
                generation_max=generation_max,
                generation_validator_keys=[],
            )
        Binding.objects.create(
            key="minimax-h3-ref2va-official-v7",
            name="H3 Ref2VA v7",
            operation="video.generate",
            model_family="minimax_h3",
            mode="Ref2VA",
            prompt_profile_version=h3,
            priority=100,
            is_active=True,
        )
        self.source_profile_id = profile.id

    def test_v2_freezes_static_initial_policy_and_switches_binding(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")

        source = Profile.objects.get(pk=self.source_profile_id)
        self.assertEqual(source.version, 1)
        self.assertEqual(source.source_projection_config, {})
        target = Profile.objects.get(
            profile_key="z-image.bible.reference",
            version=2,
        )
        self.assertEqual(target.contract_schema_version, 4)
        self.assertEqual(target.status, "published")
        policy = target.source_projection_config["static_reference_policy"]
        self.assertEqual(policy["snapshot_selector"], "initial")
        self.assertFalse(policy["allow_context"]["default"])
        self.assertTrue(policy["allow_context"]["target_overrides"]["scene"])
        self.assertEqual(policy["violation_action"], "block")
        rules = list(
            target.field_rules.select_related("field_definition")
            .order_by("position", "id")
        )
        contract = {
            "schema_version": 4,
            "profile_key": target.profile_key,
            "version": target.version,
            "model_family": target.model_family,
            "operation": target.operation,
            "mode": target.mode,
            "source_projection_key": target.source_projection_key,
            "channel_limits": target.channel_limits,
            "source_projection_config": target.source_projection_config,
            "rules": [{
                "field_key": rule.field_definition.key,
                "field_value_type": rule.field_definition.value_type,
                "field_default_value": rule.field_definition.default_value,
                "field_value_options": rule.field_definition.value_options,
                "field_required": rule.field_definition.is_required,
                "source_selector": rule.source_selector,
                "channel": rule.channel,
                "position": rule.position,
                "label_style": rule.label_style,
                "separator": rule.separator,
                "required": rule.required,
                "omit_if_empty": rule.omit_if_empty,
                "max_chars": rule.max_chars,
                "generation_mode": rule.generation_mode,
                "generation_source_selector": rule.generation_source_selector,
                "generation_instruction": rule.generation_instruction,
                "generation_output_semantics": rule.generation_output_semantics,
                "generation_required": rule.generation_required,
                "language_policy": rule.language_policy,
                "length_unit": rule.length_unit,
                "generation_min": rule.generation_min,
                "generation_max": rule.generation_max,
                "generation_validator_keys": rule.generation_validator_keys,
            } for rule in rules],
        }
        size, sha256 = self._contract(contract)
        self.assertEqual(target.contract_size_bytes, size)
        self.assertEqual(target.contract_sha256, sha256)
        self.assertFalse(Binding.objects.get(
            key="z-image-turbo-bible-v1",
        ).is_active)
        self.assertTrue(Binding.objects.get(
            key="z-image-turbo-bible-v2",
        ).is_active)

    def test_h3_v8_only_advances_contract_schema_and_active_binding(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        source = Profile.objects.get(
            profile_key="minimax_h3.video.ref2va",
            version=7,
        )
        target = Profile.objects.get(
            profile_key="minimax_h3.video.ref2va",
            version=8,
        )
        self.assertEqual(target.contract_schema_version, 4)
        self.assertEqual(target.source_projection_config, {})
        for field in (
            "name",
            "description",
            "model_family",
            "operation",
            "mode",
            "source_projection_key",
            "official_reference",
            "channel_limits",
            "review_state",
        ):
            self.assertEqual(getattr(target, field), getattr(source, field))
        source_rules = list(
            source.field_rules.order_by("position", "id").values(
                "field_definition_id",
                "source_selector",
                "channel",
                "position",
                "label_style",
                "separator",
                "required",
                "omit_if_empty",
                "max_chars",
                "generation_mode",
                "generation_source_selector",
                "generation_instruction",
                "generation_output_semantics",
                "generation_required",
                "language_policy",
                "length_unit",
                "generation_min",
                "generation_max",
                "generation_validator_keys",
            )
        )
        target_rules = list(
            target.field_rules.order_by("position", "id").values(
                "field_definition_id",
                "source_selector",
                "channel",
                "position",
                "label_style",
                "separator",
                "required",
                "omit_if_empty",
                "max_chars",
                "generation_mode",
                "generation_source_selector",
                "generation_instruction",
                "generation_output_semantics",
                "generation_required",
                "language_policy",
                "length_unit",
                "generation_min",
                "generation_max",
                "generation_validator_keys",
            )
        )
        self.assertEqual(target_rules, source_rules)
        self.assertFalse(Binding.objects.get(
            key="minimax-h3-ref2va-official-v7",
        ).is_active)
        target_binding = Binding.objects.get(
            key="minimax-h3-ref2va-official-v8",
        )
        self.assertTrue(target_binding.is_active)
        self.assertEqual(
            Binding.objects.filter(
                operation=target_binding.operation,
                model_family=target_binding.model_family,
                mode=target_binding.mode,
                model_profile_version_id=target_binding.model_profile_version_id,
                is_active=True,
            ).count(),
            1,
        )
