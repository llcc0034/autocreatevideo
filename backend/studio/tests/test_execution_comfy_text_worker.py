"""Inline text through the real frozen graph, original queues and result store.

Only resource observations and the remote peer are isolated fixtures. No text
output validator, graph compiler, original claim or finalization is bypassed.
"""
from datetime import timedelta
from hashlib import sha256
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import Asset, AssetGenerationJob, WorkflowSmokeAttempt, ExecutionCapacityObservation
from studio.services.asset_generation import create_asset_generation_job, claim_next_comfy_asset_job, process_comfy_asset_job
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_instance, create_execution_draft, publish_execution_version
from studio.services.execution_smoke import create_instance_smoke
from studio.services.execution_asset_requests import read_asset_prompt_hint
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.workflow_versions import import_workflow_version
from studio.services.workflow_smoke_queue import claim_next_workflow_smoke, process_workflow_smoke
from studio.tests import test_text_workflow_smoke as fixtures
from studio.tests.workflow_projector_fixtures import workflow_projector_reference


@override_settings(COMFYUI_OUTPUT_DIR="", AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionComfyTextWorkerTests(TestCase):
    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        self.enterContext(self.settings(MEDIA_ROOT=media.name))
        fixtures.TextWorkflowSmokeTests.setUp(self)
        assign_json_contract(self.provider, "driver_config", {"base_url": "http://127.0.0.1:8188", "timeout_seconds": 30,
            "capability_ttl_seconds": 3600, "poll_interval_seconds": 1, "max_poll_seconds": 60})
        self.provider.save()
        assign_json_contract(self.profile, "specification", {"capabilities": ["text.generate"]})
        self.profile.save()
        self.workflow = import_workflow_version(template=self.template, provider=self.provider, model_profile_version=self.profile,
            graph={"1": {"class_type": "TextGenerate", "inputs": {"prompt": "unused imported example"}},
                   "2": {"class_type": "PreviewAny", "inputs": {"source": ["1", 0]}}},
            input_contract={**workflow_projector_reference(), "schema_version": 3, "workflow_mode": "semantic",
                "capability": "text.generate", "compiler": {"id": "generic.comfy", "version": 1},
                "parameters": {"prompt.positive": {"type": "string", "required": True, "min_length": 1}},
                "outputs": {"primary": {"node_id": "2", "kind": "text", "required": True}}},
            semantic_bindings=[{"semantic_key": "prompt.positive", "node_id": "1", "input_name": "prompt",
                "value_type": "string", "is_required": True}], model_requirements=[])
        self.workflow.status, self.workflow.validated_deployment = "published", self.deployment
        self.workflow.save()
        family = create_execution_instance(key="inline-text", name="Inline text", description="", actor=self.user)
        self.version = create_execution_draft(instance_id=family.pk, expected_revision=family.revision, actor=self.user,
            selection={"deployment": self.deployment, "operation": "text.generate", "mode": "", "workflow_version": self.workflow,
                "prompt_mode": "manual", "prompt_profile_version": None})
        self.definition = build_execution_definition(self.version.pk)
        publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        resources = json_contract({"schema_version": 1, "blockers": [], "test_scope": "isolated resource facts",
            "capacity_runtime": {"kind": "unknown", "host_id": None, "fingerprint": None}})
        for path in ("execution_smoke_admission", "execution_asset_admission"):
            self.enterContext(patch(f"studio.services.{path}.read_execution_resource_state", return_value=resources))
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("No network")))
        self.peer = Mock()
        self.enterContext(patch("studio.services.execution_workflow_smoke_worker.driver_for", return_value=self.peer))
        self.enterContext(patch("studio.services.asset_generation.driver_for", return_value=self.peer))
        self.intent = {"inputs": {}, "parameters": {}, "manual_channels": {"positive_prompt": "Describe the blue parcel."},
            "subject_facts": [], "source_language": "", "target_language": ""}

    def output(self, text="A blue parcel rests on the table."):
        encoded = text.encode("utf-8")
        return {"request_id": getattr(self, "business_preparation_id", None),
            "outputs": [{"node_id": "2", "kind": "text", "text": text,
            "size_bytes": len(encoded), "sha256": sha256(encoded).hexdigest()}]}

    def smoke(self):
        attempt, prep, _ = create_instance_smoke(version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            fixture=self.intent, field_values=[], client_idempotency_key="inline-smoke", allow_external_calls=False,
            budget_limit=None, actor=self.user)
        ready = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
        self.assertEqual(ready.status, "dispatched", ready.reason)
        self.peer.invoke.return_value = {"request_id": str(prep.pk)}
        claim = claim_next_workflow_smoke("inline-text-smoke")
        self.assertIsNotNone(claim)
        submitted = process_workflow_smoke(claim)
        self.assertEqual(submitted.status, "running", submitted.error_message)
        graph = self.peer.invoke.call_args.args[2]["workflow"]
        self.assertEqual(graph["1"]["inputs"]["prompt"], self.intent["manual_channels"]["positive_prompt"])
        WorkflowSmokeAttempt.objects.filter(pk=attempt.pk).update(next_poll_at=timezone.now() - timedelta(seconds=1))
        self.peer.invoke.side_effect = [{"status": "succeeded"}, self.output()]
        result = process_workflow_smoke(claim_next_workflow_smoke("inline-text-smoke"))
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.peer.invoke.side_effect = None
        return result, prep

    def job(self, *, job_type="prompt", media_type="video"):
        self.smoke()
        job, _ = create_asset_generation_job(owner=self.user, execution_instance_version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, job_type=job_type, media_type=media_type,
            name="Inline candidate", **self.intent, field_values=[], client_idempotency_key="inline-business")
        prep = job.execution_preparations.get()
        ready = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
        self.assertEqual(ready.status, "dispatched", ready.reason)
        self.peer.invoke.return_value = {"request_id": str(prep.pk)}
        submitted = self.cycle(job)
        self.assertEqual(submitted.status, "submitted", submitted.error_message)
        self.peer.invoke.return_value = {"status": "succeeded"}
        self.assertEqual(self.cycle(job).status, "running")
        self.business_preparation_id = str(prep.pk)
        return job, prep

    def cycle(self, job):
        AssetGenerationJob.objects.filter(pk=job.pk).update(next_poll_at=timezone.now() - timedelta(seconds=1))
        claim = claim_next_comfy_asset_job("inline-business")
        self.assertIsNotNone(claim)
        return process_comfy_asset_job(claim)

    def test_inline_smoke_then_prompt_result_without_output_directory_or_asset_file(self):
        job, prep = self.job()
        AssetGenerationJob.objects.filter(pk=job.pk).update(prompt="Stale pre-cutover column; not the submitted input.")
        job.refresh_from_db()
        self.assertEqual(read_asset_prompt_hint(job), self.intent["manual_channels"]["positive_prompt"])
        self.peer.invoke.return_value = self.output("蓝色纸盒放在桌面上。")
        result = self.cycle(job)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(result.result_text, "蓝色纸盒放在桌面上。")
        self.assertEqual(result.result_text_sha256, sha256(result.result_text.encode()).hexdigest())
        self.assertIsNone(result.output_asset_id)
        self.assertIsNone(result.actual_cost)
        self.assertFalse(Asset.objects.exists())
        self.assertEqual(ExecutionCapacityObservation.objects.filter(preparation=prep).count(), 1)
        self.assertIsNone(claim_next_comfy_asset_job("no-repeat"))

    def test_matching_receipt_with_invalid_text_protocol_fails_after_confirmed_completion(self):
        job, prep = self.job()
        self.peer.invoke.return_value = self.output("Text containing \x00 a control character.")
        result = self.cycle(job)
        self.assertEqual(result.status, "failed", result.error_message)
        self.assertEqual(result.remote_status, "succeeded")
        self.assertEqual(result.result_text, "")
        self.assertFalse(ExecutionCapacityObservation.objects.filter(preparation=prep).exists())
        self.assertIsNone(claim_next_comfy_asset_job("no-paid-repeat"))
