"""Isolated single-step upgrades for 0219 and 0220.

Stored JSON fixtures exercise preservation, not runtime recipe acceptance.
No current business model, media file, provider or execution service is used.
Schema roundtrips below contain no data in the newly introduced field/table.
"""
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError

from studio.services.json_contracts import json_contract
from studio.tests.test_configuration_migrations import _MigrationTestCase


M0218 = "0218_bible_setting_schema_default"
M0219 = "0219_stagerun_selected_variant_route_index"
M0220 = "0220_production_material_fulfillment"

OLD_TABLES = (
    "SeriesProject", "ProductionRecipeVersion", "ProductionRun",
    "ProductionRunPlan", "StageRun", "StageRouteEvent",
    "Asset", "AssetReferenceSeries", "AssetReferenceVersion",
    "ApprovalDecision", "ArtifactSelectionDecision", "GenerationAttempt",
    "GeneratedArtifact", "AssetGenerationJob", "ExecutionPreparation",
)
EMPTY_TABLES = (
    "ProductionRun", "ProductionRunPlan", "StageRun",
    "Asset", "AssetReferenceSeries", "AssetReferenceVersion",
)


def snapshot(prefix, value):
    contract = json_contract(value)
    return {
        prefix: contract.value,
        f"{prefix}_schema_version": contract.schema_version,
        f"{prefix}_size_bytes": contract.size_bytes,
        f"{prefix}_sha256": contract.sha256,
    }


def rows(apps, name):
    return list(apps.get_model("studio", name).objects.order_by("pk").values())


def migrate_to(name):
    # Tested transitions always use the ordinary, unmodified executor.
    targets = [("studio", name)]
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


class _StoredAppearanceFixture:
    variant_choices = ()

    def set_up_before_migration(self, apps):
        def model(name):
            return apps.get_model("studio", name)

        User = apps.get_model(settings.AUTH_USER_MODEL)
        owner = User.objects.create(username="appearance-cutover-owner")
        # Separate from every pre-existing owner/creator, so the selected_by
        # PROTECT test cannot pass because of an unrelated ownership FK.
        selector = User.objects.create(username="appearance-cutover-selector")
        self.user_ids = (owner.pk, selector.pk)
        self.selector_id = selector.pk
        project = model("SeriesProject").objects.create(
            owner=owner, title="Isolated historical schema fixture",
            width=768, height=1024, fps=24,
            default_shot_duration_ms=1000, continuity_seed=1,
        )
        recipe = model("ProductionRecipeVersion").objects.create(
            recipe_key="appearance-cutover-fixture", version=1,
            name="Stored historical fixture", status="retired",
            **snapshot("definition", {"fixture": [0, False, None, "保留原值"]}),
        )
        self.stage_ids = []
        self.plan_hashes = []
        for ordinal, primary_choice in enumerate((None, 0, 2)):
            run = model("ProductionRun").objects.create(
                owner=owner, project=project, recipe_version=recipe,
                plan_source="recipe", status="cancelled",
                idempotency_key=f"appearance-cutover-run-{ordinal}",
                request_sha256=json_contract({"fixture_run": ordinal}).sha256,
                **snapshot("config_snapshot", {}),
                **snapshot("input_snapshot", {"fixture": [0, False, None]}),
            )
            plan = model("ProductionRunPlan").objects.create(
                run=run, source="recipe", created_by=owner,
                stage_catalog_key="isolated-schema-fixture",
                stage_catalog_version=1,
                stage_catalog_sha256=json_contract({"fixture_catalog": 1}).sha256,
                **snapshot("definition", {
                    "fixture": "opaque stored plan",
                    "run_id": str(run.pk),
                    "recipe_version_id": str(recipe.pk),
                    "values": [0, False, None],
                }),
            )
            choices = {}
            if self.variant_choices:
                choices["selected_variant_route_index"] = self.variant_choices[ordinal]
            stage = model("StageRun").objects.create(
                production_run=run, stage_key="bible_image_generation",
                ordinal=1, mode="existing_pipeline", status="cancelled",
                idempotency_key=f"appearance-cutover-stage-{ordinal}",
                selected_route_index=primary_choice,
                **snapshot("input_snapshot", {"fixture": [0, False, None]}),
                **choices,
            )
            if primary_choice is not None:
                model("StageRouteEvent").objects.create(
                    stage_run=stage, sequence=1, unit_key="isolated-unit",
                    route_index=primary_choice, event_type="isolated_fixture",
                )
            self.stage_ids.append(stage.pk)
            self.plan_hashes.append(plan.definition_sha256)

        # Metadata-only historical rows: no file is written or probed.
        asset = model("Asset").objects.create(
            owner=owner, name="Isolated stored asset", kind="reference",
            metadata={"isolated_fixture": True},
        )
        primary = model("AssetReferenceSeries").objects.create(
            owner=owner, name="Isolated primary", role="primary",
        )
        variant = model("AssetReferenceSeries").objects.create(
            owner=owner, name="Isolated variant", role="variant",
            primary_series=primary,
        )
        model("AssetReferenceVersion").objects.create(
            series=primary, asset=asset, version=1, source="existing",
        )
        first = model("AssetReferenceVersion").objects.create(
            series=variant, asset=asset, version=1, source="existing",
        )
        latest = model("AssetReferenceVersion").objects.create(
            series=variant, asset=asset, version=2, source="existing",
        )
        self.asset_id = asset.pk
        self.version_id = first.pk
        self.latest_version_id = latest.pk
        self.target_digest = json_contract({
            "isolated_reference_version": str(first.pk),
        }).sha256

        self.before = {}
        for name in OLD_TABLES:
            historical = model(name)
            fields = [field.attname for field in historical._meta.concrete_fields]
            self.before[name] = (
                fields,
                list(historical.objects.order_by("pk").values(*fields)),
            )
        self.users_before = list(
            User.objects.filter(pk__in=self.user_ids).order_by("pk").values()
        )
        connection.check_constraints()

    def assert_old_facts(self, exclude=()):
        for name, (fields, expected) in self.before.items():
            if name in exclude:
                continue
            with self.subTest(model=name):
                actual = list(
                    self.apps.get_model("studio", name).objects
                    .order_by("pk").values(*fields)
                )
                self.assertEqual(actual, expected)
        User = self.apps.get_model(settings.AUTH_USER_MODEL)
        self.assertEqual(
            list(User.objects.filter(pk__in=self.user_ids).order_by("pk").values()),
            self.users_before,
        )
        connection.check_constraints()

    def assert_reapply_is_noop(self, names):
        before = {name: rows(self.apps, name) for name in names}
        targets = [("studio", self.migrate_to)]
        executor = MigrationExecutor(connection)
        self.assertEqual(executor.migration_plan(targets), [])
        executor.migrate(targets)
        for name, expected in before.items():
            self.assertEqual(rows(self.apps, name), expected, name)
        self.assert_old_facts()


class VariantRouteMigrationTests(_StoredAppearanceFixture, _MigrationTestCase):
    migrate_from = M0218
    migrate_to = M0219

    def test_upgrade_preserves_old_facts_without_copying_primary_choice(self):
        Stage = self.apps.get_model("studio", "StageRun")
        field = Stage._meta.get_field("selected_variant_route_index")
        self.assertEqual(field.get_internal_type(), "PositiveSmallIntegerField")
        self.assertTrue(field.null)
        self.assertTrue(field.blank)
        self.assertFalse(field.has_default())
        self.assertFalse(field.db_index)
        with connection.cursor() as cursor:
            columns = {
                column.name: column
                for column in connection.introspection.get_table_description(
                    cursor, Stage._meta.db_table,
                )
            }
        self.assertTrue(columns["selected_variant_route_index"].null_ok)
        for stage_id in self.stage_ids:
            self.assertIsNone(
                Stage.objects.get(pk=stage_id).selected_variant_route_index
            )
        self.assert_old_facts()

    def test_reapplying_recorded_migration_preserves_later_variant_choices(self):
        Stage = self.apps.get_model("studio", "StageRun")
        for stage_id, value in zip(self.stage_ids, (2, None, 0)):
            Stage.objects.filter(pk=stage_id).update(
                selected_variant_route_index=value,
            )
        self.assert_reapply_is_noop(("StageRun",))
        self.assertEqual(
            [Stage.objects.get(pk=pk).selected_variant_route_index
             for pk in self.stage_ids],
            [2, None, 0],
        )


class _EmptyAppearanceFixture:
    def set_up_before_migration(self, apps):
        # Migration-owned configuration seeds remain; business roots are empty.
        self.assert_empty_business_rows(apps)

    def assert_empty_business_rows(self, apps):
        for name in EMPTY_TABLES:
            self.assertFalse(apps.get_model("studio", name).objects.exists(), name)
        connection.check_constraints()


class EmptyVariantRouteMigrationTests(_EmptyAppearanceFixture, _MigrationTestCase):
    migrate_from = M0218
    migrate_to = M0219

    def test_empty_upgrade_and_empty_schema_roundtrip(self):
        self.assert_empty_business_rows(self.apps)
        old_apps = migrate_to(self.migrate_from)
        self.assertNotIn(
            "selected_variant_route_index",
            {field.name for field in old_apps.get_model("studio", "StageRun")._meta.fields},
        )
        self.apps = migrate_to(self.migrate_to)
        self.assertIn(
            "selected_variant_route_index",
            {field.name for field in self.apps.get_model("studio", "StageRun")._meta.fields},
        )
        self.assert_empty_business_rows(self.apps)


class MaterialFulfillmentMigrationTests(_StoredAppearanceFixture, _MigrationTestCase):
    migrate_from = M0219
    migrate_to = M0220
    variant_choices = (2, None, 0)

    def create_choice(self, **changes):
        values = {
            "stage_run_id": self.stage_ids[0],
            "unit_key": "isolated-unit",
            "requirement_key": "isolated-requirement",
            "run_plan_sha256": self.plan_hashes[0],
            "reference_version_id": self.version_id,
            "target_sha256": self.target_digest,
            "selected_by_id": self.selector_id,
        }
        values.update(changes)
        return self.apps.get_model("studio", "ProductionMaterialFulfillment").objects.create(
            **values,
        )

    def test_upgrade_adds_empty_table_without_inventing_choices_or_execution(self):
        Fulfillment = self.apps.get_model("studio", "ProductionMaterialFulfillment")
        self.assertFalse(Fulfillment.objects.exists())
        self.assertNotIn("selection_mode", {field.name for field in Fulfillment._meta.fields})
        for name in ("stage_run", "reference_version", "selected_by"):
            self.assertFalse(Fulfillment._meta.get_field(name).null, name)
        self.assert_old_facts()

    def test_explicit_choice_keeps_exact_old_version_and_original_links(self):
        row = self.create_choice()
        row.refresh_from_db()
        self.assertEqual(row.reference_version_id, self.version_id)
        self.assertNotEqual(row.reference_version_id, self.latest_version_id)
        self.assertEqual(row.reference_version.asset_id, self.asset_id)
        self.assertEqual(row.stage_run_id, self.stage_ids[0])
        self.assertEqual(row.selected_by_id, self.selector_id)
        self.assertEqual(row.run_plan_sha256, self.plan_hashes[0])
        self.assertEqual(row.target_sha256, self.target_digest)
        self.assertEqual(
            self.apps.get_model("studio", "ProductionMaterialFulfillment").objects.count(),
            1,
        )
        self.assert_old_facts()

    def test_missing_and_dangling_required_links_are_rejected(self):
        User = self.apps.get_model(settings.AUTH_USER_MODEL)
        missing_user = User.objects.create(username="appearance-cutover-deleted-user")
        missing_user_id = missing_user.pk
        missing_user.delete()
        Fulfillment = self.apps.get_model("studio", "ProductionMaterialFulfillment")
        invalid_links = (
            ("stage_run_id", (None, uuid4())),
            ("reference_version_id", (None, uuid4())),
            ("selected_by_id", (None, missing_user_id)),
        )
        for field, values in invalid_links:
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(IntegrityError), transaction.atomic():
                        self.create_choice(**{field: value})
                        # Foreign keys can be deferred on SQLite/PostgreSQL.
                        connection.check_constraints()
                    self.assertFalse(Fulfillment.objects.exists())
        self.assert_old_facts()

    def test_stage_unit_uniqueness_rejects_duplicate_and_conflicting_inserts(self):
        Fulfillment = self.apps.get_model("studio", "ProductionMaterialFulfillment")
        original = self.create_choice()
        saved = Fulfillment.objects.filter(pk=original.pk).values().get()
        for changes in (
            {},
            {
                "reference_version_id": self.latest_version_id,
                "requirement_key": "different-requirement",
                "target_sha256": "c" * 64,
            },
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self.create_choice(**changes)
                    connection.check_constraints()
                self.assertEqual(Fulfillment.objects.count(), 1)
                self.assertEqual(
                    Fulfillment.objects.filter(pk=original.pk).values().get(), saved,
                )
        # The actual key is stage + unit, not requirement or material version.
        self.create_choice(
            stage_run_id=self.stage_ids[1],
            run_plan_sha256=self.plan_hashes[1],
        )
        self.create_choice(unit_key="another-unit")
        self.assertEqual(Fulfillment.objects.count(), 3)
        self.assert_old_facts()

    def test_referenced_version_and_selector_cannot_be_deleted(self):
        row = self.create_choice()
        targets = (
            self.apps.get_model("studio", "AssetReferenceVersion").objects.get(
                pk=self.version_id,
            ),
            self.apps.get_model(settings.AUTH_USER_MODEL).objects.get(
                pk=self.selector_id,
            ),
        )
        for target in targets:
            with self.subTest(model=target._meta.label):
                with self.assertRaises(ProtectedError) as caught:
                    target.delete()
                self.assertEqual(
                    {item.pk for item in caught.exception.protected_objects},
                    {row.pk},
                )
        self.assert_old_facts()

    def test_stage_delete_cascades_its_choice_without_losing_shared_material(self):
        removed = self.create_choice()
        retained = self.create_choice(
            stage_run_id=self.stage_ids[1],
            run_plan_sha256=self.plan_hashes[1],
        )
        Fulfillment = self.apps.get_model("studio", "ProductionMaterialFulfillment")
        retained_before = Fulfillment.objects.filter(pk=retained.pk).values().get()
        Stage = self.apps.get_model("studio", "StageRun")
        Stage.objects.get(pk=self.stage_ids[0]).delete()
        self.assertFalse(Fulfillment.objects.filter(pk=removed.pk).exists())
        self.assertEqual(rows(self.apps, "ProductionMaterialFulfillment"), [retained_before])
        fields, old_stages = self.before["StageRun"]
        self.assertEqual(
            list(Stage.objects.order_by("pk").values(*fields)),
            [row for row in old_stages if row["id"] != self.stage_ids[0]],
        )
        # The removed stage had no route event; other historical rows survive.
        self.assert_old_facts(exclude=("StageRun",))

    def test_reapplying_recorded_migration_preserves_nonempty_table(self):
        self.create_choice()
        self.assert_reapply_is_noop(("ProductionMaterialFulfillment",))


class EmptyMaterialFulfillmentMigrationTests(_EmptyAppearanceFixture, _MigrationTestCase):
    migrate_from = M0219
    migrate_to = M0220

    def test_empty_upgrade_and_empty_table_roundtrip(self):
        Fulfillment = self.apps.get_model("studio", "ProductionMaterialFulfillment")
        table = Fulfillment._meta.db_table
        self.assertFalse(Fulfillment.objects.exists())
        self.assert_empty_business_rows(self.apps)
        migrate_to(self.migrate_from)
        self.assertNotIn(table, connection.introspection.table_names())
        self.apps = migrate_to(self.migrate_to)
        self.assertIn(table, connection.introspection.table_names())
        self.assertFalse(
            self.apps.get_model("studio", "ProductionMaterialFulfillment").objects.exists()
        )
        self.assert_empty_business_rows(self.apps)
