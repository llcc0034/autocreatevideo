from __future__ import annotations

from studio.tests.reference_rendering_fixtures import LEGACY_H3_SELECTOR_TO_PRIMITIVE

import hashlib
import re
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase

from studio.models import (
    Asset,
    ModelProfileVersion,
    PromptArtifact,
    PromptFieldDefinition,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProviderProfile,
    PropBible,
    Shot,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.consistency import capture_consistency_snapshot
from studio.services.prompt_profile_compiler import (
    MediaPromptCompilationContext,
    compile_media_prompt_profile,
    media_prompt_input_fingerprint,
)
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.prompt_quality_gate import evaluate_prompt_language_gate
from studio.services.production_pipeline import (
    compile_prompt_input,
    materialize_compiled_prompt,
)
from studio.services.prompt_profiles import (
    CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY,
    DECLARATIVE_SOURCE_PROJECTION_KEY,
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    PromptProfileError,
    ResolvedPromptProfile,
    frozen_profile_reference,
    publish_prompt_profile,
    refresh_prompt_profile_contract,
    validate_prompt_profile_source_projection,
)
from studio.services.source_projection_primitives import (
    REFERENCE_CONTEXT_ENGINE_KEY,
)
from studio.tests.reference_rendering_fixtures import FRAME_POLICY, rendering_config
from studio.tests.factories import (
    H3_REF2VA_OFFICIAL_RULE_SPECS,
    build_story,
    conditioning_contract,
)


PROFILE_POSITIVE_MAX_CHARS = 7_000


class ConfiguredPromptProfileCompilerTests(TestCase):
    CUSTOM_MODE = "NebulaMotion-X"

    def setUp(self):
        self.actor, self.project, _character, _scene, _episode, self.shot, _shot2 = (
            build_story(username="configured-prompt-compiler")
        )
        self.shot.camera = "TRACKING"
        self.shot.save(update_fields=("camera", "updated_at"))
        provider = ProviderProfile(
            key="configured-prompt-vendor",
            name="Configured Prompt Vendor",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="video",
            operations="health,object_info,submit,poll,cancel,collect",
        )
        assign_json_contract(provider, "driver_config", {})
        provider.save()
        self.model = ModelProfileVersion(
            profile_key="configured-prompt-video",
            version=1,
            provider=provider,
            model_identifier="acme-nebula-video-v7",
            modality=ModelProfileVersion.Modality.VIDEO,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        text_contract = conditioning_contract("T2VA")
        reference_contract = conditioning_contract(
            "Ref2VA",
            input_kind="classified_references",
            requires_bible=True,
            entity_images=(0, 9),
            reference_videos=(0, 3),
            paired_video_audios=(0, 3),
            reference_audios=(0, 3),
        )
        text_contract["modes"].extend(reference_contract["modes"])
        text_contract["modes"].extend(
            conditioning_contract(self.CUSTOM_MODE)["modes"],
        )
        assign_json_contract(self.model, "specification", {
            "schema_version": 1,
            "model_family": "nebula_motion",
            "conditioning_contract": text_contract,
        })
        self.model.save()
        self.direction = PromptFieldDefinition.objects.create(
            key="qa_configured_direction",
            label="Direction",
            category="qa",
            description="Configured direction",
            impact="Controls test output",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        self.camera = PromptFieldDefinition.objects.create(
            key="qa_configured_camera",
            label="Camera",
            category="qa",
            description="Configured camera",
            impact="Controls test output",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        self.exclusion = PromptFieldDefinition.objects.create(
            key="qa_configured_exclusion",
            label="Exclusion",
            category="qa",
            description="Configured exclusion",
            impact="Controls test output",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
            default_value="AVOID",
        )

    def _publish_profile(
        self,
        *,
        version: int,
        rules: list[dict],
        mode: str = "T2VA",
    ) -> PromptProfileBinding:
        profile = PromptProfileVersion.objects.create(
            profile_key="qa.vendor.configured",
            version=version,
            name=f"Configured vendor prompt v{version}",
            model_family="nebula_motion",
            operation="video.generate",
            mode=mode,
            review_state=PromptProfileVersion.ReviewState.READY,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        for spec in rules:
            selector = spec["source_selector"]
            PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=spec["field_definition"],
                source_selector=selector,
                channel=spec["channel"],
                position=spec["position"],
                label_style=spec["label_style"],
                separator=spec.get(
                    "separator", PromptProfileFieldRule.Separator.NEWLINE,
                ),
                max_chars=spec.get("max_chars", 100),
                generation_mode=PromptProfileFieldRule.GenerationMode.PASSTHROUGH,
                generation_source_selector=selector,
            )
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        profile = publish_prompt_profile(profile, actor=self.actor)
        return PromptProfileBinding.objects.create(
            key=f"qa.vendor.configured.v{version}",
            name=f"Configured vendor binding v{version}",
            operation="video.generate",
            model_family="nebula_motion",
            mode=mode,
            model_profile_version=self.model,
            prompt_profile_version=profile,
        )

    def _compile(self, *, extra_direction: str = "EXTRA"):
        return compile_prompt_input(
            project=self.project,
            shots=[self.shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode="T2VA",
            include_audio=True,
            extra_direction=extra_direction,
            language_gate_mode="warn",
            model_profile_version=self.model,
        )

    def _reviewed_context_profile(
        self,
        *,
        declarative: bool = True,
    ) -> tuple[PromptProfileVersion, dict[str, PromptProfileFieldRule]]:
        """Publish the configured six-section contract used by contribution tests."""

        profile = PromptProfileVersion.objects.create(
            profile_key=(
                "qa.vendor.declarative-context"
                if declarative
                else "qa.vendor.reviewed-context-dedup"
            ),
            version=1,
            name=(
                "Declarative reviewed context"
                if declarative
                else "Reviewed contribution de-duplication"
            ),
            model_family="nebula_motion",
            operation="video.generate",
            mode="Ref2VA",
            source_projection_key=DECLARATIVE_SOURCE_PROJECTION_KEY,
            source_projection_config=rendering_config(),
            review_state=PromptProfileVersion.ReviewState.READY,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
            channel_limits={
                "positive_prompt": {
                    "max_chars": PROFILE_POSITIVE_MAX_CHARS,
                    "warn_at_chars": 6_200,
                },
            },
        )
        contribution_sources = {
            "detailed_description": (
                PromptProfileFieldRule.SourceSelector.SHOT_DETAIL,
                PromptProfileFieldRule.LengthUnit.ENGLISH_WORDS,
                350,
                500,
                "Write only the reviewed chronological director contribution.",
            ),
            "overall_soundscape": (
                PromptProfileFieldRule.SourceSelector.SHOT_SOUND_EFFECTS,
                PromptProfileFieldRule.LengthUnit.ENGLISH_WORDS,
                1,
                80,
                "Write only the reviewed diegetic sound contribution.",
            ),
            "non_diegetic_music": (
                PromptProfileFieldRule.SourceSelector.SHOT_MUSIC,
                PromptProfileFieldRule.LengthUnit.ENGLISH_WORDS,
                1,
                60,
                "Write only the reviewed non-diegetic music contribution.",
            ),
        }
        rules: dict[str, PromptProfileFieldRule] = {}
        for position, (selector, field_key, label_style) in enumerate(
            H3_REF2VA_OFFICIAL_RULE_SPECS,
            start=1,
        ):
            definition = PromptFieldDefinition.objects.get(key=field_key)
            generated = contribution_sources.get(field_key)
            configured_selector = (
                PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
                if declarative else selector
            )
            rules[field_key] = PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector=configured_selector,
                projection_primitive=(
                    LEGACY_H3_SELECTOR_TO_PRIMITIVE[selector]
                    if declarative else ""
                ),
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=position,
                label_style=label_style,
                separator=PromptProfileFieldRule.Separator.NEWLINE,
                required=True,
                omit_if_empty=False,
                max_chars=PROFILE_POSITIVE_MAX_CHARS,
                generation_mode=(
                    PromptProfileFieldRule.GenerationMode.MODEL_GENERATED
                    if generated else PromptProfileFieldRule.GenerationMode.SERVER_LOCKED
                ),
                generation_source_selector=(
                    generated[0] if generated else configured_selector
                ),
                generation_instruction=(generated[4] if generated else ""),
                generation_output_semantics=(
                    PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
                    if generated
                    else PromptProfileFieldRule.GenerationOutputSemantics.FINAL
                ),
                generation_required=True,
                language_policy="en" if generated else "any",
                length_unit=(
                    generated[1]
                    if generated else PromptProfileFieldRule.LengthUnit.NONE
                ),
                generation_min=generated[2] if generated else None,
                generation_max=generated[3] if generated else None,
                generation_validator_keys=(
                    [
                        PromptProfileFieldRule.GenerationValidator.NON_EMPTY,
                        PromptProfileFieldRule.GenerationValidator.NO_CONTROL_CHARACTERS,
                        PromptProfileFieldRule.GenerationValidator.NO_UNRESOLVED_PLACEHOLDERS,
                    ]
                    if generated else []
                ),
            )
        refresh_prompt_profile_contract(profile)
        if declarative:
            profile = publish_prompt_profile(profile, actor=self.actor)
        else:
            # Simulate an immutable pre-deprecation Profile so runtime replay
            # stays covered without reopening the legacy publish path.
            validate_prompt_profile_source_projection(profile)
            profile.status = PromptProfileVersion.Status.PUBLISHED
            profile.save(update_fields=("status", "updated_at"))
        return profile, rules

    def _reviewed_context_rows(
        self,
        *,
        profile: PromptProfileVersion,
        rules: dict[str, PromptProfileFieldRule],
        selection_sha256: str,
        values: dict[str, str],
    ) -> tuple[dict, ...]:
        rows = []
        for field_key, value in values.items():
            rule = rules[field_key]
            rows.append({
                "profile_selection_sha256": selection_sha256,
                "prompt_profile_version_id": str(profile.id),
                "profile_field_key": field_key,
                "source_selector": rule.source_selector,
                "generation_mode": rule.generation_mode,
                "generation_source_selector": rule.generation_source_selector,
                "value": value,
                "value_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "request_id": f"qa-context-dedup-{field_key}",
                "shot_id": str(self.shot.id),
                "output_semantics": rule.generation_output_semantics,
            })
        return tuple(rows)

    def test_profile_versions_change_order_labels_separators_and_channels_only_by_config(self):
        first_binding = self._publish_profile(version=1, rules=[
            {
                "field_definition": self.direction,
                "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
                "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                "position": 1,
                "label_style": PromptProfileFieldRule.LabelStyle.BRACKETED,
            },
            {
                "field_definition": self.camera,
                "source_selector": PromptProfileFieldRule.SourceSelector.SHOT_CAMERA,
                "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                "position": 2,
                "label_style": PromptProfileFieldRule.LabelStyle.LABEL_BLOCK,
                "separator": PromptProfileFieldRule.Separator.SEMICOLON,
            },
            {
                "field_definition": self.exclusion,
                "source_selector": PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
                "channel": PromptProfileFieldRule.Channel.NEGATIVE_PROMPT,
                "position": 3,
                "label_style": PromptProfileFieldRule.LabelStyle.NONE,
            },
        ])

        first = self._compile()
        self.assertEqual(
            first.prompt_text,
            "[qa_configured_direction] EXTRA; qa_configured_camera:\nTRACKING",
        )
        self.assertEqual(first.negative_prompt, "AVOID")
        self.assertEqual(
            first.structured_payload["model"],
            "acme-nebula-video-v7",
        )
        self.assertNotIn("minimax", first.structured_payload["model"].casefold())

        first_binding.is_active = False
        first_binding.save(update_fields=("is_active", "updated_at"))
        self._publish_profile(version=2, rules=[
            {
                "field_definition": self.exclusion,
                "source_selector": PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
                "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                "position": 1,
                "label_style": PromptProfileFieldRule.LabelStyle.LABEL_BLOCK,
            },
            {
                "field_definition": self.direction,
                "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
                "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                "position": 2,
                "label_style": PromptProfileFieldRule.LabelStyle.NONE,
                "separator": PromptProfileFieldRule.Separator.SPACE,
            },
            {
                "field_definition": self.camera,
                "source_selector": PromptProfileFieldRule.SourceSelector.SHOT_CAMERA,
                "channel": PromptProfileFieldRule.Channel.NEGATIVE_PROMPT,
                "position": 3,
                "label_style": PromptProfileFieldRule.LabelStyle.BRACKETED,
            },
        ])

        second = self._compile()
        self.assertEqual(
            second.prompt_text,
            "qa_configured_exclusion:\nAVOID EXTRA",
        )
        self.assertEqual(second.negative_prompt, "[qa_configured_camera] TRACKING")

    def test_profile_field_max_chars_blocks_without_model_specific_code(self):
        self._publish_profile(version=1, rules=[{
            "field_definition": self.direction,
            "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            "position": 1,
            "label_style": PromptProfileFieldRule.LabelStyle.NONE,
            "max_chars": 5,
        }])

        with self.assertRaises(PromptProfileError) as error:
            self._compile(extra_direction="TOO-LONG")
        self.assertEqual(error.exception.code, "prompt_profile_field_too_long")

    def test_direct_v1_accepts_fictional_model_family_and_mode_from_configuration(self):
        binding = self._publish_profile(
            version=1,
            mode=self.CUSTOM_MODE,
            rules=[
                {
                    "field_definition": self.camera,
                    "source_selector": PromptProfileFieldRule.SourceSelector.SHOT_CAMERA,
                    "channel": PromptProfileFieldRule.Channel.NEGATIVE_PROMPT,
                    "position": 2,
                    "label_style": PromptProfileFieldRule.LabelStyle.BRACKETED,
                },
                {
                    "field_definition": self.direction,
                    "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
                    "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                    "position": 1,
                    "label_style": PromptProfileFieldRule.LabelStyle.LABEL_COLON,
                    "separator": PromptProfileFieldRule.Separator.SPACE,
                },
            ],
        )

        result = compile_media_prompt_profile(
            binding.prompt_profile_version,
            context=MediaPromptCompilationContext(
                project=self.project,
                shots=(self.shot,),
                prompt_type=PromptArtifact.PromptType.VIDEO,
                mode=self.CUSTOM_MODE,
                include_audio=True,
                effective_durations_ms={str(self.shot.id): self.shot.duration_ms},
                consistency_snapshot=capture_consistency_snapshot(self.project),
                extra_direction="CONFIGURED DIRECTION",
            ),
        )

        self.assertEqual(binding.model_family, "nebula_motion")
        self.assertEqual(binding.mode, self.CUSTOM_MODE)
        self.assertEqual(binding.model_profile_version, self.model)
        self.assertEqual(
            result.assembly["channels"]["positive_prompt"],
            "qa_configured_direction: CONFIGURED DIRECTION",
        )
        self.assertEqual(
            result.assembly["channels"]["negative_prompt"],
            "[qa_configured_camera] TRACKING",
        )
        self.assertEqual(result.assembly["source_projection_key"], "direct_v1")
        self.assertEqual(
            [rule["field_key"] for rule in result.assembly["field_rules"]],
            ["qa_configured_direction", "qa_configured_camera"],
        )
        self.assertEqual(
            [rule["channel"] for rule in result.assembly["field_rules"]],
            ["positive_prompt", "negative_prompt"],
        )

    def test_generic_production_compiler_keeps_fictional_mode_without_h3_inference(self):
        self._publish_profile(
            version=1,
            mode=self.CUSTOM_MODE,
            rules=[{
                "field_definition": self.direction,
                "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
                "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                "position": 1,
                "label_style": PromptProfileFieldRule.LabelStyle.NONE,
            }],
        )

        result = compile_prompt_input(
            project=self.project,
            shots=[self.shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode=self.CUSTOM_MODE,
            include_audio=True,
            extra_direction="FICTIONAL PIPELINE INPUT",
            model_profile_version=self.model,
        )

        self.assertEqual(result.effective_conditioning_mode, self.CUSTOM_MODE)
        self.assertEqual(result.structured_payload["prompt_template_key"], "")
        self.assertEqual(
            result.structured_payload["target_model_profile"]["model_family"],
            "nebula_motion",
        )

    def test_explicit_workflow_contract_compiles_without_model_contract(self):
        self._publish_profile(version=1, rules=[{
            "field_definition": self.direction,
            "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            "position": 1,
            "label_style": PromptProfileFieldRule.LabelStyle.NONE,
        }])
        assign_json_contract(self.model, "specification", {
            "schema_version": 1,
            "model_family": "nebula_motion",
        })
        self.model.save(update_fields=(
            "specification",
            "specification_schema_version",
            "specification_size_bytes",
            "specification_sha256",
            "updated_at",
        ))
        workflow_contract = conditioning_contract("T2VA")
        workflow_contract_sha256 = json_contract(workflow_contract).sha256

        result = compile_prompt_input(
            project=self.project,
            shots=[self.shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode="T2VA",
            include_audio=True,
            extra_direction="WORKFLOW-ONLY CONTRACT",
            model_profile_version=self.model,
            frozen_conditioning_contract=workflow_contract,
            frozen_conditioning_contract_sha256=workflow_contract_sha256,
        )

        self.assertEqual(
            result.structured_payload["conditioning_contract"],
            workflow_contract,
        )
        self.assertEqual(
            result.structured_payload["conditioning_contract_sha256"],
            workflow_contract_sha256,
        )
        self.assertEqual(
            result.structured_payload["conditioning_mode_contract"]["key"],
            "T2VA",
        )

    def test_explicit_workflow_contract_drift_or_incomplete_pair_fails_closed(self):
        self._publish_profile(version=1, rules=[{
            "field_definition": self.direction,
            "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            "position": 1,
            "label_style": PromptProfileFieldRule.LabelStyle.NONE,
        }])
        workflow_contract = conditioning_contract("T2VA")
        kwargs = {
            "project": self.project,
            "shots": [self.shot],
            "prompt_type": PromptArtifact.PromptType.VIDEO,
            "mode": "T2VA",
            "include_audio": True,
            "extra_direction": "FAIL CLOSED",
            "model_profile_version": self.model,
            "frozen_conditioning_contract": workflow_contract,
        }

        with self.assertRaisesRegex(PromptBuildError, "必须同时提供"):
            compile_prompt_input(**kwargs)
        with self.assertRaisesRegex(PromptBuildError, "完整性校验失败"):
            compile_prompt_input(
                **kwargs,
                frozen_conditioning_contract_sha256="f" * 64,
            )

    def test_direct_v1_fingerprint_uses_generic_facts_without_h3_options(self):
        context = MediaPromptCompilationContext(
            project=self.project,
            shots=(self.shot,),
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode=self.CUSTOM_MODE,
            include_audio=True,
            effective_durations_ms={str(self.shot.id): self.shot.duration_ms},
            consistency_snapshot=capture_consistency_snapshot(self.project),
            extra_direction="FICTIONAL DIRECT INPUT",
        )
        with patch(
            "studio.services.prompt_profile_compiler.compile_reference_context",
            side_effect=AssertionError("direct_v1 must not call the reference renderer"),
        ) as generic_fingerprint:
            first = media_prompt_input_fingerprint(context)
            second = media_prompt_input_fingerprint(
                MediaPromptCompilationContext(
                    **{
                        **context.__dict__,
                        "extra_direction": "FICTIONAL DIRECT INPUT CHANGED",
                    },
                ),
            )

        generic_fingerprint.assert_not_called()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)

    def test_direct_v1_rejects_adapter_owned_reference_context_selectors(self):
        profile = PromptProfileVersion.objects.create(
            profile_key="qa.nebula.invalid-adapter-selector",
            version=1,
            name="Invalid direct adapter selector",
            model_family="nebula_motion",
            operation="video.generate",
            mode=self.CUSTOM_MODE,
            review_state=PromptProfileVersion.ReviewState.READY,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=PromptFieldDefinition.objects.get(key="subject_definitions"),
            source_selector=(
                PromptProfileFieldRule.SourceSelector.H3_REF2VA_SUBJECT_DEFINITIONS
            ),
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            generation_mode=PromptProfileFieldRule.GenerationMode.SERVER_LOCKED,
            generation_source_selector=(
                PromptProfileFieldRule.SourceSelector.H3_REF2VA_SUBJECT_DEFINITIONS
            ),
            language_policy="any",
        )
        refresh_prompt_profile_contract(profile)

        with self.assertRaises(PromptProfileError) as context:
            publish_prompt_profile(profile, actor=self.actor)

        self.assertEqual(
            context.exception.code,
            "prompt_profile_source_selector_runtime_only",
        )

    def test_reviewed_final_value_overrides_profile_field_without_source_mutation(self):
        profile = PromptProfileVersion.objects.create(
            profile_key="qa.vendor.reviewed-final",
            version=1,
            name="Reviewed final value",
            model_family="nebula_motion",
            operation="video.generate",
            mode=self.CUSTOM_MODE,
            review_state=PromptProfileVersion.ReviewState.READY,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=self.camera,
            source_selector=PromptProfileFieldRule.SourceSelector.SHOT_CAMERA,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            label_style=PromptProfileFieldRule.LabelStyle.NONE,
            max_chars=100,
            generation_mode=PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
            generation_source_selector=(
                PromptProfileFieldRule.SourceSelector.SHOT_NARRATIVE
            ),
            generation_instruction="Write the final camera direction.",
            generation_output_semantics=(
                PromptProfileFieldRule.GenerationOutputSemantics.FINAL
            ),
            generation_required=True,
            language_policy="en",
            length_unit=PromptProfileFieldRule.LengthUnit.CHARACTERS,
            generation_min=1,
            generation_max=100,
            generation_validator_keys=[
                PromptProfileFieldRule.GenerationValidator.NON_EMPTY,
            ],
        )
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        selection_sha256 = "a" * 64
        reviewed = "LOCKED REVIEWED CAMERA"
        result = compile_media_prompt_profile(
            profile,
            context=MediaPromptCompilationContext(
                project=self.project,
                shots=(self.shot,),
                prompt_type=PromptArtifact.PromptType.VIDEO,
                mode=self.CUSTOM_MODE,
                include_audio=True,
                effective_durations_ms={str(self.shot.id): self.shot.duration_ms},
                consistency_snapshot=capture_consistency_snapshot(self.project),
                profile_selection_sha256=selection_sha256,
                reviewed_profile_fields=({
                    "profile_selection_sha256": selection_sha256,
                    "prompt_profile_version_id": str(profile.id),
                    "profile_field_key": self.camera.key,
                    "source_selector": (
                        PromptProfileFieldRule.SourceSelector.SHOT_CAMERA
                    ),
                    "generation_mode": (
                        PromptProfileFieldRule.GenerationMode.MODEL_GENERATED
                    ),
                    "generation_source_selector": (
                        PromptProfileFieldRule.SourceSelector.SHOT_NARRATIVE
                    ),
                    "value": reviewed,
                    "value_sha256": hashlib.sha256(
                        reviewed.encode("utf-8"),
                    ).hexdigest(),
                    "request_id": "qa-reviewed-final",
                    "shot_id": str(self.shot.id),
                    "output_semantics": (
                        PromptProfileFieldRule.GenerationOutputSemantics.FINAL
                    ),
                },),
            ),
        )

        self.assertEqual(result.assembly["channels"]["positive_prompt"], reviewed)
        self.assertEqual(self.shot.camera, "TRACKING")
        self.assertNotIn(
            "value",
            result.reviewed_field_evidence["rows"][0],
        )

    def test_direct_v1_executes_schema_v2_value_contract_for_every_value_path(self):
        def publish_single_rule(
            suffix: str,
            *,
            source_selector: str,
            default_value=None,
            generation_mode: str,
            generation_required: bool,
            language_policy: str,
            length_unit: str = PromptProfileFieldRule.LengthUnit.NONE,
            generation_min: int | None = None,
            generation_max: int | None = None,
            validators: list[str] | None = None,
        ) -> tuple[PromptProfileVersion, PromptProfileFieldRule]:
            definition = PromptFieldDefinition.objects.create(
                key=f"qa_value_contract_{suffix}",
                label=f"Value contract {suffix}",
                category="qa",
                description="Schema v2 runtime value contract",
                impact="Rejects invalid configured prompt values",
                value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
                default_value=default_value,
            )
            profile = PromptProfileVersion.objects.create(
                profile_key=f"qa.nebula.value-contract-{suffix}",
                version=1,
                name=f"Value contract {suffix}",
                model_family="nebula_motion",
                operation="video.generate",
                mode=self.CUSTOM_MODE,
                review_state=PromptProfileVersion.ReviewState.READY,
                contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
            )
            rule = PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector=source_selector,
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=1,
                label_style=PromptProfileFieldRule.LabelStyle.NONE,
                required=False,
                omit_if_empty=True,
                max_chars=1000,
                generation_mode=generation_mode,
                generation_source_selector=(
                    PromptProfileFieldRule.SourceSelector.SHOT_NARRATIVE
                    if generation_mode
                    == PromptProfileFieldRule.GenerationMode.MODEL_GENERATED
                    else source_selector
                ),
                generation_instruction=(
                    "Write the final configured field value."
                    if generation_mode
                    == PromptProfileFieldRule.GenerationMode.MODEL_GENERATED
                    else ""
                ),
                generation_required=generation_required,
                language_policy=language_policy,
                length_unit=length_unit,
                generation_min=generation_min,
                generation_max=generation_max,
                generation_validator_keys=validators or [],
            )
            refresh_prompt_profile_contract(profile)
            return publish_prompt_profile(profile, actor=self.actor), rule

        def compile_direct(
            profile: PromptProfileVersion,
            *,
            extra_direction: str = "",
            reviewed_profile_fields=None,
            selection_sha256: str = "",
        ):
            return compile_media_prompt_profile(
                profile,
                context=MediaPromptCompilationContext(
                    project=self.project,
                    shots=(self.shot,),
                    prompt_type=PromptArtifact.PromptType.VIDEO,
                    mode=self.CUSTOM_MODE,
                    include_audio=True,
                    effective_durations_ms={
                        str(self.shot.id): self.shot.duration_ms,
                    },
                    consistency_snapshot=capture_consistency_snapshot(self.project),
                    extra_direction=extra_direction,
                    profile_selection_sha256=selection_sha256,
                    reviewed_profile_fields=reviewed_profile_fields,
                ),
            )

        required_profile, _required_rule = publish_single_rule(
            "server-required",
            source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
            default_value="",
            generation_mode=PromptProfileFieldRule.GenerationMode.SERVER_LOCKED,
            generation_required=True,
            language_policy="any",
        )
        with self.assertRaises(PromptProfileError) as required_error:
            compile_direct(required_profile)
        self.assertEqual(
            required_error.exception.code,
            "prompt_profile_generation_value_required",
        )

        validator_profile, _validator_rule = publish_single_rule(
            "server-validator",
            source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
            default_value="{{pending}}",
            generation_mode=PromptProfileFieldRule.GenerationMode.SERVER_LOCKED,
            generation_required=True,
            language_policy="any",
            validators=[
                PromptProfileFieldRule.GenerationValidator.NO_UNRESOLVED_PLACEHOLDERS,
            ],
        )
        with self.assertRaises(PromptProfileError) as validator_error:
            compile_direct(validator_profile)
        self.assertEqual(
            validator_error.exception.code,
            "prompt_profile_generation_value_validator_failed",
        )

        language_profile, _language_rule = publish_single_rule(
            "passthrough-language",
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            generation_mode=PromptProfileFieldRule.GenerationMode.PASSTHROUGH,
            generation_required=True,
            language_policy="en",
        )
        with self.assertRaises(PromptProfileError) as language_error:
            compile_direct(language_profile, extra_direction="仅中文说明")
        self.assertEqual(
            language_error.exception.code,
            "prompt_profile_generation_language_invalid",
        )

        final_profile, final_rule = publish_single_rule(
            "reviewed-final-length",
            source_selector=PromptProfileFieldRule.SourceSelector.SHOT_CAMERA,
            generation_mode=PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
            generation_required=True,
            language_policy="en",
            length_unit=PromptProfileFieldRule.LengthUnit.CHARACTERS,
            generation_min=10,
            generation_max=20,
            validators=[PromptProfileFieldRule.GenerationValidator.NON_EMPTY],
        )
        selection_sha256 = "c" * 64
        reviewed = "short"
        with self.assertRaises(PromptProfileError) as length_error:
            compile_direct(
                final_profile,
                selection_sha256=selection_sha256,
                reviewed_profile_fields=({
                    "profile_selection_sha256": selection_sha256,
                    "prompt_profile_version_id": str(final_profile.id),
                    "profile_field_key": final_rule.field_definition.key,
                    "source_selector": final_rule.source_selector,
                    "generation_mode": final_rule.generation_mode,
                    "generation_source_selector": (
                        final_rule.generation_source_selector
                    ),
                    "value": reviewed,
                    "value_sha256": hashlib.sha256(
                        reviewed.encode("utf-8"),
                    ).hexdigest(),
                    "request_id": "qa-reviewed-final-length",
                    "shot_id": str(self.shot.id),
                    "output_semantics": (
                        PromptProfileFieldRule.GenerationOutputSemantics.FINAL
                    ),
                },),
            )
        self.assertEqual(
            length_error.exception.code,
            "prompt_profile_generation_value_length_invalid",
        )

        advisory_profile, advisory_rule = publish_single_rule(
            "reviewed-advisory-length",
            source_selector=PromptProfileFieldRule.SourceSelector.SHOT_CAMERA,
            generation_mode=PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
            generation_required=True,
            language_policy="en",
            length_unit=PromptProfileFieldRule.LengthUnit.CHARACTERS,
            generation_min=10,
            generation_max=20,
            validators=[
                PromptProfileFieldRule.GenerationValidator.NON_EMPTY,
                PromptProfileFieldRule.GenerationValidator.LENGTH_ADVISORY,
            ],
        )
        advisory_selection_sha256 = "d" * 64
        compiled = compile_direct(
            advisory_profile,
            selection_sha256=advisory_selection_sha256,
            reviewed_profile_fields=({
                "profile_selection_sha256": advisory_selection_sha256,
                "prompt_profile_version_id": str(advisory_profile.id),
                "profile_field_key": advisory_rule.field_definition.key,
                "source_selector": advisory_rule.source_selector,
                "generation_mode": advisory_rule.generation_mode,
                "generation_source_selector": (
                    advisory_rule.generation_source_selector
                ),
                "value": reviewed,
                "value_sha256": hashlib.sha256(
                    reviewed.encode("utf-8"),
                ).hexdigest(),
                "request_id": "qa-reviewed-advisory-length",
                "shot_id": str(self.shot.id),
                "output_semantics": (
                    PromptProfileFieldRule.GenerationOutputSemantics.FINAL
                ),
            },),
        )
        audit = next(
            row
            for row in compiled.generation_length_audit
            if row["field_key"] == advisory_rule.field_definition.key
        )
        self.assertEqual(audit["status"], "warning")
        self.assertEqual(audit["enforcement"], "advisory")

    def test_reviewed_contributions_are_projected_into_complete_configured_sections(self):
        self.shot.conditioning_mode = "Ref2VA"
        self.shot.detailed_description = "ORIGINAL DETAIL"
        self.shot.sound_effects = "ORIGINAL SOUND"
        self.shot.music = "ORIGINAL MUSIC"
        self.shot.save(update_fields=(
            "conditioning_mode",
            "detailed_description",
            "sound_effects",
            "music",
            "updated_at",
        ))
        profile = PromptProfileVersion.objects.create(
            profile_key="qa.vendor.reviewed-contribution",
            version=1,
            name="Reviewed contribution projection",
            model_family="nebula_motion",
            operation="video.generate",
            mode="Ref2VA",
            source_projection_key=DECLARATIVE_SOURCE_PROJECTION_KEY,
            source_projection_config=rendering_config(),
            review_state=PromptProfileVersion.ReviewState.READY,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        contribution_sources = {
            "detailed_description": (
                PromptProfileFieldRule.SourceSelector.SHOT_DETAIL,
                "Write the reviewed visible chronological detail.",
            ),
            "overall_soundscape": (
                PromptProfileFieldRule.SourceSelector.SHOT_SOUND_EFFECTS,
                "Write the reviewed diegetic sound contribution.",
            ),
            "non_diegetic_music": (
                PromptProfileFieldRule.SourceSelector.SHOT_MUSIC,
                "Write the reviewed non-diegetic music contribution.",
            ),
        }
        for position, (selector, field_key, label_style) in enumerate(
            H3_REF2VA_OFFICIAL_RULE_SPECS,
            start=1,
        ):
            definition = PromptFieldDefinition.objects.get(key=field_key)
            generated = contribution_sources.get(field_key)
            PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector="projection.output",
                projection_primitive=LEGACY_H3_SELECTOR_TO_PRIMITIVE[selector],
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=position,
                label_style=label_style,
                separator=PromptProfileFieldRule.Separator.NEWLINE,
                required=True,
                omit_if_empty=False,
                max_chars=7000,
                generation_mode=(
                    PromptProfileFieldRule.GenerationMode.MODEL_GENERATED
                    if generated else PromptProfileFieldRule.GenerationMode.SERVER_LOCKED
                ),
                generation_source_selector=(generated[0] if generated else "projection.output"),
                generation_instruction=(generated[1] if generated else ""),
                generation_output_semantics=(
                    PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
                    if generated
                    else PromptProfileFieldRule.GenerationOutputSemantics.FINAL
                ),
                generation_required=True,
                language_policy="en" if generated else "any",
                length_unit=(
                    PromptProfileFieldRule.LengthUnit.CHARACTERS
                    if generated else PromptProfileFieldRule.LengthUnit.NONE
                ),
                generation_min=1 if generated else None,
                generation_max=1000 if generated else None,
                generation_validator_keys=(
                    [PromptProfileFieldRule.GenerationValidator.NON_EMPTY]
                    if generated else []
                ),
            )
        refresh_prompt_profile_contract(profile)
        validate_prompt_profile_source_projection(profile)
        profile.status = PromptProfileVersion.Status.PUBLISHED
        profile.save(update_fields=("status", "updated_at"))
        binding = PromptProfileBinding.objects.create(
            key="qa.vendor.reviewed-contribution",
            name="Reviewed contribution projection",
            operation="video.generate",
            model_family="nebula_motion",
            mode="Ref2VA",
            model_profile_version=self.model,
            prompt_profile_version=profile,
        )
        frozen_selection = frozen_profile_reference(
            ResolvedPromptProfile(binding=binding, profile_version=profile),
            model_profile_version=self.model,
            operation="video.generate",
            mode="Ref2VA",
        )
        selection_sha256 = json_contract(frozen_selection).sha256
        reviewed_values = {
            "detailed_description": "REVIEWED VISIBLE DETAIL",
            "overall_soundscape": "REVIEWED DIEGETIC SOUND",
            "non_diegetic_music": "REVIEWED NON-DIEGETIC MUSIC",
        }
        rows = []
        for field_key, value in reviewed_values.items():
            source_selector, _instruction = contribution_sources[field_key]
            rows.append({
                "profile_selection_sha256": selection_sha256,
                "prompt_profile_version_id": str(profile.id),
                "profile_field_key": field_key,
                "source_selector": "projection.output",
                "generation_mode": (
                    PromptProfileFieldRule.GenerationMode.MODEL_GENERATED
                ),
                "generation_source_selector": source_selector,
                "value": value,
                "value_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "request_id": f"qa-contribution-{field_key}",
                "shot_id": str(self.shot.id),
                "output_semantics": (
                    PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
                ),
            })
        character = self.shot.characters.get()
        reference_binding = {
            "category": "character",
            "entity_id": str(character.id),
            "entity_name": character.name,
            "asset_id": str(character.anchor_asset_id),
            "id": str(character.anchor_asset_id),
            "name": "reviewed canonical portrait",
            "description": "",
            "prompt_hint": "",
            "media_metadata": {},
            "local_path": "C:/fixtures/lin.png",
            "checksum": "fixture-lin",
            "input_slot": 1,
            "input_label": "<Picture 1>",
            "project_slot": 1,
            "entity_label": "<Subject 1>",
        }
        with patch(
            "studio.services.production_pipeline.resolve_group_reference_bindings",
            return_value=[reference_binding],
        ), patch(
            "studio.services.prompt_profile_compiler.media_prompt_input_fingerprint",
            return_value="a" * 64,
        ) as generic_fingerprint:
            result = compile_prompt_input(
                project=self.project,
                shots=[self.shot],
                prompt_type=PromptArtifact.PromptType.VIDEO,
                mode="Ref2VA",
                include_audio=True,
                consistency_snapshot=capture_consistency_snapshot(self.project),
                language_gate_mode="warn",
                model_profile_version=self.model,
                frozen_prompt_profile=frozen_selection,
                frozen_frame_policy=FRAME_POLICY,
                reviewed_profile_fields=rows,
            )
        generic_fingerprint.assert_called_once()

        prompt = result.prompt_text
        self.assertIn("detailed_description:", prompt)
        self.assertIn("REVIEWED VISIBLE DETAIL", prompt)
        self.assertIn("REVIEWED DIEGETIC SOUND", prompt)
        self.assertIn("REVIEWED NON-DIEGETIC MUSIC", prompt)
        self.assertNotIn("ORIGINAL DETAIL", prompt)
        self.assertNotIn("ORIGINAL SOUND", prompt)
        self.assertNotIn("ORIGINAL MUSIC", prompt)
        self.assertEqual(
            result.structured_payload["model_prompt_contract"]["format"],
            "reference_context_ir_v2",
        )
        self.assertEqual(
            len(result.structured_payload["profile_field_value_contract"]["rows"]),
            3,
        )

    def test_declarative_projection_builds_six_sections_and_consumes_three_contributions(self):
        self.shot.conditioning_mode = "Ref2VA"
        self.shot.detailed_description = "ORIGINAL DECLARATIVE DETAIL"
        self.shot.sound_effects = "ORIGINAL DECLARATIVE SOUND"
        self.shot.music = "ORIGINAL DECLARATIVE MUSIC"
        self.shot.save(update_fields=(
            "conditioning_mode",
            "detailed_description",
            "sound_effects",
            "music",
            "updated_at",
        ))
        profile, rules = self._reviewed_context_profile(declarative=True)
        binding = PromptProfileBinding.objects.create(
            key="qa.vendor.declarative-context",
            name="Declarative reference context",
            operation="video.generate",
            model_family="nebula_motion",
            mode="Ref2VA",
            model_profile_version=self.model,
            prompt_profile_version=profile,
        )
        frozen_selection = frozen_profile_reference(
            ResolvedPromptProfile(binding=binding, profile_version=profile),
            model_profile_version=self.model,
            operation="video.generate",
            mode="Ref2VA",
        )
        selection_sha256 = json_contract(frozen_selection).sha256
        reviewed_detail = " ".join(
            ["reviewed visible timeline"] * 120,
        )
        reviewed_values = {
            "detailed_description": reviewed_detail,
            "overall_soundscape": "REVIEWED DECLARATIVE SOUND",
            "non_diegetic_music": "REVIEWED DECLARATIVE MUSIC",
        }
        reviewed_rows = self._reviewed_context_rows(
            profile=profile,
            rules=rules,
            selection_sha256=selection_sha256,
            values=reviewed_values,
        )
        character = self.shot.characters.get()
        reference_binding = {
            "category": "character",
            "entity_id": str(character.id),
            "entity_name": character.name,
            "asset_id": str(character.anchor_asset_id),
            "id": str(character.anchor_asset_id),
            "name": "reviewed canonical portrait",
            "description": "",
            "prompt_hint": "",
            "media_metadata": {},
            "local_path": "C:/fixtures/declarative-character.png",
            "checksum": "fixture-declarative-character",
            "input_slot": 1,
            "input_label": "<Picture 1>",
            "project_slot": 1,
            "entity_label": "<Subject 1>",
        }

        with patch(
            "studio.services.production_pipeline.resolve_group_reference_bindings",
            return_value=[reference_binding],
        ), patch(
            "studio.services.prompt_profile_compiler.media_prompt_input_fingerprint",
            return_value="b" * 64,
        ) as generic_fingerprint:
            result = compile_prompt_input(
                project=self.project,
                shots=[self.shot],
                prompt_type=PromptArtifact.PromptType.VIDEO,
                mode="Ref2VA",
                include_audio=True,
                consistency_snapshot=capture_consistency_snapshot(self.project),
                language_gate_mode="warn",
                model_profile_version=self.model,
                frozen_prompt_profile=frozen_selection,
                frozen_frame_policy=FRAME_POLICY,
                reviewed_profile_fields=reviewed_rows,
            )

        generic_fingerprint.assert_called_once()
        expected_fields = [
            field_key
            for _selector, field_key, _label_style
            in H3_REF2VA_OFFICIAL_RULE_SPECS
        ]
        model_contract = result.structured_payload["model_prompt_contract"]
        self.assertEqual(model_contract["format"], "reference_context_ir_v2")
        self.assertEqual(model_contract["section_order"], expected_fields)
        self.assertTrue(
            set(expected_fields).issubset(result.structured_payload["fields"]),
        )
        self.assertIn(reviewed_detail, result.prompt_text)
        self.assertIn("REVIEWED DECLARATIVE SOUND", result.prompt_text)
        self.assertIn("REVIEWED DECLARATIVE MUSIC", result.prompt_text)
        self.assertNotIn("ORIGINAL DECLARATIVE DETAIL", result.prompt_text)
        self.assertNotIn("ORIGINAL DECLARATIVE SOUND", result.prompt_text)
        self.assertNotIn("ORIGINAL DECLARATIVE MUSIC", result.prompt_text)
        self.assertEqual(
            len(result.structured_payload["profile_field_value_contract"]["rows"]),
            3,
        )

    def test_profile_motion_advisory_reaches_projection_quality_without_blocking(self):
        profile, _rules = self._reviewed_context_profile()
        binding = PromptProfileBinding.objects.create(
            key="qa.vendor.reviewed-context-quality",
            name="Reviewed context quality advisory",
            operation="video.generate",
            model_family="nebula_motion",
            mode="Ref2VA",
            model_profile_version=self.model,
            prompt_profile_version=profile,
        )
        frozen_selection = frozen_profile_reference(
            ResolvedPromptProfile(binding=binding, profile_version=profile),
            model_profile_version=self.model,
        )
        reference_binding = {
            "category": "video",
            "entity_id": "",
            "entity_name": "",
            "asset_id": "fixture-generic-motion-video",
            "id": "fixture-generic-motion-video",
            "name": "generic motion fixture",
            "description": "",
            "prompt_hint": "workflow smoke fixture",
            "media_metadata": {"fps": 24},
            "local_path": "C:/fixtures/generic-motion.mp4",
            "checksum": "fixture-generic-motion-video",
            "purpose": "motion_camera_reference",
            "input_slot": 1,
            "input_label": "<Video 1>",
            "entity_label": "<Video 1>",
            "audio_input_label": None,
        }

        with patch(
            "studio.services.production_pipeline.resolve_group_reference_bindings",
            return_value=[reference_binding],
        ):
            compiled = compile_prompt_input(
                project=self.project,
                shots=[self.shot],
                prompt_type=PromptArtifact.PromptType.VIDEO,
                mode="Ref2VA",
                include_audio=True,
                consistency_snapshot=capture_consistency_snapshot(self.project),
                language_gate_mode="warn",
                model_profile_version=self.model,
                frozen_prompt_profile=frozen_selection,
                frozen_frame_policy=FRAME_POLICY,
            )
        with transaction.atomic():
            artifact = materialize_compiled_prompt(
                compiled=compiled,
                created_by=self.actor,
            )

        quality = artifact.structured_payload["quality"]
        self.assertTrue(any(
            "运镜参考说明未达到本策略建议值" in warning
            for warning in quality.get("warnings", [])
        ))
        self.assertFalse(any(
            "prompt_hint" in field
            for field in quality.get("hard_block_fields", [])
        ))
        self.assertFalse(
            quality["production_language_gate"]["would_block_enqueue"],
        )
        self.assertEqual(artifact.prompt_profile_version_id, profile.id)
        self.assertFalse(evaluate_prompt_language_gate(artifact).blocks_enqueue)

    def test_reviewed_detail_contribution_owns_creative_prose_without_repeating_locked_sections(self):
        self.shot.conditioning_mode = "Ref2VA"
        self.shot.detailed_description = "ORIGINAL DETERMINISTIC DETAIL"
        self.shot.sound_effects = "ORIGINAL DETERMINISTIC SOUND"
        self.shot.music = "ORIGINAL DETERMINISTIC MUSIC"
        self.shot.metadata = {
            "prompt_field_version": 2,
            "negative_prompt": "identity drift",
        }
        self.shot.continuity_notes = "ORIGINAL VISIBLE CONTINUITY CHECKLIST"
        self.shot.requires_first_frame_continuity = True
        self.shot.requires_last_frame_continuity = True
        self.shot.save(update_fields=(
            "conditioning_mode",
            "detailed_description",
            "sound_effects",
            "music",
            "metadata",
            "continuity_notes",
            "requires_first_frame_continuity",
            "requires_last_frame_continuity",
            "updated_at",
        ))
        character = self.shot.characters.get()
        scene = self.shot.scene
        prop_asset = Asset.objects.create(
            owner=self.actor,
            style=self.project.style,
            kind=Asset.Kind.PROP,
            name="烧焦信纸标准图",
            local_path="C:/fixtures/charred-letter.png",
            checksum="fixture-charred-letter",
            mime_type="image/png",
        )
        prop = PropBible.objects.create(
            project=self.project,
            slug="charred-letter",
            name="烧焦信纸",
            visual_identity="blackened folded evidence letter",
            material="charred cotton paper",
            distinctive_marks="one diagonal amber burn line",
            continuity_state="partly unfolded on the archive desk",
            anchor_asset=prop_asset,
        )
        self.shot.props.add(prop)
        reference_bindings = ({
            "category": "character",
            "entity_id": str(character.id),
            "entity_name": character.name,
            "asset_id": str(character.anchor_asset_id),
            "id": str(character.anchor_asset_id),
            "name": "canonical character portrait",
            "description": "",
            "prompt_hint": "",
            "media_metadata": {},
            "local_path": "C:/fixtures/lin.png",
            "checksum": "fixture-lin",
            "input_slot": 1,
            "input_label": "<Picture 1>",
            "project_slot": 1,
            "entity_label": "<Subject 1>",
        }, {
            "category": "scene",
            "entity_id": str(scene.id),
            "entity_name": scene.name,
            "asset_id": str(scene.anchor_asset_id),
            "id": str(scene.anchor_asset_id),
            "name": "canonical archive room",
            "description": "",
            "prompt_hint": "",
            "media_metadata": {},
            "local_path": "C:/fixtures/archive.png",
            "checksum": "fixture-archive",
            "input_slot": 2,
            "input_label": "<Picture 2>",
            "project_slot": 2,
            "entity_label": "<Subject 2>",
        }, {
            "category": "prop",
            "entity_id": str(prop.id),
            "entity_name": prop.name,
            "asset_id": str(prop.anchor_asset_id),
            "id": str(prop.anchor_asset_id),
            "name": "canonical charred letter",
            "description": "",
            "prompt_hint": "",
            "media_metadata": {},
            "local_path": "C:/fixtures/charred-letter.png",
            "checksum": "fixture-charred-letter",
            "input_slot": 3,
            "input_label": "<Picture 3>",
            "project_slot": 3,
            "entity_label": "<Subject 3>",
        }, {
            "category": "video",
            "entity_id": "",
            "entity_name": "",
            "asset_id": "fixture-motion-video",
            "id": "fixture-motion-video",
            "name": "same-story camera reference",
            "description": "",
            "prompt_hint": (
                "Use the slow forward dolly with restrained handheld sway and "
                "one measured rack focus near the end"
            ),
            "media_metadata": {"fps": 24},
            "local_path": "C:/fixtures/motion.mp4",
            "checksum": "fixture-motion-video",
            "purpose": "motion_camera_reference",
            "input_slot": 1,
            "input_label": "<Video 1>",
            "entity_label": "<Video 1>",
        })
        profile, rules = self._reviewed_context_profile()

        # With no reviewed contribution, the same adapter retains its previous
        # deterministic projection path, including the detailed Bible anchor,
        # continuity, and diegetic sound additions.
        baseline = compile_media_prompt_profile(
            profile,
            context=MediaPromptCompilationContext(
                project=self.project,
                shots=(self.shot,),
                prompt_type=PromptArtifact.PromptType.VIDEO,
                mode="Ref2VA",
                include_audio=True,
                effective_durations_ms={str(self.shot.id): self.shot.duration_ms},
                consistency_snapshot=capture_consistency_snapshot(self.project),
                reference_bindings=reference_bindings,
                model_identifier=self.model.model_identifier,
                frame_policy=FRAME_POLICY,
                conditioning_mode_contract=conditioning_contract("Ref2VA", input_kind="classified_references")["modes"][0],
                language_gate_mode="warn",
            ),
        )
        baseline_detail = baseline.assembly["structured_fields"]["detailed_description"]
        self.assertIn("Environment:", baseline_detail)
        self.assertIn("ORIGINAL DETERMINISTIC SOUND", baseline.assembly["structured_fields"]["overall_soundscape"])
        self.assertIn("ORIGINAL VISIBLE CONTINUITY CHECKLIST", baseline_detail)

        # Reproduce the real failure shape: the first three locked sections own
        # long character/scene/prop anchors, while Layer 2 supplies a complete
        # reviewed director contribution.  The detailed section must not project
        # those independent facts a second time.
        anchor_clause = (
            "precise locked visual evidence with asymmetric weathering, stable "
            "material response, controlled amber highlights, measured scale, "
            "repeatable silhouette, and production continuity markers"
        )
        character.visual_identity = "CHARACTER_LOCKED_ANCHOR " + " ".join(
            [anchor_clause] * 2
        )
        character.save(update_fields=("visual_identity", "updated_at"))
        scene.visual_identity = "SCENE_LOCKED_ANCHOR " + " ".join(
            [anchor_clause] * 2
        )
        scene.save(update_fields=("visual_identity", "updated_at"))
        prop.visual_identity = "PROP_LOCKED_ANCHOR " + " ".join(
            [anchor_clause] * 2
        )
        prop.save(update_fields=("visual_identity", "updated_at"))
        contribution_words = (
            "camera observes deliberate motion through rain as the investigator "
            "opens evidence and follows a controlled visual beat with precise "
            "framing natural posture restrained expression and readable action"
        ).split()
        reviewed_detail = " ".join((contribution_words * 24)[:389])
        self.assertEqual(
            len(re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)?", reviewed_detail)),
            389,
        )
        self.shot.detailed_description = reviewed_detail
        self.shot.save(update_fields=("detailed_description", "updated_at"))
        reviewed_sound = "Muted rain and paper friction stay synchronized to the visible hand movement."
        reviewed_music = "A sparse low cello pulse remains non-diegetic and restrained."
        selection_sha256 = "d" * 64
        reviewed_rows = self._reviewed_context_rows(
            profile=profile,
            rules=rules,
            selection_sha256=selection_sha256,
            values={
                "detailed_description": reviewed_detail,
                "overall_soundscape": reviewed_sound,
                "non_diegetic_music": reviewed_music,
            },
        )
        result = compile_media_prompt_profile(
            profile,
            context=MediaPromptCompilationContext(
                project=self.project,
                shots=(self.shot,),
                prompt_type=PromptArtifact.PromptType.VIDEO,
                mode="Ref2VA",
                include_audio=True,
                effective_durations_ms={str(self.shot.id): self.shot.duration_ms},
                consistency_snapshot=capture_consistency_snapshot(self.project),
                reference_bindings=reference_bindings,
                profile_selection_sha256=selection_sha256,
                reviewed_profile_fields=reviewed_rows,
                model_identifier=self.model.model_identifier,
                frame_policy=FRAME_POLICY,
                conditioning_mode_contract=conditioning_contract("Ref2VA", input_kind="classified_references")["modes"][0],
                language_gate_mode="warn",
            ),
        )

        prompt = result.assembly["channels"]["positive_prompt"]
        detail = result.assembly["structured_fields"]["detailed_description"]
        retention = result.assembly["structured_fields"]["retention_analysis"]
        self.assertLessEqual(len(prompt), PROFILE_POSITIVE_MAX_CHARS)
        self.assertLessEqual(
            result.assembly["channel_audit"]["positive_prompt"]["character_count"],
            PROFILE_POSITIVE_MAX_CHARS,
        )
        self.assertEqual(
            result.assembly["channel_audit"]["positive_prompt"]["max_chars"],
            PROFILE_POSITIVE_MAX_CHARS,
        )
        detailed_audit = next(
            row for row in result.generation_length_audit
            if row["field_key"] == "detailed_description"
        )
        self.assertEqual(detailed_audit["length_unit"], "english_words")
        self.assertEqual(detailed_audit["generation_min"], 350)
        self.assertEqual(detailed_audit["generation_max"], 500)
        self.assertEqual(detailed_audit["measured"], 389)
        self.assertEqual(prompt.count(reviewed_detail), 1)
        self.assertLess(
            detail.index("Visual style: "),
            detail.index("[Shot 1]"),
        )
        self.assertLess(
            detail.index("<Subject 1>"),
            detail.index(reviewed_detail),
        )
        self.assertIn(
            "(shots 1): fully_preserved",
            retention,
        )
        self.assertNotIn("<Subject 1>: fully_preserved —", retention)
        self.assertIn("<d>[Chinese] 这封信不是昨晚烧的。</d>", detail)
        self.assertEqual(
            detail.count("<d>[Chinese] 这封信不是昨晚烧的。</d>"),
            1,
        )
        self.assertIn("<Subject 1>", detail)
        self.assertIn("<Subject 2>", detail)
        self.assertIn("<Subject 3>", detail)
        self.assertNotIn("CHARACTER_LOCKED_ANCHOR", detail)
        self.assertNotIn("SCENE_LOCKED_ANCHOR", detail)
        self.assertNotIn("PROP_LOCKED_ANCHOR", detail)
        self.assertNotIn(reviewed_sound, detail)
        self.assertNotIn(reviewed_music, detail)
        self.assertNotIn("ORIGINAL VISIBLE CONTINUITY CHECKLIST", detail)
        self.assertIn("CHARACTER_LOCKED_ANCHOR", prompt)
        self.assertIn("SCENE_LOCKED_ANCHOR", prompt)
        self.assertIn("PROP_LOCKED_ANCHOR", prompt)
        self.assertEqual(prompt.count(reviewed_sound), 1)
        self.assertEqual(prompt.count(reviewed_music), 1)
        self.assertEqual(
            set(re.findall(r"<(?:Picture|Video|Audio)\s+[1-9][0-9]*>", prompt)),
            {"<Picture 1>", "<Picture 2>", "<Picture 3>", "<Video 1>"},
        )
