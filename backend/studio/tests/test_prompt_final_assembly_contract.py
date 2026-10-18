"""M6-04: real preparation -> public prompt preview -> frozen request.

External admission observations are substituted; compiler/selector/request
writer services are real. All model/Workflow rows and media are isolated test
fixtures, no queue is dispatched, and the 269 generation suite is not run.
"""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    AssetGenerationJob, ExecutionFieldValue, ExecutionPreparationContract,
    GenerationAttempt, ModelDeploymentSmokeAttempt, PromptProfileVersion,
    WorkflowSmokeAttempt,
)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_instances import model_family_for_profile, publish_execution_version, update_execution_draft
from studio.services.execution_preparation_fields import create_manual_execution_field, confirm_execution_field
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_preparations import create_execution_instance_preparation, read_frozen_execution_request
from studio.services.execution_profile_preparation import compile_preparation_profile
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.tests import test_execution_definitions as workflow_fixtures
from studio.tests import test_profile_media_source_policy as media_fixtures
from studio.tests import test_video_reference_projection as generation_fixtures
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule
from studio.tests.test_execution_asset_requests import isolated_observations


class AssemblyAssertions:
    def isolate(self):
        self.assertEqual(settings.DATABASES["default"]["ENGINE"], "django.db.backends.sqlite3")
        self.assertIn("memory", str(settings.DATABASES["default"]["NAME"]))
        root = self.enterContext(TemporaryDirectory(prefix="prompt-final-assembly-"))
        self.enterContext(override_settings(MEDIA_ROOT=root))
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "studio.services.provider_registry.resolve_credential"):
            blocked = self.enterContext(patch(target, side_effect=AssertionError("No network or credentials in M6 assembly")))
            self.addCleanup(blocked.assert_not_called)

    def revision(self):
        self.prep.refresh_from_db()
        return self.prep.revision

    def display_response(self, revision=None):
        return self.client.get(f"/api/v1/execution-preparations/{self.prep.pk}/prompt-display/", {
            "expected_preparation_revision": self.revision() if revision is None else revision,
        })

    def display(self):
        response = self.display_response()
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()

    def preview(self):
        return compile_preparation_profile(preparation_id=self.prep.pk,
            expected_preparation_revision=self.revision(), actor=self.user)

    def transport(self):
        return compile_prepared_execution_transport(preparation_id=self.prep.pk,
            expected_preparation_revision=self.revision(), actor=self.user)

    def finalize(self, collector=isolated_observations):
        self.prep = finalize_execution(preparation_id=self.prep.pk,
            expected_preparation_revision=self.revision(), actor=self.user, collect_admission_facts=collector)
        return self.prep

    def assert_no_dispatch(self):
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        self.assertFalse(WorkflowSmokeAttempt.objects.exists())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "preparing")

    def assert_same_frozen_assembly(self):
        before = self.display()
        self.assertEqual(before["mode"], "preview")
        self.assertTrue(all(row["origin"] == "adopted" for row in before["field_basis"]))
        compilation = self.preview()
        channels = compilation.resolved_channels["values"]
        self.assertEqual({row["key"]: row["text"] for row in before["channels"]}, channels)
        request = self.transport()
        self.assertEqual(request.transport.value["prompt_channels"], compilation.resolved_channels)
        self.assertEqual(request.transport.value["definition_sha256"], self.definition.sha256)
        prompt = self.definition.value["interface"]["prompt"]
        self.assertEqual(prompt["profile_id"], str(self.prompt_profile.pk))
        self.assertEqual(prompt["profile_sha256"], self.prompt_profile.contract_sha256)
        self.finalize()
        self.assertEqual(self.prep.status, "ready", self.prep.reason)
        detail, _, frozen = read_frozen_execution_request(self.prep)
        self.assertEqual(frozen, request.transport)
        self.assertEqual(detail.prepared_request["field_selections"], request.field_selections.value)
        root = Path(settings.MEDIA_ROOT).resolve()
        self.assertTrue((root / detail.prepared_request_key).resolve().is_relative_to(root))
        self.assertTrue((root / detail.prepared_request_key).is_file())
        with patch("studio.services.execution_prompt_display._compile_preparation_profile",
                   side_effect=AssertionError("Frozen display must not reassemble")):
            after = self.display()
        self.assertEqual(after["mode"], "frozen")
        self.assertEqual(after["channels"], before["channels"])
        self.assertEqual(after["field_basis"], before["field_basis"])
        self.assertEqual(after["length_diagnostics"], before["length_diagnostics"])
        self.assertEqual(after["profile_basis"], before["profile_basis"])
        self.assertEqual(after["profile_basis"]["version_id"], str(self.prompt_profile.pk))
        self.assertEqual(after["request_sha256"], self.prep.request_sha256)
        self.assert_no_dispatch()
        return channels, frozen.value


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class ReferenceFinalAssemblyTests(AssemblyAssertions, TestCase):
    _create_version = media_fixtures.ProfileMediaSourcePolicyTests._create_version
    selection = media_fixtures.ProfileMediaSourcePolicyTests.selection
    resolve = media_fixtures.ProfileMediaSourcePolicyTests.resolve
    prepare = media_fixtures.ProfileMediaSourcePolicyTests.prepare

    def configure(self, profile):
        auxiliaries = generation_fixtures.VideoAuxiliaryMediaPreparationTests.configure_generation(self, profile)
        rule = profile.field_rules.get()
        field = create_field_definition(key="custom_final_motion", label="DYNAMIC_TITLE_NOT_CONFIGURED", value_type="long_text")
        rule.field_definition, rule.field_definition_version = field, field._fixture_version
        rule.label_style, rule.position = "none", 2
        rule.save()
        rendering = profile.source_projection_config["primitive_options"]["reference_context_ir_v1"]["rendering"]
        rendering["list_prefix"] = ""
        rendering["templates"].update({
            "entity_reference": "{subject} = {label}",
            "reference": "{subject} = {label}",
            "reviewed_shot": "{shot_marker} {description}\n{reference_usage}\n{dialogue}\n{voice_profiles}",
            "sound": "{description}", "music": "{description}",
            "style": "", "empty_references": "", "empty_retention": "", "silent_sound": "", "silent_music": "",
            "retention": "RETENTION_NOT_SELECTED {label} {relationship}",
        })
        profile.save(update_fields=("source_projection_config",))
        for key, primitive, position, label, required in (
            ("custom_name_mapping", "reference_context.subject_definitions", 1, "NAMES_TITLE_NOT_CONFIGURED", True),
            ("custom_original_sound", "reference_context.overall_soundscape", 3, "SOUND_TITLE_NOT_CONFIGURED", True),
            ("custom_optional_music", "reference_context.non_diegetic_music", 4, "EMPTY_MUSIC_TITLE", False),
        ):
            field = create_field_definition(key=key, label=label, value_type="long_text")
            create_profile_field_rule(profile_version=profile, field_definition=field,
                source_selector="projection.output", projection_primitive=primitive,
                generation_source_selector="projection.output", generation_mode="passthrough",
                generation_output_semantics="final", language_policy="any", channel="positive_prompt",
                position=position, label_style="none" if required else "label_colon",
                required=required, omit_if_empty=True, max_chars=7000)
        return auxiliaries

    def setUp(self):
        self.isolate()
        media_fixtures.ProfileMediaSourcePolicyTests.setUp(self, configure_profile=self.configure)
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=("is_active",))
        self.asset.name = "同名角色 / Alex"
        self.asset.description = "ASSET_APPEARANCE_MUST_NOT_LEAK"
        self.asset.prompt_hint = "IMAGE_PROMPT_MUST_NOT_LEAK"
        self.asset.save(update_fields=("name", "description", "prompt_hint"))
        self.dialogue = '别走！ "Wait—停下。"\n第二句，保留标点。'
        self.voice = "原声：轻声、沙哑，不译写。"
        self.sound = "原声音效：咔嗒—沙沙；不要改成音乐。"
        self.unit = self.sources["units"]["value"]["ordered_units"][0]
        self.unit["dialogue"] = [{"speaker_id": "speaker-original", "subject": "Original speaker",
            "text": self.dialogue, "start_ms": 100, "end_ms": 1800, "voiceover": False,
            "delivery": "保留轻声", "voice_profile": "原声，不译写"}]
        self.unit["voice_profiles"] = [self.voice]
        self.unit["ambient_sound"] = self.sound
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def candidate(self, text="The courier in an explicitly authored blue coat turns toward the parcel.", *, revision=1, adopt=True):
        row = create_manual_execution_field(preparation_id=self.prep.pk, unit_key="composition",
            field_key="custom_final_motion", text=text, revision=revision,
            expected_preparation_revision=self.revision(), actor=self.user)
        if adopt:
            confirm_execution_field(preparation_id=self.prep.pk, field_value_id=row.pk,
                expected_text_sha256=row.text_sha256, expected_source_sha256=row.source_sha256,
                expected_preparation_revision=self.revision(), select_for_execution=True,
                expected_selection_revision=None, replace=False, actor=self.user)
        return row

    def references(self):
        self.prep.refresh_from_db()
        return self.prep.contract.resolved_inputs["profile_sources"]["reference_context"]["value"]["context"]["reference_bindings"]

    def test_names_dynamic_body_original_dialogue_and_sound_match_preview_and_frozen_request(self):
        self.prepare()
        selected = self.candidate()
        self.candidate("New unadopted text must not replace the selected wording.", revision=2, adopt=False)
        references = deepcopy(self.references())
        self.asset.name = "Later asset name must not rewrite this request"
        self.asset.save(update_fields=("name",))
        channels, frozen = self.assert_same_frozen_assembly()
        text = channels["positive_prompt"]
        self.assertEqual(text.count(selected.text), 1)
        for literal in (self.dialogue, self.voice, self.sound):
            self.assertEqual(text.count(literal), 1)
        for row in references:
            self.assertTrue(row["name_disambiguated"])
            self.assertEqual(text.count(f"{row['entity_label']} = {row['input_label']}"), 1)
            self.assertEqual(text.count(row["entity_label"]), 1)
        for forbidden in ("ASSET_APPEARANCE_MUST_NOT_LEAK", "IMAGE_PROMPT_MUST_NOT_LEAK", "RETENTION_NOT_SELECTED",
                          "DYNAMIC_TITLE_NOT_CONFIGURED", "NAMES_TITLE_NOT_CONFIGURED", "SOUND_TITLE_NOT_CONFIGURED",
                          "EMPTY_MUSIC_TITLE", "New unadopted", self.asset.name):
            self.assertNotIn(forbidden, text)
        self.assertEqual(frozen["payload"]["compiled_workflow"]["graph"]["6"]["inputs"]["prompt"], text)
        self.assertEqual(self.display()["sources"]["dialogue"][0]["text"], self.dialogue)

    def test_reordered_same_name_uses_keep_stable_names_and_actual_graph_slots(self):
        results = []
        for reverse in (False, True):
            if reverse:
                self.sources["references"]["selection"]["items"].reverse()
                self.job = AssetGenerationJob.objects.create(owner=self.user, model_deployment=self.deployment,
                    execution_instance_version=self.version, workflow_template_version=self.workflow,
                    execution_kind="comfy_workflow", job_type="asset", media_type="video", operation=self.version.operation,
                    prompt="", idempotency_key=f"reordered-{uuid4().hex}", request_snapshot_sha256=json_contract({}).sha256)
            self.prepare()
            self.candidate()
            references = deepcopy(self.references())
            channels, frozen = self.assert_same_frozen_assembly()
            bindings = frozen["payload"]["media_preparation"]["bindings"]
            graph = frozen["payload"]["compiled_workflow"]["graph"]
            self.assertEqual([row["item_key"] for row in bindings], [row["item_key"] for row in references])
            for index, (binding, reference) in enumerate(zip(bindings, references, strict=True)):
                self.assertEqual(reference["input_slot"], index + 1)
                self.assertEqual(reference["input_label"], f"<Picture {index + 1}>")
                self.assertEqual(graph[f"cr_images_{index}"]["inputs"]["image"], binding["token"])
                self.assertEqual(binding["asset_id"], reference["asset_id"])
                self.assertIn(f"{reference['entity_label']} = {reference['input_label']}", channels["positive_prompt"])
            results.append({row["item_key"]: row for row in references})
        self.assertEqual(set(results[0]), set(results[1]))
        for key, first in results[0].items():
            self.assertEqual(first["entity_label"], results[1][key]["entity_label"])
            self.assertEqual(first["asset_id"], results[1][key]["asset_id"])
            self.assertNotEqual(first["input_slot"], results[1][key]["input_slot"])

    def test_unadopted_preview_is_labelled_and_cannot_finalize_or_accept_stale_revision(self):
        self.prepare()
        self.assertEqual(self.display()["mode"], "unavailable")
        value = self.candidate(adopt=False)
        preview = self.display()
        self.assertEqual(preview["mode"], "preview")
        self.assertEqual(preview["field_basis"][0]["origin"], "candidate")
        self.assertIn(value.text, preview["channels"][0]["text"])
        collector = Mock(side_effect=AssertionError("Unadopted fields cannot reach admission"))
        for action in (self.preview, self.transport, lambda: self.finalize(collector)):
            with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
                action()
        collector.assert_not_called()
        stale = self.revision()
        confirm_execution_field(preparation_id=self.prep.pk, field_value_id=value.pk,
            expected_text_sha256=value.text_sha256, expected_source_sha256=value.source_sha256,
            expected_preparation_revision=stale, select_for_execution=True,
            expected_selection_revision=None, replace=False, actor=self.user)
        self.assertEqual(self.display_response(stale).status_code, 409)
        with self.assertRaises(ValidationError):
            finalize_execution(preparation_id=self.prep.pk, expected_preparation_revision=stale,
                actor=self.user, collect_admission_facts=collector)
        collector.assert_not_called()
        self.assertIsNone(self.prep.request_frozen_at)
        self.assert_no_dispatch()

    def test_damaged_selected_text_and_rehashed_wrong_slot_fail_without_reassembly_fallback(self):
        self.prepare()
        selected = self.candidate()
        # Bypass the public immutable-row manager only to simulate test-DB damage.
        ExecutionFieldValue._base_manager.filter(pk=selected.pk).update(text="Damaged selected value")
        for action in (self.preview, self.transport, self.finalize):
            with self.assertRaises(ValidationError):
                action()
        self.assertEqual(self.display_response().status_code, 400)
        ExecutionFieldValue._base_manager.filter(pk=selected.pk).update(text=selected.text)
        detail = self.prep.contract
        facts = deepcopy(detail.resolved_inputs)
        envelope = facts["profile_sources"]["reference_context"]
        envelope["value"]["context"]["reference_bindings"][0]["input_slot"] = 99
        facts["profile_sources"]["reference_context"] = asdict(json_contract(envelope["value"]))
        assign_json_contract(detail, "resolved_inputs", facts, max_bytes=262144)
        ExecutionPreparationContract._base_manager.filter(pk=detail.pk).update(**{
            key: getattr(detail, key) for key in ("resolved_inputs", "resolved_inputs_schema_version",
                "resolved_inputs_size_bytes", "resolved_inputs_sha256")
        })
        with self.assertRaises(ValidationError):
            self.finalize()
        self.assertIsNone(self.prep.request_frozen_at)
        self.assert_no_dispatch()

    def test_definition_drift_blocks_freeze_and_damaged_frozen_manifest_is_not_recompiled(self):
        self.prepare()
        self.candidate()
        original_sha = self.prompt_profile.contract_sha256
        PromptProfileVersion.objects.filter(pk=self.prompt_profile.pk).update(contract_sha256="f" * 64)
        with self.assertRaises(ValidationError):
            self.finalize()
        self.assertIsNone(self.prep.request_frozen_at)
        PromptProfileVersion.objects.filter(pk=self.prompt_profile.pk).update(contract_sha256=original_sha)
        self.assert_same_frozen_assembly()
        ExecutionPreparationContract._base_manager.filter(preparation=self.prep).update(prepared_request_sha256="0" * 64)
        with patch("studio.services.execution_prompt_display._compile_preparation_profile",
                   side_effect=AssertionError("Corrupt request must never fall back to live compilation")):
            self.assertEqual(self.display_response().status_code, 400)
        self.assert_no_dispatch()


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class MultiChannelFinalAssemblyTests(AssemblyAssertions, TestCase):
    def setUp(self):
        self.isolate()
        workflow_fixtures.WorkflowExecutionDefinitionTests.setUp(self)
        assign_json_contract(self.profile, "specification", {
            **self.profile.specification, "model_family": "m6-multi-channel-fixture",
        })
        self.profile.save()
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=("is_active",))
        self.positive = getattr(self, "positive_text", "显式正向正文：小盒子缓缓旋转。")
        self.negative = "Explicit negative instruction; do not merge into the positive channel."
        self.prompt_profile = PromptProfileVersion.objects.create(profile_key="m6-two-channels", name="Two channels",
            version=1, model_family=model_family_for_profile(self.profile), operation="image.generate",
            mode="", review_state="ready", contract_schema_version=8)
        for position, (key, channel, text) in enumerate((
            ("custom_positive", "positive_prompt", self.positive),
            ("custom_negative", "negative_prompt", self.negative),
        ) + getattr(self, "extra_fragments", ()), 1):
            field = create_field_definition(key=key, label=f"UNCONFIGURED_{key}", value_type="long_text")
            create_profile_field_rule(profile_version=self.prompt_profile, field_definition=field,
                source_selector="fixed.value", fixed_value=text, generation_source_selector="fixed.value",
                generation_mode="passthrough", generation_output_semantics="final", language_policy="any",
                channel=channel, position=position, label_style="none", required=True, max_chars=2000)
        self.prompt_profile.channel_limits = getattr(self, "channel_limits", {})
        self.prompt_profile.save(update_fields=("channel_limits",))
        refresh_prompt_profile_contract(self.prompt_profile)
        self.prompt_profile = publish_prompt_profile(self.prompt_profile, actor=self.user)
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision,
            actor=self.user, changes={"prompt_mode": "profile", "prompt_profile_version": self.prompt_profile})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        self.job = AssetGenerationJob.objects.create(owner=self.user, model_deployment=self.deployment,
            execution_instance_version=self.version, workflow_template_version=self.workflow,
            execution_kind="comfy_workflow", job_type="asset", media_type="image", operation=self.version.operation,
            prompt="", idempotency_key="m6-two-channels", request_snapshot_sha256=json_contract({}).sha256)
        request = {"inputs": {}, "parameters": {"image.width": 512, "image.height": 512, "sampling.seed": 7},
            "manual_channels": {}, "subject_facts": [], "source_language": "Chinese", "target_language": "Chinese"}

        def resolve(definition, owner):
            return resolve_execution_request_facts(definition, request, owner=owner.owner, unit_key="image-unit",
                max_media_bytes=65536, review_contract=json_contract({"schema_version": 1, "mode": "manual"}),
                parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No implicit parameter source"))

        self.prep = create_execution_instance_preparation(owner_ref=("asset_job_id", self.job.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            unit_key="image-unit", request_key="candidate-1", intent=json_contract(request), resolve_facts=resolve, actor=self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_configured_channels_remain_separate_and_equal_in_public_preview_and_frozen_graph(self):
        channels, frozen = self.assert_same_frozen_assembly()
        self.assertEqual(channels, {"positive_prompt": self.positive, "negative_prompt": self.negative})
        graph = frozen["payload"]["compiled_workflow"]["graph"]
        self.assertEqual(graph["2"]["inputs"]["text"], self.positive)
        self.assertEqual(graph["3"]["inputs"]["text"], self.negative)
        self.assertEqual(self.display()["field_basis"], [])  # Fixed Profile text is not a fabricated field approval.
