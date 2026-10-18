"""0217 removes only duplicate columns; all values here are isolated fixtures."""
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class BibleVisualIdentityMigrationTests(_MigrationTestCase):
    migrate_from = "0216_script_bible_setting_uses"
    migrate_to = "0217_remove_bible_visual_identity"
    entity_models = ("CharacterBible", "SceneBible", "PropBible")

    def set_up_before_migration(self, apps):
        model = lambda name: apps.get_model("studio", name)
        actor = apps.get_model("auth", "User").objects.create(username="retired-column-fixture")
        project = model("SeriesProject").objects.create(owner=actor, title="Isolated column removal",
            width=768, height=1024, fps=24, default_shot_duration_ms=1000, continuity_seed=1)
        asset = model("Asset").objects.create(owner=actor, kind="image", name="Shared fixture", checksum="a" * 64)
        source = model("ScriptSource").objects.create(project=project, owner=actor,
            source_type="paste", source_format="plain")
        revision = model("ScriptRevision").objects.create(source=source, revision=1,
            content="Explicit isolated text", content_size_bytes=22, content_sha256="b" * 64, created_by=actor)
        self.missing_setting_ids = []
        for kind, name in zip(("character", "scene", "prop"), self.entity_models):
            Entity = model(name)
            entity = Entity.objects.create(project=project, slug="authored", name=f"Authored {kind}",
                description="Entity description stays", visual_identity=f"Discarded duplicate {kind}",
                metadata={"isolated_note": "preserve exactly"}, anchor_asset=asset,
                **({"stable_speaker_id": "S1"} if kind == "character" else {}))
            entity.reference_assets.add(asset)
            missing = Entity.objects.create(project=project, slug="no-setting", name=f"No setting {kind}",
                visual_identity=f"Never convert {kind}", **({"stable_speaker_id": "S2"} if kind == "character" else {}))
            self.missing_setting_ids.append((kind, missing.pk))
            model("BibleEntityIdentity").objects.create(project=project, entity_key=kind, **{kind: entity})
            binding = model("ScriptBibleEntityBinding").objects.create(script_revision=revision,
                entity_key=kind, declaration_sha256="c" * 64, **{kind: entity})
            primary = None
            for role, key in (("primary", ""), ("variant", "night")):
                appearance = model("BibleVariant").objects.create(project=project, **{kind: entity},
                    source_binding=binding, appearance_role=role, source_variant_key=key,
                    name=role, reference_name=f"{kind} {role}", reference_key=f"{kind}-{role}", created_by=actor)
                setting = model("BibleVariantRevision").objects.create(variant=appearance, revision=1,
                    name=role, reference_name=f"{kind} {role}",
                    identity_anchor=f"Authored identity {kind}" if role == "primary" else "",
                    appearance_anchor=f"Authored {role}", basis_asset=asset, basis_asset_sha256=asset.checksum,
                    primary_setting_revision=primary, payload_schema_version=2,
                    source_declaration_sha256="d" * 64, content_sha256="e" * 64, created_by=actor)
                if role == "primary":
                    primary = setting
                model("BibleVariantReview").objects.create(setting=setting, decision="approve",
                    comment="Explicit isolated review fixture", decided_by=actor)
                model("ScriptBibleAppearanceBinding").objects.create(source_binding=binding, variant_key=key,
                    declaration_sha256="d" * 64, setting=setting, setting_sha256=setting.content_sha256)
        model("ConsistencyLock").objects.create(project=project, scope="global", lock_key="fixture",
            locked_value={"visual_identity": "Saved historical snapshot is not a current setting"})
        names = (*self.entity_models, "Asset", "SeriesProject", "ScriptSource", "ScriptRevision",
            "BibleEntityIdentity", "ScriptBibleEntityBinding", "BibleVariant", "BibleVariantRevision",
            "BibleVariantReview", "ScriptBibleAppearanceBinding", "ConsistencyLock")
        self.before = {name: self.rows_without_removed_column(model(name)) for name in names}
        self.references = {name: list(model(name).reference_assets.through.objects.order_by("pk").values())
            for name in self.entity_models}

    def rows_without_removed_column(self, model):
        return [{key: value for key, value in row.items() if key != "visual_identity"}
            for row in model.objects.order_by("pk").values()]

    def assert_surviving_facts_unchanged(self):
        for name, rows in self.before.items():
            self.assertEqual(self.rows_without_removed_column(self.apps.get_model("studio", name)), rows, name)
        for name, rows in self.references.items():
            through = self.apps.get_model("studio", name).reference_assets.through
            self.assertEqual(list(through.objects.order_by("pk").values()), rows)
        Appearance = self.apps.get_model("studio", "BibleVariant")
        for kind, pk in self.missing_setting_ids:
            self.assertFalse(Appearance.objects.filter(**{f"{kind}_id": pk}).exists())
        connection.check_constraints()

    def test_removes_all_three_columns_without_rewriting_or_inventing_facts(self):
        for name in self.entity_models:
            model = self.apps.get_model("studio", name)
            self.assertNotIn("visual_identity", {field.name for field in model._meta.fields})
            with connection.cursor() as cursor:
                columns = connection.introspection.get_table_description(cursor, model._meta.db_table)
            self.assertNotIn("visual_identity", {column.name for column in columns})
        self.assert_surviving_facts_unchanged()

    def test_reversing_schema_recreates_blank_columns_not_lost_text(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        old_apps = executor.loader.project_state([("studio", self.migrate_from)]).apps
        for name in self.entity_models:
            self.assertEqual(set(old_apps.get_model("studio", name).objects.values_list("visual_identity", flat=True)), {""})
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assert_surviving_facts_unchanged()

    def test_entity_unique_indexes_survive_column_removal(self):
        for name in self.entity_models:
            model = self.apps.get_model("studio", name)
            entity = model.objects.get(slug="authored")
            with self.assertRaises(IntegrityError), transaction.atomic():
                model.objects.create(project_id=entity.project_id, slug=entity.slug, name="Duplicate",
                    **({"stable_speaker_id": "S3"} if name == "CharacterBible" else {}))
        self.assert_surviving_facts_unchanged()
