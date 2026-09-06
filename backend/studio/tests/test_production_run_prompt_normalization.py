from __future__ import annotations
from studio.tests.deployment_smoke_fixtures import run_test_deployment_smoke, create_test_capacity_profile, test_normalization_response

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    CharacterBible,
    Episode,
    GenerationAttempt,
    GenerationAttemptResolution,
    ModelDeployment,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    PromptFieldTranslation,
    ProviderProfile,
    SceneBible,
    SeriesProject,
    Shot,
    StageRun,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.prompt_normalization import PROMPT_NORMALIZE_OPERATION
from studio.services.prompt_normalization_contract import (
    _protected_token_sentinel,
    project_prompt_normalization_request_contract,
    prompt_normalization_chat_payload,
    trusted_prompt_normalization_generation_tasks,
)
from studio.services.prompt_profiles import ResolvedPromptProfile, frozen_profile_reference
from studio.services.production_runs import (
    StageBlocked,
    _assert_frozen_formal_objects,
    _blank_prompt_normalization_candidates,
    _build_prompt_normalization_batch_plan,
    _build_prompt_normalization_input,
    _prompt_field_retry_policy,
    _reviewed_profile_fields_by_shot,
    _stage_snapshot_max_bytes,
)
from studio.services.prompt_translation import (
    build_translation_context,
    confirm_prompt_normalization_candidates,
    prepare_prompt_normalization_item,
    translation_context_hash,
)
from studio.services.provider_registry import OpenAICompatibleDriver
from studio.services.provider_registry import ProviderDriverError
from studio.services.script_sources import confirm_script_revision, create_pasted_script_source
from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
from studio.tests.secret_fixtures import create_test_secret, enable_test_keyring
from studio.tests.factories import conditioning_contract, create_test_prompt_profile_binding


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("normalize.example",))
class ProductionRunPromptNormalizationTests(TestCase):
    def setUp(self):
        enable_test_keyring(self)
        self.user = get_user_model().objects.create_user(
            username="prompt-normalization-run-owner",
            password="StrongPass123!",
        )
        self.admin = get_user_model().objects.create_superuser(
            username="prompt-normalization-run-admin",
            password="StrongPass123!",
            email="prompt-normalization-run-admin@example.test",
        )
        self.credential = create_test_secret(
            key="production.run.prompt-normalize",
            actor=self.user,
            value="test-secret",
        )
        self.project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16",
            owner=self.user,
            title="提示词规范化审批闭环",
            width=768, height=1344, fps=24, default_shot_duration_ms=5000,
            continuity_seed=1,
        )
        _, revision = create_pasted_script_source(
            project=self.project,
            user=self.user,
            content="# 第一集\n\n侦探进入旧宅。",
        )
        self.revision = confirm_script_revision(revision, user=self.user)
        episode = Episode.objects.create(prompt_settings={"include_audio": True},
            project=self.project,
            number=1,
            title="旧宅",
            script="侦探进入旧宅。",
        )
        self.shot = Shot.objects.create(media_type="video", include_audio=True,
            episode=episode,
            number=1,
            narrative="侦探缓慢走进昏暗的旧宅。",
            duration_ms=3000,
        )
        self.deployment = self._deployment()
        create_test_capacity_profile(self.admin)
        self._pass_smoke()
        self.recipe = self._recipe()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _deployment(self) -> ModelDeployment:
        provider = ProviderProfile(
            key="run-prompt-normalize-provider",
            name="Run prompt normalize provider",
            driver_type=ProviderProfile.DriverType.OPENAI_COMPATIBLE,
            modalities="text",
            operations=PROMPT_NORMALIZE_OPERATION,
            credential=self.credential,
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "https://normalize.example",
            "chat_path": "/v1/chat/completions",
            "timeout_seconds": 10,
            "capability_ttl_seconds": 3_600,
            "pricing": {"currency": "USD", "fixed_per_request": "0.001"},
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key="run-prompt-normalize-profile",
            version=1,
            provider=provider,
            model_identifier="strict-prompt-normalizer",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "model_family": "strict_prompt_normalizer",
            "capabilities": [PROMPT_NORMALIZE_OPERATION],
            "prompt_normalization": {
                "max_input_bytes": 29_000,
                "max_output_tokens": 8_192,
                "max_items_per_batch": 4,
                "timeout_seconds": 180.0,
                "chat_request_options": {
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
            },
        })
        profile.save()
        deployment = ModelDeployment(
            key="run-prompt-normalize-deployment",
            name="Run prompt normalize deployment",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        create_test_prompt_profile_binding(
            actor=self.admin,
            model_profile_version=profile,
            operation=PROMPT_NORMALIZE_OPERATION,
        )
        return deployment

    def _pass_smoke(self):
        with patch.object(OpenAICompatibleDriver, "invoke", side_effect=lambda *args, **kwargs: test_normalization_response(args[-1])):
            snapshot = run_test_deployment_smoke(self.deployment, operation="prompt.normalize")
        self.assertEqual(snapshot.status, snapshot.Status.PASSED, snapshot.evidence)
        return snapshot

    def _recipe(self) -> ProductionRecipeVersion:
        recipe = ProductionRecipeVersion(
            recipe_key="prompt-normalization-review",
            version=1,
            name="Prompt normalization review",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [
                {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
                {"key": "storyboard", "mode": "manual", "approval": "required"},
                {"key": "storyboard_review", "mode": "review", "approval": "required"},
                {
                    "key": "script_normalize",
                    "mode": "ai",
                    "approval": "auto",
                    "routing": {
                        "mode": "fixed",
                        "variant_count": 1,
                        "evaluator": None,
                        "routes": [{"model_deployment_id": str(self.deployment.id)}],
                    },
                },
                {
                    "key": "script_normalize_review",
                    "mode": "review",
                    "approval": "required",
                },
            ],
        })
        recipe.save()
        return recipe

    def _manual_recipe(self) -> ProductionRecipeVersion:
        definition = deepcopy(self.recipe.definition)
        normalize = next(
            item
            for item in definition["stages"]
            if item["key"] == "script_normalize"
        )
        normalize["mode"] = "manual"
        normalize.pop("routing", None)
        recipe = ProductionRecipeVersion(
            recipe_key="prompt-normalization-manual-review",
            version=1,
            name="Prompt normalization manual review",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", definition)
        recipe.save()
        return recipe

    def test_run_creation_fails_closed_without_prompt_normalize_profile_binding(self):
        self.deployment.profile_version.prompt_profile_bindings.update(is_active=False)

        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "script_revision_id": str(self.revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "missing-normalize-profile",
            "allow_external_calls": True,
            "budget_limit": "1.0000",
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        # Removing the exact strategy first invalidates its Smoke evidence;
        # admission must stop here, before creating a run or selecting a fallback.
        self.assertIn("prompt_normalize_smoke_required", str(response.data))
        self.assertFalse(ProductionRun.objects.filter(
            idempotency_key="missing-normalize-profile",
        ).exists())

    def test_manual_recipe_uses_audited_human_review_without_provider_smoke(self):
        self.deployment.capability_snapshots.update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        manual_recipe = self._manual_recipe()
        created = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "script_revision_id": str(self.revision.id),
            "recipe_version_id": str(manual_recipe.id),
            "idempotency_key": "prompt-normalization-human-only",
            "allow_external_calls": True,
            "budget_limit": "1.0000",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])

        for stage_key in ("storyboard", "storyboard_review"):
            approval = {
                "stage_key": stage_key,
                "decision": "approve",
                "comment": "human-only path reviewed",
            }
            if stage_key == "storyboard_review":
                detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
                approval["expected_source_snapshot_sha256"] = next(
                    item["output_snapshot_sha256"]
                    for item in detail.data["stages"]
                    if item["stage_key"] == "storyboard"
                )
            response = self.client.post(
                f"/api/v1/production-runs/{run.id}/approve/",
                approval,
                format="json",
            )
            self.assertEqual(response.status_code, 200, response.data)

        run.refresh_from_db()
        normalize_stage = run.stages.get(stage_key="script_normalize")
        review_payload = normalize_stage.output_snapshot["review_payload"]
        self.assertEqual(run.current_stage_key, "script_normalize_review")
        self.assertEqual(normalize_stage.generation_attempts.count(), 0)
        self.assertEqual(
            review_payload["review_origin"],
            "human_only_recipe",
        )
        self.assertEqual(
            review_payload["submission_semantics"],
            "human_only_no_submission",
        )
        self.assertTrue(review_payload["candidates"])

        detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
        public_normalize_hash = next(
            item["output_snapshot_sha256"]
            for item in detail.data["stages"]
            if item["stage_key"] == "script_normalize"
        )
        approved = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "comment": "all fields written and reviewed by a human",
            "expected_source_snapshot_sha256": public_normalize_hash,
            "candidate_overrides": {
                candidate["request_id"]: "A detective slowly enters the dim old mansion."
                for candidate in review_payload["candidates"]
            },
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        entries = list(PromptFieldTranslation.objects.all())
        self.assertTrue(entries)
        self.assertTrue(all(item.origin_generation_attempt_id is None for item in entries))
        self.assertTrue(all(
            item.runtime_metadata["review_origin"]
            == "human_only_recipe"
            for item in entries
        ))

    def test_external_enabled_still_requires_fresh_prompt_normalize_smoke(self):
        self.deployment.capability_snapshots.update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        created = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "script_revision_id": str(self.revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "prompt-normalization-stale-smoke-external",
            "allow_external_calls": True,
            "budget_limit": "1.0000",
        }, format="json")

        self.assertEqual(created.status_code, 400, created.data)
        self.assertIn("fresh-smoke", str(created.data))
        self.assertFalse(ProductionRun.objects.filter(
            idempotency_key="prompt-normalization-stale-smoke-external",
        ).exists())

    def _create_and_reach_normalization(
        self,
        *,
        configure_profile: bool = False,
    ) -> tuple[ProductionRun, GenerationAttempt]:
        created = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "script_revision_id": str(self.revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "prompt-normalization-run",
            "allow_external_calls": True,
            "budget_limit": "1.0000",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        if configure_profile:
            self._configure_generic_profile_v2(run)
        for stage_key in ("storyboard", "storyboard_review"):
            payload = {
                "stage_key": stage_key,
                "decision": "approve",
                "comment": "formal input reviewed",
            }
            if stage_key == "storyboard_review":
                detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
                self.assertEqual(detail.status_code, 200, detail.data)
                payload["expected_source_snapshot_sha256"] = next(
                    item["output_snapshot_sha256"]
                    for item in detail.data["stages"]
                    if item["stage_key"] == "storyboard"
                )
            approved = self.client.post(
                f"/api/v1/production-runs/{run.id}/approve/",
                payload,
                format="json",
            )
            self.assertEqual(approved.status_code, 200, approved.data)
        attempt = claim_next_text_attempt("prompt-normalization-test-worker")
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.operation, PROMPT_NORMALIZE_OPERATION)
        return run, attempt

    def _bulk_prompt_normalization_input(self, count: int) -> dict:
        item, target = prepare_prompt_normalization_item(
            project=self.project,
            scope_type=PromptFieldTranslation.ScopeType.SHOT,
            scope_id=self.shot.id,
            field_key="shot.narrative",
            source_text=self.shot.narrative,
            label="Bulk shot narrative",
            request_id="bulk-000",
        )
        items = []
        targets = []
        for index in range(count):
            request_id = f"bulk-{index:03d}"
            cloned_item = deepcopy(item)
            cloned_item["request_id"] = request_id
            cloned_target = deepcopy(target)
            cloned_target["request_id"] = request_id
            items.append(cloned_item)
            targets.append(cloned_target)
        return {
            "payload": {"schema_version": 1, "items": items},
            "targets": targets,
        }

    def test_complete_run_over_100_items_is_deterministically_split_by_model_batch_limit(self):
        item_count = 105
        max_items_per_batch = int(
            self.deployment.profile_version.specification[
                "prompt_normalization"
            ]["max_items_per_batch"],
        )
        run = ProductionRun(
            project=self.project,
            generation_task_count=0,
            max_generation_tasks=100,
        )
        binding = self.deployment.profile_version.prompt_profile_bindings.get(
            is_active=True,
        )

        plan = _build_prompt_normalization_batch_plan(
            run,
            frozen_input=self._bulk_prompt_normalization_input(item_count),
            policy=self.deployment.profile_version.specification[
                "prompt_normalization"
            ],
            prompt_profile_version=binding.prompt_profile_version,
        )

        self.assertEqual(len(plan["original_request_ids"]), item_count)
        self.assertEqual(len(plan["batches"]), 27)
        self.assertTrue(all(
            1 <= len(batch["request_ids"]) <= max_items_per_batch
            for batch in plan["batches"]
        ))
        self.assertEqual(
            [
                request_id
                for batch in plan["batches"]
                for request_id in batch["request_ids"]
            ],
            plan["original_request_ids"],
        )
        self.assertLessEqual(
            plan["full_frozen_input_size_bytes"],
            8 * 1024 * 1024,
        )

    def test_too_many_batches_uses_existing_run_task_budget_not_100_item_block(self):
        run = ProductionRun(
            project=self.project,
            generation_task_count=0,
            max_generation_tasks=26,
        )
        binding = self.deployment.profile_version.prompt_profile_bindings.get(
            is_active=True,
        )

        with self.assertRaises(StageBlocked) as blocked:
            _build_prompt_normalization_batch_plan(
                run,
                frozen_input=self._bulk_prompt_normalization_input(105),
                policy=self.deployment.profile_version.specification[
                    "prompt_normalization"
                ],
                prompt_profile_version=binding.prompt_profile_version,
            )

        self.assertEqual(
            blocked.exception.code,
            "generation_task_budget_exhausted",
        )
        self.assertIn("27", str(blocked.exception))
        self.assertNotIn("100 项", str(blocked.exception))

    def test_expired_frozen_smoke_can_continue_with_fresh_same_target_evidence(self):
        created = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "script_revision_id": str(self.revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "prompt-normalization-smoke-revalidation",
            "allow_external_calls": True,
            "budget_limit": "1.0000",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        normalize_stage = run.stages.get(stage_key="script_normalize")
        original_routing_sha256 = normalize_stage.routing_sha256
        original_smoke = normalize_stage.routing["routes"][0]["smoke_evidence"][
            PROMPT_NORMALIZE_OPERATION
        ]
        self.deployment.capability_snapshots.filter(
            pk=original_smoke["snapshot_id"],
        ).update(expires_at=timezone.now() - timedelta(seconds=1))

        for stage_key in ("storyboard", "storyboard_review"):
            payload = {
                "stage_key": stage_key,
                "decision": "approve",
                "comment": "formal input reviewed",
            }
            if stage_key == "storyboard_review":
                detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
                payload["expected_source_snapshot_sha256"] = next(
                    item["output_snapshot_sha256"]
                    for item in detail.data["stages"]
                    if item["stage_key"] == "storyboard"
                )
            approved = self.client.post(
                f"/api/v1/production-runs/{run.id}/approve/",
                payload,
                format="json",
            )
            self.assertEqual(approved.status_code, 200, approved.data)

        run.refresh_from_db()
        normalize_stage.refresh_from_db()
        self.assertEqual(run.blocked_code, "prompt_normalize_smoke_required")
        self.assertEqual(normalize_stage.generation_attempts.count(), 0)

        fresh = self._pass_smoke()
        retried = self.client.post(
            f"/api/v1/production-runs/{run.id}/continue/",
            {},
            format="json",
        )
        self.assertEqual(retried.status_code, 200, retried.data)
        normalize_stage.refresh_from_db()
        attempt = normalize_stage.generation_attempts.get()
        effective_smoke = attempt.request_snapshot["route"]["smoke_evidence"][
            PROMPT_NORMALIZE_OPERATION
        ]
        self.assertEqual(effective_smoke["snapshot_id"], str(fresh.id))
        self.assertEqual(effective_smoke["target_sha256"], original_smoke["target_sha256"])
        self.assertEqual(
            effective_smoke["revalidated_from_snapshot_id"],
            original_smoke["snapshot_id"],
        )
        self.assertEqual(normalize_stage.routing_sha256, original_routing_sha256)
        self.assertEqual(
            normalize_stage.routing["routes"][0]["smoke_evidence"][
                PROMPT_NORMALIZE_OPERATION
            ]["snapshot_id"],
            original_smoke["snapshot_id"],
        )
        audit = normalize_stage.routing_decision["smoke_revalidations"][-1]
        self.assertEqual(audit["current_smoke"]["snapshot_id"], str(fresh.id))
        self.assertEqual(
            normalize_stage.route_events.filter(
                event_type="smoke_revalidated",
                code=audit["revalidation_sha256"],
            ).count(),
            1,
        )

    def test_fresh_smoke_with_changed_target_cannot_revalidate_frozen_route(self):
        created = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "script_revision_id": str(self.revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "prompt-normalization-smoke-target-drift",
            "allow_external_calls": True,
            "budget_limit": "1.0000",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        normalize_stage = run.stages.get(stage_key="script_normalize")
        frozen_smoke = normalize_stage.routing["routes"][0]["smoke_evidence"][
            PROMPT_NORMALIZE_OPERATION
        ]
        self.deployment.capability_snapshots.filter(
            pk=frozen_smoke["snapshot_id"],
        ).update(expires_at=timezone.now() - timedelta(seconds=1))

        for stage_key in ("storyboard", "storyboard_review"):
            payload = {
                "stage_key": stage_key,
                "decision": "approve",
                "comment": "formal input reviewed",
            }
            if stage_key == "storyboard_review":
                detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
                payload["expected_source_snapshot_sha256"] = next(
                    item["output_snapshot_sha256"]
                    for item in detail.data["stages"]
                    if item["stage_key"] == "storyboard"
                )
            approved = self.client.post(
                f"/api/v1/production-runs/{run.id}/approve/",
                payload,
                format="json",
            )
            self.assertEqual(approved.status_code, 200, approved.data)

        assign_json_contract(self.deployment, "runtime_config", {"timeout_seconds": 11})
        self.deployment.save(update_fields=(
            "runtime_config",
            "runtime_config_schema_version",
            "runtime_config_size_bytes",
            "runtime_config_sha256",
            "updated_at",
        ))
        fresh = self._pass_smoke()
        self.assertNotEqual(fresh.target_sha256, frozen_smoke["target_sha256"])

        retried = self.client.post(
            f"/api/v1/production-runs/{run.id}/retry/",
            {},
            format="json",
        )
        self.assertEqual(retried.status_code, 200, retried.data)
        run.refresh_from_db()
        normalize_stage.refresh_from_db()
        self.assertEqual(run.status, ProductionRun.Status.BLOCKED)
        self.assertEqual(run.blocked_code, "prompt_normalize_smoke_drift")
        self.assertEqual(normalize_stage.generation_attempts.count(), 0)
        self.assertFalse(normalize_stage.route_events.filter(
            event_type="smoke_revalidated",
        ).exists())

    def test_explicit_retry_can_rebuild_invalid_input_before_provider_submission(self):
        run, attempt = self._create_and_reach_normalization()
        normalize_stage = run.stages.get(stage_key="script_normalize")
        attempt.delete()
        normalize_stage.status = StageRun.Status.BLOCKED
        normalize_stage.reason_code = "prompt_normalize_input_invalid"
        normalize_stage.reason = "deterministic input requires repaired schema"
        normalize_stage.save(update_fields=(
            "status", "reason_code", "reason", "updated_at",
        ))
        run.status = ProductionRun.Status.BLOCKED
        run.current_stage_key = "script_normalize"
        run.blocked_code = "prompt_normalize_input_invalid"
        run.blocked_reason = normalize_stage.reason
        run.save(update_fields=(
            "status", "current_stage_key", "blocked_code", "blocked_reason", "updated_at",
        ))

        retried = self.client.post(
            f"/api/v1/production-runs/{run.id}/retry/",
            {},
            format="json",
        )

        self.assertEqual(retried.status_code, 200, retried.data)
        normalize_stage.refresh_from_db()
        self.assertEqual(normalize_stage.status, StageRun.Status.RUNNING)
        self.assertEqual(normalize_stage.generation_attempts.count(), 1)

    def _configure_generic_profile_v2(self, run: ProductionRun) -> None:
        character = CharacterBible.objects.create(
            project=self.project,
            slug="lin-mo",
            name="林默",
            stable_speaker_id="S1",
        )
        self.shot.conditioning_mode = "Ref2VA"
        self.shot.detailed_description = "依据剧本、Bible、对白与镜头时间线扩写林默的完整镜头描述。"
        self.shot.sound_effects = "雨声、脚步、衣料和怀表机械声与画面同步。"
        self.shot.music = "低音弦乐只在对白间隙出现，并在切镜前淡出。"
        self.shot.dialogue = [{
            "speaker_id": "S1",
            "text": "别碰那封信。",
            "delivery": "压低声音",
            "voiceover": False,
            "start_ms": 800,
            "end_ms": 1800,
        }]
        self.shot.save(update_fields=(
            "conditioning_mode",
            "detailed_description",
            "sound_effects",
            "music",
            "dialogue",
            "updated_at",
        ))
        self.shot.characters.add(character)
        media_binding = create_test_prompt_profile_binding(
            actor=self.admin,
            model_profile_version=self.deployment.profile_version,
            operation="video.generate",
            mode="Ref2VA",
        )
        frozen_profile = frozen_profile_reference(
            ResolvedPromptProfile(
                binding=media_binding,
                profile_version=media_binding.prompt_profile_version,
            ),
            model_profile_version=self.deployment.profile_version,
            operation="video.generate",
            mode="Ref2VA",
        )
        profile_selection = {
            "schema_version": 2,
            "mode": "uniform",
            "prompt_profile_selections": [{
                "operation": "video.generate",
                "mode": "Ref2VA",
                "prompt_profile": frozen_profile,
            }],
            "shot_routes": [],
        }
        model_profile = self.deployment.profile_version
        frozen_conditioning_contract = conditioning_contract(
            "Ref2VA",
            input_kind="classified_references",
        )
        route = {
            "index": 0,
            "model_deployment_id": str(self.deployment.id),
            "model_profile_version_id": str(model_profile.id),
            "model_identifier": model_profile.model_identifier,
            "model_specification_sha256": model_profile.specification_sha256,
            "model_specification": deepcopy(model_profile.specification),
            "frozen_model_specification_sha256": json_contract(
                model_profile.specification,
            ).sha256,
            "provider_id": str(model_profile.provider_id),
            "driver_type": model_profile.provider.driver_type,
            "conditioning_contract": frozen_conditioning_contract,
            "conditioning_contract_sha256": json_contract(
                frozen_conditioning_contract,
            ).sha256,
        }
        routing = {
            "schema_version": 1,
            "mode": "fixed",
            "variant_count": 1,
            "evaluator": None,
            "routes": [route],
            "prompt_profile_selection": profile_selection,
        }
        media_stage = StageRun(
            production_run=run,
            stage_key="media_generation",
            ordinal=10,
            mode="existing_pipeline",
            idempotency_key=f"{run.id}:media_generation",
            selected_route_index=0,
        )
        assign_json_contract(media_stage, "routing", routing)
        assign_json_contract(media_stage, "routing_decision", {
            "schema_version": 1,
            "state": "selected",
            "selected_route_index": 0,
        })
        media_stage.save()
        workflow_selection = {
            "schema_version": 2,
            "mode": "uniform",
            "selected_episode_ids": [str(self.shot.episode_id)],
            "route": {
                "route_index": 0,
                "conditioning_contract": frozen_conditioning_contract,
                "conditioning_contract_sha256": json_contract(
                    frozen_conditioning_contract,
                ).sha256,
            },
            "shot_routes": [],
            "prompt_profile_selections": [{
                "operation": "video.generate",
                "mode": "Ref2VA",
                "prompt_profile": frozen_profile,
            }],
            "media_stage_routing_sha256": json_contract(routing).sha256,
        }
        run_input = deepcopy(run.input_snapshot)
        run_input["episode_ids"] = [str(self.shot.episode_id)]
        run_input["workflow_selection"] = workflow_selection
        run_input["workflow_selection_sha256"] = json_contract(
            workflow_selection,
        ).sha256
        assign_json_contract(run, "input_snapshot", run_input)
        run.save(update_fields=(
            "input_snapshot",
            "input_snapshot_schema_version",
            "input_snapshot_size_bytes",
            "input_snapshot_sha256",
            "updated_at",
        ))

    def _create_and_approve_manual_profile_v2(
        self,
    ) -> tuple[ProductionRun, dict]:
        manual_recipe = self._manual_recipe()
        created = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "script_revision_id": str(self.revision.id),
            "recipe_version_id": str(manual_recipe.id),
            "idempotency_key": "prompt-normalization-manual-profile-v2",
            "allow_external_calls": False,
            "budget_limit": "1.0000",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        self._configure_generic_profile_v2(run)
        for stage_key in ("storyboard", "storyboard_review"):
            approval = {
                "stage_key": stage_key,
                "decision": "approve",
                "comment": "manual Profile v2 input reviewed",
            }
            if stage_key == "storyboard_review":
                detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
                approval["expected_source_snapshot_sha256"] = next(
                    item["output_snapshot_sha256"]
                    for item in detail.data["stages"]
                    if item["stage_key"] == "storyboard"
                )
            response = self.client.post(
                f"/api/v1/production-runs/{run.id}/approve/",
                approval,
                format="json",
            )
            self.assertEqual(response.status_code, 200, response.data)

        normalize_stage = run.stages.get(stage_key="script_normalize")
        review_payload = normalize_stage.output_snapshot["review_payload"]
        profile_candidates = {
            candidate["profile_field_key"]: candidate
            for candidate in review_payload["candidates"]
            if candidate.get("target_kind") == "prompt_profile_field"
        }
        self.assertEqual(set(profile_candidates), {
            "detailed_description",
            "overall_soundscape",
            "non_diegetic_music",
        })
        detailed = " ".join(["Lin", "Mo", *("carefully" for _ in range(138))])
        profile_values = {
            "detailed_description": detailed,
            "overall_soundscape": "Rain footsteps cloth and watch mechanisms remain synchronized.",
            "non_diegetic_music": "Low strings fade before the cut.",
        }
        overrides = {}
        for candidate in review_payload["candidates"]:
            if candidate.get("target_kind") == "prompt_profile_field":
                value = profile_values[candidate["profile_field_key"]]
            elif str(candidate.get("field_key") or "").endswith(".name"):
                value = "Lin Mo"
            else:
                value = "Lin Mo carefully enters the dim old mansion under cold blue light."
            overrides[candidate["request_id"]] = value
        detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
        public_normalize_hash = next(
            item["output_snapshot_sha256"]
            for item in detail.data["stages"]
            if item["stage_key"] == "script_normalize"
        )
        approved = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "comment": "manual Profile v2 fields reviewed",
            "expected_source_snapshot_sha256": public_normalize_hash,
            "candidate_overrides": overrides,
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        run.refresh_from_db()
        return run, review_payload

    def test_run_freezes_generic_profile_v2_fields_context_and_generation_rules(self):
        run, _attempt = self._create_and_reach_normalization()
        self._configure_generic_profile_v2(run)

        frozen_input = _build_prompt_normalization_input(run)
        contract = frozen_input["payload"]["target_prompt_contract"]
        representative_request_id = contract["item_generation_rules"][0][
            "request_id"
        ]
        provider_request = project_prompt_normalization_request_contract(
            frozen_input["payload"],
            request_ids=[representative_request_id],
        )
        provider_payload = prompt_normalization_chat_payload(
            provider_request,
            policy=self.deployment.profile_version.specification[
                "prompt_normalization"
            ],
        )
        blank_candidates = _blank_prompt_normalization_candidates(
            frozen_input["targets"],
            invalid_message="test fixture invalid",
        )
        blank_profile_candidates = [
            candidate for candidate in blank_candidates
            if candidate.get("target_kind") == "prompt_profile_field"
        ]

        self.assertEqual(contract["workflow_selection_sha256"], run.input_snapshot[
            "workflow_selection_sha256"
        ])
        profile_rules = contract["profiles"][0]["contract"]["rules"]
        self.assertEqual(
            [rule["generation_mode"] for rule in profile_rules],
            ["server_locked"] * 3 + ["model_generated"] * 3,
        )
        self.assertEqual(
            {rule["field_value_type"] for rule in profile_rules},
            {"long_text"},
        )
        self.assertEqual(
            {
                rule["generation_source_selector"]
                for rule in contract["item_generation_rules"]
            },
            {
                "shot.detailed_description",
                "shot.sound_effects",
                "shot.music",
            },
        )
        self.assertEqual(len(blank_profile_candidates), 3)
        self.assertTrue(all(
            candidate.get("profile_selection_sha256")
            and candidate.get("prompt_profile_version_id")
            and candidate.get("profile_field_key")
            and candidate.get("generation_source_selector")
            for candidate in blank_profile_candidates
        ))
        self.assertEqual(len(contract["frozen_context"]["shots"]), 1)
        self.assertEqual(
            contract["frozen_context"]["shots"][0]["dialogue"][0]["text"],
            "别碰那封信。",
        )
        self.assertEqual(
            contract["frozen_context_sha256"],
            json_contract(contract["frozen_context"]).sha256,
        )
        provider_user_payload = json.loads(
            provider_payload["messages"][1]["content"],
        )
        trusted_tasks = json.loads(
            provider_payload["messages"][0]["content"]
            .split("TRUSTED_GENERATION_TASKS_JSON_BEGIN\n", 1)[1]
            .split("\nTRUSTED_GENERATION_TASKS_JSON_END", 1)[0]
        )
        self.assertNotIn("target_prompt_contract", provider_user_payload)
        provider_json = json.dumps(provider_user_payload, ensure_ascii=False)
        for token in provider_request["items"][0]["protected_tokens"]:
            self.assertNotIn(token, provider_json)
        self.assertEqual(
            trusted_tasks["tasks"][0]["source_data_refs"][1]["sha256"],
            json_contract(provider_user_payload["generation_contexts"][0]).sha256,
        )

    def test_explicit_manual_profile_v2_review_materializes_profile_field_values(self):
        run, _review_payload = self._create_and_approve_manual_profile_v2()

        review_stage = run.stages.get(stage_key="script_normalize_review")
        rows = review_stage.output_snapshot["canonical"]["profile_field_values"]

        self.assertEqual(
            {row["profile_field_key"] for row in rows},
            {
                "detailed_description",
                "overall_soundscape",
                "non_diegetic_music",
            },
        )
        self.assertTrue(all(row["request_id"].startswith("profile-") for row in rows))
        self.assertEqual(
            {row["shot_id"] for row in rows},
            {str(self.shot.id)},
        )

    def test_manual_snapshot_fails_closed_without_profile_identity_fields(self):
        run, _review_payload = self._create_and_approve_manual_profile_v2()
        normalize_stage = run.stages.get(stage_key="script_normalize")
        normalize_snapshot = deepcopy(normalize_stage.output_snapshot)
        payload = normalize_snapshot["review_payload"]
        profile_metadata_keys = {
            "target_kind",
            "shot_id",
            "profile_selection_sha256",
            "prompt_profile_version_id",
            "profile_field_key",
            "profile_source_selector",
            "generation_mode",
            "generation_output_semantics",
            "generation_source_selector",
            "generation_source_scope_type",
            "generation_source_scope_id",
            "generation_source_field_key",
        }
        for candidate in payload["candidates"]:
            for key in profile_metadata_keys:
                candidate.pop(key, None)
        payload["result_sha256"] = json_contract({
            "schema_version": 1,
            "review_origin": payload["review_origin"],
            "frozen_input_sha256": payload["frozen_input_sha256"],
            "candidates": payload["candidates"],
        }).sha256
        assign_json_contract(
            normalize_stage,
            "output_snapshot",
            normalize_snapshot,
            max_bytes=8 * 1024 * 1024,
        )
        normalize_stage.save(update_fields=(
            "output_snapshot",
            "output_snapshot_schema_version",
            "output_snapshot_size_bytes",
            "output_snapshot_sha256",
            "updated_at",
        ))
        review_stage = run.stages.get(stage_key="script_normalize_review")
        review_snapshot = deepcopy(review_stage.output_snapshot)
        review_snapshot["canonical"]["profile_field_values"] = []
        review_snapshot["canonical_sha256"] = json_contract(
            review_snapshot["canonical"],
        ).sha256
        assign_json_contract(
            review_stage,
            "output_snapshot",
            review_snapshot,
            max_bytes=8 * 1024 * 1024,
        )
        review_stage.save(update_fields=(
            "output_snapshot",
            "output_snapshot_schema_version",
            "output_snapshot_size_bytes",
            "output_snapshot_sha256",
            "updated_at",
        ))

        self.assertEqual(_reviewed_profile_fields_by_shot(run), {})
        with self.assertRaises(ValidationError):
            _assert_frozen_formal_objects(run)

    @staticmethod
    def _runtime_result(attempt: GenerationAttempt) -> dict:
        targets = {
            str(target.get("request_id") or ""): target
            for target in attempt.request_snapshot.get("targets") or []
            if isinstance(target, dict)
        }
        target_contract = attempt.request_snapshot["payload"].get(
            "target_prompt_contract",
        ) or {}
        rules = {
            str(rule.get("request_id") or ""): rule
            for rule in target_contract.get("item_generation_rules") or []
            if isinstance(rule, dict)
        }

        def output(item):
            request_id = str(item.get("request_id") or "")
            target = targets.get(request_id, {})
            rule = rules.get(request_id, {})
            profile_field_key = str(target.get("profile_field_key") or "")
            protected = " ".join(
                _protected_token_sentinel(token)
                for token in (item.get("protected_tokens") or [])
                for _ in range(str(item.get("text") or "").count(token))
            )
            if profile_field_key == "detailed_description":
                minimum = int(rule.get("generation_min") or 140)
                word_count = minimum + 5
                value = " ".join(
                    ["Detective", "Lin", "Mo"]
                    + ["carefully"] * max(0, word_count - 3)
                )
            elif profile_field_key == "overall_soundscape":
                value = "Rain footsteps cloth and watch mechanisms remain synchronized."
            elif profile_field_key == "non_diegetic_music":
                value = "Low suspense strings fade completely before the cut."
            else:
                value = "A detective slowly enters the dim old mansion."
            return f"{protected} {value}".strip()

        return {
            "schema_version": 1,
            "request_id": "provider-normalization-id",
            "items": [
                {
                    "request_id": item["request_id"],
                    "translation": output(item),
                }
                for item in attempt.request_snapshot["payload"]["items"]
            ],
            "metadata": {},
        }

    def test_soft_token_warning_reaches_review_without_retry_or_failure(self):
        profile = self.deployment.profile_version
        specification = deepcopy(profile.specification)
        specification["prompt_normalization"] = {
            **specification["prompt_normalization"],
            "max_input_bytes": 65_536,
            "input_byte_warning_threshold": 1,
            "output_token_warning_threshold": 10,
        }
        assign_json_contract(profile, "specification", specification)
        profile.save(update_fields=(
            "specification",
            "specification_schema_version",
            "specification_size_bytes",
            "specification_sha256",
            "updated_at",
        ))
        self._pass_smoke()
        run, attempt = self._create_and_reach_normalization()

        while attempt is not None:
            result = self._runtime_result(attempt)
            result["metadata"] = {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 11,
                    "total_tokens": 111,
                },
                "provider_warning": "must not persist",
            }
            with patch.object(
                OpenAICompatibleDriver,
                "invoke",
                return_value=result,
            ):
                processed = process_text_attempt(attempt)
            self.assertEqual(
                processed.status,
                GenerationAttempt.Status.SUCCEEDED,
                processed.error_message,
            )
            attempt = claim_next_text_attempt("soft-token-warning-tail")

        run.refresh_from_db()
        normalize_stage = run.stages.get(stage_key="script_normalize")
        review_payload = normalize_stage.output_snapshot["review_payload"]
        self.assertEqual(run.status, ProductionRun.Status.AWAITING_APPROVAL)
        self.assertTrue(review_payload["advisories"])
        token_warnings = [
            item
            for item in review_payload["advisories"]
            if item["code"] == "recommended_output_tokens_exceeded"
        ]
        self.assertTrue(token_warnings)
        self.assertTrue(all(item["measured"] == 11 for item in token_warnings))
        input_warnings = [
            item
            for item in review_payload["advisories"]
            if item["code"] == "recommended_input_bytes_exceeded"
        ]
        self.assertTrue(input_warnings)
        self.assertTrue(all(item["measured"] > 1 for item in input_warnings))
        self.assertTrue(all(item["recommended_maximum"] == 1 for item in input_warnings))
        self.assertTrue(all(item["hard_maximum"] == 65_536 for item in input_warnings))
        self.assertFalse(
            normalize_stage.generation_attempts.filter(
                status=GenerationAttempt.Status.FAILED,
            ).exists(),
        )
        self.assertNotIn(
            "provider_warning",
            json.dumps(review_payload, ensure_ascii=False),
        )

    def test_nonfirst_failed_batch_retries_only_that_batch(self):
        run, first = self._create_and_reach_normalization(
            configure_profile=True,
        )
        batch_plan_sha256 = first.request_snapshot["batch_plan"][
            "batch_plan_sha256"
        ]
        self.assertGreater(len(first.request_snapshot["batch_plan"]["batches"]), 2)
        failed = self._reach_model_generated_attempt(
            first,
            worker_prefix="prompt-normalization-nonfirst-safe",
        )
        failed_batch_index = failed.request_snapshot["batch_index"]
        self.assertGreater(failed_batch_index, 0)
        self._process_safe_length_rejection(failed)
        retry = claim_next_text_attempt("prompt-normalization-batch-2-retry-worker")
        self.assertIsNotNone(retry)
        self.assertEqual(retry.request_snapshot["batch_index"], failed_batch_index)
        self.assertEqual(
            retry.request_snapshot["batch_plan"]["batch_plan_sha256"],
            batch_plan_sha256,
        )
        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            return_value=self._runtime_result(retry),
        ):
            process_text_attempt(retry)

        current = claim_next_text_attempt("prompt-normalization-batch-tail-worker")
        while current is not None:
            with patch.object(
                OpenAICompatibleDriver,
                "invoke",
                return_value=self._runtime_result(current),
            ):
                process_text_attempt(current)
            current.refresh_from_db()
            self.assertEqual(
                current.status,
                GenerationAttempt.Status.SUCCEEDED,
                json.dumps({
                    "error": current.error_message,
                    "batch_index": current.request_snapshot.get("batch_index"),
                    "targets": current.request_snapshot.get("targets"),
                }, ensure_ascii=False, default=str),
            )
            current = claim_next_text_attempt(
                f"prompt-normalization-batch-tail-{current.request_snapshot['batch_index'] + 1}",
            )
        attempts = list(
            GenerationAttempt.objects.filter(
                stage_run=first.stage_run,
            ).order_by("created_at", "id")
        )
        batch_count = len(first.request_snapshot["batch_plan"]["batches"])
        self.assertEqual(
            [item.request_snapshot["batch_index"] for item in attempts],
            [
                *range(failed_batch_index),
                failed_batch_index,
                failed_batch_index,
                *range(failed_batch_index + 1, batch_count),
            ],
        )
        normalize_snapshot = run.stages.get(
            stage_key="script_normalize",
        ).output_snapshot
        review_payload = normalize_snapshot["review_payload"]
        original_request_ids = first.request_snapshot["batch_plan"][
            "original_request_ids"
        ]
        self.assertEqual(
            [row["request_id"] for row in review_payload["candidates"]],
            original_request_ids,
        )
        successful_attempt_by_request_id = {
            request_id: str(successful.id)
            for successful in attempts
            if successful.status == GenerationAttempt.Status.SUCCEEDED
            for request_id in successful.request_snapshot["batch_plan"]["batches"][
                successful.request_snapshot["batch_index"]
            ]["request_ids"]
        }
        self.assertEqual(
            {
                row["request_id"]: row["generation_attempt_id"]
                for row in review_payload["candidates"]
            },
            successful_attempt_by_request_id,
        )

    def test_nonfirst_uncertain_batch_resolution_retry_preserves_progress(self):
        run, first = self._create_and_reach_normalization(
            configure_profile=True,
        )
        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            return_value=self._runtime_result(first),
        ):
            process_text_attempt(first)
        first.refresh_from_db()
        self.assertEqual(
            first.status,
            GenerationAttempt.Status.SUCCEEDED,
            first.error_message,
        )
        second = claim_next_text_attempt("prompt-normalization-uncertain-batch-2")
        self.assertIsNotNone(second)
        self.assertEqual(second.request_snapshot["batch_index"], 1)
        frozen_plan_sha256 = second.request_snapshot["batch_plan"][
            "batch_plan_sha256"
        ]
        failure = ProviderDriverError(
            "connection lost after POST",
            code="provider_request_failed",
            submission_uncertain=True,
        )
        with patch.object(OpenAICompatibleDriver, "invoke", side_effect=failure):
            process_text_attempt(second)

        admin_client = APIClient()
        admin_client.force_authenticate(self.admin)
        resolved = admin_client.post(
            f"/api/v1/production-runs/{run.id}/resolve-uncertain/",
            {
                "attempt_id": str(second.id),
                "outcome": "confirmed_not_submitted",
                "note": "Provider audit confirms the second batch was absent.",
            },
            format="json",
        )
        self.assertEqual(resolved.status_code, 200, resolved.data)
        retried = self.client.post(
            f"/api/v1/production-runs/{run.id}/retry/",
            {},
            format="json",
        )
        self.assertEqual(retried.status_code, 200, retried.data)
        retry = GenerationAttempt.objects.filter(
            stage_run=first.stage_run,
            status=GenerationAttempt.Status.QUEUED,
        ).get()
        self.assertEqual(retry.request_snapshot["batch_index"], 1)
        self.assertEqual(
            retry.request_snapshot["batch_plan"]["batch_plan_sha256"],
            frozen_plan_sha256,
        )
        progress = run.stages.get(
            stage_key="script_normalize",
        ).output_snapshot
        self.assertEqual(
            [row["batch_index"] for row in progress["completed_batches"]],
            [0],
        )

    def test_single_item_over_capacity_blocks_before_attempt_or_provider_post(self):
        created = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.id),
            "script_revision_id": str(self.revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "prompt-normalization-single-item-over-capacity",
            "allow_external_calls": True,
            "budget_limit": "1.0000",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        self._configure_generic_profile_v2(run)
        self.shot.detailed_description = "雨" * 20_000
        self.shot.save(update_fields=("detailed_description", "updated_at"))

        with patch.object(OpenAICompatibleDriver, "invoke") as invoke:
            for stage_key in ("storyboard", "storyboard_review"):
                approval = {
                    "stage_key": stage_key,
                    "decision": "approve",
                    "comment": "capacity preflight fixture",
                }
                if stage_key == "storyboard_review":
                    detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
                    approval["expected_source_snapshot_sha256"] = next(
                        item["output_snapshot_sha256"]
                        for item in detail.data["stages"]
                        if item["stage_key"] == "storyboard"
                    )
                response = self.client.post(
                    f"/api/v1/production-runs/{run.id}/approve/",
                    approval,
                    format="json",
                )
                self.assertEqual(response.status_code, 200, response.data)

        run.refresh_from_db()
        normalize_stage = run.stages.get(stage_key="script_normalize")
        self.assertEqual(run.status, ProductionRun.Status.BLOCKED)
        self.assertEqual(
            run.blocked_code,
            "prompt_normalization_item_exceeds_capacity",
        )
        self.assertEqual(normalize_stage.generation_attempts.count(), 0)
        invoke.assert_not_called()

    @staticmethod
    def _set_frozen_retry_limit(attempt: GenerationAttempt, retry_limit: int) -> None:
        stage = attempt.stage_run
        snapshot = deepcopy(stage.config_snapshot)
        snapshot["base"]["values"]["production.prompt_field_retry_limit"] = retry_limit
        snapshot["routes"][str(attempt.route_index)]["values"][
            "production.prompt_field_retry_limit"
        ] = retry_limit
        assign_json_contract(stage, "config_snapshot", snapshot, max_bytes=_stage_snapshot_max_bytes())
        stage.save(update_fields=(
            "config_snapshot",
            "config_snapshot_schema_version",
            "config_snapshot_size_bytes",
            "config_snapshot_sha256",
            "updated_at",
        ))

    @staticmethod
    def _process_invalid_response(
        attempt: GenerationAttempt,
        *,
        classifier: str = "response_output_budget_exhausted",
        finish_reason: str = "length",
        reason_code: str = "",
        validation_context: dict | None = None,
    ) -> None:
        failure = ProviderDriverError(
            "prompt.normalize response is not one JSON document",
            code="provider_response_invalid",
            submission_uncertain=False,
        )
        failure.metadata = {
            "contract_error_code": classifier,
            "finish_reason": finish_reason,
            "reason_code": reason_code,
            "validation_context": validation_context or {},
            "usage": {
                "prompt_tokens": 7000,
                "completion_tokens": 8192,
                "total_tokens": 15192,
            },
            "response_id_sha256": "a" * 64,
            "raw_response_id": "provider-secret-response-id",
            "reasoning_content": "provider-authored secret reasoning",
            "response_text": "provider-authored secret body",
            "raw_reason": "provider-authored secret validation detail",
        }
        with patch.object(OpenAICompatibleDriver, "invoke", side_effect=failure):
            process_text_attempt(attempt)

    def _process_safe_length_rejection(
        self,
        attempt: GenerationAttempt,
        *,
        under_by: int = 1,
    ) -> dict:
        generation_rule = attempt.request_snapshot["payload"][
            "target_prompt_contract"
        ]["item_generation_rules"][0]
        minimum = generation_rule["generation_min"]
        maximum = generation_rule["generation_max"]
        if minimum is not None and int(minimum) >= under_by:
            measured = int(minimum) - under_by
        elif maximum is not None:
            measured = int(maximum) + 1
        else:
            raise AssertionError("safe length retry fixture requires one frozen bound")
        validation_context = {
            "schema_version": 1,
            "field_key": generation_rule["profile_field_key"],
            "measured": measured,
            "minimum": minimum,
            "maximum": maximum,
            "length_unit": generation_rule["length_unit"],
            "request_index": 0,
        }
        self._process_invalid_response(
            attempt,
            classifier="prompt_field_generation_contract_invalid",
            finish_reason="stop",
            reason_code="length_out_of_range",
            validation_context=validation_context,
        )
        return validation_context

    def _reach_model_generated_attempt(
        self,
        attempt: GenerationAttempt,
        *,
        worker_prefix: str,
    ) -> GenerationAttempt:
        visited = set()
        while attempt.request_snapshot["generation_mode"] != "model_generated":
            self.assertNotIn(attempt.id, visited, "normalization did not advance to a new attempt")
            visited.add(attempt.id)
            with patch.object(
                OpenAICompatibleDriver,
                "invoke",
                return_value=self._runtime_result(attempt),
            ):
                process_text_attempt(attempt)
            attempt.refresh_from_db()
            self.assertEqual(attempt.status, GenerationAttempt.Status.SUCCEEDED, attempt.error_message)
            attempt = claim_next_text_attempt(
                f"{worker_prefix}-{attempt.request_snapshot['batch_index'] + 1}",
            )
            self.assertIsNotNone(attempt)
        return attempt

    def test_validation_retry_limit_two_stops_after_three_attempts_and_hands_to_review(self):
        run, first = self._create_and_reach_normalization(configure_profile=True)
        first = self._reach_model_generated_attempt(
            first,
            worker_prefix="prompt-normalization-safe-limit",
        )
        self._set_frozen_retry_limit(first, 2)

        self._process_safe_length_rejection(first, under_by=1)
        second = claim_next_text_attempt("prompt-normalization-test-worker-2")
        self.assertIsNotNone(second)
        self.assertEqual(second.attempt_number, 2)
        self._process_safe_length_rejection(second, under_by=2)
        second.refresh_from_db()
        self.assertEqual(
            second.error_code,
            "provider_response_invalid",
            second.error_message,
        )
        third = claim_next_text_attempt("prompt-normalization-test-worker-3")
        self.assertIsNotNone(third)
        self.assertEqual(third.attempt_number, 3)
        self._process_safe_length_rejection(third, under_by=3)

        run.refresh_from_db()
        normalize_stage = run.stages.get(stage_key="script_normalize")
        review_stage = run.stages.get(stage_key="script_normalize_review")
        self.assertEqual(normalize_stage.status, normalize_stage.Status.SUCCEEDED)
        self.assertEqual(review_stage.status, review_stage.Status.AWAITING_APPROVAL)
        self.assertEqual(run.status, ProductionRun.Status.AWAITING_APPROVAL)
        payload = normalize_stage.output_snapshot["review_payload"]
        self.assertEqual(
            payload["review_origin"],
            "human_after_validation_retry_exhausted",
        )
        self.assertEqual(payload["stop_reason"], "retry_limit_reached")
        self.assertEqual(payload["retry_policy"], {
            "config_key": "production.prompt_field_retry_limit",
            "retry_limit": 2,
            "max_attempts": 3,
            "attempts_used": 3,
        })
        self.assertEqual(len(payload["validation_failures"]), 3)
        self.assertTrue(all(
            failure["error_code"] == "provider_response_invalid"
            and failure["contract_error_code"]
            == "prompt_field_generation_contract_invalid"
            and failure["reason_code"] == "length_out_of_range"
            and failure["finish_reason"] == "stop"
            and failure["usage"]["completion_tokens"] == 8192
            and failure["response_id_sha256"] == "a" * 64
            and len(failure["resolution_evidence_sha256"]) == 64
            and failure["resolution_id"]
            for failure in payload["validation_failures"]
        ))
        serialized_failures = json.dumps(
            payload["validation_failures"],
            ensure_ascii=False,
        )
        self.assertNotIn("provider-secret-response-id", serialized_failures)
        self.assertNotIn("provider-authored secret reasoning", serialized_failures)
        self.assertNotIn("provider-authored secret body", serialized_failures)
        self.assertTrue(payload["candidates"])
        failed_request_ids = set(
            first.request_snapshot["batch_plan"]["batches"][
                first.request_snapshot["batch_index"]
            ]["request_ids"]
        )
        self.assertTrue(all(
            candidate["translated_text"] == ""
            for candidate in payload["candidates"]
            if candidate["request_id"] in failed_request_ids
        ))
        self.assertEqual(
            GenerationAttempt.objects.filter(
                stage_run=normalize_stage,
                unit_key=first.unit_key,
            ).count(),
            3,
        )

        # Re-reading/continuing an awaiting review must not create attempt 4.
        continued = self.client.post(
            f"/api/v1/production-runs/{run.id}/continue/",
            {},
            format="json",
        )
        self.assertEqual(continued.status_code, 200, continued.data)
        self.assertEqual(
            GenerationAttempt.objects.filter(
                stage_run=normalize_stage,
                unit_key=first.unit_key,
            ).count(),
            3,
        )

    def test_reasoning_only_failure_persists_only_safe_classifier_evidence(self):
        run, attempt = self._create_and_reach_normalization()
        self._set_frozen_retry_limit(attempt, 0)

        self._process_invalid_response(
            attempt,
            classifier="response_reasoning_without_text",
            finish_reason="stop",
        )

        normalize_stage = run.stages.get(stage_key="script_normalize")
        failure = normalize_stage.output_snapshot["review_payload"][
            "validation_failures"
        ][0]
        self.assertEqual(
            failure["contract_error_code"],
            "response_reasoning_without_text",
        )
        self.assertEqual(failure["finish_reason"], "stop")
        resolution = GenerationAttempt.objects.get(pk=attempt.pk).resolutions.get(
            outcome=GenerationAttemptResolution.Outcome.KNOWN_RESPONSE_REJECTED,
        )
        serialized = json.dumps(resolution.evidence, ensure_ascii=False)
        self.assertNotIn("provider-secret-response-id", serialized)
        self.assertNotIn("provider-authored secret reasoning", serialized)
        self.assertNotIn("provider-authored secret body", serialized)

    def test_field_validation_reason_is_safe_in_resolution_and_review_summary(self):
        run, attempt = self._create_and_reach_normalization(configure_profile=True)
        attempt = self._reach_model_generated_attempt(attempt, worker_prefix="prompt-normalization-context")
        generation_rule = attempt.request_snapshot["payload"]["target_prompt_contract"][
            "item_generation_rules"
        ][0]
        minimum = generation_rule["generation_min"]
        maximum = generation_rule["generation_max"]
        measured = (
            int(minimum) - 1
            if minimum is not None
            else int(maximum) + 1
        )
        validation_context = {
            "schema_version": 1,
            "field_key": generation_rule["profile_field_key"],
            "measured": measured,
            "minimum": minimum,
            "maximum": maximum,
            "length_unit": generation_rule["length_unit"],
            "request_index": 0,
        }
        self._set_frozen_retry_limit(attempt, 0)

        self._process_invalid_response(
            attempt,
            classifier="prompt_field_generation_contract_invalid",
            finish_reason="stop",
            reason_code="length_out_of_range",
            validation_context=validation_context,
        )

        normalize_stage = run.stages.get(stage_key="script_normalize")
        failure = normalize_stage.output_snapshot["review_payload"][
            "validation_failures"
        ][0]
        self.assertEqual(
            failure["contract_error_code"],
            "prompt_field_generation_contract_invalid",
        )
        self.assertEqual(failure["reason_code"], "length_out_of_range")
        self.assertEqual(failure["validation_context"], validation_context)
        resolution = GenerationAttempt.objects.get(pk=attempt.pk).resolutions.get(
            outcome=GenerationAttemptResolution.Outcome.KNOWN_RESPONSE_REJECTED,
        )
        self.assertEqual(resolution.evidence["reason_code"], "length_out_of_range")
        self.assertEqual(
            resolution.evidence["validation_context"],
            failure["validation_context"],
        )
        serialized = json.dumps({
            "resolution": resolution.evidence,
            "failure": failure,
        }, ensure_ascii=False)
        self.assertNotIn("provider-authored secret validation detail", serialized)
        self.assertNotIn("provider-authored secret body", serialized)

    def test_missing_frozen_retry_policy_fails_closed(self):
        _run, attempt = self._create_and_reach_normalization()
        stage = attempt.stage_run
        snapshot = deepcopy(stage.config_snapshot)
        snapshot["base"]["values"].pop("production.prompt_field_retry_limit")
        snapshot["routes"][str(attempt.route_index)]["values"].pop(
            "production.prompt_field_retry_limit",
        )
        assign_json_contract(stage, "config_snapshot", snapshot, max_bytes=_stage_snapshot_max_bytes())
        stage.save(update_fields=(
            "config_snapshot",
            "config_snapshot_schema_version",
            "config_snapshot_size_bytes",
            "config_snapshot_sha256",
            "updated_at",
        ))

        with self.assertRaises(StageBlocked) as blocked:
            _prompt_field_retry_policy(
                stage,
                route_index=attempt.route_index,
            )
        self.assertEqual(blocked.exception.code, "config_snapshot_missing")

    def test_retry_limit_zero_requires_human_values_and_can_continue_after_review(self):
        run, attempt = self._create_and_reach_normalization()
        self._set_frozen_retry_limit(attempt, 0)
        self._process_invalid_response(attempt)

        run.refresh_from_db()
        normalize_stage = run.stages.get(stage_key="script_normalize")
        payload = normalize_stage.output_snapshot["review_payload"]
        self.assertEqual(payload["retry_policy"]["max_attempts"], 1)
        self.assertEqual(
            GenerationAttempt.objects.filter(stage_run=normalize_stage).count(),
            1,
        )
        detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
        normalize_projection = next(
            row for row in detail.data["stages"]
            if row["stage_key"] == "script_normalize"
        )
        approved = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "comment": "人工接管并逐项填写英文执行字段",
            "expected_source_snapshot_sha256": normalize_projection[
                "output_snapshot_sha256"
            ],
            "candidate_overrides": {
                candidate["request_id"]: "A detective slowly enters the dim old mansion."
                for candidate in payload["candidates"]
            },
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        entries = list(PromptFieldTranslation.objects.all())
        self.assertTrue(entries)
        self.assertTrue(all(
            entry.origin_generation_attempt_id == attempt.id
            and entry.runtime_metadata.get("review_origin")
            == "human_after_validation_retry_exhausted"
            for entry in entries
        ))

    def test_unattended_policy_escalates_only_exhausted_validation_to_human_approval(self):
        run, attempt = self._create_and_reach_normalization()
        self._set_frozen_retry_limit(attempt, 0)
        review_stage = run.stages.get(stage_key="script_normalize_review")
        review_stage.decision_policy = "accept_all_valid_frozen_prompt_translations_v2"
        review_stage.decision_policy_sha256 = "a" * 64
        review_stage.save(update_fields=(
            "decision_policy",
            "decision_policy_sha256",
            "updated_at",
        ))

        with patch(
            "studio.services.production_runs._is_unattended_run",
            return_value=True,
        ):
            self._process_invalid_response(attempt)

        run.refresh_from_db()
        review_stage.refresh_from_db()
        self.assertEqual(run.status, ProductionRun.Status.AWAITING_APPROVAL)
        self.assertEqual(review_stage.status, review_stage.Status.AWAITING_APPROVAL)
        self.assertEqual(
            review_stage.reason_code,
            "prompt_validation_review_required",
        )
        normalize_stage = run.stages.get(stage_key="script_normalize")
        payload = normalize_stage.output_snapshot["review_payload"]
        detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
        normalize_projection = next(
            row for row in detail.data["stages"]
            if row["stage_key"] == "script_normalize"
        )
        with patch(
            "studio.services.production_runs._is_unattended_run",
            return_value=True,
        ):
            approved = self.client.post(
                f"/api/v1/production-runs/{run.id}/approve/",
                {
                    "stage_key": "script_normalize_review",
                    "decision": "approve",
                    "comment": "全自动运行校验耗尽后人工接管",
                    "expected_source_snapshot_sha256": normalize_projection[
                        "output_snapshot_sha256"
                    ],
                    "candidate_overrides": {
                        candidate["request_id"]: (
                            "A detective slowly enters the dim old mansion."
                        )
                        for candidate in payload["candidates"]
                    },
                },
                format="json",
            )
        self.assertEqual(approved.status_code, 200, approved.data)
        review_stage.refresh_from_db()
        decision = review_stage.approval_decisions.get()
        self.assertEqual(decision.actor, decision.Actor.HUMAN)
        self.assertEqual(decision.decided_by_id, self.user.id)

    def test_validation_without_safe_feedback_does_not_replay_and_hands_to_review(self):
        run, first = self._create_and_reach_normalization()
        self._set_frozen_retry_limit(first, 2)
        self._process_invalid_response(first)
        second = claim_next_text_attempt("prompt-normalization-test-worker-2")
        self.assertIsNone(second)

        run.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(first.status, GenerationAttempt.Status.FAILED)
        self.assertEqual(first.error_code, "provider_response_invalid")
        self.assertTrue(first.resolutions.filter(
            outcome="known_response_rejected",
        ).exists())
        self.assertEqual(run.current_stage_key, "script_normalize_review")
        normalize_stage = run.stages.get(stage_key="script_normalize")
        payload = normalize_stage.output_snapshot["review_payload"]
        self.assertEqual(
            payload["review_origin"],
            "human_after_validation_retry_exhausted",
        )
        self.assertEqual(payload["stop_reason"], "safe_feedback_unavailable")
        self.assertEqual(payload["retry_policy"], {
            "config_key": "production.prompt_field_retry_limit",
            "retry_limit": 2,
            "max_attempts": 3,
            "attempts_used": 1,
        })
        self.assertEqual(len(payload["validation_failures"]), 1)
        self.assertEqual(
            GenerationAttempt.objects.filter(stage_run=normalize_stage).count(),
            1,
        )
        self.assertFalse(normalize_stage.route_events.filter(
            event_type="validation_retry_queued",
        ).exists())
        self.assertNotIn("retry_feedback", first.request_snapshot["payload"])
        self.assertNotIn("repair_of_attempt_id", first.request_snapshot)

        detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
        normalize_projection = next(
            row for row in detail.data["stages"]
            if row["stage_key"] == "script_normalize"
        )
        approved = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "comment": "无安全修复反馈，人工逐项定稿",
            "expected_source_snapshot_sha256": normalize_projection[
                "output_snapshot_sha256"
            ],
            "candidate_overrides": {
                candidate["request_id"]: (
                    "A detective slowly enters the dim old mansion."
                )
                for candidate in payload["candidates"]
            },
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertTrue(PromptFieldTranslation.objects.exists())

    def test_safe_length_rejection_freezes_attempt_specific_repair_feedback(self):
        run, first = self._create_and_reach_normalization(configure_profile=True)
        first = self._reach_model_generated_attempt(first, worker_prefix="prompt-normalization-repair")
        self._set_frozen_retry_limit(first, 1)
        generation_rule = first.request_snapshot["payload"][
            "target_prompt_contract"
        ]["item_generation_rules"][0]
        minimum = generation_rule["generation_min"]
        maximum = generation_rule["generation_max"]
        measured = (
            int(minimum) - 1
            if minimum is not None
            else int(maximum) + 1
        )
        validation_context = {
            "schema_version": 1,
            "field_key": generation_rule["profile_field_key"],
            "measured": measured,
            "minimum": minimum,
            "maximum": maximum,
            "length_unit": generation_rule["length_unit"],
            "request_index": 0,
        }
        original_payload = deepcopy(first.request_snapshot["payload"])
        original_batch_plan_sha256 = first.request_snapshot["batch_plan"][
            "batch_plan_sha256"
        ]
        original_context_sha256 = original_payload["target_prompt_contract"][
            "frozen_context_sha256"
        ]

        self._process_invalid_response(
            first,
            classifier="prompt_field_generation_contract_invalid",
            finish_reason="stop",
            reason_code="length_out_of_range",
            validation_context=validation_context,
        )
        second = claim_next_text_attempt("prompt-normalization-safe-repair-2")
        self.assertIsNotNone(second)
        self.assertEqual(second.attempt_number, first.attempt_number + 1)
        self.assertEqual(
            second.request_snapshot["repair_of_attempt_id"],
            str(first.id),
        )
        feedback = second.request_snapshot["payload"]["retry_feedback"]
        self.assertEqual(feedback, {
            "schema_version": 1,
            "reason_code": "length_out_of_range",
            "validation_context": validation_context,
        })
        self.assertEqual(
            second.request_snapshot["retry_feedback_sha256"],
            json_contract(feedback).sha256,
        )
        public_attempt = self.client.get(
            f"/api/v1/generation-attempts/{second.id}/",
        )
        self.assertEqual(public_attempt.status_code, 200, public_attempt.data)
        self.assertEqual(public_attempt.data["repair_of_attempt_id"], str(first.id))
        self.assertEqual(
            public_attempt.data["retry_feedback_sha256"],
            json_contract(feedback).sha256,
        )
        self.assertNotIn("request_snapshot", public_attempt.data)
        self.assertNotIn("retry_feedback", public_attempt.data)
        self.assertNotEqual(second.request_sha256, first.request_sha256)
        self.assertEqual(
            second.request_snapshot["batch_plan"]["batch_plan_sha256"],
            original_batch_plan_sha256,
        )
        self.assertEqual(
            second.request_snapshot["payload"]["target_prompt_contract"][
                "frozen_context_sha256"
            ],
            original_context_sha256,
        )
        retry_base_payload = deepcopy(second.request_snapshot["payload"])
        retry_base_payload.pop("retry_feedback")
        self.assertEqual(retry_base_payload, original_payload)
        self.assertEqual(second.prompt_profile_version_id, first.prompt_profile_version_id)
        self.assertEqual(
            second.prompt_profile_contract_sha256,
            first.prompt_profile_contract_sha256,
        )
        policy = second.request_snapshot["capacity_policy"]
        chat = prompt_normalization_chat_payload(
            second.request_snapshot["payload"],
            policy=policy,
        )
        self.assertNotIn(
            "retry_feedback",
            json.loads(chat["messages"][1]["content"]),
        )
        trusted = trusted_prompt_normalization_generation_tasks(
            second.request_snapshot["payload"],
        )
        repaired_task = next(
            task for task in trusted["tasks"]
            if task["request_index"] == feedback["validation_context"]["request_index"]
        )
        self.assertEqual(repaired_task["repair_feedback"], feedback)
        self.assertIn(
            json.dumps(
                trusted,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            chat["messages"][0]["content"],
        )
        first.refresh_from_db()
        self.assertTrue(first.resolutions.filter(
            outcome=GenerationAttemptResolution.Outcome.KNOWN_RESPONSE_REJECTED,
        ).exists())

    def test_translate_only_canonical_token_rejection_queues_targeted_retry(self):
        CharacterBible.objects.create(
            project=self.project,
            slug="detective",
            name="侦探",
            stable_speaker_id="S9",
        )
        run, first = self._create_and_reach_normalization()
        for batch_number in range(20):
            payload = first.request_snapshot["payload"]
            protected_index = next((
                index
                for index, item in enumerate(payload.get("items") or [])
                if isinstance(item, dict) and item.get("protected_tokens")
            ), None)
            if (
                protected_index is not None
                and "target_prompt_contract" not in payload
                and first.request_snapshot.get("execution_kind") == "provider"
            ):
                break
            with patch.object(
                OpenAICompatibleDriver,
                "invoke",
                return_value=self._runtime_result(first),
            ):
                process_text_attempt(first)
            first = claim_next_text_attempt(
                f"prompt-normalization-canonical-repair-{batch_number}",
            )
            self.assertIsNotNone(first)
        else:
            self.fail("未找到包含 canonical identity token 的 translate_only 批次")

        self._set_frozen_retry_limit(first, 1)
        item = payload["items"][protected_index]
        token = item["protected_tokens"][0]
        expected_count = item["text"].count(token)
        validation_context = {
            "schema_version": 1,
            "field_key": item["field_key"],
            "observed_count": 0,
            "expected_min": expected_count,
            "expected_max": expected_count,
            "request_index": protected_index,
        }
        original_payload = deepcopy(payload)
        original_batch_plan_sha256 = first.request_snapshot["batch_plan"][
            "batch_plan_sha256"
        ]

        self._process_invalid_response(
            first,
            classifier="prompt_normalize_contract_invalid",
            finish_reason="stop",
            reason_code="protected_token_mismatch",
            validation_context=validation_context,
        )
        second = claim_next_text_attempt(
            "prompt-normalization-canonical-safe-repair",
        )
        self.assertIsNotNone(second)
        self.assertEqual(second.unit_key, first.unit_key)
        self.assertEqual(second.attempt_number, first.attempt_number + 1)
        feedback = second.request_snapshot["payload"]["retry_feedback"]
        self.assertEqual(feedback, {
            "schema_version": 1,
            "reason_code": "protected_token_mismatch",
            "validation_context": validation_context,
        })
        self.assertEqual(
            second.request_snapshot["repair_of_attempt_id"],
            str(first.id),
        )
        self.assertEqual(
            second.request_snapshot["batch_plan"]["batch_plan_sha256"],
            original_batch_plan_sha256,
        )
        retry_base_payload = deepcopy(second.request_snapshot["payload"])
        retry_base_payload.pop("retry_feedback")
        self.assertEqual(retry_base_payload, original_payload)

        chat = prompt_normalization_chat_payload(
            second.request_snapshot["payload"],
            policy=second.request_snapshot["capacity_policy"],
        )
        self.assertNotIn(
            "retry_feedback",
            json.loads(chat["messages"][1]["content"]),
        )
        system_content = chat["messages"][0]["content"]
        encoded_constraints = system_content.split(
            "TRUSTED_TRANSLATION_CONSTRAINTS_JSON_BEGIN\n",
            1,
        )[1].split("\nTRUSTED_TRANSLATION_CONSTRAINTS_JSON_END", 1)[0]
        constraints = json.loads(encoded_constraints)
        repaired_task = next(
            task for task in constraints["tasks"]
            if task["request_index"] == protected_index
        )
        self.assertEqual(repaired_task["repair_feedback"], feedback)
        self.assertEqual(
            repaired_task["protected_token_requirements"][0],
            {
                "placeholder": _protected_token_sentinel(token),
                "minimum": expected_count,
                "maximum": expected_count,
            },
        )

        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            return_value=self._runtime_result(second),
        ):
            process_text_attempt(second)
        second.refresh_from_db()
        self.assertEqual(second.status, GenerationAttempt.Status.SUCCEEDED)
        normalize_stage = run.stages.get(stage_key="script_normalize")
        self.assertFalse(
            normalize_stage.output_snapshot.get("review_payload", {}).get(
                "stop_reason",
            ) == "safe_feedback_unavailable",
        )

    def test_validation_retry_budget_counts_only_definite_validation_failures(self):
        run, first = self._create_and_reach_normalization(configure_profile=True)
        first = self._reach_model_generated_attempt(
            first,
            worker_prefix="prompt-normalization-safe-count",
        )
        self._set_frozen_retry_limit(first, 2)
        first.attempt_number = 2
        first.save(update_fields=("attempt_number", "updated_at"))
        earlier = GenerationAttempt.objects.create(
            stage_run=first.stage_run,
            unit_key=first.unit_key,
            attempt_number=1,
            model_deployment=first.model_deployment,
            prompt_profile_version=first.prompt_profile_version,
            prompt_profile_binding_key=first.prompt_profile_binding_key,
            prompt_profile_contract_sha256=first.prompt_profile_contract_sha256,
            route_index=first.route_index,
            operation=first.operation,
            is_external=first.is_external,
            status=GenerationAttempt.Status.FAILED,
            request_sha256=first.request_sha256,
            request_snapshot=deepcopy(first.request_snapshot),
            request_snapshot_schema_version=first.request_snapshot_schema_version,
            request_snapshot_size_bytes=first.request_snapshot_size_bytes,
            request_snapshot_sha256=first.request_snapshot_sha256,
            error_code="provider_request_rejected",
            error_message="Known request rejection before any response validation.",
            finished_at=timezone.now(),
        )
        GenerationAttempt.objects.filter(pk=earlier.pk).update(
            created_at=first.created_at - timedelta(seconds=1),
        )

        self._process_safe_length_rejection(first, under_by=1)
        second_validation = claim_next_text_attempt(
            "prompt-normalization-validation-count-worker-2",
        )
        self.assertEqual(second_validation.attempt_number, 3)
        self._process_safe_length_rejection(second_validation, under_by=2)
        third_validation = claim_next_text_attempt(
            "prompt-normalization-validation-count-worker-3",
        )
        self.assertIsNotNone(third_validation)
        self.assertEqual(third_validation.attempt_number, 4)
        self._process_safe_length_rejection(third_validation, under_by=3)

        run.refresh_from_db()
        normalize_stage = run.stages.get(stage_key="script_normalize")
        payload = normalize_stage.output_snapshot["review_payload"]
        self.assertEqual(payload["retry_policy"]["attempts_used"], 3)
        self.assertEqual(
            [row["attempt_number"] for row in payload["validation_failures"]],
            [2, 3, 4],
        )
        self.assertEqual(
            GenerationAttempt.objects.filter(
                stage_run=normalize_stage,
                unit_key=first.unit_key,
            ).count(),
            4,
        )

    def test_candidates_are_not_written_until_review_approval(self):
        run, attempt = self._create_and_reach_normalization()
        self.assertEqual(PromptFieldTranslation.objects.count(), 0)
        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            return_value=self._runtime_result(attempt),
        ):
            process_text_attempt(attempt)

        run.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, GenerationAttempt.Status.SUCCEEDED)
        self.assertEqual(run.current_stage_key, "script_normalize_review")
        self.assertEqual(PromptFieldTranslation.objects.count(), 0)
        owner_projection = self.client.get(f"/api/v1/generation-attempts/{attempt.id}/")
        self.assertEqual(owner_projection.status_code, 200, owner_projection.data)
        self.assertNotIn("provider_request_id", owner_projection.data)
        self.assertTrue(owner_projection.data["provider_request_id_sha256"])
        self.assertEqual(owner_projection.data["provider_request_id_suffix"], "")
        serialized_owner = str(owner_projection.data)
        self.assertNotIn("provider-normalization-id", serialized_owner)
        self.assertNotIn("request_snapshot", owner_projection.data)

        staff_client = APIClient()
        staff_client.force_authenticate(self.admin)
        staff_projection = staff_client.get(f"/api/v1/generation-attempts/{attempt.id}/")
        self.assertEqual(staff_projection.status_code, 200, staff_projection.data)
        self.assertEqual(staff_projection.data["provider_request_id_suffix"], "ion-id")
        self.assertNotIn("provider-normalization-id", str(staff_projection.data))

        run.refresh_from_db()
        normalize_snapshot = run.stages.get(
            stage_key="script_normalize",
        ).output_snapshot
        candidates = normalize_snapshot["review_payload"]["candidates"]
        reviewed_request_id = candidates[0]["request_id"]
        reviewed_translation = "A detective enters the old mansion under cold blue light."

        detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
        public_normalize_hash = next(
            item["output_snapshot_sha256"]
            for item in detail.data["stages"]
            if item["stage_key"] == "script_normalize"
        )
        approved = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "comment": "English prompt fields reviewed",
            "expected_source_snapshot_sha256": public_normalize_hash,
            "candidate_overrides": {
                **{
                    candidate["request_id"]: candidate["translated_text"]
                    for candidate in candidates
                },
                reviewed_request_id: reviewed_translation,
            },
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        entries = list(PromptFieldTranslation.objects.all())
        self.assertTrue(entries)
        self.assertTrue(all(item.status == PromptFieldTranslation.Status.CONFIRMED for item in entries))
        self.assertTrue(all(item.origin_generation_attempt_id == attempt.id for item in entries))
        self.assertIn(reviewed_translation, [item.translated_text for item in entries])
        with self.assertRaises(ProtectedError):
            attempt.delete()

    def test_target_language_source_uses_zero_cost_server_materialization(self):
        self.shot.narrative = (
            "A detective walks slowly into the dim old mansion under cold blue light."
        )
        self.shot.save(update_fields=("narrative", "updated_at"))

        _run, attempt = self._create_and_reach_normalization()

        self.assertEqual(
            attempt.request_snapshot["execution_kind"],
            "server_deterministic",
        )
        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            side_effect=AssertionError("deterministic materialization must not call Provider"),
        ) as invoke:
            process_text_attempt(attempt)
        invoke.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, GenerationAttempt.Status.SUCCEEDED)
        self.assertFalse(attempt.is_external)
        self.assertEqual(attempt.estimated_cost, 0)
        self.assertEqual(attempt.committed_cost, 0)
        self.assertEqual(attempt.actual_cost, 0)
        self.assertIsNone(attempt.submission_started_at)
        self.assertIsNone(attempt.submitted_at)
        self.assertEqual(attempt.provider_request_id, "")
        self.assertTrue(all(
            proof.get("materialization_kind")
            == "source_already_satisfies_target_contract"
            for proof in attempt.request_snapshot["batch_plan"]["batches"][0][
                "materialization_proofs"
            ]
        ))

    def test_protected_only_name_uses_zero_cost_server_materialization(self):
        CharacterBible.objects.create(
            project=self.project,
            slug="zhou-lan",
            name="周岚",
            stable_speaker_id="S1",
        )
        run, attempt = self._create_and_reach_normalization()
        execution_kinds = {
            str(batch.get("execution_kind") or "")
            for batch in attempt.request_snapshot["batch_plan"]["batches"]
        }
        self.assertEqual(execution_kinds, {"provider", "server_deterministic"})

        local_attempt = None
        provider_invocations = 0
        while True:
            execution_kind = str(attempt.request_snapshot.get("execution_kind") or "")
            if execution_kind == "server_deterministic":
                with patch.object(
                    OpenAICompatibleDriver,
                    "invoke",
                    side_effect=AssertionError("local materialization must not call Provider"),
                ) as invoke:
                    process_text_attempt(attempt)
                invoke.assert_not_called()
                attempt.refresh_from_db()
                local_attempt = attempt
                self.assertEqual(attempt.status, GenerationAttempt.Status.SUCCEEDED)
                self.assertFalse(attempt.is_external)
                self.assertEqual(attempt.estimated_cost, 0)
                self.assertEqual(attempt.committed_cost, 0)
                self.assertEqual(attempt.actual_cost, 0)
                self.assertIsNone(attempt.submission_started_at)
                self.assertIsNone(attempt.submitted_at)
                self.assertEqual(attempt.provider_request_id, "")
            else:
                with patch.object(
                    OpenAICompatibleDriver,
                    "invoke",
                    return_value=self._runtime_result(attempt),
                ) as invoke:
                    process_text_attempt(attempt)
                provider_invocations += invoke.call_count
                attempt.refresh_from_db()
                self.assertEqual(attempt.status, GenerationAttempt.Status.SUCCEEDED, attempt.error_message)
            run.refresh_from_db()
            if run.current_stage_key == "script_normalize_review":
                break
            attempt = claim_next_text_attempt("prompt-normalization-test-worker-next")
            self.assertIsNotNone(attempt)

        self.assertIsNotNone(local_attempt)
        self.assertGreater(provider_invocations, 0)
        self.assertEqual(
            run.actual_cost_total,
            Decimal("0.001") * provider_invocations,
        )
        normalize_stage = run.stages.get(stage_key="script_normalize")
        payload = normalize_stage.output_snapshot["review_payload"]
        self.assertEqual(payload["review_origin"], "hybrid_candidate")
        name_candidate = next(
            item for item in payload["candidates"]
            if item["field_key"] == "character.name"
        )
        self.assertEqual(name_candidate["translated_text"], "S1")
        self.assertEqual(name_candidate["generation_attempt_id"], str(local_attempt.id))
        detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
        public_snapshot_sha256 = next(
            item["output_snapshot_sha256"]
            for item in detail.data["stages"]
            if item["stage_key"] == "script_normalize"
        )
        approved = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "comment": "mixed local and Provider candidates reviewed",
            "expected_source_snapshot_sha256": public_snapshot_sha256,
            "candidate_overrides": {
                candidate["request_id"]: (
                    "Zhou Lan"
                    if candidate["field_key"] == "character.name"
                    else candidate["translated_text"]
                )
                for candidate in payload["candidates"]
            },
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        name_entry = PromptFieldTranslation.objects.get(
            project=self.project,
            scope_type=PromptFieldTranslation.ScopeType.CHARACTER,
            field_key="character.name",
        )
        self.assertEqual(name_entry.provider, "")
        self.assertEqual(name_entry.model, "")
        self.assertEqual(name_entry.translated_text, "Zhou Lan")
        self.assertEqual(
            name_entry.runtime_metadata["submission_semantics"],
            "server_deterministic_no_submission",
        )
        self.assertEqual(
            name_entry.runtime_metadata["review_origin"],
            "server_deterministic_candidate",
        )
        provider_entry = PromptFieldTranslation.objects.exclude(
            origin_generation_attempt=local_attempt,
        ).first()
        self.assertIsNotNone(provider_entry)
        self.assertEqual(provider_entry.provider, "run-prompt-normalize-provider")
        self.assertEqual(provider_entry.model, "strict-prompt-normalizer")

    def test_review_rejects_override_for_unknown_frozen_candidate(self):
        run, attempt = self._create_and_reach_normalization()
        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            return_value=self._runtime_result(attempt),
        ):
            process_text_attempt(attempt)

        detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
        public_normalize_hash = next(
            item["output_snapshot_sha256"]
            for item in detail.data["stages"]
            if item["stage_key"] == "script_normalize"
        )

        rejected = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "comment": "unknown candidate must fail",
            "expected_source_snapshot_sha256": public_normalize_hash,
            "candidate_overrides": {
                "999-not-in-frozen-snapshot": "Invented replacement",
            },
        }, format="json")

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(PromptFieldTranslation.objects.count(), 0)

    def test_reviewed_names_atomically_become_canonical_project_glossary(self):
        run, attempt = self._create_and_reach_normalization()
        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            return_value=self._runtime_result(attempt),
        ):
            process_text_attempt(attempt)
        attempt.refresh_from_db()

        character = CharacterBible.objects.create(
            project=self.project,
            slug="lin-wu",
            name="林雾",
            stable_speaker_id="S1",
        )
        scene = SceneBible.objects.create(
            project=self.project,
            slug="rainy-hotel-room",
            name="旅馆房间",
        )
        self.shot.narrative = "林雾在旅馆房间按下录音机。"
        self.shot.save(update_fields=("narrative", "updated_at"))

        specs = (
            (
                PromptFieldTranslation.ScopeType.CHARACTER,
                character.id,
                "character.name",
                character.name,
                "Lin Wu",
            ),
            (
                PromptFieldTranslation.ScopeType.SCENE,
                scene.id,
                "scene.name",
                scene.name,
                "Rainy Hotel Room",
            ),
            (
                PromptFieldTranslation.ScopeType.SHOT,
                self.shot.id,
                "shot.narrative",
                self.shot.narrative,
                "Lin Wu presses the recorder in the Rainy Hotel Room.",
            ),
        )
        candidates = []
        frozen_items = []
        frozen_targets = []
        for index, (scope_type, scope_id, field_key, source, translated) in enumerate(
            specs, start=1,
        ):
            item, target = prepare_prompt_normalization_item(
                project=self.project,
                scope_type=scope_type,
                scope_id=scope_id,
                field_key=field_key,
                source_text=source,
                label=f"review-{index}",
                request_id=f"review-{index}",
            )
            frozen_items.append(item)
            frozen_targets.append(target)
            candidates.append({
                key: value for key, value in target.items()
                if key in {
                    "request_id", "scope_type", "scope_id", "field_key",
                    "source_hash", "context_hash",
                }
            } | {"translated_text": translated})

        request_snapshot = deepcopy(attempt.request_snapshot)
        request_snapshot["payload"] = {
            "schema_version": 1,
            "items": frozen_items,
        }
        request_snapshot["targets"] = frozen_targets
        assign_json_contract(attempt, "request_snapshot", request_snapshot)
        attempt.save(update_fields=(
            "request_snapshot",
            "request_snapshot_schema_version",
            "request_snapshot_size_bytes",
            "request_snapshot_sha256",
            "updated_at",
        ))

        materialized = confirm_prompt_normalization_candidates(
            project=self.project,
            attempt=attempt,
            candidates=candidates,
            user=self.user,
            provider="deepseek",
            model="strict-prompt-normalizer",
            result_sha256=attempt.result_sha256,
            submission_semantics="durable_single_submit_manual_resolution",
        )

        self.project.refresh_from_db()
        self.assertEqual(
            self.project.production_settings["translation_glossary"],
            {"林雾": "Lin Wu", "旅馆房间": "Rainy Hotel Room"},
        )
        self.assertEqual(len(materialized), 3)
        narrative = PromptFieldTranslation.objects.get(
            scope_type=PromptFieldTranslation.ScopeType.SHOT,
            scope_id=self.shot.id,
            field_key="shot.narrative",
        )
        current_context_hash = translation_context_hash(build_translation_context(
            self.project,
            scope_type=narrative.scope_type,
            scope_id=narrative.scope_id,
            field_key=narrative.field_key,
            source_text=narrative.source_text,
        ))
        self.assertEqual(
            narrative.runtime_metadata["translation_context_hash"],
            current_context_hash,
        )
        self.assertNotEqual(
            narrative.runtime_metadata["review_context_hash"],
            current_context_hash,
        )

    def test_source_drift_rejects_review_without_partial_translation_writes(self):
        run, attempt = self._create_and_reach_normalization()
        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            return_value=self._runtime_result(attempt),
        ):
            process_text_attempt(attempt)
        detail = self.client.get(f"/api/v1/production-runs/{run.id}/")
        normalize_stage = next(
            item for item in detail.data["stages"]
            if item["stage_key"] == "script_normalize"
        )
        candidate_overrides = {
            candidate["request_id"]: candidate["translated_text"]
            for candidate in normalize_stage["output_snapshot"]["review_payload"]["candidates"]
        }
        self.shot.narrative = "镜头内容已被另一位操作者修改。"
        self.shot.save(update_fields=("narrative", "updated_at"))

        rejected = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "comment": "stale candidate should fail",
            "expected_source_snapshot_sha256": normalize_stage["output_snapshot_sha256"],
            "candidate_overrides": candidate_overrides,
        }, format="json")

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(PromptFieldTranslation.objects.count(), 0)

    def test_uncertain_external_submit_cannot_retry_until_staff_confirms_absence(self):
        run, attempt = self._create_and_reach_normalization()
        failure = ProviderDriverError(
            "connection lost after POST",
            code="provider_request_failed",
            submission_uncertain=True,
        )
        with patch.object(OpenAICompatibleDriver, "invoke", side_effect=failure):
            process_text_attempt(attempt)

        run.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, GenerationAttempt.Status.FAILED)
        self.assertEqual(attempt.error_code, "uncertain_remote_response")
        self.assertIsNotNone(attempt.committed_cost)
        self.assertGreater(attempt.committed_cost, 0)
        blocked_retry = self.client.post(
            f"/api/v1/production-runs/{run.id}/retry/",
            {},
            format="json",
        )
        self.assertEqual(blocked_retry.status_code, 409, blocked_retry.data)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=attempt.stage_run).count(), 1)
        owner_resolution = self.client.post(
            f"/api/v1/production-runs/{run.id}/resolve-uncertain/",
            {
                "attempt_id": str(attempt.id),
                "outcome": "confirmed_not_submitted",
                "note": "owner must not resolve",
            },
            format="json",
        )
        self.assertEqual(owner_resolution.status_code, 403, owner_resolution.data)

        admin_client = APIClient()
        admin_client.force_authenticate(self.admin)
        resolved = admin_client.post(
            f"/api/v1/production-runs/{run.id}/resolve-uncertain/",
            {
                "attempt_id": str(attempt.id),
                "outcome": "confirmed_not_submitted",
                "note": "Provider audit confirms no request was accepted.",
            },
            format="json",
        )
        self.assertEqual(resolved.status_code, 200, resolved.data)
        attempt.refresh_from_db()
        self.assertEqual(attempt.error_code, "remote_absence_confirmed")
        self.assertEqual(attempt.committed_cost, 0)

        retried = self.client.post(
            f"/api/v1/production-runs/{run.id}/retry/",
            {},
            format="json",
        )
        self.assertEqual(retried.status_code, 200, retried.data)
        attempts = list(
            GenerationAttempt.objects.filter(stage_run=attempt.stage_run).order_by("attempt_number"),
        )
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1].attempt_number, 2)
        self.assertEqual(attempts[1].status, GenerationAttempt.Status.QUEUED)
        self.assertGreater(attempts[1].committed_cost, 0)
