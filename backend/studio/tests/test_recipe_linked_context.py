"""Approved Shot + Bible facts through the real structured mapping path."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import (ApprovalDecision, CharacterBible, SceneBible, PropBible, ProductionRecipeVersion,
    ProductionRun, StageRun, ExecutionPreparation, GenerationAttempt, ShotCharacterRelation)
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_contracts import validate_recipe_input_sources
from studio.services.production_recipe_media_preparations import resolve_recipe_media_sources
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_runs import _formal_snapshot, _save_stage_snapshot
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.production_reviewed_content import read_reviewed_bible, read_reviewed_storyboard
from studio.tests import test_production_shot_context as fixtures


class LinkedContextTests(TestCase):
    approve = fixtures.ShotContextTests.approve
    reapprove = fixtures.ShotContextTests.reapprove

    def setUp(self):
        fixtures.ShotContextTests.setUp(self)
        self.first = CharacterBible.objects.create(project=self.project, slug="first", stable_speaker_id="S1",
            name="甲", voice_profile="沉稳低音")
        self.voice = CharacterBible.objects.create(project=self.project, slug="voice", stable_speaker_id="S2",
            name="乙", voice_profile="轻柔高音", metadata={"visual_reference_policy": {"schema_version": 1, "mode": "exclude"}})
        self.unlinked = CharacterBible.objects.create(project=self.project, slug="outside", stable_speaker_id="S3", name="无关人物")
        self.scene = SceneBible.objects.create(project=self.project, slug="room", name="房间")
        self.prop = PropBible.objects.create(project=self.project, slug="cup", name="茶杯")
        ShotCharacterRelation.objects.bulk_create([
            ShotCharacterRelation(shot=self.shot, character=self.first, role="visible", position=0),
            ShotCharacterRelation(shot=self.shot, character=self.voice, role="speaker", position=0),
            ShotCharacterRelation(shot=self.shot, character=self.first, role="speaker", position=1),
        ])
        self.shot.props.set([self.prop]); self.shot.scene = self.scene
        self.shot.dialogue = [{"speaker_id": "S2", "text": "先说", "voiceover": True},
            {"speaker_id": "S1", "text": "后说", "start_ms": 100, "end_ms": 900}]
        self.shot.save(update_fields=("scene", "dialogue"))
        recipe = ProductionRecipeVersion(recipe_key="linked-context", version=1, name="Linked content fixture", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "bible_review", "mode": "review", "approval": "required"},
            {"key": "storyboard_review", "mode": "review", "approval": "required"},
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "required"}]}, schema_version=3)
        recipe.save()
        self.run = ProductionRun.objects.create(owner=self.actor, project=self.project, recipe_version=recipe,
            plan_source="recipe", status="running", idempotency_key=str(uuid4()), request_sha256="b"*64)
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "episodes", "episode_ids": [str(self.episode.pk)]},
            delivery={"kind": "episode_masters"}, initial_content=None, stage_catalog_key=catalog.value["key"],
            expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        self.bible = StageRun.objects.create(production_run=self.run, stage_key="bible_review", mode="review", ordinal=1,
            status="succeeded", idempotency_key=str(uuid4()))
        self.review = StageRun.objects.create(production_run=self.run, stage_key="storyboard_review", mode="review", ordinal=2,
            status="succeeded", idempotency_key=str(uuid4()))
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", mode="existing_pipeline", ordinal=3,
            status="running", idempotency_key=str(uuid4()))
        _save_stage_snapshot(self.bible, "output_snapshot", _formal_snapshot(self.run, self.bible, "bible"))
        self.bible_decision = ApprovalDecision.objects.create(stage_run=self.bible, revision=1, decision="approve", actor="human",
            decided_by=self.actor, output_snapshot_sha256=self.bible.output_snapshot_sha256)
        self.approve()

    def definition(self):
        return fixtures.ShotContextTests.definition(self, "json")

    def rule(self, field="dialogue_with_speakers"):
        return {"kind": "context_field", "selector_key": f"shot.{field}", "scope": {"kind": "shot"}}

    def bind(self, template=None):
        rules = {"brief": {"kind": "structured", "template": template or self.rule()}}
        validate_recipe_input_sources(self.definition(), rules, prior_stages=["bible_review", "storyboard_review"])
        return resolve_recipe_media_sources(definition=self.definition(), route={"input_sources": rules}, stage=self.stage,
            plan=get_run_execution_plan(self.run), scope={"kind": "shot", "id": str(self.shot.pk)})

    def resolve(self, bound):
        return resolve_execution_input_sources(self.definition(), bound["inputs"], owner=self.actor, subject_facts=[],
            max_media_bytes=1024*1024, project_id=self.project.pk, production_run_id=str(self.run.pk),
            eligible_stage_ids=bound["eligible_stage_ids"], production_scope={"kind": "shot", "id": str(self.shot.pk)},
            unit_key=f"shot:{self.shot.pk}").value

    def test_ordered_speakers_keep_original_facts_and_both_review_identities(self):
        bound = self.bind(); source = bound["inputs"]["brief"]["template"]
        self.assertEqual(source["content_revision_id"], str(self.decision.pk))
        self.assertEqual(source["related_revision_ids"], {"bible": str(self.bible_decision.pk)})
        result = self.resolve(bound); rows = result["logical_values"]["brief"]
        self.assertEqual(rows, [{**self.shot.dialogue[0], "subject": "乙", "voice_profile": "轻柔高音"},
            {**self.shot.dialogue[1], "subject": "甲", "voice_profile": "沉稳低音"}])
        self.assertNotIn("start_ms", rows[0]); self.assertNotIn("voiceover", rows[1])
        self.assertEqual(result["input_provenance"][0]["leaves"][0]["related_reviews"]["bible"]["decision_id"], str(self.bible_decision.pk))
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())

    def test_linked_bible_excludes_unlinked_entities_and_media_bindings(self):
        value = self.resolve(self.bind(self.rule("linked_bible")))["logical_values"]["brief"]
        self.assertEqual({row["id"] for row in value["characters"]}, {str(self.first.pk), str(self.voice.pk)})
        self.assertEqual([row["id"] for row in value["scenes"]], [str(self.scene.pk)])
        self.assertEqual([row["id"] for row in value["props"]], [str(self.prop.pk)])
        self.assertNotIn("anchor_asset_id", value["characters"][0])
        self.assertNotIn("reference_asset_ids", value["characters"][0])
        self.assertEqual([row["character_id"] for row in value["character_relations"] if row["role"] == "visible"], [str(self.first.pk)])
        self.assertEqual([row["character_id"] for row in value["character_relations"] if row["role"] == "speaker"], [str(self.voice.pk), str(self.first.pk)])

    def test_each_can_explicitly_map_voice_facts_without_invented_timing(self):
        node = {"kind": "each", "source": self.rule(), "item": {"kind": "object", "fields": {
            "who": {"kind": "item_field", "path": ["subject"]}, "voice": {"kind": "item_field", "path": ["voice_profile"]}}}}
        value = self.resolve(self.bind(node))["logical_values"]["brief"]
        self.assertEqual(value, [{"who": "乙", "voice": "轻柔高音"}, {"who": "甲", "voice": "沉稳低音"}])
        node["item"]["fields"]["start"] = {"kind": "item_field", "path": ["start_ms"]}
        with self.assertRaisesMessage(ValidationError, "缺少所选字段"): self.resolve(self.bind(node))

    def test_missing_unknown_old_or_changed_bible_review_never_falls_back(self):
        bound = self.bind()
        for ids in ({}, {"bible": str(uuid4())}, {"bible": str(self.bible_decision.pk), "extra": str(uuid4())}):
            bad = deepcopy(bound); bad["inputs"]["brief"]["template"]["related_revision_ids"] = ids
            with self.subTest(ids=ids), self.assertRaises(ValidationError): self.resolve(bad)
        ApprovalDecision.objects.create(stage_run=self.bible, revision=2, decision="approve", actor="human",
            decided_by=self.actor, output_snapshot_sha256=self.bible.output_snapshot_sha256)
        with self.assertRaisesMessage(ValidationError, "不一致"): self.resolve(bound)
        self.voice.voice_profile = "未经审核的改动"; self.voice.save(update_fields=("voice_profile",))
        with self.assertRaisesMessage(ValidationError, "已变更"): self.bind()

    def test_unlinked_dialogue_speaker_is_not_found_by_searching_the_whole_project(self):
        self.shot.dialogue = [{"speaker_id": "S3", "text": "未关联"}, self.shot.dialogue[1]]
        self.shot.save(update_fields=("dialogue",)); self.reapprove()
        with self.assertRaisesMessage(ValidationError, "当前镜头关联"): self.resolve(self.bind())

    def test_two_dependencies_required_and_reused_within_one_mapping(self):
        rules = {"brief": {"kind": "structured", "template": self.rule()}}
        for prior in ([], ["storyboard_review"], ["bible_review"]):
            with self.subTest(prior=prior), self.assertRaisesMessage(ValidationError, "更早"):
                validate_recipe_input_sources(self.definition(), rules, prior_stages=prior)
        template = {"kind": "object", "fields": {"bible": self.rule("linked_bible"), "dialogue": self.rule()}}
        with (patch("studio.services.production_reviewed_content.read_reviewed_bible", wraps=read_reviewed_bible) as bible,
                patch("studio.services.production_reviewed_content.read_reviewed_storyboard", wraps=read_reviewed_storyboard) as storyboard):
            self.resolve(self.bind(template))
        self.assertEqual(bible.call_count, 2); self.assertEqual(storyboard.call_count, 2)

    def test_exact_relation_never_resolves_a_different_character_with_the_same_spoken_name(self):
        # Same human name is legal; the declared speaker ID, not text/name, wins.
        self.first.name = self.voice.name; self.first.save(update_fields=("name",))
        _save_stage_snapshot(self.bible, "output_snapshot", _formal_snapshot(self.run, self.bible, "bible"))
        self.bible_decision = ApprovalDecision.objects.create(stage_run=self.bible, revision=2, decision="approve", actor="human",
            decided_by=self.actor, output_snapshot_sha256=self.bible.output_snapshot_sha256)
        value = self.resolve(self.bind())["logical_values"]["brief"]
        self.assertEqual([row["voice_profile"] for row in value], ["轻柔高音", "沉稳低音"])
        ShotCharacterRelation.objects.filter(shot=self.shot, role="speaker", position=0).update(character=self.first)
        self.reapprove()
        with self.assertRaisesMessage(ValidationError, "确切发言者不一致"):
            self.resolve(self.bind())

    def test_incomplete_or_duplicate_roles_do_not_fall_back_to_old_pool(self):
        from studio.services.production_linked_content import linked_bible_content
        shot = {"character_relations": [{"role": "visible", "position": 0, "character_id": str(self.first.pk)},
            {"role": "speaker", "position": 0, "character_id": str(self.voice.pk)}],
            "dialogue": [self.shot.dialogue[0]], "character_ids": [str(self.first.pk), str(self.voice.pk)],
            "scene_id": None, "prop_ids": []}
        bible = {"characters": [{"id": str(self.first.pk)}, {"id": str(self.voice.pk)}], "scenes": [], "props": []}
        original = deepcopy(shot)
        value = linked_bible_content(shot, bible)
        self.assertEqual(value["character_relations"], shot["character_relations"])
        for relations in (None, [], shot["character_relations"][:1], shot["character_relations"] * 2,
                [{**shot["character_relations"][1], "position": 1}], [{**shot["character_relations"][1], "role": []}],
                [{**shot["character_relations"][1], "character_id": "乙"}]):
            with self.subTest(relations=relations), self.assertRaises(ValidationError):
                linked_bible_content({**shot, "character_relations": relations}, bible)
        self.assertEqual(shot, original)
        value["characters"][0]["id"] = "not an authority"
        self.assertEqual(bible["characters"][0]["id"], str(self.first.pk))
