"""Production-root Comfy preparation contracts, not completed GPU admission.

Uses real registered graph, local PNG and original Run/Stage/Preparation rows.
This request-focused suite isolates the admission collector for task allocation
and reading. It does not prove current resource readiness or GPU execution.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import GenerationAttempt, ModelExecutionInstanceVersion, ProductionRun, SeriesProject, StageRun
from studio.services.configuration import resolve_configuration
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_instances import publish_execution_version
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_production_admission import collect_production_execution_admission
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.execution_production_media import read_prepared_production_media, production_media_peer
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.security_boundaries import boundary_int
from studio.tests import test_execution_workflow_request_media as fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_execution_production_auxiliaries import production_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ProductionWorkflowRequestTests(TestCase):
    _create_version = fixtures.ExecutionWorkflowMediaRequestTests._create_version

    def setUp(self):
        fixtures.ExecutionWorkflowMediaRequestTests.setUp(self)
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        specification = deepcopy(self.profile.specification)
        specification["execution_monitoring"] = {"schema_version": 1, "default_idle_timeout_seconds": 240,
            "node_type_overrides": {"LoadImage": 75}}
        assign_json_contract(self.profile, "specification", specification)
        self.profile.save()
        self.version = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=["is_active"])
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        self.project = SeriesProject.objects.create(owner=self.user, title="Workflow request", **PROJECT_CONFIG)
        self.run = ProductionRun.objects.create(owner=self.user, project=self.project, plan_source="manual",
            status="running", allow_external_calls=True, idempotency_key=str(uuid4()), request_sha256="a" * 64)
        snapshot = resolve_configuration(run=self.run).snapshot()
        snapshot["values"].update({"runtime.task_idle_timeout_seconds": 180, "production.task_timeout_seconds": 900,
            "production.default_concurrency": 3})
        assign_json_contract(self.run, "config_snapshot", snapshot, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        from studio.services.production_run_plans import freeze_run_execution_plan
        from studio.services.production_stage_catalog import production_stage_catalog
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        media_stage = next(row for row in catalog.value["stages"] if row["key"] == "media_generation")
        self.stage = StageRun.objects.create(production_run=self.run, stage_key=media_stage["key"], ordinal=media_stage["ordinal"],
            mode="manual", status="running", idempotency_key=str(uuid4()))
        self.intent = json_contract({"inputs": {"references": {"items": [{"kind": "asset", "item_key": key,
            "asset_id": str(self.asset.pk), "purpose": None} for key in ("second", "first")]}},
            "parameters": {key: value for key, value in self.parameters["values"].items() if self.parameters["sources"][key] == "request"},
            "manual_channels": {"positive_prompt": "A parcel opens."}, "subject_facts": [], "source_language": "", "target_language": ""})
        self.preparation = create_execution_instance_preparation(owner_ref=("stage_run_id", self.stage.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256, unit_key="media-unit", request_key="candidate-1",
            intent=self.intent, resolve_facts=self.resolve, actor=self.user)
        self.preparation = finalize_execution(preparation_id=self.preparation.pk,
            expected_preparation_revision=self.preparation.revision, actor=self.user, collect_admission_facts=production_observations)
        self.assertEqual(self.preparation.status, "ready", self.preparation.reason)
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=production_observations):
            self.attempt = dispatch_production_execution(preparation_id=self.preparation.pk,
                expected_preparation_revision=self.preparation.revision, actor=self.user)
        self.preparation.refresh_from_db()

    def resolve(self, definition, stage):
        return resolve_execution_request_facts(definition, self.intent.value, owner=self.user, unit_key="media-unit",
            max_media_bytes=65536, parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No guessed source"),
            review_contract=json_contract({"schema_version": 1, "mode": "manual"}),
            project_id=self.project.pk, production_run_id=str(self.run.pk),
            production_scope={"kind": "project", "id": str(self.project.pk)})

    def test_production_reads_own_graph_parameters_and_frozen_runtime_without_legacy_artifact(self):
        from studio.models import GenerationTask
        with patch("studio.services.workflow_compiler.compile_frozen_workflow_values", side_effect=AssertionError("No recompile")), \
                patch("studio.services.execution_production_media.resolve_configuration_from_snapshot", side_effect=AssertionError("No new deadlines")):
            result = read_prepared_production_media(self.attempt)
        self.assertEqual(result["driver_type"], "comfyui")
        self.assertEqual(result["output_kind"], "video")
        self.assertEqual(result["parameters"]["sampling.seed"], 37)
        self.assertEqual(result["output_prefix"], f"autoprompt/execution/{self.preparation.pk}")
        self.assertEqual(result["execution_deadlines"]["idle_timeout_seconds"], 180)
        self.assertEqual(result["execution_deadlines"]["hard_timeout_seconds"], 900)
        self.assertEqual(result["execution_deadlines"]["configuration_sha256"], self.run.config_snapshot_sha256)
        self.assertEqual(result["execution_monitoring"]["default_idle_timeout_seconds"], 240)
        self.assertEqual(result["execution_monitoring"]["node_type_overrides"], {"LoadImage": 75})
        self.assertEqual([row["item_key"] for row in result["payload"]["media_preparation"]["bindings"]], ["second", "first"])
        self.assertEqual(GenerationTask.objects.count(), 1)
        self.assertEqual(self.attempt.generation_task.kind, "video")

    def test_explicit_shot_scope_is_frozen_and_original_task_uses_its_episode(self):
        from studio.models import Episode, Shot
        from studio.services.execution_production_workflow_queue import read_prepared_generation_task
        episode = Episode.objects.create(project=self.project, number=1, title="Target episode")
        shot = Shot.objects.create(episode=episode, number=1,
            duration_ms=1000, media_type="video", include_audio=False)
        scope = {"kind": "shot", "id": str(shot.pk)}
        def resolve(definition, stage):
            return resolve_execution_request_facts(definition, self.intent.value, owner=self.user, unit_key="opaque-target",
                max_media_bytes=65536, parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No source"),
                review_contract=json_contract({"schema_version": 1, "mode": "manual"}), project_id=self.project.pk,
                production_run_id=str(self.run.pk), production_scope=scope)
        prep = create_execution_instance_preparation(owner_ref=("stage_run_id", self.stage.pk), version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, unit_key="opaque-target", request_key="explicit-shot",
            intent=json_contract({**self.intent.value, "scope": scope}), resolve_facts=resolve, actor=self.user)
        prep = finalize_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            actor=self.user, collect_admission_facts=production_observations)
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=production_observations):
            attempt = dispatch_production_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
        task = attempt.generation_task
        self.assertEqual(task.episode_id, episode.pk)
        self.assertEqual(read_prepared_generation_task(task).media["production_unit"]["scope"], scope)
        task.episode_id = None
        with self.assertRaises(ValidationError):
            read_prepared_generation_task(task)

    def test_task_reads_frozen_frame_and_fps_values_not_later_project_defaults(self):
        from studio.services.execution_production_workflow_queue import read_prepared_generation_task
        original = read_prepared_generation_task(self.attempt.generation_task).media
        self.assertEqual(original["parameters"]["video.frames"], 124)
        self.assertEqual(original["parameters"]["video.fps"], 24)
        compiled = original["payload"]["compiled_workflow"]["resolved_semantics"]
        self.assertEqual(compiled["video.frames"], original["parameters"]["video.frames"])
        self.assertEqual(compiled["video.fps"], original["parameters"]["video.fps"])
        SeriesProject.objects.filter(pk=self.project.pk).update(width=256, height=256, fps=60)
        self.project.refresh_from_db()
        self.assertEqual(read_prepared_generation_task(self.attempt.generation_task).media, original)

    def test_dispatch_replay_preserves_original_task_and_has_no_legacy_required_fields(self):
        from studio.models import GenerationTask
        self.run.refresh_from_db()
        before = self.run.committed_cost_total
        with patch("studio.services.execution_production_dispatch.current_production_gate", side_effect=AssertionError("Replay is not a new submit")):
            existing = dispatch_production_execution(preparation_id=self.preparation.pk,
                expected_preparation_revision=self.preparation.revision, actor=self.user)
        self.assertEqual(existing.pk, self.attempt.pk)
        self.assertEqual(existing.generation_task_id, self.attempt.generation_task_id)
        self.assertEqual(GenerationTask.objects.count(), 1)
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, before)
        retired = {"generated_workflow", "capability_snapshot", "max_attempts", "seed", "backend", "model_profile",
            "workflow_version", "effective_frames", "effective_duration_ms"}
        self.assertFalse(retired & {field.name for field in GenerationTask._meta.fields})

    def test_replay_cannot_reconstruct_a_missing_task_link(self):
        from studio.models import GenerationTask
        task_id = self.attempt.generation_task_id
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(generation_task=None)
        with self.assertRaisesMessage(ValidationError, "缺少原任务"):
            dispatch_production_execution(preparation_id=self.preparation.pk,
                expected_preparation_revision=self.preparation.revision, actor=self.user)
        self.assertEqual(list(GenerationTask.objects.values_list("id", flat=True)), [task_id])
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.generation_task_id)

    def test_task_insert_failure_rolls_back_attempt_reservation_and_stage_count(self):
        from studio.models import GenerationTask
        next_preparation = create_execution_instance_preparation(owner_ref=("stage_run_id", self.stage.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256, unit_key="media-unit",
            request_key="candidate-2", intent=self.intent, resolve_facts=self.resolve, actor=self.user)
        next_preparation = finalize_execution(preparation_id=next_preparation.pk,
            expected_preparation_revision=next_preparation.revision, actor=self.user, collect_admission_facts=production_observations)
        self.run.refresh_from_db()
        self.stage.refresh_from_db()
        cost, count = self.run.committed_cost_total, self.stage.attempt_count
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=production_observations), \
                patch.object(GenerationTask, "save", side_effect=RuntimeError("controlled task insert failure")), self.assertRaises(RuntimeError):
            dispatch_production_execution(preparation_id=next_preparation.pk,
                expected_preparation_revision=next_preparation.revision, actor=self.user)
        self.assertFalse(GenerationAttempt.objects.filter(preparation=next_preparation).exists())
        self.assertEqual(GenerationTask.objects.count(), 1)
        self.run.refresh_from_db()
        self.stage.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, cost)
        self.assertEqual(self.stage.attempt_count, count)

    def test_schema_removal_requires_reviewed_p5_cleanup_not_implicit_conversion(self):
        from importlib import import_module
        from types import SimpleNamespace
        from django.apps import apps
        from django.db import connection
        from studio.models import GenerationTask
        migration = import_module("studio.migrations.0155_preparation_backed_generation_tasks")
        with self.assertRaisesMessage(RuntimeError, "P5"):
            migration.require_empty_task_queue(apps, SimpleNamespace(connection=connection))
        self.assertEqual(GenerationTask.objects.count(), 1)
        self.assertEqual(migration.Migration.operations[0].code, migration.require_empty_task_queue)

    def test_old_or_changed_runtime_cannot_be_silently_repaired(self):
        detail = self.preparation.contract
        for value in (None, {}, {"schema_version": 1, "idle_timeout_seconds": True}):
            original = deepcopy(detail.resolved_inputs)
            detail.resolved_inputs["execution_deadlines"] = value
            from studio.services.execution_production_media import read_production_media_deadlines
            with self.subTest(value=value), self.assertRaises(ValidationError):
                read_production_media_deadlines(detail)
            detail.resolved_inputs = original
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_sha256="0" * 64)
        self.attempt.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "冻结准备请求"):
            read_prepared_production_media(self.attempt)

    def test_frozen_node_policy_is_not_replaced_by_current_model_defaults(self):
        first = read_prepared_production_media(self.attempt)
        specification = deepcopy(self.profile.specification)
        specification["execution_monitoring"]["default_idle_timeout_seconds"] = 600
        assign_json_contract(self.profile, "specification", specification)
        self.profile.save()
        self.assertEqual(read_prepared_production_media(self.attempt)["execution_monitoring"], first["execution_monitoring"])

    def test_workflow_checks_real_admission_and_cannot_fall_through_async_queue(self):
        facts = collect_production_execution_admission(self.preparation, self.definition,
            json_contract(self.preparation.contract.prepared_request))
        # This request-only fixture has no recorded host/model observations.
        # Supporting its protocol must not certify its current resources.
        self.assertTrue(facts.hard_blockers)
        with self.assertRaisesMessage(ValidationError, "异步媒体队列不能接管"):
            production_media_peer(self.attempt)
        from studio.services.remote_artifacts import archive_remote_artifacts
        with self.assertRaisesMessage(ValidationError, "ComfyUI须核对"):
            archive_remote_artifacts(self.attempt, artifacts=[], provider_config={}, expected_kind="video")
