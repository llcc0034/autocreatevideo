"""Explicit project context uses primary settings, never duplicate entity text."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import BibleVariant, CharacterBible, SceneBible, PropBible, GenerationAttempt, ExecutionPreparation
from studio.services.bible_variants import create_variant, revise_variant, review_variant, setting_payload
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.json_contracts import json_contract
from studio.services.production_visual_rules import current_visual_context_snapshot
from studio.tests import test_execution_context_inputs as context_fixture


class PrimaryVisualContextTests(TestCase):
    setUp = context_fixture.ExecutionContextInputTests.setUp

    def entities(self):
        return [(kind, model.objects.create(project=self.project, slug=kind, name=kind,
            description=f"description {kind}"))
            for kind, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible))]

    def primary(self, kind, entity):
        return create_variant(actor=self.owner, project_id=self.project.pk, entity_kind=kind, entity_id=entity.pk,
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="",
            value={"name": "主图", "reference_name": f"{kind}·主图", "identity_anchor": f"identity {kind}",
                "appearance_anchor": f"appearance {kind}", "basis_asset_id": None, "basis_asset_sha256": ""})

    def resolve(self, selector="script.visual_context"):
        payload = deepcopy(self.definition.value)
        payload["configuration"]["inputs"][0].update(kind="json", targets=[])
        return resolve_execution_input_sources(json_contract(payload),
            {"script": {**self.input, "selector_key": selector}}, owner=self.owner, subject_facts=[],
            max_media_bytes=65536, project_id=self.project.pk).value

    def test_empty_absent_and_blank_are_distinct_without_fallback_or_writes(self):
        self.assertEqual(current_visual_context_snapshot(self.project)["bible"],
            {"characters": [], "scenes": [], "props": []})
        entities = self.entities()
        absent = self.resolve()
        for rows in absent["logical_values"]["script"]["project_context"]["confirmed_bible"].values():
            self.assertIsNone(rows[0]["primary_setting"])
            self.assertNotIn("visual_identity", rows[0])
        primary = self.primary(*entities[0])
        original = primary.revisions.get(revision=1)
        revise_variant(actor=self.owner, variant=primary, expected_revision=1,
            value={**setting_payload(original), "identity_anchor": "", "appearance_anchor": ""})
        value = self.resolve()["logical_values"]["script"]["project_context"]["confirmed_bible"]
        self.assertEqual(value["characters"][0]["primary_setting"]["identity_anchor"], "")
        self.assertEqual(value["characters"][0]["primary_setting"]["appearance_anchor"], "")
        self.assertEqual(value["characters"][0]["primary_setting"]["revision"], 2)
        self.assertIsNone(absent["logical_values"]["script"]["project_context"]["confirmed_bible"]["characters"][0]["primary_setting"])
        self.assertNotIn("visual_identity", str(value))
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_three_kinds_share_bounded_primary_queries_and_original_frozen_values(self):
        entities = self.entities()
        primaries = [self.primary(kind, entity) for kind, entity in entities]
        with CaptureQueriesContext(connection) as queries:
            snapshot = current_visual_context_snapshot(self.project)
        self.assertEqual(len(queries), 5)  # Three entity sets, one primary-pointer set, one exact-body set.
        for query in queries:
            self.assertNotIn('"visual_identity"', query["sql"])
            self.assertNotIn('"metadata"', query["sql"])
            self.assertNotIn('"studio_scriptrevision"."content"', query["sql"])
            self.assertNotIn("SELECT *", query["sql"].upper())
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                for query in queries:
                    cursor.execute("EXPLAIN QUERY PLAN " + query["sql"])
                    plan = " ".join(str(row[-1]) for row in cursor.fetchall()).upper()
                    self.assertIn("SEARCH", plan)
                    self.assertNotIn("SCAN STUDIO_", plan)
        frozen = self.resolve()
        copy = deepcopy(frozen)
        for section, primary in zip(("characters", "scenes", "props"), primaries):
            setting = primary.revisions.get(revision=1)
            value = snapshot["bible"][section][0]["primary_setting"]
            self.assertEqual(value["setting_revision_id"], str(setting.pk))
            self.assertEqual(value["content_sha256"], setting.content_sha256)
            self.assertEqual(value["identity_anchor"], setting.identity_anchor)
            self.assertEqual(value["appearance_anchor"], setting.appearance_anchor)
        primary = primaries[0]; original = primary.revisions.get(revision=1)
        review_variant(actor=self.owner, variant=primary, expected_revision=1,
            expected_sha256=original.content_sha256, decision="approve", comment="isolated review")
        self.assertEqual(current_visual_context_snapshot(self.project), snapshot)
        revise_variant(actor=self.owner, variant=primary, expected_revision=1,
            value={**setting_payload(original), "appearance_anchor": "New primary appearance"})
        current = current_visual_context_snapshot(self.project)
        self.assertNotEqual(current["bible_sha256"], snapshot["bible_sha256"])
        self.assertEqual(current["bible"]["characters"][0]["primary_setting"]["revision"], 2)
        self.assertEqual(frozen, copy)

    def test_broken_primary_is_an_error_but_style_only_input_has_no_bible_dependency(self):
        primary = self.primary(*self.entities()[0])
        for corrupt in ({"revision": 99}, {"revision": 1, "name": "Corrupt pointer name"}):
            BibleVariant.objects.filter(pk=primary.pk).update(**corrupt)
            with self.subTest(corrupt=corrupt), self.assertRaisesMessage(ValidationError, "主图设定缺失或损坏"):
                self.resolve()
            with CaptureQueriesContext(connection) as queries:
                self.assertIsNone(self.resolve("script.project_style")["logical_values"]["script"])
            self.assertFalse(any("studio_bible" in query["sql"] or "studio_characterbible" in query["sql"]
                or "studio_scenebible" in query["sql"] or "studio_propbible" in query["sql"] for query in queries))
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
