"""Names are display/terminology, never the key for a production subject."""
import json
from copy import deepcopy

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import CharacterBible, GenerationAttempt, PromptFieldTranslation, PropBible, ShotCharacterRelation
from studio.services.bible_setting_evidence import read_entity_primary_settings
from studio.services.prompt_translation import (
    TRANSLATION_CONTEXT_VERSION, build_translation_context, canonical_identity_mappings,
    source_text_hash, translation_context_hash, translation_entry_context_current,
)
from studio.tests.factories import build_story
from studio.tests.primary_setting_fixtures import create_primary_setting


class MultilingualSubjectIdentityTests(TestCase):
    def setUp(self):
        self.user, self.project, self.first, self.scene, _, self.shot, self.shot2 = build_story()
        self.first.wardrobe = ""
        self.first.save(update_fields=("wardrobe",))
        self.second = CharacterBible.objects.create(project=self.project, slug="second",
            name=self.first.name, stable_speaker_id="S2")
        self.prop = PropBible.objects.create(project=self.project, slug="same-name-prop", name=self.first.name)
        for shot in (self.shot, self.shot2):
            ShotCharacterRelation.objects.bulk_create([
                ShotCharacterRelation(shot=shot, character=character, role="visible", position=position)
                for position, character in enumerate((self.first, self.second))
            ] + [ShotCharacterRelation(shot=shot, character=self.first, role="speaker", position=0)])
            shot.dialogue[0]["voiceover"] = False
            shot.save(update_fields=("dialogue",))
        self.shot.props.add(self.prop)
        self.shot2.props.add(self.prop)
        for entity, identity in ((self.first, "人物甲身份"), (self.second, "人物乙身份"), (self.prop, "道具身份")):
            create_primary_setting(entity, identity)

    def glossary(self, values):
        self.project.production_settings = {"translation_glossary": values}
        self.project.save(update_fields=("production_settings",))

    def test_homonyms_keep_distinct_identity_keys_and_exact_setting_bodies(self):
        with CaptureQueriesContext(connection) as queries:
            settings = read_entity_primary_settings(project_id=self.project.pk,
                entity_keys=[("character", str(self.first.pk)), ("character", str(self.second.pk)),
                    ("prop", str(self.prop.pk))])
        for kind, entity, body in (
            ("character", self.first, "人物甲身份"),
            ("character", self.second, "人物乙身份"),
            ("prop", self.prop, "道具身份"),
        ):
            self.assertEqual(settings[(kind, str(entity.pk))].identity_anchor, body)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        self.assertFalse(PromptFieldTranslation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_lexical_context_does_not_guess_an_entity_for_an_ambiguous_name(self):
        mappings = canonical_identity_mappings(self.project)
        self.assertNotIn(self.first.name, mappings)
        self.assertEqual(mappings[self.scene.name], f"scene:{self.scene.slug}")
        # Explicit terminology remains lexical, not an entity identity lookup.
        self.glossary({self.first.name: "Configured term", "词语": "Term"})
        mappings = canonical_identity_mappings(self.project)
        self.assertEqual(mappings[self.first.name], "Configured term")
        self.assertEqual(mappings["词语"], "Term")

    def test_new_homonym_invalidates_old_implicit_translation_context_without_rewriting_it(self):
        self.project.continuity_summary = f"{self.scene.name}门前等候"
        self.project.save(update_fields=("continuity_summary",))
        context = build_translation_context(self.project, scope_type="project", scope_id=self.project.pk,
            field_key="project.continuity_summary")
        self.assertEqual(json.loads(context)["canonical_identity_mappings"][self.scene.name], "scene:archive")
        entry = PromptFieldTranslation.objects.create(project=self.project, scope_type="project", scope_id=self.project.pk,
            field_key="project.continuity_summary", source_text=self.project.continuity_summary, source_hash=source_text_hash(self.project.continuity_summary),
            translated_text="Wait before scene:archive.", status="confirmed",
            runtime_metadata={"translation_context_version": TRANSLATION_CONTEXT_VERSION,
                "translation_context_hash": translation_context_hash(context)})
        self.assertTrue(translation_entry_context_current(entry))
        before = deepcopy(entry.runtime_metadata)
        PropBible.objects.create(project=self.project, slug="homonymous-place", name=self.scene.name)
        self.assertFalse(translation_entry_context_current(entry))
        entry.refresh_from_db()
        self.assertEqual(entry.runtime_metadata, before)
        self.assertEqual(entry.translated_text, "Wait before scene:archive.")
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_catalog_is_three_narrow_indexed_project_queries(self):
        other = build_story(username="other-catalog-project")
        self.glossary({self.first.name: "Display"})
        with CaptureQueriesContext(connection) as queries:
            mappings = canonical_identity_mappings(self.project)
        self.assertEqual(len(queries), 3)
        self.assertEqual(mappings, {self.first.name: "Display", self.scene.name: f"scene:{self.scene.slug}"})
        for row in queries:
            sql = row["sql"]
            self.assertIn(self.project.pk.hex, sql)
            self.assertNotIn(other[1].pk.hex, sql)
            for excluded in ('"metadata"', '"description"', '"visual_identity"', '"wardrobe"', 'ORDER BY'):
                self.assertNotIn(excluded, sql)
            if connection.vendor == "sqlite":
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + sql)
                    plan = " ".join(str(item[3]) for item in cursor.fetchall()).upper()
                self.assertIn("SEARCH", plan)
                self.assertIn("INDEX", plan)
                self.assertNotIn("SCAN", plan)

    def test_unrelated_project_and_unicode_homonyms_do_not_supply_implicit_identity(self):
        self.first.name = "Ｆｏｘ"
        self.first.save(update_fields=("name",))
        self.prop.name = " fox "
        self.prop.save(update_fields=("name",))
        mappings = canonical_identity_mappings(self.project)
        self.assertNotIn("Ｆｏｘ", mappings)
        self.assertNotIn(" fox ", mappings)
        self.assertEqual(mappings[self.second.name], "S2")
        other = build_story(username="other-homonym-project")
        other[2].name = self.second.name
        other[2].save(update_fields=("name",))
        self.assertEqual(canonical_identity_mappings(self.project), mappings)
