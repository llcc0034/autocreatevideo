"""Exact setting and translation boundaries, without legacy prompt adapters."""
import json
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from studio.models import BibleVariant, PropBible, PromptFieldTranslation, GenerationAttempt, ShotCharacterRelation
from studio.services.bible_setting_evidence import read_entity_primary_settings, read_setting_revisions, primary_setting_content
from studio.services.prompt_translation import (resolve_current_source, build_translation_context,
    translation_context_hash, TRANSLATION_CONTEXT_VERSION, TranslationValidationError,
    source_text_hash, translation_entry_context_current)
from studio.services.bible_variants import BibleVariantConflict, review_variant
from studio.tests.factories import build_story
from studio.tests.primary_setting_fixtures import create_primary_setting, revise_primary_setting


class PrimarySettingPromptSourceTests(TestCase):
    def setUp(self):
        self.user, self.project, self.character, self.scene, _, self.shot, self.shot2 = build_story()
        ShotCharacterRelation.objects.bulk_create([ShotCharacterRelation(shot=shot, character=self.character,
            role=role, position=0) for shot in (self.shot, self.shot2) for role in ("visible", "speaker")])
        self.prop = PropBible.objects.create(project=self.project, slug="metal-key", name="Metal key")
        self.shot.props.add(self.prop)
        self.shot2.props.add(self.prop)
        self.entities = {"character": self.character, "scene": self.scene, "prop": self.prop}
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def primary(self):
        return {kind: create_primary_setting(row, f"identity {kind}", f"appearance {kind}")
            for kind, row in self.entities.items()}

    def source(self, kind, attribute="identity_anchor", **changes):
        return resolve_current_source(**{"project": self.project, "scope_type": kind,
            "scope_id": self.entities[kind].pk, "field_key": f"{kind}.primary_setting.{attribute}", **changes})

    def settings(self):
        return read_entity_primary_settings(project_id=self.project.pk,
            entity_keys=[(kind, str(row.pk)) for kind, row in self.entities.items()])

    def test_exact_sources_are_narrow_and_never_fall_back_to_entity_columns(self):
        self.primary()
        for kind in self.entities:
            for attribute in ("identity_anchor", "appearance_anchor"):
                with self.subTest(kind=kind, attribute=attribute), CaptureQueriesContext(connection) as queries:
                    self.assertEqual(self.source(kind, attribute), f"{attribute.split('_')[0]} {kind}")
                self.assertEqual(len(queries), 3)
                self.assertTrue(all('"visual_identity"' not in row["sql"] for row in queries))
                self.assertTrue(all(not row["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for row in queries))
                with self.assertRaises(TranslationValidationError):
                    self.source(kind, field_key=f"{kind}.visual_identity")
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_missing_blank_and_corrupt_sources_are_distinct(self):
        with self.assertRaises(TranslationValidationError):
            self.source("character")
        absent = self.settings()
        self.assertTrue(all(value is None for value in absent.values()))
        primary = create_primary_setting(self.character, "", "")
        self.assertEqual(self.source("character"), "")
        self.assertEqual(self.source("character", "appearance_anchor"), "")
        BibleVariant.objects.filter(pk=primary.pk).update(revision=999)
        with self.assertRaises(TranslationValidationError):
            self.source("character")
        with self.assertRaises(BibleVariantConflict):
            self.settings()

    def test_wrong_owner_is_rejected_before_primary_body_read(self):
        self.primary()
        with patch("studio.services.bible_setting_evidence.read_entity_primary_setting") as reader:
            with self.assertRaises(TranslationValidationError):
                self.source("character", scope_id=self.prop.pk)
        reader.assert_not_called()
        other = build_story(username="another-source-owner")[1]
        with self.assertRaises(TranslationValidationError):
            self.source("character", project=other)

    def test_exact_setting_reader_preserves_both_anchors_and_does_not_follow_current_pointer(self):
        primaries = self.primary()
        with CaptureQueriesContext(connection) as queries:
            settings = self.settings()
        self.assertEqual(len(queries), 2)
        content = {}
        for (kind, entity_id), setting in settings.items():
            content[entity_id] = primary_setting_content(setting)
            self.assertEqual((setting.identity_anchor, setting.appearance_anchor),
                (f"identity {kind}", f"appearance {kind}"))
            self.assertEqual(content[entity_id]["setting_revision_id"], str(setting.pk))
            self.assertEqual(content[entity_id]["content_sha256"], setting.content_sha256)
        original_ids = [setting.pk for setting in settings.values()]
        revise_primary_setting(primaries["character"], identity_anchor="new current identity")
        exact = read_setting_revisions(original_ids, project_id=self.project.pk)
        self.assertEqual({str(row.pk): primary_setting_content(row) for row in exact.values()},
            {str(setting.pk): content[entity_id] for (_, entity_id), setting in settings.items()})
        self.assertEqual(self.settings()[("character", str(self.character.pk))].identity_anchor, "new current identity")
        other = build_story(username="wrong-setting-scope")[1]
        with self.assertRaises(BibleVariantConflict):
            read_setting_revisions(original_ids, project_id=other.pk)
        BibleVariant.objects.filter(project=self.project).update(revision=999)
        with self.assertRaises(BibleVariantConflict):
            self.settings()
        self.assertEqual(set(read_setting_revisions(original_ids, project_id=self.project.pk)), set(exact))

    def test_translation_revision_context_ignores_review_but_detects_same_text_new_revision(self):
        primary = create_primary_setting(self.character, "共同身份", "外观")
        key = "character.primary_setting.identity_anchor"
        context = build_translation_context(self.project, scope_type="character", scope_id=self.character.pk, field_key=key)
        entry = PromptFieldTranslation.objects.create(project=self.project, scope_type="character", scope_id=self.character.pk,
            field_key=key, source_text="共同身份", source_hash=source_text_hash("共同身份"), translated_text="Shared identity",
            status="confirmed", runtime_metadata={"translation_context_version": TRANSLATION_CONTEXT_VERSION,
                "translation_context_hash": translation_context_hash(context)})
        self.assertTrue(translation_entry_context_current(entry))
        setting = primary.revisions.get(revision=1)
        review_variant(actor=self.user, variant=primary, expected_revision=1, expected_sha256=setting.content_sha256,
            decision="approve", comment="isolated")
        self.assertTrue(translation_entry_context_current(entry))
        before = PromptFieldTranslation.objects.values().get(pk=entry.pk)
        self.assertEqual(self.source("character"), "共同身份")
        revise_primary_setting(primary, reference_name="new reference name")
        self.assertEqual(self.source("character"), "共同身份")
        self.assertFalse(translation_entry_context_current(entry))
        self.assertEqual(PromptFieldTranslation.objects.values().get(pk=entry.pk), before)
        self.assertEqual(json.loads(context)["primary_setting"]["setting_revision_id"], str(setting.pk))

    def test_original_manual_review_api_uses_setting_and_rejects_old_selector_without_write(self):
        primary = create_primary_setting(self.character, "共同身份", "主图外观")
        entries = [{"scope_type": "character", "scope_id": str(self.character.pk),
            "field_key": f"character.primary_setting.{key}", "source_text": value}
            for key, value in (("identity_anchor", "共同身份"), ("appearance_anchor", "主图外观"))]
        url = reverse("project-create-manual-prompt-fields", args=[self.project.pk])
        response = self.client.post(url, {"entries": entries}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        entry = PromptFieldTranslation.objects.get(field_key=entries[0]["field_key"])
        response = self.client.post(reverse("translation-confirm", args=[entry.pk]),
            {"expected_revision": entry.revision, "translation": "Shared identity"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.character.refresh_from_db()
        self.assertEqual(primary.revisions.get(revision=1).identity_anchor, "共同身份")
        self.assertEqual(primary.revisions.count(), 1)
        response = self.client.post(url, {"entries": [{**entries[0], "field_key": "character.visual_identity"}]}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(PromptFieldTranslation.objects.count(), 2)
        revise_primary_setting(primary, appearance_anchor="新外观")
        response = self.client.post(reverse("translation-confirm", args=[entry.pk]),
            {"expected_revision": response.data.get("revision", 2), "translation": "Shared identity"}, format="json")
        self.assertEqual(response.status_code, 409, response.data)

    def test_unrelated_broken_primary_does_not_pollute_selected_entities(self):
        self.primary()
        unrelated = PropBible.objects.create(project=self.project, slug="unrelated", name="Other")
        primary = create_primary_setting(unrelated, "irrelevant")
        BibleVariant.objects.filter(pk=primary.pk).update(revision=999)
        self.assertEqual(self.settings()[("prop", str(self.prop.pk))].identity_anchor, "identity prop")

    def test_mismatched_text_cannot_be_stamped_with_a_current_revision_context(self):
        self.primary()
        with self.assertRaises(TranslationValidationError):
            build_translation_context(self.project, scope_type="character", scope_id=self.character.pk,
                field_key="character.primary_setting.identity_anchor", source_text="OLD DUPLICATE")
