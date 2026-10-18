"""Single-step 0223/0225/0228 upgrades on historical, isolated schema fixtures.

These are stored-row contracts, not runtime acceptance or real execution facts.
Only the empty-business tests reverse schema; no nonempty rollback is claimed.
"""
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import PROTECT, RESTRICT, ProtectedError, RestrictedError
from django.utils import timezone

from studio.services.json_contracts import json_contract
from studio.tests.test_configuration_migrations import _MigrationTestCase


BIBLES = ("CharacterBible", "SceneBible", "PropBible")
M0222 = "0222_asset_text_response_owner"
M0223 = "0223_bible_image_binding_review"
M0224 = "0224_material_fulfillment_selection_mode"
M0225 = "0225_execution_preparation_supersedes"
M0227 = "0227_retire_script_breakdown_policies"
M0228 = "0228_manual_shot_reference_selection_action"


def snapshot(prefix, value):
    contract = json_contract(value)
    return {prefix: contract.value,
            f"{prefix}_schema_version": contract.schema_version,
            f"{prefix}_size_bytes": contract.size_bytes,
            f"{prefix}_sha256": contract.sha256}


def business_snapshot(columns=None):
    """Compare every studio table, including M2M and old snapshot bytes/metadata."""
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        if columns is None:
            columns = {}
            for table in connection.introspection.table_names(cursor):
                if table.startswith("studio_"):
                    # Read actual result columns, without parsing historical
                    # SQLite CREATE SQL for irrelevant collation metadata.
                    cursor.execute("SELECT * FROM " + quote(table) + " WHERE 1=0")
                    columns[table] = [column[0] for column in cursor.description]
        result = {}
        for table, fields in columns.items():
            cursor.execute("SELECT " + ", ".join(map(quote, fields)) + " FROM " + quote(table))
            result[table] = sorted(cursor.fetchall(), key=repr)
    return columns, result


def migrate_to(name):
    targets = [("studio", name)]
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


class _LinkFixture:
    def prevent_external_calls(self):
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "studio.services.provider_registry.resolve_credential"):
            blocked = self.enterContext(patch(target, side_effect=AssertionError("No external calls")))
            self.addCleanup(blocked.assert_not_called)

    def create_roots(self, apps):
        self.prevent_external_calls()
        model = lambda name: apps.get_model("studio", name)
        self.actor = apps.get_model(settings.AUTH_USER_MODEL).objects.create(username="link-cutover")
        self.project = model("SeriesProject").objects.create(
            owner=self.actor, title="Isolated historical rows only", width=768, height=1024,
            fps=24, default_shot_duration_ms=1000, continuity_seed=1,
        )
        recipe = model("ProductionRecipeVersion").objects.create(
            recipe_key="link-cutover", version=1, name="Stored fixture", status="retired",
            **snapshot("definition", {"fixture": [0, False, None, "原值"]}),
        )
        self.run = model("ProductionRun").objects.create(
            owner=self.actor, project=self.project, recipe_version=recipe,
            plan_source="recipe", status="cancelled", idempotency_key="link-cutover",
            request_sha256=json_contract({"isolated": True}).sha256,
            **snapshot("config_snapshot", {}),
            **snapshot("input_snapshot", {"fixture": [0, False, None]}),
        )
        self.stage = model("StageRun").objects.create(
            production_run=self.run, stage_key="bible_image_review", ordinal=1,
            mode="existing_pipeline", status="cancelled", idempotency_key="link-cutover",
            **snapshot("input_snapshot", {"fixture": [0, False, None]}),
        )

    def remember_business(self):
        connection.check_constraints()
        self.old_columns, self.before = business_snapshot()

    def assert_business_unchanged(self):
        self.assertEqual(business_snapshot(self.old_columns)[1], self.before)
        connection.check_constraints()

    def assert_reapply_is_noop(self):
        before = business_snapshot()
        executor = MigrationExecutor(connection)
        targets = [("studio", self.migrate_to)]
        self.assertEqual(executor.migration_plan(targets), [])
        executor.migrate(targets)
        self.assertEqual(business_snapshot(), before)
        connection.check_constraints()


class BibleReviewLinkMigrationTests(_LinkFixture, _MigrationTestCase):
    migrate_from = M0222
    migrate_to = M0223

    def set_up_before_migration(self, apps):
        self.create_roots(apps)
        Asset = apps.get_model("studio", "Asset")
        anchor = Asset.objects.create(owner=self.actor, name="Stored anchor only", kind="reference")
        reference = Asset.objects.create(owner=self.actor, name="Stored reference only", kind="reference")
        for name in BIBLES:
            values = {"stable_speaker_id": "S1"} if name == "CharacterBible" else {}
            row = apps.get_model("studio", name).objects.create(
                project=self.project, slug="isolated", name="Original entity",
                description="原始正文", anchor_asset=anchor,
                metadata={"fixture": [0, False, None]}, **values,
            )
            row.reference_assets.add(reference, anchor)
        self.remember_business()

    def test_upgrade_keeps_entities_and_assets_without_inventing_review(self):
        for name in BIBLES:
            Model = self.apps.get_model("studio", name)
            field = Model._meta.get_field("image_binding_review")
            self.assertTrue(field.null)
            self.assertTrue(field.blank)
            self.assertFalse(field.has_default())
            self.assertIs(field.remote_field.on_delete, RESTRICT)
            self.assertTrue(Model.objects.filter(image_binding_review__isnull=True).exists())
            self.assertFalse(Model.objects.filter(image_binding_review__isnull=False).exists())
        self.assert_business_unchanged()

    def test_dangling_review_fails_without_replacing_original_asset_bindings(self):
        for name in BIBLES:
            with self.subTest(model=name):
                Model = self.apps.get_model("studio", name)
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Model.objects.update(image_binding_review_id=uuid4())
                    connection.check_constraints()
                self.assertFalse(Model.objects.filter(image_binding_review__isnull=False).exists())
        self.assert_business_unchanged()

    def test_each_explicit_review_restricts_deletion_until_unlinked(self):
        Stage = self.apps.get_model("studio", "StageRun")
        for name in BIBLES:
            with self.subTest(model=name):
                Model = self.apps.get_model("studio", name)
                row = Model.objects.get(project_id=self.project.pk)
                Model.objects.filter(pk=row.pk).update(image_binding_review_id=self.stage.pk)
                with self.assertRaises(RestrictedError) as caught:
                    Stage.objects.get(pk=self.stage.pk).delete()
                self.assertEqual({item.pk for item in caught.exception.restricted_objects}, {row.pk})
                Model.objects.filter(pk=row.pk).update(image_binding_review_id=None)
        self.assert_business_unchanged()
        Stage.objects.get(pk=self.stage.pk).delete()
        self.assertFalse(Stage.objects.filter(pk=self.stage.pk).exists())
        for name in BIBLES:
            self.assertEqual(self.apps.get_model("studio", name).objects.count(), 1)
        self.assertEqual(self.apps.get_model("studio", "Asset").objects.count(), 2)
        connection.check_constraints()

    def test_reapply_keeps_later_explicit_review_without_changing_old_columns(self):
        for name in BIBLES:
            self.apps.get_model("studio", name).objects.update(image_binding_review_id=self.stage.pk)
        self.assert_reapply_is_noop()
        self.assert_business_unchanged()


class PreparationSupersedesMigrationTests(_LinkFixture, _MigrationTestCase):
    migrate_from = M0224
    migrate_to = M0225

    def set_up_before_migration(self, apps):
        self.create_roots(apps)
        model = lambda name: apps.get_model("studio", name)
        provider = model("ProviderProfile").objects.create(key="link-cutover", name="Stored", driver_type="deepseek_api")
        profile = model("ModelProfileVersion").objects.create(
            profile_key="link-cutover", version=1, model_identifier="fixture", modality="text", provider=provider,
        )
        deployment = model("ModelDeployment").objects.create(key="link-cutover", name="Stored", profile_version=profile)
        instance = model("ModelExecutionInstance").objects.create(key="link-cutover", name="Stored")
        version = model("ModelExecutionInstanceVersion").objects.create(
            instance=instance, version=1, deployment=deployment, operation="prompt.draft", prompt_mode="manual",
        )
        self.preparation_ids = []
        now = timezone.now()
        for index, status in enumerate(("awaiting_input", "preparing", "awaiting_review",
                                        "blocked", "ready", "dispatched", "cancelled")):
            frozen = status in ("ready", "dispatched", "cancelled")
            intent = {"isolated_status": status, "values": [0, False, None, "保留原值"]}
            row = model("ExecutionPreparation").objects.create(
                stage_run=self.stage, execution_instance_version=version, created_by=self.actor,
                purpose="production", unit_key="isolated", request_key=f"original-{index}",
                status=status, revision=index + 1, intent_sha256=json_contract(intent).sha256,
                request_sha256=json_contract({"fixture": index}).sha256 if frozen else None,
                request_frozen_at=now if frozen else None,
                claim_token=uuid4() if status == "preparing" else None,
                claim_until=now if status == "preparing" else None,
                next_check_at=now if status == "preparing" else None,
                cancelled_at=now if status == "cancelled" else None,
                cancelled_by=self.actor if status == "cancelled" else None,
                evaluation_revision=index + 1, evaluated_at=now,
                reason_code="isolated_fixture", reason="Stored history, not execution evidence",
            )
            model("ExecutionPreparationContract").objects.create(
                preparation=row, **snapshot("intent", intent),
                **snapshot("resolved_inputs", {"original": [0, False, None]}),
                **snapshot("review_contract", {"isolated": True}),
                **snapshot("evaluation", {"fixture": status}),
            )
            self.preparation_ids.append(row.pk)
        self.original_rows = list(model("ExecutionPreparation").objects.order_by("pk").values())
        self.remember_business()

    def replacement(self, **changes):
        Model = self.apps.get_model("studio", "ExecutionPreparation")
        original = Model.objects.get(pk=self.preparation_ids[0])
        values = dict(stage_run_id=original.stage_run_id,
                      execution_instance_version_id=original.execution_instance_version_id,
                      created_by_id=original.created_by_id, purpose="production",
                      unit_key=original.unit_key, request_key=str(uuid4()),
                      status="awaiting_input", intent_sha256=original.intent_sha256,
                      supersedes_id=original.pk)
        values.update(changes)
        return Model.objects.create(**values)

    def assert_original_preparations_unchanged(self):
        Model = self.apps.get_model("studio", "ExecutionPreparation")
        actual = list(Model.objects.filter(pk__in=self.preparation_ids).order_by("pk")
                      .values(*self.original_rows[0]))
        self.assertEqual(actual, self.original_rows)

    def test_upgrade_preserves_all_states_claims_requests_and_contracts(self):
        Model = self.apps.get_model("studio", "ExecutionPreparation")
        field = Model._meta.get_field("supersedes")
        self.assertTrue(field.one_to_one)
        self.assertTrue(field.null)
        self.assertTrue(field.blank)
        self.assertFalse(field.has_default())
        self.assertIs(field.remote_field.on_delete, PROTECT)
        self.assertEqual(field.remote_field.related_name, "replacement")
        self.assertEqual(Model.objects.count(), 7)
        self.assertFalse(Model.objects.filter(supersedes__isnull=False).exists())
        self.assert_business_unchanged()

    def test_one_replacement_per_parent_and_protect_preserve_original_request(self):
        Model = self.apps.get_model("studio", "ExecutionPreparation")
        replacement = self.replacement()
        original = Model.objects.get(pk=self.preparation_ids[0])
        self.assertEqual(original.replacement.pk, replacement.pk)
        self.assertEqual(replacement.supersedes_id, original.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.replacement()
        self.assertEqual(Model.objects.count(), 8)
        with self.assertRaises(ProtectedError) as caught:
            original.delete()
        self.assertEqual({item.pk for item in caught.exception.protected_objects}, {replacement.pk})
        self.assert_original_preparations_unchanged()
        replacement.delete()
        self.assert_business_unchanged()

    def test_dangling_parent_fails_without_creating_replacement(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.replacement(supersedes_id=uuid4())
            connection.check_constraints()
        self.assert_business_unchanged()

    def test_reapply_preserves_later_explicit_replacement(self):
        self.replacement()
        self.assert_reapply_is_noop()
        self.assert_original_preparations_unchanged()


class ManualSelectChoiceMigrationTests(_LinkFixture, _MigrationTestCase):
    migrate_from = M0227
    migrate_to = M0228

    def set_up_before_migration(self, apps):
        self.create_roots(apps)
        Command = apps.get_model("studio", "ProductionManualCommand")
        field = Command._meta.get_field("action")
        self.old_choices = list(field.choices)
        self.assertNotIn("select", dict(self.old_choices))
        with self.assertRaises(ValidationError):
            field.clean("select", None)
        for action, _label in self.old_choices:
            Command.objects.create(
                run=self.run, stage_run=self.stage, action=action, idempotency_key=f"isolated-{action}",
                request_sha256=json_contract({"isolated": action}).sha256, created_by=self.actor,
                result_kind="isolated_fixture", target_id=self.stage.pk,
                **snapshot("result_refs", {"fixture": action, "values": [0, False, None, "原值"]}),
            )
        self.remember_business()

    def test_upgrade_adds_only_choice_without_creating_command_or_selection(self):
        Command = self.apps.get_model("studio", "ProductionManualCommand")
        field = Command._meta.get_field("action")
        expected = self.old_choices[:3] + [("select", "选择镜头素材")] + self.old_choices[3:]
        self.assertEqual(list(field.choices), expected)
        self.assertEqual(field.max_length, 8)
        self.assertEqual(field.clean("select", None), "select")
        for invalid in ("unknown", "", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                field.clean(invalid, None)
        self.assertEqual(Command.objects.count(), len(self.old_choices))
        self.assertFalse(Command.objects.filter(action="select").exists())
        self.assert_business_unchanged()
        self.assert_reapply_is_noop()

    def test_explicit_select_keeps_receipt_and_original_idempotency_constraint(self):
        Command = self.apps.get_model("studio", "ProductionManualCommand")
        fields = dict(run_id=self.run.pk, stage_run_id=self.stage.pk, action="select",
                      idempotency_key="isolated-select", request_sha256="b" * 64,
                      created_by_id=self.actor.pk, result_kind="isolated_fixture", target_id=self.stage.pk,
                      **snapshot("result_refs", {"fixture": [0, False, None, "确切引用"]}))
        row = Command.objects.create(**fields)
        saved = Command.objects.filter(pk=row.pk).values().get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Command.objects.create(**fields)
        self.assertEqual(Command.objects.filter(pk=row.pk).values().get(), saved)
        self.assertEqual(Command.objects.count(), len(self.old_choices) + 1)
        self.assert_reapply_is_noop()
        row.delete()
        self.assert_business_unchanged()


class _EmptyLinkFixture(_LinkFixture):
    def set_up_before_migration(self, apps):
        self.prevent_external_calls()
        for name in ("SeriesProject", "ProductionRun", "StageRun", "Asset", "ExecutionPreparation",
                     "ProductionManualCommand", *BIBLES):
            self.assertFalse(apps.get_model("studio", name).objects.exists(), name)
        self.remember_business()

    def roundtrip(self, verify_old, verify_new):
        verify_new(self.apps)
        self.assert_business_unchanged()
        verify_old(migrate_to(self.migrate_from))
        self.assert_business_unchanged()
        self.apps = migrate_to(self.migrate_to)
        verify_new(self.apps)
        self.assert_business_unchanged()

    def assert_column(self, apps, name, field, exists):
        Model = apps.get_model("studio", name)
        self.assertEqual(field in {item.name for item in Model._meta.fields}, exists)
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM " + connection.ops.quote_name(Model._meta.db_table) + " WHERE 1=0")
            columns = {item[0] for item in cursor.description}
        self.assertEqual(field + "_id" in columns, exists)


class EmptyBibleReviewLinkMigrationTests(_EmptyLinkFixture, _MigrationTestCase):
    migrate_from = M0222
    migrate_to = M0223

    def test_empty_upgrade_and_schema_roundtrip(self):
        def verify(apps, exists):
            for name in BIBLES:
                self.assert_column(apps, name, "image_binding_review", exists)
        self.roundtrip(lambda apps: verify(apps, False), lambda apps: verify(apps, True))


class EmptyPreparationSupersedesMigrationTests(_EmptyLinkFixture, _MigrationTestCase):
    migrate_from = M0224
    migrate_to = M0225

    def test_empty_upgrade_and_schema_roundtrip(self):
        self.roundtrip(
            lambda apps: self.assert_column(apps, "ExecutionPreparation", "supersedes", False),
            lambda apps: self.assert_column(apps, "ExecutionPreparation", "supersedes", True),
        )


class EmptyManualSelectChoiceMigrationTests(_EmptyLinkFixture, _MigrationTestCase):
    migrate_from = M0227
    migrate_to = M0228

    def test_empty_upgrade_and_choice_roundtrip(self):
        def choices(apps):
            return dict(apps.get_model("studio", "ProductionManualCommand")._meta.get_field("action").choices)
        self.roundtrip(lambda apps: self.assertNotIn("select", choices(apps)),
                       lambda apps: self.assertIn("select", choices(apps)))
