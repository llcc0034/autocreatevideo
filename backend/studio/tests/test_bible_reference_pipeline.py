from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase, override_settings
from studio.tests.workflow_projector_fixtures import workflow_projector_reference
from rest_framework.test import APIClient

from studio.platform_serializers import ApprovalDecisionInputSerializer
from studio.models import (
    ApprovalDecision,
    ArtifactSelectionDecision,
    Asset,
    CharacterBible,
    Episode,
    GeneratedArtifact,
    GenerationAttempt,
    GenerationTask,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    PromptArtifact,
    PromptFieldDefinition,
    PromptFieldTranslation,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProviderProfile,
    PropBible,
    SceneBible,
    SeriesProject,
    Shot,
    StageRun,
    Style,
    WorkflowTemplate,
)
from studio.services.bible_reference_contract import (
    BIBLE_IMAGE_GENERATION_PURPOSE,
    BIBLE_IMAGE_GENERATION_STAGE_KEY,
    BIBLE_IMAGE_REVIEW_STAGE_KEY,
    bible_candidate_unit_key,
    parse_bible_candidate_unit_key,
)
from studio.services.configuration import redact_sensitive_bindings
from studio.services.content_contracts import audit_note_bounds
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.media_capabilities import conditioning_mode_contract
from studio.services.production_pipeline import (
    compile_bible_image_prompt_input,
    materialize_compiled_workflow_version,
    materialize_compiled_prompt,
    materialize_generation_task,
)
from studio.services.production_runs import (
    ProductionRunConflict,
    ProductionRunCreateRejected,
    StageBlocked,
    UNATTENDED_DECISION_POLICY_REGISTRY,
    _assert_frozen_formal_objects,
    _assert_bible_prompt_source_current,
    _assert_entity_reference_bible_bindings_ready,
    _canonical_media,
    _enforce_prompt_language_gate,
    _formal_snapshot,
    _freeze_bible_prompt_profile_selection,
    _freeze_stage_input,
    _freeze_stage_routing,
    _execute_unattended_policy_review,
    _save_stage_snapshot,
    _sha256,
    _validate_bible_text_to_image_workflow_snapshot,
    _validate_delivery_recipe_closure,
    _validated_prompt_normalization_candidate_overrides,
    authorize_stage_cost,
    decide_stage,
    regenerate_bible_image_candidates,
)
from studio.services.prompt_profiles import (
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    PromptProfileError,
    PromptProfileResolutionError,
    ResolvedPromptProfile,
    frozen_profile_reference,
    model_family_for_profile,
    publish_prompt_profile,
    validate_bible_image_prompt_profile,
)
from studio.services.prompt_translation import (
    TRANSLATION_CONTEXT_VERSION,
    build_translation_context,
    source_text_hash,
    translation_context_hash,
)
from studio.services.prompt_normalization_contract import (
    normalization_batch_max_items,
    normalization_text_max_characters,
)
from studio.services.generated_artifacts import record_generation_task_artifacts
from studio.services.provenance import persist_task_provenance
from studio.services.task_queue import prepare_workflow
from studio.services.workflow_compiler import compile_frozen_workflow
from studio.services.workflow_versions import (
    import_workflow_version,
    workflow_version_execution_snapshot,
)
from studio.tests.media_prompt_fixtures import (
    MediaPromptOptions,
    build_test_video_prompt,
    sync_consistency_locks,
)
from studio.tests.factories import conditioning_contract, create_test_prompt_profile_binding


def _text_to_image_workflow_snapshot() -> dict:
    semantic_keys = (
        "prompt.positive",
        "prompt.negative",
        "image.width",
        "image.height",
        "sampling.seed",
        "output.prefix",
    )
    return {
        "semantic_bindings": [
            {"semantic_key": key, "node_id": str(index + 1), "input_key": "value"}
            for index, key in enumerate(semantic_keys)
        ],
        "input_contract": {
            "schema_version": 1,
            "workflow_mode": "text_to_image",
            "parameters": {key: {"type": "string"} for key in semantic_keys},
        },
    }


class BibleReferenceContractTests(TestCase):
    def test_approval_input_uses_security_boundaries_for_comment_and_overrides(self):
        _minimum, comment_maximum = audit_note_bounds()
        text_maximum = normalization_text_max_characters()
        item_maximum = normalization_batch_max_items()
        too_long_comment = ApprovalDecisionInputSerializer(data={
            "stage_key": "script_review",
            "decision": "approve",
            "comment": "x" * (comment_maximum + 1),
        })
        self.assertFalse(too_long_comment.is_valid())
        self.assertIn("comment", too_long_comment.errors)
        too_long_text = ApprovalDecisionInputSerializer(data={
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "a" * 64,
            "candidate_overrides": {"request": "x" * (text_maximum + 1)},
        })
        self.assertFalse(too_long_text.is_valid())
        self.assertIn("candidate_overrides", too_long_text.errors)
        too_many_items = ApprovalDecisionInputSerializer(data={
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "a" * 64,
            "candidate_overrides": {
                f"request-{index}": "valid" for index in range(item_maximum + 1)
            },
        })
        self.assertFalse(too_many_items.is_valid())
        self.assertIn("candidate_overrides", too_many_items.errors)

    def test_prompt_normalization_service_revalidates_override_boundaries(self):
        with self.assertRaises(ValidationError):
            _validated_prompt_normalization_candidate_overrides({
                "request": "x" * (normalization_text_max_characters() + 1),
            })
        with self.assertRaises(ValidationError):
            _validated_prompt_normalization_candidate_overrides({
                f"request-{index}": "valid"
                for index in range(normalization_batch_max_items() + 1)
            })

    def test_candidates_are_independently_selectable_variant_groups(self):
        entity_id = uuid4()
        first = parse_bible_candidate_unit_key(
            bible_candidate_unit_key("character", entity_id, 1),
        )
        second = parse_bible_candidate_unit_key(
            bible_candidate_unit_key("character", entity_id, 2),
        )

        self.assertEqual(first["entity_id"], str(entity_id))
        self.assertNotEqual(first["variant_group"], second["variant_group"])
        self.assertTrue(first["variant_group"].endswith(":candidate:1"))
        self.assertTrue(second["variant_group"].endswith(":candidate:2"))

    def test_review_accepts_multiple_candidates_with_one_anchor(self):
        entity_id = uuid4()
        first = uuid4()
        second = uuid4()
        serializer = ApprovalDecisionInputSerializer(data={
            "stage_key": "bible_image_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "a" * 64,
            "bible_image_bindings": [{
                "entity_type": "character",
                "entity_id": str(entity_id),
                "selected_artifact_ids": [str(first), str(second)],
                "anchor_artifact_id": str(second),
            }],
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        binding = serializer.validated_data["bible_image_bindings"][0]
        self.assertEqual(binding["selected_artifact_ids"], [first, second])
        self.assertEqual(binding["anchor_artifact_id"], second)

    def test_review_accepts_mixed_model_and_asset_library_sources(self):
        entity_id = uuid4()
        artifact_id = uuid4()
        asset_id = uuid4()
        serializer = ApprovalDecisionInputSerializer(data={
            "stage_key": "bible_image_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "9" * 64,
            "bible_image_bindings": [{
                "entity_type": "character",
                "entity_id": str(entity_id),
                "selected_artifact_ids": [str(artifact_id)],
                "selected_library_assets": [{
                    "asset_id": str(asset_id),
                    "expected_sha256": "7" * 64,
                }],
                "anchor_library_asset_id": str(asset_id),
            }],
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        binding = serializer.validated_data["bible_image_bindings"][0]
        self.assertEqual(binding["selected_artifact_ids"], [artifact_id])
        self.assertEqual(binding["selected_library_assets"], [{
            "asset_id": asset_id,
            "expected_sha256": "7" * 64,
        }])
        self.assertEqual(binding["anchor_library_asset_id"], asset_id)
        self.assertIsNone(binding.get("anchor_artifact_id"))

    def test_review_rejects_two_anchors_across_sources(self):
        entity_id = uuid4()
        artifact_id = uuid4()
        asset_id = uuid4()
        serializer = ApprovalDecisionInputSerializer(data={
            "stage_key": "bible_image_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "8" * 64,
            "bible_image_bindings": [{
                "entity_type": "character",
                "entity_id": str(entity_id),
                "selected_artifact_ids": [str(artifact_id)],
                "selected_library_assets": [{
                    "asset_id": str(asset_id),
                    "expected_sha256": "6" * 64,
                }],
                "anchor_artifact_id": str(artifact_id),
                "anchor_library_asset_id": str(asset_id),
            }],
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("bible_image_bindings", serializer.errors)

    def test_review_rejects_anchor_outside_selection_and_duplicate_entity(self):
        entity_id = uuid4()
        first = uuid4()
        outside = uuid4()
        serializer = ApprovalDecisionInputSerializer(data={
            "stage_key": "bible_image_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "b" * 64,
            "bible_image_bindings": [{
                "entity_type": "character",
                "entity_id": str(entity_id),
                "selected_artifact_ids": [str(first)],
                "anchor_artifact_id": str(outside),
            }],
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("bible_image_bindings", serializer.errors)

        duplicate = ApprovalDecisionInputSerializer(data={
            "stage_key": "bible_image_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "c" * 64,
            "bible_image_bindings": [
                {
                    "entity_type": "character",
                    "entity_id": str(entity_id),
                    "selected_artifact_ids": [str(first)],
                    "anchor_artifact_id": str(first),
                },
                {
                    "entity_type": "character",
                    "entity_id": str(entity_id),
                    "selected_artifact_ids": [str(outside)],
                    "anchor_artifact_id": str(outside),
                },
            ],
        })
        self.assertFalse(duplicate.is_valid())
        self.assertIn("bible_image_bindings", duplicate.errors)

    def test_storyboard_review_accepts_only_explicit_per_shot_bindings(self):
        shot_id = uuid4()
        asset_id = uuid4()
        serializer = ApprovalDecisionInputSerializer(data={
            "stage_key": "storyboard_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "d" * 64,
            "shot_reference_bindings": [{
                "shot_id": str(shot_id),
                "reference_bindings": {
                    "first_frame_asset_id": str(asset_id),
                },
            }],
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["shot_reference_bindings"][0]["shot_id"],
            shot_id,
        )

    def test_bible_workflow_requires_text_only_materializable_semantics(self):
        _validate_bible_text_to_image_workflow_snapshot(
            _text_to_image_workflow_snapshot(),
        )

        with_asset = _text_to_image_workflow_snapshot()
        with_asset["semantic_bindings"].append({
            "semantic_key": "asset.reference_image",
            "node_id": "7",
            "input_key": "image",
        })
        with self.assertRaisesMessage(ValidationError, "禁止 asset semantic"):
            _validate_bible_text_to_image_workflow_snapshot(with_asset)

        missing_seed = _text_to_image_workflow_snapshot()
        missing_seed["semantic_bindings"] = [
            item for item in missing_seed["semantic_bindings"]
            if item["semantic_key"] != "sampling.seed"
        ]
        with self.assertRaisesMessage(ValidationError, "sampling.seed"):
            _validate_bible_text_to_image_workflow_snapshot(missing_seed)

    def test_ref2va_recipe_cannot_omit_bible_image_closure(self):
        stages = [
            {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
            {"key": "storyboard", "mode": "deterministic", "approval": "auto"},
            {"key": "storyboard_review", "mode": "review", "approval": "required"},
            {"key": "prompt_compile", "mode": "existing_pipeline", "approval": "auto"},
            {
                "key": "media_generation",
                "mode": "existing_pipeline",
                "approval": "auto",
                "routing": {
                    "schema_version": 1,
                    "mode": "fixed",
                    "routes": [{
                        "conditioning_contract": conditioning_contract(
                            "Ref2VA",
                            input_kind="classified_references",
                            requires_bible=True,
                            first_frame=(0, 1),
                            entity_images=(0, 9),
                            reference_videos=(0, 3),
                            paired_video_audios=(0, 3),
                            reference_audios=(0, 3),
                        ),
                    }],
                    "variant_count": 1,
                    "evaluator": None,
                },
            },
            {"key": "artifact_selection", "mode": "review", "approval": "required"},
            {"key": "assembly", "mode": "existing_pipeline", "approval": "auto"},
            {"key": "delivery", "mode": "existing_pipeline", "approval": "auto"},
        ]

        with self.assertRaisesMessage(ValidationError, "不能作为人物、场景和道具"):
            _validate_delivery_recipe_closure(stages)


class BibleReferenceApprovalTests(TestCase):
    PNG_BYTES = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8A"
        "AQUBAScY42YAAAAASUVORK5CYII="
    )

    def setUp(self):
        self.media_directory = TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.settings_override = override_settings(
            MEDIA_ROOT=self.media_directory.name,
            ASSET_LIBRARY_ROOT=str(Path(self.media_directory.name) / "library"),
            COMFYUI_OUTPUT_DIR=str(Path(self.media_directory.name) / "comfy-output"),
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.owner = get_user_model().objects.create_user(
            username="bible-project-owner",
            password="StrongPass123!",
        )
        self.admin = get_user_model().objects.create_superuser(
            username="bible-admin-operator",
            password="StrongPass123!",
            email="bible-admin@example.test",
        )
        self.style = Style.objects.create(
            owner=self.owner,
            slug="bible-reference-style",
            name="Reviewed Style",
            category=Style.Category.OTHER,
            visual_prompt="cinematic low-key amber lighting",
            negative_prompt="identity drift",
        )
        self.project = SeriesProject.objects.create(aspect_ratio="9:16", 
            owner=self.owner,
            title="中文参考图测试",
            width=768, height=1344, fps=24, default_shot_duration_ms=5000,
            continuity_seed=1,
            language="Chinese",
            style=self.style,
            production_settings={
                "translation_glossary": {"弟弟": "Younger Brother"},
            },
        )
        self.old_anchor = Asset.objects.create(
            owner=self.owner,
            kind=Asset.Kind.CHARACTER,
            name="旧锚点",
            mime_type="image/png",
        )
        self.old_anchor.file.save("old-anchor.png", ContentFile(self.PNG_BYTES))
        self.old_anchor.checksum = hashlib.sha256(self.PNG_BYTES).hexdigest()
        self.old_anchor.save(update_fields=("checksum", "updated_at"))
        library_path = Path(self.media_directory.name) / "library" / "global-reference.png"
        library_path.parent.mkdir(parents=True, exist_ok=True)
        library_path.write_bytes(self.PNG_BYTES)
        self.global_reference = Asset.objects.create(
            owner=None,
            kind=Asset.Kind.CHARACTER,
            name="全局人物参考",
            local_path=str(library_path),
            mime_type="image/png",
            checksum=hashlib.sha256(self.PNG_BYTES).hexdigest(),
        )
        self.character = CharacterBible.objects.create(
            project=self.project,
            slug="younger-brother",
            name="弟弟",
            description="在雨夜寻找真相的紧张少年。",
            stable_speaker_id="S1",
            visual_identity="a tense younger brother wearing a dark jacket",
            anchor_asset=self.old_anchor,
        )
        self.character.reference_assets.add(self.global_reference)
        PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type=PromptFieldTranslation.ScopeType.CHARACTER,
            scope_id=self.character.id,
            field_key="character.name",
            source_text=self.character.name,
            source_hash=source_text_hash(self.character.name),
            translated_text="Younger Brother",
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata={
                "translation_context_hash": translation_context_hash(
                    build_translation_context(
                        self.project,
                        scope_type="character",
                        scope_id=self.character.id,
                        field_key="character.name",
                        source_text=self.character.name,
                    )
                ),
                "translation_context_version": TRANSLATION_CONTEXT_VERSION,
            },
            confirmed_by=self.admin,
        )
        self.provider = ProviderProfile(
            key="bible-qwen-image-provider",
            name="Bible Qwen Image Provider",
            driver_type=ProviderProfile.DriverType.ASYNC_REST,
            modalities="image",
            operations="image.generate",
            is_active=True,
        )
        assign_json_contract(self.provider, "driver_config", {})
        self.provider.save()
        self.model_profile = ModelProfileVersion(
            profile_key="bible-qwen-image",
            version=1,
            provider=self.provider,
            model_identifier="Qwen/Qwen-Image",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(self.model_profile, "specification", {
            "schema_version": 1,
            "model_family": "qwen_image",
            "capabilities": ["image.text_to_image"],
        })
        self.model_profile.save()
        self.deployment = ModelDeployment(
            key="bible-qwen-image-deployment",
            name="Bible Qwen Image Deployment",
            profile_version=self.model_profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(self.deployment, "runtime_config", {})
        self.deployment.save()
        self._create_bible_prompt_profile_binding()
        self.recipe = ProductionRecipeVersion(
            recipe_key="bible-reference-approval-test",
            version=1,
            name="Bible reference approval test",
            status=ProductionRecipeVersion.Status.DRAFT,
        )
        assign_json_contract(self.recipe, "definition", {
            "schema_version": 1,
            "stages": [],
        })
        self.recipe.save()
        self.run = ProductionRun(
            owner=self.admin,
            project=self.project,
            recipe_version=self.recipe,
            idempotency_key="admin-operates-owner-project",
            request_sha256="1" * 64,
            status=ProductionRun.Status.AWAITING_APPROVAL,
            current_stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
        )
        assign_json_contract(self.run, "config_snapshot", {
            "schema_version": 1,
            "values": {"artifact.max_bytes": 10 * 1024 * 1024},
        })
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 1})
        self.run.save()

        self.bible_stage = self._stage(
            "bible_review", 1, StageRun.Status.SUCCEEDED, mode="review",
        )
        _save_stage_snapshot(
            self.bible_stage,
            "output_snapshot",
            _formal_snapshot(self.run, self.bible_stage, "approved-bible"),
        )
        self.generation_stage = self._stage(
            BIBLE_IMAGE_GENERATION_STAGE_KEY,
            2,
            StageRun.Status.SUCCEEDED,
        )
        self.review_stage = self._stage(
            BIBLE_IMAGE_REVIEW_STAGE_KEY,
            3,
            StageRun.Status.AWAITING_APPROVAL,
            mode="review",
            approval_required=True,
        )
        self._configure_entity_reference_media_route()
        compiled = compile_bible_image_prompt_input(
            project=self.project,
            entity_type="character",
            entity=self.character,
            model_profile_version=self.model_profile,
        )
        self.compiled = compiled
        with transaction.atomic():
            self.prompt = materialize_compiled_prompt(
                compiled=compiled,
                created_by=self.admin,
            )
        self.artifacts = [
            self._candidate(variant_number=1),
            self._candidate(variant_number=2),
        ]
        _save_stage_snapshot(
            self.generation_stage,
            "output_snapshot",
            _formal_snapshot(
                self.run,
                self.generation_stage,
                "generated-candidates",
            ),
        )

    def test_materialization_rejects_mismatched_frozen_model_identifiers(self):
        compiled = replace(
            self.compiled,
            request_options={
                **self.compiled.request_options,
                "model_identifier": "different-model.safetensors",
            },
        )
        with self.assertRaisesRegex(ValueError, "do not match"):
            with transaction.atomic():
                materialize_compiled_prompt(
                    compiled=compiled,
                    created_by=self.admin,
                )

    def test_bible_prompt_uses_generic_image_conditioning_semantics(self):
        self.assertEqual(
            self.compiled.requested_conditioning_mode, "IMAGE_TEXT_ONLY",
        )
        self.assertEqual(self.compiled.effective_conditioning_mode, "TEXT_ONLY")
        self.assertEqual(self.compiled.structured_payload["mode"], "TEXT_TO_IMAGE")

    @patch("studio.services.production_runs.advance_production_run")
    @patch("studio.services.production_runs._create_media_tasks")
    def test_review_can_append_frozen_model_candidate_idempotently(
        self,
        create_media_tasks,
        advance_run,
    ):
        create_media_tasks.return_value = [Mock(id=uuid4())]
        advance_run.side_effect = lambda run: run
        request_id = uuid4()
        public_sha = json_contract(
            redact_sensitive_bindings(self.generation_stage.output_snapshot),
        ).sha256
        before_artifact_ids = set(
            GeneratedArtifact.objects.filter(
                stage_run=self.generation_stage,
                role="bible_reference_candidate",
            ).values_list("id", flat=True),
        )
        before_anchor_id = self.character.anchor_asset_id

        result = regenerate_bible_image_candidates(
            self.run,
            user=self.admin,
            request_id=request_id,
            expected_source_snapshot_sha256=public_sha,
            entities=[{
                "entity_type": "character",
                "entity_id": self.character.id,
            }],
        )

        self.assertEqual(result.status, ProductionRun.Status.QUEUED)
        create_media_tasks.assert_called_once()
        kwargs = create_media_tasks.call_args.kwargs
        self.assertEqual(
            kwargs["bible_variant_numbers"],
            {("character", str(self.character.id)): [3]},
        )
        self.assertEqual(
            kwargs["generation_intent"]["kind"],
            "human_regenerate",
        )
        self.assertIsNone(kwargs["approved_unknown_cost_total"])
        self.generation_stage.refresh_from_db()
        self.review_stage.refresh_from_db()
        self.character.refresh_from_db()
        self.assertEqual(self.generation_stage.status, StageRun.Status.PENDING)
        self.assertEqual(self.review_stage.status, StageRun.Status.PENDING)
        self.assertEqual(self.review_stage.input_snapshot, {})
        self.assertEqual(self.review_stage.output_snapshot, {})
        self.assertEqual(self.character.anchor_asset_id, before_anchor_id)
        self.assertEqual(
            set(GeneratedArtifact.objects.filter(
                stage_run=self.generation_stage,
                role="bible_reference_candidate",
            ).values_list("id", flat=True)),
            before_artifact_ids,
        )

        repeated = regenerate_bible_image_candidates(
            self.run,
            user=self.admin,
            request_id=request_id,
            expected_source_snapshot_sha256=public_sha,
            entities=[{
                "entity_type": "character",
                "entity_id": self.character.id,
            }],
        )
        self.assertEqual(repeated.id, self.run.id)
        create_media_tasks.assert_called_once()
        with self.assertRaises(ProductionRunConflict) as captured:
            regenerate_bible_image_candidates(
                self.run,
                user=self.admin,
                request_id=request_id,
                expected_source_snapshot_sha256="f" * 64,
                entities=[{
                    "entity_type": "character",
                    "entity_id": self.character.id,
                }],
            )
        self.assertEqual(
            captured.exception.code,
            "bible_regeneration_idempotency_conflict",
        )

    def test_review_regeneration_rejects_stale_candidate_snapshot_without_writes(self):
        before_attempt_count = GenerationAttempt.objects.filter(
            stage_run=self.generation_stage,
        ).count()
        before_event_count = self.generation_stage.route_events.count()

        with self.assertRaises(ProductionRunConflict) as captured:
            regenerate_bible_image_candidates(
                self.run,
                user=self.admin,
                request_id=uuid4(),
                expected_source_snapshot_sha256="e" * 64,
                entities=[{
                    "entity_type": "character",
                    "entity_id": self.character.id,
                }],
            )

        self.assertEqual(captured.exception.code, "bible_image_snapshot_stale")
        self.assertEqual(
            GenerationAttempt.objects.filter(stage_run=self.generation_stage).count(),
            before_attempt_count,
        )
        self.assertEqual(self.generation_stage.route_events.count(), before_event_count)
        self.run.refresh_from_db()
        self.generation_stage.refresh_from_db()
        self.review_stage.refresh_from_db()
        self.assertEqual(self.run.status, ProductionRun.Status.AWAITING_APPROVAL)
        self.assertEqual(self.generation_stage.status, StageRun.Status.SUCCEEDED)
        self.assertEqual(self.review_stage.status, StageRun.Status.AWAITING_APPROVAL)

    def test_ref2va_prompt_compile_fails_without_approved_bible_images(self):
        episode = Episode.objects.create(prompt_settings={"include_audio": True}, 
            project=self.project,
            number=1,
            title="一致性门禁",
        )
        shot = Shot.objects.create(include_audio=True, 
            episode=episode,
            number=1,
            title="共享人物镜头",
            media_type=Shot.MediaType.VIDEO,
            conditioning_mode="Ref2VA",
            duration_ms=1000,
        )
        shot.characters.add(self.character)

        with self.assertRaises(StageBlocked) as captured:
            _assert_entity_reference_bible_bindings_ready(self.run, [shot])

        self.assertEqual(captured.exception.code, "entity_reference_bible_images_required")

    def _freeze_approved_character_binding(self, asset_id: str) -> None:
        self.review_stage.status = StageRun.Status.SUCCEEDED
        self.review_stage.save(update_fields=("status", "updated_at"))
        canonical = {
            "kind": "bible_image_bindings",
            "generation_stage_id": str(self.generation_stage.id),
            "generation_snapshot_sha256": self.generation_stage.output_snapshot_sha256,
            "bindings": [{
                "entity_type": "character",
                "entity_id": str(self.character.id),
                "selected_asset_ids": [asset_id],
                "anchor_asset_id": asset_id,
            }],
        }
        _save_stage_snapshot(self.review_stage, "output_snapshot", {
            "schema_version": 1,
            "stage_key": BIBLE_IMAGE_REVIEW_STAGE_KEY,
            "output_reference": "approved-bible-images",
            "formal_object_type": "bible_image_bindings",
            "canonical": canonical,
            "canonical_sha256": _sha256(canonical),
        })

    def test_ref2va_prompt_compile_rejects_a_shot_without_any_bible_entity_image(self):
        self._freeze_approved_character_binding(str(self.old_anchor.id))
        episode = Episode.objects.create(prompt_settings={"include_audio": True}, 
            project=self.project,
            number=1,
            title="空实体镜头门禁",
        )
        shot = Shot.objects.create(include_audio=True, 
            episode=episode,
            number=1,
            title="仅动态参考视频",
            media_type=Shot.MediaType.VIDEO,
            conditioning_mode="Ref2VA",
            duration_ms=1000,
        )

        with self.assertRaises(StageBlocked) as captured:
            _assert_entity_reference_bible_bindings_ready(self.run, [shot])

        self.assertEqual(captured.exception.code, "entity_reference_shot_bible_image_missing")

    def test_ref2va_two_shots_can_share_the_same_approved_bible_anchor(self):
        asset_id = str(self.old_anchor.id)
        self._freeze_approved_character_binding(asset_id)
        episode = Episode.objects.create(prompt_settings={"include_audio": True}, 
            project=self.project,
            number=1,
            title="双镜一致性",
        )
        shots = []
        for number in (1, 2):
            shot = Shot.objects.create(include_audio=True, 
                episode=episode,
                number=number,
                title=f"共享人物镜头 {number}",
                media_type=Shot.MediaType.VIDEO,
                conditioning_mode="Ref2VA",
                duration_ms=1000,
                reference_bindings={
                    "characters": [{
                        "entity_id": str(self.character.id),
                        "asset_id": asset_id,
                    }],
                },
            )
            shot.characters.add(self.character)
            shots.append(shot)

        _assert_entity_reference_bible_bindings_ready(self.run, shots)

        self.assertEqual(
            [
                shot.reference_bindings["characters"][0]["asset_id"]
                for shot in shots
            ],
            [asset_id, asset_id],
        )

    @patch("studio.services.production_runs.advance_production_run")
    @patch("studio.services.production_runs._create_media_tasks")
    def test_unknown_cost_regeneration_requires_fresh_authorization_and_resumes_once(
        self,
        create_media_tasks,
        advance_run,
    ):
        create_media_tasks.side_effect = [
            StageBlocked(
                "unknown_cost",
                "本次追加生成费用未知",
                awaiting_approval=True,
            ),
            [Mock(id=uuid4())],
        ]
        advance_run.side_effect = lambda run: run
        public_sha = json_contract(
            redact_sensitive_bindings(self.generation_stage.output_snapshot),
        ).sha256

        gated = regenerate_bible_image_candidates(
            self.run,
            user=self.admin,
            request_id=uuid4(),
            expected_source_snapshot_sha256=public_sha,
            entities=[{
                "entity_type": "character",
                "entity_id": self.character.id,
            }],
        )

        self.assertEqual(gated.status, ProductionRun.Status.AWAITING_APPROVAL)
        self.generation_stage.refresh_from_db()
        self.review_stage.refresh_from_db()
        self.assertEqual(
            self.generation_stage.status,
            StageRun.Status.AWAITING_APPROVAL,
        )
        self.assertEqual(self.generation_stage.reason_code, "unknown_cost")
        self.assertEqual(self.review_stage.status, StageRun.Status.PENDING)
        with self.assertRaisesMessage(
            ValidationError,
            "必须为这一次重新生成明确授权上限",
        ):
            authorize_stage_cost(
                gated,
                stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
                allow_external_calls=None,
                budget_limit=None,
                approved_estimated_cost=None,
                currency="USD",
                comment="missing explicit amount",
                user=self.admin,
            )

        resumed = authorize_stage_cost(
            gated,
            stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
            allow_external_calls=None,
            budget_limit=Decimal("10.0000"),
            approved_estimated_cost=Decimal("1.250000"),
            currency="USD",
            comment="authorize this regeneration only",
            user=self.admin,
        )

        self.assertEqual(resumed.status, ProductionRun.Status.QUEUED)
        self.assertEqual(create_media_tasks.call_count, 2)
        resumed_kwargs = create_media_tasks.call_args.kwargs
        self.assertEqual(
            resumed_kwargs["approved_unknown_cost_total"],
            Decimal("1.250000"),
        )
        self.generation_stage.refresh_from_db()
        self.assertNotIn(
            "pending_bible_regeneration",
            self.generation_stage.routing_decision,
        )
        self.assertEqual(
            self.generation_stage.route_events.filter(
                event_type="bible_regeneration_queued",
            ).count(),
            1,
        )

    def test_unattended_policy_selects_the_only_current_candidate_per_entity(self):
        rejected = self.artifacts[1]
        rejected_attempt_id = rejected.generation_attempt_id
        rejected.delete()
        GenerationAttempt.objects.filter(pk=rejected_attempt_id).delete()
        _save_stage_snapshot(
            self.generation_stage,
            "output_snapshot",
            _formal_snapshot(
                self.run,
                self.generation_stage,
                "single-generated-candidate",
            ),
        )
        run_input = {
            **self.run.input_snapshot,
            "interaction_mode": "unattended",
            "unattended_confirmed": True,
        }
        assign_json_contract(self.run, "input_snapshot", run_input)
        self.run.save(update_fields=(
            "input_snapshot", "input_snapshot_schema_version",
            "input_snapshot_size_bytes", "input_snapshot_sha256", "updated_at",
        ))
        policy = UNATTENDED_DECISION_POLICY_REGISTRY[BIBLE_IMAGE_REVIEW_STAGE_KEY]
        policy_sha = _sha256({
            "schema_version": 1,
            "interaction_mode": "unattended",
            "stage_key": BIBLE_IMAGE_REVIEW_STAGE_KEY,
            "decision_policy": policy,
            "recipe_definition_sha256": self.recipe.definition_sha256,
        })
        self.review_stage.decision_policy = policy
        self.review_stage.decision_policy_sha256 = policy_sha
        self.review_stage.save(update_fields=(
            "decision_policy", "decision_policy_sha256", "updated_at",
        ))
        old_anchor_id = self.character.anchor_asset_id

        reference = _execute_unattended_policy_review(
            self.run,
            self.review_stage,
        )

        self.assertTrue(reference.startswith("approval:"))
        self.character.refresh_from_db()
        self.artifacts[0].refresh_from_db()
        self.review_stage.refresh_from_db()
        self.assertNotEqual(self.character.anchor_asset_id, old_anchor_id)
        self.assertEqual(
            self.artifacts[0].status,
            GeneratedArtifact.Status.SELECTED,
        )
        decision = ApprovalDecision.objects.get(stage_run=self.review_stage)
        self.assertEqual(decision.actor, ApprovalDecision.Actor.RECIPE_POLICY)
        self.assertIsNone(decision.decided_by)
        self.assertEqual(decision.decision_policy, policy)
        self.assertEqual(
            self.review_stage.output_snapshot["policy_evidence"]["kind"],
            "unattended_bible_image_policy",
        )

    def _create_bible_prompt_profile_binding(self) -> PromptProfileBinding:
        family = model_family_for_profile(self.model_profile)
        profile = PromptProfileVersion.objects.create(
            profile_key="test.bible.qwen.image",
            version=1,
            name="Reviewed Bible Qwen image",
            model_family=family,
            operation="image.generate",
            mode="",
            review_state=PromptProfileVersion.ReviewState.READY,
        )
        subject = PromptFieldDefinition.objects.create(
            key="test_bible_subject",
            label="Bible subject",
            category="test",
            description="Reviewed translated Bible identity",
            impact="Controls formal reference image identity",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        style = PromptFieldDefinition.objects.create(
            key="test_bible_style",
            label="Bible style",
            category="test",
            description="Reviewed translated visual style",
            impact="Controls formal reference image style",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=subject,
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_SUBJECT,
            generation_source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_SUBJECT,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            label_style=PromptProfileFieldRule.LabelStyle.NONE,
            required=True,
            max_chars=4000,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=style,
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_STYLE,
            generation_source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_STYLE,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=2,
            label_style=PromptProfileFieldRule.LabelStyle.NONE,
            # Style is optional because projects may intentionally have no
            # StylePreset; the formal Bible route must remain executable for
            # character/scene/prop in that case.
            required=False,
            max_chars=4000,
        )
        profile = publish_prompt_profile(profile, actor=self.admin)
        return PromptProfileBinding.objects.create(
            key="test-bible-qwen-image",
            name="Test Bible Qwen image",
            operation="image.generate",
            model_family=family,
            mode="",
            model_profile_version=self.model_profile,
            prompt_profile_version=profile,
            is_active=True,
        )

    def _published_image_profile_for_selector(
        self,
        *,
        suffix: str,
        selector: str,
        required: bool,
    ) -> PromptProfileVersion:
        definition = PromptFieldDefinition.objects.create(
            key=f"test_bible_profile_{suffix}",
            label=f"Bible profile {suffix}",
            category="test",
            description="Bible PromptProfile satisfiability fixture",
            impact="Must be executable for character, scene, and prop",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        profile = PromptProfileVersion.objects.create(
            profile_key=f"test.bible.profile.{suffix}",
            version=1,
            name=f"Bible profile {suffix}",
            model_family=model_family_for_profile(self.model_profile),
            operation="image.generate",
            mode="",
            review_state=PromptProfileVersion.ReviewState.READY,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=definition,
            source_selector=selector,
            generation_source_selector=selector,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            required=required,
            max_chars=4000,
        )
        return publish_prompt_profile(profile, actor=self.admin)

    def _stage(
        self,
        key: str,
        ordinal: int,
        status: str,
        *,
        mode: str = "existing_pipeline",
        approval_required: bool = False,
    ) -> StageRun:
        routing = json_contract({
            "schema_version": 1,
            "mode": "unconfigured",
            "routes": [],
            "variant_count": 1,
            "evaluator": None,
        })
        decision = json_contract({"schema_version": 1, "state": "not_selected"})
        return StageRun.objects.create(
            production_run=self.run,
            stage_key=key,
            ordinal=ordinal,
            mode=mode,
            status=status,
            idempotency_key=f"{self.run.id}:{key}",
            approval_required=approval_required,
            routing=routing.value,
            routing_schema_version=routing.schema_version,
            routing_size_bytes=routing.size_bytes,
            routing_sha256=routing.sha256,
            routing_decision=decision.value,
            routing_decision_schema_version=decision.schema_version,
            routing_decision_size_bytes=decision.size_bytes,
            routing_decision_sha256=decision.sha256,
        )

    def _configure_entity_reference_media_route(self) -> None:
        zero = {"minimum_items": 0, "maximum_items": 0}
        inputs = {
            "total_images": {"minimum_items": 0, "maximum_items": 9},
            "first_frame": dict(zero),
            "last_frame": dict(zero),
            "entity_images": {"minimum_items": 0, "maximum_items": 9},
            "reference_videos": {"minimum_items": 0, "maximum_items": 3},
            "paired_video_audios": {"minimum_items": 0, "maximum_items": 3},
            "reference_audios": {"minimum_items": 0, "maximum_items": 3},
        }
        route_conditioning_contract = {
            "schema_version": 1,
            "adapter_key": "media_conditioning_v1",
            "modes": [{
                "key": "Ref2VA",
                "label": "实体参考",
                "description": "测试配置声明的实体参考模式",
                "input_kind": "classified_references",
                "requires_entity_reference_bible": True,
                "supports_native_audio": True,
                "inputs": inputs,
            }, conditioning_contract(
                "I2VA",
                input_kind="frame_slots",
                first_frame=(1, 1),
            )["modes"][0]],
        }
        route = {
            "index": 0,
            "conditioning_contract": route_conditioning_contract,
            "conditioning_contract_sha256": _sha256(route_conditioning_contract),
        }
        selection = {
            "schema_version": 2,
            "mode": "uniform",
            "route": {"route_index": 0, **route},
            "prompt_profile_selections": [],
            "shot_routes": [],
        }
        routing = {
            "schema_version": 1,
            "mode": "fixed",
            "routes": [route],
            "variant_count": 1,
            "evaluator": None,
            "prompt_profile_selection": {
                "schema_version": 2,
                "mode": "uniform",
                "prompt_profile_selections": [],
                "shot_routes": [],
            },
        }
        selection["media_stage_routing_sha256"] = _sha256(routing)
        media_stage = self._stage(
            "media_generation",
            6,
            StageRun.Status.PENDING,
        )
        assign_json_contract(media_stage, "routing", routing)
        media_stage.save(update_fields=(
            "routing", "routing_schema_version", "routing_size_bytes",
            "routing_sha256", "updated_at",
        ))
        run_input = {
            **self.run.input_snapshot,
            "workflow_selection": selection,
            "workflow_selection_sha256": _sha256(selection),
        }
        assign_json_contract(self.run, "input_snapshot", run_input)
        self.run.save(update_fields=(
            "input_snapshot", "input_snapshot_schema_version",
            "input_snapshot_size_bytes", "input_snapshot_sha256", "updated_at",
        ))

    def _candidate(
        self,
        *,
        variant_number: int,
        entity: CharacterBible | None = None,
        prompt: PromptArtifact | None = None,
    ) -> GeneratedArtifact:
        entity = entity or self.character
        prompt = prompt or self.prompt
        identity = parse_bible_candidate_unit_key(
            bible_candidate_unit_key(
                "character",
                entity.id,
                variant_number,
            ),
        )
        request_contract = json_contract({
            "schema_version": 1,
            "transport": "restricted_async_rest",
            "operation": "image.text_to_image",
            "output_kind": "image",
            "prompt_artifact_id": str(prompt.id),
            "prompt_content_hash": prompt.content_hash,
            "generation_purpose": BIBLE_IMAGE_GENERATION_PURPOSE,
            "target": prompt.request_options["target"],
            "source_snapshot_sha256": prompt.request_options["target"]["source_snapshot_sha256"],
        })
        attempt = GenerationAttempt.objects.create(
            stage_run=self.generation_stage,
            unit_key=bible_candidate_unit_key(
                "character",
                entity.id,
                variant_number,
            ),
            attempt_number=1,
            model_deployment=self.deployment,
            prompt_profile_version=prompt.prompt_profile_version,
            prompt_profile_binding_key=prompt.prompt_profile_binding_key,
            prompt_profile_contract_sha256=prompt.prompt_profile_contract_sha256,
            status=GenerationAttempt.Status.SUCCEEDED,
            operation="image.text_to_image",
            request_sha256=request_contract.sha256,
            request_snapshot=request_contract.value,
            request_snapshot_schema_version=request_contract.schema_version,
            request_snapshot_size_bytes=request_contract.size_bytes,
            request_snapshot_sha256=request_contract.sha256,
        )
        object_key = (
            f"generated-artifacts/{self.owner.id}/{self.project.id}/async/"
            f"{attempt.id}/artifact-1.media"
        )
        path = Path(self.media_directory.name) / object_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.PNG_BYTES)
        checksum = hashlib.sha256(self.PNG_BYTES).hexdigest()
        metadata = json_contract({
            "schema_version": 1,
            "source": "restricted_async_rest",
            "tenant_owner_id": str(self.owner.id),
            "generation_attempt_id": str(attempt.id),
            "production_run_id": str(self.run.id),
            "stage_run_id": str(self.generation_stage.id),
            "request_sha256": attempt.request_sha256,
            "generation_purpose": BIBLE_IMAGE_GENERATION_PURPOSE,
            "target": prompt.request_options["target"],
            "prompt_artifact_id": str(prompt.id),
            "prompt_content_hash": prompt.content_hash,
            "prompt_profile_version_id": str(prompt.prompt_profile_version_id),
            "prompt_profile_binding_key": prompt.prompt_profile_binding_key,
            "prompt_profile_binding_id": prompt.request_options[
                "prompt_profile_binding_id"
            ],
            "prompt_profile_contract_sha256": prompt.prompt_profile_contract_sha256,
        })
        return GeneratedArtifact.objects.create(
            manifest_key=(
                f"bible-candidate:{self.run.id}:{entity.id}:{variant_number}"
            ),
            production_run=self.run,
            stage_run=self.generation_stage,
            generation_attempt=attempt,
            kind=GeneratedArtifact.Kind.IMAGE,
            role="bible_reference_candidate",
            variant_group=identity["variant_group"],
            variant_index=variant_number,
            status=GeneratedArtifact.Status.AVAILABLE,
            storage_backend="managed_local",
            object_key=object_key,
            mime_type="image/png",
            size_bytes=len(self.PNG_BYTES),
            sha256=checksum,
            manifest_metadata=metadata.value,
            manifest_schema_version=metadata.schema_version,
            manifest_metadata_size_bytes=metadata.size_bytes,
            manifest_metadata_sha256=metadata.sha256,
        )

    def _comfy_candidate_from_signed_task(self) -> tuple[GeneratedArtifact, GenerationTask]:
        """Build the real Comfy task/archive chain used by formal Bible approval."""

        GeneratedArtifact.objects.filter(stage_run=self.generation_stage).delete()
        GenerationAttempt.objects.filter(stage_run=self.generation_stage).delete()
        self.provider.driver_type = ProviderProfile.DriverType.COMFYUI
        assign_json_contract(
            self.provider,
            "driver_config",
            {
                "base_url": "http://127.0.0.1:8188",
                "timeout_seconds": 180,
                "poll_interval_seconds": 2,
                "max_poll_seconds": 3_600,
                "capability_ttl_seconds": 3_600,
            },
        )
        self.provider.save(update_fields=(
            "driver_type", "driver_config", "driver_config_schema_version",
            "driver_config_size_bytes", "driver_config_sha256", "updated_at",
        ))
        model_artifact = ModelArtifact.objects.create(
            profile_version=self.model_profile,
            role=ModelArtifact.Role.CHECKPOINT,
            logical_name="base",
            filename="qwen-image.safetensors",
            sha256="a" * 64,
            status=ModelArtifact.Status.AVAILABLE,
        )
        template = WorkflowTemplate.objects.create(
            name="Formal Bible candidate T2I",
            slug=f"formal-bible-candidate-{uuid4()}",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            provider="comfyui",
            model=self.model_profile.model_identifier,
            definition={},
            input_schema={},
            is_system=True,
            is_active=False,
        )
        graph = {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "qwen-image.safetensors"},
            },
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "", "clip": ["1", 1]},
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "", "clip": ["1", 1]},
            },
            "4": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 512, "height": 512, "batch_size": 1},
            },
            "5": {
                "class_type": "KSampler",
                "inputs": {
                    "model": ["1", 0], "positive": ["2", 0],
                    "negative": ["3", 0], "latent_image": ["4", 0],
                    "seed": 0, "steps": 10, "cfg": 5.0,
                    "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                },
            },
            "6": {
                "class_type": "SaveImage",
                "inputs": {"images": "fixture", "filename_prefix": "output"},
            },
        }
        parameters = {
            "prompt.positive": {"type": "string", "required": True},
            "prompt.negative": {"type": "string", "required": False, "default": ""},
            "image.width": {
                "type": "integer", "required": True, "multiple_of": 8,
            },
            "image.height": {
                "type": "integer", "required": True, "multiple_of": 8,
            },
            "sampling.seed": {"type": "integer", "required": True, "min": 0},
            "model.base.filename": {
                "type": "model_artifact", "requirement_alias": "base",
            },
            "output.prefix": {"type": "string", "required": True},
        }
        semantic_bindings = [
            {
                "semantic_key": "model.base.filename", "node_id": "1",
                "input_name": "ckpt_name", "value_type": "model_artifact",
            },
            {
                "semantic_key": "prompt.positive", "node_id": "2",
                "input_name": "text", "value_type": "string",
            },
            {
                "semantic_key": "prompt.negative", "node_id": "3",
                "input_name": "text", "value_type": "string",
            },
            {
                "semantic_key": "image.width", "node_id": "4",
                "input_name": "width", "value_type": "integer",
            },
            {
                "semantic_key": "image.height", "node_id": "4",
                "input_name": "height", "value_type": "integer",
            },
            {
                "semantic_key": "sampling.seed", "node_id": "5",
                "input_name": "seed", "value_type": "integer",
            },
            {
                "semantic_key": "output.prefix", "node_id": "6",
                "input_name": "filename_prefix", "value_type": "string",
            },
        ]
        version = import_workflow_version(
            template=template,
            graph=graph,
            input_contract={
                "schema_version": 2,
                "workflow_mode": "text_to_image",
                "capability": "image.text_to_image",
                "compiler": {"id": "generic.comfy", "version": 1},
                **workflow_projector_reference(),
                "resolution_strategy": {
                    "mode": "explicit_width_height", "node_ids": ["4"],
                },
                "required_dimension_multiple": 8,
                "parameters": parameters,
                "outputs": {
                    "primary": {"node_id": "6", "kind": "image", "required": True},
                },
                "smoke_inputs": {"prompt.positive": "smoke"},
            },
            semantic_bindings=semantic_bindings,
            model_requirements=[{
                "role": ModelArtifact.Role.CHECKPOINT,
                "alias": "base",
                "artifact_id": str(model_artifact.id),
                "expected_filename": model_artifact.filename,
                "expected_sha256": model_artifact.sha256,
                "is_required": True,
            }],
            provider=self.provider,
            model_profile_version=self.model_profile,
        )
        version.status = version.Status.PUBLISHED
        version.validation_state = version.ValidationState.PASSED
        version.smoke_test_state = version.ValidationState.PASSED
        version.validated_deployment = self.deployment
        version.save(update_fields=(
            "status", "validation_state", "smoke_test_state",
            "validated_deployment", "updated_at",
        ))
        workflow_snapshot = workflow_version_execution_snapshot(version)
        compiled = compile_frozen_workflow(
            workflow_snapshot=workflow_snapshot,
            prompt=self.prompt,
            project=self.project,
            overrides={
                "image.width": 512,
                "image.height": 512,
                "sampling.seed": 42,
                "output.prefix": "formal-bible-candidate",
            },
        )
        route_sha = json_contract({
            "workflow_version_id": str(version.id),
            "model_deployment_id": str(self.deployment.id),
        }).sha256
        identity = parse_bible_candidate_unit_key(
            bible_candidate_unit_key("character", self.character.id, 1),
        )
        request = json_contract({
            "schema_version": 1,
            "operation": "media.generate",
            "prompt_artifact_id": str(self.prompt.id),
            "prompt_content_hash": self.prompt.content_hash,
            "generation_purpose": BIBLE_IMAGE_GENERATION_PURPOSE,
            "target": self.prompt.request_options["target"],
            "source_snapshot_sha256": (
                self.prompt.request_options["target"]["source_snapshot_sha256"]
            ),
            "compiled_workflow_sha256": compiled.graph_sha256,
            "workflow_contract_sha256": compiled.workflow_contract_sha256,
            "resolved_semantics_sha256": compiled.resolved_semantics_sha256,
            "asset_bindings_sha256": compiled.asset_bindings_sha256,
            "model_requirements_sha256": compiled.model_requirements_sha256,
            "route_snapshot_sha256": route_sha,
            "model_deployment_id": str(self.deployment.id),
            "model_profile_version_id": str(self.model_profile.id),
            "provider_profile_id": str(self.provider.id),
            "config_snapshot_sha256": self.generation_stage.config_snapshot_sha256,
            "variant_number": 1,
            "execution_identity": {
                "schema_version": 1,
                "seed": 42,
                "output_prefix": "formal-bible-candidate",
            },
            "output_contract": compiled.output_contract,
        })
        attempt = GenerationAttempt.objects.create(
            stage_run=self.generation_stage,
            unit_key=bible_candidate_unit_key("character", self.character.id, 1),
            attempt_number=1,
            model_deployment=self.deployment,
            prompt_profile_version=self.prompt.prompt_profile_version,
            prompt_profile_binding_key=self.prompt.prompt_profile_binding_key,
            prompt_profile_contract_sha256=self.prompt.prompt_profile_contract_sha256,
            route_index=0,
            operation="media.generate",
            status=GenerationAttempt.Status.QUEUED,
            request_sha256=request.sha256,
            request_snapshot=request.value,
            request_snapshot_schema_version=request.schema_version,
            request_snapshot_size_bytes=request.size_bytes,
            request_snapshot_sha256=request.sha256,
        )
        provider_config: dict = {}
        deployment_config: dict = {}
        provider_sha = json_contract(provider_config).sha256
        deployment_sha = json_contract(deployment_config).sha256
        deployment_summary = {
            "schema_version": 1,
            "route_index": 0,
            "model_deployment_id": str(self.deployment.id),
            "model_profile_version_id": str(self.model_profile.id),
            "provider_profile_id": str(self.provider.id),
            "provider_config_sha256": provider_sha,
            "deployment_config_sha256": deployment_sha,
            "credential_binding_sha256": "",
            "endpoint_is_external": False,
            "effective_timeout_seconds": 180,
        }
        with transaction.atomic():
            workflow = materialize_compiled_workflow_version(
                user=self.owner,
                workflow_version=version,
                prompt=self.prompt,
                compiled=compiled,
                route_snapshot_sha256=route_sha,
                workflow_snapshot=workflow_snapshot,
            )
            task = materialize_generation_task(
                user=self.owner,
                workflow=workflow,
                priority=0,
                max_attempts=1,
                capability_snapshot_overrides={
                    "production_run_id": str(self.run.id),
                    "stage_run_id": str(self.generation_stage.id),
                    "generation_attempt_id": str(attempt.id),
                    "model_deployment_id": str(self.deployment.id),
                    "model_profile_version_id": str(self.model_profile.id),
                    "provider_profile_id": str(self.provider.id),
                    "provider_config": provider_config,
                    "deployment_config": deployment_config,
                    "provider_config_sha256": provider_sha,
                    "deployment_config_sha256": deployment_sha,
                    "frozen_execution_config_sha256": json_contract({
                        "provider_config": provider_config,
                        "deployment_config": deployment_config,
                    }).sha256,
                    "credential_binding_sha256": "",
                    "endpoint_is_external": False,
                    "effective_timeout_seconds": 180,
                    "route_index": 0,
                    "config_snapshot_sha256": self.generation_stage.config_snapshot_sha256,
                    "deployment_summary": deployment_summary,
                    "deployment_summary_sha256": json_contract(deployment_summary).sha256,
                },
            )
            attempt.generation_task = task
            attempt.save(update_fields=("generation_task", "updated_at"))

        comfy_output = Path(self.media_directory.name) / "comfy-output"
        comfy_output.mkdir(parents=True, exist_ok=True)
        (comfy_output / "candidate.png").write_bytes(self.PNG_BYTES)
        task.status = GenerationTask.Status.SUCCEEDED
        task.outputs = [{
            "filename": "candidate.png",
            "subfolder": "",
            "type": "output",
            "node_id": "6",
        }]
        task.save(update_fields=("status", "outputs", "updated_at"))
        with override_settings(COMFYUI_OUTPUT_DIR=str(comfy_output)):
            persist_task_provenance(task, list(task.outputs))
            artifacts = record_generation_task_artifacts(
                task,
                production_run=self.run,
                stage_run=self.generation_stage,
                generation_attempt=attempt,
            )
        self.assertEqual(len(artifacts), 1)
        artifact = artifacts[0]
        self.assertEqual(artifact.variant_group, identity["variant_group"])
        attempt.status = GenerationAttempt.Status.SUCCEEDED
        attempt.save(update_fields=("status", "updated_at"))
        self.artifacts = [artifact]
        _save_stage_snapshot(
            self.generation_stage,
            "output_snapshot",
            _formal_snapshot(self.run, self.generation_stage, "generated-candidates"),
        )
        return artifact, task

    def _generic_single_image_video_workflow(
        self,
        *,
        profile,
        provider,
        deployment,
        conditioning,
    ):
        template = WorkflowTemplate.objects.create(
            name="Exact Bible reference video",
            slug=f"exact-bible-reference-video-{uuid4()}",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            provider="comfyui",
            model=profile.model_identifier,
            definition={},
            input_schema={},
            is_system=True,
            is_active=False,
        )
        version = import_workflow_version(
            template=template,
            graph={
                "1": {"class_type": "LoadImage", "inputs": {"image": "fixture.png"}},
                "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
                "3": {
                    "class_type": "EmptyLatentImage",
                    "inputs": {"width": 512, "height": 512, "batch_size": 1},
                },
                "4": {
                    "class_type": "SaveVideo",
                    "inputs": {"video": "fixture", "filename_prefix": "output"},
                },
            },
            input_contract={
                "schema_version": 2,
                "workflow_mode": "image_to_video",
                "capability": "video.image_to_video",
                "compiler": {"id": "generic.comfy", "version": 1},
                **workflow_projector_reference(),
                "conditioning_contract": conditioning,
                "static_semantics": {"audio.enabled": False},
                "resolution_strategy": {
                    "mode": "explicit_width_height", "node_ids": ["3"],
                },
                "required_dimension_multiple": 8,
                "parameters": {
                    "asset.image.source": {
                        "type": "asset_image", "required": True,
                        "input_slot": 1, "asset_category": "image",
                    },
                    "prompt.positive": {"type": "string", "required": True},
                    "image.width": {
                        "type": "integer", "required": True, "multiple_of": 8,
                    },
                    "image.height": {
                        "type": "integer", "required": True, "multiple_of": 8,
                    },
                    "output.prefix": {"type": "string", "required": True},
                },
                "outputs": {
                    "primary": {"node_id": "4", "kind": "video", "required": True},
                },
                "smoke_inputs": {"prompt.positive": "smoke"},
            },
            semantic_bindings=[
                {
                    "semantic_key": "asset.image.source", "node_id": "1",
                    "input_name": "image", "value_type": "asset_image",
                },
                {
                    "semantic_key": "prompt.positive", "node_id": "2",
                    "input_name": "text", "value_type": "string",
                },
                {
                    "semantic_key": "image.width", "node_id": "3",
                    "input_name": "width", "value_type": "integer",
                },
                {
                    "semantic_key": "image.height", "node_id": "3",
                    "input_name": "height", "value_type": "integer",
                },
                {
                    "semantic_key": "output.prefix", "node_id": "4",
                    "input_name": "filename_prefix", "value_type": "string",
                },
            ],
            model_requirements=[],
            provider=provider,
            model_profile_version=profile,
        )
        version.status = version.Status.PUBLISHED
        version.validation_state = version.ValidationState.PASSED
        version.smoke_test_state = version.ValidationState.PASSED
        version.validated_deployment = deployment
        version.save(update_fields=(
            "status", "validation_state", "smoke_test_state",
            "validated_deployment", "updated_at",
        ))
        return version

    def _materialize_exact_video_task(
        self,
        *,
        prompt: PromptArtifact,
        version,
        deployment: ModelDeployment,
        stage: StageRun,
        attempt_number: int,
    ) -> tuple[GenerationAttempt, GenerationTask]:
        workflow_snapshot = workflow_version_execution_snapshot(version)
        compiled = compile_frozen_workflow(
            workflow_snapshot=workflow_snapshot,
            prompt=prompt,
            project=self.project,
            overrides={
                "image.width": 512,
                "image.height": 512,
                "output.prefix": f"shot-{prompt.shot_id}",
            },
        )
        route_sha = json_contract({
            "workflow_version_id": str(version.id),
            "model_deployment_id": str(deployment.id),
        }).sha256
        request = json_contract({
            "schema_version": 1,
            "operation": "media.generate",
            "prompt_artifact_id": str(prompt.id),
            "prompt_content_hash": prompt.content_hash,
            "compiled_workflow_sha256": compiled.graph_sha256,
            "workflow_contract_sha256": compiled.workflow_contract_sha256,
            "asset_bindings_sha256": compiled.asset_bindings_sha256,
            "route_snapshot_sha256": route_sha,
            "model_deployment_id": str(deployment.id),
        })
        attempt = GenerationAttempt.objects.create(
            stage_run=stage,
            unit_key=f"shot:{prompt.shot_id}:variant:1",
            attempt_number=attempt_number,
            model_deployment=deployment,
            prompt_profile_version=prompt.prompt_profile_version,
            prompt_profile_binding_key=prompt.prompt_profile_binding_key,
            prompt_profile_contract_sha256=prompt.prompt_profile_contract_sha256,
            route_index=0,
            operation="media.generate",
            status=GenerationAttempt.Status.QUEUED,
            request_sha256=request.sha256,
            request_snapshot=request.value,
            request_snapshot_schema_version=request.schema_version,
            request_snapshot_size_bytes=request.size_bytes,
            request_snapshot_sha256=request.sha256,
        )
        with transaction.atomic():
            workflow = materialize_compiled_workflow_version(
                user=self.owner,
                workflow_version=version,
                prompt=prompt,
                compiled=compiled,
                route_snapshot_sha256=route_sha,
                workflow_snapshot=workflow_snapshot,
            )
            task = materialize_generation_task(
                user=self.owner,
                workflow=workflow,
                priority=0,
                max_attempts=1,
                capability_snapshot_overrides={
                    "production_run_id": str(self.run.id),
                    "stage_run_id": str(stage.id),
                    "generation_attempt_id": str(attempt.id),
                    "model_deployment_id": str(deployment.id),
                    "model_profile_version_id": str(deployment.profile_version_id),
                    "provider_profile_id": str(deployment.profile_version.provider_id),
                    "route_index": 0,
                },
            )
            attempt.generation_task = task
            attempt.save(update_fields=("generation_task", "updated_at"))
        return attempt, task

    def _approve_selection(self, artifacts, *, anchor):
        public_sha = json_contract(
            redact_sensitive_bindings(self.generation_stage.output_snapshot),
        ).sha256
        with patch(
            "studio.services.production_runs.advance_production_run",
            side_effect=lambda run: run,
        ), patch(
            "studio.services.production_runs._assert_frozen_bible_run_closure",
        ):
            return decide_stage(
                self.run,
                stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
                decision="approve",
                comment="approve multiple formal references",
                user=self.admin,
                expected_source_snapshot_sha256=public_sha,
                bible_image_bindings=[{
                    "entity_type": "character",
                    "entity_id": self.character.id,
                    "selected_artifact_ids": [item.id for item in artifacts],
                    "anchor_artifact_id": anchor.id,
                }],
            )

    def _approve(self):
        return self._approve_selection(
            self.artifacts,
            anchor=self.artifacts[1],
        )

    def _begin_second_run(self, *, idempotency_key: str) -> GeneratedArtifact:
        self.run = ProductionRun(
            owner=self.admin,
            project=self.project,
            recipe_version=self.recipe,
            idempotency_key=idempotency_key,
            request_sha256=hashlib.sha256(idempotency_key.encode()).hexdigest(),
            status=ProductionRun.Status.AWAITING_APPROVAL,
            current_stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
        )
        assign_json_contract(self.run, "config_snapshot", {
            "schema_version": 1,
            "values": {"artifact.max_bytes": 10 * 1024 * 1024},
        })
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 1})
        self.run.save()
        self.bible_stage = self._stage(
            "bible_review", 1, StageRun.Status.SUCCEEDED, mode="review",
        )
        _save_stage_snapshot(
            self.bible_stage,
            "output_snapshot",
            _formal_snapshot(self.run, self.bible_stage, "approved-bible"),
        )
        self.generation_stage = self._stage(
            BIBLE_IMAGE_GENERATION_STAGE_KEY,
            2,
            StageRun.Status.SUCCEEDED,
        )
        self.review_stage = self._stage(
            BIBLE_IMAGE_REVIEW_STAGE_KEY,
            3,
            StageRun.Status.AWAITING_APPROVAL,
            mode="review",
            approval_required=True,
        )
        compiled = compile_bible_image_prompt_input(
            project=self.project,
            entity_type="character",
            entity=self.character,
            model_profile_version=self.model_profile,
        )
        with transaction.atomic():
            self.prompt = materialize_compiled_prompt(
                compiled=compiled,
                created_by=self.admin,
            )
        candidate = self._candidate(variant_number=1)
        self.artifacts = [candidate]
        _save_stage_snapshot(
            self.generation_stage,
            "output_snapshot",
            _formal_snapshot(
                self.run,
                self.generation_stage,
                "generated-candidates",
            ),
        )
        return candidate

    def test_staff_approval_keeps_multiple_assets_and_one_anchor_for_project_owner(self):
        self.assertNotIn(
            "stable-diffusion-xl",
            str(self.prompt.structured_payload).casefold(),
        )
        self.assertEqual(
            self.prompt.structured_payload["model"],
            self.model_profile.model_identifier,
        )
        self.assertFalse(
            self.prompt.structured_payload["quality"]["production_language_gate"]
            ["would_block_enqueue"],
        )
        _enforce_prompt_language_gate([self.prompt])
        self._approve()

        self.character.refresh_from_db()
        self.review_stage.refresh_from_db()
        for artifact in self.artifacts:
            artifact.refresh_from_db()
        selected_assets = [artifact.asset for artifact in self.artifacts]
        self.assertEqual(self.character.anchor_asset_id, selected_assets[1].id)
        self.assertEqual(
            set(self.character.reference_assets.values_list("id", flat=True)),
            {selected_assets[0].id, self.old_anchor.id, self.global_reference.id},
        )
        self.assertTrue(all(asset.owner_id == self.owner.id for asset in selected_assets))
        self.assertTrue(all(asset.name == self.character.name for asset in selected_assets))
        self.assertTrue(
            all(asset.description == self.character.description for asset in selected_assets)
        )
        self.assertTrue(
            all(asset.prompt_hint == self.character.visual_identity for asset in selected_assets)
        )
        self.assertTrue(all(asset.style_id == self.project.style_id for asset in selected_assets))
        self.assertTrue(
            all(
                asset.source_info
                == (
                    f"ProductionRun {self.run.id} 审核绑定的 Bible 参考图"
                    f" · 候选 {artifact.variant_index}"
                )
                for asset, artifact in zip(selected_assets, self.artifacts, strict=True)
            )
        )
        self.assertEqual(
            ArtifactSelectionDecision.objects.filter(stage_run=self.review_stage).count(),
            2,
        )
        self.assertEqual(
            self.review_stage.output_snapshot["canonical"]["bindings"][0]["anchor_asset_id"],
            str(selected_assets[1].id),
        )
        _assert_frozen_formal_objects(self.run)
        self.assertEqual(_canonical_media(self.run), {"artifacts": []})

        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(
            f"/api/v1/projects/{self.project.id}/bible-image-candidates/",
        )
        self.assertEqual(response.status_code, 200, response.data)
        history = response.data["candidates"][0]["candidate_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual({item["binding_role"] for item in history}, {"anchor", "reference"})

    def test_review_can_bind_asset_library_image_without_selecting_model_candidate(self):
        before_assets = Asset.objects.count()
        public_sha = json_contract(
            redact_sensitive_bindings(self.generation_stage.output_snapshot),
        ).sha256
        with patch(
            "studio.services.production_runs.advance_production_run",
            side_effect=lambda run: run,
        ), patch(
            "studio.services.production_runs._assert_frozen_bible_run_closure",
        ):
            decide_stage(
                self.run,
                stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
                decision="approve",
                comment="use verified library image",
                user=self.admin,
                expected_source_snapshot_sha256=public_sha,
                bible_image_bindings=[{
                    "entity_type": "character",
                    "entity_id": self.character.id,
                    "selected_artifact_ids": [],
                    "selected_library_assets": [{
                        "asset_id": self.global_reference.id,
                        "expected_sha256": self.global_reference.checksum,
                    }],
                    "anchor_artifact_id": None,
                    "anchor_library_asset_id": self.global_reference.id,
                }],
            )

        self.character.refresh_from_db()
        self.review_stage.refresh_from_db()
        for artifact in self.artifacts:
            artifact.refresh_from_db()
        self.assertEqual(Asset.objects.count(), before_assets)
        self.assertEqual(self.character.anchor_asset_id, self.global_reference.id)
        self.assertEqual(
            set(self.character.reference_assets.values_list("id", flat=True)),
            {self.old_anchor.id},
        )
        self.assertTrue(all(
            artifact.status == GeneratedArtifact.Status.AVAILABLE
            for artifact in self.artifacts
        ))
        binding = self.review_stage.output_snapshot["canonical"]["bindings"][0]
        self.assertEqual(binding["selected_artifact_ids"], [])
        self.assertEqual(
            binding["selected_library_asset_ids"],
            [str(self.global_reference.id)],
        )
        self.assertEqual(binding["selected_library_assets"], [{
            "asset_id": str(self.global_reference.id),
            "expected_sha256": self.global_reference.checksum,
        }])
        self.assertEqual(
            binding["anchor_library_asset_id"],
            str(self.global_reference.id),
        )
        self.assertEqual(binding["anchor_asset_id"], str(self.global_reference.id))
        self.assertEqual(binding["selection_sources"], [{
            "source_type": "asset_library",
            "source_id": str(self.global_reference.id),
            "asset_id": str(self.global_reference.id),
        }])

    def test_review_can_mix_generated_candidate_and_asset_library_image(self):
        before_assets = Asset.objects.count()
        generated = self.artifacts[0]
        public_sha = json_contract(
            redact_sensitive_bindings(self.generation_stage.output_snapshot),
        ).sha256
        with patch(
            "studio.services.production_runs.advance_production_run",
            side_effect=lambda run: run,
        ), patch(
            "studio.services.production_runs._assert_frozen_bible_run_closure",
        ):
            decide_stage(
                self.run,
                stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
                decision="approve",
                comment="mix model candidate and verified library image",
                user=self.admin,
                expected_source_snapshot_sha256=public_sha,
                bible_image_bindings=[{
                    "entity_type": "character",
                    "entity_id": self.character.id,
                    "selected_artifact_ids": [generated.id],
                    "selected_library_assets": [{
                        "asset_id": self.global_reference.id,
                        "expected_sha256": self.global_reference.checksum,
                    }],
                    "anchor_artifact_id": generated.id,
                    "anchor_library_asset_id": None,
                }],
            )

        generated.refresh_from_db()
        self.artifacts[1].refresh_from_db()
        self.character.refresh_from_db()
        self.review_stage.refresh_from_db()
        self.assertEqual(Asset.objects.count(), before_assets + 1)
        self.assertEqual(generated.status, GeneratedArtifact.Status.SELECTED)
        self.assertEqual(
            self.artifacts[1].status,
            GeneratedArtifact.Status.AVAILABLE,
        )
        self.assertEqual(self.character.anchor_asset_id, generated.asset_id)
        self.assertEqual(
            set(self.character.reference_assets.values_list("id", flat=True)),
            {self.old_anchor.id, self.global_reference.id},
        )
        binding = self.review_stage.output_snapshot["canonical"]["bindings"][0]
        self.assertEqual(binding["selected_artifact_ids"], [str(generated.id)])
        self.assertEqual(binding["selected_library_assets"], [{
            "asset_id": str(self.global_reference.id),
            "expected_sha256": self.global_reference.checksum,
        }])
        self.assertEqual(binding["anchor_artifact_id"], str(generated.id))
        self.assertIsNone(binding["anchor_library_asset_id"])
        self.assertEqual(binding["selection_sources"], [
            {
                "source_type": "generated_artifact",
                "source_id": str(generated.id),
                "asset_id": str(generated.asset_id),
            },
            {
                "source_type": "asset_library",
                "source_id": str(self.global_reference.id),
                "asset_id": str(self.global_reference.id),
            },
        ])

    def test_library_asset_expected_checksum_drift_rejects_without_writes(self):
        before_assets = Asset.objects.count()
        before_reference_ids = set(
            self.character.reference_assets.values_list("id", flat=True),
        )
        public_sha = json_contract(
            redact_sensitive_bindings(self.generation_stage.output_snapshot),
        ).sha256
        with self.assertRaises(ProductionRunConflict) as raised:
            with patch(
                "studio.services.production_runs.advance_production_run",
                side_effect=lambda run: run,
            ), patch(
                "studio.services.production_runs._assert_frozen_bible_run_closure",
            ):
                decide_stage(
                    self.run,
                    stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
                    decision="approve",
                    comment="stale library checksum",
                    user=self.admin,
                    expected_source_snapshot_sha256=public_sha,
                    bible_image_bindings=[{
                        "entity_type": "character",
                        "entity_id": self.character.id,
                        "selected_artifact_ids": [],
                        "selected_library_assets": [{
                            "asset_id": self.global_reference.id,
                            "expected_sha256": "f" * 64,
                        }],
                        "anchor_artifact_id": None,
                        "anchor_library_asset_id": self.global_reference.id,
                    }],
                )

        self.assertEqual(
            raised.exception.code,
            "bible_library_asset_snapshot_stale",
        )
        self.character.refresh_from_db()
        self.review_stage.refresh_from_db()
        for artifact in self.artifacts:
            artifact.refresh_from_db()
        self.assertEqual(Asset.objects.count(), before_assets)
        self.assertEqual(self.character.anchor_asset_id, self.old_anchor.id)
        self.assertEqual(
            set(self.character.reference_assets.values_list("id", flat=True)),
            before_reference_ids,
        )
        self.assertEqual(
            self.review_stage.status,
            StageRun.Status.AWAITING_APPROVAL,
        )
        self.assertTrue(all(
            artifact.status == GeneratedArtifact.Status.AVAILABLE
            for artifact in self.artifacts
        ))

    def test_same_library_asset_cannot_bind_multiple_bible_entities(self):
        second = CharacterBible.objects.create(
            project=self.project,
            slug="older-sister",
            name="Older Sister",
            stable_speaker_id="S2",
            visual_identity="an older sister wearing a light coat",
        )
        compiled = compile_bible_image_prompt_input(
            project=self.project,
            entity_type="character",
            entity=second,
            model_profile_version=self.model_profile,
        )
        with transaction.atomic():
            second_prompt = materialize_compiled_prompt(
                compiled=compiled,
                created_by=self.admin,
            )
        second_candidate = self._candidate(
            variant_number=1,
            entity=second,
            prompt=second_prompt,
        )
        _save_stage_snapshot(
            self.generation_stage,
            "output_snapshot",
            _formal_snapshot(
                self.run,
                self.generation_stage,
                "generated-candidates",
            ),
        )
        public_sha = json_contract(
            redact_sensitive_bindings(self.generation_stage.output_snapshot),
        ).sha256
        repeated_selection = {
            "selected_artifact_ids": [],
            "selected_library_assets": [{
                "asset_id": self.global_reference.id,
                "expected_sha256": self.global_reference.checksum,
            }],
            "anchor_artifact_id": None,
            "anchor_library_asset_id": self.global_reference.id,
        }

        with self.assertRaisesMessage(
            ValidationError,
            "同一最终 Asset 不能绑定到多个 Bible 实体",
        ):
            with patch(
                "studio.services.production_runs.advance_production_run",
                side_effect=lambda run: run,
            ), patch(
                "studio.services.production_runs._assert_frozen_bible_run_closure",
            ):
                decide_stage(
                    self.run,
                    stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
                    decision="approve",
                    comment="reject cross-entity duplicate library image",
                    user=self.admin,
                    expected_source_snapshot_sha256=public_sha,
                    bible_image_bindings=[
                        {
                            "entity_type": "character",
                            "entity_id": self.character.id,
                            **repeated_selection,
                        },
                        {
                            "entity_type": "character",
                            "entity_id": second.id,
                            **repeated_selection,
                        },
                    ],
                )

        self.character.refresh_from_db()
        second.refresh_from_db()
        self.review_stage.refresh_from_db()
        second_candidate.refresh_from_db()
        self.assertEqual(self.character.anchor_asset_id, self.old_anchor.id)
        self.assertEqual(
            set(self.character.reference_assets.values_list("id", flat=True)),
            {self.global_reference.id},
        )
        self.assertIsNone(second.anchor_asset_id)
        self.assertEqual(second.reference_assets.count(), 0)
        self.assertEqual(
            self.review_stage.status,
            StageRun.Status.AWAITING_APPROVAL,
        )
        self.assertEqual(
            ArtifactSelectionDecision.objects.filter(
                stage_run=self.review_stage,
            ).count(),
            0,
        )
        self.assertEqual(
            second_candidate.status,
            GeneratedArtifact.Status.AVAILABLE,
        )

    def test_stale_generation_snapshot_hash_does_not_copy_assets(self):
        before = Asset.objects.count()
        with self.assertRaisesMessage(Exception, "已过期"):
            with patch(
                "studio.services.production_runs.advance_production_run",
                side_effect=lambda run: run,
            ), patch(
                "studio.services.production_runs._assert_frozen_bible_run_closure",
            ):
                decide_stage(
                    self.run,
                    stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
                    decision="approve",
                    comment="stale",
                    user=self.admin,
                    expected_source_snapshot_sha256="f" * 64,
                    bible_image_bindings=[{
                        "entity_type": "character",
                        "entity_id": self.character.id,
                        "selected_artifact_ids": [self.artifacts[0].id],
                        "anchor_artifact_id": self.artifacts[0].id,
                    }],
                )
        self.assertEqual(Asset.objects.count(), before)

    def test_comfy_candidate_without_exact_task_chain_is_rejected_before_copy(self):
        self.provider.driver_type = ProviderProfile.DriverType.COMFYUI
        self.provider.save(update_fields=("driver_type", "updated_at"))
        before_assets = Asset.objects.count()
        with self.assertRaisesMessage(Exception, "task/attempt/artifact"):
            self._approve()
        self.assertEqual(Asset.objects.count(), before_assets)
        self.assertEqual(
            ArtifactSelectionDecision.objects.filter(stage_run=self.review_stage).count(),
            0,
        )

    def test_real_comfy_candidate_archive_preview_and_approval_chain(self):
        artifact, task = self._comfy_candidate_from_signed_task()
        self.assertEqual(artifact.generation_task_id, task.id)
        self.assertEqual(
            task.generated_workflow.injected_values["execution_metadata"][
                "required_dimension_multiple"
            ],
            8,
        )
        self.assertEqual(task.capability_snapshot["resolution_multiple"], 8)
        self.assertEqual(
            artifact.manifest_metadata["request_sha256"],
            artifact.generation_attempt.request_sha256,
        )

        client = APIClient()
        client.force_authenticate(self.owner)
        response = client.get(
            f"/api/v1/projects/{self.project.id}/bible-image-candidates/",
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item
            for item in response.data["candidates"][0]["candidate_history"]
            if str(item["artifact_id"]) == str(artifact.id)
        )
        self.assertEqual(row["integrity_status"], "valid")
        self.assertTrue(row["source_current"])
        self.assertTrue(row["preview_url"])
        preview = client.get(row["preview_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(b"".join(preview.streaming_content), self.PNG_BYTES)

        self._approve_selection([artifact], anchor=artifact)
        artifact.refresh_from_db()
        self.assertIsNotNone(artifact.asset_id)
        self.assertEqual(artifact.asset.owner_id, self.project.owner_id)
        self.assertEqual(artifact.asset.checksum, hashlib.sha256(self.PNG_BYTES).hexdigest())

    def test_async_candidate_manifest_attempt_mismatch_is_rejected_before_copy(self):
        artifact = self.artifacts[0]
        assign_json_contract(artifact, "manifest_metadata", {
            **artifact.manifest_metadata,
            "generation_attempt_id": "11111111-1111-4111-8111-111111111111",
        })
        artifact.save(update_fields=(
            "manifest_metadata", "manifest_schema_version",
            "manifest_metadata_size_bytes", "manifest_metadata_sha256", "updated_at",
        ))
        before_assets = Asset.objects.count()
        with self.assertRaisesMessage(Exception, "execution identity"):
            self._approve()
        self.assertEqual(Asset.objects.count(), before_assets)
        self.assertEqual(
            ArtifactSelectionDecision.objects.filter(stage_run=self.review_stage).count(),
            0,
        )

    def test_candidate_with_incomplete_request_contract_is_invalid_in_history(self):
        attempt = self.artifacts[0].generation_attempt
        incomplete = json_contract({
            key: value
            for key, value in attempt.request_snapshot.items()
            if key != "prompt_content_hash"
        })
        attempt.request_snapshot = incomplete.value
        attempt.request_sha256 = incomplete.sha256
        attempt.request_snapshot_schema_version = incomplete.schema_version
        attempt.request_snapshot_size_bytes = incomplete.size_bytes
        attempt.request_snapshot_sha256 = incomplete.sha256
        attempt.save(update_fields=(
            "request_snapshot", "request_sha256", "request_snapshot_schema_version",
            "request_snapshot_size_bytes", "request_snapshot_sha256", "updated_at",
        ))
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(
            f"/api/v1/projects/{self.project.id}/bible-image-candidates/",
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item
            for item in response.data["candidates"][0]["candidate_history"]
            if str(item["artifact_id"]) == str(self.artifacts[0].id)
        )
        self.assertEqual(row["integrity_status"], "invalid")
        self.assertEqual(
            row["integrity_error_code"],
            "bible_image_candidate_provenance_invalid",
        )
        self.assertEqual(row["preview_url"], "")

    def test_project_owner_can_read_staff_run_attempt_and_artifact_but_other_tenant_cannot(self):
        other = get_user_model().objects.create_user(
            username="bible-other-tenant",
            password="StrongPass123!",
        )
        owner_client = APIClient()
        owner_client.force_authenticate(self.owner)
        self.assertEqual(
            owner_client.get(f"/api/v1/production-runs/{self.run.id}/").status_code,
            200,
        )
        attempt_response = owner_client.get(
            "/api/v1/generation-attempts/",
            {"stage_run": str(self.generation_stage.id)},
        )
        self.assertEqual(attempt_response.status_code, 200, attempt_response.data)
        self.assertEqual(attempt_response.data["count"], 2)
        self.assertEqual(
            owner_client.get(
                f"/api/v1/generated-artifacts/{self.artifacts[0].id}/",
            ).status_code,
            200,
        )

        other_client = APIClient()
        other_client.force_authenticate(other)
        self.assertEqual(
            other_client.get(f"/api/v1/production-runs/{self.run.id}/").status_code,
            404,
        )
        self.assertEqual(
            other_client.get(
                f"/api/v1/generated-artifacts/{self.artifacts[0].id}/",
            ).status_code,
            404,
        )

    def test_second_run_can_reapprove_materialized_and_unmaterialized_current_history(self):
        run_one = self.run
        artifact_a, artifact_c = self.artifacts
        self._approve_selection([artifact_a], anchor=artifact_a)
        artifact_a.refresh_from_db()
        artifact_c.refresh_from_db()
        asset_a = artifact_a.asset
        self.assertIsNotNone(asset_a)
        self.assertIsNone(artifact_c.asset_id)

        artifact_b = self._begin_second_run(idempotency_key="history-reapprove-run-two")
        generation_canonical = self.generation_stage.output_snapshot["canonical"]
        frozen_ids = {
            item["artifact_id"]
            for entity in generation_canonical["entities"]
            for item in entity["candidates"]
        }
        self.assertEqual(
            frozen_ids,
            {str(artifact_a.id), str(artifact_b.id), str(artifact_c.id)},
        )
        self._approve_selection(
            [artifact_a, artifact_b, artifact_c],
            anchor=artifact_b,
        )
        for artifact in (artifact_a, artifact_b, artifact_c):
            artifact.refresh_from_db()
            self.assertIsNotNone(artifact.asset_id)
        self.review_stage.refresh_from_db()
        self.assertEqual(artifact_a.asset_id, asset_a.id, "已物化历史版本必须复用 Asset")
        self.character.refresh_from_db()
        self.assertEqual(self.character.anchor_asset_id, artifact_b.asset_id)
        self.assertEqual(
            set(self.character.reference_assets.values_list("id", flat=True)),
            {artifact_a.asset_id, artifact_c.asset_id, self.old_anchor.id, self.global_reference.id},
        )
        selected_ids = set(
            self.review_stage.output_snapshot["canonical"]["bindings"][0]
            ["selected_asset_ids"]
        )
        self.assertEqual(
            selected_ids,
            {str(artifact_a.asset_id), str(artifact_b.asset_id), str(artifact_c.asset_id)},
        )
        self.assertEqual(
            ArtifactSelectionDecision.objects.filter(stage_run=self.review_stage).count(),
            3,
        )
        self.assertEqual(run_one.project_id, self.run.project_id)

    def test_second_run_unbinds_stale_formal_anchor_but_preserves_asset_history(self):
        artifact_a = self.artifacts[0]
        self._approve_selection([artifact_a], anchor=artifact_a)
        artifact_a.refresh_from_db()
        asset_a_id = artifact_a.asset_id
        self.project.style.visual_prompt = (
            self.project.style.visual_prompt + ", blue hour backlight"
        )
        self.project.style.save(update_fields=("visual_prompt", "updated_at"))

        artifact_b = self._begin_second_run(idempotency_key="stale-anchor-run-two")
        frozen_ids = {
            item["artifact_id"]
            for entity in self.generation_stage.output_snapshot["canonical"]["entities"]
            for item in entity["candidates"]
        }
        self.assertEqual(frozen_ids, {str(artifact_b.id)})
        self._approve_selection([artifact_b], anchor=artifact_b)

        artifact_a.refresh_from_db()
        artifact_b.refresh_from_db()
        self.character.refresh_from_db()
        self.review_stage.refresh_from_db()
        self.assertEqual(self.character.anchor_asset_id, artifact_b.asset_id)
        self.assertNotIn(
            asset_a_id,
            set(self.character.reference_assets.values_list("id", flat=True)),
        )
        self.assertTrue(Asset.objects.filter(pk=asset_a_id).exists())
        self.assertEqual(artifact_a.asset_id, asset_a_id)
        self.assertIn(
            str(asset_a_id),
            self.review_stage.output_snapshot["canonical"]["bindings"][0]
            ["removed_stale_asset_ids"],
        )
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(
            f"/api/v1/projects/{self.project.id}/bible-image-candidates/",
        )
        self.assertEqual(response.status_code, 200, response.data)
        history = response.data["candidates"][0]["candidate_history"]
        old = next(item for item in history if str(item["artifact_id"]) == str(artifact_a.id))
        self.assertEqual(old["binding_role"], "unbound")
        self.assertIsNotNone(old["materialized_asset"])
        self.assertFalse(old["source_current"])

    def test_ref2va_explicit_asset_overrides_historical_subject_picture_default(self):
        # Freeze the historic subject slot while A is still the preferred image.
        sync_consistency_locks(self.project)
        subject_lock = self.project.consistency_locks.get(
            scope="global",
            target_id=None,
            lock_key="reference_slots",
        )
        subject_key = f"character:{self.character.id}"
        self.assertEqual(
            subject_lock.locked_value["subject"][subject_key]["picture_asset_id"],
            str(self.old_anchor.id),
        )

        self._approve()
        self.character.refresh_from_db()
        for artifact in self.artifacts:
            artifact.refresh_from_db()
        asset_b = self.artifacts[1].asset  # the newly approved anchor
        asset_c = self.artifacts[0].asset  # another approved reference
        episode = Episode.objects.create(prompt_settings={"include_audio": True}, 
            project=self.project,
            number=1,
            title="Reference choice",
        )
        shot = Shot.objects.create(media_type="video", include_audio=True, 
            episode=episode,
            number=1,
            title="Exact reference",
            narrative="The younger brother turns toward the doorway.",
            detailed_description="A tense medium shot in a dim interior.",
            camera="A slow controlled push-in.",
            action="He turns toward the doorway.",
            duration_ms=3500,
            conditioning_mode="Ref2VA",
        )
        shot.characters.add(self.character)

        for selected in (asset_c, asset_b):
            bindings = {
                "characters": [{
                    "entity_id": str(self.character.id),
                    "asset_id": str(selected.id),
                }],
            }
            compiled = build_test_video_prompt(
                self.project,
                [shot],
                MediaPromptOptions(
                    model_identifier="minimax-h3-test",
                    mode="Ref2VA",
                    include_audio=False,
                    language_gate_mode="warn",
                    single_shot_reference_bindings=bindings,
                    single_shot_reference_assets=[selected],
                ),
            )
            self.assertEqual(
                compiled.payload["reference_bindings"][0]["asset_id"],
                str(selected.id),
            )
            self.assertEqual(
                compiled.payload["reference_bindings"][0]["checksum"],
                selected.checksum,
            )

    def test_storyboard_review_persists_run_scoped_ref2va_and_i2va_assets(self):
        self._approve()
        self.character.refresh_from_db()
        for artifact in self.artifacts:
            artifact.refresh_from_db()
        asset_b = self.artifacts[1].asset
        asset_c = self.artifacts[0].asset
        episode = Episode.objects.create(prompt_settings={"include_audio": True}, 
            project=self.project,
            number=1,
            title="Formal exact reference review",
        )
        ref_shot = Shot.objects.create(media_type="video", include_audio=True, 
            episode=episode,
            number=1,
            title="Ref2VA exact C",
            narrative="The younger brother turns.",
            camera="Slow push in.",
            action="He turns.",
            duration_ms=3500,
            conditioning_mode="Ref2VA",
        )
        ref_shot.characters.add(self.character)
        i2v_shot = Shot.objects.create(media_type="video", include_audio=True, 
            episode=episode,
            number=2,
            title="I2VA exact B",
            narrative="The first frame comes alive.",
            camera="Locked frame.",
            action="Subtle breathing.",
            duration_ms=3500,
            conditioning_mode="I2VA",
        )
        self.run.input_snapshot = {
            **self.run.input_snapshot,
            "episode_ids": [str(episode.id)],
        }
        assign_json_contract(self.run, "input_snapshot", self.run.input_snapshot)
        self.run.status = ProductionRun.Status.AWAITING_APPROVAL
        self.run.current_stage_key = "storyboard_review"
        self.run.save(update_fields=(
            "input_snapshot", "input_snapshot_schema_version", "input_snapshot_size_bytes",
            "input_snapshot_sha256", "status", "current_stage_key", "updated_at",
        ))
        storyboard_stage = self._stage(
            "storyboard", 4, StageRun.Status.SUCCEEDED, mode="deterministic",
        )
        _save_stage_snapshot(
            storyboard_stage,
            "output_snapshot",
            _formal_snapshot(self.run, storyboard_stage, "formal-storyboard"),
        )
        storyboard_review = self._stage(
            "storyboard_review",
            5,
            StageRun.Status.AWAITING_APPROVAL,
            mode="review",
            approval_required=True,
        )
        source_sha = json_contract(
            redact_sensitive_bindings(storyboard_stage.output_snapshot),
        ).sha256

        old_payload = [{
            "shot_id": ref_shot.id,
            "reference_bindings": {
                "characters": [{
                    "entity_id": self.character.id,
                    "asset_id": self.old_anchor.id,
                }],
            },
        }, {
            "shot_id": i2v_shot.id,
            "reference_bindings": {"first_frame_asset_id": self.old_anchor.id},
        }]
        with patch(
            "studio.services.production_runs._assert_frozen_bible_run_closure",
        ), patch(
            "studio.services.production_runs.advance_production_run",
            side_effect=lambda run: run,
        ):
            with self.assertRaisesMessage(Exception, "本次已批准"):
                decide_stage(
                    self.run,
                    stage_key="storyboard_review",
                    decision="approve",
                    comment="old A must be rejected",
                    user=self.admin,
                    expected_source_snapshot_sha256=source_sha,
                    shot_reference_bindings=old_payload,
                )
        self.assertFalse(
            storyboard_review.approval_decisions.exists(),
            "invalid run-scoped binding must write no ApprovalDecision",
        )
        ref_shot.refresh_from_db()
        self.assertEqual(ref_shot.reference_bindings, {})

        valid_payload = [{
            "shot_id": ref_shot.id,
            "reference_bindings": {
                "characters": [{
                    "entity_id": self.character.id,
                    "asset_id": asset_c.id,
                }],
            },
        }, {
            "shot_id": i2v_shot.id,
            "reference_bindings": {"first_frame_asset_id": asset_b.id},
        }]
        with patch(
            "studio.services.production_runs._assert_frozen_bible_run_closure",
        ), patch(
            "studio.services.production_runs.advance_production_run",
            side_effect=lambda run: run,
        ):
            decide_stage(
                self.run,
                stage_key="storyboard_review",
                decision="approve",
                comment="exact B and C",
                user=self.admin,
                expected_source_snapshot_sha256=source_sha,
                shot_reference_bindings=valid_payload,
            )
        ref_shot.refresh_from_db()
        i2v_shot.refresh_from_db()
        self.assertEqual(
            ref_shot.reference_bindings["characters"][0]["asset_id"],
            str(asset_c.id),
        )
        self.assertEqual(
            i2v_shot.reference_bindings["first_frame_asset_id"],
            str(asset_b.id),
        )
        compiled_rows = []
        for shot, mode, expected in (
            (ref_shot, "Ref2VA", asset_c),
            (i2v_shot, "I2VA", asset_b),
        ):
            compiled = build_test_video_prompt(
                self.project,
                [shot],
                MediaPromptOptions(
                    model_identifier="minimax-h3-test",
                    mode=mode,
                    include_audio=False,
                    language_gate_mode="warn",
                ),
            )
            self.assertEqual(
                compiled.payload["reference_bindings"][0]["asset_id"],
                str(expected.id),
            )
            self.assertEqual(
                compiled.payload["reference_bindings"][0]["checksum"],
                expected.checksum,
            )
            compiled_rows.append((shot, mode, expected, compiled))

        video_provider = ProviderProfile(
            key=f"exact-reference-video-provider-{uuid4()}",
            name="Exact reference video Comfy",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="video",
            operations="health,object_info,models,submit,poll,cancel,collect",
            is_active=True,
        )
        assign_json_contract(
            video_provider,
            "driver_config",
            {"base_url": "http://127.0.0.1:8188"},
        )
        video_provider.save()
        video_profile = ModelProfileVersion(
            profile_key=f"exact-reference-video-{uuid4()}",
            version=1,
            provider=video_provider,
            model_identifier="minimax-h3-test",
            modality=ModelProfileVersion.Modality.VIDEO,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        video_conditioning = {
            "schema_version": 1,
            "adapter_key": "media_conditioning_v1",
            "modes": [
                conditioning_contract(
                    "Ref2VA",
                    input_kind="classified_references",
                    requires_bible=True,
                    entity_images=(0, 9),
                    reference_videos=(0, 3),
                    paired_video_audios=(0, 3),
                    reference_audios=(0, 3),
                )["modes"][0],
                conditioning_contract(
                    "I2VA",
                    input_kind="frame_slots",
                    first_frame=(1, 1),
                )["modes"][0],
            ],
        }
        assign_json_contract(video_profile, "specification", {
            "schema_version": 1,
            "model_family": "minimax_h3",
            "capabilities": [
                "video.image_to_video", "video.reference_to_video",
            ],
            "conditioning_contract": video_conditioning,
        })
        video_profile.save()
        video_deployment = ModelDeployment(
            key=f"exact-reference-video-{uuid4()}",
            name="Exact reference video deployment",
            profile_version=video_profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(video_deployment, "runtime_config", {})
        video_deployment.save()
        video_version = self._generic_single_image_video_workflow(
            profile=video_profile,
            provider=video_provider,
            deployment=video_deployment,
            conditioning=video_conditioning,
        )
        media_stage = self.run.stages.get(stage_key="media_generation")
        media_stage.status = StageRun.Status.RUNNING
        media_stage.save(update_fields=("status", "updated_at"))
        staged_expectations = []
        frozen_conditioning = video_version.input_contract["conditioning_contract"]
        for index, (shot, mode, expected, compiled) in enumerate(
            compiled_rows,
            start=1,
        ):
            frozen_mode = conditioning_mode_contract(
                frozen_conditioning,
                mode,
            )
            prompt_payload = {
                **compiled.payload,
                "conditioning_contract": frozen_conditioning,
                "conditioning_contract_sha256": json_contract(
                    frozen_conditioning,
                ).sha256,
                "conditioning_mode_contract": frozen_mode,
                "conditioning_mode_contract_sha256": json_contract(
                    frozen_mode,
                ).sha256,
            }
            prompt = PromptArtifact.objects.create(
                project=self.project,
                episode=episode,
                shot=shot,
                created_by=self.admin,
                prompt_type=PromptArtifact.PromptType.VIDEO,
                model=video_profile.model_identifier,
                conditioning_mode=mode,
                revision=1,
                is_multi_shot=False,
                include_audio=False,
                total_duration_ms=compiled.total_duration_ms,
                prompt_text=compiled.text,
                negative_prompt="",
                structured_payload=prompt_payload,
                consistency_snapshot=compiled.consistency_snapshot,
                request_options={"shot_ids": [str(shot.id)]},
                content_hash=compiled.content_hash,
            )
            _, task = self._materialize_exact_video_task(
                prompt=prompt,
                version=video_version,
                deployment=video_deployment,
                stage=media_stage,
                attempt_number=index,
            )
            self.assertEqual(
                task.generated_workflow.injected_values["execution_metadata"][
                    "required_dimension_multiple"
                ],
                8,
            )
            self.assertEqual(task.capability_snapshot["resolution_multiple"], 8)
            self.assertIs(
                task.capability_snapshot["workflow_parameters"]["audio.enabled"],
                False,
            )
            self.assertIs(task.capability_snapshot["include_audio"], False)
            bindings = task.capability_snapshot["asset_bindings"]
            self.assertEqual(len(bindings), 1)
            self.assertEqual(bindings[0]["asset_id"], str(expected.id))
            self.assertEqual(bindings[0]["checksum"], expected.checksum)
            self.assertEqual(
                task.capability_snapshot["asset_bindings_sha256"],
                json_contract(bindings).sha256,
            )
            upload_client = Mock()
            uploaded = []

            def upload(path, *, subfolder, filename):
                uploaded.append({
                    "bytes": Path(path).read_bytes(),
                    "subfolder": subfolder,
                    "filename": filename,
                })
                return {"name": filename, "subfolder": subfolder}

            upload_client.upload_input_file.side_effect = upload
            prepared = prepare_workflow(task, upload_client)
            task.refresh_from_db()
            self.assertEqual(len(uploaded), 1)
            self.assertEqual(uploaded[0]["bytes"], self.PNG_BYTES)
            self.assertEqual(task.staged_inputs[0]["asset_id"], str(expected.id))
            self.assertEqual(task.staged_inputs[0]["checksum"], expected.checksum)
            self.assertEqual(
                prepared["1"]["inputs"]["image"],
                task.staged_inputs[0]["comfy_path"],
            )
            self.assertEqual(
                task.staged_inputs[0]["comfy_path"],
                f"{uploaded[0]['subfolder']}/{uploaded[0]['filename']}",
            )
            staged_expectations.append((mode, str(expected.id), expected.checksum))
        self.assertEqual(
            staged_expectations,
            [
                ("Ref2VA", str(asset_c.id), asset_c.checksum),
                ("I2VA", str(asset_b.id), asset_b.checksum),
            ],
        )

    def test_approved_asset_rejects_in_place_file_replacement_with_conflict(self):
        self._approve()
        self.artifacts[1].refresh_from_db()
        client = APIClient()
        client.force_authenticate(self.owner)
        response = client.patch(
            f"/api/v1/assets/{self.artifacts[1].asset_id}/",
            {
                "description": "replacement attempt",
                "prompt_hint": "replacement attempt",
                "file": SimpleUploadedFile(
                    "replacement.png",
                    self.PNG_BYTES,
                    content_type="image/png",
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("bible_reference_asset_immutable", str(response.data))

    def test_binding_byte_facts_and_prompt_source_are_revalidated_after_approval(self):
        self._approve()
        self.artifacts[1].refresh_from_db()
        anchor = self.artifacts[1].asset
        anchor.checksum = "0" * 64
        anchor.save(update_fields=("checksum", "updated_at"))
        with self.assertRaisesMessage(Exception, "checksum 已漂移"):
            _assert_frozen_formal_objects(self.run)

        anchor.checksum = hashlib.sha256(self.PNG_BYTES).hexdigest()
        anchor.save(update_fields=("checksum", "updated_at"))
        self.style.visual_prompt = "changed style after approval"
        self.style.save(update_fields=("visual_prompt", "updated_at"))
        with self.assertRaisesMessage(Exception, "语义来源已变化"):
            _assert_frozen_formal_objects(self.run)

    def test_materialized_candidate_history_remains_visible_after_unbinding(self):
        self._approve()
        self.artifacts[0].refresh_from_db()
        materialized_id = self.artifacts[0].asset_id
        self.character.refresh_from_db()
        self.character.reference_assets.remove(materialized_id)

        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(
            f"/api/v1/projects/{self.project.id}/bible-image-candidates/",
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item
            for item in response.data["candidates"][0]["candidate_history"]
            if str(item["artifact_id"]) == str(self.artifacts[0].id)
        )
        self.assertEqual(row["binding_role"], "unbound")
        self.assertEqual(str(row["materialized_asset"]["id"]), str(materialized_id))

        artifact = self.artifacts[0]
        artifact.refresh_from_db()
        asset_path = Path(artifact.asset.file.path)
        delete_response = client.delete(f"/api/v1/assets/{materialized_id}/")
        self.assertEqual(delete_response.status_code, 409, delete_response.data)
        self.assertTrue(Asset.objects.filter(pk=materialized_id).exists())
        self.assertTrue(GeneratedArtifact.objects.filter(pk=artifact.id).exists())
        self.assertTrue(asset_path.exists())

    def test_corrupt_candidate_target_is_explicit_in_history(self):
        artifact = self.artifacts[0]
        artifact.manifest_metadata = {
            **artifact.manifest_metadata,
            "target": {"schema_version": 1, "broken": True},
        }
        artifact.save(update_fields=("manifest_metadata", "updated_at"))
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(
            f"/api/v1/projects/{self.project.id}/bible-image-candidates/",
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item
            for item in response.data["candidates"][0]["candidate_history"]
            if str(item["artifact_id"]) == str(artifact.id)
        )
        self.assertEqual(row["integrity_status"], "invalid")
        self.assertEqual(
            row["integrity_error_code"],
            "bible_image_candidate_manifest_invalid",
        )
        self.assertFalse(row["source_current"])

    def test_missing_candidate_bytes_are_explicit_and_urls_are_withheld(self):
        artifact = self.artifacts[0]
        path = Path(self.media_directory.name) / artifact.object_key
        path.unlink()
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(
            f"/api/v1/projects/{self.project.id}/bible-image-candidates/",
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item
            for item in response.data["candidates"][0]["candidate_history"]
            if str(item["artifact_id"]) == str(artifact.id)
        )
        self.assertEqual(row["integrity_status"], "invalid")
        self.assertEqual(
            row["integrity_error_code"],
            "bible_image_candidate_media_integrity_invalid",
        )
        self.assertEqual(row["preview_url"], "")
        self.assertEqual(row["download_url"], "")

    def test_image_profile_publish_rejects_bible_sources_without_normalize_path(self):
        selectors = (
            PromptProfileFieldRule.SourceSelector.PROJECT_TITLE,
            PromptProfileFieldRule.SourceSelector.PROJECT_LOGLINE,
            PromptProfileFieldRule.SourceSelector.PROJECT_SCRIPT,
            PromptProfileFieldRule.SourceSelector.STYLE_NAME,
            PromptProfileFieldRule.SourceSelector.STYLE_DESCRIPTION,
        )
        for index, selector in enumerate(selectors, start=1):
            definition = PromptFieldDefinition.objects.create(
                key=f"unsupported_bible_source_{index}",
                label=selector,
                category="test",
                description="must be rejected before publication",
                impact="would create an unrecoverable Chinese Bible route",
                value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
            )
            profile = PromptProfileVersion.objects.create(
                profile_key=f"test.unsupported.bible.{index}",
                version=1,
                name=f"Unsupported Bible source {index}",
                model_family=model_family_for_profile(self.model_profile),
                operation="image.generate",
                review_state=PromptProfileVersion.ReviewState.READY,
            )
            PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector=selector,
                generation_source_selector=selector,
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=1,
                required=True,
                max_chars=4000,
            )
            with self.assertRaises(PromptProfileError) as caught:
                publish_prompt_profile(profile, actor=self.admin)
            self.assertEqual(
                caught.exception.code,
                "bible_image_selector_not_normalizable",
            )

    def test_bible_profile_rejects_required_sources_not_available_for_all_entities(self):
        for suffix, selector in (
            ("required-shot", PromptProfileFieldRule.SourceSelector.SHOT_NARRATIVE),
            ("required-lighting", PromptProfileFieldRule.SourceSelector.REQUEST_LIGHTING),
        ):
            with self.subTest(selector=selector):
                profile = self._published_image_profile_for_selector(
                    suffix=suffix,
                    selector=selector,
                    required=True,
                )
                with self.assertRaises(PromptProfileResolutionError) as caught:
                    validate_bible_image_prompt_profile(profile)
                self.assertEqual(
                    caught.exception.code,
                    "bible_image_prompt_profile_required_source_unavailable",
                )

    def test_bible_profile_rejects_optional_only_empty_positive_source(self):
        profile = self._published_image_profile_for_selector(
            suffix="optional-shot-only",
            selector=PromptProfileFieldRule.SourceSelector.SHOT_NARRATIVE,
            required=False,
        )

        with self.assertRaises(PromptProfileResolutionError) as caught:
            validate_bible_image_prompt_profile(profile)

        self.assertEqual(
            caught.exception.code,
            "bible_image_prompt_profile_positive_source_unavailable",
        )

        _artifact, task = self._comfy_candidate_from_signed_task()
        workflow_version = task.generated_workflow.workflow_template_version
        workflow_version.is_current = True
        workflow_version.save(update_fields=("is_current", "updated_at"))
        invalid_binding = PromptProfileBinding.objects.create(
            key="test-bible-optional-shot-only",
            name="Invalid Bible optional shot-only profile",
            operation="image.generate",
            model_family=model_family_for_profile(self.model_profile),
            mode="",
            model_profile_version=self.model_profile,
            prompt_profile_version=profile,
            priority=100,
            is_active=True,
        )
        self.assertTrue(invalid_binding.is_active)
        routing = _freeze_stage_routing(
                {
                    "mode": "fixed",
                    "routes": [{
                        "workflow_version_id": str(workflow_version.id),
                        "model_deployment_id": str(self.deployment.id),
                    }],
                    "variant_count": 1,
                    "evaluator": None,
                },
                stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
            )
        with self.assertRaises(ProductionRunCreateRejected) as route_error:
            _freeze_bible_prompt_profile_selection(
                stages=[{
                    "key": BIBLE_IMAGE_GENERATION_STAGE_KEY,
                    "routing": routing,
                }],
                binding_id=invalid_binding.id,
            )
        self.assertEqual(
            route_error.exception.code,
            "bible_image_prompt_profile_positive_source_unavailable",
        )

    def test_bible_route_freezes_exact_profile_selection_across_binding_priority_changes(self):
        _artifact, task = self._comfy_candidate_from_signed_task()
        workflow_version = task.generated_workflow.workflow_template_version
        workflow_version.is_current = True
        workflow_version.save(update_fields=("is_current", "updated_at"))
        routing = _freeze_stage_routing(
            {
                "mode": "fixed",
                "routes": [{
                    "workflow_version_id": str(workflow_version.id),
                    "model_deployment_id": str(self.deployment.id),
                }],
                "variant_count": 1,
                "evaluator": None,
            },
            stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
        )
        original_binding = PromptProfileBinding.objects.get(key="test-bible-qwen-image")
        frozen = _freeze_bible_prompt_profile_selection(
            stages=[{
                "key": BIBLE_IMAGE_GENERATION_STAGE_KEY,
                "routing": routing,
            }],
            binding_id=original_binding.id,
        )
        selection = routing["routes"][0]["prompt_profile"]
        self.assertEqual(frozen["prompt_profile_binding_id"], str(original_binding.id))
        self.assertEqual(selection["schema_version"], 2)
        self.assertEqual(
            selection["prompt_profile_binding_id"], str(original_binding.id),
        )
        self.assertEqual(
            selection["prompt_profile_version_id"],
            str(original_binding.prompt_profile_version_id),
        )
        self.assertEqual(
            selection["model_profile_version_id"], str(self.model_profile.id),
        )
        self.assertEqual(selection["operation"], "image.generate")
        self.assertEqual(selection["mode"], "")

        replacement = create_test_prompt_profile_binding(
            actor=self.admin,
            model_profile_version=self.model_profile,
            operation="image.generate",
        )
        replacement.priority = 100
        replacement.save(update_fields=("priority", "updated_at"))

        live_compiled = compile_bible_image_prompt_input(
            project=self.project,
            entity_type="character",
            entity=self.character,
            model_profile_version=self.model_profile,
        )
        frozen_compiled = compile_bible_image_prompt_input(
            project=self.project,
            entity_type="character",
            entity=self.character,
            model_profile_version=self.model_profile,
            frozen_prompt_profile=selection,
        )
        self.assertEqual(live_compiled.prompt_profile_version.id, replacement.prompt_profile_version_id)
        self.assertEqual(
            frozen_compiled.prompt_profile_version.id,
            original_binding.prompt_profile_version_id,
        )
        self.assertEqual(
            frozen_compiled.request_options["prompt_profile_binding_id"],
            str(original_binding.id),
        )

    def test_static_reference_replay_uses_same_character_scene_and_prop_target_kind(self):
        profile = PromptProfileVersion.objects.create(
            profile_key="test.bible.static-replay",
            version=1,
            name="Static Bible replay",
            model_family=model_family_for_profile(self.model_profile),
            operation="image.generate",
            mode="",
            source_projection_key="direct_v1",
            source_projection_config={
                "schema_version": 2,
                "static_reference_policy": {
                    "snapshot_selector": "initial",
                    "default_target_kind": "character",
                    "allow_context": {
                        "default": False,
                        "target_overrides": {"scene": True},
                    },
                    "context_source_selectors": ["request.style"],
                    "transition_markers": [" and changes once to "],
                    "transition_patterns": [{
                        "kind": "from_to",
                        "from_marker": " changes from ",
                        "to_marker": " to ",
                    }],
                    "context_clause_policy": {
                        "delimiters": [",", ";"],
                        "discard_markers": [" held in "],
                    },
                    "negative_token_exclusions": {},
                    "forbidden_markers": {
                        "always": [" both states "],
                        "when_context_disallowed": [" held in "],
                    },
                    "violation_action": "block",
                    "initial_snapshot_clause_policy": {
                        "delimiters": [",", ";"],
                        "initial_state_markers": ["initially", "at first"],
                        "continuity_source_selector": "request.action",
                        "trailing_clause_action": "relocate_to_continuity",
                    },
                },
            },
            review_state=PromptProfileVersion.ReviewState.READY,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        subject = PromptFieldDefinition.objects.create(
            key="test_static_replay_subject",
            label="Static subject",
            category="test",
            description="Single static entity identity",
            impact="Controls Bible reference identity",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        style = PromptFieldDefinition.objects.create(
            key="test_static_replay_style",
            label="Static style",
            category="test",
            description="Scene-only context style",
            impact="Controls scene context",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        for position, (definition, selector) in enumerate((
            (subject, PromptProfileFieldRule.SourceSelector.REQUEST_SUBJECT),
            (style, PromptProfileFieldRule.SourceSelector.REQUEST_STYLE),
        ), start=1):
            PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector=selector,
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=position,
                label_style=PromptProfileFieldRule.LabelStyle.NONE,
                required=position == 1,
                max_chars=4000,
                generation_mode=PromptProfileFieldRule.GenerationMode.PASSTHROUGH,
                generation_source_selector=selector,
                generation_output_semantics=(
                    PromptProfileFieldRule.GenerationOutputSemantics.FINAL
                ),
                language_policy="inherit",
                length_unit=PromptProfileFieldRule.LengthUnit.NONE,
            )
        profile = publish_prompt_profile(profile, actor=self.admin)
        binding = PromptProfileBinding.objects.create(
            key="test-bible-static-replay",
            name="Static Bible replay",
            operation="image.generate",
            model_family=model_family_for_profile(self.model_profile),
            mode="",
            model_profile_version=self.model_profile,
            prompt_profile_version=profile,
            priority=500,
            is_active=True,
        )
        frozen = frozen_profile_reference(
            ResolvedPromptProfile(binding=binding, profile_version=profile),
            model_profile_version=self.model_profile,
            operation="image.generate",
            mode="",
        )
        scene = SceneBible.objects.create(
            project=self.project,
            slug="seed-vault",
            name="Seed Vault",
            visual_identity="underground seed vault with damp metal cabinets",
            location="underground vault",
            lighting="cold blue stable light",
        )
        prop = PropBible.objects.create(
            project=self.project,
            slug="access-card",
            name="Access Card",
            visual_identity=(
                "worn black access card, held in Zhou Lan's right hand; "
                "one red numeral begins as 03 and changes once to 02"
            ),
        )
        rows = (
            ("character", self.character),
            ("scene", scene),
            ("prop", prop),
        )
        prompts = {}
        for entity_type, entity in rows:
            compiled = compile_bible_image_prompt_input(
                project=self.project,
                entity_type=entity_type,
                entity=entity,
                model_profile_version=self.model_profile,
                frozen_prompt_profile=frozen,
            )
            with transaction.atomic():
                prompt = materialize_compiled_prompt(
                    compiled=compiled,
                    created_by=self.admin,
                )
            prompts[entity_type] = prompt
            replay = _assert_bible_prompt_source_current(
                prompt=prompt,
                entity_type=entity_type,
                entity=entity,
                project=self.project,
            )
            self.assertEqual(replay["entity_type"], entity_type)

        self.assertNotIn(
            self.style.visual_prompt,
            prompts["character"].prompt_text,
        )
        self.assertIn(self.style.visual_prompt, prompts["scene"].prompt_text)
        self.assertNotIn(self.style.visual_prompt, prompts["prop"].prompt_text)
        self.assertNotIn("held in", prompts["prop"].prompt_text)
        self.assertIn("begins as 03", prompts["prop"].prompt_text)
        self.assertNotIn("02", prompts["prop"].prompt_text)

        dynamic_prop = PropBible.objects.create(
            project=self.project,
            slug="single-state-signal",
            name="Signal Object",
            visual_identity=(
                "palm-sized cyan-green object, initially dark, "
                "pulses once with glow"
            ),
        )
        dynamic_compiled = compile_bible_image_prompt_input(
            project=self.project,
            entity_type="prop",
            entity=dynamic_prop,
            model_profile_version=self.model_profile,
            frozen_prompt_profile=frozen,
        )
        with transaction.atomic():
            dynamic_prompt = materialize_compiled_prompt(
                compiled=dynamic_compiled,
                created_by=self.admin,
            )
        replay = _assert_bible_prompt_source_current(
            prompt=dynamic_prompt,
            entity_type="prop",
            entity=dynamic_prop,
            project=self.project,
        )
        self.assertEqual(replay["entity_type"], "prop")
        self.assertIn("initially dark", dynamic_prompt.prompt_text)
        self.assertNotIn("pulses once", dynamic_prompt.prompt_text)
        source_snapshot = dynamic_prompt.request_options["source_snapshot"]
        self.assertIn(
            "pulses once with glow",
            source_snapshot["sources"]["request.subject"],
        )
        projection = source_snapshot["prompt_profile"]["assembly"][
            "source_projection"
        ]
        self.assertEqual(
            projection["initial_clause_projections"]["request.subject"][
                "continuity_source_selector"
            ],
            "request.action",
        )
        self.assertEqual(
            projection["initial_clause_projections"]["request.subject"][
                "continuity_facts"
            ],
            ["pulses once with glow"],
        )
        self.assertEqual(
            projection["continuity_facts"],
            {"request.action": "pulses once with glow"},
        )

    def test_create_api_freezes_explicit_bible_binding_end_to_end_and_replays_it(self):
        _artifact, task = self._comfy_candidate_from_signed_task()
        workflow_version = task.generated_workflow.workflow_template_version
        workflow_version.is_current = True
        workflow_version.save(update_fields=("is_current", "updated_at"))
        Episode.objects.create(prompt_settings={"include_audio": True}, project=self.project, number=1, title="第一集")

        selected = PromptProfileBinding.objects.get(key="test-bible-qwen-image")
        selected.priority = -100
        selected.save(update_fields=("priority", "updated_at"))
        preferred = create_test_prompt_profile_binding(
            actor=self.admin,
            model_profile_version=self.model_profile,
            operation="image.generate",
        )
        preferred.priority = 100
        preferred.save(update_fields=("priority", "updated_at"))

        recipe = ProductionRecipeVersion(
            recipe_key="bible-explicit-selection-api",
            version=1,
            name="Bible explicit selection API",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [{
                "key": BIBLE_IMAGE_GENERATION_STAGE_KEY,
                "mode": "existing_pipeline",
                "approval": "auto",
                "routing": {
                    "mode": "fixed",
                    "routes": [{
                        "workflow_version_id": str(workflow_version.id),
                        "model_deployment_id": str(self.deployment.id),
                    }],
                    "variant_count": 1,
                    "evaluator": None,
                },
            }],
        })
        recipe.save()
        client = APIClient()
        client.force_authenticate(self.owner)
        payload = {
            "project_id": str(self.project.id),
            "recipe_version_id": str(recipe.id),
            "idempotency_key": "bible-explicit-binding-api",
            "bible_image_prompt_profile_binding_id": str(selected.id),
        }
        # This focused fixture intentionally contains only the Bible generation
        # stage. The production closure itself is covered separately; here the
        # API boundary and every frozen/runtime PromptProfile reference stay real.
        with patch("studio.services.production_runs._validate_delivery_recipe_closure"):
            created = client.post("/api/v1/production-runs/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        frozen = run.input_snapshot["bible_image_prompt_profile"]
        self.assertEqual(frozen["prompt_profile_binding_id"], str(selected.id))
        stage = run.stages.get(stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY)
        self.assertEqual(
            stage.routing["routes"][0]["prompt_profile"]["prompt_profile_binding_id"],
            str(selected.id),
        )
        public_stage = next(
            item for item in created.data["stages"]
            if item["stage_key"] == BIBLE_IMAGE_GENERATION_STAGE_KEY
        )
        self.assertEqual(
            public_stage["routing"]["routes"][0]["prompt_profile"]
            ["prompt_profile_binding_id"],
            str(selected.id),
        )
        self.assertEqual(
            stage.input_snapshot["bible_image_prompt_profile"]
            ["prompt_profile_binding_id"],
            str(selected.id),
        )
        prompt = PromptArtifact.objects.get(
            request_options__production_run_id=str(run.id),
        )
        self.assertEqual(prompt.prompt_profile_binding_key, selected.key)
        self.assertEqual(
            prompt.request_options["prompt_profile_selection"]
            ["prompt_profile_binding_id"],
            str(selected.id),
        )
        attempt = GenerationAttempt.objects.get(stage_run=stage)
        self.assertEqual(attempt.prompt_profile_binding_key, selected.key)
        self.assertEqual(
            attempt.request_snapshot["prompt_profile_selection"]
            ["prompt_profile_binding_id"],
            str(selected.id),
        )

        selected.is_active = False
        selected.save(update_fields=("is_active", "updated_at"))
        preferred.priority = 1000
        preferred.save(update_fields=("priority", "updated_at"))
        replay = client.post("/api/v1/production-runs/", payload, format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], created.data["id"])

        changed = dict(payload)
        changed["bible_image_prompt_profile_binding_id"] = str(preferred.id)
        conflict = client.post("/api/v1/production-runs/", changed, format="json")
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertEqual(conflict.data["error"]["code"], "production_run_conflict")

    def test_legacy_bible_stage_without_v2_selection_fails_closed(self):
        with self.assertRaises(StageBlocked) as raised:
            _freeze_stage_input(self.run, self.generation_stage)
        self.assertEqual(
            raised.exception.code,
            "prompt_profile_selection_snapshot_missing",
        )

    def test_project_delete_preserves_async_and_comfy_bible_history_with_explicit_conflict(self):
        client = APIClient()
        client.force_authenticate(self.owner)
        async_artifact = self.artifacts[0]
        async_path = Path(self.media_directory.name) / async_artifact.object_key

        response = client.delete(f"/api/v1/projects/{self.project.id}/")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "protected_resource")
        self.assertTrue(SeriesProject.objects.filter(pk=self.project.id).exists())
        self.assertTrue(ProductionRun.objects.filter(pk=self.run.id).exists())
        self.assertTrue(GeneratedArtifact.objects.filter(pk=async_artifact.id).exists())
        self.assertTrue(async_path.is_file())

        comfy_artifact, task = self._comfy_candidate_from_signed_task()
        comfy_path = Path(self.media_directory.name) / comfy_artifact.object_key
        response = client.delete(f"/api/v1/projects/{self.project.id}/")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "protected_resource")
        self.assertTrue(ProductionRun.objects.filter(pk=self.run.id).exists())
        self.assertTrue(GenerationTask.objects.filter(pk=task.id).exists())
        self.assertTrue(GeneratedArtifact.objects.filter(pk=comfy_artifact.id).exists())
        self.assertTrue(comfy_path.is_file())
