"""Actual PNG reads and durable upload receipts with a fake Comfy peer.

These are staging protocol tests, not paid Smoke or worker/admission acceptance.
"""
from copy import deepcopy
from hashlib import sha256

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import AssetGenerationJob
from studio.services.execution_workflow_staging import stage_execution_workflow_media
from studio.services.json_contracts import json_contract
from studio.services.workflow_media_tokens import collect_workflow_media_use_tokens
from studio.tests import test_execution_workflow_request_media as fixtures


class RecordingUploadClient:
    def __init__(self):
        self.files, self.uploads = {}, []

    def upload_input_file(self, source, *, subfolder, filename):
        content = source.read_bytes()
        path = subfolder + "/" + filename
        self.files[path] = content
        self.uploads.append(path)
        return {"type": "input", "subfolder": subfolder, "name": filename}

    def staged_input_sha256(self, path):
        return sha256(self.files[path]).hexdigest()

    def queue_prompt(self, *_args, **_kwargs):
        raise AssertionError("Staging cannot submit a generation")


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionWorkflowStagingTests(TestCase):
    _create_version = fixtures.ExecutionWorkflowMediaRequestTests._create_version
    compile = fixtures.ExecutionWorkflowMediaRequestTests.compile

    def setUp(self):
        fixtures.ExecutionWorkflowMediaRequestTests.setUp(self)
        self.transport = self.compile()
        self.peer = RecordingUploadClient()
        self.job = AssetGenerationJob.objects.create(owner=self.user, model_deployment=self.deployment,
            execution_instance_version_id=self.definition.value["version_id"],
            workflow_template_version=self.workflow, execution_kind="comfy_workflow", job_type="asset", media_type="video", operation="video.reference_to_video",
            prompt="", idempotency_key="staging-protocol-fixture", request_snapshot_sha256=json_contract({}).sha256)

    def persist(self, records):
        self.job.staged_inputs = records
        self.job.save(update_fields=["staged_inputs", "updated_at"])

    def stage(self, **changes):
        self.job.refresh_from_db()
        return stage_execution_workflow_media(**{"definition": self.definition, "resolved_inputs": self.inputs,
            "transport": self.transport, "owner": self.user, "root_id": self.job.pk, "client": self.peer,
            "staged_inputs": self.job.staged_inputs, "persist_staged_inputs": self.persist,
            "check_active": lambda: None, "max_media_bytes": 65536, **changes})

    def test_missing_frozen_subject_is_rejected_before_upload_or_receipt_writes(self):
        value = deepcopy(self.inputs.value)
        row = value["logical_values"]["references"][0]
        row.pop("subject")
        row["source_sha256"] = json_contract({key: item for key, item in row.items() if key != "source_sha256"}).sha256
        with self.assertRaisesMessage(ValidationError, "无主体须明确为null"):
            self.stage(resolved_inputs=json_contract(value))
        self.assertEqual(self.peer.uploads, [])
        self.job.refresh_from_db()
        self.assertEqual(self.job.staged_inputs, [])

    def test_each_use_uploads_once_and_replay_reuses_durable_receipts_without_graph_mutation(self):
        original = deepcopy(self.transport.value)
        result = self.stage()
        self.assertEqual(len(self.peer.uploads), 2)
        self.assertEqual(collect_workflow_media_use_tokens(result.graph), set())
        self.assertEqual(result.graph["cr_images_0"]["inputs"]["image"], self.peer.uploads[0])
        self.assertEqual(result.graph["cr_images_1"]["inputs"]["image"], self.peer.uploads[1])
        self.job.refresh_from_db()
        self.assertEqual(self.job.staged_inputs, result.receipts.value)
        replay = self.stage()
        self.assertEqual(replay, result)
        self.assertEqual(len(self.peer.uploads), 2)
        self.assertEqual(self.transport.value, original)

    def test_interrupt_after_one_persisted_upload_resumes_only_the_remaining_use(self):
        def interrupt():
            self.job.refresh_from_db()
            if len(self.job.staged_inputs) == 1:
                raise RuntimeError("simulated worker stop")
        with self.assertRaisesMessage(RuntimeError, "simulated worker stop"):
            self.stage(check_active=interrupt)
        self.job.refresh_from_db()
        self.assertEqual(len(self.job.staged_inputs), 1)
        self.assertEqual(len(self.peer.uploads), 1)
        result = self.stage()
        self.assertEqual(len(result.receipts.value), 2)
        self.assertEqual(len(self.peer.uploads), 2)

    def test_cached_path_cannot_outlive_asset_deactivation_or_remote_content_change(self):
        result = self.stage()
        self.asset.is_active = False
        self.asset.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            self.stage()
        self.asset.is_active = True
        self.asset.save(update_fields=["is_active"])
        self.peer.files[result.uploaded_paths[0]] = b"changed remotely"
        with self.assertRaisesMessage(ValidationError, "字节摘要"):
            self.stage()
        self.assertEqual(len(self.peer.uploads), 2)

    def test_frozen_use_order_survives_display_edits_but_not_changed_source_digest(self):
        original = deepcopy(self.transport.value)
        self.asset.name = "Renamed after preparation"
        self.asset.save(update_fields=["name"])
        result = self.stage()
        self.assertEqual(self.transport.value, original)
        self.assertEqual([row["item_key"] for row in result.receipts.value], ["second", "first"])
        self.assertEqual(result.graph["cr_images_0"]["inputs"]["image"], self.peer.uploads[0])
        self.asset.checksum = "f" * 64
        self.asset.save(update_fields=["checksum"])
        with self.assertRaises(ValidationError):
            self.stage()
        self.assertEqual(len(self.peer.uploads), 2)

    def test_receipts_cannot_cross_task_target_or_upload_namespace(self):
        original = self.stage().receipts.value
        for field, value in (("root_id", "another-root"), ("target_fingerprint", "0" * 64),
                ("comfy_path", "autoprompt/other-user/other-task/source.png")):
            modified = deepcopy(original)
            modified[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.stage(staged_inputs=modified)
        self.assertEqual(len(self.peer.uploads), 2)

    def test_failure_to_persist_receipt_does_not_return_a_successful_staged_graph(self):
        def fail(_records):
            raise RuntimeError("receipt storage unavailable")
        with self.assertRaisesMessage(RuntimeError, "receipt storage unavailable"):
            self.stage(persist_staged_inputs=fail)
        self.job.refresh_from_db()
        self.assertEqual(self.job.staged_inputs, [])
        self.assertEqual(len(self.peer.uploads), 1)
