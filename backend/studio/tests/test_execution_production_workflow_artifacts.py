"""Preparation-backed archival with real registry, PNG, sidecar and manifest.

Task allocation requires no trace or PromptArtifact.
No live service, remote model, real database migration or GPU certification.
"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from PIL import Image
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import Asset, ExecutionPreparation, GeneratedArtifact, GenerationAttempt, GenerationTask, ProductionRun, SeriesProject, StageRun
from studio.services.configuration import resolve_configuration
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_instances import publish_execution_version
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.generated_artifacts import inspect_task_output_media, record_generation_task_artifacts
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.provenance import persist_task_provenance, ProvenanceError, task_manifest_path, verified_task_manifest
from studio.services.provider_registry import ProviderDriverError
from studio.services.security_boundaries import boundary_int
from studio.services.task_queue import _ensure_task_deadlines, claim_next_task, prepare_workflow, process_task
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_execution_production_auxiliaries import production_observations
from studio.tests import test_execution_workflow_resolution as image_fixtures
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ProductionWorkflowArtifactTests(TestCase):
    def setUp(self):
        image_fixtures.ExecutionImageResolutionRequestTests.setUp(self)
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=["is_active"])
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        self.project = SeriesProject.objects.create(owner=self.user, title="Media archive", **PROJECT_CONFIG)
        self.run = ProductionRun.objects.create(owner=self.user, project=self.project, plan_source="manual", status="running",
            allow_external_calls=True, idempotency_key=str(uuid4()), request_sha256="a" * 64)
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        from studio.services.production_run_plans import freeze_run_execution_plan
        from studio.services.production_stage_catalog import production_stage_catalog
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        media_stage = next(row for row in catalog.value["stages"] if row["key"] == "media_generation")
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", ordinal=media_stage["ordinal"],
            mode="manual", status="running", idempotency_key=str(uuid4()))
        png = BytesIO()
        Image.new("RGB", (12, 18), "blue").save(png, format="PNG")
        self.asset = Asset(owner=self.user, name="Actual reference", kind="reference", mime_type="image/png", checksum=sha256(png.getvalue()).hexdigest())
        self.asset.file.save("reference.png", ContentFile(png.getvalue()))
        self.intent = json_contract({"inputs": {"picture": {"items": [{"kind": "asset", "item_key": "picture-use",
            "asset_id": str(self.asset.pk), "purpose": None}]}}, "parameters": {"sampling.seed": 7},
            "manual_channels": {"positive_prompt": "PRIVATE PROMPT <Picture 1>.", "negative_prompt": "blur"},
            "subject_facts": [], "source_language": "", "target_language": ""})
        self.preparation = create_execution_instance_preparation(owner_ref=("stage_run_id", self.stage.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256, unit_key="image-unit",
            request_key="candidate-image", intent=self.intent, resolve_facts=self.resolve, actor=self.user)
        self.preparation = finalize_execution(preparation_id=self.preparation.pk,
            expected_preparation_revision=self.preparation.revision, actor=self.user, collect_admission_facts=production_observations)
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=production_observations):
            self.attempt = dispatch_production_execution(preparation_id=self.preparation.pk,
                expected_preparation_revision=self.preparation.revision, actor=self.user)
        self.task = self.attempt.generation_task
        GenerationTask.objects.filter(pk=self.task.pk).update(status="running", attempts=1, started_at=timezone.now())
        self.task.refresh_from_db()
        _ensure_task_deadlines(self.task)
        self.peer = RecordingUploadClient()
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=production_observations):
            prepare_workflow(self.task, self.peer)
        from django.conf import settings
        self.output_root = Path(settings.MEDIA_ROOT) / "comfy-output"
        setting = override_settings(COMFYUI_OUTPUT_DIR=str(self.output_root))
        setting.enable()
        self.addCleanup(setting.disable)
        self.filename = f"{self.preparation.pk}_00001_.png"
        self.output = self.output_root / "autoprompt" / "execution" / self.filename
        self.output.parent.mkdir(parents=True)
        Image.new("RGB", (512, 768), "green").save(self.output)
        self.receipt = {"node_id": "7", "filename": self.filename, "subfolder": "autoprompt/execution", "type": "output"}
        self.mark_submitted()

    def resolve(self, definition, stage):
        return resolve_execution_request_facts(definition, self.intent.value, owner=self.user, unit_key="image-unit",
            max_media_bytes=65536, parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No inferred source"),
            review_contract=json_contract({"schema_version": 1, "mode": "manual"}), project_id=self.project.pk,
            production_run_id=str(self.run.pk), production_scope={"kind": "project", "id": str(self.project.pk)})

    def mark_submitted(self):
        now = timezone.now()
        self.attempt.status = "submitted"
        self.attempt.provider_request_id = str(self.attempt.pk)
        self.attempt.submission_started_at = self.attempt.submitted_at = now
        self.attempt.save(update_fields=["status", "provider_request_id", "submission_started_at", "submitted_at", "updated_at"])
        self.task.submission_state = "submitted"
        self.task.submission_token = self.task.comfy_prompt_id = str(self.attempt.pk)
        self.task.submission_prepared_at = self.task.submission_submitted_at = now
        self.task.save()

    def archive(self):
        observed = inspect_task_output_media(self.task, [self.receipt])
        with transaction.atomic():
            self.task.outputs = persist_task_provenance(self.task, observed)
            self.task.status = "succeeded"
            self.task.save(update_fields=["status", "updated_at"])
            return record_generation_task_artifacts(self.task)

    def test_actual_bytes_archive_without_old_snapshot_or_implicit_selection(self):
        reserved = self.attempt.committed_cost
        artifact, = self.archive()
        self.assertEqual(artifact.status, "available")
        self.assertEqual(artifact.kind, "image")
        self.assertEqual(artifact.sha256, sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(artifact.manifest_metadata["preparation_id"], str(self.preparation.pk))
        self.assertEqual(artifact.manifest_metadata["observed_media"]["width"], 512)
        self.assertEqual(artifact.comparison_parameters["generation"]["sampling.seed"], 7)
        manifest = verified_task_manifest(self.task, 0)
        self.assertEqual(manifest["generation"]["execution_instance_version_id"], str(self.version.pk))
        self.assertEqual(manifest["source_media"][0]["asset_id"], str(self.asset.pk))
        text = task_manifest_path(self.task, 0).read_text(encoding="utf-8")
        for forbidden in ("PRIVATE PROMPT", "WRONG OLD PROMPT", "prompt_artifact_id", "base_url", str(self.output_root)):
            self.assertNotIn(forbidden, text)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, "succeeded")
        self.assertIsNone(self.attempt.actual_cost)
        self.assertEqual(self.attempt.committed_cost, reserved)
        self.assertEqual(record_generation_task_artifacts(self.task)[0].pk, artifact.pk)
        self.assertEqual(GeneratedArtifact.objects.count(), 1)

    def test_primary_node_type_count_prefix_and_actual_size_are_verified(self):
        for rows in ([{**self.receipt, "node_id": "other"}], [self.receipt, self.receipt],
                [{**self.receipt, "filename": f"{uuid4()}_00001_.png"}], [{**self.receipt, "type": "temp"}]):
            with self.subTest(rows=rows), self.assertRaises((ValidationError, ProviderDriverError)):
                inspect_task_output_media(self.task, rows)
        Image.new("RGB", (12, 18), "green").save(self.output)
        with self.assertRaises((ValidationError, ProvenanceError)):
            inspect_task_output_media(self.task, [self.receipt])
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_auxiliary_preview_output_is_not_archived_or_sealed(self):
        result = inspect_task_output_media(self.task, [{**self.receipt, "node_id": "preview", "filename": "missing.png"}, self.receipt])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["node_id"], "7")

    def test_missing_or_modified_use_receipts_cannot_be_sealed(self):
        original = deepcopy(self.task.staged_inputs)
        bad = deepcopy(original)
        bad[0]["source_sha256"] = "0" * 64
        for rows in ([], bad, original + original):
            self.task.staged_inputs = rows
            with self.subTest(rows=rows), self.assertRaises(ValidationError):
                self.archive()
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertFalse(task_manifest_path(self.task, 0).exists())

    def test_no_submit_fact_no_formal_candidate(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="queued", provider_request_id="", submission_started_at=None)
        with self.assertRaisesMessage(ValidationError, "仍有效的本次主调用"):
            self.archive()
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "running")
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_foreign_remote_identity_and_stopped_run_cannot_publish_outputs(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(provider_request_id=str(uuid4()))
        with self.assertRaisesMessage(ValidationError, "明确远端提交事实"):
            self.archive()
        self.mark_submitted()
        ProductionRun.objects.filter(pk=self.run.pk).update(status="cancelled")
        with self.assertRaisesMessage(ValidationError, "仍有效的本次主调用"):
            self.archive()
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_repeat_does_not_overwrite_corrupted_managed_bytes(self):
        from django.conf import settings
        artifact, = self.archive()
        managed = Path(settings.MEDIA_ROOT) / artifact.object_key
        managed.write_bytes(b"corrupted bytes")
        with self.assertRaises(ProvenanceError):
            record_generation_task_artifacts(self.task)
        self.assertEqual(managed.read_bytes(), b"corrupted bytes")

    def test_manifest_failure_rolls_back_task_success_and_candidate(self):
        with patch.object(GeneratedArtifact, "save", side_effect=RuntimeError("manifest unavailable")):
            with self.assertRaisesMessage(RuntimeError, "manifest unavailable"):
                self.archive()
        self.task.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(self.task.status, "running")
        self.assertEqual(self.attempt.status, "submitted")
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_request_corruption_is_not_repaired_from_old_task_snapshot(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_sha256="0" * 64)
        with self.assertRaisesMessage(ValidationError, "冻结准备请求"):
            self.archive()
        self.assertFalse(GeneratedArtifact.objects.exists())

    def reset_unsubmitted(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="queued", provider_request_id="",
            submission_started_at=None, submitted_at=None)
        GenerationTask.objects.filter(pk=self.task.pk).update(status="running", submission_token="", comfy_prompt_id="",
            submission_state="unprepared", submission_prepared_at=None, submission_submitted_at=None)
        self.task.refresh_from_db()

    def test_original_worker_reaches_actual_signed_manifest_once(self):
        self.reset_unsubmitted()
        peer = self.peer
        post_ids = []
        def release():
            now = timezone.now().isoformat()
            self.assertFalse(GenerationTask.objects.get(pk=self.task.pk).submission_token)
            return {"schema_version": 1, "status": "succeeded", "policy_sha256": "d" * 64,
                "unload_models": True, "free_memory": True, "attempted_at": now, "completed_at": now,
                "error_code": "", "http_status": 200}
        peer.release_resources_before_submit = Mock(side_effect=release)
        def queue(graph, client_id, *, prompt_id):
            current = GenerationAttempt.objects.get(pk=self.attempt.pk)
            self.assertEqual(current.status, "submitting")
            self.assertIsNotNone(current.submission_started_at)
            self.assertEqual(prompt_id, str(self.attempt.pk))
            self.assertFalse(current.provider_request_id)
            self.assertEqual(list(self.task.observations.order_by("created_at").values_list("kind", "status")),
                [("preflight", "passed"), ("resource_release", "passed")])
            post_ids.append(prompt_id)
            return SimpleNamespace(prompt_id=prompt_id, node_errors={})
        peer.queue_prompt = queue
        peer.extract_outputs = lambda _: [self.receipt]
        self.run.refresh_from_db()
        committed = self.run.committed_cost_total
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=production_observations), \
                patch("studio.services.task_queue.preflight_comfyui_workflow", return_value=SimpleNamespace(as_dict=lambda: {"node_count": 3, "link_count": 2, "class_types": ["ControlledPeer"], "schema_sha256": "c" * 64})), \
                patch("studio.services.task_queue._wait_for_task_completion", return_value={"status": {"status_str": "success", "completed": True}}), \
                patch("studio.services.task_distribution_recovery.prepare_all_task_distributions", return_value=[]), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(result.submission_state, "terminal_succeeded")
        self.assertEqual(post_ids, [str(self.attempt.pk)])
        peer.release_resources_before_submit.assert_called_once_with()
        self.assertEqual(GeneratedArtifact.objects.get(generation_task=self.task).status, "available")
        self.attempt.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(self.attempt.status, "succeeded")
        self.assertEqual(self.attempt.provider_request_id, str(self.attempt.pk))
        self.assertEqual(self.run.committed_cost_total, committed)
        self.assertIsNone(self.attempt.actual_cost)

    def test_final_gate_does_not_submit_or_exhaust_claim_count(self):
        from datetime import timedelta
        self.reset_unsubmitted()
        peer = self.peer
        peer.queue_prompt = Mock(side_effect=AssertionError("No POST when final gate changes"))
        calls = []
        def admission(*args, **kwargs):
            facts = production_observations(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                facts.hard_blockers.append({"code": "test_capacity_changed", "message": "Controlled capacity change after upload"})
            return facts
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=admission), \
                patch("studio.services.task_queue.preflight_comfyui_workflow", return_value=SimpleNamespace(as_dict=lambda: {"node_count": 3, "link_count": 2, "class_types": ["ControlledPeer"], "schema_sha256": "c" * 64})):
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "queued", result.error_message)
        self.assertFalse(result.submission_token)
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.submission_started_at)
        self.assertIsNotNone(self.attempt.next_poll_at)
        self.assertIsNone(claim_next_task())
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(next_poll_at=timezone.now() - timedelta(seconds=1))
        # A new worker claim is not a paid model retry; no old max_attempts field.
        next_task = claim_next_task()
        self.assertEqual(next_task.pk, self.task.pk)
        self.assertEqual(next_task.attempts, 2)
        self.assertEqual(next_task.hard_deadline_at, self.task.hard_deadline_at)
        self.assertEqual(GenerationAttempt.objects.filter(preparation=self.preparation).count(), 1)
        from django.db import connection
        from django.db.models import Q
        if connection.vendor == "sqlite":
            query = GenerationTask.objects.filter(status="queued", submission_token="").filter(
                Q(platform_attempts__next_poll_at__isnull=True) | Q(platform_attempts__next_poll_at__lte=timezone.now()))
            with connection.cursor() as cursor:
                constraints = connection.introspection.get_constraints(cursor, GenerationAttempt._meta.db_table)
            self.assertTrue(any(row["unique"] and row["columns"] == ["generation_task_id"]
                for row in constraints.values()))
            self.assertRegex(query.explain(), r"SEARCH studio_generationattempt USING INDEX \S+ \(generation_task_id=\?\)")
