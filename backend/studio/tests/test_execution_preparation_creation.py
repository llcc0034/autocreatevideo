"""Isolated persistence and real definition/parameter resolution; no model calls."""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import TestCase, override_settings

from studio.models import (
    AssetGenerationJob, ExecutionPreparation, ExecutionPreparationContract, ModelDeploymentSmokeAttempt,
    ProductionRun, SeriesProject, StageRun,
)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import publish_execution_version, update_execution_draft
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.execution_preparations import ResolvedExecutionPreparation, create_execution_instance_preparation
from studio.services.json_contracts import JsonContract, assign_json_contract, json_contract
from studio.tests import test_execution_definitions as definition_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionPreparationCreationTests(TestCase):
    setUpTestData = classmethod(definition_fixtures.ExecutionDefinitionTests.setUpTestData.__func__)
    write_contract = staticmethod(definition_fixtures.ExecutionDefinitionTests.write_contract)

    def setUp(self):
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential", "studio.services.comfyui.ComfyUIClient"):
            self.enterContext(patch(target, side_effect=AssertionError("Preparation cannot call a model")))
        specification = deepcopy(self.model.specification)
        specification["async_media"]["operations"]["video.text_to_video"]["request_schema"]["properties"]["seed"] = {
            "type": "integer", "minimum": 0, "maximum": 30,
        }
        self.write_contract(self.model, "specification", specification)
        configuration = deepcopy(self.provider.driver_config)
        configuration["request_field_map"]["seed"] = "seed"
        self.write_contract(self.provider, "driver_config", configuration)
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision,
            actor=self.actor, changes={"parameters": [{
                "key": "seed", "value_type": "integer", "minimum": 0, "maximum": 30, "enum_values": [],
                "step": 5, "remainder": 0, "policy": "overridable", "default_value": None,
                "locked_value": None, "seed_policy": "generate_once",
            }]})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.job = self.job_row("prepare-root")
        self.intent = json_contract({"parameters": {"width": 768, "height": 1344, "fps": 24, "duration_seconds": 3},
            "inputs": {}, "manual_channels": {"positive_prompt": "A red cube turns."},
            "subject_facts": [], "source_language": "", "target_language": ""})
        self.resolver = Mock(side_effect=self.resolve)

    def job_row(self, key):
        return AssetGenerationJob.objects.create(owner=self.actor, model_deployment=self.deployment, execution_instance_version=self.version,
            job_type="asset", media_type="video", prompt="", operation="video.text_to_video",
            idempotency_key=key, request_snapshot_sha256=json_contract({}).sha256)

    def resolve(self, definition, owner):
        return resolve_execution_request_facts(definition, self.intent.value, owner=self.actor, unit_key="asset-request",
            max_media_bytes=65536, parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No undeclared parameter sources"),
            review_contract=json_contract({"schema_version": 1, "mode": "manual"}))

    def create(self, **changes):
        return create_execution_instance_preparation(**{
            "owner_ref": ("asset_job_id", self.job.pk), "version_id": self.version.pk,
            "expected_definition_sha256": self.definition.sha256, "unit_key": "asset-request", "request_key": "candidate-1",
            "intent": self.intent, "resolve_facts": self.resolver, "actor": self.actor, **changes,
        })

    def test_exact_replay_uses_frozen_seed_and_never_resolves_or_draws_again(self):
        with patch("studio.services.execution_parameter_preparation.randbelow", return_value=2) as random:
            first = self.create()
            self.assertEqual(first.contract.resolved_inputs["parameters"]["generated_values"], {"seed": 10})
            self.assertEqual(first.contract.intent["resolved_inputs_sha256"], first.contract.resolved_inputs_sha256)
            self.assertEqual(first.contract.intent["review_contract_sha256"], first.contract.review_contract_sha256)
            self.assertEqual(self.create().pk, first.pk)
            random.assert_called_once()
        self.resolver.assert_called_once()
        self.assertEqual(self.resolver.call_args.args[1].pk, self.job.pk)
        self.assertEqual(first.status, "preparing")
        self.assertIsNone(first.request_frozen_at)
        self.assertIsNone(first.contract.prepared_request)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "preparing")  # Preparation allocation does not dispatch the root.

    def test_large_resolved_facts_persist_read_and_replay_without_resolution(self):
        from studio.services.execution_preparations import read_execution_preparation_detail
        from studio.services.execution_request_storage import execution_request_max_bytes
        original = self.resolve(self.definition, self.job)
        payload = deepcopy(original.resolved_inputs.value)
        payload["profile_sources"] = {"context": "界" * 100000}
        large = json_contract(payload, max_bytes=execution_request_max_bytes())
        self.assertGreater(large.size_bytes, 262144)
        resolver = Mock(return_value=ResolvedExecutionPreparation(large, original.review_contract))
        prep = self.create(resolve_facts=resolver)
        detail = read_execution_preparation_detail(prep)
        self.assertEqual(detail.resolved_inputs, large.value)
        self.assertEqual(detail.resolved_inputs_sha256, large.sha256)
        self.assertEqual(detail.resolved_inputs_size_bytes, large.size_bytes)
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("No new seed")):
            self.assertEqual(self.create(resolve_facts=resolver).pk, prep.pk)
        resolver.assert_called_once()
        with patch("studio.services.execution_request_storage.boundary_int", return_value=65536):
            with self.assertRaisesMessage(ValidationError, "65536"):
                read_execution_preparation_detail(prep)
        detail.resolved_inputs["profile_sources"]["context"] += "篡改"
        with self.assertRaises(ValidationError):
            read_execution_preparation_detail(prep, detail=detail)

    def test_replay_after_target_drift_reads_history_but_new_request_fails(self):
        first = self.create()
        config = deepcopy(self.provider.driver_config)
        config["poll_path"] = "/changed/jobs/{request_id}"
        self.write_contract(self.provider, "driver_config", config)
        self.assertEqual(self.create().pk, first.pk)
        other = self.job_row("prepare-drift")
        with self.assertRaises(ValidationError):
            self.create(owner_ref=("asset_job_id", other.pk))
        self.resolver.assert_called_once()

    def test_same_owner_cannot_change_request_unit_inputs_or_definition(self):
        self.create()
        for changes in (
            {"request_key": "candidate-2"}, {"unit_key": "another"},
            {"intent": json_contract({**self.intent.value, "inputs": {"unexpected": "value"}})},
            {"expected_definition_sha256": "a" * 64},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError) as caught:
                self.create(**changes)
            self.assertEqual(caught.exception.code, "execution_preparation_conflict")
        self.resolver.assert_called_once()

    def test_owner_permission_is_required_even_on_replay(self):
        self.create()
        outsider = get_user_model().objects.create_user(username="prepare-outsider")
        with self.assertRaises(PermissionDenied):
            self.create(actor=outsider)
        self.actor.is_active = False
        with self.assertRaises(PermissionDenied):
            self.create()
        self.resolver.assert_called_once()

    def test_inactive_instance_blocks_new_prepare_not_reading_existing(self):
        first = self.create()
        self.instance.is_active = False
        self.instance.save(update_fields=("is_active",))
        self.assertEqual(self.create().pk, first.pk)
        other = self.job_row("prepare-inactive")
        with self.assertRaisesMessage(ValidationError, "启用实例"):
            self.create(owner_ref=("asset_job_id", other.pk))

    def test_bad_facts_and_write_failure_do_not_leave_partial_preparation(self):
        invalid = ResolvedExecutionPreparation(
            JsonContract({"schema_version": 1, "field_requests": []}, 1, 1, "b" * 64),
            json_contract({"schema_version": 1, "mode": "manual"}),
        )
        with self.assertRaises(ValidationError):
            self.create(resolve_facts=lambda *_: invalid)
        self.assertFalse(ExecutionPreparation.objects.exists())
        with patch.object(ExecutionPreparationContract, "save", side_effect=RuntimeError("storage failure")):
            with self.assertRaisesMessage(RuntimeError, "storage failure"):
                self.create()
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ExecutionPreparationContract.objects.exists())

    def test_root_creation_outer_transaction_rolls_back_with_preparation(self):
        with self.assertRaisesMessage(RuntimeError, "caller failure"):
            with transaction.atomic():
                self.job = self.job_row("prepare-atomic")
                self.create()
                raise RuntimeError("caller failure")
        self.assertFalse(AssetGenerationJob.objects.filter(idempotency_key="prepare-atomic").exists())
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_missing_detail_or_inconsistent_final_request_is_not_rebuilt(self):
        prep = self.create()
        detail = prep.contract
        from studio.services.execution_request_storage import store_prepared_request
        store_prepared_request(detail, {"schema_version": 1, "preparation_id": str(prep.pk), "payload": "not finalized"})
        detail.save()
        with self.assertRaisesMessage(ValidationError, "最终请求索引"):
            self.create()
        detail.delete()
        with self.assertRaisesMessage(ValidationError, "缺少已冻结详情"):
            self.create()
        self.resolver.assert_called_once()

    def test_no_automatic_approval_or_untyped_factory_result(self):
        good = self.resolve(self.definition, self.job)
        for invalid in ({"resolved_inputs": good.resolved_inputs.value}, ResolvedExecutionPreparation(
                good.resolved_inputs, json_contract({"schema_version": 1, "mode": "automatic"}))):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.create(resolve_facts=lambda *_: invalid)
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_stage_units_and_candidates_have_separate_preparations_and_seeds(self):
        # Actual persisted root relationships. This fixture is not a published
        # recipe/admission proof; production queue integration is still separate.
        member = get_user_model().objects.create_user(username="prepare-run-owner")
        project = SeriesProject.objects.create(owner=member, title="Preparation storage", language="English",
            aspect_ratio="9:16", width=768, height=1344, fps=24, default_shot_duration_ms=3000, continuity_seed=1)
        run = ProductionRun.objects.create(owner=member, project=project, plan_source="manual", recipe_version=None,
            idempotency_key="prepare-run", request_sha256=json_contract({}).sha256,
            config_snapshot_sha256=json_contract({}).sha256, input_snapshot_sha256=json_contract({}).sha256)
        from studio.services.production_run_plans import freeze_run_execution_plan
        from studio.services.production_stage_catalog import production_stage_catalog
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=member)
        stage = StageRun.objects.create(production_run=run, stage_key="media_generation", ordinal=16, mode="manual", idempotency_key="prepare-stage")
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=[1, 2, 3]) as random:
            first = self.create(owner_ref=("stage_run_id", stage.pk), actor=member, unit_key="unit-one")
            second = self.create(owner_ref=("stage_run_id", stage.pk), actor=member, unit_key="unit-two")
            third = self.create(owner_ref=("stage_run_id", stage.pk), actor=member, unit_key="unit-one", request_key="candidate-2")
            self.assertEqual(self.create(owner_ref=("stage_run_id", stage.pk), actor=member, unit_key="unit-one").pk, first.pk)
            self.assertEqual(random.call_count, 3)
        self.assertEqual([row.contract.resolved_inputs["parameters"]["generated_values"]["seed"] for row in (first, second, third)], [5, 10, 15])
        self.assertEqual(stage.execution_preparations.count(), 3)
        outsider = get_user_model().objects.create_user(username="prepare-run-outsider")
        with self.assertRaises(PermissionDenied):
            self.create(owner_ref=("stage_run_id", stage.pk), actor=outsider, unit_key="unit-one")

    def test_smoke_purpose_is_derived_from_root_and_requires_admin(self):
        smoke = ModelDeploymentSmokeAttempt.objects.create(deployment=self.deployment, operation=self.version.operation,
            scope="instance", execution_instance_version=self.version,
            status="preparing", request_sha256="a" * 64, target_sha256="b" * 64)
        member = get_user_model().objects.create_user(username="prepare-smoke-member")
        with self.assertRaises(PermissionDenied):
            self.create(owner_ref=("deployment_smoke_id", smoke.pk), actor=member)
        prep = self.create(owner_ref=("deployment_smoke_id", smoke.pk))
        self.assertEqual(prep.purpose, "instance_smoke")
        self.assertEqual(prep.execution_instance_version_id, self.version.pk)
        self.assertIsNone(prep.asset_job_id)
