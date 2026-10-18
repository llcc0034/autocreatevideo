"""Only measured dimensions are described; no live capacity certification."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from studio.services.execution_capacity_signatures import capacity_runtime_facts, describe_execution_capacity
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_workflow_request_media as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionCapacityRequestTests(TestCase):
    setUp = fixtures.ExecutionWorkflowMediaRequestTests.setUp
    _create_version = fixtures.ExecutionWorkflowMediaRequestTests._create_version
    compile = fixtures.ExecutionWorkflowMediaRequestTests.compile

    def describe(self, transport=None):
        return describe_execution_capacity(self.definition, transport or self.compile(),
            runtime={"kind": "observed_host", "host_id": "isolated-host", "fingerprint": "a" * 64}).value

    def test_decoded_reference_uses_and_compiled_output_are_not_claimed_as_covered(self):
        result = self.describe()
        known = result["known_dimensions"]
        self.assertEqual(known["output"], {"width": 320, "height": 512, "frames": 124, "fps": 24, "steps": 4})
        self.assertEqual(known["reference_count"], 2)
        self.assertEqual(known["references"][0]["effective_dimensions"], {"width": 12, "height": 18, "frame_count": 1})
        self.assertEqual((result["state"], result["signature_sha256"]), ("unknown", None))
        self.assertIn("text.input_tokens_and_tokenizer", result["missing_dimensions"])
        self.assertNotIn("sampling.seed", known["parameters"])
        self.assertNotIn(str(self.asset.pk), str(result))
        self.assertNotIn("A parcel opens", str(result))

    def test_seed_prompt_and_file_identity_are_not_size_identity(self):
        before = self.describe()
        request = deepcopy(self.compile().value)
        request["payload"]["compiled_workflow"]["resolved_semantics"]["sampling.seed"] = 999
        request["prompt_channels"]["values"]["positive_prompt"] = "Different text without tokenizer."
        for binding in request["payload"]["media_preparation"]["bindings"]:
            binding["asset_id"] = "another-asset"
            binding["item_key"] = "another-use"
            binding["source_sha256"] = "b" * 64
        self.assertEqual(self.describe(json_contract(request)), before)
        request["payload"]["compiled_workflow"]["resolved_semantics"]["video.frames"] = 141
        self.assertNotEqual(self.describe(json_contract(request))["known_dimensions"], before["known_dimensions"])

    def test_missing_effective_video_dimensions_do_not_use_source_as_processed_size(self):
        request = deepcopy(self.compile().value)
        media = request["payload"]["media_preparation"]
        media["reference_media_profile"] = {"sha256": "b" * 64}
        media["bindings"][0]["kind"] = "video"
        media["bindings"][0]["media_facts"]["observed_media"] = {"width": 1920, "height": 1080, "duration_ms": 4000}
        result = self.describe(json_contract(request))
        reference = result["known_dimensions"]["references"][0]
        self.assertEqual(reference["source_dimensions"]["width"], 1920)
        self.assertIsNone(reference["effective_dimensions"])
        self.assertIn("references.0.effective_dimensions", result["missing_dimensions"])

    def test_other_request_identity_and_unknown_runtime_schema_are_rejected(self):
        request = deepcopy(self.compile().value)
        request["target_fingerprint"] = "b" * 64
        with self.assertRaises(ValidationError):
            self.describe(json_contract(request))
        with self.assertRaises(ValidationError):
            describe_execution_capacity(self.definition, self.compile(), runtime={"gpu": "guessed"})


class ExecutionCapacityRuntimeTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch("studio.services.execution_capacity_signatures.execution_configuration_max_bytes", return_value=65536))
        self.version = SimpleNamespace(deployment=SimpleNamespace(runtime_host_id="host-1", provider=SimpleNamespace(driver_type="comfyui")))
        self.snapshot = SimpleNamespace(probe_status="succeeded", cpu_architecture="AMD64", cpu_name="Test CPU",
            logical_cpu_count=8, memory_total_bytes=16 * 1024**3, gpu_count=1, comfyui_version="isolated-runtime",
            details={"probe_scope": "local_process", "gpus": [{"index": 0, "name": "Test GPU", "vram_total_bytes": 8 * 1024**3,
                "driver_version": "1", "compute_capability": "test"}]})

    def test_host_signature_does_not_change_for_probe_time_id_or_free_space(self):
        before = capacity_runtime_facts(self.version, self.snapshot)
        self.snapshot.id, self.snapshot.disk_free_bytes = "next-probe", 100
        self.snapshot.details["probe_errors"] = []
        self.assertEqual(capacity_runtime_facts(self.version, self.snapshot), before)
        self.snapshot.details["gpus"][0]["vram_total_bytes"] *= 2
        self.assertNotEqual(capacity_runtime_facts(self.version, self.snapshot)["fingerprint"], before["fingerprint"])

    def test_partial_unattested_or_missing_host_is_unknown_not_zero_hardware(self):
        self.snapshot.probe_status = "partial"
        self.assertEqual(capacity_runtime_facts(self.version, self.snapshot)["kind"], "unknown")
        self.snapshot.probe_status = "succeeded"
        self.snapshot.details["probe_scope"] = "remote_unattested"
        self.assertEqual(capacity_runtime_facts(self.version, self.snapshot)["kind"], "unknown")
        self.assertEqual(capacity_runtime_facts(self.version, None)["kind"], "unknown")
        self.version.deployment.runtime_host_id = None
        self.assertEqual(capacity_runtime_facts(self.version, None)["kind"], "unknown")
        self.version.deployment.provider.driver_type = "async_rest"
        self.assertEqual(capacity_runtime_facts(self.version, None), {"kind": "provider_managed", "host_id": None, "fingerprint": None})
