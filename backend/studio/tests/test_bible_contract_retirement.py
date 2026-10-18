"""M3 cutover invariants on isolated records, never a legacy-data converter."""
from copy import deepcopy
from hashlib import sha256

from django.db import connection
from django.test import SimpleTestCase, TestCase

from studio.models import ApprovalDecision, BibleVariant, BibleVariantRevision, BibleVariantReview, GenerationAttempt
from studio.services.bible_reference_contract import bible_reference_name_key
from studio.services.bible_setting_evidence import read_setting_revisions
from studio.services.bible_variants import BibleVariantConflict, setting_payload
from studio.services.json_contracts import canonical_json_bytes
from studio.services.script_semantic_relations import script_relation_errors
from studio.tests import test_bible_variants as api_fixtures
from studio.tests import test_script_reference_materialization as materialization_fixtures
from studio.tests import test_bible_policy_continuation as policy_fixtures
from studio.tests.structured_bible_fixtures import confirmed_structured_bible_source, entities_fixture


def retire_fixture(setting):
    """Stage an actual old body-only digest using isolated corruption access."""
    digest = sha256(canonical_json_bytes(setting_payload(setting))).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute("UPDATE studio_biblevariantrevision SET payload_schema_version=1, content_sha256=%s WHERE id=%s",
            [digest, setting.pk.hex])
    return digest


class BibleReferenceContractTests(SimpleTestCase):
    def test_normalized_conflicts_do_not_translate_or_rewrite_authored_names(self):
        for first, second in (("ＡＢＣ", "abc"), ("Straße", "STRASSE"), ("Cafe\u0301", "Café"), ("雨衣", "雨衣")):
            with self.subTest(first=first):
                self.assertEqual(bible_reference_name_key(first), bible_reference_name_key(second))
                value = {"entities": [{"key": "one", "kind": "character", "name": "同名人物",
                    "primary": {"reference_name": first}, "variants": [{"key": "second", "name": "差异",
                        "reference_name": second}]}], "episodes": []}
                original = deepcopy(value)
                errors = script_relation_errors(value, provenance=[], consume=lambda _: None)
                self.assertEqual([(row["code"], row["path"]) for row in errors], [
                    ("reference_name_duplicate", "value.entities[0].variants[0].reference_name")])
                self.assertEqual(value, original)
        self.assertNotEqual(bible_reference_name_key("雨衣"), bible_reference_name_key("raincoat"))

    def test_new_model_default_is_current_contract_not_retired_body_only_hash(self):
        self.assertEqual(BibleVariantRevision().payload_schema_version, 2)

    def test_metadata_help_does_not_advertise_retired_writer(self):
        from studio.serializers import SCRIPT_BIBLE_METADATA_HELP
        self.assertIn("无写入入口", SCRIPT_BIBLE_METADATA_HELP)
        self.assertNotIn("apply", SCRIPT_BIBLE_METADATA_HELP)


class BibleRetiredSettingApiTests(TestCase):
    setUp = api_fixtures.BibleVariantTests.setUp
    create = api_fixtures.BibleVariantTests.create
    detail_url = api_fixtures.BibleVariantTests.detail_url
    review = api_fixtures.BibleVariantTests.review
    revise = api_fixtures.BibleVariantTests.revise

    def test_old_digest_cannot_be_read_revised_or_reviewed_and_is_never_upgraded(self):
        row = self.create()
        setting = BibleVariantRevision.objects.get(pk=row["setting"]["id"])
        digest = retire_fixture(setting)
        row["setting"]["content_sha256"] = digest
        before = list(BibleVariantRevision.objects.values())
        responses = [self.client.get(self.detail_url(row)),
            self.client.get(self.detail_url(row), {"revision": 1}),
            self.client.get(self.detail_url(row) + "source/", {"revision": 1}),
            self.revise(row, appearance_anchor="不能用新修订洗掉旧合同"), self.review(row)]
        for response in responses:
            self.assertEqual(response.status_code, 409, response.data)
            self.assertIn("设定摘要版本已停用", str(response.data))
        with self.assertRaisesMessage(BibleVariantConflict, "设定摘要版本已停用"):
            read_setting_revisions([setting.pk], project_id=self.project.pk)
        self.assertEqual(list(BibleVariantRevision.objects.values()), before)
        self.assertFalse(BibleVariantReview.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_old_primary_cannot_become_new_variant_dependency(self):
        row = self.create()
        digest = retire_fixture(BibleVariantRevision.objects.get(pk=row["setting"]["id"]))
        response = self.client.post(self.url, {**self.value, "identity_anchor": "", "reference_name": "新变体",
            "project_id": str(self.project.pk), "entity_kind": "character", "entity_id": str(self.character.pk),
            "appearance_role": "variant", "primary_setting_revision_id": row["setting"]["id"],
            "primary_setting_sha256": digest}, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("设定摘要版本已停用", str(response.data))
        self.assertEqual(BibleVariant.objects.count(), 1)
        self.assertEqual(BibleVariantRevision.objects.count(), 1)


class BibleMappedDeclarationRetirementTests(TestCase):
    setUp = materialization_fixtures.ScriptReferenceMaterializationTests.setUp
    source_revision = materialization_fixtures.ScriptReferenceMaterializationTests.source_revision
    frozen = materialization_fixtures.ScriptReferenceMaterializationTests.frozen
    apply = materialization_fixtures.ScriptReferenceMaterializationTests.apply

    def test_all_field_layouts_keep_multilingual_anchors_without_old_parser_or_renderer(self):
        entities = entities_fixture()
        entities[0]["name"] = "海遥・لينا"
        entities[0]["identity_anchor"] = "  黑色短发\n目元に小さな傷  "
        entities[0]["primary"]["reference_name"] = "لينا・基準"
        entities[0]["primary"]["anchor"] = ""
        entities[0]["variants"][0]["reference_name"] = "海遥・制服"
        entities[0]["variants"][1]["reference_name"] = "لينا・雨衣"
        identifiers = None
        for layout in ("a", "b", "renamed"):
            self.revision = confirmed_structured_bible_source(self, entities=entities, layout=layout)
            result = self.apply()
            current = {row["entity_key"]: row["entity_id"] for row in result["entities"]}
            identifiers = identifiers or current
            self.assertEqual(current, identifiers)
            self.assertEqual(self.apply(), result)
        primary = BibleVariant.objects.get(character_id=identifiers["person_a"], appearance_role="primary")
        setting = primary.revisions.get()
        self.assertEqual(primary.character.name, entities[0]["name"])
        self.assertEqual((setting.reference_name, setting.identity_anchor, setting.appearance_anchor),
            ("لينا・基準", "  黑色短发\n目元に小さな傷  ", ""))
        self.assertEqual(BibleVariant.objects.count(), 4)
        self.assertFalse(BibleVariantReview.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())


class BibleRetiredSettingPolicyTests(TestCase):
    setUp = policy_fixtures.BiblePolicyContinuationTests.setUp
    root = policy_fixtures.BiblePolicyContinuationTests.root
    recipe = policy_fixtures.BiblePolicyContinuationTests.recipe
    build = policy_fixtures.BiblePolicyContinuationTests.build

    def test_auto_and_assisted_cannot_approve_legacy_setting_or_invent_human_review(self):
        from studio.services.bible_content_review import recipe_bible_review_detail
        from studio.services.production_runs import advance_production_run
        from studio.services.script_bible_materialization import freeze_script_reference_bible, materialize_script_reference_bible
        materialize_script_reference_bible(actor=self.actor, project_id=self.project.pk, script_revision_id=self.revision.pk,
            expected_source=freeze_script_reference_bible(revision=self.revision, project_id=self.project.pk))
        retire_fixture(BibleVariantRevision.objects.filter(variant__appearance_role="primary").first())
        before = list(BibleVariantRevision.objects.order_by("pk").values())
        for automatic in (True, False):
            with self.subTest(automatic=automatic):
                run = advance_production_run(self.build(automatic=automatic), automatic=True)
                if automatic:
                    self.assertEqual(run.status, "blocked")
                    self.assertEqual(run.blocked_code, "bible_content_policy_invalid")
                else:
                    # Assisted mode waits for its real review action; it must
                    # not become an automatic approval or an eager policy gate.
                    self.assertEqual(run.status, "awaiting_approval")
                    with self.assertRaisesMessage(BibleVariantConflict, "设定摘要版本已停用"):
                        recipe_bible_review_detail(run)
                self.assertFalse(ApprovalDecision.objects.exists())
                self.assertFalse(BibleVariantReview.objects.exists())
                self.assertEqual(list(BibleVariantRevision.objects.order_by("pk").values()), before)
        self.assertFalse(GenerationAttempt.objects.exists())
