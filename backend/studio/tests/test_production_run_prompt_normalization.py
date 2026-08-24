from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    CharacterBible,
    Episode,
    GenerationAttempt,
    ModelDeployment,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    PromptFieldTranslation,
    ProviderProfile,
    SceneBible,
    SeriesProject,
    Shot,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.prompt_normalization import PROMPT_NORMALIZE_OPERATION
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
from studio.tests.factories import create_test_prompt_profile_binding


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
        self.project = SeriesProject.objects.create(
            owner=self.user,
            title="提示词规范化审批闭环",
        )
        _, revision = create_pasted_script_source(
            project=self.project,
            user=self.user,
            content="# 第一集\n\n侦探进入旧宅。",
        )
        self.revision = confirm_script_revision(revision, user=self.user)
        episode = Episode.objects.create(
            project=self.project,
            number=1,
            title="旧宅",
            script="侦探进入旧宅。",
        )
        self.shot = Shot.objects.create(
            episode=episode,
            number=1,
            narrative="侦探缓慢走进昏暗的旧宅。",
            duration_ms=3000,
        )
        self.deployment = self._deployment()
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
            "capabilities": [PROMPT_NORMALIZE_OPERATION],
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

    def _pass_smoke(self) -> None:
        result = {
            "schema_version": 1,
            "request_id": "smoke-provider-id",
            "items": [{
                "request_id": "prompt-normalize-smoke",
                "ok": True,
                "translation": "Slow push-in __AUTOPROMPT_LITERAL_SMOKE_0001__",
            }],
            "metadata": {},
        }
        from studio.services.prompt_normalization import smoke_prompt_normalization_deployment

        with patch.object(OpenAICompatibleDriver, "invoke", return_value=result):
            snapshot = smoke_prompt_normalization_deployment(self.deployment)
        self.assertEqual(snapshot.status, snapshot.Status.PASSED)

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
        self.assertIn("PromptProfile binding", str(response.data))
        self.assertFalse(ProductionRun.objects.filter(
            idempotency_key="missing-normalize-profile",
        ).exists())

    def _create_and_reach_normalization(self) -> tuple[ProductionRun, GenerationAttempt]:
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

    @staticmethod
    def _runtime_result(attempt: GenerationAttempt) -> dict:
        return {
            "schema_version": 1,
            "request_id": "provider-normalization-id",
            "items": [
                {
                    "request_id": item["request_id"],
                    "ok": True,
                    "translation": "A detective slowly enters the dim old mansion.",
                }
                for item in attempt.request_snapshot["payload"]["items"]
            ],
            "metadata": {},
        }

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
        for index, (scope_type, scope_id, field_key, source, translated) in enumerate(
            specs, start=1,
        ):
            _item, target = prepare_prompt_normalization_item(
                project=self.project,
                scope_type=scope_type,
                scope_id=scope_id,
                field_key=field_key,
                source_text=source,
                label=f"review-{index}",
                request_id=f"review-{index}",
            )
            candidates.append({
                key: value for key, value in target.items()
                if key in {
                    "request_id", "scope_type", "scope_id", "field_key",
                    "source_hash", "context_hash",
                }
            } | {"translated_text": translated})

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
