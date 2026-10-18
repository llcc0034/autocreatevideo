"""M8 selection commands: storage fixtures, no model, media playback or network."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from studio.models import (
    ApprovalDecision, ArtifactSelectionDecision, AssemblyJob, Episode, GeneratedArtifact,
    ProductionAssemblyLink, ProductionManualCommand, ProductionRun, StageRun,
)
from studio.production_manual_serializers import ProductionManualActionResultSerializer
from studio.services.generated_artifacts import select_artifact
from studio.services.json_contracts import assign_json_contract
from studio.services.production_artifact_selection_receipts import select_run_artifact_command
from studio.services.production_manual_commands import read_manual_command_result
from studio.services.production_runs import ProductionRunConflict, _StoredAssetFileGuard
from studio.tests import test_production_run_plans as fixtures
from studio.tests import test_production_manual_commands as manual_fixtures


class SelectionFixture:
    root = fixtures.ProductionRunPlanTests.root
    recipe = fixtures.ProductionRunPlanTests.recipe

    def setUp(self):
        fixtures.ProductionRunPlanTests.setUp(self)
        self.run = self.root(plan_source="recipe", recipe_version=self.recipe(), status="running")
        assign_json_contract(self.run, "config_snapshot", {
            "schema_version": 1, "values": {"artifact.max_bytes": 1024 * 1024},
        })
        self.run.save()
        self.stage = StageRun(production_run=self.run, stage_key="media_generation", ordinal=1,
            mode="ai", status="succeeded", idempotency_key=str(uuid4()))
        for field in ("routing", "routing_decision", "config_snapshot", "input_snapshot", "output_snapshot"):
            assign_json_contract(self.stage, field, {"schema_version": 1})
        self.stage.save()
        self.first, self.second = self.candidate(1), self.candidate(2)
        # Receipt/transaction tests isolate candidate verification. The actual
        # file and preparation reader is exercised by the two production-entry
        # tests, not by these deliberately non-media storage fixtures.
        self.enterContext(patch("studio.services.execution_production_candidates.read_production_media_candidate", return_value={}))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("no network in receipt tests")))

    def candidate(self, index):
        # Stored manifest only: this test does not claim media validation.
        artifact = GeneratedArtifact(production_run=self.run, stage_run=self.stage,
            manifest_key=f"isolated:{uuid4()}", variant_group="shot:isolated", variant_index=index,
            kind="video", role="primary_generation_output", status="available",
            storage_backend="local_media", object_key=f"isolated/{index}.mp4", mime_type="video/mp4",
            size_bytes=1, sha256="a" * 64)
        assign_json_contract(artifact, "manifest_metadata", {"schema_version": 1})
        artifact.save()
        return artifact

    def choose(self, artifact=None, **kwargs):
        return select_run_artifact_command(self.run, artifact or self.first,
            **{"actor": self.actor, "request_key": "choose-a", **kwargs})


class ArtifactSelectionReceiptTests(SelectionFixture, TestCase):
    def test_unknown_response_replay_has_stable_serialized_receipt_and_no_writes(self):
        first = self.choose()
        self.assertEqual(set(first), {"id", "action", "created_at", "result_kind", "target_id"})
        self.assertEqual((first["action"], first["result_kind"]), ("select", "artifact_selection"))
        forbidden = Mock(side_effect=AssertionError("replay cannot run the writer"))
        with patch("studio.services.production_runs.select_run_artifact", forbidden), CaptureQueriesContext(connection) as captured:
            replay = self.choose()
        forbidden.assert_not_called()
        self.assertEqual(ProductionManualActionResultSerializer(first).data, ProductionManualActionResultSerializer(replay).data)
        for query in captured:
            self.assertFalse(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")), query["sql"])
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)
        receipt = ProductionManualCommand.objects.get()
        self.assertEqual((receipt.created_by_id, receipt.stage_run_id, receipt.target_id),
            (self.actor.pk, self.stage.pk, first["target_id"]))
        self.assertEqual(read_manual_command_result(receipt), {"kind": "artifact_selection", "id": str(first["target_id"])})
        self.assertIn("INDEX", ProductionManualCommand.objects.filter(run=self.run,
            idempotency_key="choose-a").order_by().values("id").explain().upper())

    def test_a_b_replay_a_keeps_b_current_and_returns_original_decision(self):
        first = self.choose()
        second = self.choose(self.second, request_key="choose-b")
        baseline = list(ArtifactSelectionDecision.objects.values())
        self.assertEqual(self.choose(), first)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual((self.first.status, self.second.status), ("available", "selected"))
        self.assertNotEqual(first["target_id"], second["target_id"])
        self.assertEqual(list(ArtifactSelectionDecision.objects.values()), baseline)

    def test_new_key_same_media_candidate_is_a_new_intent_and_decision(self):
        first = self.choose()
        second = self.choose(request_key="new-intent")
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["target_id"], second["target_id"])
        self.assertEqual(list(ArtifactSelectionDecision.objects.values_list("revision", flat=True)), [1, 2])
        self.assertEqual(ProductionManualCommand.objects.count(), 2)

    def test_same_key_conflicts_on_artifact_notes_material_body_and_authorized_actor(self):
        self.choose()
        staff = get_user_model().objects.create_user(username="selection-staff", is_staff=True)
        for changes in ({"artifact": self.second}, {"notes": "changed"},
                {"material_selection": {"expected_decision_id": None}}, {"actor": staff}):
            with self.subTest(changes=changes), self.assertRaises(ProductionRunConflict) as raised:
                self.choose(**changes)
            self.assertEqual(raised.exception.code, "run_plan_source_conflict")
        self.assertEqual((ProductionManualCommand.objects.count(), ArtifactSelectionDecision.objects.count()), (1, 1))

    def test_permissions_precede_collision_and_no_inactive_or_anonymous_replay(self):
        for already_saved in (False, True):
            if already_saved:
                self.choose()
            for actor in (self.other, None):
                with self.subTest(saved=already_saved, actor=actor), self.assertRaises(PermissionDenied):
                    self.choose(actor=actor, notes="different body")
        self.actor.is_active = False
        with self.assertRaises(PermissionDenied):
            self.choose()

    def test_missing_or_cross_stage_decision_does_not_recreate_a_receipt(self):
        result = self.choose()
        row = ProductionManualCommand.objects.get(pk=result["id"])
        other_stage = deepcopy(self.stage)
        other_stage.pk = uuid4()
        other_stage.stage_key = "other"
        other_stage.ordinal = 2
        other_stage.idempotency_key = str(uuid4())
        other_stage.save()
        ArtifactSelectionDecision.objects.filter(pk=result["target_id"]).update(stage_run=other_stage)
        with self.assertRaises(ValidationError):
            self.choose()
        ArtifactSelectionDecision.objects.filter(pk=result["target_id"]).delete()
        with self.assertRaises(ValidationError):
            read_manual_command_result(row)
        self.assertEqual(ProductionManualCommand.objects.count(), 1)

    def test_receipt_failure_rolls_back_choice_revision_and_current_candidate(self):
        self.choose()
        before = list(ArtifactSelectionDecision.objects.values())
        with patch.object(ProductionManualCommand, "save", side_effect=RuntimeError("receipt failed")):
            with self.assertRaisesMessage(RuntimeError, "receipt failed"):
                self.choose(self.second, request_key="choose-b")
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual((self.first.status, self.second.status), ("selected", "available"))
        self.assertEqual(list(ArtifactSelectionDecision.objects.values()), before)
        self.assertEqual(ProductionManualCommand.objects.count(), 1)
        self.choose(self.second, request_key="choose-b")
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 2)

    def test_invalid_key_failed_candidate_and_manual_run_do_not_write(self):
        for key in (None, "", " ", "x" * 192):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.choose(request_key=key)
        for status in ("invalid", "rejected", "legacy_missing"):
            GeneratedArtifact.objects.filter(pk=self.first.pk).update(status=status)
            with self.subTest(status=status), self.assertRaises(ValidationError):
                self.choose()
        self.run = self.root()
        with self.assertRaises(ValidationError):
            self.choose()
        self.assertFalse(ProductionManualCommand.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_ended_run_and_assembly_freeze_allow_only_original_replay(self):
        original = self.choose()
        for status in ("succeeded", "failed", "cancelled", "cancel_requested"):
            ProductionRun.objects.filter(pk=self.run.pk).update(status=status)
            self.assertEqual(self.choose(), original)
            with self.subTest(status=status), self.assertRaises(ValidationError):
                self.choose(request_key="new-intent")
        ProductionRun.objects.filter(pk=self.run.pk).update(status="running")
        episode = Episode.objects.create(project=self.project, number=1, title="Storage fixture")
        job = AssemblyJob.objects.create(owner=self.actor, project=self.project, episode=episode,
            scope="episode", max_attempts=1)
        ProductionAssemblyLink.objects.create(production_run=self.run, stage_run=self.stage, episode=episode, assembly_job=job)
        self.assertEqual(self.choose(), original)
        with self.assertRaisesMessage(ValidationError, "冻结选片"):
            self.choose(self.second, request_key="after-freeze")

    def test_automatic_selection_remains_automatic_and_creates_no_human_receipt(self):
        select_artifact(self.first, user=self.actor, selection_mode="automatic")
        original = ArtifactSelectionDecision.objects.get()
        self.assertEqual((original.selection_mode, original.selected_by_id), ("automatic", None))
        self.assertFalse(ProductionManualCommand.objects.exists())
        self.choose(self.second)
        original.refresh_from_db()
        self.assertEqual((original.selection_mode, original.selected_by_id), ("automatic", None))
        self.assertFalse(ApprovalDecision.objects.exists())


class SelectionCommitFailureTests(SelectionFixture, TransactionTestCase):
    def test_database_commit_failure_rolls_back_receipt_choice_and_nested_guard_file(self):
        from studio.services.production_runs import select_run_artifact
        created_path = Path(settings.MEDIA_ROOT) / f"isolated-{uuid4()}.bin"
        def writer(*args, **kwargs):
            # Isolated file-producing suboperation under the real writer. The
            # full material/repreparation path is covered in its separate test.
            with _StoredAssetFileGuard() as guard:
                guard.track_path(created_path)
                created_path.write_bytes(b"isolated transaction test")
                return select_run_artifact(*args, **kwargs)
        with patch("studio.services.production_runs.select_run_artifact", side_effect=writer), patch.object(
                connection, "commit", side_effect=DatabaseError("isolated commit failure")):
            with self.assertRaisesMessage(DatabaseError, "isolated commit failure"):
                self.choose()
        self.assertFalse(created_path.exists())
        self.assertFalse(ProductionManualCommand.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.first.refresh_from_db()
        self.assertEqual(self.first.status, "available")
        self.assertIsNone(_StoredAssetFileGuard._current.get())


class SharedManualReceiptBoundaryTests(TestCase):
    root = manual_fixtures.ProductionManualCommandTests.root
    recipe = manual_fixtures.ProductionManualCommandTests.recipe
    setUp = manual_fixtures.ProductionManualCommandTests.setUp
    operation = manual_fixtures.ProductionManualCommandTests.operation
    execute = manual_fixtures.ProductionManualCommandTests.execute

    def test_shared_replay_rejects_a_different_authorized_actor(self):
        self.execute()
        self.other.is_staff = True
        self.other.save(update_fields=("is_staff",))
        with self.assertRaises(ValidationError) as raised:
            self.execute(actor=self.other)
        self.assertEqual(raised.exception.code, "run_plan_source_conflict")
        self.assertEqual(ProductionManualCommand.objects.count(), 1)

    def test_recipe_dispatcher_does_not_accept_arbitrary_artifact_selection_stages(self):
        self.run = self.root(plan_source="recipe", recipe_version=self.recipe())
        forbidden = Mock(side_effect=AssertionError("recipe must not enter arbitrary manual adapters"))
        for stage_key in ("media_generation", "artifact_selection", "bible_image_generation", "script_review"):
            with self.subTest(stage=stage_key), self.assertRaisesMessage(ValidationError, "配方仅可复用"):
                self.execute({**self.body, "action": "select", "stage_key": stage_key}, execute=forbidden)
        forbidden.assert_not_called()
        self.assertFalse(ProductionManualCommand.objects.exists())
