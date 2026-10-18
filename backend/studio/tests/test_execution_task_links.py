"""Required root links and one active attempt per preparation, on a real DB."""
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from studio.models import (AssetGenerationJob, ExecutionAuxiliaryBinding, ExecutionPreparation, GenerationAttempt,
    ModelExecutionInstanceVersion, ProductionRecipeVersion, ProductionRun, SeriesProject, StageRun)
from studio.services.execution_task_links import validate_execution_task_links
from studio.tests import test_execution_preparation_storage as fixtures


class ExecutionTaskLinkTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionPreparationStorageTests.setUpTestData.__func__)

    def setUp(self):
        self.job = self.job_row()
        self.prep = ExecutionPreparation.objects.create(asset_job=self.job, execution_instance_version=self.version,
            purpose="production", unit_key="asset", request_key="main", status="preparing", created_by=self.actor,
            intent_sha256="a" * 64)
        project = SeriesProject.objects.create(owner=self.actor, title="Storage fixture", language="English",
            aspect_ratio="9:16", width=512, height=768, fps=24, default_shot_duration_ms=3000, continuity_seed=1)
        recipe = ProductionRecipeVersion.objects.create(recipe_key="link-test", version=1, name="Storage fixture", definition_sha256="a" * 64)
        run = ProductionRun.objects.create(owner=self.actor, project=project, recipe_version=recipe, plan_source="recipe",
            idempotency_key="link-run", request_sha256="a" * 64, config_snapshot_sha256="a" * 64, input_snapshot_sha256="a" * 64)
        self.stage = StageRun.objects.create(production_run=run, stage_key="text", ordinal=1,
            mode="manual", idempotency_key="link-stage")
        self.stage_prep = ExecutionPreparation.objects.create(stage_run=self.stage, execution_instance_version=self.version,
            purpose="production", unit_key="source", request_key="main", status="preparing", created_by=self.actor,
            intent_sha256="a" * 64)

    def job_row(self, **changes):
        return AssetGenerationJob.objects.create(**{ "owner": self.actor, "model_deployment": self.deployment,
            "execution_instance_version": self.version, "job_type": "prompt", "media_type": "script",
            "operation": self.version.operation, "idempotency_key": str(uuid4()), "request_snapshot_sha256": "a" * 64, **changes})

    def attempt_row(self, **changes):
        return GenerationAttempt.objects.create(**{"stage_run": self.stage, "preparation": self.stage_prep,
            "execution_instance_version": self.version, "model_deployment": self.deployment, "operation": self.version.operation,
            "role": "primary", "unit_key": self.stage_prep.unit_key, "attempt_number": 1, "request_sha256": "a" * 64, **changes})

    def test_required_foreign_keys_cannot_accept_unlinked_old_jobs_or_attempts(self):
        for create, values in ((self.job_row, {"execution_instance_version": None}),
                (self.attempt_row, {"execution_instance_version": None}), (self.attempt_row, {"preparation": None})):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                create(**values)
        with self.assertRaises(ProtectedError):
            self.version.delete()

    def test_preparing_is_not_remote_queue_and_auxiliary_fields_are_an_exact_union(self):
        self.assertEqual(self.job.status, "preparing")
        for values in ({"job_type": "field", "media_type": "text"}, {"parent_preparation": self.prep},
                {"auxiliary_purpose": "generate_fields"}, {"attempt_number": 1}, {"job_type": "media", "media_type": "text"}):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                self.job_row(**values)
        # A primary prompt operation can legitimately return text without
        # becoming an auxiliary field-generation request.
        text_job = self.job_row(media_type="text")
        self.assertIsNone(text_job.parent_preparation_id)
        self.assertEqual(text_job.auxiliary_purpose, "")

    def test_field_batch_retry_number_is_unique_and_auxiliary_target_is_explicit(self):
        leaf = ModelExecutionInstanceVersion.objects.create(instance=self.instance, version=2, deployment=self.deployment,
            operation="prompt.fields.generate", prompt_mode="none")
        ExecutionAuxiliaryBinding.objects.create(version=self.version, purpose="generate_fields", target_version=leaf)
        fields = {"job_type": "field", "media_type": "text", "parent_preparation": self.prep,
            "auxiliary_purpose": "generate_fields", "field_batch_key": "batch-sha", "attempt_number": 1,
            "execution_instance_version": leaf, "operation": leaf.operation}
        child = self.job_row(**fields)
        validate_execution_task_links(child)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.job_row(**fields)
        self.job_row(**{**fields, "attempt_number": 2})
        child.execution_instance_version = self.version
        child.operation = self.version.operation
        with self.assertRaisesMessage(ValidationError, "明确绑定"):
            validate_execution_task_links(child)

    def test_only_one_active_attempt_can_use_one_preparation(self):
        first = self.attempt_row()
        validate_execution_task_links(first)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.attempt_row(attempt_number=2)
        first.status = "failed"
        first.save(update_fields=["status"])
        second = self.attempt_row(attempt_number=2)
        self.assertEqual(second.preparation_id, first.preparation_id)
        self.assertEqual(GenerationAttempt.objects.count(), 2)

    def test_attempt_cannot_use_an_asset_preparation_or_another_unit(self):
        attempt = self.attempt_row()
        attempt.preparation = self.prep
        with self.assertRaisesMessage(ValidationError, "阶段、准备"):
            validate_execution_task_links(attempt)
        attempt.preparation = self.stage_prep
        attempt.unit_key = "another-unit"
        with self.assertRaisesMessage(ValidationError, "阶段、准备"):
            validate_execution_task_links(attempt)
        for changes in ({"role": "primary", "field_batch_key": "fake-auxiliary"}, {"role": "auxiliary"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                self.attempt_row(attempt_number=2, status="failed", **changes)

    def test_root_target_indexes_do_not_override_selected_instance(self):
        self.job.operation = "script.generate"
        with self.assertRaisesMessage(ValidationError, "执行能力"):
            self.job.clean()
