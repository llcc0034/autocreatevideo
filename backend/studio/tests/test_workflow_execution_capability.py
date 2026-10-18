from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from studio.models import WorkflowProjectorVersion
from studio.services.json_contracts import assign_json_contract

from studio.services.comfyui_workflow_imports import _suggested_import_contract
from studio.services.model_profile_options import MODEL_CAPABILITY_CATALOG
from studio.services.workflow_projectors import (
    WorkflowProjectorError, current_projector_catalog,
    validate_workflow_projector_definition, workflow_execution_capability_options,
    validate_frozen_workflow_projector_contract,
)


class WorkflowExecutionCapabilityTests(TestCase):
    def projector(self, capability):
        original = next(row for row in current_projector_catalog() if row.engine_key == "generic.fixed")
        definition = deepcopy(original.definition)
        if capability is not None:
            definition["projection"]["config"]["capability"] = capability
        return replace(original, definition=definition)

    def project(self, capability, *, output="image", model_capabilities=None):
        base = ({"outputs": {"primary": {"kind": output}}}, [], [], [], [])
        with patch("studio.services.comfyui_workflow_imports._suggested_generic_import_contract", return_value=deepcopy(base)), patch(
            "studio.services.comfyui_workflow_imports._apply_generic_projector_configuration", return_value=deepcopy(base)
        ), patch("studio.services.comfyui_workflow_imports.verified_model_profile_specification", return_value={"capabilities": model_capabilities or []}):
            return _suggested_import_contract({}, {}, primary_api_node_ids=[], model_dependencies=[], asset_dependencies=[],
                model_profile_version=object() if model_capabilities is not None else None, projector=self.projector(capability))

    def test_choices_reuse_the_model_registry_not_model_names(self):
        self.assertEqual({row["value"] for row in workflow_execution_capability_options()},
            {row["value"] for row in MODEL_CAPABILITY_CATALOG if "comfyui" in row["driver_types"]})

    def test_undeclared_capability_stays_non_executable_without_inference(self):
        contract, _, _, blockers, _ = self.project(None, model_capabilities=["image.text_to_image"])
        self.assertNotIn("capability", contract)
        self.assertEqual(blockers[0]["code"], "workflow_projector_capability_required")

    def test_declared_matching_capability_is_frozen_and_model_optional_until_binding(self):
        for capabilities in (None, ["image.text_to_image"]):
            contract, _, _, blockers, _ = self.project("image.text_to_image", model_capabilities=capabilities)
            self.assertEqual(contract["capability"], "image.text_to_image")
            self.assertEqual(blockers, [])

    def test_model_and_output_mismatches_cannot_pass(self):
        for kwargs in ({"output": "video"}, {"model_capabilities": ["image.image_to_image"]}, {"model_capabilities": []}):
            with self.subTest(kwargs=kwargs):
                _, _, _, blockers, _ = self.project("image.text_to_image", **kwargs)
                self.assertEqual(blockers[0]["code"], "workflow_projector_capability_mismatch")

    def test_config_rejects_unknown_nontext_or_blank_capabilities(self):
        for value in ("", "text.custom", " image.text_to_image", {}, [], True):
            with self.subTest(value=value), self.assertRaises(WorkflowProjectorError):
                validate_workflow_projector_definition(self.projector(value).definition, engine_key="generic.fixed", engine_version=1)
        self.assertTrue(validate_workflow_projector_definition(self.projector("image.text_to_image").definition,
            engine_key="generic.fixed", engine_version=1))

    def test_frozen_capability_must_equal_the_exact_projector_version(self):
        base = self.projector("image.text_to_image")
        previous = WorkflowProjectorVersion.objects.get(pk=base.version_id)
        row = WorkflowProjectorVersion(
            projector=previous.projector, version=previous.version + 1,
            provider_driver_type=base.provider_driver_type, operation=base.operation,
            output_kind=base.output_kind, engine_key=base.engine_key, engine_version=base.engine_version,
            status=WorkflowProjectorVersion.Status.PUBLISHED, is_current=False, published_at=timezone.now(),
        )
        assign_json_contract(row, "definition", base.definition)
        row.save()
        identity = replace(base, version_id=str(row.id), version=row.version, definition_sha256=row.definition_sha256).identity
        frozen = {"contract_projector": identity, "capability": "image.text_to_image",
            "workflow_adapters": base.definition["adapters"], "compiler": {"id": "generic.comfy", "version": 1}}
        self.assertEqual(validate_frozen_workflow_projector_contract(frozen).version_id, str(row.id))
        for capability in (None, "image.image_to_image"):
            with self.subTest(capability=capability), self.assertRaisesRegex(WorkflowProjectorError, "执行能力与 Projector"):
                validate_frozen_workflow_projector_contract({**frozen, "capability": capability})
