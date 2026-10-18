"""Read-only authoring metadata, never an alternative preparation or gate."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import AssetGenerationJob, ExecutionPreparation, ProductionRun
from studio.services.execution_definitions import read_execution_input_contract
from studio.services.media_appearance_input_catalog import media_appearance_input_options
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR as SOURCE
from studio.tests import test_media_generation_appearances as fixtures
from studio.tests import test_media_appearance_profile_images as profile_fixtures
from studio.tests.test_data_structures import StructureFixture


class AppearanceInputProjectionTests(SimpleTestCase):
    def setUp(self):
        self.definition = {"configuration": {"inputs": []}, "interface": {
            "prompt": {"mode": "profile", "channels": {}}, "input_targets": {}, "profile_inputs": {}}}

    def input(self, key, kind, fields, **target):
        self.definition["configuration"]["inputs"].append({"key": key, "kind": kind,
            "position": len(self.definition["configuration"]["inputs"]),
            "targets": [{"target_kind": "profile_input", "target_key": key, "is_primary": False}]})
        self.definition["interface"]["profile_inputs"][key] = {"fields": fields, **target}

    def test_no_workflow_or_prompt_does_not_infer_any_capability(self):
        self.definition["interface"]["prompt"] = {"mode": "none", "channels": {}}
        self.assertEqual(media_appearance_input_options(self.definition), {"primary_inputs": [], "difference_sources": []})

    def test_text_assembly_and_generation_deduplicate_but_context_is_excluded(self):
        self.input("brief", "text", [{"usage": use} for use in ("assembly_source", "generation_source", "generation_context")])
        self.input("context", "text", [{"usage": "generation_context"}])
        self.assertEqual(media_appearance_input_options(self.definition)["difference_sources"], [{"kind": "logical_input", "key": "brief"}])

    def test_structured_catalog_uses_published_path_types_without_defaults_or_text(self):
        fields = [{"field_key": key, "usage": "assembly_source", "selector": SOURCE, "source_path": ["items", 1, key]}
            for key in ("detail.text", "flag", "count")]
        self.input(SOURCE, "json", fields, value_schema={"type": "object", "properties": {"items": {"type": "array", "items": {
            "type": "object", "properties": {"detail.text": {"anyOf": [{"type": "string", "default": "not actual text"}, {"type": "null"}]},
                "flag": {"type": "boolean"}, "count": {"type": "integer"}}}}}})
        original = deepcopy(self.definition)
        self.assertEqual(media_appearance_input_options(self.definition)["difference_sources"],
            [{"kind": "structured_field", "key": SOURCE, "field_key": "detail.text"}])
        self.assertEqual(self.definition, original)

    def test_invalid_published_path_fails_instead_of_skipping_the_field(self):
        self.input(SOURCE, "json", [{"field_key": "text", "usage": "assembly_source", "selector": SOURCE,
            "source_path": ["missing"]}], value_schema={"type": "object", "properties": {}})
        with self.assertRaises(ValidationError):
            media_appearance_input_options(self.definition)

    def test_reference_fields_do_not_invent_unit_identity_or_select_auxiliary_context(self):
        self.input("units", "json", [{"field_key": "direction", "usage": "reference_units"}] * 2)
        self.input("unit_context", "json", [{"field_key": "direction", "usage": "reference_generation_context"}])
        self.assertEqual(media_appearance_input_options(self.definition)["difference_sources"],
            [{"kind": "reference_field", "key": "units", "field_key": "direction"}])


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class AppearanceInputContractTests(TestCase):
    def setUp(self):
        fixtures.AppearanceAssetPreparationTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_actual_manual_contract_projects_only_primary_image_wiring_without_business_writes(self):
        with self.assertNumQueries(0):
            expected = media_appearance_input_options(self.definition.value)
        self.assertEqual(expected["primary_inputs"], [{"input_key": f"reference_{index}", "target_key": f"asset.image.source_{index}"} for index in range(1, 4)])
        self.assertEqual(expected["difference_sources"], [{"kind": "manual_channel", "key": key} for key in ("negative_prompt", "positive_prompt")])
        result = read_execution_input_contract(self.version.pk)
        self.assertEqual(result["appearance_inputs"], expected)
        self.assertEqual(result["sha256"], self.definition.sha256)
        self.assertEqual(result["definition"], self.definition.value)
        response = self.client.get(f"/api/v1/model-execution-instance-versions/{self.version.pk}/input-contract/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["appearance_inputs"], expected)
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ProductionRun.objects.exists())
        self.external.assert_not_called()

    def test_secondary_and_context_only_images_are_not_primary_ports(self):
        definition = deepcopy(self.definition.value)
        rows = definition["configuration"]["inputs"]
        rows[0]["targets"][0]["is_primary"] = False
        rows[1]["targets"] = []
        rows[2]["kind"] = "video"
        self.assertEqual(media_appearance_input_options(definition)["primary_inputs"], [])


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class ProfileAppearanceInputContractTests(StructureFixture):
    setUp = profile_fixtures.ProfileImageAppearanceTests.setUp
    write = profile_fixtures.ProfileImageAppearanceTests.write
    input = staticmethod(profile_fixtures.ProfileImageAppearanceTests.input)
    primary_version = profile_fixtures.ProfileImageAppearanceTests.primary_version

    def test_published_profile_keeps_exact_field_sources_and_no_body_or_paths(self):
        result = read_execution_input_contract(self.version.pk)
        self.assertEqual(result["appearance_inputs"]["difference_sources"], [
            {"kind": "reference_field", "key": "units", "field_key": "unit_description"},
            {"kind": "structured_field", "key": SOURCE, "field_key": "exact_change"}])
        self.assertEqual(result["sha256"], self.definition.sha256)
        with self.assertNumQueries(0):
            self.assertEqual(result["appearance_inputs"], media_appearance_input_options(result["definition"]))
        self.external.assert_not_called()
