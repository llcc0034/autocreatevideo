"""M3 project-key schema foundation; no writer cutover or real provider."""
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import BibleEntityIdentity, CharacterBible, PropBible, SceneBible, SeriesProject
from studio.tests.primary_setting_fixtures import create_primary_setting, revise_primary_setting


class BibleEntityIdentityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = get_user_model().objects.create_user(username="bible-project-key")
        cls.project = cls.make_project("identity fixture")
        cls.other_project = cls.make_project("other fixture")
        cls.character = CharacterBible.objects.create(project=cls.project, slug="one", name="同名",
            stable_speaker_id="S1", is_locked=False)
        cls.scene = SceneBible.objects.create(project=cls.project, slug="one", name="同名", is_locked=False)
        cls.prop = PropBible.objects.create(project=cls.project, slug="one", name="同名", is_locked=False)

    @classmethod
    def make_project(cls, title):
        return SeriesProject.objects.create(owner=cls.actor, title=title, width=768, height=1024,
            fps=24, default_shot_duration_ms=1000, continuity_seed=1)

    def test_display_name_is_not_identity_and_typed_targets_share_one_key_namespace(self):
        for kind in ("character", "scene", "prop"):
            BibleEntityIdentity.objects.create(project=self.project, entity_key=kind,
                **{kind: getattr(self, kind)})
        self.assertEqual(BibleEntityIdentity.objects.filter(project=self.project).count(), 3)
        self.assertEqual(self.character.declaration_identity.entity_key, "character")
        with self.assertRaises(ValidationError):
            BibleEntityIdentity.objects.create(project=self.project, entity_key="character", scene=self.scene)

    def test_same_name_same_kind_different_keys_do_not_merge(self):
        other = CharacterBible.objects.create(project=self.project, slug="two", name=self.character.name,
            stable_speaker_id="S2")
        first = BibleEntityIdentity.objects.create(project=self.project, entity_key="角色甲", character=self.character)
        second = BibleEntityIdentity.objects.create(project=self.project, entity_key="角色乙", character=other)
        self.assertNotEqual(first.character_id, second.character_id)

    def test_same_key_in_another_project_is_distinct(self):
        other = CharacterBible.objects.create(project=self.other_project, slug="one", name="同名", stable_speaker_id="S1")
        first = BibleEntityIdentity.objects.create(project=self.project, entity_key="person", character=self.character)
        second = BibleEntityIdentity.objects.create(project=self.other_project, entity_key="person", character=other)
        self.assertNotEqual(first.character_id, second.character_id)

    def test_cross_project_target_is_not_silently_reassigned(self):
        with self.assertRaisesMessage(ValidationError, "本项目"):
            BibleEntityIdentity.objects.create(project=self.other_project, entity_key="person", character=self.character)
        self.assertFalse(BibleEntityIdentity.objects.exists())

    def test_missing_target_and_too_long_key_report_validation_errors(self):
        with self.assertRaisesMessage(ValidationError, "不存在"):
            BibleEntityIdentity.objects.create(project=self.project, entity_key="person", character_id=uuid4())
        with self.assertRaises(ValidationError):
            BibleEntityIdentity.objects.create(project=self.project, entity_key="a" * 65, character=self.character)
        self.assertFalse(BibleEntityIdentity.objects.exists())

    def test_renaming_and_manual_content_edits_keep_exact_identity(self):
        identity = BibleEntityIdentity.objects.create(project=self.project, entity_key="角色甲", character=self.character)
        primary = create_primary_setting(self.character, "原共同身份")
        revise_primary_setting(primary, identity_anchor="人工修改身份")
        self.character.name = "Renamed / 改名"
        self.character.save(update_fields=("name",))
        with CaptureQueriesContext(connection) as queries:
            current = BibleEntityIdentity.objects.select_related("character").get(project=self.project, entity_key="角色甲")
        self.assertEqual((current.pk, current.character_id), (identity.pk, self.character.pk))
        self.assertEqual(current.character.name, "Renamed / 改名")
        self.assertEqual(primary.revisions.get(revision=2).identity_anchor, "人工修改身份")
        self.assertEqual(len(queries), 1)
        self.assertTrue(queries[0]["sql"].lstrip().upper().startswith("SELECT "))

    def test_identity_reassignment_is_explicit_error(self):
        identity = BibleEntityIdentity.objects.create(project=self.project, entity_key="person", character=self.character)
        identity.entity_key = "new-key"
        with self.assertRaisesMessage(ValidationError, "不能改写"):
            identity.save()
        identity.refresh_from_db()
        identity.character, identity.scene = None, self.scene
        with self.assertRaisesMessage(ValidationError, "不能改写"):
            identity.save()
        identity.refresh_from_db()
        self.assertEqual((identity.entity_key, identity.character_id), ("person", self.character.pk))

    def test_database_rejects_empty_ambiguous_duplicate_and_multiple_alias_keys(self):
        # bulk_create intentionally bypasses model validation to exercise SQL constraints.
        for fields in ({}, {"character": self.character, "scene": self.scene},
                {"entity_key": "", "character": self.character}):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                BibleEntityIdentity.objects.bulk_create([BibleEntityIdentity(project=self.project,
                    **{"entity_key": "person", **fields})])
        BibleEntityIdentity.objects.create(project=self.project, entity_key="person", character=self.character)
        for fields in ({"entity_key": "person", "scene": self.scene},
                {"entity_key": "alias", "character": self.character}):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                BibleEntityIdentity.objects.bulk_create([BibleEntityIdentity(project=self.project, **fields)])
        self.assertEqual(BibleEntityIdentity.objects.count(), 1)

    def test_key_lookup_uses_project_key_index(self):
        BibleEntityIdentity.objects.create(project=self.project, entity_key="person", character=self.character)
        explanation = BibleEntityIdentity.objects.filter(project=self.project, entity_key="person").explain().upper()
        self.assertIn("INDEX", explanation)
        self.assertNotIn("SCAN STUDIO_BIBLEENTITYIDENTITY", explanation)

    def test_existing_api_cannot_move_identified_entities_but_can_edit_names(self):
        client = APIClient()
        client.force_authenticate(self.actor)
        for kind in ("character", "scene", "prop"):
            entity = getattr(self, kind)
            identity = BibleEntityIdentity.objects.create(project=self.project, entity_key=kind, **{kind: entity})
            response = client.patch(f"/api/v1/{kind}s/{entity.pk}/", {"project": str(self.other_project.pk)}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
            entity.refresh_from_db()
            self.assertEqual(entity.project_id, self.project.pk)
            response = client.patch(f"/api/v1/{kind}s/{entity.pk}/", {"name": "改名 / renamed"}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            identity.refresh_from_db()
            self.assertEqual(identity.entity_key, kind)

    def test_no_duplicate_creative_fields_or_source_revision_columns(self):
        self.assertEqual({field.name for field in BibleEntityIdentity._meta.fields},
            {"id", "created_at", "updated_at", "project", "entity_key", "character", "scene", "prop"})
