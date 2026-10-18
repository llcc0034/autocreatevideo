from hashlib import sha256
from unittest.mock import Mock, patch
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from studio.tests.workflow_projector_fixtures import workflow_projector_reference

from studio.models import (
    ModelDeployment,
    ModelProfileVersion,
    ProviderProfile,
    RuntimeHost,
    WorkflowTemplate,
    WorkflowSmokeAttempt,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.workflow_versions import (
    import_workflow_version,
    validate_workflow_version,
)
from studio.services.execution_instances import ExecutionResourceSelection
from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
from studio.services.execution_smoke import create_resource_smoke
from studio.services.workflow_smoke_queue import claim_next_workflow_smoke, process_workflow_smoke
from studio.tests.test_execution_resource_smoke_media import dispatch_resource_worker_fixture


@override_settings(COMFYUI_OUTPUT_DIR="", AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class TextWorkflowSmokeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="text-workflow-admin",
            password="StrongPass123!",
            email="text-workflow@example.test",
        )
        self.provider = ProviderProfile.objects.get(key="local-comfyui")
        assign_json_contract(self.provider, "driver_config", {"base_url": "http://127.0.0.1:8188", "timeout_seconds": 30,
            "capability_ttl_seconds": 3600, "poll_interval_seconds": 1, "max_poll_seconds": 60})
        self.provider.save()
        self.profile = ModelProfileVersion(
            profile_key="comfy-text-test",
            version=1,
            provider=self.provider,
            model_identifier="qwen3.5-text-test",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_by=self.user,
        )
        assign_json_contract(self.profile, "specification", {"capabilities": ["text.generate"]})
        self.profile.save()
        host = RuntimeHost.objects.create(key="text-workflow-host", name="Text workflow host")
        self.deployment = ModelDeployment(
            key="text-workflow-deployment",
            name="Text workflow deployment",
            profile_version=self.profile,
            runtime_host=host,
        )
        assign_json_contract(self.deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8188", "timeout_seconds": 10,
        })
        self.deployment.save()
        self.template = WorkflowTemplate.objects.create(
            owner=None,
            name="Comfy text workflow",
            slug="comfy-text-workflow",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.TEXT,
            model="qwen3.5-text-test",
            version=1,
            definition={},
            input_schema={},
            is_system=False,
            is_active=True,
        )

    @staticmethod
    def object_info():
        return {
            "TextGenerate": {
                "input": {"required": {"prompt": ["STRING", {}]}},
                "output": ["STRING"],
                "output_name": ["text"],
            },
            "PreviewAny": {
                "input": {"required": {"source": ["STRING", {}]}},
                "output": [],
                "output_name": [],
                "output_node": True,
            },
        }

    def version(self):
        return import_workflow_version(
            template=self.template,
            graph={
                "1": {"class_type": "TextGenerate", "inputs": {"prompt": "draft"}},
                "2": {"class_type": "PreviewAny", "inputs": {"source": ["1", 0]}},
            },
            input_contract={
                **workflow_projector_reference(),
                "schema_version": 3,
                "workflow_mode": "semantic",
                "capability": "text.generate",
                "compiler": {"id": "generic.comfy", "version": 1},
                "parameters": {
                    "prompt.positive": {"type": "string", "required": True, "min_length": 1},
                },
                "outputs": {"primary": {"node_id": "2", "kind": "text", "required": True}},
            },
            semantic_bindings=[{
                "semantic_key": "prompt.positive",
                "node_id": "1",
                "input_name": "prompt",
                "value_type": "string",
                "is_required": True,
            }],
            model_requirements=[],
            provider=self.provider,
            model_profile_version=self.profile,
        )

    def driver(self, outputs):
        driver = Mock()
        driver.capabilities.return_value = {
            "operations": ["health", "object_info", "submit", "poll", "cancel", "collect"],
        }

        def invoke(_deployment, operation, payload):
            if operation == "object_info":
                return self.object_info()
            if operation == "health":
                return {"system": {"comfyui_version": "test"}}
            if operation == "submit":
                return {"request_id": payload["submission_token"]}
            if operation == "poll":
                return {"status": "succeeded"}
            if operation == "collect":
                return {"status": "succeeded", "outputs": outputs}
            raise AssertionError((operation, payload))

        driver.invoke.side_effect = invoke
        return driver

    def test_text_resource_smoke_uses_declared_node_and_original_preparation_result(self):
        from studio.tests.execution_runtime_fixtures import record_workflow_runtime
        record_workflow_runtime(self.deployment)
        version = self.version()
        text = "generated text"
        encoded = text.encode("utf-8")
        driver = self.driver([{
            "node_id": "2",
            "kind": "text",
            "text": text,
            "size_bytes": len(encoded),
            "sha256": sha256(encoded).hexdigest(),
        }])

        with patch("studio.services.workflow_versions.driver_for", return_value=driver):
            validated, _report = validate_workflow_version(
                version, deployment=self.deployment,
            )
        selection = ExecutionResourceSelection(self.deployment, "text.generate", "", "manual", None, validated)
        definition = build_resource_smoke_definition(selection)
        attempt, prep, _ = create_resource_smoke(selection=selection, expected_definition_sha256=definition.sha256,
            fixture={"inputs": {}, "parameters": {}, "manual_channels": {"positive_prompt": "bounded smoke test"},
                "subject_facts": [], "source_language": "", "target_language": ""},
            field_values=[], client_idempotency_key="text-resource", allow_external_calls=False, budget_limit=None, actor=self.user)
        prep = dispatch_resource_worker_fixture(prep, attempt, self.user)
        # Admission reads the isolated host observations; only the remote peer
        # is substituted. Both original queue cycles and output checks are real.
        with patch("studio.services.execution_workflow_smoke_worker.driver_for", return_value=driver):
            submitted = process_workflow_smoke(claim_next_workflow_smoke("text-resource"))
            self.assertEqual(submitted.status, "running", submitted.error_code)
            self.assertEqual(submitted.provider_request_id, str(prep.pk))
            WorkflowSmokeAttempt.objects.filter(pk=attempt.pk).update(next_poll_at=timezone.now() - timedelta(seconds=1))
            done = process_workflow_smoke(claim_next_workflow_smoke("text-resource"))
        self.assertEqual(done.status, "succeeded", done.error_code)
        facts = done.result["evidence"]["artifact_facts"][0]
        self.assertEqual((facts["node_id"], facts["kind"], facts["size_bytes"], facts["sha256"]),
            ("2", "text", len(encoded), sha256(encoded).hexdigest()))
        self.assertNotIn("text", facts)  # Parsed result, not duplicate raw output.
        validated.refresh_from_db()
        self.assertEqual(validated.smoke_test_state, "passed")
        self.assertEqual(validated.smoke_test_evidence["result_sha256"], done.result_sha256)
        self.assertEqual(validated.smoke_test_evidence["preparation_id"], str(prep.pk))
        self.assertNotIn(text, str(validated.smoke_test_evidence))
