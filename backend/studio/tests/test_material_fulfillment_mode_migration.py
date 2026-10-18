"""0224 preserves original manual facts in an isolated migration database."""
from uuid import uuid4

from django.db import IntegrityError, transaction

from studio.tests.test_configuration_migrations import _MigrationTestCase


class MaterialFulfillmentModeMigrationTests(_MigrationTestCase):
    migrate_from = "0223_bible_image_binding_review"
    migrate_to = "0224_material_fulfillment_selection_mode"

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="isolated-fulfillment-migration")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=actor, title="Isolated schema",
            width=768, height=1024, fps=24, default_shot_duration_ms=1000, continuity_seed=1)
        recipe = apps.get_model("studio", "ProductionRecipeVersion").objects.create(
            recipe_key="isolated-fulfillment-schema", version=1, name="Schema fixture")
        run = apps.get_model("studio", "ProductionRun").objects.create(owner=actor, project=project,
            plan_source="recipe", recipe_version=recipe, status="running",
            idempotency_key=str(uuid4()), request_sha256="a" * 64)
        stage = apps.get_model("studio", "StageRun").objects.create(production_run=run,
            stage_key="bible_image_generation", ordinal=2, mode="existing_pipeline", idempotency_key=str(uuid4()))
        asset = apps.get_model("studio", "Asset").objects.create(owner=actor, name="Isolated schema only", kind="reference")
        series = apps.get_model("studio", "AssetReferenceSeries").objects.create(owner=actor, name="Isolated", role="primary")
        version = apps.get_model("studio", "AssetReferenceVersion").objects.create(series=series, asset=asset,
            version=1, source="existing")
        model = apps.get_model("studio", "ProductionMaterialFulfillment")
        self.row_id = model.objects.create(stage_run=stage, unit_key="appearance:isolated", requirement_key="setting:isolated",
            run_plan_sha256="a" * 64, reference_version=version, target_sha256="b" * 64, selected_by=actor).pk
        self.before = model.objects.get(pk=self.row_id).__dict__.copy()
        self.before.pop("_state")

    def test_existing_manual_facts_unchanged_and_new_identity_constraint_active(self):
        model = self.apps.get_model("studio", "ProductionMaterialFulfillment")
        row = model.objects.get(pk=self.row_id)
        actual = row.__dict__.copy()
        actual.pop("_state")
        self.assertEqual(actual.pop("selection_mode"), "manual")
        self.assertEqual(actual, self.before)
        for changes in ({"selection_mode": "automatic"}, {"selected_by_id": None}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                model.objects.filter(pk=self.row_id).update(**changes)
        self.assertEqual(model.objects.count(), 1)
