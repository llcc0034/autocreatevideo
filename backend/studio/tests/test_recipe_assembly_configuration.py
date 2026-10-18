"""M8 recipe configuration through the original queue, no model/encoder calls.

The recipe is a storage-only fixture, not publication or end-to-end acceptance.
Media bytes/probing use the existing explicit assembly unit-test boundary.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import AssemblyJob, ConfigRevision, GenerationAttempt, GenerationTask, ProductionAssemblyLink, StageRun
from studio.services.assembly import AssemblyError, resolve_assembly_configuration
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_stage_snapshots import recipe_assembly_configuration, recipe_assembly_sources
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_runs import _execute_assembly, _freeze_stage_config, _freeze_stage_input, _save_stage_snapshot, retry_production_run
from studio.services.production_execution_retry import stage_retry_context
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.security_boundaries import boundary_int
from studio.tests import test_assembly as fixtures
from studio.tests import test_production_run_plans as plan_fixtures


class RecipeAssemblyConfigurationTests(TestCase):
    _selected_artifact = fixtures.AssemblyTests._selected_artifact
    _read_fixture_candidate = fixtures.AssemblyTests._read_fixture_candidate
    recipe = plan_fixtures.ProductionRunPlanTests.recipe

    def setUp(self):
        fixtures.AssemblyTests.setUp(self)
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("No network")))
        self.enterContext(patch("studio.services.assembly.subprocess.run", side_effect=AssertionError("No encoding")))
        self._selected_artifact(self.shot1, "one.mp4")
        self._selected_artifact(self.shot2, "two.mp4")
        recipe = self.recipe(definition={"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "required"},
            {"key": "assembly", "mode": "existing_pipeline", "approval": "auto"}]})
        self.run.plan_source, self.run.recipe_version = "recipe", recipe
        self.run.current_stage_key = "assembly"
        self.run.save(update_fields=("plan_source", "recipe_version", "current_stage_key"))
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content=None, stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256,
            actor=self.user)
        self.stage.mode, self.stage.status = "existing_pipeline", "succeeded"
        self.stage.save(update_fields=("mode", "status"))
        _save_stage_snapshot(self.stage, "output_snapshot", {"schema_version": 1, "kind": "isolated-media-selection"})
        self.assembly_stage = StageRun.objects.create(production_run=self.run, stage_key="assembly", ordinal=2,
            mode="existing_pipeline", status="running", idempotency_key=str(uuid4()))

    def freeze(self):
        _freeze_stage_config(self.run, self.assembly_stage)
        _freeze_stage_input(self.run, self.assembly_stage)
        return recipe_assembly_configuration(self.assembly_stage)

    def change_current_configuration(self):
        old = self.run.config_snapshot["values"]["delivery.assembly_max_attempts"]
        revision = create_config_revision(scope_type=ConfigRevision.ScopeType.PROJECT, project=self.project,
            values={"delivery.assembly_max_attempts": old + 1}, created_by=self.user, clone_published=False)
        publish_config_revision(revision, published_by=self.user)

    def test_first_freeze_uses_run_policy_and_replays_do_not_read_current_geometry_or_font(self):
        self.change_current_configuration()
        value = self.freeze()
        self.assertEqual(value["max_attempts"], self.run.config_snapshot["values"]["delivery.assembly_max_attempts"])
        self.assertEqual(value["media_profile"], self.run.config_snapshot["values"]["delivery.assembly_media_profile"])
        self.assertEqual(value["label_text"], self.run.config_snapshot["values"]["delivery.ai_label_text"])
        self.assertEqual((value["width"], value["height"], value["fps"]),
            (self.project.width, self.project.height, self.project.fps))
        hashes = (self.assembly_stage.config_snapshot_sha256, self.assembly_stage.input_snapshot_sha256)
        self.project.width, self.project.height, self.project.fps = 320, 240, 30
        self.project.save(update_fields=("width", "height", "fps"))
        with patch("studio.services.assembly.resolve_assembly_configuration", side_effect=AssertionError("Frozen only")), \
                override_settings(ASSEMBLY_AI_LABEL_FONTFILE="changed-font.ttf"):
            self.assertEqual(self.freeze(), value)
        self.assertEqual((self.assembly_stage.config_snapshot_sha256, self.assembly_stage.input_snapshot_sha256), hashes)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_initial_enqueue_and_explicit_failed_retry_use_same_configuration_and_signature(self):
        value = self.freeze()
        self.change_current_configuration()
        self.project.width, self.project.fps = 320, 30
        self.project.save(update_fields=("width", "fps"))
        with patch("studio.services.assembly.resolve_assembly_configuration", side_effect=AssertionError("No latest config")):
            _execute_assembly(self.run, self.assembly_stage)
            link = ProductionAssemblyLink.objects.get(production_run=self.run)
            first = link.assembly_job
            self.assertEqual(first.consumer_stage_id, self.assembly_stage.pk)
            self.assertEqual(first.consumer_input_sha256, self.assembly_stage.input_snapshot_sha256)
            self.assertEqual(first.max_attempts, value["max_attempts"])
            self.assertIn(f"{value['width']}:{value['height']}", str(first.ffmpeg_command))
            self.assertEqual([str(value) for value in first.source_items.order_by("ordinal").values_list("generated_artifact_id", flat=True)],
                [self.sources[str(shot.pk)]["generated_artifact_id"] for shot in (self.shot1, self.shot2)])
            _execute_assembly(self.run, self.assembly_stage)
            self.assertEqual(AssemblyJob.objects.count(), 1)
            first.status = "failed"
            first.save(update_fields=("status",))
            self.run.status, self.assembly_stage.status = "blocked", "blocked"
            self.run.save(update_fields=("status",))
            self.assembly_stage.save(update_fields=("status",))
            with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run):
                retry_production_run(self.run, user=self.user, **stage_retry_context(self.assembly_stage))
            link.refresh_from_db()
            second = link.assembly_job
            self.assertNotEqual(first.pk, second.pk)
            self.assertEqual(second.consumer_stage_id, first.consumer_stage_id)
            self.assertEqual(second.consumer_input_sha256, first.consumer_input_sha256)
            self.assertEqual(first.source_signature, second.source_signature)
            self.assertEqual(first.ffmpeg_command[1:-1], second.ffmpeg_command[1:-1])
            self.assertEqual(second.max_attempts, value["max_attempts"])
            self.assertEqual(list(first.source_items.order_by("ordinal").values_list("generated_artifact_id", "duration_ms")),
                list(second.source_items.order_by("ordinal").values_list("generated_artifact_id", "duration_ms")))
        self.assertEqual(AssemblyJob.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GenerationTask.objects.exists())

    def test_missing_or_corrupt_stage_contract_refuses_before_probe_or_job(self):
        with patch("studio.services.assembly._resolve_output", side_effect=AssertionError("No probe")):
            with self.assertRaises(ValidationError):
                _execute_assembly(self.run, self.assembly_stage)
        self.freeze()
        original = deepcopy(self.assembly_stage.config_snapshot)
        cases = []
        missing = deepcopy(original)
        missing.pop("assembly_configuration")
        cases.append(missing)
        for key, bad in (("fps", True), ("media_profile_sha256", "0" * 64), ("max_attempts", 0)):
            changed = deepcopy(original)
            changed["assembly_configuration"][key] = bad
            cases.append(changed)
        changed = deepcopy(original)
        changed["run_plan_sha256"] = "0" * 64
        cases.append(changed)
        for changed in cases:
            assign_json_contract(self.assembly_stage, "config_snapshot", changed,
                max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
            with self.subTest(changed=changed), patch("studio.services.assembly._resolve_output", side_effect=AssertionError("No probe")):
                with self.assertRaises(ValidationError):
                    _execute_assembly(self.run, self.assembly_stage)
        self.assertFalse(AssemblyJob.objects.exists())
        self.assertFalse(ProductionAssemblyLink.objects.exists())

    def test_frozen_values_missing_or_invalid_never_fall_back_to_current_policy(self):
        original = deepcopy(self.run.config_snapshot["values"])
        for key in ("delivery.assembly_media_profile", "delivery.assembly_max_attempts", "delivery.ai_label_text"):
            value = deepcopy(original)
            value.pop(key)
            with self.subTest(key=key), patch("studio.services.assembly.resolve_configuration", side_effect=AssertionError("No defaults")):
                with self.assertRaises(AssemblyError):
                    resolve_assembly_configuration(self.project, frozen_values=value)
        with self.assertRaises(AssemblyError):
            resolve_assembly_configuration(self.project, frozen_values={**original, "delivery.ai_label_enabled": "false"})

    def assert_source_conflict(self):
        original = deepcopy(self.assembly_stage.input_snapshot)
        digest = self.assembly_stage.input_snapshot_sha256
        with patch("studio.services.production_runs.create_assembly", side_effect=AssertionError("No enqueue")):
            with self.assertRaises(ValidationError):
                _execute_assembly(self.run, self.assembly_stage)
            with self.assertRaises(ValidationError):
                _freeze_stage_input(self.run, self.assembly_stage)
        self.assembly_stage.refresh_from_db()
        self.assertEqual(self.assembly_stage.input_snapshot, original)
        self.assertEqual(self.assembly_stage.input_snapshot_sha256, digest)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GenerationTask.objects.exists())

    def test_source_freeze_records_exact_selection_and_authored_timeline(self):
        self.freeze()
        snapshot = self.assembly_stage.input_snapshot["assembly_sources"]
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual([row["episode_id"] for row in snapshot["episodes"]], [str(self.episode.pk)])
        shots = snapshot["episodes"][0]["shots"]
        self.assertEqual([row["shot_id"] for row in shots], [str(self.shot1.pk), str(self.shot2.pk)])
        for row, shot in zip(shots, (self.shot1, self.shot2), strict=True):
            self.assertEqual((row["number"], row["duration_ms"], row["media_kind"]),
                (shot.number, shot.duration_ms, shot.media_type))
            self.assertIsNone(row["episode_start_ms"])
            self.assertIsNone(row["episode_end_ms"])
            source = row["source"]
            self.assertEqual(source["generated_artifact_id"], self.sources[str(shot.pk)]["generated_artifact_id"])
            decision = self.stage.artifact_selection_decisions.get(variant_group=str(shot.pk))
            self.assertEqual((source["decision_id"], source["decision_revision"]), (str(decision.pk), decision.revision))
            self.assertEqual(source["sha256"], decision.artifact.sha256)
            self.assertEqual(source["manifest_sha256"], decision.artifact.manifest_metadata_sha256)
        self.assertEqual(recipe_assembly_sources(self.assembly_stage), snapshot)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_changed_duration_or_timeline_is_not_refrozen_before_first_enqueue(self):
        self.freeze()
        self.shot1.duration_ms = 4000
        self.shot1.save(update_fields=("duration_ms",))
        self.assert_source_conflict()
        self.assertFalse(AssemblyJob.objects.exists())

    def test_changed_order_is_not_silently_accepted_even_with_same_media_and_durations(self):
        self.freeze()
        self.shot1.number = 3
        self.shot1.save(update_fields=("number",))
        self.assert_source_conflict()
        self.assertFalse(AssemblyJob.objects.exists())

    def test_changed_selection_never_replaces_frozen_source(self):
        self.freeze()
        self._selected_artifact(self.shot1, "replacement.mp4")
        self.assert_source_conflict()
        self.assertFalse(AssemblyJob.objects.exists())

    def test_same_artifact_reselected_with_new_decision_is_still_a_source_revision_conflict(self):
        from studio.services.generated_artifacts import select_artifact
        from studio.models import GeneratedArtifact

        original = GeneratedArtifact.objects.get(pk=self.sources[str(self.shot1.pk)]["generated_artifact_id"])
        self.freeze()
        self._selected_artifact(self.shot1, "temporary-selection.mp4")
        select_artifact(original, user=self.user)
        self.assert_source_conflict()
        self.assertFalse(AssemblyJob.objects.exists())

    def test_retry_rejects_changed_timing_without_replacing_failed_job(self):
        self.freeze()
        _execute_assembly(self.run, self.assembly_stage)
        link = ProductionAssemblyLink.objects.get(production_run=self.run)
        first = link.assembly_job
        first.status = "failed"
        first.save(update_fields=("status",))
        self.run.status, self.assembly_stage.status = "blocked", "blocked"
        self.run.save(update_fields=("status",))
        self.assembly_stage.save(update_fields=("status",))
        self.shot2.duration_ms = 4000
        self.shot2.save(update_fields=("duration_ms",))
        with patch("studio.services.production_runs.create_assembly", side_effect=AssertionError("No retry enqueue")):
            with self.assertRaises(ValidationError):
                retry_production_run(self.run, user=self.user, **stage_retry_context(self.assembly_stage))
        link.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(link.assembly_job_id, first.pk)
        self.assertEqual(first.status, "failed")
        self.assertEqual(AssemblyJob.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GenerationTask.objects.exists())

    def test_missing_source_snapshot_refuses_without_backfilling_from_current_selection(self):
        self.freeze()
        snapshot = deepcopy(self.assembly_stage.input_snapshot)
        snapshot.pop("assembly_sources")
        assign_json_contract(self.assembly_stage, "input_snapshot", snapshot,
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        with patch("studio.services.production_recipe_stage_snapshots._assembly_source_snapshot",
                side_effect=AssertionError("No historical backfill")):
            with self.assertRaisesMessage(ValidationError, "不能为旧阶段补造"):
                recipe_assembly_sources(self.assembly_stage)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_authored_intervals_and_manifest_revision_are_part_of_the_frozen_identity(self):
        from studio.models import GeneratedArtifact

        self.freeze()
        # Same duration, newly authored (valid) interval is not an implicit edit
        # to the already frozen assembly input, nor a media trim instruction.
        self.shot1.episode_start_ms, self.shot1.episode_end_ms = 0, self.shot1.duration_ms
        self.shot1.save(update_fields=("episode_start_ms", "episode_end_ms"))
        self.assert_source_conflict()
        self.shot1.episode_start_ms = self.shot1.episode_end_ms = None
        self.shot1.save(update_fields=("episode_start_ms", "episode_end_ms"))
        # Explicit isolated metadata corruption; never rewrite a live manifest.
        GeneratedArtifact.objects.filter(pk=self.sources[str(self.shot1.pk)]["generated_artifact_id"]).update(
            manifest_metadata_sha256="c" * 64)
        self.assert_source_conflict()
        self.assertFalse(AssemblyJob.objects.exists())

    def test_source_metadata_query_is_batched_narrow_and_indexed(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from studio.services.production_recipe_stage_snapshots import _assembly_source_snapshot

        with CaptureQueriesContext(connection) as capture:
            _assembly_source_snapshot(self.assembly_stage)
        queries = [row["sql"] for row in capture.captured_queries if 'AS "decision_artifact_id"' in row["sql"]]
        self.assertEqual(len(queries), 1)
        query = queries[0]
        self.assertNotIn('"manifest_metadata"', query)
        self.assertNotIn("SELECT *", query)
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + query)
            plan = "\n".join(row[3] for row in cursor.fetchall())
        self.assertIn("SEARCH studio_generatedartifact", plan)
        self.assertNotIn("SCAN studio_generatedartifact", plan)
        self.assertIn("SEARCH U0", plan)
