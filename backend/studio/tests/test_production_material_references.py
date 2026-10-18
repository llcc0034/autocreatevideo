"""Exact upstream result reads; all records/files are isolated fixtures."""
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import AssetReferenceVersion, ProductionMaterialFulfillment, StageRun
from studio.services.production_material_references import read_material_result_reference, read_upstream_material_result
from studio.services.production_recipe_material_fulfillment import material_fulfillment_receipt, _target_digest, _version_target
from studio.services.production_runs import _save_stage_snapshot, _sha256
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_material_review as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class ProductionMaterialReferenceTests(StructureFixture):
    Fixture = fixtures.RecipeMaterialReviewTests
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    facts = Fixture.facts
    attach = Fixture.attach
    choose = Fixture.choose
    create_job = Fixture.create_job
    primary_version = Fixture.primary_version
    write = Fixture.write
    input = staticmethod(Fixture.input)
    configure_variant_sources = Fixture.configure_variant_sources
    configure_declaration = Fixture.configure_declaration
    target = Fixture.target
    prepare = Fixture.prepare
    complete = Fixture.complete

    def source(self):
        self.complete()
        return {"stage_run_id": str(self.stage.pk), "output_snapshot_sha256": self.stage.output_snapshot_sha256,
            "field": "material_fulfillments", "reference": deepcopy(self.stage.output_snapshot["canonical"]["material_fulfillments"][0])}

    def test_upstream_exact_receipt_is_read_only_with_indexed_identity(self):
        source = self.source()
        before = (self.facts(), list(AssetReferenceVersion.objects.values()), list(ProductionMaterialFulfillment.objects.values()))
        with CaptureQueriesContext(connection) as queries:
            choice = read_upstream_material_result(stage=self.review, source=source)
        self.assertEqual(str(choice["version"].pk), source["reference"]["reference_version_id"])
        self.assertEqual(choice["fulfillment"].selection_mode, "manual")
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
        plan = ProductionMaterialFulfillment.objects.filter(pk=source["reference"]["fulfillment_id"]).explain().upper()
        self.assertIn("INDEX", plan)
        self.assertNotIn("SCAN", plan)
        self.assertEqual(before, (self.facts(), list(AssetReferenceVersion.objects.values()), list(ProductionMaterialFulfillment.objects.values())))

    def test_upstream_requires_exact_completed_eligible_stage_and_snapshot(self):
        source = self.source()
        for changes in ({"stage_run_id": str(self.review.pk)}, {"stage_run_id": str(uuid4())},
                {"output_snapshot_sha256": "0" * 64}, {"field": "declared_reference_asset"},
                {"extra": "latest"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                read_upstream_material_result(stage=self.review, source={**source, **changes})
        StageRun.objects.filter(pk=self.stage.pk).update(status="running")
        with self.assertRaisesMessage(ValidationError, "已完成的上游"):
            read_upstream_material_result(stage=self.review, source=source)

    def test_reference_cannot_substitute_version_project_stage_or_malformed_identity(self):
        source = self.source()
        for changes in ({"reference_version_id": str(uuid4())}, {"fulfillment_id": []},
                {"source": "generated"}, {"unit_key": "appearance:missing"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                read_material_result_reference(project_id=self.project.pk, field=source["field"],
                    reference={**source["reference"], **changes})
        for changes in ({"project_id": uuid4()}, {"stage_id": self.review.pk}, {"field": "guess"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                read_material_result_reference(**{"project_id": self.project.pk, "field": source["field"],
                    "reference": source["reference"], **changes})

    def test_recomputed_output_hash_cannot_authorize_duplicate_or_forged_result(self):
        source = self.source()
        original = deepcopy(self.stage.output_snapshot)
        for mode in ("duplicate", "version", "wrong_stage"):
            value = deepcopy(original)
            reference = value["canonical"]["material_fulfillments"][0]
            if mode == "duplicate":
                value["canonical"]["material_fulfillments"].append(deepcopy(reference))
            elif mode == "version":
                reference["reference_version_id"] = str(uuid4())
            else:
                # A syntactically valid reference copied to another stage does
                # not change the original choice's stage ownership.
                ProductionMaterialFulfillment._base_manager.filter(pk=reference["fulfillment_id"]).update(stage_run=self.review)
            value["canonical_sha256"] = _sha256(value["canonical"])
            _save_stage_snapshot(self.stage, "output_snapshot", value)
            with self.subTest(mode=mode), self.assertRaises(ValidationError):
                read_upstream_material_result(stage=self.review, source={**source,
                    "reference": deepcopy(reference), "output_snapshot_sha256": self.stage.output_snapshot_sha256})

    def test_changed_file_is_not_an_upstream_result_and_does_not_pick_another_version(self):
        source = self.source()
        choice = read_upstream_material_result(stage=self.review, source=source)
        Path(choice["asset"].file.path).write_bytes(b"isolated corrupt original file")
        before = list(ProductionMaterialFulfillment.objects.values())
        with self.assertRaises(ValidationError):
            read_upstream_material_result(stage=self.review, source=source)
        self.assertEqual(list(ProductionMaterialFulfillment.objects.values()), before)

    def test_automatic_identity_has_no_actor_and_manual_action_cannot_relabel_it(self):
        # Isolated model fixture only: no automatic policy/producer is claimed.
        version, _ = self.attach()
        row = ProductionMaterialFulfillment(stage_run=self.stage, unit_key=f"appearance:{self.settings[0].variant_id}",
            requirement_key=f"setting:{self.settings[0].pk}", run_plan_sha256=self.plan.sha256,
            reference_version=version, target_sha256=_target_digest(_version_target(version)),
            selection_mode="automatic", selected_by=None)
        row.full_clean()
        row.save()
        receipt = material_fulfillment_receipt(row)
        self.assertEqual((receipt["selection_mode"], receipt["selected_by_id"]), ("automatic", None))
        replay, created = self.choose()
        self.assertFalse(created)
        self.assertEqual((replay.selection_mode, replay.selected_by_id), ("automatic", None))
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/material-fulfillment-context/",
            {"stage_run_id": str(self.stage.pk), "unit_key": row.unit_key})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["selection"]["selection_mode"], "automatic")
        self.assertIsNone(response.data["selection"]["selected_by_id"])
        for field, value in (("selection_mode", "manual"), ("selected_by_id", self.user.pk)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ProductionMaterialFulfillment.objects.filter(pk=row.pk).update(**{field: value})

    def test_actor_mode_contract_is_enforced_by_model_and_database(self):
        row, _ = self.choose()
        for mode, actor in (("automatic", self.user.pk), ("manual", None), ("unknown", None)):
            values = {name: getattr(row, name) for name in row._SNAPSHOT_FIELDS}
            values.update(selection_mode=mode, selected_by_id=actor, unit_key=f"appearance:{uuid4()}")
            invalid = ProductionMaterialFulfillment(**values)
            with self.subTest(mode=mode), self.assertRaises(ValidationError):
                invalid.clean()
            with self.assertRaises(IntegrityError), transaction.atomic():
                ProductionMaterialFulfillment.objects.bulk_create([invalid])
        self.assertEqual(ProductionMaterialFulfillment.objects.count(), 1)
