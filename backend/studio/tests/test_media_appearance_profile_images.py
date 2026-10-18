"""Real isolated PNGs + published Profile + original appearance job/worker.

Only provider transport/admission are test peers. No live Smoke, generated
image result, reference-effect claim or business adoption is fabricated.
"""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import override_settings

from studio.models import AssetGenerationJob, AssetReferenceVersion, ModelDeployment, ModelProfileVersion, ProviderProfile, PromptProfileVersion, ProductionRun
from studio.services.asset_generation import claim_next_comfy_asset_job, process_comfy_asset_job
from studio.services.execution_asset_dispatch import dispatch_asset_execution
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_draft, create_execution_instance, model_family_for_profile, publish_execution_version
from studio.services.json_contracts import json_contract
from studio.services.media_generation_targets import read_media_generation_target
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR as SOURCE
from studio.services.workflow_versions import import_workflow_version, workflow_version_execution_snapshot
from studio.tests import test_media_generation_appearances as appearance_fixtures
from studio.tests import test_execution_reference_preparation as reference_fixtures
from studio.tests import test_reference_context_facts as unit_fixtures
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule
from studio.tests.reference_rendering_fixtures import rendering_config
from studio.tests.test_data_structures import StructureFixture
from studio.tests.test_execution_asset_requests import freeze, isolated_observations
from studio.tests.test_execution_text_contracts import text_specification
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class ProfileImageAppearanceTests(StructureFixture):
    create_job = appearance_fixtures.AppearanceAssetPreparationTests.create_job
    primary_version = appearance_fixtures.AppearanceAssetPreparationTests.primary_version
    http_payload = appearance_fixtures.AppearanceAssetPreparationTests.http_payload
    post_target = appearance_fixtures.AppearanceAssetPreparationTests.post_target
    write = reference_fixtures.ExecutionReferencePreparationTests.write
    input = staticmethod(reference_fixtures.ExecutionReferencePreparationTests.input)
    select = reference_fixtures.ExecutionReferencePreparationTests.select
    preview = reference_fixtures.ExecutionReferencePreparationTests.preview

    def setUp(self):
        appearance_fixtures.AppearanceAssetPreparationTests.setUp(self)
        image_definition = self.definition
        self.write(self.profile, "specification", {**self.profile.specification, "model_family": "image-reference-model"})
        snapshot = workflow_version_execution_snapshot(self.version.workflow_version)
        contract = deepcopy(snapshot["input_contract"])
        for index in range(3):
            contract["parameters"][f"asset.image.source_{index + 1}"]["asset_category"] = "prop"
        for key, constraints in getattr(self, "workflow_parameter_constraints", {}).items():
            contract["parameters"][key].update(deepcopy(constraints))
        self.workflow = import_workflow_version(template=self.version.workflow_version.template,
            graph=snapshot["graph"], input_contract=contract, semantic_bindings=snapshot["semantic_bindings"],
            model_requirements=snapshot["model_requirements"], provider=self.provider, model_profile_version=self.profile)
        self.workflow.status, self.workflow.validated_deployment = "published", self.deployment
        self.workflow.save()
        provider = self.write(ProviderProfile(key="image-field-provider", name="Field", driver_type="openai_compatible", modalities="text"),
            "driver_config", {"base_url": "https://api.example.com", "chat_path": "/v1/chat/completions", "timeout_seconds": 30, "capability_ttl_seconds": 3600})
        model = self.write(ModelProfileVersion(profile_key="image-field-model", version=1, provider=provider,
            model_identifier="test-text-model", modality="text", status="published"), "specification", text_specification())
        deployment = self.write(ModelDeployment(key="image-field-leaf", name="Field", profile_version=model), "runtime_config", {})
        instance = create_execution_instance(key="image-field-leaf", name="Field", description="", actor=self.user)
        leaf = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.user,
            selection={"deployment": deployment, "operation": "prompt.fields.generate", "mode": "", "workflow_version": None,
                "prompt_profile_version": None, "prompt_mode": "none"},
            configuration={"inputs": [self.input("field_request", "field_request", kind="json", target_kind="operation_input")]})
        leaf = publish_execution_version(version_id=leaf.pk, expected_revision=leaf.revision,
            expected_definition_sha256=build_execution_definition(leaf.pk).sha256, actor=self.user)
        structure = self.publish(self.create([self.node("root", "object"),
            self.node("changes", "list", "root", property_name="changes", required=True),
            self.node("change", "object", "changes"),
            self.node("text", "text", "change", property_name="detail.text", required=True)]))
        config = rendering_config()
        config.pop("quality_advisories")
        profile = PromptProfileVersion.objects.create(profile_key="image-reference-profile", name="Image reference", version=1,
            model_family=model_family_for_profile(self.profile), operation="image.generate", mode="", review_state="ready",
            contract_schema_version=8, source_projection_key="declarative_v1", source_projection_config=config, input_structure_version=structure)
        field = create_field_definition(key="unit_description", label="Reference direction", value_type="long_text")
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector="projection.output",
            projection_primitive="reference_context.detailed_description", generation_mode="model_generated",
            generation_output_semantics="contribution", generation_source_selector=SOURCE, source_path=["changes", 0, "detail.text"],
            generation_context_selectors=[], generation_instruction="Use only the given unit facts.",
            generation_required=getattr(self, "contribution_required", True),
            language_policy="en", channel="positive_prompt", position=1,
            required=getattr(self, "contribution_required", True), max_chars=7000)
        field = create_field_definition(key="exact_change", label="Exact change", value_type="long_text")
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector=SOURCE,
            source_path=["changes", 1, "detail.text"], generation_source_selector=SOURCE,
            channel="positive_prompt", position=2, required=True, max_chars=7000)
        field = create_field_definition(key="negative", label="Negative", value_type="long_text")
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector="fixed.value",
            fixed_value="blur", generation_source_selector="fixed.value", channel="negative_prompt",
            position=3, required=True, max_chars=7000)
        refresh_prompt_profile_contract(profile)
        self.prompt_profile = publish_prompt_profile(profile, actor=self.user)
        instance = create_execution_instance(key="image-reference-variant", name="Variant fixture", description="", actor=self.user)
        image_inputs = deepcopy(image_definition.value["configuration"]["inputs"])
        self.version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.user,
            selection={"deployment": self.deployment, "operation": "image.image_to_image", "mode": "",
                "workflow_version": self.workflow, "prompt_profile_version": self.prompt_profile, "prompt_mode": "profile"},
            configuration={"inputs": image_inputs + [self.input("units", "reference_context.units", kind="json", position=3)],
                "auxiliaries": [{"purpose": "generate_fields", "target_version_id": str(leaf.pk)}]})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        unit_fixtures.ReferenceContextFactTests.setUp(self)
        units = [asdict(self.unit), asdict(self.unit)]
        for index, unit in enumerate(units):
            unit["unit_key"] = ("first", "second")[index]
            unit.pop("render_values")
            unit["structured_input"] = {"changes": [{"detail.text": ("Keep the parcel closed.", "Open the red parcel.")[index]}]}
            unit["subject_keys"], unit["dialogue"], unit["voice_profiles"] = [], [], []
        self.inputs["units"] = {"kind": "literal", "value": {"schema_version": 1, "ordered_units": units,
            "color_script": "", "extra_direction": "", "overall_soundscape": "", "non_diegetic_music": ""}}
        self.inputs[SOURCE] = {"kind": "literal", "value": {"changes": [{"detail.text": "Do not select the first field."},
            {"detail.text": "Turn only the red collar outward."}]}}
        self.primary = self.primary_version(asset=self.assets[1])

    def request(self, kind):
        return {**appearance_fixtures.declaration("variant", primary=self.primary),
            "primary_input": {"input_key": "reference_2", "item_key": "image_2"},
            "difference_input": ({"kind": "reference_field", "key": "units", "unit_key": "second", "field_key": "unit_description"}
                if kind == "reference_field" else {"kind": "structured_field", "key": SOURCE, "field_key": "exact_change"})}

    def job(self, kind, key):
        job, created = self.create_job(self.request(kind), key=key, manual_channels={}, source_language="English", target_language="English")
        self.prep = job.execution_preparations.get()
        return job, created

    def test_reference_contribution_shares_image_preparation_and_original_worker(self):
        self.assert_image_preparation_and_original_worker("reference_field")

    def test_structured_path_shares_image_preparation_and_original_worker(self):
        self.assert_image_preparation_and_original_worker("structured_field")

    def test_http_reference_contribution_reaches_original_image_worker(self):
        self.assert_image_preparation_and_original_worker("reference_field", via_http=True)

    def test_http_structured_path_reaches_original_image_worker(self):
        self.assert_image_preparation_and_original_worker("structured_field", via_http=True)

    def assert_image_preparation_and_original_worker(self, kind, *, via_http=False):
        if via_http:
            response = self.post_target(self.request(kind), key=f"http-image-profile-{kind}",
                manual_channels={}, source_language="English", target_language="English")
            self.assertEqual(response.status_code, 202, response.data)
            job = AssetGenerationJob.objects.get(pk=response.data["id"])
            self.prep = job.execution_preparations.get()
            self.assertEqual(self.prep.contract.intent["request"]["requested"]["appearance_target"], self.request(kind))
        else:
            job, created = self.job(kind, f"image-profile-{kind}")
            self.assertTrue(created)
        original = read_media_generation_target(self.prep).value["appearance"]
        expected = "Open the red parcel." if kind == "reference_field" else "Turn only the red collar outward."
        self.assertEqual(original["difference"], {**self.request(kind)["difference_input"], "value_sha256": json_contract(expected).sha256})
        with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
            self.preview()
        self.select("first", "Keep the parcel beside the blue box.")
        self.select("second", "Open the parcel while preserving the supplied shape.")
        preview = self.preview()
        text = preview.resolved_channels["values"]["positive_prompt"]
        self.assertIn("Open the parcel while preserving", text)
        self.assertIn("Turn only the red collar outward.", text)
        self.assertNotIn("Do not select the first field.", text)
        prep = freeze(job, self.user)
        peer, uploads = Mock(), RecordingUploadClient()
        with patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission", side_effect=isolated_observations), \
                patch("studio.services.asset_generation.driver_for", return_value=peer), \
                patch("studio.services.execution_workflow_staging.ExecutionWorkflowUploadClient", return_value=uploads):
            dispatch_asset_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
            peer.invoke.return_value = {"request_id": str(prep.pk)}
            claimed = claim_next_comfy_asset_job(f"image-profile-{kind}")
            self.assertEqual(claimed.pk, job.pk)
            submitted = process_comfy_asset_job(claimed)
        self.assertEqual(submitted.status, "submitted", submitted.error_message)
        graph = peer.invoke.call_args.args[2]["workflow"]
        self.assertEqual(graph["2"]["inputs"]["text"], text)
        self.assertEqual(len(uploads.uploads), 3)
        self.assertEqual(sha256(uploads.files[graph["11"]["inputs"]["image"]]).hexdigest(), self.primary.asset.checksum)
        self.assertNotEqual(graph["11"]["inputs"]["image"], graph["10"]["inputs"]["image"])
        prep.refresh_from_db()
        self.assertEqual(read_media_generation_target(prep).value["appearance"], original)
        self.assertIsNone(submitted.output_asset_id)
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)
        self.assertFalse(ProductionRun.objects.exists())
        self.external.assert_not_called()

    def test_http_reference_unit_identity_is_not_trimmed_or_replaced_by_a_sibling(self):
        # Both are valid, distinct identities in the original reference input.
        self.inputs["units"]["value"]["ordered_units"][0]["unit_key"] = " second "
        target = self.request("reference_field")
        target["difference_input"]["unit_key"] = " second "
        changes = {"manual_channels": {}, "source_language": "English", "target_language": "English"}
        response = self.post_target(target, key="exact-unit", **changes)
        self.assertEqual(response.status_code, 202, response.data)
        job = AssetGenerationJob.objects.get(pk=response.data["id"])
        prep = job.execution_preparations.get()
        self.assertEqual(prep.contract.intent["request"]["requested"]["appearance_target"], target)
        self.assertEqual(read_media_generation_target(prep).value["appearance"]["difference"],
            {**target["difference_input"], "value_sha256": json_contract("Keep the parcel closed.").sha256})
        replay = self.post_target(target, key="exact-unit", **changes)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], response.data["id"])
        conflict = self.post_target(self.request("reference_field"), key="exact-unit", **changes)
        self.assertEqual(conflict.status_code, 409, conflict.data)
        missing = deepcopy(target)
        missing["difference_input"]["unit_key"] = " second  "
        rejected = self.post_target(missing, key="missing-exact-unit", **changes)
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(AssetGenerationJob.objects.count(), 1)
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)
        self.assertFalse(ProductionRun.objects.exists())
        self.external.assert_not_called()

    def test_same_request_replays_without_new_owner_and_different_input_conflicts(self):
        first, _ = self.job("structured_field", "same-structured-image")
        replay, created = self.job("structured_field", "same-structured-image")
        self.assertFalse(created)
        self.assertEqual(first.pk, replay.pk)
        self.inputs[SOURCE]["value"]["changes"][1]["detail.text"] = "Another change."
        with self.assertRaises(ValidationError):
            self.job("structured_field", "same-structured-image")
        self.assertEqual(AssetGenerationJob.objects.count(), 1)

    def test_wrong_primary_or_unknown_field_rolls_back_without_creating_a_root(self):
        for kind, changes in (("reference_field", {"primary_input": {"input_key": "reference_1", "item_key": "image_1"}}),
                ("structured_field", {"difference_input": {"kind": "structured_field", "key": SOURCE, "field_key": "unknown"}})):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                self.create_job({**self.request(kind), **changes}, key=kind, manual_channels={})
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.external.assert_not_called()
