"""Read-only approval evidence and the original setting API, isolated DB only."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import (ApprovalDecision, Asset, BibleVariant, BibleVariantRevision, BibleVariantReview,
    GeneratedArtifact, GenerationAttempt, ProductionRecipeVersion, ProductionRun, StageRun)
from studio.services.bible_setting_evidence import (
    project_setting_evidence, read_entity_primary_setting, read_setting_revisions, setting_reference,
)
from studio.services.bible_variants import BibleVariantConflict, review_variant, revise_variant, setting_payload
from studio.services.json_contracts import assign_json_contract
from studio.services.production_reviewed_content import read_reviewed_bible
from studio.services.production_runs import _canonical_bible, _bible_creative_drift_projection
from studio.tests import test_script_reference_materialization as fixtures


class BibleSettingEvidenceTests(TestCase):
    setUp = fixtures.ScriptReferenceMaterializationTests.setUp
    source_revision = fixtures.ScriptReferenceMaterializationTests.source_revision
    frozen = fixtures.ScriptReferenceMaterializationTests.frozen
    apply = fixtures.ScriptReferenceMaterializationTests.apply

    def canonical(self):
        return _canonical_bible(SimpleNamespace(project=self.project, project_id=self.project.pk))

    def variant(self):
        return BibleVariant.objects.get(source_variant_key="uniform")

    def detail(self, variant, revision=None):
        response = self.client.get(f"/api/v1/bible-variants/{variant.pk}/", {"revision": revision} if revision else {})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_detail_exposes_exact_source_role_and_primary_without_creative_gates(self):
        self.apply()
        variant = self.variant()
        setting = variant.revisions.get(revision=1)
        value = self.detail(variant)
        self.assertEqual(value["appearance_role"], "variant")
        self.assertEqual(value["source"]["entity_key"], variant.source_binding.entity_key)
        self.assertEqual(value["source"]["script_revision_id"], str(self.revision.pk))
        self.assertEqual(value["source"]["variant_key"], "uniform")
        self.assertEqual(value["setting"]["identity_anchor"], "")
        self.assertNotIn("edit_requirements", value)
        primary = value["primary_setting"]
        self.assertEqual(primary["id"], str(setting.primary_setting_revision_id))
        self.assertEqual(primary["variant_id"], str(setting.primary_setting_revision.variant_id))
        self.assertEqual(primary["identity_anchor"], setting.primary_setting_revision.identity_anchor)
        self.assertIsNone(primary["review"])
        listing = self.client.get("/api/v1/bible-variants/", {"project_id": self.project.pk,
            "entity_kind": "character", "entity_id": variant.character_id})
        self.assertEqual(listing.status_code, 200)
        self.assertTrue({"primary", "variant"} <= {row["appearance_role"] for row in listing.data["results"]})
        self.assertTrue(all("source" in row and "primary_setting" not in row and "setting" not in row
            for row in listing.data["results"]))

    def test_primary_revision_change_never_rebinds_or_relabels_variant_dependency(self):
        self.apply()
        variant = self.variant()
        before = self.detail(variant)
        original = variant.revisions.get(revision=1).primary_setting_revision
        revise_variant(actor=self.actor, variant=original.variant, expected_revision=1,
            value={**setting_payload(original), "name": "新主图设定", "reference_name": "New primary"})
        after = self.detail(variant)
        self.assertEqual(after["primary_setting"], before["primary_setting"])
        self.assertEqual(after["setting"], before["setting"])
        current_primary = self.detail(original.variant)
        self.assertEqual(current_primary["setting"]["revision"], 2)
        self.assertEqual(current_primary["setting"]["source_declaration_sha256"], "")
        self.assertEqual(self.detail(original.variant, 1)["setting"]["reference_name"], original.reference_name)

    def test_exact_body_reader_batches_fixed_primary_and_does_not_load_other_bodies(self):
        from studio.bible_variant_serializers import BibleVariantSettingOutput, BiblePrimarySettingOutput
        self.apply()
        variant = self.variant()
        child = variant.revisions.get()
        primary = child.primary_setting_revision
        revise_variant(actor=self.actor, variant=primary.variant, expected_revision=1,
            value={**setting_payload(primary), "identity_anchor": "新版共同身份"})
        ids = list(BibleVariantRevision.objects.values_list("id", flat=True))
        with CaptureQueriesContext(connection) as queries:
            records = read_setting_revisions(ids, project_id=self.project.pk)
            references = [setting_reference(row) for row in records.values()]
            bodies = [BibleVariantSettingOutput(row).data for row in records.values()]
            fixed = BiblePrimarySettingOutput(records[str(child.pk)].primary_setting_revision).data
        self.assertEqual(len(queries), 1, [row["sql"] for row in queries])
        self.assertEqual(len(records), len(ids))
        self.assertEqual(fixed["id"], str(primary.pk))
        self.assertEqual(fixed["identity_anchor"], primary.identity_anchor)
        self.assertTrue(any(row["identity_anchor"] == "新版共同身份" for row in bodies))
        self.assertEqual(len(references), len(ids))
        sql = queries[0]["sql"]
        for column in ('"studio_scriptrevision"."content"', '"studio_seriesproject".',
                '"visual_identity"', '"metadata"'):
            self.assertNotIn(column, sql)
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + sql)
                plan = "\n".join(str(row[-1]) for row in cursor.fetchall())
            self.assertIn("SEARCH studio_biblevariantrevision USING INDEX", plan)
            self.assertNotIn("SCAN studio_biblevariantrevision", plan)

    def test_exact_body_reader_rejects_missing_foreign_and_corrupt_primary_without_fallback(self):
        with self.assertNumQueries(0):
            self.assertEqual(read_setting_revisions([], project_id=self.project.pk), {})
        self.apply()
        child = self.variant().revisions.get()
        for ids, project_id in (([child.pk, uuid4()], self.project.pk), ([child.pk], uuid4())):
            with self.subTest(ids=ids, project_id=project_id), self.assertRaises(BibleVariantConflict):
                read_setting_revisions(ids, project_id=project_id)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_biblevariantrevision SET identity_anchor=%s WHERE id=%s",
                ["isolated corruption", child.primary_setting_revision_id.hex])
        with self.assertRaises(BibleVariantConflict):
            read_setting_revisions([child.pk], project_id=self.project.pk)
        self.assertEqual(self.client.get(f"/api/v1/bible-variants/{child.variant_id}/").status_code, 409)

    def test_detail_loads_selected_body_and_primary_in_one_revision_query(self):
        self.apply()
        variant = self.variant()
        with CaptureQueriesContext(connection) as queries:
            value = self.detail(variant)
        bodies = [row["sql"] for row in queries if 'FROM "studio_biblevariantrevision"' in row["sql"]]
        self.assertEqual(len(bodies), 1, bodies)
        self.assertEqual(value["primary_setting"]["revision"], 1)
        self.assertNotIn('"studio_scriptrevision"."content"', bodies[0])

    def test_current_primary_reader_is_narrow_exact_and_absence_is_not_corruption(self):
        self.apply()
        original = self.variant().revisions.get().primary_setting_revision
        primary = original.variant
        args = {"project_id": str(self.project.pk), "entity_kind": "character", "entity_id": str(primary.character_id)}
        with CaptureQueriesContext(connection) as queries:
            setting = read_entity_primary_setting(**args)
            self.assertEqual(setting.pk, original.pk)
            self.assertEqual(setting.identity_anchor, original.identity_anchor)
        self.assertEqual(len(queries), 2, [row["sql"] for row in queries])
        for query in queries:
            for column in ('"visual_identity"', '"metadata"', '"studio_scriptrevision"."content"'):
                self.assertNotIn(column, query["sql"])
            if connection.vendor == "sqlite":
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + query["sql"])
                    plan = "\n".join(str(row[-1]) for row in cursor.fetchall())
                self.assertIn("SEARCH", plan)
                self.assertNotIn("SCAN studio_biblevariant", plan)
        revise_variant(actor=self.actor, variant=primary, expected_revision=1,
            value={**setting_payload(original), "identity_anchor": "", "appearance_anchor": ""})
        current = read_entity_primary_setting(**args)
        self.assertEqual((current.revision, current.identity_anchor, current.appearance_anchor), (2, "", ""))
        self.assertEqual(read_setting_revisions([original.pk], project_id=self.project.pk)[str(original.pk)].revision, 1)
        with self.assertNumQueries(1):
            self.assertIsNone(read_entity_primary_setting(**{**args, "project_id": uuid4()}))
        BibleVariant.objects.filter(pk=primary.pk).update(revision=3)
        with self.assertRaises(BibleVariantConflict):
            read_entity_primary_setting(**args)
        BibleVariant.objects.filter(pk=primary.pk).update(revision=2, name="损坏的当前名称")
        with self.assertRaises(BibleVariantConflict):
            read_entity_primary_setting(**args)

    def test_target_batch_freezes_primary_references_not_bodies_and_verifies_original_revision(self):
        from studio.services.production_bible_targets import (
            bible_target_content_matches, bible_target_value, read_bible_target_contents, verify_bible_target,
        )
        self.declarations["entities"].append({"key": "key", "kind": "prop", "name": "铜钥匙", "description": "",
            "identity_anchor": "铜制", "primary": {"reference_name": "钥匙主图", "anchor": "方形握柄"}, "variants": []})
        self.revision = self.source_revision()
        self.apply()
        entities = {}
        for primary in BibleVariant.objects.filter(project=self.project, appearance_role="primary").select_related("character", "scene", "prop"):
            kind = next(kind for kind in ("character", "scene", "prop") if getattr(primary, f"{kind}_id"))
            entities[(kind, str(getattr(primary, f"{kind}_id")))] = getattr(primary, kind)
        self.assertEqual({key[0] for key in entities}, {"character", "scene", "prop"})
        with CaptureQueriesContext(connection) as queries:
            contents = read_bible_target_contents(entities, project_id=self.project.pk)
        self.assertEqual(len(queries), 2, [row["sql"] for row in queries])
        for query in queries:
            for column in ('"visual_identity"', '"metadata"', '"studio_scriptrevision"."content"'):
                self.assertNotIn(column, query["sql"])
            if connection.vendor == "sqlite":
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + query["sql"])
                    plan = "\n".join(str(row[-1]) for row in cursor.fetchall())
                self.assertIn("SEARCH", plan)
                self.assertNotIn("SCAN studio_biblevariant", plan)
        for key, entity in entities.items():
            with self.subTest(kind=key[0]):
                scope = {"kind": key[0], "id": key[1]}
                target = bible_target_value(entity, scope, [], content=contents[key])
                unit = {"scope": scope, "project_id": str(self.project.pk)}
                reference = target["primary_setting"]
                setting = read_setting_revisions([reference["setting_revision_id"]], project_id=self.project.pk)[reference["setting_revision_id"]]
                self.assertNotIn("identity_anchor", str(target))
                self.assertNotIn("appearance_anchor", str(target))
                self.assertNotIn("visual_identity", target)
                self.assertEqual(verify_bible_target(target, unit), target)
                revise_variant(actor=self.actor, variant=setting.variant, expected_revision=1,
                    value={**setting_payload(setting), "identity_anchor": "", "appearance_anchor": ""})
                self.assertFalse(bible_target_content_matches(target, entity))
                self.assertEqual(verify_bible_target(target, unit), target)
                fresh = bible_target_value(entity, scope, [])
                self.assertEqual(fresh["primary_setting"]["revision"], 2)
                for invalid in ({**reference, "content_sha256": "0" * 64}, {**reference, "revision": True},
                        {**reference, "setting_revision_id": str(uuid4())}, {**reference, "appearance_id": str(uuid4())}):
                    with self.assertRaises(ValidationError):
                        verify_bible_target({**target, "primary_setting": invalid}, unit)
                with self.assertRaises(ValidationError):
                    verify_bible_target({**target, "schema_version": 1, "visual_identity": "old"}, unit)
    def test_canonical_covers_exact_settings_without_copying_bodies_and_batches_queries(self):
        self.apply()
        with CaptureQueriesContext(connection) as queries:
            evidence = project_setting_evidence(self.project.pk)
        self.assertEqual(len(queries), 2, [row["sql"] for row in queries])
        self.assertFalse(any(word in row["sql"].upper() for row in queries for word in ("INSERT", "UPDATE", "DELETE")))
        serialized = str(evidence)
        self.assertNotIn("identity_anchor", serialized)
        self.assertNotIn("appearance_anchor", serialized)
        self.assertNotIn('"studio_scriptrevision"."content"', " ".join(row["sql"] for row in queries))
        if connection.vendor == "sqlite":
            plans = []
            with connection.cursor() as cursor:
                for query in queries:
                    cursor.execute("EXPLAIN QUERY PLAN " + query["sql"])
                    plans.extend(str(row) for row in cursor.fetchall())
            self.assertTrue(any("INDEX" in plan and "studio_biblevariant" in plan for plan in plans), plans)
        variant = self.variant()
        original = variant.revisions.get(revision=1).primary_setting_revision
        revise_variant(actor=self.actor, variant=original.variant, expected_revision=1,
            value={**setting_payload(original), "appearance_anchor": "明确的新设定"})
        with CaptureQueriesContext(connection) as queries:
            evidence = project_setting_evidence(self.project.pk)
        self.assertEqual(len(queries), 3, [row["sql"] for row in queries])
        rows = evidence[("character", str(variant.character_id))]
        self.assertEqual(next(row for row in rows if row["appearance_id"] == str(variant.pk))["primary_setting"]["revision"], 1)
        canonical = self.canonical()
        facts = _bible_creative_drift_projection(canonical)
        character = next(row for row in facts["characters"] if row["id"] == str(variant.character_id))
        self.assertEqual(character["appearance_settings"], rows)
        self.assertNotIn("anchor_asset_id", character)

    def test_declared_optional_blanks_are_displayable_and_editable(self):
        for entity in self.declarations["entities"]:
            entity["identity_anchor"] = ""
            entity["primary"]["anchor"] = ""
            entity["variants"] = []
        self.revision = self.source_revision()
        self.apply()
        primary = BibleVariant.objects.filter(source_binding__isnull=False).first()
        detail = self.detail(primary)
        self.assertEqual(detail["setting"]["appearance_anchor"], "")
        self.assertIsNone(detail["primary_setting"])
        original = primary.revisions.get(revision=1)
        response = self.client.post(f"/api/v1/bible-variants/{primary.pk}/revise/", {
            **setting_payload(original), "name": "明确更名", "expected_revision": 1}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["setting"]["appearance_anchor"], "")
        self.assertEqual(response.data["setting"]["identity_anchor"], "")

    def test_no_appearances_is_explicit_and_later_settings_do_not_rewrite_original_review(self):
        original = self.canonical()
        self.assertTrue(any(original.values()))
        self.assertTrue(all(row["appearance_settings"] == [] for group in original.values() for row in group))
        run, stage = self._approve_recipe_snapshot()
        saved = deepcopy(stage.output_snapshot)
        read_reviewed_bible(run=run, eligible_stage_ids=[stage.pk])
        self.apply()
        with self.assertRaises(ValidationError):
            read_reviewed_bible(run=run, eligible_stage_ids=[stage.pk])
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot, saved)

    def _approve_recipe_snapshot(self):
        recipe = ProductionRecipeVersion(recipe_key=str(uuid4()), version=1, name="Isolated review evidence")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "stages": []}, schema_version=3)
        recipe.save()
        run = ProductionRun.objects.create(project=self.project, owner=self.actor, plan_source="recipe",
            recipe_version=recipe, idempotency_key=str(uuid4()), request_sha256="a" * 64)
        stage = StageRun(production_run=run, stage_key="bible_review", mode="review", ordinal=1,
            status="succeeded", idempotency_key=str(uuid4()))
        canonical = self.canonical()
        from studio.services.json_contracts import json_contract
        assign_json_contract(stage, "output_snapshot", {"schema_version": 1, "stage_key": "bible_review",
            "formal_object_type": "bible", "canonical": canonical,
            "canonical_sha256": json_contract(canonical).sha256})
        stage.save()
        ApprovalDecision.objects.create(stage_run=stage, revision=1, decision="approve", actor="human",
            decided_by=self.actor, output_snapshot_sha256=stage.output_snapshot_sha256)
        return run, stage

    def test_original_reviewed_reader_rejects_changed_settings_and_preserves_old_snapshot(self):
        self.apply()
        run, stage = self._approve_recipe_snapshot()
        saved = deepcopy(stage.output_snapshot)
        reviewed = read_reviewed_bible(run=run, eligible_stage_ids=[stage.pk])
        variant = self.variant()
        original = variant.revisions.get(revision=1)
        self.assertIn("appearance_settings", next(row for row in reviewed["canonical"]["characters"]
            if row["id"] == str(variant.character_id)))
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(original), "appearance_anchor": "人工修订"})
        with self.assertRaises(ValidationError) as caught:
            read_reviewed_bible(run=run, eligible_stage_ids=[stage.pk])
        self.assertEqual(caught.exception.code, "reference_review_changed")
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot, saved)

    def test_setting_review_enters_baseline_but_image_binding_is_separate(self):
        self.apply()
        before = _bible_creative_drift_projection(self.canonical())
        variant = self.variant()
        setting = variant.revisions.get(revision=1)
        review_variant(actor=self.actor, variant=variant, expected_revision=1, expected_sha256=setting.content_sha256,
            decision="approve", comment="隔离审核夹具")
        after = _bible_creative_drift_projection(self.canonical())
        self.assertNotEqual(before, after)
        asset = Asset.objects.create(owner=self.actor, name="仅验证绑定排除", kind="character", mime_type="image/png")
        entity = variant.character
        entity.anchor_asset = asset
        entity.save(update_fields=("anchor_asset",))
        entity.reference_assets.add(asset)
        self.assertEqual(after, _bible_creative_drift_projection(self.canonical()))
        self.assertEqual(BibleVariantReview.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_corrupt_revision_or_current_pointer_is_not_silently_hidden(self):
        self.apply()
        variant = self.variant()
        # Deliberate isolated corruption bypasses append-only writer guards.
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_biblevariantrevision SET appearance_anchor=%s WHERE variant_id=%s",
                ["tampered", variant.pk.hex])
        with self.assertRaises(BibleVariantConflict):
            self.canonical()
        response = self.client.get(f"/api/v1/bible-variants/{variant.pk}/")
        self.assertEqual(response.status_code, 409)

    def test_missing_current_revision_rejects_approval_projection(self):
        self.apply()
        variant = self.variant()
        BibleVariant.objects.filter(pk=variant.pk).update(revision=99)
        with self.assertRaises(BibleVariantConflict):
            self.canonical()

    def test_detail_and_history_preserve_source_schema_and_permissions(self):
        self.apply()
        variant = self.variant()
        history = self.client.get(f"/api/v1/bible-variants/{variant.pk}/revisions/")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["results"][0]["payload_schema_version"], 2)
        self.assertNotIn("identity_anchor", history.data["results"][0])
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user(username="not-the-project-owner")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(f"/api/v1/bible-variants/{variant.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/bible-variants/{variant.pk}/revisions/").status_code, 404)


class BibleSettingApiContractTests(SimpleTestCase):
    def test_creation_has_explicit_role_and_primary_reference_but_revise_cannot_rebind(self):
        from drf_spectacular.generators import SchemaGenerator
        components = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        summary = components["BibleVariantSummary"]["properties"]
        detail = components["BibleVariantDetail"]["properties"]
        self.assertTrue({"appearance_role", "source"} <= set(summary))
        self.assertIn("primary_setting", detail)
        self.assertNotIn("edit_requirements", detail)
        self.assertNotIn("BibleSettingEditRequirements", components)
        self.assertNotIn("identity_anchor", components["BibleVariantRevisionSummary"]["properties"])
        creation = components["BibleVariantCreateInputRequest"]
        self.assertTrue({"appearance_role", "primary_setting_revision_id", "primary_setting_sha256"} <= set(creation["required"]))
        self.assertNotIn("source", creation["properties"])
        for schema in ("BibleVariantReviseInputRequest", "BibleVariantReviewInputRequest"):
            self.assertFalse({"appearance_role", "source", "primary_setting"}
                & set(components[schema]["properties"]))
