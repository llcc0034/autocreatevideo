"""0216 schema-only transition; real data is neither inferred nor backfilled."""
from django.db import IntegrityError, transaction

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ScriptBibleSettingUsesMigrationTests(_MigrationTestCase):
    migrate_from = "0215_bible_entity_identity"
    migrate_to = "0216_script_bible_setting_uses"

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="setting-use-schema")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=actor, title="Isolated schema fixture",
            width=768, height=1024, fps=24, default_shot_duration_ms=1000, continuity_seed=1)
        entity = apps.get_model("studio", "CharacterBible").objects.create(project=project, name="原人物",
            slug="person", stable_speaker_id="S1", visual_identity="原人工设定")
        source = apps.get_model("studio", "ScriptSource").objects.create(project=project, owner=actor,
            source_type="paste", source_format="plain")
        revisions = [apps.get_model("studio", "ScriptRevision").objects.create(source=source, revision=index,
            content="isolated source", content_size_bytes=15, content_sha256=str(index) * 64, created_by=actor)
            for index in (1, 2)]
        binding = apps.get_model("studio", "ScriptBibleEntityBinding").objects.create(script_revision=revisions[0],
            entity_key="person", character=entity, declaration_sha256="a" * 64)
        appearance = apps.get_model("studio", "BibleVariant").objects.create(project=project, character=entity,
            source_binding=binding, appearance_role="primary", source_variant_key="", name="主图", reference_name="主图",
            reference_key="primary", created_by=actor)
        setting = apps.get_model("studio", "BibleVariantRevision").objects.create(variant=appearance, revision=1,
            name="主图", reference_name="主图", appearance_anchor="原正文", content_sha256="b" * 64, created_by=actor)
        self.ids = dict(project=project.pk, entity=entity.pk, binding=binding.pk, setting=setting.pk, revision=revisions[1].pk)
        self.before = {name: list(apps.get_model("studio", name).objects.order_by("pk").values())
            for name in ("ScriptBibleEntityBinding", "BibleVariant", "BibleVariantRevision", "CharacterBible")}

    def test_preserves_source_rows_without_inventing_project_identity_or_setting_uses(self):
        for name, rows in self.before.items():
            self.assertEqual(list(self.apps.get_model("studio", name).objects.order_by("pk").values()), rows)
        self.assertFalse(self.apps.get_model("studio", "BibleEntityIdentity").objects.exists())
        self.assertFalse(self.apps.get_model("studio", "ScriptBibleAppearanceBinding").objects.exists())

    def test_allows_new_source_same_entity_but_not_duplicate_look_identity_or_source_use(self):
        Binding = self.apps.get_model("studio", "ScriptBibleEntityBinding")
        binding = Binding.objects.create(script_revision_id=self.ids["revision"], entity_key="person",
            character_id=self.ids["entity"], declaration_sha256="c" * 64)
        Use = self.apps.get_model("studio", "ScriptBibleAppearanceBinding")
        values = dict(source_binding=binding, variant_key="", declaration_sha256="d" * 64,
            setting_id=self.ids["setting"], setting_sha256="b" * 64)
        Use.objects.create(**values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Use.objects.create(**values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Binding.objects.create(script_revision_id=self.ids["revision"], entity_key="alias",
                character_id=self.ids["entity"], declaration_sha256="e" * 64)
        self.assertEqual(Binding.objects.filter(character_id=self.ids["entity"]).count(), 2)
        Appearance = self.apps.get_model("studio", "BibleVariant")
        initial = Appearance.objects.get(source_binding_id=self.ids["binding"])
        values = dict(project_id=self.ids["project"], character_id=self.ids["entity"], appearance_role="variant",
            source_variant_key="uniform", name="制服", reference_name="制服", created_by_id=initial.created_by_id)
        Appearance.objects.create(source_binding_id=self.ids["binding"], reference_key="look-one", **values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Appearance.objects.create(source_binding=binding, reference_key="look-two", **values)
