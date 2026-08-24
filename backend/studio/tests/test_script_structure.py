import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ApprovalDecision,
    GeneratedArtifact,
    GenerationAttempt,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    ProviderProfile,
    ScriptRevision,
    ScriptSource,
    SeriesProject,
    StageRun,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.deployment_smoke import record_deployment_capability_snapshot
from studio.services.deployment_smoke_queue import (
    claim_next_deployment_smoke,
    process_deployment_smoke,
)
from studio.services.script_structure import (
    ScriptStructureError,
    build_script_structure_plan,
    canonical_no_submission_result,
    finalize_script_structure,
    is_canonical_script,
    parse_canonical_script,
    structure_provider_payload,
)
from studio.services.series_concept_drafts import render_compatible_master_script
from studio.services.script_sources import (
    confirm_script_revision,
    create_pasted_script_source,
)
from studio.services.text_generation_queue import (
    claim_next_text_attempt,
    process_text_attempt,
)
from studio.tests.factories import create_test_prompt_profile_binding


class ScriptStructureContractTests(SimpleTestCase):
    def setUp(self):
        self.bible = {
            "characters": [{
                "name": "林雾",
                "description": "追查失踪案的记者",
                "visual_identity_en": "Chinese investigative reporter in a dark raincoat",
            }],
            "scenes": [{
                "name": "雨夜旅馆",
                "description": "停电后的旧旅馆走廊",
                "visual_identity_en": "old hotel corridor during a rainy blackout",
            }],
            "props": [],
        }
        self.episodes = [{
            "title": "回声",
            "shots": [
                {
                    "setting": "雨夜旅馆",
                    "action": "林雾推开房门，旧磁带开始自行转动。",
                    "duration_ms": 2_000,
                    "dialogue": {"speaker": "", "delivery": "", "text": ""},
                },
                {
                    "setting": "雨夜旅馆",
                    "action": "林雾拿起磁带。",
                    "duration_ms": 2_000,
                    "dialogue": {
                        "speaker": "林雾",
                        "delivery": "quietly",
                        "text": "谁在那里？",
                    },
                },
            ],
        }]
        self.canonical = render_compatible_master_script(self.bible, self.episodes)

    def _revision(self, content: str):
        encoded = content.encode("utf-8")
        import hashlib

        return SimpleNamespace(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            revision=1,
            content=content,
            content_size_bytes=len(encoded),
            content_sha256=hashlib.sha256(encoded).hexdigest(),
            status=ScriptRevision.Status.CONFIRMED,
        )

    def _plan(self, content: str):
        return build_script_structure_plan(
            revision=self._revision(content),
            model_identifier="strict-structure-model",
            policy={
                "minimum_shot_duration_ms": 1_000,
                "maximum_shot_duration_ms": 10_000,
                "max_input_bytes": 64_000,
                "max_output_tokens": 8_192,
                "timeout_seconds": 30,
            },
        )

    def test_exact_renderer_output_is_the_only_deterministic_no_submission_form(self):
        parsed = parse_canonical_script(self.canonical)

        self.assertTrue(is_canonical_script(self.canonical))
        self.assertEqual(parsed["master_script"], self.canonical)
        self.assertEqual(len(parsed["episodes"][0]["shots"]), 2)
        self.assertFalse(is_canonical_script(self.canonical.replace("【尾帧连续=是】", "【尾帧连续=否】", 1)))
        self.assertFalse(is_canonical_script(self.canonical + "\n"))
        self.assertFalse(is_canonical_script("\ufeff" + self.canonical))

        revision = self._revision(self.canonical)
        no_op = canonical_no_submission_result(
            revision,
            title="雾港回声",
            logline="记者在雨夜旅馆追查一盘会预告失踪的磁带。",
        )
        self.assertEqual(no_op["source_revision"]["id"], str(revision.id))
        self.assertEqual(no_op["candidate"]["master_script"], self.canonical)
        self.assertEqual(
            no_op["evidence"]["submission_policy"],
            "canonical_input_no_submission",
        )

    def test_full_source_is_frozen_in_provider_payload_without_slicing(self):
        source = "第一幕\n林雾进入旅馆。\n结尾标记-不可丢失"
        plan = self._plan(source)

        payload = structure_provider_payload(plan)
        user = json.loads(payload["messages"][1]["content"])

        self.assertEqual(user["source_text"], source)
        self.assertTrue(user["source_text"].endswith("结尾标记-不可丢失"))
        self.assertEqual(user["non_empty_source_line_numbers"], [1, 2, 3])
        self.assertEqual(
            plan["resource_estimate"]["submission_policy"],
            "full_source_or_fail_no_truncation",
        )

    def test_strict_ir_requires_exact_source_identity_and_every_non_empty_line_once(self):
        source = "第一幕\n林雾进入旅馆。"
        plan = self._plan(source)
        candidate = {
            "schema_version": 1,
            "source_revision_id": plan["source_revision"]["id"],
            "source_sha256": plan["source_revision"]["content_sha256"],
            "source_size_bytes": plan["source_revision"]["content_size_bytes"],
            "title": "雾港回声",
            "logline": "林雾在旧旅馆追查失踪案。",
            "bible": self.bible,
            "episodes": [{
                "title": "第一幕",
                "shots": [{
                    "source_lines": [1, 2],
                    "setting": "雨夜旅馆",
                    "action": "林雾进入旅馆。",
                    "duration_ms": 2_000,
                    "dialogue": {"speaker": "", "delivery": "", "text": ""},
                }],
            }],
        }

        result = finalize_script_structure(plan=plan, payload=candidate)
        self.assertEqual(result["evidence"]["covered_non_empty_line_count"], 2)
        self.assertTrue(is_canonical_script(result["candidate"]["master_script"]))

        missing = {**candidate, "episodes": [{
            **candidate["episodes"][0],
            "shots": [{**candidate["episodes"][0]["shots"][0], "source_lines": [1]}],
        }]}
        with self.assertRaises(ScriptStructureError) as raised:
            finalize_script_structure(plan=plan, payload=missing)
        self.assertEqual(raised.exception.code, "script_structure_source_coverage_invalid")

        duplicated = {**candidate, "episodes": [{
            **candidate["episodes"][0],
            "shots": [{**candidate["episodes"][0]["shots"][0], "source_lines": [1, 1, 2]}],
        }]}
        with self.assertRaises(ScriptStructureError) as raised:
            finalize_script_structure(plan=plan, payload=duplicated)
        self.assertEqual(raised.exception.code, "script_structure_source_coverage_invalid")

        wrong_source = {**candidate, "source_sha256": "0" * 64}
        with self.assertRaises(ScriptStructureError) as raised:
            finalize_script_structure(plan=plan, payload=wrong_source)
        self.assertEqual(raised.exception.code, "script_structure_source_mismatch")

    def test_input_larger_than_profile_limit_blocks_instead_of_truncating(self):
        revision = self._revision("完整剧本不可截断")
        with self.assertRaises(ScriptStructureError) as raised:
            build_script_structure_plan(
                revision=revision,
                model_identifier="strict-structure-model",
                policy={
                    "minimum_shot_duration_ms": 1_000,
                    "maximum_shot_duration_ms": 10_000,
                    "max_input_bytes": 3,
                    "max_output_tokens": 8_192,
                    "timeout_seconds": 30,
                },
            )
        self.assertEqual(raised.exception.code, "script_structure_input_too_large")


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ScriptStructureProductionRunTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="structure-owner",
            password="StrongPass123!",
            is_staff=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        provider = ProviderProfile(
            key="structure-provider",
            name="Structure provider",
            driver_type=ProviderProfile.DriverType.OPENAI_COMPATIBLE,
            modalities="text",
            operations="chat_completion,script.structure",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "http://127.0.0.1:9901",
            "chat_path": "/v1/chat/completions",
            "timeout_seconds": 30,
            "pricing": {"currency": "USD", "fixed_per_request": "0.01"},
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key="strict-structure-model",
            version=1,
            provider=provider,
            model_identifier="strict-structure-model",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["script.structure"],
            "script_structure": {
                "minimum_shot_duration_ms": 1_000,
                "maximum_shot_duration_ms": 10_000,
                "max_input_bytes": 256_000,
                "max_output_tokens": 8_192,
                "timeout_seconds": 30,
            },
        })
        profile.save()
        self.deployment = ModelDeployment(
            key="strict-structure-deployment",
            name="Strict structure deployment",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(self.deployment, "runtime_config", {})
        self.deployment.save()
        create_test_prompt_profile_binding(
            actor=self.user,
            model_profile_version=profile,
            operation="script.structure",
        )
        self.structure_smoke = record_deployment_capability_snapshot(
            self.deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["script.structure"],
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "request_sha256": "1" * 64,
                "response_sha256": "2" * 64,
                "result_sha256": "3" * 64,
            },
        )
        self.recipe = ProductionRecipeVersion(
            recipe_key="import-and-structure",
            version=1,
            name="Import and structure",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(self.recipe, "definition", {
            "schema_version": 1,
            "stages": [
                {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
                {
                    "key": "script_structure",
                    "mode": "ai",
                    "approval": "auto",
                    "routing": {
                        "mode": "fixed",
                        "variant_count": 1,
                        "evaluator": None,
                        "routes": [{"model_deployment_id": str(self.deployment.id)}],
                    },
                },
                {"key": "script_review", "mode": "review", "approval": "required"},
            ],
        })
        self.recipe.save()

    def _project_revision(self, content: str, *, suffix: str = ""):
        project = SeriesProject.objects.create(
            owner=self.user,
            title=f"人工项目{suffix}",
            logline=f"人工梗概{suffix}",
        )
        _source, revision = create_pasted_script_source(
            project=project,
            user=self.user,
            content=content,
        )
        confirm_script_revision(revision, user=self.user)
        revision.refresh_from_db()
        project.master_script = revision.content
        project.save(update_fields=("master_script", "updated_at"))
        return project, revision

    def _create_run(self, project, revision, *, key: str):
        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(project.id),
            "script_revision_id": str(revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": key,
            "allow_external_calls": False,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return ProductionRun.objects.get(pk=response.data["id"])

    def test_run_creation_fails_closed_without_script_structure_profile_binding(self):
        project, revision = self._project_revision(
            "第一集\n记者进入雨夜旅馆，寻找失踪者留下的线索。",
            suffix="-missing-profile",
        )
        self.deployment.profile_version.prompt_profile_bindings.update(is_active=False)

        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(project.id),
            "script_revision_id": str(revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "structure-missing-profile",
            "allow_external_calls": False,
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("PromptProfile binding", str(response.data))
        self.assertFalse(ProductionRun.objects.filter(
            idempotency_key="structure-missing-profile",
        ).exists())

    @staticmethod
    def _response_for_attempt(attempt):
        plan = attempt.request_snapshot["plan"]
        return json.dumps({
            "schema_version": 1,
            "source_revision_id": plan["source_revision"]["id"],
            "source_sha256": plan["source_revision"]["content_sha256"],
            "source_size_bytes": plan["source_revision"]["content_size_bytes"],
            "title": "AI 冻结标题",
            "logline": "AI 冻结梗概",
            "bible": {"characters": [], "scenes": [], "props": []},
            "episodes": [{
                "title": "第一集",
                "shots": [{
                    "source_lines": plan["request"]["non_empty_source_line_numbers"],
                    "setting": "雨夜旅馆",
                    "action": "林雾进入旅馆，发现自行转动的磁带。",
                    "duration_ms": 2_000,
                    "dialogue": {"speaker": "", "delivery": "", "text": ""},
                }],
            }],
        }, ensure_ascii=False)

    def test_noncanonical_import_persists_attempt_preview_and_adopts_only_frozen_metadata(self):
        project, original = self._project_revision(
            "林雾进入雨夜旅馆。\n她发现一盘自行转动的磁带。",
        )
        run = self._create_run(project, original, key="structure-success")
        self.assertEqual(run.current_stage_key, "script_structure")
        self.assertEqual(run.input_snapshot["project_base_title"], project.title)
        self.assertEqual(run.input_snapshot["project_base_logline"], project.logline)

        attempt = claim_next_text_attempt("structure-worker")
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.operation, "script.structure")
        driver = Mock()
        driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "structure-request-1",
            "text": self._response_for_attempt(attempt),
        }
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(attempt)

        run.refresh_from_db()
        original.refresh_from_db()
        preview = ScriptRevision.objects.get(pk=run.script_revision_id)
        self.assertEqual(original.status, ScriptRevision.Status.CONFIRMED)
        self.assertEqual(preview.status, ScriptRevision.Status.PREVIEW)
        self.assertNotEqual(preview.id, original.id)
        self.assertEqual(preview.origin_generation_attempt_id, attempt.id)
        self.assertEqual(run.current_stage_key, "script_review")
        self.assertEqual(run.status, ProductionRun.Status.AWAITING_APPROVAL)

        approved = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_review",
            "decision": "approve",
            "comment": "结构化候选与原稿逐项核对通过",
            "adopt_generated_metadata": True,
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        preview.refresh_from_db()
        original.refresh_from_db()
        project.refresh_from_db()
        self.assertEqual(preview.status, ScriptRevision.Status.CONFIRMED)
        self.assertEqual(original.status, ScriptRevision.Status.CONFIRMED)
        self.assertEqual(project.master_script, preview.content)
        self.assertEqual(project.title, "AI 冻结标题")
        self.assertEqual(project.logline, "AI 冻结梗概")
        decision = ApprovalDecision.objects.get(stage_run__production_run=run)
        self.assertTrue(decision.output_snapshot_sha256)
        review = StageRun.objects.get(production_run=run, stage_key="script_review")
        evidence = review.output_snapshot["review_payload"]
        self.assertEqual(evidence["metadata_adoption"]["title"]["status"], "adopted")
        self.assertEqual(evidence["metadata_adoption"]["logline"]["status"], "adopted")

    def test_concurrent_project_metadata_edits_are_preserved_and_audited(self):
        project, original = self._project_revision(
            "林雾进入雨夜旅馆。\n她听见磁带中的求救声。",
            suffix="-并发",
        )
        run = self._create_run(project, original, key="structure-concurrent")
        attempt = claim_next_text_attempt("structure-worker-concurrent")
        driver = Mock()
        driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "structure-request-2",
            "text": self._response_for_attempt(attempt),
        }
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(attempt)
        project.title = "人工并发标题"
        project.logline = "人工并发梗概"
        project.save(update_fields=("title", "logline", "updated_at"))

        approved = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_review",
            "decision": "approve",
            "adopt_generated_metadata": True,
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        project.refresh_from_db()
        self.assertEqual(project.title, "人工并发标题")
        self.assertEqual(project.logline, "人工并发梗概")
        evidence = StageRun.objects.get(
            production_run=run,
            stage_key="script_review",
        ).output_snapshot["review_payload"]
        self.assertEqual(evidence["metadata_adoption"]["title"]["status"], "not_adopted")
        self.assertEqual(evidence["metadata_adoption"]["title"]["reason"], "concurrent_edit")
        self.assertEqual(evidence["metadata_adoption"]["logline"]["status"], "not_adopted")
        self.assertEqual(evidence["metadata_adoption"]["logline"]["reason"], "concurrent_edit")

    def test_canonical_import_creates_review_snapshot_without_generation_attempt(self):
        # Exact canonical input is intentionally executable without a model
        # route or smoke; only non-canonical text crosses the submission gate.
        self.deployment.capability_snapshots.all().delete()
        definition = dict(self.recipe.definition)
        definition["stages"] = [
            {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
            {"key": "script_structure", "mode": "ai", "approval": "auto"},
            {"key": "script_review", "mode": "review", "approval": "required"},
        ]
        assign_json_contract(self.recipe, "definition", definition)
        self.recipe.save(update_fields=(
            "definition", "definition_schema_version", "definition_size_bytes",
            "definition_sha256", "updated_at",
        ))
        canonical = render_compatible_master_script(
            {"characters": [], "scenes": [], "props": []},
            [{
                "title": "已规范",
                "shots": [{
                    "setting": "空镜走廊",
                    "action": "灯光逐盏熄灭。",
                    "duration_ms": 2_000,
                    "dialogue": {"speaker": "", "delivery": "", "text": ""},
                }],
            }],
        )
        project, original = self._project_revision(canonical, suffix="-规范")
        run = self._create_run(project, original, key="structure-no-op")

        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=run).exists())
        self.assertEqual(run.script_revision_id, original.id)
        self.assertEqual(run.current_stage_key, "script_review")
        stage = StageRun.objects.get(production_run=run, stage_key="script_structure")
        self.assertEqual(
            stage.output_snapshot["review_payload"]["submission_semantics"],
            "no_submission",
        )
        self.assertEqual(stage.output_snapshot["review_payload"]["script_revision_id"], str(original.id))
        self.assertEqual(
            GeneratedArtifact.objects.filter(
                production_run=run,
                kind=GeneratedArtifact.Kind.SCRIPT,
            ).count(),
            1,
        )

    def test_dedicated_structure_smoke_is_api_configurable_and_hash_only(self):
        self.deployment.capability_snapshots.all().delete()
        driver = Mock()
        driver.estimate.return_value = {
            "schema_version": 1,
            "amount": "0.01",
            "currency": "USD",
            "basis": "fixed_per_request",
        }

        def invoke(_deployment, operation, payload):
            self.assertEqual(operation, "chat_completion")
            request_body = json.loads(payload["messages"][1]["content"])
            source = request_body["source_revision"]
            return {
                "schema_version": 1,
                "request_id": "structure-smoke-request-plaintext",
                "text": json.dumps({
                    "schema_version": 1,
                    "source_revision_id": source["id"],
                    "source_sha256": source["content_sha256"],
                    "source_size_bytes": source["content_size_bytes"],
                    "title": "灯塔来信",
                    "logline": "旅人在灯塔中发现一封旧信。",
                    "bible": {"characters": [], "scenes": [], "props": []},
                    "episodes": [{
                        "title": "来信",
                        "shots": [{
                            "source_lines": request_body["non_empty_source_line_numbers"],
                            "setting": "风雨灯塔",
                            "action": "旅人进入灯塔并发现旧信。",
                            "duration_ms": 1_000,
                            "dialogue": {"speaker": "", "delivery": "", "text": ""},
                        }],
                    }],
                }, ensure_ascii=False),
            }

        driver.invoke.side_effect = invoke
        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/smoke-test/",
                {"operation": "script.structure"},
                format="json",
            )
            attempt = claim_next_deployment_smoke("structure-smoke-test-worker")
            finished = process_deployment_smoke(attempt)

        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(finished.status, ModelDeploymentSmokeAttempt.Status.SUCCEEDED)
        snapshot = finished.final_snapshot
        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.PASSED)
        self.assertEqual(snapshot.operations, ["script.structure"])
        persisted = json.dumps(snapshot.evidence, ensure_ascii=False)
        self.assertNotIn("旅人在灯塔", persisted)
        self.assertNotIn("structure-smoke-request-plaintext", persisted)
        self.assertEqual(len(snapshot.evidence["result_sha256"]), 64)

    def test_script_generate_smoke_does_not_authorize_structure_submission(self):
        self.deployment.capability_snapshots.all().delete()
        record_deployment_capability_snapshot(
            self.deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["script.generate"],
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "request_sha256": "4" * 64,
                "response_sha256": "5" * 64,
                "result_sha256": "6" * 64,
            },
        )
        project, original = self._project_revision(
            "旅人进入灯塔。\n旅人发现一封旧信。",
            suffix="-错误烟测",
        )

        run = self._create_run(project, original, key="structure-wrong-smoke")

        self.assertFalse(GenerationAttempt.objects.filter(
            stage_run__production_run=run,
            operation="script.structure",
        ).exists())
        stage = StageRun.objects.get(production_run=run, stage_key="script_structure")
        self.assertEqual(stage.reason_code, "script_structure_smoke_missing_or_stale")

    def test_model_profile_publish_rejects_incomplete_structure_contract(self):
        draft = ModelProfileVersion(
            profile_key="incomplete-structure-model",
            version=1,
            provider=self.deployment.profile_version.provider,
            model_identifier="incomplete-structure-model",
            modality=ModelProfileVersion.Modality.TEXT,
        )
        assign_json_contract(draft, "specification", {
            "schema_version": 1,
            "capabilities": ["script.structure"],
            "script_structure": {
                "minimum_shot_duration_ms": 1_000,
                "maximum_shot_duration_ms": 10_000,
                # max_input_bytes is intentionally absent.
                "max_output_tokens": 8_192,
                "timeout_seconds": 30,
            },
        })
        draft.save()

        response = self.client.post(
            f"/api/v1/model-profile-versions/{draft.id}/publish/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("script_structure policy", str(response.data))
        draft.refresh_from_db()
        self.assertEqual(draft.status, ModelProfileVersion.Status.DRAFT)

    def test_worker_rechecks_structure_smoke_at_submission_boundary(self):
        project, original = self._project_revision(
            "旅人进入灯塔。\n旅人发现一封旧信。",
            suffix="-烟测过期",
        )
        run = self._create_run(project, original, key="structure-smoke-expired")
        attempt = claim_next_text_attempt("structure-expired-worker")
        self.assertIsNotNone(attempt)
        self.structure_smoke.expires_at = timezone.now() - timedelta(seconds=1)
        self.structure_smoke.save(update_fields=("expires_at", "updated_at"))
        driver = Mock()

        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(attempt)

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, GenerationAttempt.Status.FAILED)
        self.assertEqual(
            attempt.error_code,
            "script_structure_smoke_missing_or_stale",
        )
        driver.invoke.assert_not_called()
        self.assertEqual(run.artifacts.filter(role="structured_script").count(), 0)
