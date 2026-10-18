"""M5 relation foundation only; no production writer or media-file acceptance."""
from django.core.exceptions import ValidationError
from django.db import connection, IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from studio.models import (Asset, AssetReferenceSeries, AssetReferenceVersion, BibleVariant, BibleVariantRevision,
    ShotCharacterRelation, ShotReferenceUse)
from studio.tests.factories import build_story


class ShotSemanticModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.project, cls.character, cls.scene, _, cls.shot, cls.other_shot = build_story("shot-relations")
        cls.other_user, cls.other_project, cls.other_character, *_ = build_story("foreign-relations")
        cls.setting = cls.make_setting(cls.project, cls.character, cls.user, "primary")
        cls.other_setting = cls.make_setting(cls.project, cls.character, cls.user, "variant")
        cls.foreign_setting = cls.make_setting(cls.other_project, cls.other_character, cls.other_user, "primary")
        cls.asset = Asset.objects.create(owner=cls.user, kind="reference", name="Isolated relation fixture",
            checksum="a" * 64, mime_type="image/png")
        cls.series = AssetReferenceSeries.objects.create(owner=cls.user, role="primary", name="Fixture",
            bible_variant=cls.setting.variant)
        cls.version = AssetReferenceVersion.objects.create(series=cls.series, version=1, asset=cls.asset,
            source="existing", setting_revision=cls.setting)

    @staticmethod
    def make_setting(project, character, actor, role):
        variant = BibleVariant.objects.create(project=project, character=character, appearance_role=role,
            name=role, reference_name=role, reference_key=role, created_by=actor)
        return BibleVariantRevision.objects.create(variant=variant, revision=1, name=role, reference_name=role,
            appearance_anchor="Isolated model fixture", content_sha256="b" * 64, created_by=actor)

    def use(self, **changes):
        return ShotReferenceUse(shot=self.shot, use_key="appearance", position=0, media_kind="image",
            purpose="appearance", **changes)

    def test_speaker_role_does_not_create_visibility_or_copy_old_mixed_participants(self):
        self.assertNotIn("characters", {field.name for field in self.shot._meta.get_fields()})
        self.assertFalse(self.shot.character_relations.exists())
        speaker = ShotCharacterRelation(shot=self.shot, character=self.character, role="speaker", position=0)
        speaker.full_clean(); speaker.save()
        self.assertFalse(self.shot.character_relations.filter(role="visible").exists())
        visible = ShotCharacterRelation(shot=self.shot, character=self.character, role="visible", position=0)
        visible.full_clean(); visible.save()
        # Repeated dialogue from the same speaker retains separate row positions.
        self.shot.dialogue.append({"text": "再次发言"})
        self.shot.save(update_fields=("dialogue",))
        repeat = ShotCharacterRelation(shot=self.shot, character=self.character, role="speaker", position=1)
        repeat.full_clean(); repeat.save()
        self.assertEqual(list(self.shot.character_relations.values_list("role", "position")),
            [("speaker", 0), ("speaker", 1), ("visible", 0)])

    def test_character_scope_and_database_role_position_uniqueness(self):
        row = ShotCharacterRelation(shot=self.shot, character=self.other_character, role="visible", position=0)
        with self.assertRaises(ValidationError): row.full_clean()
        with self.assertRaises(ValidationError):
            ShotCharacterRelation(shot=self.shot, character=self.character, role="speaker", position=99).full_clean()
        ShotCharacterRelation.objects.create(shot=self.shot, character=self.character, role="visible", position=0)
        for role, position in (("visible", 0), ("visible", 1), ("invented", 2)):
            with self.subTest(role=role, position=position), self.assertRaises(IntegrityError), transaction.atomic():
                ShotCharacterRelation.objects.create(shot=self.shot, character=self.character, role=role, position=position)

    def test_pending_use_is_explicit_and_exact_versions_are_not_reselected(self):
        row = self.use(setting_revision=self.setting)
        row.full_clean(); row.save()
        self.assertEqual(row.source_mode, "")
        self.assertIsNone(row.reference_version_id)
        self.assertIsNone(row.asset_id)
        row.source_mode, row.reference_version, row.asset_sha256 = "existing", self.version, self.asset.checksum
        row.full_clean(); row.save()
        newer = AssetReferenceVersion.objects.create(series=self.series, version=2, asset=self.asset, source="existing")
        row.refresh_from_db()
        self.assertEqual(row.reference_version_id, self.version.pk)
        self.assertNotEqual(row.reference_version_id, newer.pk)

    def test_same_setting_can_have_multiple_ordered_media_uses_and_per_shot_clothes(self):
        for index, kind in enumerate(("audio", "image", "video")):
            row = ShotReferenceUse(shot=self.shot, use_key=kind, position=index, media_kind=kind,
                purpose=f"configured-{kind}", setting_revision=self.setting)
            row.full_clean(); row.save()
        ShotReferenceUse.objects.create(shot=self.other_shot, use_key="image", position=0, media_kind="image",
            purpose="appearance", setting_revision=self.other_setting)
        self.assertEqual(list(self.shot.reference_uses.values_list("media_kind", flat=True)), ["audio", "image", "video"])
        self.assertEqual(self.other_shot.reference_uses.get().setting_revision_id, self.other_setting.pk)
        self.assertEqual(set(self.shot.reference_uses.values_list("setting_revision_id", flat=True)), {self.setting.pk})
        self.assertFalse(self.shot.character_relations.exists())
        self.scene.refresh_from_db()
        self.assertEqual(self.shot.scene_id, self.scene.pk)

    def test_independent_audio_asset_needs_no_bible_or_reference_series(self):
        audio = Asset.objects.create(owner=self.user, kind="reference_audio", name="Fixture audio",
            checksum="c" * 64, mime_type="audio/wav")
        row = self.use(asset=audio, source_mode="upload", asset_sha256=audio.checksum)
        row.media_kind, row.purpose = "audio", "configured-audio-purpose"
        row.full_clean(); row.save()
        self.assertIsNone(row.setting_revision_id)
        self.assertIsNone(row.reference_version_id)
        self.assertFalse(audio.reference_versions.exists())

    def test_foreign_setting_owner_wrong_revision_and_changed_asset_are_rejected(self):
        for changes in ({"setting_revision": self.foreign_setting},
                {"setting_revision": self.other_setting, "reference_version": self.version,
                    "source_mode": "existing", "asset_sha256": self.asset.checksum},
                {"asset": self.asset, "source_mode": "existing", "asset_sha256": "d" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError): self.use(**changes).full_clean()
        foreign = Asset.objects.create(owner=self.other_user, name="Foreign", kind="reference", checksum="e" * 64)
        with self.assertRaises(ValidationError):
            self.use(asset=foreign, source_mode="existing", asset_sha256=foreign.checksum).full_clean()
        foreign_series = AssetReferenceSeries.objects.create(owner=self.other_user, role="primary", name="Foreign")
        foreign_version = AssetReferenceVersion.objects.create(series=foreign_series, version=1, asset=foreign, source="existing")
        with self.assertRaises(ValidationError):
            self.use(reference_version=foreign_version, source_mode="existing", asset_sha256=foreign.checksum).full_clean()
        row = self.use(asset=self.asset, source_mode="existing", asset_sha256=self.asset.checksum)
        row.media_kind = "audio"
        with self.assertRaises(ValidationError): row.full_clean()

    def test_database_rejects_ambiguous_missing_or_unidentified_material(self):
        for changes in ({"asset": self.asset}, {"asset_sha256": "a" * 64},
                {"reference_version": self.version, "asset": self.asset, "source_mode": "existing", "asset_sha256": "a" * 64},
                {"source_mode": "latest"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                self.use(**changes).save()
        for field, value in (("media_kind", "text"), ("use_key", "")):
            row = self.use(); setattr(row, field, value)
            with self.subTest(field=field), self.assertRaises(IntegrityError), transaction.atomic(): row.save()
        optional_description = self.use()
        optional_description.purpose = ""
        optional_description.full_clean()
        optional_description.save()
        self.assertEqual(self.shot.reference_uses.get().purpose, "")
        for key, position in (("appearance", 1), ("other", 0)):
            row = self.use(); row.use_key, row.position = key, position
            with self.subTest(key=key), self.assertRaises(IntegrityError), transaction.atomic(): row.save()

    def test_exact_references_are_protected_and_shot_local_reads_are_indexed(self):
        row = self.use(setting_revision=self.setting, reference_version=self.version,
            source_mode="existing", asset_sha256=self.asset.checksum)
        row.full_clean(); row.save()
        for target in (self.version, self.asset):
            with self.subTest(target=type(target).__name__), self.assertRaises(ProtectedError): target.delete()
        if connection.vendor == "sqlite":
            self.assertRegex(self.shot.reference_uses.only("id", "position").explain(), "USING INDEX")
            self.assertRegex(self.shot.character_relations.filter(role="visible").explain(), "USING INDEX")
        connection.check_constraints()
