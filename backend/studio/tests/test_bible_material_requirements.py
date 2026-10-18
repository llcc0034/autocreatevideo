"""M3 demand projection only; isolated data, no M4 dispatch or media adoption."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image

from studio.models import ApprovalDecision, Asset, BibleVariant, BibleVariantReview, Episode, ExecutionPreparation, GeneratedArtifact, GenerationAttempt
from studio.services.bible_material_requirements import (
    build_bible_material_requirements, project_bible_material_requirements, read_bible_material_requirements,
)
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.production_runs import _canonical_bible, advance_production_run, decide_stage
from studio.tests import test_bible_policy_continuation as policy_fixture
from studio.tests import test_production_manual_bible as manual_fixture
from studio.tests import test_script_reference_materialization as source_fixture


class BibleMaterialProjectionTests(TestCase):
    setUp = source_fixture.ScriptReferenceMaterializationTests.setUp
    source_revision = source_fixture.ScriptReferenceMaterializationTests.source_revision
    frozen = source_fixture.ScriptReferenceMaterializationTests.frozen
    apply = source_fixture.ScriptReferenceMaterializationTests.apply

    def rows(self):
        from types import SimpleNamespace
        value = _canonical_bible(SimpleNamespace(project_id=self.project.pk))
        return {(kind, row["id"]): row for kind, section in
            (("character", "characters"), ("scene", "scenes"), ("prop", "props")) for row in value[section]}

    def project_requirements(self, rows=None):
        return project_bible_material_requirements(rows=self.rows() if rows is None else rows, project_id=self.project.pk)

    def test_dynamic_entities_only_expand_authored_settings_without_candidates_or_images(self):
        self.declarations["entities"].extend({"key": f"prop_{i}", "kind": "prop", "name": "同名道具",
            "primary": {"reference_name": f"道具{i}", "anchor": ""}, "variants": []} for i in range(12))
        self.declarations["entities"].append({"key": "no_look", "kind": "prop", "name": "没有外观声明"})
        self.revision = self.source_revision()
        self.apply()
        assets_before = list(Asset.objects.order_by("pk").values())
        targets = self.project_requirements()
        self.assertEqual(len(targets), 16)  # Original four settings + twelve primary-only props.
        self.assertEqual(sum(row["appearance_role"] == "variant" for row in targets), 2)
        self.assertEqual(sum(row["scope"]["kind"] == "prop" for row in targets), 12)
        self.assertTrue(all(row["declared_reference_asset"] is None for row in targets))
        self.assertNotIn("candidate_number", str(targets))
        self.assertNotIn("asset_id", str(targets))
        self.assertEqual(list(Asset.objects.order_by("pk").values()), assets_before)
        for model in (ExecutionPreparation, GenerationAttempt, GeneratedArtifact):
            self.assertFalse(model.objects.exists())

    def test_empty_and_explicit_subset_are_not_a_fallback_to_all_project_entities(self):
        with self.assertNumQueries(0):
            self.assertEqual(project_bible_material_requirements(rows={}, project_id=self.project.pk), [])
        self.apply()
        rows = {key: row for key, row in self.rows().items() if key[0] == "scene"}
        targets = self.project_requirements(rows)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["appearance_role"], "primary")
        self.assertIsNone(targets[0]["primary_dependency"])

    def test_exact_old_primary_dependency_survives_new_primary_revision(self):
        self.apply()
        child = BibleVariant.objects.get(source_variant_key="uniform").revisions.get()
        old = child.primary_setting_revision
        revise_variant(actor=self.actor, variant=old.variant, expected_revision=1,
            value={**setting_payload(old), "identity_anchor": "新版共同身份"})
        targets = self.project_requirements()
        current = next(row for row in targets if row["scope"]["kind"] == "character" and row["appearance_role"] == "primary")
        self.assertEqual(current["setting"]["revision"], 2)
        children = [row for row in targets if row["appearance_role"] == "variant"]
        self.assertEqual(len(children), 2)
        for row in children:
            self.assertEqual(row["primary_dependency"]["setting_revision_id"], str(old.pk))
            self.assertEqual(row["primary_dependency"]["content_sha256"], old.content_sha256)
            self.assertNotEqual(row["primary_dependency"], current["setting"])
        self.assertNotIn("identity_anchor", str(targets))  # Bodies remain in the original revisions.

    def test_declared_asset_is_a_reference_input_not_an_adopted_result_or_child_primary(self):
        self.apply()
        child = BibleVariant.objects.get(source_variant_key="uniform").revisions.get()
        path = Path(self.enterContext(TemporaryDirectory())) / "reference.png"
        Image.new("RGB", (12, 12), "white").save(path)
        digest = sha256(path.read_bytes()).hexdigest()
        asset = Asset.objects.create(owner=self.actor, name="隔离参考图", kind="character",
            local_path=str(path), mime_type="image/png", checksum=digest)
        revise_variant(actor=self.actor, variant=child.variant, expected_revision=1,
            value={**setting_payload(child), "basis_asset_id": str(asset.pk), "basis_asset_sha256": digest})
        row = next(row for row in self.project_requirements() if row["setting"]["appearance_id"] == str(child.variant_id))
        self.assertEqual(row["declared_reference_asset"], {"asset_id": str(asset.pk), "sha256": digest})
        self.assertNotIn("asset_id", row["primary_dependency"])
        self.assertNotIn("selected_asset", row)
        self.assertNotIn("ready", row)
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_duplicate_missing_wrong_entity_and_tampered_refs_fail_closed(self):
        self.apply()
        original = self.rows()
        key = next(key for key, row in original.items() if len(row["appearance_settings"]) == 3)
        for change in ("duplicate", "missing_primary", "wrong_entity", "wrong_hash", "missing_revision"):
            rows = deepcopy(original)
            refs = rows[key]["appearance_settings"]
            if change == "duplicate":
                refs.append(deepcopy(refs[0]))
            elif change == "missing_primary":
                rows[key]["appearance_settings"] = [ref for ref in refs if ref["appearance_role"] == "variant"]
            elif change == "wrong_entity":
                rows[key]["id"] = str(uuid4())
            elif change == "wrong_hash":
                refs[0]["content_sha256"] = "0" * 64
            else:
                refs[0]["setting_revision_id"] = str(uuid4())
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.project_requirements(rows)

    def test_exact_references_reject_foreign_project_and_corrupt_historical_primary(self):
        self.apply()
        rows = self.rows()
        with self.assertRaises(ValidationError):
            project_bible_material_requirements(rows=rows, project_id=uuid4())
        child = BibleVariant.objects.get(source_variant_key="uniform").revisions.get()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_biblevariantrevision SET appearance_anchor=%s WHERE id=%s",
                ["isolated corruption", child.primary_setting_revision_id.hex])
        with self.assertRaises(ValidationError):
            self.project_requirements(rows)

    def test_projection_is_deterministic_one_indexed_query_and_has_no_writes(self):
        self.apply()
        rows = self.rows()
        before = deepcopy(rows)
        with CaptureQueriesContext(connection) as queries:
            first = self.project_requirements(rows)
        self.assertEqual(len(queries), 1)
        self.assertEqual(first, self.project_requirements(dict(reversed(list(rows.items())))))
        self.assertEqual(rows, before)
        sql = queries[0]["sql"]
        for unwanted in ('"studio_scriptrevision"."content"', '"metadata"', "SELECT *"):
            self.assertNotIn(unwanted, sql)
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + sql)
                plan = "\n".join(str(row[-1]) for row in cursor.fetchall())
            self.assertIn("SEARCH studio_biblevariantrevision USING INDEX", plan)
            self.assertNotIn("SCAN studio_biblevariantrevision", plan)

    def snapshot(self, materialization):
        from types import SimpleNamespace
        return build_bible_material_requirements(project_id=self.project.pk,
            canonical=_canonical_bible(SimpleNamespace(project_id=self.project.pk)), materialization=materialization).value

    def test_current_source_excludes_other_entities_and_old_looks_of_the_same_entity(self):
        first = self.apply()
        original = self.snapshot(first)
        self.declarations["entities"] = [deepcopy(self.declarations["entities"][0])]
        self.declarations["entities"][0]["variants"] = []
        self.revision = self.source_revision()
        second = self.apply()
        output = self.snapshot(second)
        self.assertEqual(len(original["targets"]), 4)
        self.assertEqual(len(output["targets"]), 1)
        self.assertEqual(output["targets"][0]["scope"]["kind"], "character")
        self.assertEqual(output["targets"][0]["appearance_role"], "primary")
        self.assertEqual(output["source"], second["source"])
        self.assertEqual(BibleVariant.objects.count(), 4)  # No old settings deleted or silently included.
        self.assertEqual(self.snapshot(first), original)

    def test_declared_identity_without_looks_never_inherits_existing_looks(self):
        self.apply()
        self.declarations["entities"] = [{key: row[key] for key in ("key", "kind", "name")}
            for row in self.declarations["entities"]]
        self.revision = self.source_revision()
        output = self.snapshot(self.apply())
        self.assertEqual(output["targets"], [])
        self.assertEqual(BibleVariant.objects.count(), 4)

    def test_source_identity_selects_current_review_revision_without_reverting_to_bound_revision(self):
        materialization = self.apply()
        child = BibleVariant.objects.get(source_variant_key="uniform").revisions.get()
        revise_variant(actor=self.actor, variant=child.variant, expected_revision=1,
            value={**setting_payload(child), "appearance_anchor": "已修改的外观"})
        output = self.snapshot(materialization)
        target = next(row for row in output["targets"] if row["setting"]["appearance_id"] == str(child.variant_id))
        self.assertEqual(target["setting"]["revision"], 2)
        self.assertEqual(target["primary_dependency"]["setting_revision_id"], str(child.primary_setting_revision_id))
        source = next(row for row in materialization["entities"] if row["entity_kind"] == "character")
        self.assertEqual(next(row for row in source["settings"] if row["variant_key"] == "uniform")["setting_revision_id"], str(child.pk))

    def test_manual_project_review_has_explicit_non_script_source(self):
        self.apply()
        output = self.snapshot(None)
        self.assertEqual(output["source"], {"kind": "project_bible"})
        self.assertEqual(len(output["targets"]), 4)

    def test_invalid_materialization_receipts_cannot_guess_a_selection(self):
        original = self.apply()
        for change in ("missing_receipt", "missing_entity", "duplicate_entity", "duplicate_look", "missing_look"):
            receipt = deepcopy(original)
            if change == "missing_receipt":
                receipt = {}
            elif change == "missing_entity":
                receipt["entities"][0]["entity_id"] = str(uuid4())
            elif change == "duplicate_entity":
                receipt["entities"].append(deepcopy(receipt["entities"][0]))
            elif change == "duplicate_look":
                receipt["entities"][0]["settings"].append(deepcopy(receipt["entities"][0]["settings"][0]))
            else:
                receipt["entities"][0]["settings"][0]["appearance_id"] = str(uuid4())
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.snapshot(receipt)


class BibleMaterialRunBoundaryTests(TestCase):
    setUp = policy_fixture.BiblePolicyContinuationTests.setUp
    root = policy_fixture.BiblePolicyContinuationTests.root
    recipe = policy_fixture.BiblePolicyContinuationTests.recipe
    build = policy_fixture.BiblePolicyContinuationTests.build
    reviewed = policy_fixture.BiblePolicyContinuationTests.reviewed

    def approved(self):
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("No provider")))
        run = advance_production_run(self.build(), automatic=True)
        self.assertEqual(run.stages.get(stage_key="bible_review").status, "succeeded")
        return run

    def read(self, run, **changes):
        stage = run.stages.get(stage_key="bible_review")
        receipt = stage.output_snapshot.get("policy_evidence") or stage.output_snapshot["review_payload"]
        scopes = [{"kind": row["entity_kind"], "id": row["entity_id"]} for row in receipt["materialization"]["entities"]]
        return read_bible_material_requirements(**{"run": run,
            "eligible_stage_ids": list(run.stages.values_list("pk", flat=True)), "scopes": scopes, **changes})

    def test_real_policy_decision_is_preserved_without_dispatch_or_fake_human_review(self):
        run = self.approved()
        first = self.read(run)
        self.assertEqual(len(first.value["targets"]), 4)
        self.assertEqual(first, self.read(run))
        self.assertEqual(first.value["bible_review"], self.reviewed(run)["review"])
        self.assertEqual(run.stages.get(stage_key="bible_review").approval_decisions.get().actor, "recipe_policy")
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        stage = run.stages.get(stage_key="bible_review")
        self.assertEqual(first.value["targets"], stage.output_snapshot["policy_evidence"]["material_requirements"]["targets"])
        self.assertFalse(BibleVariantReview.objects.exists())

    def test_unreviewed_and_ineligible_decisions_do_not_produce_requirements(self):
        run = advance_production_run(self.build(automatic=False), automatic=True)
        with self.assertRaises(ValidationError):
            read_bible_material_requirements(run=run, eligible_stage_ids=list(run.stages.values_list("pk", flat=True)), scopes=[])
        run = self.approved()
        with self.assertRaises(ValidationError):
            self.read(run, eligible_stage_ids=[])

    def test_invalid_duplicate_missing_foreign_and_unsupported_scopes_are_rejected(self):
        run = self.approved()
        row = self.reviewed(run)["canonical"]["characters"][0]
        valid = {"kind": "character", "id": row["id"]}
        for scopes in (None, [valid, valid], [{**valid, "id": "bad-id"}], [{**valid, "id": str(uuid4())}],
                [{**valid, "kind": "shot"}], [{**valid, "name": "must not match by name"}]):
            with self.subTest(scopes=scopes), self.assertRaises(ValidationError):
                self.read(run, scopes=scopes)
        self.assertEqual(self.read(run, scopes=[]).value["targets"], [])

    def test_source_changes_fail_without_rewriting_original_approval(self):
        run = self.approved()
        original = self.read(run)
        stage = run.stages.get(stage_key="bible_review")
        frozen = deepcopy(stage.output_snapshot)
        variant = BibleVariant.objects.get(source_variant_key="uniform")
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(variant.revisions.get()), "appearance_anchor": "明确修订"})
        with self.assertRaises(ValidationError):
            read_bible_material_requirements(run=run, eligible_stage_ids=[stage.pk],
                scopes=[row["scope"] for row in original.value["targets"] if row["appearance_role"] == "primary"])
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot, frozen)

    def test_assisted_recipe_uses_real_human_approval_with_the_same_requirement_contract(self):
        from studio.services.bible_content_review import recipe_bible_review_detail
        run = advance_production_run(self.build(automatic=False), automatic=True)
        detail = recipe_bible_review_detail(run)
        run = decide_stage(run, stage_key="bible_review", decision="approve", comment="isolated fixture", user=self.actor,
            bible_review={"expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
                "expected_project_bible_sha256": detail["project_bible_sha256"], "reviewed_project_bible": True,
                "reviewed_candidate_ids": [row["id"] for row in detail["candidates"]],
                "reviewed_setting_keys": [row["review_key"] for row in detail["settings"]]})
        output = self.read(run).value
        self.assertEqual(len(output["targets"]), 4)
        decision = run.stages.get(stage_key="bible_review").approval_decisions.get()
        self.assertEqual((decision.actor, decision.decided_by_id), ("human", self.actor.pk))
        self.assertEqual(output["bible_review"]["decision_id"], str(decision.pk))
        self.assertEqual(output["targets"], decision.stage_run.output_snapshot["review_payload"]["material_requirements"]["targets"])

    def test_saved_output_missing_or_changed_fails_even_if_envelope_is_internally_consistent(self):
        from studio.services.json_contracts import assign_json_contract, DATABASE_JSON_FIELD_MAX_BYTES
        run = self.approved()
        stage = run.stages.get(stage_key="bible_review")
        original = deepcopy(stage.output_snapshot)
        for change in ("missing", "wrong_target", "extra_target", "wrong_source"):
            altered = deepcopy(original)
            receipt = altered["policy_evidence"]
            if change == "missing":
                receipt.pop("material_requirements")
            elif change == "wrong_target":
                receipt["material_requirements"]["targets"][0]["setting"]["content_sha256"] = "0" * 64
            elif change == "extra_target":
                receipt["material_requirements"]["targets"].append(deepcopy(receipt["material_requirements"]["targets"][0]))
            else:
                receipt["materialization"]["source"]["declaration_sha256"] = "0" * 64
            assign_json_contract(stage, "output_snapshot", altered, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
            stage.save()
            stage.approval_decisions.update(output_snapshot_sha256=stage.output_snapshot_sha256)
            with self.subTest(change=change), self.assertRaises(ValidationError), CaptureQueriesContext(connection) as queries:
                self.read(run)
            self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for row in queries))
            stage.refresh_from_db()
            self.assertEqual(stage.output_snapshot, altered)  # Read cannot backfill or repair.

    def test_requirement_output_failure_rolls_back_the_original_decision_transaction(self):
        with patch("studio.services.bible_material_requirements.build_bible_material_requirements",
                side_effect=ValidationError("isolated output failure")):
            result = advance_production_run(self.build(), automatic=True)
        self.assertEqual(result.status, "blocked")
        self.assertFalse(BibleVariant.objects.exists())
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_real_policy_output_contains_only_the_current_source_and_preserves_old_settings(self):
        from studio.services.script_bible_materialization import freeze_script_reference_bible, materialize_script_reference_bible
        from studio.tests.structured_bible_fixtures import confirmed_structured_bible_source, entities_fixture
        materialize_script_reference_bible(actor=self.actor, project_id=self.project.pk, script_revision_id=self.revision.pk,
            expected_source=freeze_script_reference_bible(revision=self.revision, project_id=self.project.pk))
        declarations = entities_fixture()[:1]
        declarations[0]["variants"] = []
        self.revision = confirmed_structured_bible_source(self, entities=declarations)
        run = self.approved()
        output = self.read(run).value
        self.assertEqual(len(output["targets"]), 1)
        self.assertEqual(output["targets"][0]["appearance_role"], "primary")
        self.assertEqual(output["source"]["structure_source"]["script"]["id"], str(self.revision.pk))
        self.assertEqual(BibleVariant.objects.count(), 4)
        scene = self.reviewed(run)["canonical"]["scenes"][0]
        with self.assertRaisesMessage(ValidationError, "不在本次已采用声明"):
            self.read(run, scopes=[{"kind": "scene", "id": scene["id"]}])

    def test_project_owner_mismatch_is_not_a_permission_to_read_other_scopes(self):
        run = self.approved()
        run.owner = self.other
        with self.assertRaisesMessage(ValidationError, "归属不一致"):
            read_bible_material_requirements(run=run, eligible_stage_ids=[], scopes=[])

    def test_episode_scope_does_not_expand_unrelated_project_entities(self):
        episode = Episode.objects.create(project=self.project, number=1, title="Explicit episode scope")
        self.request["scope"] = {"kind": "episodes", "episode_ids": [str(episode.pk)]}
        run = self.approved()
        # Source materialization can create project entities, but this fixture
        # has not assigned any to the selected episode's shots. No scope leak.
        self.assertEqual(len(run.stages.get(stage_key="bible_review").output_snapshot[
            "policy_evidence"]["material_requirements"]["targets"]), 4)
        with self.assertRaisesMessage(ValidationError, "冻结运行范围"):
            self.read(run)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ManualBibleMaterialBoundaryTests(TestCase):
    setUp = manual_fixture.ProductionManualBibleTests.setUp
    create = manual_fixture.ProductionManualBibleTests.create
    post = manual_fixture.ProductionManualBibleTests.post
    read = manual_fixture.ProductionManualBibleTests.read
    adopt_source = manual_fixture.ProductionManualBibleTests.adopt_source
    approval = manual_fixture.ProductionManualBibleTests.approval

    def test_original_manual_api_decision_feeds_the_same_read_only_requirement_contract(self):
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("No provider")))
        self.adopt_source()
        response = self.post(self.approval())
        self.assertEqual(response.status_code, 201, response.data)
        self.run.refresh_from_db()
        state = self.read()
        scopes = [{"kind": kind, "id": row["id"]} for kind, section in
            (("character", "characters"), ("scene", "scenes"), ("prop", "props"))
            for row in state["project_bible"][section]]
        output = read_bible_material_requirements(run=self.run,
            eligible_stage_ids=list(self.run.stages.values_list("pk", flat=True)), scopes=scopes).value
        self.assertEqual(len(output["targets"]), 4)
        self.assertEqual(output["bible_review"]["decision_id"], state["approval"]["decision_id"])
        stage = self.run.stages.get(stage_key="bible_review")
        self.assertEqual(output["targets"], stage.output_snapshot["materialization"]["material_requirements"]["targets"])
        detail = self.client.get(f"/api/v1/production-runs/{self.run.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        public = next(row for row in detail.data["stages"] if row["stage_key"] == "bible_review")
        self.assertEqual(public["output_snapshot"]["materialization"]["material_requirements"]["targets"], output["targets"])
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_manual_requirement_source_cannot_claim_another_adopted_declaration(self):
        from studio.services.json_contracts import assign_json_contract, DATABASE_JSON_FIELD_MAX_BYTES
        self.adopt_source()
        self.assertEqual(self.post(self.approval()).status_code, 201)
        self.run.refresh_from_db()
        stage = self.run.stages.get(stage_key="bible_review")
        altered = deepcopy(stage.output_snapshot)
        receipt = altered["materialization"]
        receipt["materialization"]["source"]["declaration_sha256"] = "0" * 64
        receipt["material_requirements"]["source"]["declaration_sha256"] = "0" * 64
        assign_json_contract(stage, "output_snapshot", altered, schema_version=stage.output_snapshot_schema_version,
            max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        stage.save()
        stage.approval_decisions.update(output_snapshot_sha256=stage.output_snapshot_sha256)
        with self.assertRaisesMessage(ValidationError, "与原来源阶段不一致"):
            read_bible_material_requirements(run=self.run, eligible_stage_ids=[stage.pk], scopes=[])
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot, altered)
