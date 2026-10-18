"""Explicit linked Bible inputs share exact primary bodies, not old anchors."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, GenerationAttempt, ExecutionPreparation, Shot, ShotCharacterRelation
from studio.services.bible_setting_evidence import read_setting_revisions, setting_reference
from studio.services.bible_variants import create_variant, revise_variant, setting_payload
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.json_contracts import json_contract
from studio.services.production_linked_content import reviewed_linked_shot_value
from studio.services.production_recipe_media_preparations import resolve_recipe_media_sources
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_reviewed_content import read_reviewed_bible, read_reviewed_entity_primaries
from studio.services.production_runs import _formal_snapshot, _save_stage_snapshot
from studio.tests import test_recipe_linked_context as fixture


class ReviewedLinkedSettingsTests(TestCase):
    setUp = fixture.LinkedContextTests.setUp
    approve = fixture.LinkedContextTests.approve
    reapprove = fixture.LinkedContextTests.reapprove
    definition = fixture.LinkedContextTests.definition
    rule = fixture.LinkedContextTests.rule
    bind = fixture.LinkedContextTests.bind
    resolve = fixture.LinkedContextTests.resolve

    def primary(self, kind, entity, *, blank=False):
        variant = create_variant(actor=self.actor, project_id=self.project.pk, entity_kind=kind, entity_id=entity.pk,
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="",
            value={"name": "主图", "reference_name": f"{entity.slug}·主图",
                "identity_anchor": "" if blank else f"identity {entity.slug}",
                "appearance_anchor": "" if blank else f"appearance {entity.slug}",
                "basis_asset_id": None, "basis_asset_sha256": ""})
        return variant.revisions.get(revision=1)

    def approve_bible(self):
        _save_stage_snapshot(self.bible, "output_snapshot", _formal_snapshot(self.run, self.bible, "bible"))
        self.bible_decision = ApprovalDecision.objects.create(stage_run=self.bible,
            revision=self.bible.approval_decisions.count() + 1, decision="approve", actor="human",
            decided_by=self.actor, output_snapshot_sha256=self.bible.output_snapshot_sha256)

    def context(self):
        reviewed = read_reviewed_bible(run=self.run, eligible_stage_ids=[self.bible.pk])
        return deepcopy(self.review.output_snapshot["canonical"]["episodes"][0]["shots"][0]), {"bible": reviewed}

    def linked(self, shot, dependencies, *, cache=None, field="linked_bible", run_id=None):
        return reviewed_linked_shot_value(field, shot, dependencies, run_id=run_id or self.run.pk,
            project_id=str(self.project.pk), cache={} if cache is None else cache)

    def test_real_mapping_three_kinds_preserves_exact_references_and_blank_or_absent(self):
        originals = []
        for kind, entity in (("character", self.first), ("scene", self.scene), ("prop", self.prop)):
            originals.append(self.primary(kind, entity, blank=kind == "prop"))
        self.approve_bible()
        original_snapshot = deepcopy(self.bible.output_snapshot)
        template = {"kind": "object", "fields": {"bible": self.rule("linked_bible"),
            "again": self.rule("linked_bible"), "voices": self.rule()}}
        with patch("studio.services.bible_setting_evidence.read_setting_revisions", wraps=read_setting_revisions) as reader:
            result = self.resolve(self.bind(template))
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(set(reader.call_args.args[0]), {str(row.pk) for row in originals})
        values = result["logical_values"]["brief"]
        self.assertEqual(values["bible"], values["again"])
        for section, setting in zip(("characters", "scenes", "props"), originals):
            row = next(row for row in values["bible"][section] if row["appearance_settings"])
            self.assertEqual(row["primary_setting"], {"identity_anchor": setting.identity_anchor,
                "appearance_anchor": setting.appearance_anchor})
            self.assertEqual(row["appearance_settings"], [setting_reference(setting)])
            for key in ("visual_identity", "reference_bindings", "reference_asset_ids", "anchor_asset_id"):
                self.assertNotIn(key, row)
        self.assertIsNone(next(row for row in values["bible"]["characters"] if row["id"] == str(self.voice.pk))["primary_setting"])
        self.assertEqual([row["subject"] for row in values["voices"]], ["乙", "甲"])
        self.assertEqual(result["input_provenance"][0]["leaves"][0]["related_reviews"]["bible"]["decision_id"], str(self.bible_decision.pk))
        self.bible.refresh_from_db(); self.assertEqual(self.bible.output_snapshot, original_snapshot)
        self.assertFalse(GenerationAttempt.objects.exists()); self.assertFalse(ExecutionPreparation.objects.exists())

    def test_batch_is_one_narrow_indexed_read_and_shared_across_shots_without_aliasing(self):
        for kind, entity in (("character", self.first), ("scene", self.scene), ("prop", self.prop)):
            self.primary(kind, entity)
        self.approve_bible(); shot, dependencies = self.context(); cache = {}
        with CaptureQueriesContext(connection) as queries:
            value = self.linked(shot, dependencies, cache=cache)
            second = self.linked(dict(shot, id=str(uuid4())), dependencies, cache=cache)
        self.assertEqual(len(queries), 1)
        sql = queries[0]["sql"]
        for fragment in ('"visual_identity"', '"metadata"', '"studio_scriptrevision"."content"', "SELECT *"):
            self.assertNotIn(fragment, sql)
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + sql)
                plan = " ".join(str(row[-1]) for row in cursor.fetchall()).upper()
                self.assertIn("SEARCH", plan); self.assertNotIn("SCAN STUDIO_", plan)
        value["scenes"][0]["primary_setting"]["identity_anchor"] = "local edit"
        value["scenes"][0]["appearance_settings"].clear()
        self.assertNotEqual(value, second)
        self.assertEqual(second, self.linked(shot, dependencies, cache=cache))
        self.assertTrue(dependencies["bible"]["canonical"]["scenes"][0]["appearance_settings"])

    def test_real_shots_resolve_independently_and_reject_cross_shot_mapping(self):
        self.primary("character", self.first); self.primary("scene", self.scene)
        second = Shot.objects.create(episode=self.episode, number=2, duration_ms=1000,
            media_type="image", include_audio=False, scene=self.scene)
        ShotCharacterRelation.objects.create(shot=second, character=self.first, role="visible", position=0)
        self.approve_bible(); self.reapprove()
        first = self.bind(self.rule("linked_bible"))
        inputs = deepcopy(first["inputs"])
        inputs["second"] = deepcopy(inputs["brief"])
        inputs["second"]["template"]["scope"] = {"kind": "shot", "id": str(second.pk)}
        payload = deepcopy(self.definition().value)
        payload["configuration"]["inputs"].append({**deepcopy(payload["configuration"]["inputs"][0]), "key": "second"})
        with self.assertRaisesMessage(ValidationError, "不能读取其他镜头"):
            resolve_execution_input_sources(json_contract(payload), inputs, owner=self.actor, subject_facts=[],
                max_media_bytes=1024*1024, project_id=self.project.pk, production_run_id=str(self.run.pk),
                eligible_stage_ids=first["eligible_stage_ids"], production_scope={"kind": "shot", "id": str(self.shot.pk)},
                unit_key=f"shot:{self.shot.pk}")
        values = []
        for shot in (self.shot, second):
            scope = {"kind": "shot", "id": str(shot.pk)}
            bound = resolve_recipe_media_sources(definition=self.definition(),
                route={"input_sources": {"brief": {"kind": "structured", "template": self.rule("linked_bible")}}},
                stage=self.stage, plan=get_run_execution_plan(self.run), scope=scope)
            with patch("studio.services.bible_setting_evidence.read_setting_revisions", wraps=read_setting_revisions) as reader:
                result = resolve_execution_input_sources(self.definition(), bound["inputs"], owner=self.actor, subject_facts=[],
                    max_media_bytes=1024*1024, project_id=self.project.pk, production_run_id=str(self.run.pk),
                    eligible_stage_ids=bound["eligible_stage_ids"], production_scope=scope, unit_key=f"shot:{shot.pk}").value
            self.assertEqual(reader.call_count, 1)
            values.append(result["logical_values"]["brief"])
            self.assertEqual(result["input_provenance"][0]["leaves"][0]["unit"]["scope"], scope)
        self.assertEqual(len(values[0]["characters"]), 2)
        self.assertEqual([row["id"] for row in values[1]["characters"]], [str(self.first.pk)])
        self.assertEqual(values[1]["props"], [])
        self.assertEqual(values[0]["scenes"], values[1]["scenes"])

    def test_unrelated_setting_is_not_loaded_and_dialogue_does_not_load_selected_body(self):
        outside = self.primary("character", self.unlinked)
        selected = self.primary("scene", self.scene)
        self.approve_bible(); shot, dependencies = self.context()
        # Deliberate isolated corruption bypasses the append-only writer guard.
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_biblevariantrevision SET identity_anchor=%s WHERE id=%s",
                ["corrupted unlinked body", outside.pk.hex])
        with patch("studio.services.bible_setting_evidence.read_setting_revisions", wraps=read_setting_revisions) as reader:
            self.linked(shot, dependencies)
        self.assertEqual(reader.call_args.args[0], [str(selected.pk)])
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_biblevariantrevision SET identity_anchor=%s WHERE id=%s",
                ["corrupted linked body", selected.pk.hex])
        with self.assertNumQueries(0):
            voices = self.linked(shot, dependencies, field="dialogue_with_speakers")
        self.assertEqual([row["voice_profile"] for row in voices], ["轻柔高音", "沉稳低音"])
        with self.assertRaisesMessage(ValidationError, "缺失、损坏"):
            self.linked(shot, dependencies)

    def test_empty_set_and_no_settings_use_no_body_queries_but_missing_contract_fails(self):
        shot, dependencies = self.context()
        with self.assertNumQueries(0):
            rows = self.linked(shot, dependencies)
            empty = self.linked(dict(shot, character_relations=[], dialogue=[], scene_id=None, prop_ids=[]), dependencies)
            self.assertEqual(read_reviewed_entity_primaries({}, project_id=self.project.pk), {})
        self.assertEqual(empty, {"characters": [], "character_relations": [], "scenes": [], "props": []})
        self.assertEqual(rows["character_relations"], shot["character_relations"])
        self.assertTrue(all(row["primary_setting"] is None
            for section in ("characters", "scenes", "props") for row in rows[section]))
        for refs in (None, {}, [None], [{"appearance_role": "variant"}]):
            bad = deepcopy(dependencies); bad["bible"]["canonical"]["scenes"][0]["appearance_settings"] = refs
            with self.subTest(refs=refs), self.assertRaises(ValidationError): self.linked(shot, bad)

    def test_original_review_reads_original_revision_current_pipeline_requires_reapproval(self):
        original = self.primary("scene", self.scene)
        self.approve_bible(); shot, dependencies = self.context()
        bound = self.bind(self.rule("linked_bible")); frozen = self.resolve(bound)
        revise_variant(actor=self.actor, variant=original.variant, expected_revision=1,
            value={**setting_payload(original), "identity_anchor": "new identity"})
        self.assertEqual(self.linked(shot, dependencies)["scenes"][0]["primary_setting"]["identity_anchor"], original.identity_anchor)
        with self.assertRaisesMessage(ValidationError, "已变更"): self.resolve(bound)
        self.approve_bible()
        current = self.resolve(self.bind(self.rule("linked_bible")))
        self.assertEqual(current["logical_values"]["brief"]["scenes"][0]["primary_setting"]["identity_anchor"], "new identity")
        self.assertEqual(frozen["logical_values"]["brief"]["scenes"][0]["primary_setting"]["identity_anchor"], original.identity_anchor)

    def test_cache_is_scoped_to_run_and_review_and_never_hides_new_reference_error(self):
        self.primary("scene", self.scene); self.approve_bible()
        shot, dependencies = self.context(); cache = {}
        with CaptureQueriesContext(connection) as queries:
            self.linked(shot, dependencies, cache=cache)
            self.linked(shot, dependencies, cache=cache, run_id=uuid4())
        self.assertEqual(len(queries), 2)
        bad = deepcopy(dependencies)
        bad["bible"]["review"]["decision_id"] = str(uuid4())
        bad["bible"]["canonical"]["scenes"][0]["appearance_settings"][0]["revision"] += 1
        previous_cache = deepcopy(cache)
        with self.assertRaisesMessage(ValidationError, "审核引用不一致"):
            self.linked(shot, bad, cache=cache)
        self.assertEqual(cache, previous_cache)

    def test_wrong_entity_duplicate_primary_and_other_project_are_rejected(self):
        self.primary("scene", self.scene); self.primary("prop", self.prop); self.approve_bible()
        shot, dependencies = self.context()
        rows = dependencies["bible"]["canonical"]
        for references in ([rows["props"][0]["appearance_settings"][0]], rows["scenes"][0]["appearance_settings"] * 2):
            bad = deepcopy(dependencies); bad["bible"]["canonical"]["scenes"][0]["appearance_settings"] = references
            with self.subTest(references=references), self.assertRaises(ValidationError): self.linked(shot, bad)
        with self.assertRaisesMessage(ValidationError, "缺失、损坏"):
            read_reviewed_entity_primaries({("scene", str(self.scene.pk)): rows["scenes"][0]}, project_id=uuid4())
