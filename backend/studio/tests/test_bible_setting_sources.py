"""M3-03 source comparisons use isolated imports, not live/model evidence."""
from copy import deepcopy
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import BibleVariant, BibleVariantRevision, BibleVariantReview, ScriptRevisionStructure
from studio.services.bible_setting_sources import declaration_field_origin
from studio.services.bible_variants import BibleVariantConflict, create_variant, review_variant, revise_variant, setting_payload
from studio.tests import test_script_reference_materialization as fixtures
from studio.tests.structured_bible_fixtures import confirmed_structured_bible_source


class DeclarationFieldOriginTests(SimpleTestCase):
    def test_whole_object_path_and_nested_default_are_distinct(self):
        projection = {"provenance": [{"path": "value.entities", "kind": "path", "source_path": 'source["资源"]'}],
            "defaults": [{"path": 'source["资源"][0].primary', "source": "configured_default"}]}
        self.assertEqual(declaration_field_origin(projection, "value.entities[0].primary.anchor"),
            ("configured_default", 'source["资源"][0].primary.anchor'))
        self.assertEqual(declaration_field_origin(projection, "value.entities[1].primary.anchor")[0], "source_value")

    def test_lookup_final_record_and_nearest_literal_win(self):
        projection = {"provenance": [
            {"path": "value.entities[0]", "kind": "path", "source_path": "source.entities"},
            {"path": "value.entities[0]", "kind": "lookup", "source_path": "source.entities[2]"},
            {"path": "value.entities[0].primary", "kind": "literal"}], "defaults": []}
        self.assertEqual(declaration_field_origin(projection, "value.entities[0].name"),
            ("source_value", "source.entities[2].name"))
        self.assertEqual(declaration_field_origin(projection, "value.entities[0].primary.anchor"),
            ("configured_literal", None))

    def test_missing_or_unknown_origin_is_not_reported_as_source(self):
        for origins in ([], [{"path": "value.entities", "kind": "invented"}]):
            with self.assertRaises(BibleVariantConflict):
                declaration_field_origin({"provenance": origins, "defaults": []}, "value.entities[0].name")


class BibleSettingSourceTests(TestCase):
    setUp = fixtures.ScriptReferenceMaterializationTests.setUp
    source_revision = fixtures.ScriptReferenceMaterializationTests.source_revision
    frozen = fixtures.ScriptReferenceMaterializationTests.frozen
    apply = fixtures.ScriptReferenceMaterializationTests.apply

    def appearance(self):
        return BibleVariant.objects.get(source_variant_key="uniform")

    def read(self, appearance, revision=1, expected_status=200):
        response = self.client.get(f"/api/v1/bible-variants/{appearance.pk}/source/", {"revision": revision})
        self.assertEqual(response.status_code, expected_status, response.data)
        return response.data

    def test_source_is_on_demand_exact_and_read_only(self):
        self.apply()
        variant = self.appearance()
        with CaptureQueriesContext(connection) as queries:
            detail = self.client.get(f"/api/v1/bible-variants/{variant.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(any('FROM "studio_scriptrevisionstructure"' in query["sql"] for query in queries))
        with CaptureQueriesContext(connection) as queries:
            result = self.read(variant)
        self.assertEqual(result["source"]["script_revision_id"], str(self.revision.pk))
        self.assertEqual(result["document_sha256"], ScriptRevisionStructure.objects.get(revision=self.revision).document_sha256)
        fields = {row["key"]: row for row in result["fields"]}
        self.assertNotIn("identity_anchor", fields)  # Identity remains in the exact primary, not duplicated.
        self.assertEqual(fields["appearance_anchor"]["source_path"], 'source["素材设定"][0].variants[0].anchor')
        self.assertEqual(fields["appearance_anchor"]["declaration_value"], "白天穿制服")
        self.assertTrue(all(row["origin"] == "source_value" and row["changed_from_declaration"] is False for row in fields.values()))
        source_queries = [row["sql"] for row in queries if 'FROM "studio_scriptrevisionstructure"' in row["sql"]]
        self.assertEqual(len(source_queries), 1)
        self.assertFalse(any(row["sql"].lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + source_queries[0])
                plan = "\n".join(str(row[-1]) for row in cursor.fetchall())
            self.assertIn("SEARCH", plan)
            self.assertNotIn("SCAN studio_scriptrevisionstructure", plan)

    def test_approved_manual_revision_survives_new_source_and_replays(self):
        first, original_source = self.apply(), self.revision
        variant = self.appearance()
        initial = variant.revisions.get()
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(initial), "appearance_anchor": "人工蓝衣"})
        manual = variant.revisions.get(revision=2)
        review_variant(actor=self.actor, variant=variant, expected_revision=2,
            expected_sha256=manual.content_sha256, decision="approve", comment="隔离人工设定确认")
        original_comparison = self.read(variant, 2)
        self.declarations["entities"][0]["variants"][0]["anchor"] = "新源红衣，不能覆盖"
        self.revision = self.source_revision()
        second = self.apply()
        self.assertEqual(self.read(variant, 2), original_comparison)
        fields = {row["key"]: row for row in original_comparison["fields"]}
        self.assertEqual((fields["appearance_anchor"]["declaration_value"], fields["appearance_anchor"]["setting_value"],
            fields["appearance_anchor"]["changed_from_declaration"]), ("白天穿制服", "人工蓝衣", True))
        counts = (BibleVariant.objects.count(), BibleVariantRevision.objects.count(), BibleVariantReview.objects.count())
        self.assertEqual(second, self.apply())
        self.revision = original_source
        self.assertEqual(first, self.apply())
        self.assertEqual(counts, (BibleVariant.objects.count(), BibleVariantRevision.objects.count(), BibleVariantReview.objects.count()))
        self.assertEqual(BibleVariantReview.objects.get(setting=manual).decision, "approve")
        self.assertFalse(self.read(variant, 1)["fields"][-1]["changed_from_declaration"])

    def test_renamed_mapping_keeps_actual_external_path(self):
        self.revision = confirmed_structured_bible_source(self, layout="renamed")
        self.apply()
        result = self.read(self.appearance())
        self.assertEqual(result["fields"][-1]["source_path"], 'source["资源"][0]["造型"]["变化"][0]["差异"]')

    def test_literal_mapping_is_not_labeled_source_text(self):
        from studio.tests.script_semantics_fixtures import literal
        with patch("studio.tests.structured_bible_fixtures.path", return_value=literal(deepcopy(self.declarations["entities"]))):
            self.revision = self.source_revision()
        self.apply()
        self.assertTrue(all(row["origin"] == "configured_literal" and row["source_path"] is None
            for row in self.read(self.appearance())["fields"]))

    def test_absent_and_explicit_blank_declaration_are_not_the_same(self):
        self.declarations["entities"][0].pop("identity_anchor")
        self.declarations["entities"][0]["primary"]["anchor"] = ""
        self.revision = self.source_revision()
        self.apply()
        primary = BibleVariant.objects.get(source_binding__entity_key="person_a", appearance_role="primary")
        fields = {row["key"]: row for row in self.read(primary)["fields"]}
        self.assertEqual((fields["identity_anchor"]["origin"], fields["identity_anchor"]["declaration_value"]), ("missing", None))
        self.assertEqual((fields["appearance_anchor"]["origin"], fields["appearance_anchor"]["declaration_value"]), ("source_value", ""))

    def test_manual_appearance_has_no_invented_script_source(self):
        variant = create_variant(actor=self.actor, project_id=self.project.pk, entity_kind="scene",
            entity_id=self.project.scenes.first().pk, appearance_role="primary", primary_setting_revision_id=None,
            primary_setting_sha256="", value={"name": "手工主图", "reference_name": "手工主图", "identity_anchor": "",
                "appearance_anchor": "蓝色门", "basis_asset_id": None, "basis_asset_sha256": ""})
        result = self.read(variant)
        self.assertIsNone(result["source"])
        self.assertIsNone(result["document_sha256"])
        self.assertTrue(all(row["origin"] == "manual" and row["declaration_value"] is None
            and row["changed_from_declaration"] is None for row in result["fields"]))

    def test_missing_structure_or_changed_body_fails_without_current_source_fallback(self):
        self.apply()
        original = self.revision
        self.revision = self.source_revision()
        ScriptRevisionStructure.objects.filter(revision=original).delete()
        self.read(self.appearance(), expected_status=409)

    def test_corrupt_binding_or_setting_fails_explicitly(self):
        self.apply()
        variant = self.appearance()
        # Deliberate isolated corruption; the normal append-only writer refuses it.
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_biblevariantrevision SET appearance_anchor=%s WHERE variant_id=%s",
                ["isolated corruption", variant.pk.hex])
        self.read(variant, expected_status=409)

    def test_scope_authentication_and_explicit_revision_required(self):
        self.apply()
        variant = self.appearance()
        url = f"/api/v1/bible-variants/{variant.pk}/source/"
        for query in ({}, {"revision": 0}, {"revision": "bad"}, {"revision": 1, "unknown": "x"}):
            self.assertEqual(self.client.get(url, query).status_code, 400)
        self.read(variant, revision=99, expected_status=400)
        other, *_ = fixtures.build_story("source-other-owner")
        self.client.force_authenticate(other)
        self.read(variant, expected_status=404)
        self.client.force_authenticate(None)
        self.read(variant, expected_status=401)
