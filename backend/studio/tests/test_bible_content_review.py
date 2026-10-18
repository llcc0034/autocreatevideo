"""Isolated review invariants; native original-API coverage is in manual Bible tests."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import BibleVariant, BibleVariantRevision, BibleVariantReview, ScriptBibleEntityBinding
from studio.services.bible_content_review import approve_bible_content, bible_review_inventory
from studio.services.bible_variants import BibleVariantConflict, revise_variant, review_variant, setting_payload
from studio.tests import test_script_reference_materialization as fixtures


class BibleContentReviewTests(TestCase):
    setUp = fixtures.ScriptReferenceMaterializationTests.setUp
    source_revision = fixtures.ScriptReferenceMaterializationTests.source_revision
    frozen = fixtures.ScriptReferenceMaterializationTests.frozen
    apply = fixtures.ScriptReferenceMaterializationTests.apply
    manual_primary_before_appearance_declarations = fixtures.ScriptReferenceMaterializationTests.manual_primary_before_appearance_declarations

    def run_context(self):
        return SimpleNamespace(project=self.project, project_id=self.project.pk, script_revision_id=self.revision.pk)

    def request(self):
        frozen = self.frozen()
        baseline, candidates, settings = bible_review_inventory(run=self.run_context(), frozen=frozen)
        return dict(run=self.run_context(), actor=self.actor, frozen=frozen, comment="隔离明确审核",
            expected_project_bible_sha256=baseline.sha256, reviewed_project_bible=True,
            reviewed_candidate_ids=[row["id"] for row in candidates], reviewed_setting_keys=[row["review_key"] for row in settings])

    def test_read_shows_all_authored_settings_and_commit_reviews_them_without_media(self):
        request = self.request()
        self.assertEqual(len(request["reviewed_setting_keys"]), 4)
        self.assertFalse(BibleVariant.objects.exists())
        result = approve_bible_content(**request)
        self.assertEqual(result["kind"], "bible_content_review")
        self.assertEqual(BibleVariantReview.objects.count(), 4)
        self.assertFalse(BibleVariantRevision.objects.exclude(basis_asset=None).exists())
        self.assertFalse(BibleVariantReview.objects.exclude(decided_by=self.actor).exists())
        self.assertTrue(ScriptBibleEntityBinding.objects.get(entity_key="person_a").character.is_locked)

    def test_complete_inventory_uses_exact_settings_without_entity_body_columns(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.declarations["entities"].append({"key": "case", "kind": "prop", "name": "盒子",
            "identity_anchor": "木制盒子", "primary": {"reference_name": "盒子·主图", "anchor": "方形盒盖"}})
        self.revision = self.source_revision()
        self.apply()
        with CaptureQueriesContext(connection) as queries:
            baseline, candidates, settings = bible_review_inventory(run=self.run_context(), frozen=self.frozen())
        for row in queries:
            self.assertNotIn('"visual_identity"', row["sql"])
            self.assertFalse(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")))
        for section in ("characters", "scenes", "props"):
            self.assertTrue(baseline.value[section])
            for entity in baseline.value[section]:
                self.assertNotIn("visual_identity", entity)
                self.assertNotIn("identity_anchor", entity)
                self.assertNotIn("primary_setting", entity)
                self.assertIn("appearance_settings", entity)
        for candidate in candidates:
            self.assertNotIn("visual_identity", candidate)
            self.assertNotIn("visual_identity", candidate["existing"])
        self.assertTrue(any(row["identity_anchor"] for row in settings if row["kind"] == "stored"))
        refreshed, _, _ = bible_review_inventory(run=self.run_context(), frozen=self.frozen())
        self.assertEqual(refreshed.sha256, baseline.sha256)

    def test_identity_declaration_without_primary_is_source_not_an_invented_setting(self):
        from studio.production_manual_serializers import ManualBibleCandidateSerializer
        for identity in (None, "", "源中明确的共同身份"):
            with self.subTest(identity=identity):
                entity = {"key": "identity_only", "kind": "character", "name": "只有身份声明"}
                if identity is not None:
                    entity["identity_anchor"] = identity
                self.declarations["entities"] = [entity]
                self.revision = self.source_revision()
                _, candidates, settings = bible_review_inventory(run=self.run_context(), frozen=self.frozen())
                self.assertEqual(candidates[0]["identity_anchor"], identity)
                self.assertNotIn("visual_identity", candidates[0])
                self.assertEqual(ManualBibleCandidateSerializer(candidates[0]).data["identity_anchor"], identity)
                self.assertEqual(settings, [])
                self.assertFalse(BibleVariant.objects.exists())
                self.assertFalse(ScriptBibleEntityBinding.objects.exists())

    def test_new_revision_review_reuses_project_keys_and_preserves_authored_and_stored_values(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from studio.models import BibleEntityIdentity
        approve_bible_content(**self.request())
        original_reviews = list(BibleVariantReview.objects.order_by("pk").values())
        identity = BibleEntityIdentity.objects.get(project=self.project, entity_key="person_a")
        original_name = identity.character.name
        self.declarations["entities"][0]["name"] = "新来源的显示名"
        self.declarations["entities"][0]["primary"]["anchor"] = "新来源提出不同设定"
        self.revision = self.source_revision()
        with CaptureQueriesContext(connection) as queries:
            _, candidates, settings = bible_review_inventory(run=self.run_context(), frozen=self.frozen())
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for row in queries))
        self.assertTrue(all(row["action"] == "reuse" for row in candidates))
        candidate = next(row for row in candidates if row["entity_type"] == "character")
        self.assertEqual(candidate["existing"]["id"], str(identity.character_id))
        self.assertEqual(candidate["existing"]["name"], original_name)
        declared = next(row for row in settings if row["kind"] == "declaration" and row["entity_key"] == "person_a"
            and row["appearance_role"] == "primary")
        self.assertEqual(declared["appearance_anchor"], "新来源提出不同设定")
        self.assertFalse(declared["source_facts"]["already_materialized"])
        approve_bible_content(**self.request())
        identity.character.refresh_from_db()
        self.assertEqual(identity.character.name, original_name)
        self.assertEqual(BibleVariant.objects.count(), 4)
        self.assertEqual(list(BibleVariantReview.objects.order_by("pk").values()), original_reviews)

    def test_missing_duplicate_extra_keys_and_wrong_baseline_never_write(self):
        request = self.request()
        for key, value in (("reviewed_setting_keys", []), ("reviewed_setting_keys", request["reviewed_setting_keys"] * 2),
                ("reviewed_setting_keys", request["reviewed_setting_keys"] + ["another"]), ("reviewed_candidate_ids", []),
                ("reviewed_project_bible", False), ("expected_project_bible_sha256", "f" * 64)):
            with self.subTest(key=key, value=value), self.assertRaises((ValidationError, BibleVariantConflict)):
                approve_bible_content(**{**request, key: value})
            self.assertFalse(ScriptBibleEntityBinding.objects.exists())

    def test_reused_manual_primary_remains_manual_and_is_reviewed_with_new_declarations(self):
        primary = self.manual_primary_before_appearance_declarations()
        _, candidates, settings = bible_review_inventory(run=self.run_context(), frozen=self.frozen())
        self.assertTrue(all(row["action"] == "reuse" for row in candidates))
        stored = next(row for row in settings if row["kind"] == "stored")
        self.assertIsNone(stored["setting"]["source"])
        self.assertEqual(stored["identity_anchor"], "人工登记的共同身份")
        result = approve_bible_content(**self.request())
        self.assertEqual(BibleVariant.objects.count(), 4)
        self.assertEqual(BibleVariantReview.objects.count(), 4)
        primary.refresh_from_db()
        self.assertIsNone(primary.source_binding_id)
        self.assertEqual(primary.revisions.get().review.decided_by_id, self.actor.pk)
        person = next(row for row in result["materialization"]["entities"] if row["entity_key"] == "person_a")
        self.assertEqual(person["settings"][0]["appearance_id"], str(primary.pk))
        reviews = list(BibleVariantReview.objects.order_by("pk").values())
        approve_bible_content(**self.request())
        self.assertEqual(list(BibleVariantReview.objects.order_by("pk").values()), reviews)

    def test_rejected_manual_primary_does_not_get_replaced_by_declaration(self):
        primary = self.manual_primary_before_appearance_declarations()
        setting = primary.revisions.get()
        review_variant(actor=self.actor, variant=primary, expected_revision=1,
            expected_sha256=setting.content_sha256, decision="reject", comment="明确拒绝")
        with self.assertRaises(BibleVariantConflict): approve_bible_content(**self.request())
        self.assertEqual(BibleVariant.objects.count(), 1)
        self.assertFalse(ScriptBibleEntityBinding.objects.filter(script_revision=self.revision).exists())
        self.assertEqual(BibleVariantReview.objects.get().decision, "reject")

    def test_review_failure_rolls_back_whole_materialization(self):
        original = review_variant
        calls = []
        def fail_second(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2: raise ValidationError("isolated decision failure")
            return original(**kwargs)
        with patch("studio.services.bible_content_review.review_variant", side_effect=fail_second), self.assertRaises(ValidationError):
            approve_bible_content(**self.request())
        self.assertEqual(len(calls), 2)
        self.assertFalse(BibleVariant.objects.exists())
        self.assertFalse(BibleVariantReview.objects.exists())
        self.assertFalse(ScriptBibleEntityBinding.objects.exists())

    def test_replay_reviews_current_not_original_and_preserves_historical_primary(self):
        self.apply()
        primary = BibleVariant.objects.get(source_binding__entity_key="person_a", appearance_role="primary")
        original = primary.revisions.get(revision=1)
        revise_variant(actor=self.actor, variant=primary, expected_revision=1,
            value={**setting_payload(original), "identity_anchor": "明确的新身份说明"})
        variant = BibleVariant.objects.get(source_variant_key="uniform")
        prior = variant.revisions.get(revision=1)
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(prior), "appearance_anchor": "人工改为蓝衣"})
        _, _, rows = bible_review_inventory(run=self.run_context(), frozen=self.frozen())
        current = next(row for row in rows if row["kind"] == "stored" and row["setting"]["appearance_id"] == str(variant.pk))
        self.assertEqual(current["appearance_anchor"], "人工改为蓝衣")
        self.assertEqual(current["primary_setting"]["revision"], 1)
        self.assertEqual(current["primary_setting"]["identity_anchor"], original.identity_anchor)
        approve_bible_content(**self.request())
        self.assertFalse(BibleVariantReview.objects.filter(setting=original).exists())
        self.assertFalse(BibleVariantReview.objects.filter(setting=prior).exists())
        self.assertTrue(BibleVariantReview.objects.filter(setting__variant=variant, setting__revision=2).exists())
        saved = list(BibleVariantReview.objects.values())
        approve_bible_content(**self.request())
        self.assertEqual(list(BibleVariantReview.objects.values()), saved)

    def test_rejected_current_setting_is_not_overwritten_or_auto_adopted(self):
        self.apply()
        variant = BibleVariant.objects.get(source_variant_key="uniform")
        current = variant.revisions.get(revision=1)
        review_variant(actor=self.actor, variant=variant, expected_revision=1,
            expected_sha256=current.content_sha256, decision="reject", comment="原拒绝")
        saved = deepcopy(list(BibleVariantReview.objects.values()))
        with self.assertRaises(BibleVariantConflict): approve_bible_content(**self.request())
        self.assertEqual(list(BibleVariantReview.objects.values()), saved)

    def test_current_revision_drift_invalidates_previously_displayed_set(self):
        self.apply()
        request = self.request()
        variant = BibleVariant.objects.get(source_variant_key="uniform")
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(variant.revisions.get(revision=1)), "appearance_anchor": "审核期间变化"})
        with self.assertRaises(BibleVariantConflict): approve_bible_content(**request)
        self.assertFalse(BibleVariantReview.objects.exists())

    def test_inventory_rechecks_exact_reference_not_only_body_digest(self):
        self.apply()
        baseline, _, _ = bible_review_inventory(run=self.run_context())
        for field, value in (("source", None), ("primary_setting", None),
                ("review", {"id": "invented", "decision": "approve", "decided_by_id": "invented"})):
            changed = deepcopy(baseline.value)
            row = next(ref for entity in changed["characters"] for ref in entity["appearance_settings"]
                if ref["appearance_role"] == "variant")
            row[field] = value
            with self.subTest(field=field), patch("studio.services.production_runs._canonical_bible", return_value=changed):
                with self.assertRaisesMessage(BibleVariantConflict, "审核读取期间设定已变化"):
                    bible_review_inventory(run=self.run_context())
        self.assertFalse(BibleVariantReview.objects.exists())

    def test_commit_rejects_missing_requested_revision_instead_of_skipping_it(self):
        from studio.services.bible_setting_evidence import read_setting_revisions
        self.apply()
        request = self.request()
        calls = 0
        def missing_at_commit(ids, **kwargs):
            nonlocal calls
            calls += 1
            return read_setting_revisions([*ids, "11111111-1111-4111-8111-111111111111"] if calls == 2 else ids, **kwargs)
        with patch("studio.services.bible_content_review.read_setting_revisions", side_effect=missing_at_commit):
            with self.assertRaises(BibleVariantConflict):
                approve_bible_content(**request)
        self.assertEqual(calls, 2)
        self.assertFalse(BibleVariantReview.objects.exists())
