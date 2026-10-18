"""Real generic Projector/import/profile/instance and Smoke creation transaction.

Only isolated configuration and PNG input are used; language cases also read
the compiled preview. Never advance/dispatch an attempt, certify resource
readiness, or contact a model/runtime.
"""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from PIL import Image

from studio.models import Asset, ExecutionPreparation, PromptProfileVersion, WorkflowSmokeAttempt
from studio.services.execution_definitions import build_execution_definition, verify_current_execution_definition, verify_stored_execution_definition
from studio.services.execution_inputs import workflow_input_targets
from studio.services.execution_instances import create_execution_draft, create_execution_instance, model_family_for_profile, publish_execution_version
from studio.services.execution_smoke import create_instance_smoke
from studio.services.execution_profile_preparation import compile_preparation_profile
from studio.services.json_contracts import assign_json_contract
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.services.prompt_profiles import PromptProfileError
from studio.services.workflow_versions import import_workflow_version
from studio.tests import test_execution_definitions as base_fixtures
from studio.tests import test_workflow_asset_groups as group_fixtures
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule
from studio.tests.reference_rendering_fixtures import rendering_config, reference_unit_fixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class GenericReferenceSmokeCreationTests(TestCase):
    declaration_primitive = "reference_context.subject_definitions"
    reference_template = None
    retention_template = None
    requires_units = False

    def setUp(self):
        base_fixtures.WorkflowExecutionDefinitionTests.setUp(self)
        group_fixtures.GenericAssetGroupTests.setUp(self)
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        assign_json_contract(self.profile, "specification", {**self.profile.specification,
            "model_family": "generic-reference-fixture", "capabilities": ["image.image_to_image"]})
        self.profile.save()
        # Explicit synthetic node input and reviewed prompt binding; no live
        # Qwen graph or current provider state is represented by this fixture.
        self.graph["3"]["inputs"]["prompt"] = "fixture"
        self.info["MultiReferenceEncoder"]["input"]["required"]["prompt"] = ["STRING", {"default": "fixture"}]
        self.graph["5"] = {"class_type": "ImageScale", "inputs": {"image": ["3", 0], "width": 16,
            "height": 24, "upscale_method": "nearest-exact", "crop": "disabled"}}
        self.graph["4"]["inputs"]["images"] = ["5", 0]
        self.info["ImageScale"] = {"input": {"required": {"image": ["IMAGE"],
            "width": ["INT", {"default": 16, "min": 8, "max": 2048, "step": 8}],
            "height": ["INT", {"default": 24, "min": 8, "max": 2048, "step": 8}],
            "upscale_method": [["nearest-exact"]], "crop": [["disabled"]]}}, "output": ["IMAGE"]}
        self.definition["detector"]["node_counts"]["ImageScale"] = 1
        self.snapshot, _ = group_fixtures.GenericAssetGroupTests.project(self)
        contract = self.snapshot["input_contract"]
        bindings = self.snapshot["semantic_bindings"]
        for binding in bindings:
            if binding["node_id"] == "3" and binding["input_name"] == "prompt":
                contract["parameters"].pop(binding["semantic_key"])
                binding["semantic_key"] = "prompt.positive"
        contract["parameters"]["prompt.positive"] = {"type": "string", "required": True}
        contract["capability"] = "image.image_to_image"
        self.keys = contract["asset_groups"][0]["semantic_keys"]
        config = rendering_config()
        config.pop("quality_advisories")  # Video-only advisory fixture, not an image-profile input.
        config["primitive_options"]["reference_context_ir_v1"]["rendering"]["reference_rules"].append({
            "category": "image", "purpose": "", "task": "image editing", "relationship": "partially_preserved",
            "usage": "Keep the supplied primary image identity."})
        if self.retention_template is not None:
            config["primitive_options"]["reference_context_ir_v1"]["rendering"]["templates"]["retention"] = self.retention_template
        if self.reference_template is not None:
            for key in ("reference", "entity_reference"):
                config["primitive_options"]["reference_context_ir_v1"]["rendering"]["templates"][key] = self.reference_template
        profile = PromptProfileVersion.objects.create(profile_key="group-reference-profile", name="Group reference", version=1,
            model_family=model_family_for_profile(self.profile), operation="image.generate", mode="", review_state="ready",
            contract_schema_version=8, source_projection_key="declarative_v1", source_projection_config=config)
        field = create_field_definition(key="group_reference_declaration", label="References", value_type="long_text")
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector="projection.output",
            projection_primitive=self.declaration_primitive, channel="positive_prompt", position=1,
            required=True, max_chars=7000, generation_mode="passthrough", generation_source_selector="projection.output",
            generation_output_semantics="final", language_policy="preserve_source")
        refresh_prompt_profile_contract(profile)
        self.prompt_profile = publish_prompt_profile(profile, actor=self.user)
        temporary = self.enterContext(TemporaryDirectory())
        self.enterContext(override_settings(MEDIA_ROOT=temporary))
        buffer = BytesIO()
        Image.new("RGB", (16, 24), "blue").save(buffer, format="PNG")
        self.asset = Asset(owner=self.user, kind="reference", name="Primary", checksum=sha256(buffer.getvalue()).hexdigest())
        self.asset.file.save("primary.png", ContentFile(buffer.getvalue()))
        unit = asdict(reference_unit_fixture())
        unit.pop("render_values")
        for key in ("subject_keys", "dialogue", "voice_profiles"):
            unit[key] = []
        self.fixture = {"inputs": {
            "references": {"items": [{"kind": "asset", "item_key": "primary-use", "asset_id": str(self.asset.pk), "purpose": None}]},
            "units": {"kind": "literal", "value": {"schema_version": 1, "ordered_units": [unit],
                "color_script": "", "extra_direction": "", "overall_soundscape": "", "non_diegetic_music": ""}}},
            "parameters": {"image.width": 16, "image.height": 24}, "manual_channels": {}, "subject_facts": [],
            "source_language": "English", "target_language": "English"}
        if not self.requires_units:
            del self.fixture["inputs"]["units"]

    def publish(self):
        self.workflow = import_workflow_version(template=self.workflow.template, graph=self.snapshot["graph"],
            input_contract=self.snapshot["input_contract"], semantic_bindings=self.snapshot["semantic_bindings"],
            model_requirements=self.snapshot["model_requirements"], provider=self.provider, model_profile_version=self.profile)
        self.workflow.status, self.workflow.validated_deployment = "published", self.deployment
        self.workflow.save()
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=["is_active"])
        parent = create_execution_instance(key="group-smoke", name="Group fixture", description="", actor=self.user)
        version = create_execution_draft(instance_id=parent.pk, expected_revision=parent.revision, actor=self.user,
            selection={"deployment": self.deployment, "workflow_version": self.workflow, "operation": "image.image_to_image",
                "mode": "", "prompt_mode": "profile", "prompt_profile_version": self.prompt_profile},
            configuration={"inputs": [{"key": "references", "position": 0, "kind": "image", "required": True,
                "min_items": 1, "max_items": 3, "semantic_contract": {"schema_version": 1, "allowed_purposes": [],
                    "allowed_subject_kinds": [], "allowed_scope_kinds": ["request"], "paired_with_input_key": None, "allow_repeated_asset": False},
                "targets": [{"target_kind": "workflow_slot", "target_key": "references", "is_primary": True}]}] + ([
                {"key": "units", "position": 1, "kind": "json", "required": True, "min_items": 1, "max_items": 1,
                    "semantic_contract": {"schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": [],
                        "allowed_scope_kinds": ["request"], "paired_with_input_key": None, "allow_repeated_asset": False},
                    "targets": [{"target_kind": "profile_input", "target_key": "reference_context.units", "is_primary": False}]}] if self.requires_units else [])})
        definition = build_execution_definition(version.pk)
        self.version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.user)
        return self.version

    def create(self):
        return create_instance_smoke(version_id=self.version.pk, expected_definition_sha256=self.version.contract.sha256,
            fixture=self.fixture, field_values=[], client_idempotency_key="group-profile-smoke", allow_external_calls=False,
            budget_limit=None, actor=self.user)

    def test_published_full_profile_instance_creates_one_preparation_and_replays_without_dispatch(self):
        self.publish()
        before = deepcopy(self.version.contract.definition)
        attempt, prep, created = self.create()
        self.assertTrue(created)
        self.assertEqual(attempt.status, "preparing")
        self.assertIsNone(attempt.submission_started_at)
        self.assertEqual(prep.execution_instance_version_id, self.version.pk)
        reference, = prep.contract.resolved_inputs["profile_sources"]["reference_context"]["value"]["context"]["reference_bindings"]
        self.assertEqual((reference["asset_id"], reference["input_label"], reference["category"]),
            (str(self.asset.pk), "<Picture 1>", "image"))
        self.assertEqual(self.create()[0].pk, attempt.pk)
        self.assertEqual((WorkflowSmokeAttempt.objects.count(), ExecutionPreparation.objects.count()), (1, 1))
        self.version.contract.refresh_from_db()
        self.assertEqual(self.version.contract.definition, before)
        self.assertIn("reference_bindings", str(before))

    def test_used_missing_role_rolls_back_both_attempt_and_preparation(self):
        for member in ("asset_category", "input_slot"):
            self.snapshot["input_contract"]["parameters"][self.keys[0]].pop(member)
        self.publish()
        with self.assertRaisesMessage(ValidationError, "尚未声明明确参考角色"):
            self.create()
        self.assertFalse(WorkflowSmokeAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())


    def test_legacy_published_hash_stays_readable_but_new_admission_requires_successor(self):
        def previous_projection(workflow):
            targets = workflow_input_targets(workflow)
            for target in targets.values():
                target.pop("reference_bindings", None)
            return targets
        with patch("studio.services.execution_inputs.workflow_input_targets", side_effect=previous_projection), \
                patch("studio.services.execution_workflow_contracts.workflow_input_targets", side_effect=previous_projection):
            self.publish()
        contract = self.version.contract
        before = (deepcopy(contract.definition), contract.sha256)
        self.assertEqual(verify_stored_execution_definition(contract).sha256, before[1])
        with self.assertRaisesMessage(ValidationError, "请创建后继版本"):
            verify_current_execution_definition(contract)
        with self.assertRaisesMessage(ValidationError, "请创建后继版本"):
            self.create()
        contract.refresh_from_db()
        self.assertEqual((contract.definition, contract.sha256), before)
        self.assertFalse(WorkflowSmokeAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())


class GenericReferenceLanguageCompilationTests(GenericReferenceSmokeCreationTests):
    """Language contract is unchanged; explicit templates choose prompt content."""
    reference_template = "中文模板正文 {label}: {subject} {description}"
    # Keep these separate from the creation-only cases above.
    test_published_full_profile_instance_creates_one_preparation_and_replays_without_dispatch = None
    test_used_missing_role_rolls_back_both_attempt_and_preparation = None
    test_legacy_published_hash_stays_readable_but_new_admission_requires_successor = None

    def prepare_named_reference(self):
        self.asset.name = "独立主图 · 蓝夹克"
        self.asset.description = "仅用于展示的中文说明"
        self.asset.save(update_fields=["name", "description"])
        self.publish()
        attempt, prep, _ = self.create()
        return attempt, prep

    def test_english_projection_rejects_chinese_template_prose(self):
        attempt, prep = self.prepare_named_reference()
        with self.assertRaises(PromptProfileError) as caught:
            compile_preparation_profile(preparation_id=prep.pk,
                expected_preparation_revision=prep.revision, actor=self.user)
        self.assertEqual(caught.exception.code, "prompt_profile_generation_language_invalid")
        attempt.refresh_from_db()
        self.assertIsNone(attempt.submission_started_at)


class GenericReferenceExplicitDeclarationTests(GenericReferenceLanguageCompilationTests):
    declaration_primitive = "reference_context.retention_analysis"
    retention_template = "Reference image {label}: {relationship}. {usage}"
    test_english_projection_rejects_chinese_template_prose = None

    def test_explicit_reference_declaration_compiles_without_rewriting_asset_facts(self):
        attempt, prep = self.prepare_named_reference()
        before = deepcopy(prep.contract.resolved_inputs)
        preview = compile_preparation_profile(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.user)
        self.assertEqual(preview.resolved_channels["values"]["positive_prompt"],
            "group_reference_declaration: - Reference image <Picture 1>: partially_preserved. Keep the supplied primary image identity.")
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.name, "独立主图 · 蓝夹克")
        self.assertEqual(self.asset.description, "仅用于展示的中文说明")
        prep.contract.refresh_from_db()
        self.assertEqual(prep.contract.resolved_inputs, before)
        binding, = before["profile_sources"]["reference_context"]["value"]["context"]["reference_bindings"]
        self.assertEqual(binding["entity_label"], self.asset.name)
        self.assertEqual(before["profile_sources"]["reference_context"]["value"]["context"]["ordered_units"], [])
        self.assertNotIn("unit_keys", binding)
        self.assertNotIn("reference_context.units", self.version.contract.definition["interface"]["profile_inputs"])
        self.assertNotIn("summary", preview.compilation.structured_fields)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.submission_started_at)

    def test_unused_video_input_is_rejected_instead_of_ignored(self):
        self.publish()
        self.fixture["inputs"]["units"] = {"kind": "literal", "value": {"duration_ms": 1}}
        with self.assertRaises(ValidationError):
            self.create()
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_reference_image_is_still_required(self):
        self.publish()
        self.fixture["inputs"].pop("references")
        with self.assertRaises(ValidationError):
            self.create()
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_no_unit_image_preparation_replays_authorized_root_scopes(self):
        from studio.services.execution_profile_preparation import prepare_execution_profile
        from studio.services.json_contracts import json_contract
        _, prep = self.prepare_named_reference()
        resolved = prep.contract.resolved_inputs
        scope = ("character", str(self.asset.pk))
        scopes = {scope: [prep.unit_key]}
        def prepare(scope_units):
            return prepare_execution_profile(verify_stored_execution_definition(self.version.contract),
                json_contract(resolved["inputs"]), profile=self.prompt_profile, unit_key=prep.unit_key,
                source_language="English", target_language="English", prepared_parameters=resolved["parameters"],
                scope_units=scope_units).value
        first = prepare(scopes)
        self.assertEqual(scopes, {scope: [prep.unit_key]})
        self.assertEqual(first["profile_sources"]["scope_units"],
            [{"kind": scope[0], "id": scope[1], "unit_keys": []}])
        self.assertEqual(first, prepare({scope: []}))


class ImageReferenceShotUsageTests(GenericReferenceSmokeCreationTests):
    declaration_primitive = "reference_context.retention_analysis"
    retention_template = "Reference image {label}: {relationship}, used in shots {shots}. {usage}"
    requires_units = True

    def test_shot_usage_in_selected_template_keeps_explicit_units_required(self):
        self.publish()
        self.assertIn("reference_context.units", self.version.contract.definition["interface"]["profile_inputs"])
        self.fixture["inputs"].pop("units")
        with self.assertRaises(ValidationError):
            self.create()
        self.assertFalse(ExecutionPreparation.objects.exists())

