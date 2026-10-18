"""M5-03 internal relation phase, not switched entry-point acceptance.

Original M2 import/manual adoption and M3 human or recipe-policy selection are
real isolated writers. Episode/Shot body creation is an explicit fixture until
the encompassing writer is switched. No network, media or live DB operations.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import (ApprovalDecision, Asset, BibleVariant, Episode, ExecutionPreparation,
    GeneratedArtifact, GenerationAttempt, ProductionManualCommand, ProductionRun, Shot,
    ShotCharacterRelation, ShotReferenceUse, StageRun)
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.json_contracts import json_contract
from studio.services.production_runs import advance_production_run
from studio.services.production_storyboard_materialization import _declared_dialogue, _materialize_declared_relations
from studio.services.script_storyboard_plan import read_storyboard_source_plan
from studio.tests import test_bible_policy_continuation as policy_fixtures
from studio.tests import test_production_manual_bible as manual_fixtures
from studio.tests.structured_bible_fixtures import confirmed_structured_bible_source


def create_body_fixture(testcase, plan):
    """Only the targets consumed by the relation phase; not a product writer."""
    result = {}
    for source_episode in plan.episodes:
        episode = Episode.objects.create(project=testcase.project, number=source_episode["number"], title="Isolated body fixture")
        for source in source_episode["shots"]:
            shot = Shot.objects.create(episode=episode, number=source["number"],
                dialogue=_declared_dialogue(source), duration_ms=2000, media_type="video", include_audio=True)
            result[episode.number, shot.number] = shot.pk
    return result


class DeclaredDialogueTests(SimpleTestCase):
    def test_projection_preserves_optional_absence_empty_zero_false_and_original_text(self):
        for dialogue in (None, {"speaker_entity_key": None, "text": "", "start_ms": 0, "end_ms": 0, "is_voiceover": False},
                {"speaker_entity_key": "person", "text": " 原文\nBonjour。 ", "delivery": " 轻声 ", "is_voiceover": True}):
            shot = {"body": {"dialogue": deepcopy(dialogue)} if dialogue is not None else {},
                "relations": {"speaker": {"speaker_id": "S2"} if dialogue and dialogue["text"] else None}}
            before = deepcopy(shot)
            result = _declared_dialogue(shot)
            expected = [] if not dialogue or not dialogue["text"] else [{
                ("voiceover" if key == "is_voiceover" else key): value
                for key, value in dialogue.items() if key != "speaker_entity_key"}]
            if dialogue and dialogue["text"]: expected[0]["speaker_id"] = "S2"
            self.assertEqual(result, expected)
            if result: result[0]["text"] = "changed by caller"
            self.assertEqual(shot, before)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class StoryboardRelationMaterializationTests(TestCase):
    create = manual_fixtures.ProductionManualBibleTests.create
    post = manual_fixtures.ProductionManualBibleTests.post
    read = manual_fixtures.ProductionManualBibleTests.read
    adopt_source = manual_fixtures.ProductionManualBibleTests.adopt_source
    approval = manual_fixtures.ProductionManualBibleTests.approval

    def setUp(self):
        def source(testcase):
            entities = [{"key": key, "kind": kind, "name": name,
                "primary": {"reference_name": name + "·主图", "anchor": "明确主图"}, "variants": []}
                for key, kind, name in (("traveler", "character", "旅人"), ("friend", "character", "同行者"),
                    ("room", "scene", "房间"), ("box", "prop", "盒子"))]
            entities[0]["variants"] = [{"key": "coat", "name": "外套", "reference_name": "旅人·外套", "anchor": "外套差异"}]
            episodes = [{"episode": 4, "shots": [
                {"shot": 8, "scene_key": "room", "character_keys": [], "prop_keys": ["box"],
                    "dialogue": {"speaker_entity_key": "traveler", "text": "原对白  Bonjour。\n不翻译。",
                        "delivery": " 轻声 ", "is_voiceover": True, "start_ms": 300, "end_ms": 1500},
                    "references": [{"use_key": "coat", "entity_key": "traveler", "variant_key": "coat",
                        "media_kind": "image", "purpose": "appearance"},
                        {"use_key": "motion", "entity_key": "traveler", "variant_key": "coat",
                            "media_kind": "video", "purpose": "configured-motion"}]},
                {"shot": 2, "character_keys": ["friend", "traveler"], "prop_keys": [],
                    "dialogue": {"speaker_entity_key": "traveler", "text": "在画面里说话", "is_voiceover": False},
                    "references": [{"use_key": "base", "entity_key": "traveler", "variant_key": None,
                        "media_kind": "audio", "purpose": "configured-voice"}]}]},
                {"episode": 9, "shots": [{"shot": 3, "character_keys": [], "references": []}]}]
            return confirmed_structured_bible_source(testcase, entities=entities, episodes=episodes)
        with patch("studio.tests.test_production_manual_bible.confirmed_bible_source", side_effect=source):
            manual_fixtures.ProductionManualBibleTests.setUp(self)
        self.adopt_source()
        self.assertEqual(self.post(self.approval()).status_code, 201)
        self.stage = self.run.stages.get(stage_key="storyboard")
        self.plan = read_storyboard_source_plan(run=self.run, stage=self.stage)
        self.basis_sha256 = json_contract(self.plan.basis).sha256
        self.coordinates = create_body_fixture(self, self.plan)
        self.no_network = patch("requests.sessions.Session.request", side_effect=AssertionError("No external calls"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def write(self, **overrides):
        return _materialize_declared_relations(**{"run": self.run, "stage": self.stage,
            "expected_basis_sha256": self.basis_sha256, "shots_by_coordinate": self.coordinates, **overrides})

    def facts(self):
        return {model.__name__: list(model.objects.order_by("pk").values()) for model in (
            ProductionRun, StageRun, ApprovalDecision, ProductionManualCommand, ExecutionPreparation,
            GenerationAttempt, GeneratedArtifact, Asset)}

    def assert_pristine(self):
        self.assertFalse(ShotCharacterRelation.objects.exists())
        self.assertFalse(ShotReferenceUse.objects.exists())
        self.assertFalse(Shot.objects.exclude(scene_id=None).exists())
        self.assertFalse(Shot.props.through.objects.exists())

    def test_exact_relations_offscreen_and_visible_speakers_order_and_pending_media(self):
        before = self.facts()
        original_times = dict(Shot.objects.values_list("pk", "updated_at"))
        body_fields = ("pk", "dialogue", "duration_ms", "media_type", "include_audio", "metadata")
        authored = list(Shot.objects.order_by("pk").values(*body_fields))
        self.assertTrue(self.write())
        offscreen = Shot.objects.get(pk=self.coordinates[4, 8])
        visible = Shot.objects.get(pk=self.coordinates[4, 2])
        first, second = self.plan.episodes[0]["shots"]
        self.assertEqual(str(offscreen.scene_id), first["relations"]["scene"]["entity_id"])
        self.assertEqual([str(pk) for pk in offscreen.props.values_list("pk", flat=True)],
            [row["entity_id"] for row in first["relations"]["props"]])
        self.assertEqual(list(offscreen.character_relations.values_list("role", "position")), [("speaker", 0)])
        self.assertEqual(list(visible.character_relations.filter(role="visible").values_list("position", flat=True)), [0, 1])
        self.assertEqual([str(pk) for pk in visible.character_relations.filter(role="visible").values_list("character_id", flat=True)],
            [row["entity_id"] for row in second["relations"]["visible_characters"]])
        self.assertEqual(str(visible.character_relations.get(role="speaker").character_id), second["relations"]["speaker"]["entity_id"])
        self.assertEqual(list(offscreen.reference_uses.values_list("use_key", "position", "media_kind", "purpose")),
            [("coat", 0, "image", "appearance"), ("motion", 1, "video", "configured-motion")])
        self.assertEqual([str(pk) for pk in offscreen.reference_uses.values_list("setting_revision_id", flat=True)],
            [row["setting_revision_id"] for row in first["relations"]["uses"]])
        self.assertNotEqual(offscreen.reference_uses.first().setting_revision_id, visible.reference_uses.get().setting_revision_id)
        for use in ShotReferenceUse.objects.all():
            self.assertEqual((use.source_mode, use.reference_version_id, use.asset_id, use.asset_sha256), ("", None, None, ""))
            use.full_clean()
        for relation in ShotCharacterRelation.objects.all(): relation.full_clean()
        self.assertFalse({"characters", "reference_assets", "reference_bindings"} &
            {field.name for field in Shot._meta.get_fields()})
        empty = Shot.objects.get(pk=self.coordinates[9, 3])
        self.assertIsNone(empty.scene_id)
        self.assertFalse(empty.character_relations.exists())
        self.assertFalse(empty.reference_uses.exists())
        self.assertEqual(empty.updated_at, original_times[empty.pk])
        self.assertGreater(offscreen.updated_at, original_times[offscreen.pk])
        self.assertGreater(visible.updated_at, original_times[visible.pk])
        self.assertEqual(list(Shot.objects.order_by("pk").values(*body_fields)), authored)
        self.assertEqual(self.facts(), before)

    def test_exact_replay_no_mutations_and_uses_unique_indexes_without_large_bodies(self):
        self.write()
        original = list(ShotReferenceUse.objects.order_by("pk").values())
        with CaptureQueriesContext(connection) as queries:
            self.assertFalse(self.write())
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        for table in ("studio_shotcharacterrelation", "studio_shotreferenceuse"):
            reads = [q["sql"] for q in queries if f'FROM "{table}"' in q["sql"]]
            self.assertEqual(len(reads), 1)
            self.assertNotIn("SELECT *", reads[0])
        for model in (ShotCharacterRelation, ShotReferenceUse):
            plan = model.objects.filter(shot_id__in=self.coordinates.values()).order_by().explain()
            if connection.vendor == "sqlite": self.assertRegex(plan, "SEARCH .* USING INDEX")
        self.assertEqual(list(ShotReferenceUse.objects.order_by("pk").values()), original)

    def test_stale_expected_basis_and_changed_review_reject_before_mutation(self):
        with self.assertRaises(ValidationError): self.write(expected_basis_sha256="0" * 64)
        variant = BibleVariant.objects.get(source_variant_key="coat")
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(variant.revisions.get(revision=1)), "appearance_anchor": "明确更新"})
        with self.assertRaises(ValidationError): self.write()
        self.assert_pristine()

    def test_missing_extra_swapped_duplicate_and_non_integer_coordinates_reject(self):
        a, b = self.coordinates[4, 8], self.coordinates[4, 2]
        invalid = [{(4, 8): a}, {**self.coordinates, (4, 99): uuid4()},
            {**self.coordinates, (4, 8): b, (4, 2): a}, {**self.coordinates, (4, 2): a},
            {(float(key[0]), key[1]): pk for key, pk in self.coordinates.items()},
            {**self.coordinates, (4, 8): uuid4()}, {**self.coordinates, (4, 8): "not-an-id"}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError): self.write(shots_by_coordinate=value)
            self.assert_pristine()

    def test_cross_project_targets_and_mutated_in_memory_run_cannot_replace_database_ownership(self):
        other = type(self.project).objects.create(owner=self.actor, title="Another isolated project",
            **{key: getattr(self.project, key) for key in ("language", "aspect_ratio", "width", "height", "fps",
                "default_shot_duration_ms", "continuity_seed")})
        episode = Episode.objects.create(project=other, number=4, title="Foreign")
        shot = Shot.objects.create(episode=episode, number=8, dialogue=_declared_dialogue(self.plan.episodes[0]["shots"][0]),
            duration_ms=2000, media_type="video", include_audio=True)
        self.run.project_id = other.pk
        with self.assertRaises(ValidationError): self.write(shots_by_coordinate={**self.coordinates, (4, 8): shot.pk})
        self.assert_pristine()

    def test_foreign_or_wrong_stage_and_missing_real_review_reject(self):
        for stage in (self.run.stages.get(stage_key="script_input"), StageRun(id=uuid4())):
            with self.subTest(stage=stage.pk), self.assertRaises(ValidationError): self.write(stage=stage)
        # Explicit isolated corruption, not a product deletion or fabricated review.
        self.run.stages.filter(stage_key="bible_review").update(status="pending")
        with self.assertRaises(ValidationError): self.write()
        self.assert_pristine()

    def test_dialogue_text_timing_delivery_and_speaker_cannot_be_repaired_by_relation_writer(self):
        shot = Shot.objects.get(pk=self.coordinates[4, 8])
        original = deepcopy(shot.dialogue)
        for key, value in (("text", "different"), ("start_ms", 0), ("speaker_id", "S999"), ("delivery", ""), ("voiceover", False)):
            changed = deepcopy(original); changed[0][key] = value
            Shot.objects.filter(pk=shot.pk).update(dialogue=changed)
            with self.subTest(key=key), self.assertRaises(ValidationError): self.write()
            self.assert_pristine()
            shot.refresh_from_db(); self.assertEqual(shot.dialogue, changed)

    def test_existing_explicit_relations_are_not_cleared_or_dual_written(self):
        shot = Shot.objects.get(pk=self.coordinates[4, 8])
        character_id = self.plan.episodes[0]["shots"][0]["relations"]["speaker"]["entity_id"]
        row = ShotCharacterRelation.objects.create(shot=shot, character_id=character_id, role="speaker", position=0)
        before = list(ShotCharacterRelation.objects.values())
        with self.assertRaises(ValidationError): self.write()
        self.assertEqual(list(ShotCharacterRelation.objects.values()), before)
        self.assertTrue(ShotCharacterRelation.objects.filter(pk=row.pk).exists())
        self.assertFalse(ShotReferenceUse.objects.exists())

    def test_partial_relations_and_subsequent_selection_are_not_overwritten(self):
        self.write()
        first = ShotReferenceUse.objects.get(use_key="coat")
        first.source_mode = "existing"; first.save(update_fields=("source_mode",))
        before = list(ShotReferenceUse.objects.order_by("pk").values())
        with self.assertRaises(ValidationError): self.write()
        self.assertEqual(list(ShotReferenceUse.objects.order_by("pk").values()), before)
        first.source_mode = ""; first.save(update_fields=("source_mode",))
        ShotCharacterRelation.objects.filter(role="speaker").delete()  # Isolated missing-row fixture.
        before = list(ShotCharacterRelation.objects.order_by("pk").values())
        with self.assertRaises(ValidationError): self.write()
        self.assertEqual(list(ShotCharacterRelation.objects.order_by("pk").values()), before)

    def test_relation_insert_failure_rolls_back_scene_props_and_characters(self):
        before = self.facts()
        original_shots = list(Shot.objects.order_by("pk").values())
        with patch.object(ShotReferenceUse.objects, "bulk_create", side_effect=ValidationError("isolated failure")):
            with self.assertRaises(ValidationError): self.write()
        self.assert_pristine()
        self.assertEqual(list(Shot.objects.order_by("pk").values()), original_shots)
        self.assertEqual(self.facts(), before)
        self.assertTrue(self.write())

    def test_outer_receipt_failure_rolls_back_successful_relation_phase(self):
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                self.write()
                raise ValidationError("Isolated caller receipt failure")
        self.assert_pristine()

    def test_last_shot_conflict_is_detected_before_first_relation_write(self):
        last = Shot.objects.get(pk=self.coordinates[9, 3])
        last.dialogue = [{"text": "Not in the confirmed source"}]
        last.save(update_fields=("dialogue",))
        with CaptureQueriesContext(connection) as queries, self.assertRaises(ValidationError): self.write()
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        self.assert_pristine()


class RecipeStoryboardRelationMaterializationTests(TestCase):
    root = policy_fixtures.BiblePolicyContinuationTests.root
    recipe = policy_fixtures.BiblePolicyContinuationTests.recipe
    build = policy_fixtures.BiblePolicyContinuationTests.build

    def setUp(self): policy_fixtures.BiblePolicyContinuationTests.setUp(self)

    def test_same_relation_phase_accepts_original_policy_without_fabricating_human_approval(self):
        run = advance_production_run(self.build(), automatic=True)
        stage = run.stages.get(stage_key="storyboard")
        plan = read_storyboard_source_plan(run=run, stage=stage)
        coordinates = create_body_fixture(self, plan)
        original = list(ApprovalDecision.objects.order_by("pk").values())
        stages = list(run.stages.order_by("pk").values())
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No external calls")):
            self.assertTrue(_materialize_declared_relations(run=run, stage=stage,
                expected_basis_sha256=json_contract(plan.basis).sha256, shots_by_coordinate=coordinates))
        self.assertEqual(ShotCharacterRelation.objects.get().role, "visible")
        self.assertEqual(ShotReferenceUse.objects.get().source_mode, "")
        self.assertEqual(list(ApprovalDecision.objects.order_by("pk").values()), original)
        self.assertEqual(list(run.stages.order_by("pk").values()), stages)
        self.assertFalse(ApprovalDecision.objects.filter(actor="human").exists())
        self.assertFalse(GenerationAttempt.objects.exists())
