from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse, NoReverseMatch
from rest_framework.test import APIClient

from studio.models import PromptFieldTranslation, PropBible
from studio.services.canonical_glossary import validate_canonical_english
from studio.services.bible_variants import create_variant, revise_variant, review_variant, setting_payload
from studio.services.prompt_translation import TranslationValidationError
from studio.tests.factories import build_story


class CanonicalGlossaryApiTests(TestCase):
    def setUp(self):
        self.user, self.project, self.character, self.scene, _, self.shot, _ = build_story("canonical-owner")
        self.prop = PropBible.objects.create(project=self.project, slug="named-sword", name="玄纹剑",
            description="传家宝剑")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.peer = self.enterContext(patch("studio.services.asset_generation.driver_for"))
        self.original_settings = deepcopy(self.project.production_settings)

    def candidate(self, entity=None, scope="prop", field="name", source_text=None):
        entity = entity or self.prop
        response = self.client.post(reverse("project-create-manual-prompt-fields", args=[self.project.pk]), {
            "entries": [{"scope_type":scope,"scope_id":str(entity.pk),
                "field_key":f"{scope}.{field}",
                "source_text":getattr(entity,field) if source_text is None else source_text}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return PromptFieldTranslation.objects.get(scope_type=scope, scope_id=entity.pk, field_key=f"{scope}.{field}")

    def confirm(self, entry, **changes):
        return self.client.post(reverse("translation-confirm-canonical",args=[entry.pk]), {
            "expected_revision":entry.revision,"translation":"Xuanwen Sword",
            "expected_canonical":self.project.production_settings.get("translation_glossary",{}).get(entry.source_text),
            "expected_project_updated_at":self.project.updated_at.isoformat(), **changes,
        },format="json")

    def tearDown(self):
        self.peer.assert_not_called()

    def test_explicit_confirmation_writes_one_glossary_entry_and_a_current_review_receipt(self):
        entry = self.candidate()
        detail = self.client.get(reverse("translation-detail",args=[entry.pk]))
        self.assertTrue(detail.data["context_current"], detail.data)
        self.assertEqual(detail.data["confirmation_target"],"canonical_glossary")
        self.assertIsNone(detail.data["canonical_value"])
        response = self.confirm(entry)
        self.assertEqual(response.status_code,200,response.data)
        self.assertTrue(response.data["entry"]["context_current"])
        self.assertEqual(response.data["previous_canonical"],None)
        self.assertEqual(response.data["entry"]["status"],"confirmed")
        self.project.refresh_from_db()
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.name,"玄纹剑")
        self.assertEqual(self.project.production_settings,{**self.original_settings,"translation_glossary":{
            **self.original_settings.get("translation_glossary",{}),"玄纹剑":"Xuanwen Sword"}})
        self.assertEqual(response.data["updated_at"],self.project.updated_at.isoformat())
        entry.refresh_from_db()
        self.assertEqual(entry.revision,2)
        self.assertEqual(entry.confirmed_by,self.user)
        self.assertEqual(len(entry.revision_history),1)
        self.assertNotIn("auto_glossary",entry.runtime_metadata)

    def test_replay_returns_same_receipt_without_overwriting_later_project_facts(self):
        entry=self.candidate()
        first=self.confirm(entry)
        self.assertEqual(first.status_code,200,first.data)
        self.project.refresh_from_db()
        saved_at=self.project.updated_at
        self.project.continuity_summary="其他人工修改"
        self.project.save(update_fields=("continuity_summary","updated_at"))
        # Same original observed timestamp, not a newly invented request.
        response=self.confirm(entry,expected_canonical=None,
            expected_project_updated_at=first.data["entry"]["runtime_metadata"]["canonical_adoption"]["previous_project_updated_at"])
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data["updated_at"],saved_at.isoformat())
        entry.refresh_from_db()
        self.assertEqual(entry.revision,2)
        self.project.refresh_from_db()
        self.assertEqual(self.project.continuity_summary,"其他人工修改")

    def test_changed_project_glossary_or_candidate_never_gets_overwritten(self):
        for changed in ("project","glossary","candidate"):
            with self.subTest(changed=changed):
                self.project.refresh_from_db()
                entry=self.candidate()
                if changed=="project":
                    type(self.project).objects.filter(pk=self.project.pk).update(continuity_summary="另一编辑",updated_at=self.project.updated_at.replace(year=2025))
                elif changed=="glossary":
                    type(self.project).objects.filter(pk=self.project.pk).update(production_settings={**self.project.production_settings,"translation_glossary":{"玄纹剑":"Human Name"}})
                else:
                    PromptFieldTranslation.objects.filter(pk=entry.pk).update(revision=entry.revision+1,translated_text="Human draft")
                response=self.confirm(entry,expected_canonical=None)
                self.assertEqual(response.status_code,409,response.data)
                entry.refresh_from_db()
                self.assertNotEqual(entry.status,"confirmed")
                self.assertIsNone(entry.confirmed_at)

    def test_name_and_description_changes_reject_old_candidate(self):
        for field in ("name","description"):
            with self.subTest(field=field):
                self.prop.refresh_from_db()
                entry=self.candidate()
                PropBible.objects.filter(pk=self.prop.pk).update(**{field:getattr(self.prop,field)+"新"})
                response=self.confirm(entry)
                self.assertEqual(response.status_code,409,response.data)
                entry.refresh_from_db()
                self.assertEqual(entry.status,"draft_translation")

    def primary(self, entity=None, scope="prop", **changes):
        entity = entity or self.prop
        return create_variant(actor=self.user, project_id=self.project.pk, entity_kind=scope, entity_id=entity.pk,
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="",
            value={"name": entity.name, "reference_name": f"主图-{scope}",
                "identity_anchor": "共同身份", "appearance_anchor": "基础外观", "basis_asset_id": None,
                "basis_asset_sha256": "", **changes})

    def test_primary_creation_and_revision_invalidate_the_original_name_candidate(self):
        before_primary = self.candidate()
        primary = self.primary()
        self.assertEqual(self.confirm(before_primary).status_code, 409)
        entry = self.candidate()
        original = primary.revisions.get(revision=1)
        revise_variant(actor=self.user, variant=primary, expected_revision=1,
            value={**setting_payload(original), "identity_anchor": "人工修改的共同身份"})
        self.assertEqual(self.confirm(entry).status_code, 409)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "draft_translation")
        self.assertEqual(primary.revisions.get(revision=1).identity_anchor, "共同身份")
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings, self.original_settings)

    def test_old_duplicate_text_and_unrelated_setting_do_not_invalidate_name_candidate(self):
        primary = self.primary()
        setting = primary.revisions.get(revision=1)
        entry = self.candidate()
        self.primary(self.scene, "scene")
        review_variant(actor=self.user, variant=primary, expected_revision=1,
            expected_sha256=setting.content_sha256, decision="approve", comment="隔离审核，不改正文")
        response = self.confirm(entry)
        self.assertEqual(response.status_code, 200, response.data)
        self.prop.refresh_from_db()
        self.assertNotIn("visual_identity", {field.name for field in self.prop._meta.fields})
        self.assertEqual(primary.revisions.count(), 1)

    def test_broken_primary_is_an_explicit_conflict_not_an_empty_name_context(self):
        from studio.models import BibleVariant
        primary = self.primary()
        entry = self.candidate()
        BibleVariant.objects.filter(pk=primary.pk).update(revision=2)
        detail = self.client.get(reverse("translation-detail", args=[entry.pk]))
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertFalse(detail.data["context_current"])
        response = self.confirm(entry)
        self.assertEqual(response.status_code, 409, response.data)
        entry.refresh_from_db()
        self.assertEqual(entry.revision, 1)
        self.assertEqual(entry.status, "draft_translation")

    def test_cross_entity_target_collision_rolls_back_glossary_and_review(self):
        self.project.production_settings={**self.project.production_settings,"translation_glossary":{self.scene.name:"Xuanwen Sword"}}
        self.project.save(update_fields=("production_settings","updated_at"))
        entry=self.candidate()
        before=deepcopy(self.project.production_settings)
        response=self.confirm(entry)
        self.assertEqual(response.status_code,409,response.data)
        self.project.refresh_from_db();entry.refresh_from_db()
        self.assertEqual(self.project.production_settings,before)
        self.assertEqual(entry.status,"draft_translation")
        self.assertEqual(entry.revision,1)

    def test_new_confirmation_can_explicitly_replace_seen_term_not_rename_entity(self):
        self.project.production_settings={**self.project.production_settings,"translation_glossary":{"玄纹剑":"Former Name"}}
        self.project.save(update_fields=("production_settings","updated_at"))
        entry=self.candidate()
        response=self.confirm(entry,expected_canonical="Former Name")
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data["previous_canonical"],"Former Name")
        self.assertTrue(response.data["entry"]["context_current"])

    def test_ordinary_confirmation_cannot_silently_confirm_a_name(self):
        entry=self.candidate()
        response=self.client.post(reverse("translation-confirm",args=[entry.pk]),
            {"expected_revision":1,"translation":"Xuanwen Sword"},format="json")
        self.assertEqual(response.status_code,400,response.data)
        self.project.refresh_from_db();entry.refresh_from_db()
        self.assertEqual(self.project.production_settings,self.original_settings)
        self.assertEqual(entry.status,"draft_translation")

    def test_visual_description_cannot_be_written_into_the_glossary(self):
        primary = self.primary()
        setting = primary.revisions.get(revision=1)
        for field in ("identity_anchor", "appearance_anchor"):
            entry = self.candidate(field=f"primary_setting.{field}", source_text=getattr(setting, field))
            response = self.confirm(entry, translation="Silver sword")
            self.assertEqual(response.status_code, 400, response.data)
            entry.refresh_from_db()
            self.assertEqual(entry.status, "draft_translation")
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings, self.original_settings)
        self.assertEqual(primary.revisions.count(), 1)

    def test_explicit_request_shape_invalid_names_and_strict_types(self):
        entry=self.candidate()
        for changes in ({"translation":"Xuanwen 剑"},{"translation":"S1"},{"translation":"The sword used by someone."},
                {"expected_revision":True},{"expected_canonical":12},{"unexpected":True}):
            with self.subTest(changes=changes):
                response=self.confirm(entry,**changes)
                self.assertEqual(response.status_code,400,response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings,self.original_settings)

    def test_foreign_project_candidate_is_not_visible(self):
        entry=self.candidate()
        self.client.force_authenticate(build_story("other-canonical-owner")[0])
        self.assertEqual(self.confirm(entry).status_code,404)

    def test_original_synchronous_route_is_removed(self):
        with self.assertRaises(NoReverseMatch):
            reverse("project-ensure-canonical-glossary",args=[self.project.pk])
        self.assertEqual(self.client.post(f"/api/v1/projects/{self.project.pk}/ensure-canonical-glossary/",{},format="json").status_code,404)


class CanonicalEnglishValidationTests(TestCase):
    def test_accepts_short_ascii_proper_names(self):
        self.assertEqual(validate_canonical_english("  Listening  Rain Inn "), "Listening Rain Inn")
        self.assertEqual(validate_canonical_english("Shen Yan"), "Shen Yan")

    def test_rejects_mixed_language_prose_and_technical_ids(self):
        for value in ("Xuanwen 剑", "The sword used by Ling Shuang.", "prop:xuanwen", "S1"):
            with self.subTest(value=value), self.assertRaises(TranslationValidationError):
                validate_canonical_english(value)
