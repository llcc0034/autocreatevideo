import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    CharacterBible,
    GeneratedArtifact,
    GenerationAttempt,
    GenerationAttemptResolution,
    ModelDeployment,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    PropBible,
    ProviderProfile,
    SceneBible,
    ScriptRevision,
    ScriptSource,
    SeriesProject,
    StageRun,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.production_runs import validate_production_recipe_executability
from studio.services.readiness import _text_generation_worker_required
from studio.services.script_bible_materialization import apply_frozen_script_bible
from studio.services.script_sources import create_pasted_script_source
from studio.services.text_generation_queue import (
    claim_next_text_attempt,
    process_text_attempt,
    provider_payload,
    recover_stale_text_attempts,
)
from studio.services.provider_registry import ProviderDriverError
from studio.services.secret_store import rotate_secret_binding
from studio.tests.secret_fixtures import create_test_secret, enable_test_keyring
from studio.tests.factories import create_test_prompt_profile_binding


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("approved.example", "other.example", "127.0.0.1"))
class ProductionRunAiScriptTests(TestCase):
    def setUp(self):
        enable_test_keyring(self)
        self.user = get_user_model().objects.create_user(
            username="ai-script-owner", password="StrongPass123!",
        )
        self.credential = create_test_secret(
            key="production.run.script",
            actor=self.user,
            value="production-run-test-secret",
        )
        self.project = SeriesProject.objects.create(owner=self.user, title="AI 剧本闭环")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.deployment = self._deployment()
        self.recipe = self._recipe()

    def _deployment(self, *, capability=True, driver_type=ProviderProfile.DriverType.OPENAI_COMPATIBLE):
        provider = ProviderProfile(
            key=f"provider-{ProviderProfile.objects.count() + 1}",
            name="Strict chat provider",
            driver_type=driver_type,
            modalities="text",
            operations="chat_completion,script.generate",
            credential=self.credential,
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "https://approved.example",
            "chat_path": "/frozen/chat/completions",
            "timeout_seconds": 30,
            "pricing": {"currency": "USD", "fixed_per_request": "0.25"},
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key=f"script-model-{ModelProfileVersion.objects.count() + 1}",
            version=1,
            provider=provider,
            model_identifier="strict-script-model",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["script.generate"] if capability else ["text.translate"],
            "series_concept": {
                "minimum_shot_duration_ms": 1000,
                "maximum_shot_duration_ms": 10000,
                "max_output_tokens": 4096,
                "timeout_seconds": 30,
            },
        })
        profile.save()
        deployment = ModelDeployment(
            key=f"script-deployment-{ModelDeployment.objects.count() + 1}",
            name="Strict script deployment",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        if capability:
            create_test_prompt_profile_binding(
                actor=self.user,
                model_profile_version=profile,
                operation="script.generate",
            )
        return deployment

    def _recipe(self, *, deployment=None, status=ProductionRecipeVersion.Status.PUBLISHED):
        deployment = deployment or self.deployment
        recipe = ProductionRecipeVersion(
            recipe_key=f"ai-script-{ProductionRecipeVersion.objects.count() + 1}",
            version=1,
            name="AI script to Bible",
            status=status,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [
                {
                    "key": "script_generate", "mode": "ai", "approval": "auto",
                    "routing": {
                        "mode": "fixed", "variant_count": 1, "evaluator": None,
                        "routes": [{"model_deployment_id": str(deployment.id)}],
                    },
                },
                {"key": "script_review", "mode": "review", "approval": "required"},
                {"key": "bible_materialize", "mode": "deterministic", "approval": "auto"},
                {"key": "bible_review", "mode": "review", "approval": "required"},
            ],
        })
        recipe.save()
        return recipe

    def _ordered_recipe(self, second_deployment):
        recipe = self._recipe(status=ProductionRecipeVersion.Status.PUBLISHED)
        definition = dict(recipe.definition)
        definition["stages"] = list(definition["stages"])
        script_stage = dict(definition["stages"][0])
        script_stage["routing"] = {
            "mode": "ordered_fallback",
            "variant_count": 1,
            "evaluator": None,
            "routes": [
                {"model_deployment_id": str(self.deployment.id)},
                {"model_deployment_id": str(second_deployment.id)},
            ],
        }
        definition["stages"][0] = script_stage
        assign_json_contract(recipe, "definition", definition)
        recipe.save(update_fields=(
            "definition", "definition_schema_version", "definition_size_bytes",
            "definition_sha256", "updated_at",
        ))
        return recipe

    def _payload(self, *, key="ai-run", **overrides):
        payload = {
            "project_id": str(self.project.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": key,
            "idea": "侦探在雨夜老宅发现一封密信。",
            "style_hint": "冷峻悬疑",
            "episode_count": 1,
            "shots_per_episode": 1,
            "episode_duration_seconds": 6,
            "allow_external_calls": True,
            "budget_limit": "1.0000",
        }
        payload.update(overrides)
        return payload

    def test_run_creation_fails_closed_without_published_prompt_profile_binding(self):
        self.deployment.profile_version.prompt_profile_bindings.update(is_active=False)

        response = self.client.post(
            "/api/v1/production-runs/",
            self._payload(key="missing-script-prompt-profile"),
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("PromptProfile binding", str(response.data))
        self.assertFalse(ProductionRun.objects.filter(
            idempotency_key="missing-script-prompt-profile",
        ).exists())

    def test_provider_prompt_freezes_exact_requested_episode_and_shot_shape(self):
        from studio.services.series_concept_drafts import build_series_concept_plan

        plan = build_series_concept_plan(
            idea="一盘磁带预告下一名失踪者。",
            style_hint="民国黑色侦探片",
            episode_count=1,
            shots_per_episode=2,
            episode_duration_seconds=2,
            minimum_shot_duration_ms=1000,
            maximum_shot_duration_ms=1000,
            model_identifier="strict-script-model",
            timeout_seconds=30,
            configured_output_token_hint=4096,
        )

        payload = provider_payload(plan)
        system = payload["messages"][0]["content"]
        contract = json.loads(system.split("CONTRACT=", 1)[1])

        self.assertIn("exactly 1 episode(s)", system)
        self.assertIn("exactly 2 shot(s)", system)
        self.assertIn("MUST equal 1000", system)
        self.assertIn("printable ASCII phrase", system)
        self.assertEqual(len(contract["episodes"]), 1)
        self.assertEqual(len(contract["episodes"][0]["shots"]), 2)
        self.assertEqual(
            [shot["duration_ms"] for shot in contract["episodes"][0]["shots"]],
            [1000, 1000],
        )
        self.assertEqual(
            set(contract["episodes"][0]["shots"][0]),
            {"setting", "action", "duration_ms", "dialogue"},
        )

    @staticmethod
    def _candidate_text(plan):
        return json.dumps({
            "schema_version": 2,
            "title": "雨夜密信",
            "logline": "沈砚在雨夜老宅通过密信追查真相。",
            "bible": {
                "characters": [{
                    "name": "沈砚", "description": "冷静的青年侦探",
                    "visual_identity_en": "calm young detective in a charcoal trench coat",
                }],
                "scenes": [{
                    "name": "雨夜老宅", "description": "暴雨笼罩的旧宅书房",
                    "visual_identity_en": "old mansion study under heavy night rain",
                }],
                "props": [{
                    "name": "密信", "description": "写有失踪线索的旧信",
                    "visual_identity_en": "weathered sealed letter with dark red wax",
                }],
            },
            "episodes": [{
                "title": "来信",
                "shots": [{
                    "setting": "雨夜老宅",
                    "action": "沈砚展开密信并看见失踪者留下的暗号。",
                    "duration_ms": 6000,
                    "dialogue": {"speaker": "沈砚", "delivery": "quietly", "text": "他还活着。"},
                }],
            }],
        }, ensure_ascii=False)

    def _create_and_process(self, *, key="ai-run"):
        created = self.client.post("/api/v1/production-runs/", self._payload(key=key), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        attempt = claim_next_text_attempt("strict-test-worker")
        self.assertIsNotNone(attempt)
        frozen_url = attempt.request_snapshot["route"]["provider_config"]["base_url"]
        frozen_path = attempt.request_snapshot["route"]["provider_config"]["chat_path"]
        frozen_specification = attempt.request_snapshot["route"]["model_specification"]

        provider = self.deployment.profile_version.provider
        assign_json_contract(provider, "driver_config", {
            "base_url": "https://other.example",
            "chat_path": "/changed-after-create",
            "timeout_seconds": 90,
            "pricing": {"currency": "USD", "fixed_per_request": "9"},
        })
        provider.save()
        profile = self.deployment.profile_version
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["script.generate"],
            "series_concept": {
                "minimum_shot_duration_ms": 2000,
                "maximum_shot_duration_ms": 3000,
                "max_output_tokens": 128,
                "timeout_seconds": 1,
            },
        })
        profile.save()

        driver = Mock()
        driver.invoke.side_effect = lambda deployment, operation, payload: {
            "schema_version": 1,
            "request_id": "provider-request-1",
            "text": self._candidate_text(attempt.request_snapshot["plan"]),
        } if (
            deployment.profile_version.provider.driver_config["base_url"] == frozen_url
            and deployment.profile_version.provider.driver_config["chat_path"] == frozen_path
            and deployment.profile_version.specification == frozen_specification
            and operation == "chat_completion"
        ) else self.fail("worker did not consume the frozen provider route")
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(attempt)
        attempt.refresh_from_db()
        self.assertEqual(
            attempt.status, GenerationAttempt.Status.SUCCEEDED,
            (attempt.error_code, attempt.error_message),
        )
        run.refresh_from_db()
        return run, driver

    def test_recipe_publish_requires_capability_and_safe_registered_transport(self):
        stages = validate_production_recipe_executability(self.recipe)
        self.assertTrue(_text_generation_worker_required())
        self.assertEqual(stages[0]["key"], "script_generate")
        self.assertEqual(
            stages[0]["routing"]["routes"][0]["provider_config"]["chat_path"],
            "/frozen/chat/completions",
        )

        missing_capability = self._recipe(deployment=self._deployment(capability=False))
        with self.assertRaisesMessage(ValidationError, "script.generate"):
            validate_production_recipe_executability(missing_capability)

        qwen = self._deployment(driver_type=ProviderProfile.DriverType.LOCAL_QWEN)
        qwen_recipe = self._recipe(deployment=qwen)
        with self.assertRaisesMessage(ValidationError, "受控 chat endpoint"):
            validate_production_recipe_executability(qwen_recipe)

    def test_ai_create_is_mutually_exclusive_idempotent_and_gated_before_invoke(self):
        unauthorized = self.client.post(
            "/api/v1/production-runs/",
            self._payload(key="gate-run", allow_external_calls=False, budget_limit=None),
            format="json",
        )
        self.assertEqual(unauthorized.status_code, 201, unauthorized.data)
        self.assertEqual(unauthorized.data["blocked_code"], "external_call_not_authorized")
        gated_attempt = GenerationAttempt.objects.get(stage_run__production_run_id=unauthorized.data["id"])
        self.assertIsNone(gated_attempt.committed_cost)
        self.assertEqual(gated_attempt.status, GenerationAttempt.Status.QUEUED)
        authorized = self.client.post(
            f"/api/v1/production-runs/{unauthorized.data['id']}/authorize-cost/",
            {
                "stage_key": "script_generate",
                "allow_external_calls": True,
                "budget_limit": "1.0000",
                "currency": "USD",
                "comment": "授权冻结文本 route",
            },
            format="json",
        )
        self.assertEqual(authorized.status_code, 200, authorized.data)
        gated_attempt.refresh_from_db()
        self.assertEqual(str(gated_attempt.committed_cost), "0.250000")
        self.assertEqual(
            GenerationAttempt.objects.filter(stage_run__production_run_id=unauthorized.data["id"]).count(),
            1,
        )

        created = self.client.post("/api/v1/production-runs/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        replay = self.client.post("/api/v1/production-runs/", self._payload(), format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], created.data["id"])
        conflict = self.client.post(
            "/api/v1/production-runs/", self._payload(idea="另一故事"), format="json",
        )
        self.assertEqual(conflict.status_code, 400, conflict.data)

        self.assertFalse(ScriptRevision.objects.filter(source__project=self.project).exists())
        _, preview = create_pasted_script_source(
            project=self.project, user=self.user, content="人工预览剧本",
        )
        bad = self.client.post(
            "/api/v1/production-runs/",
            self._payload(key="mutual", script_revision_id=str(preview.id)),
            format="json",
        )
        self.assertEqual(bad.status_code, 400, bad.data)

    def test_worker_materializes_preview_then_reviews_atomically_publish_script_and_bible(self):
        run, driver = self._create_and_process()
        driver.invoke.assert_called_once()
        revision = ScriptRevision.objects.get(pk=run.script_revision_id)
        artifact = GeneratedArtifact.objects.get(generation_attempt=revision.origin_generation_attempt)
        self.assertEqual(revision.status, ScriptRevision.Status.PREVIEW)
        self.assertEqual(revision.source.source_type, ScriptSource.SourceType.AI_GENERATED)
        self.assertEqual(artifact.generation_attempt_id, revision.origin_generation_attempt_id)
        self.assertEqual(artifact.status, GeneratedArtifact.Status.AVAILABLE)
        self.assertEqual(run.current_stage_key, "script_review")
        self.assertFalse(CharacterBible.objects.filter(project=self.project).exists())

        bypass = self.client.post(f"/api/v1/script-revisions/{revision.id}/confirm/", {}, format="json")
        self.assertEqual(bypass.status_code, 400, bypass.data)
        derived = self.client.post("/api/v1/script-revisions/", {
            "source_id": str(revision.source_id),
            "content": revision.content + "\n人工改写",
        }, format="json")
        self.assertEqual(derived.status_code, 400, derived.data)
        revision.refresh_from_db()
        self.assertEqual(revision.status, ScriptRevision.Status.PREVIEW)

        approved = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_review", "decision": "approve", "comment": "剧本通过",
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        revision.refresh_from_db()
        artifact.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(revision.status, ScriptRevision.Status.CONFIRMED)
        self.assertEqual(artifact.status, GeneratedArtifact.Status.SELECTED)
        self.assertEqual(self.project.master_script, revision.content)
        self.assertEqual(approved.data["current_stage_key"], "bible_review")
        self.assertFalse(CharacterBible.objects.filter(project=self.project).exists())
        self.assertFalse(SceneBible.objects.filter(project=self.project).exists())
        self.assertFalse(PropBible.objects.filter(project=self.project).exists())

        reused_character = CharacterBible.objects.create(
            project=self.project,
            slug="existing-shen-yan",
            name="沈砚",
            stable_speaker_id="S1",
            description="冷静的青年侦探",
            visual_identity="calm young detective in a charcoal trench coat",
            is_locked=False,
        )

        bible = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "bible_review", "decision": "approve", "comment": "Bible 通过",
        }, format="json")
        self.assertEqual(bible.status_code, 200, bible.data)
        self.assertEqual(CharacterBible.objects.filter(project=self.project).count(), 1)
        self.assertEqual(SceneBible.objects.filter(project=self.project).count(), 1)
        self.assertEqual(PropBible.objects.filter(project=self.project).count(), 1)
        reused_character.refresh_from_db()
        self.assertTrue(reused_character.is_locked)

        materialize = StageRun.objects.get(production_run=run, stage_key="bible_materialize")
        frozen = materialize.output_snapshot.get("review_payload", materialize.output_snapshot)
        apply_frozen_script_bible(project=self.project, frozen=frozen)
        self.assertEqual(CharacterBible.objects.filter(project=self.project).count(), 1)
        self.assertEqual(SceneBible.objects.filter(project=self.project).count(), 1)
        self.assertEqual(PropBible.objects.filter(project=self.project).count(), 1)

    def test_submitted_stale_attempt_becomes_uncertain_and_is_never_reclaimed(self):
        created = self.client.post("/api/v1/production-runs/", self._payload(key="stale"), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        attempt = claim_next_text_attempt("worker-that-died")
        self.assertIsNotNone(attempt)
        self.assertIsNone(claim_next_text_attempt("competing-worker"))
        GenerationAttempt.objects.filter(pk=attempt.pk).update(
            status=GenerationAttempt.Status.SUBMITTED,
            heartbeat_at=timezone.now() - timedelta(minutes=31),
        )
        recovered = recover_stale_text_attempts(30)
        self.assertEqual(recovered, {"requeued": 0, "uncertain": 1})
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, GenerationAttempt.Status.FAILED)
        self.assertEqual(attempt.error_code, "uncertain_remote_response")
        self.assertIsNone(claim_next_text_attempt("second-worker"))

        run = attempt.stage_run.production_run
        before = {
            "attempts": GenerationAttempt.objects.filter(stage_run__production_run=run).count(),
            "tasks": run.generation_task_count,
            "committed": run.committed_cost_total,
        }
        retry = self.client.post(f"/api/v1/production-runs/{run.id}/retry/", {}, format="json")
        self.assertEqual(retry.status_code, 409, retry.data)
        run.refresh_from_db()
        self.assertEqual(
            GenerationAttempt.objects.filter(stage_run__production_run=run).count(),
            before["attempts"],
        )
        self.assertEqual(run.generation_task_count, before["tasks"])
        self.assertEqual(run.committed_cost_total, before["committed"])

        unauthorized = self.client.post(
            f"/api/v1/production-runs/{run.id}/resolve-uncertain/",
            {
                "attempt_id": str(attempt.id),
                "outcome": "confirmed_not_submitted",
                "note": "普通项目成员不能提交远端缺失核验。",
            },
            format="json",
        )
        self.assertEqual(unauthorized.status_code, 403, unauthorized.data)

        admin = get_user_model().objects.create_superuser(
            username="uncertain-auditor", password="StrongPass123!", email="audit@example.test",
        )
        admin_client = APIClient()
        admin_client.force_authenticate(admin)
        resolved = admin_client.post(
            f"/api/v1/production-runs/{run.id}/resolve-uncertain/",
            {
                "attempt_id": str(attempt.id),
                "outcome": "confirmed_not_submitted",
                "note": "Provider 控制台与账单均确认没有创建请求。",
            },
            format="json",
        )
        self.assertEqual(resolved.status_code, 200, resolved.data)
        attempt.refresh_from_db()
        self.assertEqual(attempt.error_code, "remote_absence_confirmed")
        resolution = GenerationAttemptResolution.objects.get(
            attempt=attempt,
            outcome=GenerationAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED,
        )
        resolution.note = "试图篡改审计证据"
        with self.assertRaises(ValidationError):
            resolution.save()
        with self.assertRaises(ValidationError):
            resolution.delete()
        retried = admin_client.post(f"/api/v1/production-runs/{run.id}/retry/", {}, format="json")
        self.assertEqual(retried.status_code, 200, retried.data)
        self.assertEqual(
            GenerationAttempt.objects.filter(stage_run__production_run=run).count(),
            before["attempts"] + 1,
        )
        retry_attempt = GenerationAttempt.objects.get(
            stage_run__production_run=run,
            attempt_number=2,
        )
        self.assertEqual(str(retry_attempt.committed_cost), "0.250000")

    def test_cancel_before_text_submit_cancels_attempt_without_artifact(self):
        created = self.client.post(
            "/api/v1/production-runs/", self._payload(key="cancel-before-submit"), format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        attempt = GenerationAttempt.objects.get(stage_run__production_run=run)
        self.assertEqual(attempt.status, GenerationAttempt.Status.QUEUED)
        cancelled = self.client.post(f"/api/v1/production-runs/{run.id}/cancel/", {}, format="json")
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        attempt.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(run.status, ProductionRun.Status.CANCELLED)
        self.assertEqual(attempt.status, GenerationAttempt.Status.CANCELLED)
        self.assertFalse(GeneratedArtifact.objects.filter(production_run=run).exists())
        self.assertFalse(ScriptRevision.objects.filter(source__project=self.project).exists())

    def test_cancel_after_submit_ignores_late_success_and_preserves_terminal_run(self):
        created = self.client.post(
            "/api/v1/production-runs/", self._payload(key="cancel-late-success"), format="json",
        )
        run = ProductionRun.objects.get(pk=created.data["id"])
        attempt = claim_next_text_attempt("late-success-worker")
        driver = Mock()

        def cancel_then_return(*_args):
            response = self.client.post(f"/api/v1/production-runs/{run.id}/cancel/", {}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            return {
                "schema_version": 1,
                "request_id": "late-provider-request",
                "text": self._candidate_text(attempt.request_snapshot["plan"]),
            }

        driver.invoke.side_effect = cancel_then_return
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(attempt)
        run.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(run.status, ProductionRun.Status.CANCELLED)
        self.assertEqual(attempt.status, GenerationAttempt.Status.CANCELLED)
        self.assertEqual(attempt.error_code, "late_result_ignored")
        self.assertEqual(str(attempt.actual_cost), "0.250000")
        self.assertEqual(str(run.actual_cost_total), "0.250000")
        self.assertFalse(GeneratedArtifact.objects.filter(production_run=run).exists())
        self.assertFalse(ScriptRevision.objects.filter(source__project=self.project).exists())

    def test_cancel_after_submit_ignores_late_failure_without_resurrecting_run(self):
        created = self.client.post(
            "/api/v1/production-runs/", self._payload(key="cancel-late-failure"), format="json",
        )
        run = ProductionRun.objects.get(pk=created.data["id"])
        attempt = claim_next_text_attempt("late-failure-worker")
        driver = Mock()

        def cancel_then_fail(*_args):
            response = self.client.post(f"/api/v1/production-runs/{run.id}/cancel/", {}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            raise ProviderDriverError("provider connection ended", code="provider_request_failed")

        driver.invoke.side_effect = cancel_then_fail
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(attempt)
        run.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(run.status, ProductionRun.Status.CANCELLED)
        self.assertEqual(attempt.status, GenerationAttempt.Status.CANCELLED)
        self.assertEqual(attempt.error_code, "late_result_ignored")
        self.assertFalse(GeneratedArtifact.objects.filter(production_run=run).exists())

    def test_invalid_provider_response_records_committed_actual_cost(self):
        created = self.client.post(
            "/api/v1/production-runs/", self._payload(key="invalid-response-cost"), format="json",
        )
        run = ProductionRun.objects.get(pk=created.data["id"])
        attempt = claim_next_text_attempt("invalid-response-worker")
        driver = Mock()
        driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "invalid-response-request",
            "text": "{}",
        }
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(attempt)
        run.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, GenerationAttempt.Status.FAILED)
        self.assertEqual(attempt.error_code, "series_concept_invalid_response")
        self.assertEqual(str(attempt.actual_cost), "0.250000")
        self.assertEqual(str(run.actual_cost_total), "0.250000")
        resolution = GenerationAttemptResolution.objects.get(attempt=attempt)
        self.assertEqual(
            resolution.outcome,
            GenerationAttemptResolution.Outcome.KNOWN_RESPONSE_REJECTED,
        )
        self.assertEqual(resolution.evidence["actual_cost_source"], "committed_cost")

    def test_ordered_text_fallback_uses_new_route_and_exhausts_without_reusing_attempt(self):
        second_deployment = self._deployment()
        self.recipe = self._ordered_recipe(second_deployment)
        created = self.client.post(
            "/api/v1/production-runs/",
            self._payload(key="ordered-text-fallback"),
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        first = claim_next_text_attempt("ordered-text-first")
        driver = Mock()
        driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "known-invalid-first",
            "text": "{}",
        }
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(first)

        first.refresh_from_db()
        run.refresh_from_db()
        second = GenerationAttempt.objects.get(
            stage_run__production_run=run,
            attempt_number=2,
        )
        self.assertEqual(first.route_index, 0)
        self.assertEqual(second.route_index, 1)
        self.assertEqual(second.model_deployment_id, second_deployment.id)
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.request_sha256, second.request_sha256)
        self.assertEqual(run.actual_cost_total, first.actual_cost)
        self.assertEqual(run.committed_cost_total, second.committed_cost)
        events = list(
            first.stage_run.route_events.filter(
                event_type__in=("route_failed", "route_selected"),
            ).order_by("sequence").values_list("event_type", "route_index")
        )
        self.assertIn(("route_failed", 0), events)
        self.assertGreater(events.index(("route_selected", 1)), events.index(("route_failed", 0)))

        claimed_second = claim_next_text_attempt("ordered-text-second")
        self.assertEqual(claimed_second.id, second.id)
        driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "known-invalid-second",
            "text": "{}",
        }
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(claimed_second)
        run.refresh_from_db()
        stage = run.stages.get(stage_key="script_generate")
        self.assertEqual(run.status, ProductionRun.Status.BLOCKED)
        self.assertEqual(stage.reason_code, "routing_candidates_exhausted")
        self.assertEqual(stage.generation_attempts.count(), 2)
        self.assertTrue(stage.route_events.filter(event_type="routes_exhausted").exists())

    def test_ordered_text_uncertain_response_never_falls_back(self):
        second_deployment = self._deployment()
        self.recipe = self._ordered_recipe(second_deployment)
        created = self.client.post(
            "/api/v1/production-runs/",
            self._payload(key="ordered-text-uncertain"),
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        run = ProductionRun.objects.get(pk=created.data["id"])
        first = claim_next_text_attempt("ordered-text-uncertain-worker")
        driver = Mock()
        driver.invoke.side_effect = ProviderDriverError(
            "provider connection ended after request",
            code="provider_request_failed",
            submission_uncertain=True,
        )
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(first)

        first.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(first.error_code, "uncertain_remote_response")
        self.assertEqual(run.status, ProductionRun.Status.BLOCKED)
        self.assertEqual(
            GenerationAttempt.objects.filter(stage_run__production_run=run).count(),
            1,
        )
        retry = self.client.post(
            f"/api/v1/production-runs/{run.id}/retry/", {}, format="json",
        )
        self.assertEqual(retry.status_code, 409, retry.data)
        self.assertEqual(
            GenerationAttempt.objects.filter(stage_run__production_run=run).count(),
            1,
        )

    def test_pre_submit_route_drift_fails_one_attempt_without_invoking_provider(self):
        created = self.client.post(
            "/api/v1/production-runs/", self._payload(key="pre-submit-drift"), format="json",
        )
        run = ProductionRun.objects.get(pk=created.data["id"])
        attempt = claim_next_text_attempt("drift-worker")
        rotate_secret_binding(
            self.credential,
            secret_value="rotated-after-run",
            actor=self.user,
            reason="test_route_drift",
        )
        driver = Mock()
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            result = process_text_attempt(attempt)
        run.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual(result.status, GenerationAttempt.Status.FAILED)
        self.assertNotEqual(result.error_code, "uncertain_remote_response")
        self.assertEqual(run.status, ProductionRun.Status.BLOCKED)
        driver.invoke.assert_not_called()

    def test_worker_isolates_unhandled_post_submit_failure_to_claimed_attempt(self):
        first = self.client.post(
            "/api/v1/production-runs/", self._payload(key="worker-isolation-first"), format="json",
        )
        second = self.client.post(
            "/api/v1/production-runs/", self._payload(key="worker-isolation-second"), format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        first_attempt = GenerationAttempt.objects.get(
            stage_run__production_run_id=first.data["id"],
        )
        second_attempt = GenerationAttempt.objects.get(
            stage_run__production_run_id=second.data["id"],
        )

        def crash_after_submit(claimed):
            GenerationAttempt.objects.filter(pk=claimed.pk).update(
                status=GenerationAttempt.Status.SUBMITTED,
            )
            raise RuntimeError("unexpected worker boundary failure")

        with patch(
            "studio.management.commands.run_text_generation_worker.process_text_attempt",
            side_effect=crash_after_submit,
        ):
            call_command(
                "run_text_generation_worker",
                once=True,
                recover_minutes=30,
                startup_recover_minutes=30,
                recover_interval=60,
                poll=0.1,
            )
        first_attempt.refresh_from_db()
        second_attempt.refresh_from_db()
        self.assertEqual(first_attempt.status, GenerationAttempt.Status.FAILED)
        self.assertEqual(first_attempt.error_code, "uncertain_remote_response")
        self.assertEqual(second_attempt.status, GenerationAttempt.Status.QUEUED)

    def test_script_review_cannot_overwrite_a_concurrently_changed_master_script(self):
        run, _ = self._create_and_process(key="master-script-cas")
        revision = ScriptRevision.objects.get(pk=run.script_revision_id)
        self.project.master_script = "另一条生产链已经确认的母稿"
        self.project.save(update_fields=("master_script", "updated_at"))
        response = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_review", "decision": "approve", "comment": "尝试覆盖",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.project.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(self.project.master_script, "另一条生产链已经确认的母稿")
        self.assertEqual(revision.status, ScriptRevision.Status.PREVIEW)

    def test_script_review_reject_keeps_preview_and_rejects_artifact(self):
        run, _ = self._create_and_process(key="script-reject")
        revision = ScriptRevision.objects.get(pk=run.script_revision_id)
        artifact = GeneratedArtifact.objects.get(generation_attempt=revision.origin_generation_attempt)
        response = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_review", "decision": "reject", "comment": "需要重写",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        revision.refresh_from_db()
        artifact.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(revision.status, ScriptRevision.Status.PREVIEW)
        self.assertEqual(artifact.status, GeneratedArtifact.Status.REJECTED)
        self.assertEqual(self.project.master_script, "")

    def test_bible_conflict_rolls_back_entire_batch(self):
        run, _ = self._create_and_process(key="conflict")
        self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_review", "decision": "approve", "comment": "剧本通过",
        }, format="json")
        CharacterBible.objects.create(
            project=self.project, slug="conflicting-lead", name="沈砚",
            stable_speaker_id="S99", description="与候选冲突的既有定义",
        )
        response = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "bible_review", "decision": "approve", "comment": "尝试发布",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(CharacterBible.objects.filter(project=self.project).count(), 1)
        self.assertFalse(SceneBible.objects.filter(project=self.project).exists())
        self.assertFalse(PropBible.objects.filter(project=self.project).exists())
