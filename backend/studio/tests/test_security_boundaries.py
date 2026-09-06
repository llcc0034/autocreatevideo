from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.test import APIClient

from studio.platform_serializers import (
    BibleImageBindingSelectionSerializer,
    BibleImageRegenerationInputSerializer,
    ComfySavedWorkflowBatchInputSerializer,
    ConfigRevisionSerializer,
    ProductionRunCreateSerializer,
    SelectRouteInputSerializer,
    WorkflowSmokeFixtureInputSerializer,
)
from studio.models import (
    ConfigDefinition,
    ConfigRevision,
    ConfigValue,
    ProductionRecipeVersion,
    ProductionRun,
    SeriesProject,
)
from studio.services.configuration import (
    ConfigurationResolutionError,
    _assert_effective_security_ceiling_values,
    activate_config_revision,
    create_config_revision,
    publish_config_revision,
    redact_sensitive_bindings,
    resolve_configuration,
    resolve_security_boundaries,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.asset_generation import _frozen_max_poll_seconds
from studio.services.assembly import AssemblyError, _ffmpeg_command
from studio.services.bible_reference_contract import (
    bible_candidate_unit_key,
    parse_bible_candidate_unit_key,
)
from studio.services.comfyui import ComfyUIError
from studio.services.prompt_normalization_contract import (
    PromptNormalizationContractError,
    _replace_protected_tokens_in_fact_tree,
    _validated_fact_tree,
)
from studio.services.provider_registry import _safe_chat_usage
from studio.services.script_review import _verified_snapshot
from studio.services.task_queue import _validated_comfy_timeout
from studio.services.workflow_semantics import WorkflowSemanticError, _safe_output_prefix
from studio.services.workflow_smoke_fixtures import (
    _fixture_max_assets,
    _fixture_max_values,
    _fixture_values_max_bytes,
)


SECURITY_BOUNDARY_KEY = "security.production.max_concurrency"
STANDARD_VALUE_KEY = "production.default_concurrency"
INTEGRITY_BOUNDARY_KEY = "security.snapshot.stage_max_bytes"
RUNTIME_CONTRACT_BOUNDARIES = {
    "security.workflow_smoke.fixture_values_max_bytes": 65_536,
    "security.workflow_smoke.fixture_max_values": 128,
    "security.workflow_smoke.fixture_max_assets": 18,
    "security.deferred.frame_extract_timeout_max_seconds": 120,
    "security.workflow.output_prefix_max_characters": 300,
    "security.prompt_normalization.generic_max_depth": 8,
    "security.prompt_normalization.fact_tree_max_depth": 5,
    "security.prompt_normalization.fact_tree_max_fields": 40,
    "security.prompt_normalization.fact_tree_key_max_characters": 80,
}
API_EFFECTIVE_LIMIT_BOUNDARIES = {
    "security.production.max_bible_reference_assets_per_entity": 32,
    "security.production.max_bible_regeneration_entities_per_request": 32,
}


def _active_system_revision() -> ConfigRevision:
    return ConfigRevision.objects.get(
        scope_type=ConfigRevision.ScopeType.SYSTEM,
        status=ConfigRevision.Status.PUBLISHED,
        activation_state=ConfigRevision.ActivationState.ACTIVE,
    )


def _create_standard_definition(
    key: str,
    *,
    value_type: str = ConfigDefinition.ValueType.INTEGER,
    value_schema: dict | None = None,
    allowed_scopes: str = "system,project,run",
    restart_required: bool = False,
) -> ConfigDefinition:
    definition = ConfigDefinition(
        key=key,
        domain="test",
        configuration_class=ConfigDefinition.ConfigurationClass.STANDARD,
        value_type=value_type,
        description="Security-boundary regression-test definition",
        allowed_scopes=allowed_scopes,
        allowed_default_sources="",
        restart_required=restart_required,
        is_required=False,
        is_secret=False,
        is_active=True,
    )
    assign_json_contract(definition, "value_schema", value_schema or {})
    definition.save()
    return definition


class SecurityBoundaryResolverTests(TestCase):
    def test_0112_runtime_contract_boundaries_are_active_and_system_owned(self):
        active = _active_system_revision()
        definitions = {
            row.key: row
            for row in ConfigDefinition.objects.filter(
                key__in=RUNTIME_CONTRACT_BOUNDARIES,
            )
        }
        values = dict(
            active.values.filter(
                definition__key__in=RUNTIME_CONTRACT_BOUNDARIES,
            ).values_list("definition__key", "value")
        )

        self.assertEqual(set(definitions), set(RUNTIME_CONTRACT_BOUNDARIES))
        self.assertEqual(values, RUNTIME_CONTRACT_BOUNDARIES)
        for definition in definitions.values():
            with self.subTest(key=definition.key):
                self.assertEqual(
                    definition.configuration_class,
                    ConfigDefinition.ConfigurationClass.SECURITY_BOUNDARY,
                )
                self.assertEqual(definition.domain, "security")
                self.assertEqual(definition.allowed_scopes, "system")
                self.assertEqual(definition.allowed_default_sources, "")
                self.assertTrue(definition.is_required)
                self.assertFalse(definition.is_secret)
                self.assertTrue(definition.is_active)

    def test_missing_boundary_definition_fails_closed(self):
        missing_key = "security.test.missing_boundary"

        with self.assertRaises(ConfigurationResolutionError) as raised:
            resolve_security_boundaries(required_keys=(missing_key,))

        self.assertEqual(raised.exception.code, "security_boundary_definition_missing")
        self.assertEqual(raised.exception.missing_keys, (missing_key,))

    def test_0114_api_effective_limits_and_advisory_schemas_are_current(self):
        active = _active_system_revision()
        definitions = {
            row.key: row
            for row in ConfigDefinition.objects.filter(
                key__in=API_EFFECTIVE_LIMIT_BOUNDARIES,
            )
        }
        values = dict(
            active.values.filter(
                definition__key__in=API_EFFECTIVE_LIMIT_BOUNDARIES,
            ).values_list("definition__key", "value")
        )
        self.assertEqual(set(definitions), set(API_EFFECTIVE_LIMIT_BOUNDARIES))
        self.assertEqual(values, API_EFFECTIVE_LIMIT_BOUNDARIES)
        for definition in definitions.values():
            self.assertEqual(
                definition.configuration_class,
                ConfigDefinition.ConfigurationClass.SECURITY_BOUNDARY,
            )
            self.assertEqual(definition.allowed_scopes, "system")

        schemas = dict(ConfigDefinition.objects.filter(key__in=(
            "production.recommended_max_pending",
            "production.recommended_max_active_per_user",
            "production.script_auto_split_trigger_chars",
            "production.script_auto_split_target_chars",
            "runtime.task_health_failure_limit",
            "delivery.ai_label_text",
            "recommendation.vram_tiers",
        )).values_list("key", "value_schema"))
        for key in (
            "production.recommended_max_pending",
            "production.recommended_max_active_per_user",
            "production.script_auto_split_trigger_chars",
            "production.script_auto_split_target_chars",
            "runtime.task_health_failure_limit",
        ):
            self.assertNotIn("maximum", schemas[key])
        self.assertNotIn("maxLength", schemas["delivery.ai_label_text"])
        self.assertNotIn("maxItems", schemas["recommendation.vram_tiers"])
        for key in (
            "production.default_shot_duration_ms",
            "production.video_shot_min_duration_ms",
            "production.video_shot_max_duration_ms",
        ):
            self.assertEqual(
                ConfigDefinition.objects.get(key=key).value_schema["minimum"],
                1,
            )

    def test_invalid_boundary_definition_contract_fails_closed(self):
        ConfigDefinition.objects.filter(key=INTEGRITY_BOUNDARY_KEY).update(
            allowed_scopes="system,project",
        )

        with self.assertRaises(ConfigurationResolutionError) as raised:
            resolve_security_boundaries(required_keys=(INTEGRITY_BOUNDARY_KEY,))

        self.assertEqual(raised.exception.code, "security_boundary_definition_invalid")
        self.assertEqual(raised.exception.missing_keys, (INTEGRITY_BOUNDARY_KEY,))

    def test_boundary_value_contract_hash_mismatch_fails_closed(self):
        active = _active_system_revision()
        ConfigValue.objects.filter(
            revision=active,
            definition__key=INTEGRITY_BOUNDARY_KEY,
        ).update(value_sha256="0" * 64)

        with self.assertRaises(ValidationError) as raised:
            resolve_security_boundaries(required_keys=(INTEGRITY_BOUNDARY_KEY,))

        self.assertIn("size/checksum", str(raised.exception))

    def test_boundary_revision_hash_mismatch_fails_closed(self):
        active = _active_system_revision()
        ConfigRevision.objects.filter(pk=active.pk).update(values_sha256="0" * 64)

        with self.assertRaises(ConfigurationResolutionError) as raised:
            resolve_security_boundaries(required_keys=(INTEGRITY_BOUNDARY_KEY,))

        self.assertEqual(raised.exception.code, "config_revision_checksum_mismatch")

    def test_integer_ceiling_comparison_is_exact_above_javascript_safe_range(self):
        value_key = "test.precise_integer_ceiling"
        _create_standard_definition(
            value_key,
            value_schema={
                "minimum": 1,
                "security_ceiling_key": INTEGRITY_BOUNDARY_KEY,
            },
        )
        exact_ceiling = 2**53

        with self.assertRaises(ValidationError):
            _assert_effective_security_ceiling_values({
                value_key: exact_ceiling + 1,
                INTEGRITY_BOUNDARY_KEY: exact_ceiling,
            })

        _assert_effective_security_ceiling_values({
            value_key: exact_ceiling,
            INTEGRITY_BOUNDARY_KEY: exact_ceiling,
        })


class ConfigurableSecurityBoundaryRuntimeTests(TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username="runtime-boundary-superuser",
            password="StrongPass123!",
            email="runtime-boundary@example.test",
        )

    def _publish(self, values: dict[str, object]) -> None:
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values=values,
            created_by=self.superuser,
            change_summary="Runtime boundary regression",
        )
        publish_config_revision(revision, published_by=self.superuser)

    def test_bible_variant_and_assembly_fps_follow_lower_and_higher_boundaries(self):
        entity_id = uuid.uuid4()
        creation_policy = resolve_configuration(
            required_keys=("production.project_creation_policy",),
        ).values["production.project_creation_policy"]
        self._publish({
            "security.production.max_bible_candidates_per_entity": 4,
            "security.media.max_fps": 10,
            "production.project_creation_policy": {
                **creation_policy,
                "fps": 10,
            },
        })
        with self.assertRaisesRegex(ValidationError, "1 到 4"):
            bible_candidate_unit_key("character", entity_id, 5)
        with self.assertRaisesRegex(AssemblyError, "1–10"):
            _ffmpeg_command([], Path("blocked.mp4"), 64, 64, 11)

        self._publish({
            "security.production.max_bible_candidates_per_entity": 12,
            "security.media.max_fps": 180,
        })
        unit_key = bible_candidate_unit_key("character", entity_id, 9)
        self.assertTrue(unit_key.endswith(":variant:9"))
        self.assertEqual(
            parse_bible_candidate_unit_key(unit_key)["variant_number"],
            9,
        )
        command = _ffmpeg_command(
            [],
            Path("allowed.mp4"),
            64,
            64,
            144,
        )
        self.assertIn("-filter_complex", command)

    def test_new_workflow_and_prompt_structure_limits_follow_active_values(self):
        self._publish({
            "security.workflow.output_prefix_max_characters": 5,
            "security.workflow_smoke.fixture_values_max_bytes": 128,
            "security.workflow_smoke.fixture_max_values": 2,
            "security.workflow_smoke.fixture_max_assets": 3,
            "security.prompt_normalization.generic_max_depth": 1,
            "security.prompt_normalization.fact_tree_max_depth": 1,
            "security.prompt_normalization.fact_tree_max_fields": 1,
            "security.prompt_normalization.fact_tree_key_max_characters": 3,
        })
        with self.assertRaises(WorkflowSemanticError):
            _safe_output_prefix("abcdef")
        self.assertEqual(_fixture_values_max_bytes(), 128)
        self.assertEqual(_fixture_max_values(), 2)
        self.assertEqual(_fixture_max_assets(), 3)
        with self.assertRaises(PromptNormalizationContractError):
            _replace_protected_tokens_in_fact_tree(
                {"a": {"b": {"c": 1}}},
                mapping={},
                label="facts",
            )
        with self.assertRaises(PromptNormalizationContractError):
            _validated_fact_tree({"a": 1, "b": 2}, label="facts")
        with self.assertRaises(PromptNormalizationContractError):
            _validated_fact_tree({"long": 1}, label="facts")

        self._publish({
            "security.workflow.output_prefix_max_characters": 400,
            "security.workflow_smoke.fixture_values_max_bytes": 131_072,
            "security.workflow_smoke.fixture_max_values": 256,
            "security.workflow_smoke.fixture_max_assets": 36,
            "security.prompt_normalization.generic_max_depth": 4,
            "security.prompt_normalization.fact_tree_max_depth": 4,
            "security.prompt_normalization.fact_tree_max_fields": 4,
            "security.prompt_normalization.fact_tree_key_max_characters": 16,
        })
        self.assertEqual(_safe_output_prefix("x" * 301), "x" * 301)
        self.assertEqual(_fixture_values_max_bytes(), 131_072)
        self.assertEqual(_fixture_max_values(), 256)
        self.assertEqual(_fixture_max_assets(), 36)
        self.assertEqual(
            _replace_protected_tokens_in_fact_tree(
                {"a": {"b": {"c": 1}}},
                mapping={},
                label="facts",
            ),
            {"a": {"b": {"c": 1}}},
        )
        self.assertEqual(
            _validated_fact_tree({"long": 1, "second": 2}, label="facts"),
            {"long": 1, "second": 2},
        )

    def test_existing_runtime_caps_follow_lower_and_higher_active_values(self):
        snapshot = json_contract({"payload": "x" * 100})
        stage = SimpleNamespace(
            output_snapshot=snapshot.value,
            output_snapshot_schema_version=snapshot.schema_version,
            output_snapshot_size_bytes=snapshot.size_bytes,
            output_snapshot_sha256=snapshot.sha256,
        )
        self._publish({
            "security.prompt.max_usage_count": 5,
            "security.provider.async_rest_max_poll_seconds": 20,
            "security.runtime.max_timeout_seconds": 21_600,
            "security.snapshot.stage_max_bytes": 64,
        })
        self.assertEqual(_safe_chat_usage({"prompt_tokens": 6}), {})
        self.assertEqual(
            redact_sensitive_bindings({"usage": {"prompt_tokens": 6}}),
            {"usage": {}},
        )
        with self.assertRaises(ValidationError):
            _frozen_max_poll_seconds({"max_poll_seconds": 21})
        with self.assertRaises(ComfyUIError):
            _validated_comfy_timeout(30_000)
        with self.assertRaises(ValidationError):
            _verified_snapshot(stage)

        self._publish({
            "security.prompt.max_usage_count": 100,
            "security.provider.async_rest_max_poll_seconds": 100,
            "security.runtime.max_timeout_seconds": 40_000,
            "security.snapshot.stage_max_bytes": 1_024,
        })
        self.assertEqual(
            _safe_chat_usage({"prompt_tokens": 6}),
            {"prompt_tokens": 6},
        )
        self.assertEqual(
            redact_sensitive_bindings({"usage": {"prompt_tokens": 6}}),
            {"usage": {"prompt_tokens": 6}},
        )
        self.assertEqual(
            _frozen_max_poll_seconds({"max_poll_seconds": 21}),
            21,
        )
        self.assertEqual(_validated_comfy_timeout(30_000), 30_000.0)
        self.assertEqual(_verified_snapshot(stage), snapshot.value)

    def test_production_capability_projects_current_security_limits(self):
        client = APIClient()
        client.force_authenticate(self.superuser)
        lowered = {
            "security.production.max_variants": 2,
            "security.production.max_routing_routes": 3,
            "security.production.max_episodes": 4,
            "security.production.max_shots_per_episode": 8,
            "security.production.max_total_shots": 20,
            "security.production.max_generation_tasks": 1_000,
            "security.reference.max_bindings": 6,
            "security.production.max_bible_candidates_per_entity": 2,
            "security.production.max_bible_reference_assets_per_entity": 7,
            "security.production.max_bible_regeneration_entities_per_request": 8,
        }
        self._publish(lowered)
        response = client.get("/api/v1/capabilities/production/")
        self.assertEqual(response.status_code, 200, response.data)
        expected_limits = {
            "max_variants": 2,
            "max_routing_routes": 3,
            "max_episodes": 4,
            "max_shots_per_episode": 8,
            "max_total_shots": 20,
            "max_generation_tasks": 1_000,
            "max_reference_bindings": 6,
            "max_bible_candidates_per_entity": 2,
            "max_bible_reference_assets_per_entity": 7,
            "max_bible_regeneration_entities_per_request": 8,
        }
        self.assertEqual(
            {key: response.data["effective_limits"][key] for key in expected_limits},
            expected_limits,
        )
        self.assertEqual(
            response.data["configuration_sources"]["security.production.max_variants"],
            "system",
        )
        self.assertTrue({
            "dialogue_max_entries",
            "dialogue_speaker_id_max_characters",
            "dialogue_text_max_characters",
            "dialogue_delivery_max_characters",
            "execution_monitoring_max_node_type_overrides",
            "execution_monitoring_max_node_overrides",
            "execution_monitoring_max_idle_timeout_seconds",
            "prompt_review_max_candidates",
            "prompt_review_max_advisories",
            "prompt_review_max_input_bytes",
            "prompt_review_max_field_characters",
            "prompt_review_max_usage_count",
            "audit_note_min_characters",
            "audit_note_max_characters",
        }.issubset(response.data["effective_limits"]))
        self.assertEqual(response.data["creation_defaults"], {
            "episode_count": 1,
            "shots_per_episode": 8,
            "episode_duration_seconds": 90,
            "max_generation_tasks": 20,
            "fps": 24,
            "width": 768,
            "height": 1344,
            "default_language": "Chinese",
            "default_aspect_ratio": "9:16",
            "aspect_ratio_options": [
                {
                    "value": "9:16", "label": "9:16 · 竖屏短剧",
                    "width": 768, "height": 1344,
                },
                {
                    "value": "16:9", "label": "16:9 · 横屏影视",
                    "width": 1344, "height": 756,
                },
                {
                    "value": "1:1", "label": "1:1 · 方形内容",
                    "width": 768, "height": 768,
                },
            ],
            "default_shot_media_type": "video",
            "default_include_audio": True,
        })

        self._publish({
            "security.production.max_variants": 12,
            "security.production.max_routing_routes": 24,
            "security.production.max_episodes": 140,
            "security.production.max_shots_per_episode": 240,
            "security.production.max_total_shots": 2_000,
            "security.production.max_generation_tasks": 2_001,
            "security.reference.max_bindings": 96,
            "security.production.max_bible_candidates_per_entity": 12,
            "security.production.max_bible_reference_assets_per_entity": 48,
            "security.production.max_bible_regeneration_entities_per_request": 64,
        })
        response = client.get("/api/v1/capabilities/production/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["effective_limits"]["max_variants"], 12)
        self.assertEqual(response.data["effective_limits"]["max_routing_routes"], 24)
        self.assertEqual(response.data["effective_limits"]["max_episodes"], 140)
        self.assertEqual(
            response.data["effective_limits"]["max_reference_bindings"],
            96,
        )

    def test_api_serializers_do_not_reintroduce_retired_static_maxima(self):
        self._publish({
            "security.production.max_bible_reference_assets_per_entity": 48,
            "security.production.max_bible_regeneration_entities_per_request": 64,
            "security.workflow_smoke.fixture_max_assets": 36,
        })
        create_serializer = ProductionRunCreateSerializer(data={
            "project_id": str(uuid.uuid4()),
            "recipe_version_id": str(uuid.uuid4()),
            "idempotency_key": "api-effective-limit-regression",
            "idea": "想" * 6_001,
            "style_hint": "风" * 2_001,
            "episode_count": 101,
            "shots_per_episode": 101,
            "episode_duration_seconds": 3_601,
            "reference_inputs": [
                {
                    "asset_id": str(uuid.uuid4()),
                    "purpose": "motion_camera_reference",
                    "use_embedded_audio": False,
                }
                for _ in range(65)
            ],
        })
        self.assertTrue(create_serializer.is_valid(), create_serializer.errors)
        route_serializer = SelectRouteInputSerializer(data={
            "stage_key": "media_generation",
            "route_index": 16,
        })
        self.assertTrue(route_serializer.is_valid(), route_serializer.errors)
        batch_serializer = ComfySavedWorkflowBatchInputSerializer(data={
            "items": [
                {
                    "source_path": f"workflow-{index}.json",
                    "expected_sha256": f"{index + 1:064x}",
                    "import_kind": "ignored",
                }
                for index in range(21)
            ],
        })
        self.assertTrue(batch_serializer.is_valid(), batch_serializer.errors)
        smoke_serializer = WorkflowSmokeFixtureInputSerializer(data={
            "schema_version": 2,
            "assets": [
                {"semantic_key": f"asset.{index}", "asset_id": str(uuid.uuid4())}
                for index in range(19)
            ],
        })
        self.assertTrue(smoke_serializer.is_valid(), smoke_serializer.errors)
        selected_ids = [uuid.uuid4() for _ in range(33)]
        bible_serializer = BibleImageBindingSelectionSerializer(data={
            "entity_type": "character",
            "entity_id": str(uuid.uuid4()),
            "selected_artifact_ids": [str(item) for item in selected_ids],
            "selected_library_assets": [],
            "anchor_artifact_id": str(selected_ids[0]),
            "anchor_library_asset_id": None,
        })
        self.assertTrue(bible_serializer.is_valid(), bible_serializer.errors)
        regeneration_serializer = BibleImageRegenerationInputSerializer(data={
            "request_id": str(uuid.uuid4()),
            "expected_source_snapshot_sha256": "a" * 64,
            "entities": [
                {"entity_type": "character", "entity_id": str(uuid.uuid4())}
                for _ in range(33)
            ],
        })
        self.assertTrue(
            regeneration_serializer.is_valid(),
            regeneration_serializer.errors,
        )

        self._publish({
            "security.production.max_bible_reference_assets_per_entity": 1,
        })
        tightened_ids = [uuid.uuid4(), uuid.uuid4()]
        tightened = BibleImageBindingSelectionSerializer(data={
            "entity_type": "character",
            "entity_id": str(uuid.uuid4()),
            "selected_artifact_ids": [str(item) for item in tightened_ids],
            "selected_library_assets": [],
            "anchor_artifact_id": str(tightened_ids[0]),
            "anchor_library_asset_id": None,
        })
        self.assertFalse(tightened.is_valid())
        self.assertIn("最多选择 1 张参考图", str(tightened.errors))


class SecurityBoundaryPermissionTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.staff = user_model.objects.create_user(
            username="security-boundary-staff",
            password="StrongPass123!",
            is_staff=True,
        )
        self.superuser = user_model.objects.create_superuser(
            username="security-boundary-superuser",
            password="StrongPass123!",
            email="security-boundary@example.test",
        )

    def test_django_admin_is_read_only_for_staff_and_superuser(self):
        self.staff.user_permissions.add(
            Permission.objects.get(codename="view_configdefinition"),
            Permission.objects.get(codename="view_configrevision"),
        )
        definition_admin = admin.site._registry[ConfigDefinition]
        revision_admin = admin.site._registry[ConfigRevision]
        definition = ConfigDefinition.objects.get(key=SECURITY_BOUNDARY_KEY)
        revision = _active_system_revision()

        for actor in (self.staff, self.superuser):
            with self.subTest(actor=actor.username):
                request = RequestFactory().get("/admin/studio/configdefinition/")
                request.user = actor
                self.assertTrue(
                    definition_admin.has_view_permission(request, definition),
                )
                self.assertTrue(
                    revision_admin.has_view_permission(request, revision),
                )
                for model_admin, instance in (
                    (definition_admin, definition),
                    (revision_admin, revision),
                ):
                    self.assertFalse(model_admin.has_add_permission(request))
                    self.assertFalse(
                        model_admin.has_change_permission(request, instance),
                    )
                    self.assertFalse(
                        model_admin.has_delete_permission(request, instance),
                    )

    def test_staff_can_carry_unchanged_boundary_rows_and_publish(self):
        active = _active_system_revision()
        boundary_rows = {
            row.definition.key: (row.value, row.value_sha256)
            for row in active.values.select_related("definition").filter(
                definition__configuration_class=(
                    ConfigDefinition.ConfigurationClass.SECURITY_BOUNDARY
                ),
            )
        }
        current_value = resolve_configuration(
            required_keys=("production.default_multi_shot",),
        ).values["production.default_multi_shot"]

        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={"production.default_multi_shot": not current_value},
            created_by=self.staff,
            change_summary="Carry immutable safety envelope",
        )

        carried_rows = {
            row.definition.key: (row.value, row.value_sha256)
            for row in revision.values.select_related("definition").filter(
                definition__configuration_class=(
                    ConfigDefinition.ConfigurationClass.SECURITY_BOUNDARY
                ),
            )
        }
        self.assertTrue(boundary_rows)
        self.assertEqual(carried_rows, boundary_rows)
        self.assertFalse(
            ConfigRevisionSerializer(revision).data["changes_security_boundaries"],
        )

        publish_config_revision(revision, published_by=self.staff)

        revision.refresh_from_db()
        self.assertEqual(revision.status, ConfigRevision.Status.PUBLISHED)
        self.assertEqual(revision.activation_state, ConfigRevision.ActivationState.ACTIVE)

        client = APIClient()
        client.force_authenticate(self.staff)
        response = client.get(f"/api/v1/config-revisions/{revision.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["changes_security_boundaries"])
        self.assertTrue(any(
            row["key"] == SECURITY_BOUNDARY_KEY
            and row["value"] == boundary_rows[SECURITY_BOUNDARY_KEY][0]
            for row in response.data["values"]
        ))

    def test_staff_cannot_explicitly_set_or_unset_boundary(self):
        original_count = ConfigRevision.objects.count()
        current_value = resolve_security_boundaries(
            required_keys=(SECURITY_BOUNDARY_KEY,),
        ).values[SECURITY_BOUNDARY_KEY]

        with self.subTest(operation="set"), self.assertRaises(PermissionDenied):
            create_config_revision(
                scope_type=ConfigRevision.ScopeType.SYSTEM,
                values={SECURITY_BOUNDARY_KEY: current_value + 1},
                created_by=self.staff,
            )
        with self.subTest(operation="unset"), self.assertRaises(PermissionDenied):
            create_config_revision(
                scope_type=ConfigRevision.ScopeType.SYSTEM,
                values={},
                unset_keys=(SECURITY_BOUNDARY_KEY,),
                created_by=self.staff,
            )

        self.assertEqual(ConfigRevision.objects.count(), original_count)

    def test_staff_cannot_publish_superuser_boundary_change(self):
        current_value = resolve_security_boundaries(
            required_keys=(SECURITY_BOUNDARY_KEY,),
        ).values[SECURITY_BOUNDARY_KEY]
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={SECURITY_BOUNDARY_KEY: current_value + 1},
            created_by=self.superuser,
        )
        serialized = ConfigRevisionSerializer(revision).data
        self.assertTrue(serialized["changes_security_boundaries"])

        client = APIClient()
        client.force_authenticate(self.staff)
        visible = client.get(f"/api/v1/config-revisions/{revision.id}/")
        self.assertEqual(visible.status_code, 200, visible.data)
        self.assertTrue(visible.data["changes_security_boundaries"])
        self.assertTrue(any(
            row["key"] == SECURITY_BOUNDARY_KEY
            and row["value"] == current_value + 1
            for row in visible.data["values"]
        ))

        with self.assertRaises(PermissionDenied):
            publish_config_revision(revision, published_by=self.staff)

        revision.refresh_from_db()
        self.assertEqual(revision.status, ConfigRevision.Status.DRAFT)
        self.assertEqual(revision.activation_state, ConfigRevision.ActivationState.INACTIVE)

    def test_unauthorized_failed_activation_does_not_mutate_pending_revision(self):
        restart_definition = _create_standard_definition(
            "test.security_boundary_restart_path",
            value_type=ConfigDefinition.ValueType.STRING,
            value_schema={"format": "safe-relative-path", "minLength": 1},
            allowed_scopes=ConfigRevision.ScopeType.SYSTEM,
            restart_required=True,
        )
        current_value = resolve_security_boundaries(
            required_keys=(SECURITY_BOUNDARY_KEY,),
        ).values[SECURITY_BOUNDARY_KEY]
        baseline = _active_system_revision()
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={
                SECURITY_BOUNDARY_KEY: current_value + 1,
                restart_definition.key: "runtime/security-boundary-test",
            },
            created_by=self.superuser,
        )
        with override_settings(AUTOPROMPT_BOOT_ID="security-boundary-boot-a"):
            publish_config_revision(revision, published_by=self.superuser)
        revision.refresh_from_db()
        self.assertEqual(revision.activation_state, ConfigRevision.ActivationState.PENDING)
        original_error = revision.activation_error

        ConfigValue.objects.filter(
            revision=revision,
            definition=restart_definition,
        ).update(value_sha256="0" * 64)
        with override_settings(AUTOPROMPT_BOOT_ID="security-boundary-boot-b"):
            with self.assertRaises(PermissionDenied):
                activate_config_revision(revision, activated_by=self.staff)

        revision.refresh_from_db()
        baseline.refresh_from_db()
        self.assertEqual(revision.status, ConfigRevision.Status.PUBLISHED)
        self.assertEqual(revision.activation_state, ConfigRevision.ActivationState.PENDING)
        self.assertEqual(revision.activation_error, original_error)
        self.assertEqual(baseline.status, ConfigRevision.Status.PUBLISHED)
        self.assertEqual(baseline.activation_state, ConfigRevision.ActivationState.ACTIVE)


class SecurityBoundaryEffectiveResolutionTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username="security-boundary-owner",
            password="StrongPass123!",
        )
        self.superuser = user_model.objects.create_superuser(
            username="security-boundary-effective-superuser",
            password="StrongPass123!",
            email="security-boundary-effective@example.test",
        )

    def _project(self, suffix: str) -> SeriesProject:
        return SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16", 
            owner=self.owner,
            title=f"Security boundary {suffix}",
            width=768, height=1344, fps=24, default_shot_duration_ms=5000,
            continuity_seed=1,
        )

    def _run(self, project: SeriesProject, suffix: str) -> ProductionRun:
        recipe = ProductionRecipeVersion(
            recipe_key=f"security-boundary-{suffix}",
            version=1,
            name=f"Security boundary {suffix}",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [],
        })
        recipe.save()
        run = ProductionRun(
            owner=self.owner,
            project=project,
            recipe_version=recipe,
            idempotency_key=f"security-boundary-{suffix}",
            request_sha256="a" * 64,
        )
        assign_json_contract(run, "config_snapshot", {
            "schema_version": 1,
            "values": {},
            "sources": {},
            "revision_ids": [],
            "sha256": "",
        })
        assign_json_contract(run, "input_snapshot", {"schema_version": 1})
        run.save()
        return run

    def _publish_boundary(self, value: int) -> ConfigRevision:
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={SECURITY_BOUNDARY_KEY: value},
            created_by=self.superuser,
            change_summary="Tighten concurrency safety envelope",
        )
        publish_config_revision(revision, published_by=self.superuser)
        revision.refresh_from_db()
        self.assertEqual(revision.activation_state, ConfigRevision.ActivationState.ACTIVE)
        return revision

    def test_project_effective_config_fails_after_boundary_is_lowered(self):
        project = self._project("project")
        project_revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=project,
            values={STANDARD_VALUE_KEY: 4},
            created_by=self.owner,
        )
        publish_config_revision(project_revision, published_by=self.owner)
        self.assertEqual(
            resolve_configuration(project=project).values[STANDARD_VALUE_KEY],
            4,
        )

        self._publish_boundary(2)

        with self.assertRaises(ConfigurationResolutionError) as raised:
            resolve_configuration(project=project)
        self.assertEqual(raised.exception.code, "security_boundary_exceeded")

    def test_run_effective_config_fails_after_boundary_is_lowered(self):
        project = self._project("run")
        run = self._run(project, "run")
        run_revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.RUN,
            run=run,
            values={STANDARD_VALUE_KEY: 4},
            created_by=self.owner,
        )
        publish_config_revision(run_revision, published_by=self.owner)
        self.assertEqual(
            resolve_configuration(run=run).values[STANDARD_VALUE_KEY],
            4,
        )

        self._publish_boundary(2)

        with self.assertRaises(ConfigurationResolutionError) as raised:
            resolve_configuration(run=run)
        self.assertEqual(raised.exception.code, "security_boundary_exceeded")
