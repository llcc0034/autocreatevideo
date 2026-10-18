"""Zero-call blocked preparation takeover through the original material writer."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.test import override_settings
from django.utils import timezone

from studio.models import AssetReferenceVersion, ExecutionPreparation, GenerationAttempt, ProductionMaterialFulfillment
from studio.services.production_recipe_material_fulfillment import recipe_material_fulfillment_context
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_material_fulfillment_choices as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeMaterialUnsubmittedRecoveryTests(StructureFixture):
    Fixture = fixtures.RecipeMaterialFulfillmentChoiceTests
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    facts = Fixture.facts
    attach = Fixture.attach
    create_job = Fixture.create_job
    primary_version = Fixture.primary_version
    write = Fixture.write
    input = staticmethod(Fixture.input)
    configure_variant_sources = Fixture.configure_variant_sources
    target = Fixture.target
    prepare = Fixture.prepare
    read = Fixture.read
    choose = Fixture.choose

    def configure_declaration(self, declaration):
        if self._testMethodName.startswith("test_multiple_"):
            declaration["routing"]["variant_count"] = 2
            declaration["routing"]["evaluator"] = "first_succeeded"

    def blocked(self, number=1):
        from studio.services.execution_request_preparation import resolve_execution_request_facts
        with patch("studio.services.media_generation_targets.resolve_execution_request_facts", resolve_execution_request_facts):
            preparation = self.prepare(number=number, target={"role": "primary", "setting_revision_id": str(self.primary_setting.pk),
                "primary_version_id": None, "primary_input": None})
        self.assertIsNone(preparation.request_frozen_at)
        preparation.status, preparation.reason_code = "blocked", "execution_request_invalid"
        preparation.save(update_fields=["status", "reason_code"])
        return preparation

    def child(self, parent):
        from studio.services.execution_preparations import read_execution_preparation_detail
        from studio.services.execution_production_auxiliaries import create_production_field_child
        parent.refresh_from_db()
        batch = read_execution_preparation_detail(parent).resolved_inputs["field_requests"][0]
        child, created = create_production_field_child(preparation_id=parent.pk,
            expected_preparation_revision=parent.revision, field_batch_key=batch["sha256"],
            attempt_number=1, actor=self.user)
        self.assertTrue(created)
        # Allocation legitimately reopens the parent. This fixture models the
        # subsequent local blocker with an unsubmitted field child, not the
        # still-editable state immediately after allocation.
        ExecutionPreparation.objects.filter(pk=parent.pk).update(status="blocked",
            reason_code="execution_request_invalid", next_check_at=None)
        parent.refresh_from_db()
        return child

    def primary_choice(self):
        return self.choose(setting=self.primary_setting, series=self.primary.series)

    def material_context(self):
        return recipe_material_fulfillment_context(stage_run_id=self.stage.pk,
            unit_key=f"appearance:{self.primary_setting.variant_id}", actor=self.user)

    @staticmethod
    def corrupt_row(row, **changes):
        # Deliberate isolated storage corruption, bypassing the immutable
        # manager only to exercise rejection by the original read boundary.
        QuerySet(model=ExecutionPreparation).filter(pk=row.pk).update(**changes)

    def test_blocked_zero_call_choice_stops_original_preparation_and_replay_never_dispatches(self):
        preparation = self.blocked()
        original = deepcopy(preparation.contract.resolved_inputs)
        self.assertTrue(self.material_context()["can_select"])
        preparation.refresh_from_db()
        self.assertEqual(preparation.status, "blocked")  # GET is not cancellation.
        choice, created = self.primary_choice()
        self.assertTrue(created)
        preparation.refresh_from_db()
        self.assertEqual((preparation.status, preparation.cancelled_by_id), ("cancelled", self.user.pk))
        self.assertIsNone(preparation.request_frozen_at)
        preparation.contract.refresh_from_db()
        self.assertEqual(preparation.contract.resolved_inputs, original)
        self.assertEqual(self.primary_choice(), (choice, False))
        self.assertEqual(self.read(setting=self.primary_setting)["fulfillment"].pk, choice.pk)
        self.assertEqual(ProductionMaterialFulfillment.objects.count(), 1)
        self.assertFalse(self.material_context()["can_select"])

    def test_takeover_scheduler_snapshot_and_stale_worker_never_restart_cancelled_group(self):
        from studio.services.execution_advancement import advance_execution_preparation
        from studio.services.execution_preparation_queue import due_execution_preparations
        from studio.services.production_recipe_media_stages import advance_recipe_media_stage
        from studio.services.production_runs import _formal_snapshot
        preparation = self.blocked()
        child = self.child(preparation)
        stale_revision = preparation.revision
        for setting, series in [(self.primary_setting, self.primary.series), *zip(self.settings, self.series)]:
            self.choose(setting=setting, series=series)
        before = self.facts()
        self.assertFalse(due_execution_preparations(timezone.now()).exists())
        # A worker that read before takeover must recheck the original row.
        with self.assertRaisesMessage(ValidationError, "准备已变化"):
            advance_execution_preparation(preparation_id=preparation.pk,
                expected_preparation_revision=stale_revision, actor=self.user)
        preparation.refresh_from_db()
        result = advance_execution_preparation(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=self.user)
        self.assertEqual(result.status, "cancelled")
        child.refresh_from_db()
        self.assertEqual(child.status, "cancelled")
        with patch("studio.services.production_recipe_media_stages.prepare_recipe_media_unit",
                side_effect=AssertionError("Fulfilled units must not be prepared again")):
            result = advance_recipe_media_stage(self.run, self.stage)
            self.assertEqual(result, "material-fulfillments:3")
            snapshot = _formal_snapshot(self.run, self.stage, result)
        self.assertEqual(len(snapshot["canonical"]["material_fulfillments"]), 3)
        self.assertEqual(self.facts(), before)

    def test_auxiliary_claim_freeze_and_bad_lineage_refuse_without_any_association(self):
        preparation = self.blocked()
        child = self.child(preparation)
        baseline = list(AssetReferenceVersion.objects.order_by("pk").values())
        for changes in ({"claim_token": uuid4(), "claim_until": timezone.now()},
                {"request_frozen_at": timezone.now(), "request_sha256": "a" * 64, "status": "ready"},
                {"status": "cancelled"}):
            self.corrupt_row(child, **changes)
            self.assertFalse(self.material_context()["can_select"])
            with self.assertRaises(ValidationError):
                self.primary_choice()
            self.corrupt_row(child, claim_token=None, claim_until=None,
                request_frozen_at=None, request_sha256=None, status="preparing")
        with patch("studio.services.execution_auxiliary_requests.read_production_field_context", return_value=None):
            with self.assertRaisesMessage(ValidationError, "辅助准备归属"):
                self.primary_choice()
        self.assertEqual(list(AssetReferenceVersion.objects.order_by("pk").values()), baseline)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        preparation.refresh_from_db()
        self.assertEqual(preparation.status, "blocked")

    def test_auxiliary_cancel_identity_is_checked_when_reading_original_receipt(self):
        preparation = self.blocked()
        child = self.child(preparation)
        choice, _ = self.primary_choice()
        self.assertEqual(self.primary_choice(), (choice, False))
        child.refresh_from_db()
        original = child.cancelled_at
        for changes in ({"cancelled_by_id": None}, {"cancelled_at": None},
                {"cancelled_at": timezone.now()}):
            self.corrupt_row(child, **changes)
            with self.assertRaises(ValidationError):
                self.read(setting=self.primary_setting)
            self.corrupt_row(child, cancelled_by_id=self.user.pk,
                cancelled_at=original)
        self.assertEqual(self.read(setting=self.primary_setting)["fulfillment"].pk, choice.pk)

    def test_stale_selection_context_rechecks_claim_and_conflicting_second_intent(self):
        preparation = self.blocked()
        self.assertTrue(self.material_context()["can_select"])
        ExecutionPreparation.objects.filter(pk=preparation.pk).update(claim_token=uuid4(), claim_until=timezone.now())
        before = self.facts()
        versions = list(AssetReferenceVersion.objects.order_by("pk").values())
        with self.assertRaises(ValidationError):
            self.primary_choice()
        self.assertEqual(self.facts(), before)
        self.assertEqual(list(AssetReferenceVersion.objects.order_by("pk").values()), versions)
        ExecutionPreparation.objects.filter(pk=preparation.pk).update(claim_token=None, claim_until=None)
        choice, _ = self.primary_choice()
        with self.assertRaisesMessage(ValidationError, "已有不同"):
            self.choose(setting=self.primary_setting, series=self.primary.series, asset_id=self.assets[1].pk)
        self.assertEqual(self.primary_choice(), (choice, False))

    def test_multiple_cancel_failure_rolls_back_parent_child_and_new_reference_version(self):
        from studio.services.execution_production_cancellation import cancel_production_preparation
        parents = [self.blocked(number) for number in (1, 2)]
        children = [self.child(row) for row in parents]
        before = self.facts()
        versions = list(AssetReferenceVersion.objects.order_by("pk").values())
        calls = []
        def stop(**kwargs):
            calls.append(kwargs["preparation_id"])
            row = cancel_production_preparation(**kwargs)
            if len(calls) == 2:
                raise RuntimeError("isolated second group stop failure")
            return row
        with patch("studio.services.execution_production_cancellation.cancel_production_preparation", side_effect=stop):
            with self.assertRaisesMessage(RuntimeError, "second group stop"):
                self.primary_choice()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.facts(), before)
        self.assertEqual(list(AssetReferenceVersion.objects.order_by("pk").values()), versions)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.primary_choice()
        self.assertEqual(set(ExecutionPreparation.objects.filter(pk__in=[row.pk for row in parents + children])
            .values_list("status", flat=True)), {"cancelled"})

    def test_choice_failure_rolls_back_stopping_and_material_receipt(self):
        preparation = self.blocked()
        before = self.facts()
        original_save = ProductionMaterialFulfillment.save
        def fail_after_save(row, *args, **kwargs):
            original_save(row, *args, **kwargs)
            raise RuntimeError("isolated recovery receipt failure")
        with patch.object(ProductionMaterialFulfillment, "save", fail_after_save):
            with self.assertRaisesMessage(RuntimeError, "recovery receipt failure"):
                self.primary_choice()
        self.assertEqual(self.facts(), before)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        preparation.refresh_from_db()
        self.assertIsNone(preparation.cancelled_at)
        self.primary_choice()

    def test_claim_and_unblocked_or_independent_cancelled_preparation_remain_nonreplaceable(self):
        preparation = self.blocked()
        for status in ("awaiting_input", "preparing", "awaiting_review", "cancelled"):
            ExecutionPreparation.objects.filter(pk=preparation.pk).update(status=status)
            self.assertFalse(self.material_context()["can_select"])
            with self.assertRaisesMessage(ValidationError, "已有生成准备"):
                self.primary_choice()
        ExecutionPreparation.objects.filter(pk=preparation.pk).update(status="blocked",
            claim_token=uuid4(), claim_until=timezone.now())
        self.assertFalse(self.material_context()["can_select"])
        with self.assertRaises(ValidationError):
            self.primary_choice()
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())

    def test_association_only_does_not_stop_or_fulfill_blocked_preparation(self):
        preparation = self.blocked()
        before = self.facts()
        self.attach(setting=self.primary_setting, series=self.primary.series)
        self.assertEqual(self.facts(), before)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        preparation.refresh_from_db()
        self.assertEqual(preparation.status, "blocked")

    def test_formal_api_uses_same_zero_call_stop_and_receipt(self):
        preparation = self.blocked()
        self.client.force_authenticate(self.user)
        path = f"/api/v1/production-runs/{self.run.pk}/"
        context = self.client.get(path + "material-fulfillment-context/", {
            "stage_run_id": str(self.stage.pk), "unit_key": preparation.unit_key})
        self.assertEqual(context.status_code, 200, context.data)
        self.assertTrue(context.data["can_select"])
        body = {"stage_run_id": str(self.stage.pk), "unit_key": preparation.unit_key,
            "requirement_key": f"setting:{self.primary_setting.pk}",
            "expected_run_plan_sha256": self.plan.sha256, "series_id": str(self.primary.series_id),
            "asset_id": str(self.assets[0].pk), "source": "existing"}
        response = self.client.post(path + "fulfill-material/", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["selection_mode"], "manual")
        self.assertEqual(response.data["selected_by_id"], self.user.pk)
        replay = self.client.post(path + "fulfill-material/", body, format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data, response.data)
        preparation.refresh_from_db()
        self.assertEqual(preparation.status, "cancelled")
