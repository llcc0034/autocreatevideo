from hashlib import sha256
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from studio.tests.workflow_projector_fixtures import workflow_projector_reference

from studio.models import (
    ModelDeployment,
    ModelProfileVersion,
    ProviderProfile,
    RuntimeHost,
    WorkflowTemplate,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.workflow_versions import (
    import_workflow_version,
    smoke_test_workflow_version,
    validate_workflow_version,
)
from studio.services.workflow_smoke_fixtures import (
    finalize_workflow_smoke_fixture,
    normalize_workflow_smoke_fixture,
)
from studio.services.workflow_versions import _materialize_smoke_graph


@override_settings(COMFYUI_OUTPUT_DIR="")
class TextWorkflowSmokeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="text-workflow-admin",
            password="StrongPass123!",
            email="text-workflow@example.test",
        )
        self.provider = ProviderProfile.objects.get(key="local-comfyui")
        self.profile = ModelProfileVersion(
            profile_key="comfy-text-test",
            version=1,
            provider=self.provider,
            model_identifier="qwen3.5-text-test",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_by=self.user,
        )
        assign_json_contract(self.profile, "specification", {"operations": ["text.generate"]})
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
                return {"request_id": "text-request"}
            if operation == "poll":
                return {"status": "succeeded"}
            if operation == "collect":
                return {"status": "succeeded", "outputs": outputs}
            raise AssertionError((operation, payload))

        driver.invoke.side_effect = invoke
        return driver

    def test_text_smoke_uses_declared_node_and_persists_hash_only(self):
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
            fixture = normalize_workflow_smoke_fixture(validated, {
                "schema_version": 2,
                "values": {"prompt.positive": "bounded smoke test"},
                "assets": [],
            })
            fixture = finalize_workflow_smoke_fixture(
                fixture,
                materialized_graph_sha256=_materialize_smoke_graph(
                    validated,
                    fixture_values=fixture.values,
                    fixture_asset_facts=fixture.asset_facts,
                ).graph_sha256,
            )
            smoked, _result = smoke_test_workflow_version(
                validated,
                deployment=self.deployment,
                fixture_values=fixture.values,
                fixture_asset_facts=fixture.asset_facts,
                materialized_graph_sha256=fixture.materialized_graph_sha256,
                fixture_sha256=fixture.fixture_sha256,
                smoke_attempt_id="00000000-0000-4000-8000-000000000001",
            )

        self.assertEqual(smoked.smoke_test_state, "passed")
        self.assertEqual(smoked.smoke_test_evidence["output_kind"], "text")
        self.assertEqual(smoked.smoke_test_evidence["outputs"], [{
            "node_id": "2",
            "kind": "text",
            "size_bytes": len(encoded),
            "sha256": sha256(encoded).hexdigest(),
        }])
        self.assertNotIn(text, str(smoked.smoke_test_evidence))
        self.assertEqual(smoked.smoke_test_evidence["fixture_sha256"], fixture.fixture_sha256)
        self.assertEqual(len(smoked.smoke_test_evidence["compiled_graph_sha256"]), 64)
