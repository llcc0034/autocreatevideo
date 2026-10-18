"""M6 source boundary through real Asset resolution and auxiliary wire payloads.

All data is isolated. The text interface below is an explicit protocol fixture;
no model, Smoke, provider credential or current business database is accessed.
"""
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.services.execution_reference_fields import prepare_reference_profile_fields
from studio.services.execution_text_contracts import build_execution_text_payload
from studio.services.json_contracts import JsonContract
from studio.services.prompt_profiles import refresh_prompt_profile_contract
from studio.services.reference_context import compile_reference_context
from studio.services.reference_context_facts import ReferenceSubjectFacts
from studio.tests import test_execution_reference_materials as material_fixtures
from studio.tests import test_profile_media_source_policy as media_profile_fixtures
from studio.tests import test_prompt_profiles as profile_fixtures
from studio.tests.reference_rendering_fixtures import generation_structure_fixture, rendering_config


class VideoReferenceProjectionTests(TestCase):
    setUpTestData = classmethod(material_fixtures.ExecutionReferenceMaterialTests.setUpTestData.__func__)
    definition = material_fixtures.ExecutionReferenceMaterialTests.definition
    resolve = material_fixtures.ExecutionReferenceMaterialTests.resolve
    item = material_fixtures.ExecutionReferenceMaterialTests.item
    project = material_fixtures.ExecutionReferenceMaterialTests.project
    media_protocol_fixture = material_fixtures.ExecutionReferenceMaterialTests.media_protocol_fixture
    _profile = profile_fixtures.PromptProfileServiceTests._profile
    _field = profile_fixtures.PromptProfileServiceTests._field
    _rule = profile_fixtures.PromptProfileServiceTests._rule
    _publish = profile_fixtures.PromptProfileServiceTests._publish

    def setUp(self):
        material_fixtures.ExecutionReferenceMaterialTests.setUp(self)
        self.actor = self.owner
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential"):
            self.enterContext(patch(target, side_effect=AssertionError("Projection cannot call a model")))
        self.asset.description = "ASSET_APPEARANCE_SENTINEL"
        self.asset.prompt_hint = "IMAGE_PROMPT_SENTINEL"
        self.asset.save(update_fields=("description", "prompt_hint"))

    def subject_input(self):
        identity = str(uuid4())
        return self.resolve([self.item(subject_ref={"kind": "external", "id": identity})], subject_facts=[{
            "id": identity, "category": "character", "name": "林岚 / Lin Lan",
            "description": "BIBLE_APPEARANCE_SENTINEL",
        }])

    def profile(self, prompt_type, mode):
        key = f"m6.{prompt_type}.{mode}"
        profile = self._profile(key=key, operation=f"{prompt_type}.generate", mode="explicit-mode",
            source_projection_key="declarative_v1")
        profile.source_projection_config = rendering_config()
        profile.source_projection_config.pop("quality_advisories")
        profile.save()
        generation_structure_fixture(profile)
        rule = self._rule(profile, self._field(key.replace(".", "_")), selector="projection.output")
        rule.projection_primitive = "reference_context.detailed_description"
        rule.generation_mode = mode
        rule.generation_output_semantics = "contribution"
        rule.generation_source_selector = "request.structured_input"
        rule.source_path = ["direction"]
        rule.generation_context_selectors = ["target_shot.core"]
        rule.generation_instruction = "Use only this unit's declared dynamic facts and exact references."
        rule.language_policy = "inherit"
        rule.save()
        refresh_prompt_profile_contract(profile)
        return self._publish(profile)

    def test_video_and_image_use_same_exact_identity_but_separate_appearance_sources(self):
        resolved = self.subject_input()
        before = deepcopy(resolved)
        image = self.project(resolved, prompt_type="image")
        video = self.project(resolved, prompt_type="video")
        for marker in (self.asset.description, self.asset.prompt_hint, "BIBLE_APPEARANCE_SENTINEL"):
            self.assertIn(marker, str(image))
            self.assertNotIn(marker, str(video))
        self.assertEqual(video["subjects"][0]["description_parts"], ["林岚 / Lin Lan"])
        self.assertEqual(video["references"], [{key: value for key, value in image["references"][0].items()
            if key not in {"description", "prompt_hint"}}])
        self.assertEqual(resolved, before)
        self.assertEqual(self.project(resolved, prompt_type="video"), video)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.prompt_hint, "IMAGE_PROMPT_SENTINEL")

    def test_auxiliary_wire_payload_excludes_implicit_appearance_without_editing_explicit_prose(self):
        resolved = self.subject_input()
        before = deepcopy(resolved)
        source = "穿蓝色外套的林岚转身。Keep my explicit appearance sentence."
        dialogue = [{"speaker_id": "S1", "text": "别走，等一下！", "start_ms": 125, "end_ms": 900}]
        dynamic = {"action": "turn", "camera": "follow", "dialogue": dialogue}
        for prompt_type in ("video", "image"):
            for mode, operation in (("model_generated", "prompt.fields.generate"), ("translate_only", "prompt.fields.translate")):
                with self.subTest(prompt_type=prompt_type, mode=mode):
                    projected = self.project(resolved, prompt_type=prompt_type)
                    subjects = tuple(ReferenceSubjectFacts(**{**row, "description_parts": tuple(row["description_parts"])})
                        for row in projected["subjects"])
                    identity = (subjects[0].category, subjects[0].identity)
                    units = tuple(replace(self.unit, unit_key=key, subject_keys=(identity,),
                        render_values={**self.unit.render_values, "description": source}) for key in ("first", "second"))
                    context = replace(self.context, prompt_type=prompt_type, language="Chinese", ordered_units=units,
                        subject_facts=subjects, reference_bindings=tuple(projected["references"]),
                        frame_policy={"fps": 24, "minimum_frames": 1, "maximum_frames": 240,
                            "alignment_modulus": 1, "alignment_remainder": 0} if prompt_type == "video" else None)
                    prepared = prepare_reference_profile_fields(profile=self.profile(prompt_type, mode), reference_context=context,
                        generation_contexts={key: {"target_shot.core": dynamic} for key in ("first", "second")},
                        structured_inputs={key: {"direction": source} for key in ("first", "second")},
                        source_language="Chinese", target_language="Chinese").value
                    batch = JsonContract(**prepared["field_requests"][0])
                    interface = {"operation": operation, "output": {"adapter": "text.prompt_fields/v1"},
                        "parameters": {}, "prompt": {"mode": "none", "channels": {}}, "chat_request_options": {}}
                    payload = build_execution_text_payload(interface, parameters={}, channels={}, field_request=batch)
                    wire = json.loads(payload["messages"][1]["content"])
                    self.assertEqual(wire, batch.value)
                    for row in wire["items"]:
                        self.assertEqual(row["source_text"], source)
                        self.assertEqual(row["context_facts"]["target_shot.core"], dynamic)
                        self.assertEqual(row["reference_facts"]["logical_inputs"][0]["input_label"], "<Picture 1>")
                        self.assertEqual(row["reference_facts"]["logical_inputs"][0]["entity_label"], "林岚 / Lin Lan")
                    rendered = compile_reference_context(context, self.policy)
                    self.assertIn(source, str(rendered))
                    for marker in (self.asset.description, self.asset.prompt_hint, "BIBLE_APPEARANCE_SENTINEL"):
                        if prompt_type == "video":
                            self.assertNotIn(marker, str(payload))
                            self.assertNotIn(marker, str(prepared))
                            self.assertNotIn(marker, str(rendered))
                        else:
                            self.assertIn(marker, str(payload))
                    if prompt_type == "image":
                        self.assertIn("BIBLE_APPEARANCE_SENTINEL", str(rendered))
                        self.assertIn("IMAGE_PROMPT_SENTINEL", str(rendered))
        self.assertEqual(resolved, before)
        self.assertEqual(dialogue[0]["text"], "别走，等一下！")

    def test_video_paired_and_standalone_media_do_not_reintroduce_metadata(self):
        definition, resolved = self.media_protocol_fixture()
        before = deepcopy(resolved)
        projected = self.project(resolved, definition=definition, prompt_type="video")
        for key in ("description", "prompt_hint", "audio_description", "audio_prompt_hint"):
            self.assertNotIn(key, str(projected))
        image, video, audio = projected["references"]
        self.assertEqual(image["artifact_id"], resolved["logical_values"]["images"][0]["artifact_id"])
        self.assertEqual((video["input_label"], video["audio_input_label"], audio["input_label"]),
            ("<Video 1>", "<Audio 1>", "<Audio 2>"))
        self.assertEqual(video["audio_use"]["source_sha256"], resolved["logical_values"]["video_audios"][0]["source_sha256"])
        self.assertEqual(resolved, before)

    def test_unknown_output_type_cannot_silently_use_image_projection(self):
        for value in (None, "", "audio", [], {}):
            with self.subTest(value=value), self.assertRaisesMessage(ValidationError, "输出类型"):
                self.project(prompt_type=value)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class VideoAuxiliaryMediaPreparationTests(TestCase):
    """Published root + PNG uses + published field leaf -> frozen transport.

    Admission observations are isolated fixtures. No dispatch, provider result,
    field approval or live Smoke is performed or represented as real evidence.
    """
    _create_version = media_profile_fixtures.ProfileMediaSourcePolicyTests._create_version
    selection = media_profile_fixtures.ProfileMediaSourcePolicyTests.selection

    def configure_generation(self, profile):
        from studio.models import ModelDeployment, ModelProfileVersion, ProviderProfile, PromptProfileFieldRule
        from studio.services.execution_definitions import build_execution_definition
        from studio.services.execution_instances import create_execution_draft, create_execution_instance, publish_execution_version, update_execution_draft
        from studio.services.json_contracts import assign_json_contract
        from studio.tests.test_execution_auxiliary_definitions import ExecutionAuxiliaryDefinitionTests
        from studio.tests.test_execution_text_contracts import text_specification

        rule = PromptProfileFieldRule.objects.get(profile_version=profile)
        rule.generation_mode = "model_generated"
        rule.generation_output_semantics = "contribution"
        rule.generation_source_selector = "request.structured_input"
        rule.source_path = ["direction"]
        rule.generation_instruction = "Use the explicit dynamic facts and reference identities."
        rule.generation_required = True
        rule.language_policy = "inherit"
        rule.save()
        provider = ProviderProfile(key="m6-wire-provider", name="Isolated text", driver_type="openai_compatible", modalities="text")
        assign_json_contract(provider, "driver_config", {"base_url": "https://api.example.com",
            "chat_path": "/v1/chat/completions", "timeout_seconds": 30, "capability_ttl_seconds": 3600})
        provider.save()
        model = ModelProfileVersion(profile_key="m6-wire-model", version=1, provider=provider,
            model_identifier="declared-text-model", modality="text", status="published")
        assign_json_contract(model, "specification", text_specification())
        model.save()
        deployment = ModelDeployment(key="m6-wire-leaf", name="Isolated field leaf", profile_version=model)
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        instance = create_execution_instance(key="m6-wire-leaf", name="Isolated field leaf", description="", actor=self.user)
        leaf = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.user,
            selection={"deployment": deployment, "operation": "prompt.fields.generate", "mode": "",
                "workflow_version": None, "prompt_profile_version": None, "prompt_mode": "none"})
        leaf = update_execution_draft(version_id=leaf.pk, expected_revision=leaf.revision, actor=self.user,
            changes={"inputs": [ExecutionAuxiliaryDefinitionTests.input("field_request", "field_request",
                kind="json", target_kind="operation_input")]})
        self.leaf = publish_execution_version(version_id=leaf.pk, expected_revision=leaf.revision,
            expected_definition_sha256=build_execution_definition(leaf.pk).sha256, actor=self.user)
        return [{"purpose": "generate_fields", "target_version_id": str(self.leaf.pk)}]

    def setUp(self):
        media_profile_fixtures.ProfileMediaSourcePolicyTests.setUp(self, configure_profile=self.configure_generation)
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=("is_active",))
        self.enterContext(patch("studio.services.provider_registry.resolve_credential",
            side_effect=AssertionError("No provider credentials in this isolated preparation")))
        self.asset.description = "SAVED_ASSET_APPEARANCE_MUST_NOT_LEAK"
        self.asset.prompt_hint = "SAVED_IMAGE_PROMPT_MUST_NOT_LEAK"
        self.asset.save(update_fields=("description", "prompt_hint"))

    def test_original_child_freezes_exact_projected_request_without_reloading_live_asset_metadata(self):
        from studio.services.asset_generation import create_asset_generation_job
        from studio.services.execution_asset_auxiliaries import create_asset_field_child
        from studio.services.execution_preparations import read_frozen_execution_request
        from studio.services.execution_preparation_requests import compile_prepared_execution_transport
        from studio.tests.test_execution_asset_requests import freeze

        source = "The courier in a blue coat turns toward the parcel. Keep this explicit appearance sentence."
        self.sources["units"]["value"]["ordered_units"][0]["structured_input"]["direction"] = source
        root, created = create_asset_generation_job(owner=self.user, execution_instance_version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, job_type="asset", media_type="video", field_values=[],
            name="Isolated M6 reference request", client_idempotency_key="m6-reference-wire",
            allow_external_calls=True, budget_limit=None, inputs=self.sources, parameters=self.parameter_values,
            manual_channels={}, subject_facts=[], source_language="English", target_language="English")
        self.assertTrue(created)
        parent = root.execution_preparations.get()
        facts = deepcopy(parent.contract.resolved_inputs)
        batch = facts["field_requests"][0]
        self.assertIn(self.asset.prompt_hint, str(facts["inputs"]))
        self.assertNotIn(self.asset.prompt_hint, str(facts["profile_sources"]))
        child, created = create_asset_field_child(preparation_id=parent.pk, expected_preparation_revision=parent.revision,
            field_batch_key=batch["sha256"], attempt_number=1, actor=self.user)
        self.assertTrue(created)
        prep = child.execution_preparations.get()
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.user).transport.value
        freeze(child, self.user)  # Isolated admission only; no worker/dispatch.
        prep.refresh_from_db()
        _, _, frozen = read_frozen_execution_request(prep)
        self.assertEqual(frozen.value, preview)
        wire = json.loads(preview["payload"]["messages"][1]["content"])
        self.assertEqual(wire, batch["value"])
        self.assertEqual(preview["field_request_sha256"], batch["sha256"])
        self.assertEqual(wire["items"][0]["source_text"], source)
        logical = wire["items"][0]["reference_facts"]["logical_inputs"]
        self.assertEqual([row["input_label"] for row in logical], ["<Picture 1>", "<Picture 2>"])
        # M6-02: two uses of one Asset must retain their separate names/ports
        # in the saved parent facts and the exact child wire request.
        self.assertEqual([row["item_key"] for row in logical], ["second", "first"])
        self.assertEqual([row["original_name"] for row in logical], [self.asset.name] * 2)
        self.assertEqual([row["entity_label"] for row in logical],
                         [f"{self.asset.name} [second]", f"{self.asset.name} [first]"])
        self.assertTrue(all(row["name_disambiguated"] for row in logical))
        frozen_context = facts["profile_sources"]["reference_context"]["value"]["context"]
        self.assertEqual(frozen_context["reference_bindings"], logical)
        for marker in (self.asset.description, self.asset.prompt_hint):
            self.assertNotIn(marker, str(frozen.value))
        before = deepcopy(frozen.value)
        self.asset.name = "Later unrelated Asset name"
        self.asset.prompt_hint = "Later unrelated Asset edit must not change frozen requests"
        self.asset.save(update_fields=("name", "prompt_hint"))
        self.assertEqual(read_frozen_execution_request(prep)[2].value, before)
        parent.refresh_from_db()
        self.assertEqual(parent.contract.resolved_inputs, facts)
        self.assertFalse(parent.field_values.exists())
        self.assertFalse(parent.field_selections.exists())
        self.assertIsNone(parent.request_frozen_at)
        child.refresh_from_db()
        self.assertIsNone(child.submission_started_at)
        self.assertEqual(child.result_text, "")
        self.assertEqual(child.execution_instance_version_id, self.leaf.pk)
