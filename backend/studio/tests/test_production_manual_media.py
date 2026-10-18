"""Formal manual media actions over exact preparations; no provider or GPU call."""
from copy import deepcopy
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import connection
from django.test.utils import CaptureQueriesContext

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ApprovalDecision, ArtifactSelectionDecision, Asset, CharacterBible, Episode, ExecutionPreparation, GenerationAttempt, GenerationTask, PropBible, SceneBible, ProductionManualCommand, ProductionRun, Shot
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.production_bible_targets import capture_bible_target, verify_bible_target
from studio.services.execution_selected_outputs import selected_output_item_key
from studio.services.execution_workflow_staging import stage_execution_workflow_media
from studio.services.json_contracts import json_contract
from studio.tests.test_execution_workflow_staging import RecordingUploadClient
from studio.tests import test_execution_production_workflow_artifacts as media_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ManualMediaPreparationTests(TestCase):
    resolve = media_fixtures.ProductionWorkflowArtifactTests.resolve
    mark_submitted = media_fixtures.ProductionWorkflowArtifactTests.mark_submitted

    def setUp(self):
        media_fixtures.ProductionWorkflowArtifactTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        catalog = production_stage_catalog()
        response = self.client.post("/api/v1/production-runs/", {"project_id": str(self.project.pk), "plan_source": "manual",
            "idempotency_key": "manual-media-root", "allow_external_calls": False,
            "config_overrides": {}, "manual_plan": {
                "stage_catalog_key": catalog.value["key"], "expected_stage_catalog_sha256": catalog.sha256,
                "scope": {"kind": "project"}, "delivery": {"kind": "episode_masters"}, "initial_content": None}}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.run = ProductionRun.objects.get(pk=response.data["id"])
        self.episode = Episode.objects.create(project=self.project, number=1, title="First")
        self.shot = Shot.objects.create(episode=self.episode, number=1, title="Target", duration_ms=1000, media_type="image", include_audio=False)
        self.url = f"/api/v1/production-runs/{self.run.pk}/manual-actions/"
        options = self.client.get(f"/api/v1/production-runs/{self.run.pk}/targets/", {"kind": "shot"})
        self.assertEqual(options.status_code, 200, options.data)
        target, = options.data["results"]
        self.body = {"action": "generate", "stage_key": "media_generation", "unit_key": target["unit_key"],
            "scope": target["scope"], "expected_run_plan_sha256": options.data["run_plan_sha256"],
            "expected_selection_revision": None, "idempotency_key": "media-candidate", "payload": {
                "execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
                **deepcopy(self.intent.value)}}
        self.before = (GenerationAttempt.objects.count(), GenerationTask.objects.count())
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("No external call on create")))

    def post(self, body=None):
        return self.client.post(self.url, self.body if body is None else body, format="json")

    def test_selected_shot_real_workflow_and_manual_channels_freeze_without_dispatch_or_adoption(self):
        response = self.post()
        self.assertEqual(response.status_code, 202, response.data)
        prep = ExecutionPreparation.objects.get(pk=response.data["target_id"])
        unit = prep.contract.resolved_inputs["production_unit"]
        self.assertEqual(unit["scope"], self.body["scope"])
        self.assertEqual(unit["episode_id"], str(self.episode.pk))
        self.assertEqual(unit["run_plan_sha256"], self.body["expected_run_plan_sha256"])
        self.assertEqual(prep.unit_key, self.body["unit_key"])
        self.assertIsNone(prep.next_check_at)
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=evaluated.revision, actor=self.user)
        self.assertEqual(preview.transport.value["prompt_channels"]["values"], self.body["payload"]["manual_channels"])
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), self.before)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(self.run.stages.exclude(stage_key="media_generation").exclude(status="pending").exists())

    def test_bible_image_preparation_targets_exact_entity_instead_of_a_placeholder_shot(self):
        options = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-action-options/")
        self.assertEqual(options.status_code, 200, options.data)
        action = next(row for row in options.data["actions"] if row["stage_key"] == "bible_image_generation" and row["action"] == "generate")
        self.assertEqual(action["scope_kinds"], ["character", "scene", "prop"])
        wrong = {**deepcopy(self.body), "stage_key": "bible_image_generation"}
        self.assertEqual(self.post(wrong).status_code, 400)
        for kind, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible)):
            with self.subTest(kind=kind):
                entity = model.objects.create(project=self.project, slug=f"target-{kind}", name=f"Actual {kind}",
                    **({"stable_speaker_id": "S1"} if kind == "character" else {}))
                body = {**deepcopy(self.body), "stage_key": "bible_image_generation", "idempotency_key": f"target-{kind}",
                    "scope": {"kind": kind, "id": str(entity.pk)}, "unit_key": f"{kind}:{entity.pk}"}
                response = self.post(body)
                self.assertEqual(response.status_code, 202, response.data)
                prep = ExecutionPreparation.objects.get(pk=response.data["target_id"])
                self.assertEqual(prep.contract.resolved_inputs["production_unit"]["scope"], body["scope"])
                self.assertIsNone(prep.contract.resolved_inputs["production_unit"]["episode_id"])
                self.assertEqual(prep.stage_run.stage_key, "bible_image_generation")
                unit = prep.contract.resolved_inputs["production_unit"]
                baseline = prep.contract.resolved_inputs["bible_target"]
                self.assertEqual(verify_bible_target(baseline, unit), capture_bible_target(self.run, unit))
                self.assertEqual(baseline["name"], entity.name)
                entity.description = "后来人工修改的角色或环境描述"
                entity.save(update_fields=("description",))
                self.assertNotEqual(capture_bible_target(self.run, unit), baseline)
                self.assertEqual(self.post(body).data, response.data)
                prep.contract.refresh_from_db()
                self.assertEqual(prep.contract.resolved_inputs["bible_target"], baseline)
                for invalid in (None, {**baseline, "scope": self.body["scope"]},
                        {**baseline, "reference_asset_ids": ["not-an-asset"]}):
                    with self.assertRaises(ValidationError):
                        verify_bible_target(invalid, unit)
                evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
                frozen = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=evaluated.revision, actor=self.user)
                self.assertEqual(frozen.transport.value["prompt_channels"]["values"], body["payload"]["manual_channels"])
                wrong_adopt = {**body, "action": "adopt", "stage_key": "media_generation",
                    "idempotency_key": f"wrong-adopt-{kind}", "expected_selection_revision": 0,
                    "payload": {"source": {"kind": "asset", "asset_id": str(self.asset.pk),
                        "expected_sha256": self.asset.checksum, "media_kind": "image"}}}
                self.assertEqual(self.post(wrong_adopt).status_code, 400)
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), self.before)
        self.assertEqual(Shot.objects.count(), 1)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(ApprovalDecision.objects.exists())

    def test_replay_preserves_first_target_and_inputs_but_changed_target_conflicts(self):
        first = self.post()
        self.assertEqual(first.status_code, 202, first.data)
        with patch("studio.services.production_manual_preparations.resolve_execution_request_facts", side_effect=AssertionError("No replay re-resolution")):
            second = self.post()
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["target_id"], first.data["target_id"])
        changed = deepcopy(self.body)
        changed.update(scope={"kind": "episode", "id": str(self.episode.pk)}, unit_key=f"episode:{self.episode.pk}")
        self.assertEqual(self.post(changed).status_code, 409)
        self.assertEqual(ProductionManualCommand.objects.count(), 1)

    def test_bible_runtime_reads_the_same_target_baseline_and_original_workflow_task(self):
        from studio.services.execution_finalization import finalize_execution
        from studio.services.execution_production_dispatch import dispatch_production_execution
        from studio.services.execution_production_media import read_prepared_production_media
        from studio.tests.test_execution_production_auxiliaries import production_observations

        prop = PropBible.objects.create(project=self.project, slug="key", name="铜钥匙", description="旧钥匙")
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))
        body = {**deepcopy(self.body), "stage_key": "bible_image_generation", "scope": {"kind": "prop", "id": str(prop.pk)},
            "unit_key": f"prop:{prop.pk}", "idempotency_key": "prop-runtime"}
        response = self.post(body)
        self.assertEqual(response.status_code, 202, response.data)
        prep = ExecutionPreparation.objects.get(pk=response.data["target_id"])
        frozen = deepcopy(prep.contract.resolved_inputs["bible_target"])
        prep = finalize_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            actor=self.user, collect_admission_facts=production_observations)
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=production_observations):
            attempt = dispatch_production_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
        prop.description = "后来修改"
        prop.save(update_fields=("description",))
        detail = read_prepared_production_media(attempt)
        self.assertEqual(detail["bible_target"], frozen)
        self.assertEqual(detail["production_unit"]["scope"], body["scope"])
        self.assertEqual(list(attempt.generation_task.platform_attempts.values_list("pk", flat=True)), [attempt.pk])
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(ApprovalDecision.objects.exists())

    def test_wrong_stage_unknown_scope_and_unfrozen_overrides_write_no_candidate(self):
        before = ExecutionPreparation.objects.count()
        cases = [{**self.body, "stage_key": "script_generate"}, {**self.body, "unit_key": "guessed-shot"},
            {**self.body, "scope": {"kind": "shot", "id": str(self.project.pk)}}]
        changed = deepcopy(self.body); changed["payload"]["workflow_version_id"] = str(self.workflow.pk); cases.append(changed)
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(ExecutionPreparation.objects.count(), before)
        self.assertFalse(ProductionManualCommand.objects.exists())

    def test_media_options_report_output_kinds_without_guessing_operation_from_name(self):
        options = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-action-options/")
        self.assertEqual(options.status_code, 200, options.data)
        self.assertIn({"stage_key": "media_generation", "action": "generate", "operation": None,
            "output_kinds": ["image", "video", "audio"], "scope_kinds": ["project", "episode", "shot"]}, options.data["actions"])

    def test_reviewed_asset_is_a_real_source_for_next_preparation_not_an_asset_proxy(self):
        base = {"scope": self.body["scope"], "unit_key": self.body["unit_key"],
            "expected_run_plan_sha256": self.body["expected_run_plan_sha256"]}
        adopted = self.post({**base, "action": "adopt", "stage_key": "media_generation", "idempotency_key": "adopt-ref",
            "expected_selection_revision": 0, "payload": {"source": {"kind": "asset", "asset_id": str(self.asset.pk),
                "expected_sha256": self.asset.checksum, "media_kind": "image"}}})
        self.assertEqual(adopted.status_code, 201, adopted.data)
        decision = ArtifactSelectionDecision.objects.get(stage_run__production_run=self.run)
        source = {"kind": "selected_output", "decision_id": str(decision.pk), "artifact_id": str(decision.artifact_id),
            "purpose": None, "item_key": selected_output_item_key(input_key="picture", decision_id=str(decision.pk),
                artifact_id=str(decision.artifact_id), purpose=None)}
        self.body["payload"]["inputs"]["picture"] = {"items": [source]}
        before = ExecutionPreparation.objects.count()
        denied = self.post()
        self.assertEqual(denied.status_code, 400, denied.data)
        self.assertEqual(ExecutionPreparation.objects.count(), before)
        inventory = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-media-review/").data
        reviewed = self.post({**base, "scope": {"kind": "project", "id": str(self.project.pk)},
            "unit_key": f"project:{self.project.pk}", "action": "approve", "stage_key": "artifact_selection",
            "idempotency_key": "approve-ref", "expected_selection_revision": 0,
            "payload": {"selection": inventory["selection"], "expected_selection_sha256": inventory["selection_sha256"], "comment": "核对参考图"}})
        self.assertEqual(reviewed.status_code, 201, reviewed.data)
        response = self.post()
        self.assertEqual(response.status_code, 202, response.data)
        prep = ExecutionPreparation.objects.get(pk=response.data["target_id"])
        resolved = prep.contract.resolved_inputs["inputs"]
        self.assertIn(str(decision.pk), str(resolved))
        self.assertIn(str(decision.artifact_id), str(resolved))
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=evaluated.revision, actor=self.user)
        peer, receipts = RecordingUploadClient(), []
        staged = stage_execution_workflow_media(definition=self.definition, resolved_inputs=json_contract(resolved),
            transport=preview.transport, owner=self.user, root_id=prep.pk, client=peer, staged_inputs=[],
            persist_staged_inputs=lambda rows: receipts.extend(rows), check_active=lambda: None, max_media_bytes=65536,
            production_run_id=str(self.run.pk), eligible_stage_ids=execution_source_stage_ids(prep.stage_run))
        self.assertEqual(len(peer.uploads), 1)
        self.assertTrue(peer.uploads[0].endswith(".png"))
        self.assertTrue(receipts)
        self.assertIn(peer.uploads[0], str(staged.graph))
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), self.before)
        self.assertEqual(self.run.stages.get(stage_key="media_generation").status, "running")
        replacement = Asset(owner=self.user, name="Explicit new reference", kind="reference", mime_type="image/png", checksum=self.asset.checksum)
        with self.asset.file.open("rb") as handle:
            replacement.file.save("replacement.png", ContentFile(handle.read()), save=True)
        changed = self.post({**base, "action": "adopt", "stage_key": "media_generation", "idempotency_key": "replace-ref",
            "expected_selection_revision": 1, "payload": {"source": {"kind": "asset", "asset_id": str(replacement.pk),
                "expected_sha256": replacement.checksum, "media_kind": "image"}}})
        self.assertEqual(changed.status_code, 201, changed.data)
        with self.assertRaisesMessage(ValidationError, "选片、上游内容及审核依据已变化"):
            stage_execution_workflow_media(definition=self.definition, resolved_inputs=json_contract(resolved),
                transport=preview.transport, owner=self.user, root_id=prep.pk, client=peer, staged_inputs=receipts,
                persist_staged_inputs=lambda rows: receipts.extend(rows), check_active=lambda: None, max_media_bytes=65536,
                production_run_id=str(self.run.pk), eligible_stage_ids=execution_source_stage_ids(prep.stage_run))
        self.assertEqual(len(peer.uploads), 1)
        self.assertEqual(self.post().data["target_id"], str(prep.pk))

    def test_source_stages_must_match_frozen_catalog_and_not_later_unreviewed_steps(self):
        stage = self.run.stages.get(stage_key="media_generation")
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(execution_source_stage_ids(stage), (str(stage.pk),))
        source_queries = [row["sql"] for row in queries if 'FROM "studio_stagerun"' in row["sql"]]
        self.assertEqual(len(source_queries), 1)
        self.assertNotIn('"input_snapshot"', source_queries[0])
        self.assertNotIn('"output_snapshot"', source_queries[0])
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + source_queries[0])
            explanation = str(cursor.fetchall())
        self.assertIn("USING INDEX", explanation)
        self.assertNotIn("USE TEMP B-TREE", explanation)
        earlier = self.run.stages.get(stage_key="script_generate")
        self.assertNotIn(str(stage.pk), execution_source_stage_ids(earlier))
        stage.ordinal = 1
        with self.assertRaisesMessage(ValidationError, "冻结运行计划"):
            execution_source_stage_ids(stage)
