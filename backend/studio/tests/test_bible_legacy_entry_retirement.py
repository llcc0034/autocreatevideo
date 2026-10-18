"""Isolated admin/initializer checks; no live database or demo writes."""
from io import StringIO

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path

from studio.models import (
    BibleVariant, BibleVariantRevision, BibleVariantReview, CharacterBible,
    Asset, PropBible, SceneBible, SeriesProject, ShotCharacterRelation, ShotReferenceUse,
)
from studio.tests.primary_setting_fixtures import create_primary_setting, revise_primary_setting


urlpatterns = [path("admin/", admin.site.urls)]  # Isolated form rendering, never installed in live URLconf.


@override_settings(ROOT_URLCONF=__name__)
class BibleLegacyAdminRetirementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = get_user_model().objects.create_user(username="legacy-admin", is_staff=True, is_superuser=True)
        cls.project = SeriesProject.objects.create(owner=cls.actor, title="Isolated admin",
            width=768, height=1024, fps=24, default_shot_duration_ms=1000, continuity_seed=1)

    def setUp(self):
        self.request = RequestFactory().get("/admin/")
        self.request.user = self.actor

    def entities(self):
        for kind, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible)):
            entity = model.objects.create(project=self.project, slug=kind, name=kind, is_locked=False,
                description="Visible description",
                **({"stable_speaker_id": "S1"} if kind == "character" else {}))
            yield model, entity

    def form_data(self, entity):
        return {"project": str(self.project.pk), "slug": entity.slug, "name": "Edited metadata",
            "description": "New entity description", "metadata": "{}",
            **({"stable_speaker_id": "S1"} if isinstance(entity, CharacterBible) else {})}

    def test_admin_form_excludes_old_anchor_and_keeps_exact_settings_on_save(self):
        for model, entity in self.entities():
            primary = create_primary_setting(entity, "Only authored identity", "Authored appearance")
            original = list(primary.revisions.values())
            manager = admin.site._registry[model]
            form_type = manager.get_form(self.request, obj=entity)
            form = form_type(data=self.form_data(entity), instance=entity)
            self.assertNotIn("visual_identity", form.fields)
            self.assertNotIn('name="visual_identity"', form.as_p())
            self.assertTrue(form.is_valid(), form.errors)
            manager.save_model(self.request, form.save(commit=False), form, change=True)
            entity.refresh_from_db()
            self.assertEqual(entity.name, "Edited metadata")
            self.assertNotIn("visual_identity", {field.name for field in model._meta.fields})
            self.assertEqual(list(primary.revisions.values()), original)
        self.assertFalse(BibleVariantReview.objects.exists())

    def test_admin_rejects_retired_field_even_blank_and_keeps_reserved_provenance(self):
        for model, entity in self.entities():
            form_type = admin.site._registry[model].get_form(self.request, obj=entity)
            for value in ("forged", ""):
                form = form_type(data={**self.form_data(entity), "visual_identity": value}, instance=entity)
                self.assertFalse(form.is_valid())
                self.assertIn("主图/变体设定", str(form.errors))
            form = form_type(data={**self.form_data(entity), "metadata": '{"script_bible_v1": {}}'}, instance=entity)
            self.assertFalse(form.is_valid())
            self.assertIn("不能注入", str(form.errors))
            entity.refresh_from_db()
            self.assertEqual(entity.name, entity.slug)
        self.assertFalse(BibleVariant.objects.exists())

    def test_admin_queries_and_search_no_longer_consume_retired_text(self):
        for model, entity in self.entities():
            manager = admin.site._registry[model]
            base = manager.get_queryset(self.request).filter(project=self.project)
            for term, expected in (("Visible", [entity.pk]), ("OLD ONLY SEARCH SENTINEL", [])):
                query, _ = manager.get_search_results(self.request, base, term)
                with CaptureQueriesContext(connection) as queries:
                    self.assertEqual([row.pk for row in query], expected)
                self.assertTrue(queries)
                self.assertTrue(all('"visual_identity"' not in row["sql"] for row in queries))


class BibleDemoSettingSeedTests(TestCase):
    def seed(self):
        call_command("seed_data", "--with-demo-project", stdout=StringIO(), verbosity=0)
        return SeriesProject.objects.get(owner__username="demo", title="无名剑影")

    def test_new_demo_uses_only_formal_primary_writer_without_invented_review_or_image(self):
        project = self.seed()
        for entity in (project.characters.get(), project.scenes.get()):
            self.assertNotIn("visual_identity", {field.name for field in entity._meta.fields})
            primary = BibleVariant.objects.get(project=project,
                **{"character" if isinstance(entity, CharacterBible) else "scene": entity})
            setting = primary.revisions.get(revision=1)
            self.assertEqual(primary.appearance_role, "primary")
            self.assertIsNone(primary.source_binding_id)
            self.assertTrue(setting.identity_anchor)
            self.assertEqual(setting.appearance_anchor, "")
            self.assertIsNone(setting.basis_asset_id)
            self.assertEqual(setting.source_declaration_sha256, "")
        self.assertEqual(BibleVariantRevision.objects.count(), 2)
        self.assertFalse(BibleVariantReview.objects.exists())

    def test_repeat_seed_preserves_authored_revisions_and_does_not_convert_existing_entities(self):
        project = self.seed()
        primary = BibleVariant.objects.get(character=project.characters.get())
        revise_primary_setting(primary, identity_anchor="Edited by author", appearance_anchor="New outfit")
        original = list(BibleVariantRevision.objects.order_by("id").values())
        self.seed()
        self.assertEqual(list(BibleVariantRevision.objects.order_by("id").values()), original)
        self.assertEqual(BibleVariant.objects.count(), 2)
        self.assertFalse(BibleVariantReview.objects.exists())

    def test_demo_roles_are_explicit_and_reseeding_does_not_repair_or_overwrite_authored_choices(self):
        project = self.seed()
        shot = project.episodes.get(number=1).shots.get(number=1)
        character = project.characters.get()
        self.assertEqual(list(shot.character_relations.values_list("role", "position", "character_id")),
            [("speaker", 0, character.pk), ("visible", 0, character.pk)])
        self.assertEqual(shot.dialogue, [{"speaker_id": character.stable_speaker_id, "text": "有人在等我？", "voiceover": False}])
        shot.character_relations.filter(role="visible").delete()
        shot.dialogue = [{"speaker_id": character.stable_speaker_id, "text": "  保留人工原文  ",
            "delivery": " softly ", "voiceover": True, "start_ms": 123, "end_ms": 987}]
        shot.episode_start_ms = 1200
        shot.episode_end_ms = 1200 + shot.duration_ms
        shot.save(update_fields=("dialogue", "episode_start_ms", "episode_end_ms"))
        asset = Asset.objects.create(owner=project.owner, kind="reference", name="Storage-only fixture",
            mime_type="image/png", checksum="a" * 64)
        ShotReferenceUse.objects.create(shot=shot, use_key="chosen", position=0, media_kind="image",
            purpose="  first_frame  ", source_mode="existing", asset=asset, asset_sha256=asset.checksum)
        before = (shot.dialogue, shot.episode_start_ms, shot.episode_end_ms)
        roles = list(ShotCharacterRelation.objects.order_by("pk").values())
        uses = list(ShotReferenceUse.objects.order_by("pk").values())
        self.seed()
        shot.refresh_from_db()
        self.assertEqual((shot.dialogue, shot.episode_start_ms, shot.episode_end_ms), before)
        self.assertEqual(list(ShotCharacterRelation.objects.order_by("pk").values()), roles)
        self.assertEqual(list(ShotReferenceUse.objects.order_by("pk").values()), uses)
        self.assertFalse(BibleVariantReview.objects.exists())

    def test_existing_entity_without_setting_is_not_backfilled_from_demo_defaults(self):
        # The initializer must not reinterpret an already present entity as a new sample.
        from studio.models import Style
        call_command("seed_data", stdout=StringIO(), verbosity=0)
        actor = get_user_model().objects.create_user(username="demo")
        project = SeriesProject.objects.create(owner=actor, title="无名剑影",
            style=Style.objects.get(owner=None, slug="wuxia"), width=768, height=1024,
            fps=24, default_shot_duration_ms=1000, continuity_seed=1)
        character = CharacterBible.objects.create(project=project, slug="shen-yan", name="Existing authored person",
            stable_speaker_id="S1")
        self.seed()
        self.assertFalse(BibleVariant.objects.filter(character=character).exists())
        character.refresh_from_db()
        self.assertEqual(character.name, "Existing authored person")
        self.assertNotIn("visual_identity", {field.name for field in character._meta.fields})
