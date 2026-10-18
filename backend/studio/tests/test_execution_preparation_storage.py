"""Preparation storage only: isolated rows, no new worker or real Smoke."""
import hashlib
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from studio.models import (
    AssetGenerationJob, ExecutionFieldSelection, ExecutionFieldValue,
    ExecutionPreparation, ExecutionPreparationContract, ModelDeployment,
    ModelDeploymentSmokeAttempt, ModelExecutionInstance, ModelExecutionInstanceVersion,
    ModelProfileVersion, ProviderProfile,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.execution_request_storage import store_prepared_request


class ExecutionPreparationStorageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = get_user_model().objects.create_user(username="preparation-storage")
        cls.provider = ProviderProfile.objects.create(key="prep", name="Fixture", driver_type="deepseek_api")
        cls.model = ModelProfileVersion.objects.create(
            profile_key="prep", version=1, model_identifier="fixture", modality="text", provider=cls.provider,
        )
        cls.deployment = ModelDeployment.objects.create(key="prep", name="Fixture", profile_version=cls.model)
        cls.instance = ModelExecutionInstance.objects.create(key="prep", name="Fixture")
        # Storage constraints do not grant publication or target validation.
        cls.version = ModelExecutionInstanceVersion.objects.create(
            instance=cls.instance, version=1, deployment=cls.deployment,
            operation="prompt.draft", mode="", prompt_mode="manual",
        )

    def setUp(self):
        limit = patch("studio.services.execution_instances.boundary_int", return_value=65536)
        limit.start()
        self.addCleanup(limit.stop)

    def smoke(self):
        return ModelDeploymentSmokeAttempt.objects.create(
            deployment=self.deployment, operation="prompt.draft", status="failed",
            request_sha256="a" * 64, target_sha256="b" * 64,
        )

    def preparation(self, **changes):
        fields = dict(
            deployment_smoke=changes["deployment_smoke"] if "deployment_smoke" in changes else self.smoke(),
            purpose="resource_smoke", unit_key="sample", request_key=str(uuid.uuid4()),
            status="awaiting_input", created_by=self.actor, intent_sha256=json_contract({"fixture": True}).sha256,
        )
        fields.update(changes)
        return ExecutionPreparation.objects.create(**fields)

    def contract(self, preparation):
        contract = ExecutionPreparationContract(preparation=preparation)
        for name, value in (("intent", {"fixture": True}), ("resolved_inputs", {}), ("review_contract", {"mode": "manual"})):
            assign_json_contract(contract, name, value)
        contract.save()
        return contract

    def value(self, preparation, **changes):
        text = changes.get("text", "明确的字段正文")
        values = dict(
            preparation=preparation, unit_key="sample", field_key="description", revision=1,
            source_sha256="c" * 64, field_contract_sha256="d" * 64,
            text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            origin_kind="manual", created_by=self.actor, status="pending",
        )
        values.update(changes)
        return ExecutionFieldValue.objects.create(**values)

    def confirm(self, value):
        value.status = "confirmed"
        value.approval_kind = "human"
        value.reviewed_by = self.actor
        value.reviewed_at = timezone.now()
        value.save()
        return value

    def selection(self, preparation, value):
        return ExecutionFieldSelection.objects.create(
            preparation=preparation, unit_key="sample", field_key="description", field_value=value,
            revision=1, selected_by=self.actor, selected_at=timezone.now(),
        )

    def test_exactly_one_owner_and_correct_purpose_are_database_constraints(self):
        job = AssetGenerationJob.objects.create(
            owner=self.actor, model_deployment=self.deployment, execution_instance_version=self.version, job_type="prompt", media_type="script",
            prompt="Fixture", operation="prompt.draft", idempotency_key="prep-job", request_snapshot_sha256=json_contract({}).sha256,
        )
        for changes in (
            {"deployment_smoke": None}, {"asset_job": job},
            {"execution_instance_version": self.version}, {"purpose": "production"},
            {"purpose": "instance_smoke"},
        ):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                self.preparation(**changes)
        prep = self.preparation(purpose="production", deployment_smoke=None, asset_job=job, execution_instance_version=self.version)
        self.assertEqual(prep.asset_job_id, job.pk)

    def test_root_owner_cannot_have_two_preparations(self):
        prep = self.preparation()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.preparation(deployment_smoke=prep.deployment_smoke)

    def test_status_claim_revision_and_ready_request_constraints(self):
        for changes in (
            {"status": "succeeded"}, {"status": "ready"}, {"status": "dispatched"},
            {"revision": 0}, {"claim_token": uuid.uuid4()}, {"claim_until": timezone.now()},
            {"request_sha256": "f" * 64}, {"request_frozen_at": timezone.now()},
            {"request_sha256": "f" * 64, "request_frozen_at": timezone.now(), "status": "awaiting_review"},
        ):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                self.preparation(**changes)

    def test_preparation_identity_is_immutable_on_save_and_bulk_update(self):
        prep = self.preparation()
        prep.unit_key = "different-candidate"
        with self.assertRaises(ValidationError):
            prep.save()
        with self.assertRaises(ValidationError):
            ExecutionPreparation.objects.filter(pk=prep.pk).update(deployment_smoke_id=self.smoke().pk)
        prep.refresh_from_db()
        prep.request_sha256 = "f" * 64
        prep.request_frozen_at = timezone.now()
        prep.status = "ready"
        prep.save()
        prep.request_sha256 = "e" * 64
        with self.assertRaises(ValidationError):
            prep.save()

    def test_contracts_verify_hash_size_and_parent_identity(self):
        prep = self.preparation()
        contract = self.contract(prep)
        contract.intent["fixture"] = False
        with self.assertRaises(ValidationError):
            contract.save()
        with self.assertRaises(ValidationError):
            ExecutionPreparationContract.objects.filter(pk=contract.pk).update(resolved_inputs={"changed": True})
        other = self.preparation(intent_sha256="0" * 64)
        with self.assertRaises(ValidationError):
            self.contract(other)
        contract.refresh_from_db()
        store_prepared_request(contract, {"preparation_id": str(prep.pk), "selected_fields": []})
        contract.prepared_request_size_bytes += 1
        with self.assertRaises(ValidationError):
            contract.save()

    def test_runtime_facts_follow_configured_boundary_and_review_remains_bounded(self):
        from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
        cap = 512 * 1024
        with patch("studio.services.execution_request_storage.boundary_int", return_value=cap):
            self.assertEqual(execution_configuration_max_bytes(), 65536)
            self.assertEqual(execution_resolved_inputs_max_bytes(), cap)
            # UTF-8 canonical JSON bytes, not Python character count or DB text encoding.
            value = {"data": "x" * (cap - len(b'{"data":""}'))}
            prep = self.preparation()
            contract = ExecutionPreparationContract(preparation=prep)
            for name, payload in (("intent", {"fixture": True}), ("review_contract", {"mode": "manual"}), ("resolved_inputs", value)):
                assign_json_contract(contract, name, payload, max_bytes=cap)
            contract.full_clean()
            contract.save()
            contract.refresh_from_db()
            contract.clean()
            self.assertEqual(contract.resolved_inputs_size_bytes, cap)
            self.assertEqual(contract.resolved_inputs, value)
            for name in ("review_contract", "evaluation"):
                with self.subTest(field=name):
                    candidate = self.contract(self.preparation())
                    assign_json_contract(candidate, name, {"data": "x" * 65536}, max_bytes=cap)
                    with self.assertRaisesMessage(ValidationError, "65536"):
                        candidate.clean()
            value["data"] += "x"
            with self.assertRaisesMessage(ValidationError, str(cap)):
                assign_json_contract(contract, "resolved_inputs", value, max_bytes=execution_resolved_inputs_max_bytes())

    def test_database_still_rejects_invalid_resolved_metadata(self):
        contract = self.contract(self.preparation())
        # SQL-level guard: QuerySet.update is separately prohibited by immutability.
        from django.db import connection
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("UPDATE studio_executionpreparationcontract SET resolved_inputs_size_bytes = 1 WHERE id = %s",
                [contract.pk.hex if connection.vendor == "sqlite" else contract.pk])

    def test_final_request_written_once(self):
        contract = self.contract(self.preparation())
        store_prepared_request(contract, {"preparation_id": str(contract.preparation_id), "selected_fields": [], "parameters": {"seed": 17}})
        contract.save()
        with self.assertRaises(ValidationError):
            store_prepared_request(contract, {"preparation_id": str(contract.preparation_id), "parameters": {"seed": 18}})
        contract.prepared_request_key = contract.prepared_request_key.replace("execution-requests/", "other/")
        with self.assertRaises(ValidationError):
            contract.save()

    def test_field_body_hash_utf8_bound_and_revision(self):
        prep = self.preparation()
        with self.assertRaises(ValidationError):
            self.value(prep, text_sha256="0" * 64)
        with self.assertRaises(ValidationError):
            self.value(prep, text="汉" * 22000)
        value = self.value(prep)
        value.text = "不能原位改写"
        with self.assertRaises(ValidationError):
            value.save()
        with self.assertRaises(ValidationError):
            ExecutionFieldValue.objects.filter(pk=value.pk).update(text="绕过保存")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.value(prep)
        self.value(prep, revision=2)

    def test_origin_and_approval_facts_cannot_masquerade_as_each_other(self):
        prep = self.preparation()
        for changes in (
            {"origin_kind": "model"},
            {"status": "confirmed"},
            {"status": "confirmed", "approval_kind": "server_derived", "reviewed_at": timezone.now()},
            {"status": "confirmed", "approval_kind": "policy", "reviewed_at": timezone.now()},
            {"status": "pending", "reviewed_by": self.actor},
        ):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                self.value(prep, **changes)
        server = self.value(prep, origin_kind="server", status="confirmed", approval_kind="server_derived", reviewed_at=timezone.now())
        self.assertIsNone(server.origin_attempt_id)

    def test_confirm_and_selection_are_separate_no_latest_replacement(self):
        prep = self.preparation()
        first = self.value(prep)
        with self.assertRaises(ValidationError):
            self.selection(prep, first)
        self.confirm(first)
        self.assertFalse(prep.field_selections.exists())
        selected = self.selection(prep, first)
        second = self.confirm(self.value(prep, revision=2))
        selected.refresh_from_db()
        self.assertEqual(selected.field_value_id, first.pk)
        self.assertNotEqual(selected.field_value_id, second.pk)
        second.status = "pending"
        with self.assertRaises(ValidationError):
            second.save()
        with self.assertRaises(ValidationError):
            ExecutionFieldValue.objects.filter(pk=first.pk).update(status="pending")

    def test_wrong_unit_or_field_selection_and_frozen_edits_fail(self):
        prep = self.preparation()
        value = self.confirm(self.value(prep))
        with self.assertRaises(ValidationError):
            ExecutionFieldSelection.objects.create(
                preparation=prep, unit_key="different-shot", field_key=value.field_key,
                field_value=value, revision=1, selected_by=self.actor, selected_at=timezone.now(),
            )
        selection = self.selection(prep, value)
        prep.status = "ready"
        prep.request_sha256 = "f" * 64
        prep.request_frozen_at = timezone.now()
        prep.save()
        with self.assertRaises(ValidationError):
            self.value(prep, revision=2)
        with self.assertRaises(ValidationError):
            selection.save()
        with self.assertRaises(ValidationError):
            ExecutionFieldSelection.objects.filter(pk=selection.pk).update(field_value_id=value.pk)

    def test_cancelled_preparation_cannot_add_fields(self):
        prep = self.preparation(status="cancelled")
        with self.assertRaises(ValidationError):
            self.value(prep)
