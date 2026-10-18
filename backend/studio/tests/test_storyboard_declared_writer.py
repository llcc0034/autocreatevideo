"""M5 atomic creation phase, not yet public manual/recipe receipt acceptance.

Only M2/M3 fixtures and isolated DB facts are used. No real generation,
approval of live data or fabricated script parsing/source field identities.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import (ApprovalDecision, Asset, Episode, ExecutionPreparation, GeneratedArtifact, GenerationAttempt,
    ProductionRun, ScriptRevisionStructure, SeriesProject, Shot, ShotCharacterRelation, ShotReferenceUse, ShotScriptSource, StageRun)
from studio.services.json_contracts import assign_json_contract
from studio.services.production_runs import advance_production_run
from studio.services.production_storyboard_materialization import (
    materialize_declared_storyboard, read_storyboard_materialization_plan)
from studio.services.script_storyboard_fields import read_shot_source_fields
from studio.services.security_boundaries import boundary_int
from studio.tests import test_bible_policy_continuation as recipe_fixtures
from studio.tests import test_production_manual_bible as manual_fixtures
from studio.tests import test_storyboard_materialization_plan as plan_fixtures
from studio.tests.test_storyboard_source_fields import saved_source


def no_dml(queries):
    return not any(row["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for row in queries)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class DeclaredStoryboardWriterTests(TestCase):
    create = plan_fixtures.ManualStoryboardMaterializationPlanTests.create
    post = plan_fixtures.ManualStoryboardMaterializationPlanTests.post
    read = plan_fixtures.ManualStoryboardMaterializationPlanTests.read
    adopt_source = plan_fixtures.ManualStoryboardMaterializationPlanTests.adopt_source
    approval = plan_fixtures.ManualStoryboardMaterializationPlanTests.approval
    setUp = plan_fixtures.ManualStoryboardMaterializationPlanTests.setUp

    def write(self, digest=None):
        if digest is None:
            digest = read_storyboard_materialization_plan(run=self.run, stage=self.stage).contract.sha256
        return materialize_declared_storyboard(run=self.run, stage=self.stage, expected_contract_sha256=digest)

    def targets(self):
        return {model.__name__: list(model.objects.order_by("pk").values()) for model in (
            Episode, Shot, ShotScriptSource, ShotCharacterRelation, ShotReferenceUse, Shot.props.through)}

    def other_facts(self):
        return {model.__name__: list(model.objects.order_by("pk").values()) for model in (
            ProductionRun, StageRun, ApprovalDecision, GenerationAttempt, ExecutionPreparation,
            GeneratedArtifact, Asset, ScriptRevisionStructure)}

    def test_creation_keeps_declared_numbers_timings_real_sources_without_fixed_creative_columns(self):
        before = self.other_facts()
        manuscript = self.project.master_script
        result = self.write()
        self.assertTrue(result.created)
        self.assertEqual([row.number for row in result.episodes], [4])
        self.assertEqual(list(result.shots_by_coordinate), [(4, 8), (4, 2), (4, 9)])
        voiceover = Shot.objects.get(pk=result.shots_by_coordinate[4, 8])
        self.assertEqual(voiceover.dialogue[0]["text"], " 原文\nBonjour。 ")
        self.assertEqual(voiceover.dialogue[0]["start_ms"], 0)
        self.assertEqual(list(voiceover.character_relations.values_list("role", flat=True)), ["speaker"])
        self.assertFalse(voiceover.character_relations.filter(role="visible").exists())
        self.assertEqual((voiceover.duration_ms, voiceover.episode_start_ms), (5000, None))
        timed = Shot.objects.get(pk=result.shots_by_coordinate[4, 2])
        self.assertEqual((timed.duration_ms, timed.episode_start_ms, timed.episode_end_ms), (6000, 0, 6000))
        for shot in Shot.objects.all():
            self.assertFalse({"narrative", "detailed_description", "camera", "action",
            "sound_effects", "music", "continuity_notes"} & {field.name for field in Shot._meta.get_fields()})
            self.assertEqual(shot.script_source.structure_id, self.revision.pk)
            self.assertEqual(shot.script_source.stage_id, self.stage.pk)
            shot.script_source.full_clean()
        self.assertEqual(self.other_facts(), before)
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, manuscript)

    def test_exact_replay_is_zero_write_and_keeps_all_row_ids_and_timestamps(self):
        result = self.write()
        before = self.targets(), self.other_facts()
        with CaptureQueriesContext(connection) as queries:
            replay = self.write()
        self.assertFalse(replay.created)
        self.assertEqual(replay.shots_by_coordinate, result.shots_by_coordinate)
        self.assertTrue(no_dml(queries))
        self.assertEqual((self.targets(), self.other_facts()), before)

    def test_frozen_config_change_rejects_stale_digest_before_any_target_write(self):
        proposed = read_storyboard_materialization_plan(run=self.run, stage=self.stage)
        self.run.refresh_from_db()
        config = deepcopy(self.run.config_snapshot)
        config["values"]["production.default_include_audio"] = not config["values"]["production.default_include_audio"]
        assign_json_contract(self.run, "config_snapshot", config, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        before = self.targets()
        with self.assertRaises(ValidationError) as caught, CaptureQueriesContext(connection) as queries:
            self.write(proposed.contract.sha256)
        self.assertEqual(caught.exception.code, "storyboard_source_conflict")
        self.assertTrue(no_dml(queries))
        self.assertEqual(self.targets(), before)

    def test_relation_failure_rolls_back_created_episodes_and_shots(self):
        before = self.targets(), self.other_facts()
        with patch("studio.services.production_storyboard_materialization._materialize_declared_relations",
                side_effect=ValidationError("Isolated relationship failure")), self.assertRaises(ValidationError):
            self.write()
        self.assertEqual((self.targets(), self.other_facts()), before)

    def test_changed_original_structure_is_not_reparsed_or_written_from_cached_plan(self):
        proposed = read_storyboard_materialization_plan(run=self.run, stage=self.stage)
        ScriptRevisionStructure.objects.filter(pk=self.revision.pk).update(document_sha256="0" * 64)
        before = self.targets(), self.other_facts()
        with self.assertRaises(ValidationError), CaptureQueriesContext(connection) as queries:
            self.write(proposed.contract.sha256)
        self.assertTrue(no_dml(queries))
        self.assertEqual((self.targets(), self.other_facts()), before)

    def test_source_binding_failure_rolls_back_bodies_and_all_typed_relations(self):
        before = self.targets(), self.other_facts()
        with patch.object(ShotScriptSource.objects, "bulk_create", side_effect=IntegrityError("Isolated source failure")), \
                self.assertRaises(IntegrityError):
            self.write()
        self.assertEqual((self.targets(), self.other_facts()), before)

    def test_outer_command_receipt_failure_rolls_back_every_target(self):
        before = self.targets(), self.other_facts()
        with self.assertRaisesRegex(ValueError, "receipt"):
            with transaction.atomic():
                self.write()
                raise ValueError("isolated receipt failure")
        self.assertEqual((self.targets(), self.other_facts()), before)

    def test_partial_binding_or_relation_is_never_filled_on_replay(self):
        self.write()
        for model in (ShotScriptSource, ShotCharacterRelation, ShotReferenceUse):
            with self.subTest(model=model.__name__), transaction.atomic():
                model.objects.all().delete()
                before = self.targets()
                with self.assertRaises(ValidationError), CaptureQueriesContext(connection) as queries:
                    self.write()
                self.assertTrue(no_dml(queries))
                self.assertEqual(self.targets(), before)
                transaction.set_rollback(True)

    def test_existing_unbound_or_later_edited_body_is_not_overwritten(self):
        episode = Episode.objects.create(project=self.project, number=4, title="Existing user episode")
        before = self.targets()
        with self.assertRaises(ValidationError): self.write()
        self.assertEqual(self.targets(), before)
        episode.delete()  # Only the explicitly created isolated fixture.
        self.write()
        Shot.objects.all().update(title="User change")
        before = self.targets()
        with self.assertRaises(ValidationError): self.write()
        self.assertEqual(self.targets(), before)

    def test_binding_uniqueness_positive_coordinates_and_protected_source(self):
        result = self.write()
        source = ShotScriptSource.objects.get(shot_id=result.shots_by_coordinate[4, 8])
        with self.assertRaises(ProtectedError):
            ScriptRevisionStructure.objects.get(pk=self.revision.pk).delete()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            ShotScriptSource.objects.filter(shot_id=result.shots_by_coordinate[4, 2]).update(
                episode_number=source.episode_number, shot_number=source.shot_number)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            ShotScriptSource.objects.filter(pk=source.pk).update(shot_number=0)
        source.document_sha256 = "0" * 64
        with self.assertRaises(ValidationError): source.full_clean()
        source.refresh_from_db()
        source.stage = self.run.stages.get(stage_key="script_input")
        with self.assertRaises(ValidationError): source.full_clean()

    def test_source_cannot_be_bound_to_another_projects_shot(self):
        result = self.write()
        other = SeriesProject.objects.create(owner=self.actor, title="Other isolated project", width=768, height=1024,
            fps=24, default_shot_duration_ms=5000, continuity_seed=1)
        episode = Episode.objects.create(project=other, number=4, title="Unrelated")
        shot = Shot.objects.create(episode=episode, number=8, duration_ms=5000,
            media_type="video", include_audio=False)
        original = ShotScriptSource.objects.get(shot_id=result.shots_by_coordinate[4, 8])
        invalid = ShotScriptSource(shot=shot, structure=original.structure, stage=original.stage,
            episode_number=4, shot_number=8, document_sha256=original.document_sha256,
            materialization_sha256=original.materialization_sha256)
        with self.assertRaises(ValidationError): invalid.clean()
        # A damaged imported binding is not accepted by either the reader or
        # the writer; this mutation only arranges an isolated corruption test.
        ShotScriptSource.objects.filter(pk=original.pk).update(shot_id=shot.pk)
        before = self.targets(), self.other_facts()
        with self.assertRaises(ValidationError):
            read_shot_source_fields(shot_id=shot.pk, project_id=other.pk, max_bytes=1_000_000)
        with self.assertRaises(ValidationError), CaptureQueriesContext(connection) as queries:
            self.write()
        self.assertTrue(no_dml(queries))
        self.assertEqual((self.targets(), self.other_facts()), before)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class BoundStoryboardFieldTests(TestCase):
    create = manual_fixtures.ProductionManualBibleTests.create
    post = manual_fixtures.ProductionManualBibleTests.post
    read = manual_fixtures.ProductionManualBibleTests.read
    adopt_source = manual_fixtures.ProductionManualBibleTests.adopt_source
    approval = manual_fixtures.ProductionManualBibleTests.approval

    def setUp(self):
        with patch("studio.tests.test_production_manual_bible.confirmed_bible_source", side_effect=saved_source):
            manual_fixtures.ProductionManualBibleTests.setUp(self)
        self.adopt_source()
        self.assertEqual(self.post(self.approval()).status_code, 201)
        self.stage = self.run.stages.get(stage_key="storyboard")
        proposed = read_storyboard_materialization_plan(run=self.run, stage=self.stage)
        self.result = materialize_declared_storyboard(run=self.run, stage=self.stage, expected_contract_sha256=proposed.contract.sha256)
        self.shot_id = self.result.shots_by_coordinate[1, 1]
        self.original_fields = proposed.source.episodes[0]["shots"][0]["source_fields"]
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("No network"))
        network.start()
        self.addCleanup(network.stop)

    def fields(self, **changes):
        return read_shot_source_fields(**{"shot_id": self.shot_id, "project_id": self.project.pk, "max_bytes": 1_000_000, **changes})

    def test_bound_detail_reads_exact_saved_fields_without_current_registry_or_reparse(self):
        with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                side_effect=AssertionError("No reparse")), CaptureQueriesContext(connection) as queries:
            detail = self.fields()
        self.assertEqual(detail["fields"], self.original_fields)
        self.assertTrue(no_dml(queries))
        self.assertFalse(any(table in row["sql"].lower() for row in queries for table in (
            '"studio_fielddefinition', '"studio_datastructure', '"studio_businessoutput')))
        source_query = next(row["sql"] for row in queries if 'FROM "studio_shotscriptsource"' in row["sql"])
        self.assertNotIn('"document"', source_query)
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + source_query)
            self.assertTrue(any("SEARCH" in row[3] and "studio_shotscriptsource" in row[3] for row in cursor.fetchall()))
        self.assertEqual(next(row["value"] for row in detail["fields"] if row["value_path"][-1] == "tone"), "紧张")
        detail["fields"][0]["value"] = "caller mutation"
        self.assertEqual(self.fields()["fields"], self.original_fields)

    def test_changed_display_numbers_manuscript_or_run_selection_do_not_change_original_fields(self):
        Shot.objects.filter(pk=self.shot_id).update(number=9)
        self.project.master_script = "New unrelated manuscript"
        self.project.save(update_fields=("master_script",))
        ProductionRun.objects.filter(pk=self.run.pk).update(script_revision=None)
        self.assertEqual(self.fields()["fields"], self.original_fields)

    def test_new_confirmed_revision_only_changes_writer_when_run_source_changes(self):
        original = self.fields()
        newer = saved_source(self)
        self.assertNotEqual(newer.pk, self.revision.pk)
        self.assertEqual(self.fields(), original)
        before = list(ShotScriptSource.objects.values()), list(Shot.objects.values())
        with CaptureQueriesContext(connection) as queries:
            replay = materialize_declared_storyboard(run=self.run, stage=self.stage,
                expected_contract_sha256=original["source"]["materialization_sha256"])
        self.assertFalse(replay.created)
        self.assertTrue(no_dml(queries))
        # A different confirmed source alone does not change this Run. This
        # isolated pointer mutation now exercises conflict with its original
        # adoption receipt, not a fabricated second human adoption.
        ProductionRun.objects.filter(pk=self.run.pk).update(script_revision=newer)
        self.assertEqual(self.fields(), original)
        with self.assertRaises(ValidationError):
            materialize_declared_storyboard(run=self.run, stage=self.stage,
                expected_contract_sha256=original["source"]["materialization_sha256"])
        self.assertEqual((list(ShotScriptSource.objects.values()), list(Shot.objects.values())), before)

    def test_wrong_project_missing_binding_or_damaged_source_fails_closed(self):
        with self.assertRaises(ValidationError): self.fields(project_id=uuid4())
        with self.assertRaises(ValidationError): self.fields(shot_id=uuid4())
        before = list(ShotScriptSource.objects.values())
        with self.assertRaises(ValidationError): self.fields(max_bytes=64)
        self.assertEqual(list(ShotScriptSource.objects.values()), before)
        ShotScriptSource.objects.filter(pk=self.shot_id).update(document_sha256="0" * 64)
        with self.assertRaises(ValidationError): self.fields()


class RecipeDeclaredStoryboardWriterTests(TestCase):
    root = recipe_fixtures.BiblePolicyContinuationTests.root
    recipe = recipe_fixtures.BiblePolicyContinuationTests.recipe
    build = recipe_fixtures.BiblePolicyContinuationTests.build

    def test_same_atomic_writer_uses_original_recipe_policy_without_inventing_human_review(self):
        with patch("studio.tests.test_bible_policy_continuation.confirmed_structured_bible_source",
                side_effect=lambda testcase, **_: saved_source(testcase)):
            recipe_fixtures.BiblePolicyContinuationTests.setUp(self)
        run = advance_production_run(self.build(), automatic=True)
        stage = run.stages.get(stage_key="storyboard")
        proposed = read_storyboard_materialization_plan(run=run, stage=stage)
        before = list(ApprovalDecision.objects.values()), list(StageRun.objects.values())
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No external calls")):
            result = materialize_declared_storyboard(run=run, stage=stage, expected_contract_sha256=proposed.contract.sha256)
        self.assertEqual(len(result.shots_by_coordinate), 4)
        self.assertEqual(list(ApprovalDecision.objects.values()), before[0])
        self.assertEqual(list(StageRun.objects.values()), before[1])
        self.assertEqual(ApprovalDecision.objects.get().actor, "recipe_policy")
        self.assertTrue(ShotScriptSource.objects.filter(stage=stage).exists())
        self.assertFalse(GenerationAttempt.objects.exists())
