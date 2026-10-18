import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    GeneratedArtifact,
    GenerationAttempt,
    ModelDeploymentSmokeAttempt,
    ModelProfileVersion,
    ProductionRun,
    ScriptRevision,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.script_sources import (
    confirm_script_revision,
    create_pasted_script_source,
)
from studio.tests import test_production_recipe_content_chain as content_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ScriptStructureProductionRunTests(TestCase):
    """Structure constraints on exact instances, not retired Binding/routes."""
    setUpTestData = classmethod(content_fixtures.RecipeContentChainTests.setUpTestData.__func__)
    setUp = content_fixtures.RecipeContentChainTests.setUp
    profile_instance = content_fixtures.RecipeContentChainTests.profile_instance
    root = content_fixtures.RecipeContentChainTests.root
    receipt = content_fixtures.RecipeContentChainTests.receipt
    create_root = content_fixtures.RecipeContentChainTests.create_root
    structure_instance = content_fixtures.RecipeContentChainTests.structure_instance
    pass_smoke = content_fixtures.RecipeContentChainTests.pass_smoke
    advance = content_fixtures.RecipeContentChainTests.advance
    process = content_fixtures.RecipeContentChainTests.process
    content_stages = content_fixtures.RecipeContentChainTests.content_stages

    def response(self):
        return {"text": json.dumps(self.candidate, ensure_ascii=False),
            "request_id": "isolated-structure-request"}

    def structure_root(self, *, content="旅人来到房间。\n旅人放下盒子。", duration=1):
        self.structure_instance(scopes=("project",), planning=True)
        self.profile_instance()
        _, preview = create_pasted_script_source(project=self.project, user=self.actor, content=content)
        self.revision = confirm_script_revision(preview, user=self.actor)
        self.counts = {"episode_count": 1, "shots_per_episode": 1, "episode_duration_seconds": duration}
        self.values = {"idea": "", "style_hint": "", **self.counts}
        self.candidate["episodes"][0]["shots"][0]["duration_ms"] = duration * 1000
        self.fixture["inputs"]["plan"] = {"kind": "literal", "value": deepcopy(self.counts)}
        source = {"script": {"kind": "context_field", "selector_key": "script.content", "scope": {"kind": "project"}},
            "plan": {"kind": "structured", "template": {"kind": "object", "fields": {
                key: {"kind": "context_field", "selector_key": f"run.{key}", "scope": {"kind": "project"}}
                for key in self.counts}}}}
        self.run = self.root(stage_key="script_structure", source=source, variants=1,
            prior=[{"key": "script_input", "mode": "confirmed_revision", "approval": "auto"}],
            following=self.content_stages(),
            initial={"kind": "script_revision", "id": str(self.revision.pk), "expected_sha256": self.revision.content_sha256})

    def start_root(self, *, authorized=False, **changes):
        from studio.services.production_runs import advance_production_run
        self.run, created = self.create_root(script_revision=self.revision,
            allow_external_calls=authorized, **changes)
        self.assertTrue(created)
        self.run = advance_production_run(self.run)
        return self.run

    def test_explicit_one_second_plan_is_frozen_without_global_duration_overrides(self):
        from studio.models import ExecutionPreparation
        from studio.services.execution_preparation_requests import compile_prepared_execution_transport
        self.structure_root()
        self.start_root()
        prep = ExecutionPreparation.objects.get(stage_run__production_run=self.run)
        self.assertEqual(prep.contract.resolved_inputs["inputs"]["operation_inputs"],
            {"source_text": self.revision.content, "planning": self.counts})
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor).transport.value
        plan = transport["operation_plan"]["plan"]
        self.assertEqual(plan["inputs"]["planning"], self.counts)
        self.assertEqual(plan["source_revision"]["id"], str(self.revision.pk))
        self.assertNotIn("structured_shot_duration_contract", self.run.input_snapshot)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_free_text_timing_and_planning_are_retained_without_legacy_regex_rewrite(self):
        from studio.models import ExecutionPreparation
        from studio.services.execution_preparation_requests import compile_prepared_execution_transport

        self.structure_root(content="镜头1 [45~50秒]：旅人走进房间。", duration=1)
        self.start_root()
        prep = ExecutionPreparation.objects.get(stage_run__production_run=self.run)
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor).transport.value
        plan = transport["operation_plan"]["plan"]
        self.assertEqual(plan["inputs"], {"source_text": self.revision.content, "planning": self.counts})
        wire = json.loads(transport["payload"]["messages"][1]["content"])
        self.assertEqual(wire["inputs"], plan["inputs"])
        self.assertNotIn("authored_shot_timings", plan)
        self.assertNotIn("source_lines", wire)
        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(GeneratedArtifact.objects.filter(stage_run__production_run=self.run,
            generation_attempt__isnull=False).exists())

    def test_partial_planning_is_not_completed_from_project_defaults(self):
        from studio.models import ExecutionPreparation
        self.structure_root()
        before = (ProductionRun.objects.count(), GenerationAttempt.objects.count(), ExecutionPreparation.objects.count())
        with self.assertRaisesMessage(ValidationError, "集数、镜数和时长必须完整提供"):
            self.start_root(episode_duration_seconds=None)
        self.assertEqual(before, (ProductionRun.objects.count(), GenerationAttempt.objects.count(), ExecutionPreparation.objects.count()))

    def test_smoke_for_script_generate_does_not_authorize_structure_instance(self):
        from studio.models import ExecutionPreparation
        self.pass_smoke()
        self.structure_root()
        self.start_root(authorized=True)
        prep = ExecutionPreparation.objects.get(stage_run__production_run=self.run)
        self.assertEqual(prep.reason_code, "execution_smoke_missing", prep.reason)
        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=self.run).exists())

    def test_original_structure_worker_keeps_candidate_unadopted_until_review(self):
        from studio.services.production_runs import advance_production_run, decide_stage
        self.structure_root(duration=5)
        self.pass_smoke()
        self.start_root(authorized=True)
        self.assertEqual(self.run.blocked_code, "waiting_external_task", self.run.blocked_reason)
        attempt = GenerationAttempt.objects.get(stage_run__production_run=self.run)
        frozen = deepcopy(attempt.request_snapshot)
        result, peer = self.process()
        self.assertEqual(result.status, "succeeded", result.error_code)
        peer.invoke.assert_called_once()
        self.run = advance_production_run(self.run)
        self.assertEqual(self.run.script_revision_id, self.revision.pk)
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        preview = ScriptRevision.objects.get(pk=artifact.manifest_metadata["script_revision_id"])
        self.assertEqual(preview.status, "preview")
        self.project.title, self.project.logline = "人工新标题", "人工新梗概"
        self.project.save(update_fields=("title", "logline"))
        stage = self.run.stages.get(stage_key="script_structure")
        self.run = decide_stage(self.run, stage_key="script_review", decision="approve", comment="已核对",
            user=self.actor, text_candidate={"artifact_id": str(artifact.pk), "expected_sha256": artifact.sha256},
            expected_source_snapshot_sha256=stage.output_snapshot_sha256)
        self.assertEqual(self.run.script_revision_id, preview.pk)
        self.project.refresh_from_db()
        self.assertEqual((self.project.title, self.project.logline), ("人工新标题", "人工新梗概"))
        review = self.run.stages.get(stage_key="script_review")
        self.assertNotIn("metadata_adoption", review.output_snapshot["review_payload"])
        attempt.refresh_from_db()
        self.assertEqual(attempt.request_snapshot, frozen)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.content, "旅人来到房间。\n旅人放下盒子。")

    def test_worker_rechecks_configured_smoke_freshness_without_changing_frozen_request(self):
        from studio.services.configuration import create_config_revision, publish_config_revision
        self.structure_root(duration=5)
        revision = create_config_revision(scope_type="system",
            values={"execution.admission.smoke_policy": "require_fresh"}, created_by=self.actor,
            change_summary="Isolated worker freshness test")
        publish_config_revision(revision, published_by=self.actor)
        self.pass_smoke()
        self.start_root(authorized=True)
        attempt = GenerationAttempt.objects.get(stage_run__production_run=self.run)
        frozen = deepcopy(attempt.request_snapshot)
        done = ModelDeploymentSmokeAttempt.objects.get(scope="instance", execution_instance_version=self.version)
        clock = SimpleNamespace(now=lambda: done.evidence_expires_at + timedelta(seconds=1))
        with patch("studio.services.execution_finalization.timezone", clock):
            outcome, peer = self.process()
        self.assertEqual((outcome.status, outcome.error_code), ("queued", "execution_smoke_expired"))
        peer.invoke.assert_not_called()
        self.assertIsNone(outcome.submission_started_at)
        self.assertEqual(outcome.request_snapshot, frozen)
        self.assertFalse(GeneratedArtifact.objects.filter(generation_attempt=attempt).exists())

    def test_old_canonical_text_without_saved_mapping_is_not_bible_input(self):
        from studio.services.production_runs import advance_production_run
        # Exact old text is merely text, never implicit structured evidence.
        canonical = ("【SCRIPT_BIBLE_V1】\n【/SCRIPT_BIBLE_V1】\n\n第1集：旧稿\n"
            "镜头1【时长=5秒】【首帧连续=否】【尾帧连续=否】：客栈\n[0-5秒] 动作：旅人进门。")
        self.structure_root(content=canonical, duration=5)
        recipe = self.run.recipe_version
        definition = deepcopy(recipe.definition)
        definition["stages"] = [row for row in definition["stages"]
            if row["key"] not in {"script_structure", "script_review"}]
        assign_json_contract(recipe, "definition", definition, schema_version=3)
        recipe.save()
        self.values = {"idea": "", "style_hint": "", "episode_count": None,
            "shots_per_episode": None, "episode_duration_seconds": None}
        self.run, _ = self.create_root(script_revision=self.revision)
        self.run = advance_production_run(self.run)
        self.assertEqual((self.run.status, self.run.current_stage_key), ("blocked", "bible_materialize"),
            self.run.blocked_reason)
        self.assertEqual(self.run.blocked_code, "script_bible_invalid")
        self.assertIn("结构化", self.run.blocked_reason)
        self.assertEqual(self.run.script_revision_id, self.revision.pk)
        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(GeneratedArtifact.objects.filter(stage_run__production_run=self.run,
            generation_attempt__isnull=False).exists())

    def test_model_profile_publish_rejects_incomplete_structure_contract(self):
        self.structure_root()
        model = self.version.deployment.profile_version
        draft = ModelProfileVersion(profile_key="incomplete-structure-model", version=1,
            provider=model.provider, model_identifier=model.model_identifier, modality="text")
        specification = deepcopy(model.specification)
        del specification["script_structure"]["timeout_seconds"]
        assign_json_contract(draft, "specification", specification)
        draft.save()
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.post(f"/api/v1/model-profile-versions/{draft.pk}/publish/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["code"], "invalid")
        self.assertIn("script_structure 只接受模型传输设置", str(response.data))
        draft.refresh_from_db()
        self.assertEqual(draft.status, "draft")
