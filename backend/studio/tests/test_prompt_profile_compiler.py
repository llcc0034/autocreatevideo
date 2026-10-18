from __future__ import annotations
from studio.models import FieldDefinitionVersion
from studio.tests.field_definition_fixtures import create_field_definition

from studio.tests.reference_rendering_fixtures import LEGACY_H3_SELECTOR_TO_PRIMITIVE

import re
from unittest.mock import patch

from django.test import TestCase

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import (
    ModelProfileVersion,
    FieldDefinition,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProviderProfile,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.prompt_profile_compiler import (
    compile_prompt_profile_facts,
    prompt_profile_facts_fingerprint,
)
from studio.services.reference_context_facts import ReferenceSubjectFacts
from studio.services.prompt_profiles import (
    DECLARATIVE_SOURCE_PROJECTION_KEY,
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    PromptProfileError,
    publish_prompt_profile,
    refresh_prompt_profile_contract,
    validate_prompt_profile_source_projection,
)
from studio.tests.reference_rendering_fixtures import (
    rendering_config, reference_unit_fixture, reference_context_fixture, profile_compilation_fixture,
)
from studio.tests.factories import (
    H3_REF2VA_OFFICIAL_RULE_SPECS,
    build_story,
    conditioning_contract,
)


PROFILE_POSITIVE_MAX_CHARS = 7_000


class ConfiguredPromptProfileCompilerTests(TestCase):
    CUSTOM_MODE = "NebulaMotion-X"

    def setUp(self):
        self.actor, self.project, self.character, _scene, _episode, self.shot, _shot2 = (
            build_story(username="configured-prompt-compiler")
        )
        from studio.models import ShotCharacterRelation
        ShotCharacterRelation.objects.bulk_create([
            ShotCharacterRelation(shot=self.shot, character=self.character, role=role, position=0)
            for role in ("visible", "speaker")
        ])
        self.shot.dialogue[0]["voiceover"] = False
        self.shot.save(update_fields=("dialogue",))
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
        self.direction = create_field_definition(
            key="qa_configured_direction",
            label="Direction",
            category="qa",
            description="Configured direction",
            impact="Controls test output",
            value_type=FieldDefinitionVersion.ValueType.LONG_TEXT,
        )
        self.camera = create_field_definition(
            key="qa_configured_camera",
            label="Camera",
            category="qa",
            description="Configured camera",
            impact="Controls test output",
            value_type=FieldDefinitionVersion.ValueType.LONG_TEXT,
        )
        self.exclusion = create_field_definition(
            key="qa_configured_exclusion",
            label="Exclusion",
            category="qa",
            description="Configured exclusion",
            impact="Controls test output",
            value_type=FieldDefinitionVersion.ValueType.LONG_TEXT,
        )

    def _publish_profile(
        self,
        *,
        version: int,
        rules: list[dict],
        mode: str = "T2VA",
    ) -> PromptProfileVersion:
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
            create_profile_field_rule(
                profile_version=profile,
                field_definition=spec["field_definition"],
                source_selector=selector,
                fixed_value="AVOID" if selector == "fixed.value" else None,
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
        return profile

    def _compile(self, profile, *, extra_direction: str = "EXTRA"):
        return compile_prompt_profile_facts(profile, facts=profile_compilation_fixture(
            sources={"request.text": extra_direction, "request.camera": "TRACKING"},
        ))

    def _reference_facts(self, *, sources, reference_bindings, subject_facts=(), subject_keys=(), **unit_values):
        return reference_context_fixture(
            ordered_units=(reference_unit_fixture(unit_key=str(self.shot.id),
                render_values={**reference_unit_fixture().render_values, **sources},
                subject_keys=subject_keys, **unit_values),),
            mode="Ref2VA", conditioning_mode_contract=self.model.specification["conditioning_contract"]["modes"][1],
            reference_bindings=reference_bindings, subject_facts=subject_facts,
        )

    def _reviewed_context_profile(self) -> tuple[PromptProfileVersion, dict[str, PromptProfileFieldRule]]:
        """Publish the configured six-section contract used by contribution tests."""

        profile = PromptProfileVersion.objects.create(
            profile_key="qa.vendor.declarative-context",
            version=1,
            name="Declarative reviewed context",
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
                PromptProfileFieldRule.SourceSelector.REQUEST_STRUCTURED_INPUT,
                PromptProfileFieldRule.LengthUnit.ENGLISH_WORDS,
                350,
                500,
                "Write only the reviewed chronological director contribution.",
            ),
            "overall_soundscape": (
                PromptProfileFieldRule.SourceSelector.REQUEST_STRUCTURED_INPUT,
                PromptProfileFieldRule.LengthUnit.ENGLISH_WORDS,
                1,
                80,
                "Write only the reviewed diegetic sound contribution.",
            ),
            "non_diegetic_music": (
                PromptProfileFieldRule.SourceSelector.REQUEST_STRUCTURED_INPUT,
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
            definition = FieldDefinition.objects.get(key=field_key)
            generated = contribution_sources.get(field_key)
            configured_selector = PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
            rules[field_key] = create_profile_field_rule(
                profile_version=profile,
                field_definition=definition,
                source_selector=configured_selector,
                projection_primitive=LEGACY_H3_SELECTOR_TO_PRIMITIVE[selector],
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
                    "request.structured_input" if generated else configured_selector
                ),
                source_path=[field_key] if generated else None,
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
        from studio.tests.reference_rendering_fixtures import generation_structure_fixture
        generation_structure_fixture(profile, properties=tuple(contribution_sources))
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        return profile, rules

    def _contribution_values(self, *, rules, values):
        from studio.services.source_projection_primitives import contribution_target
        return {(str(self.shot.id), contribution_target(rules[key].projection_primitive)): value for key, value in values.items()}

    def test_profile_versions_change_order_labels_separators_and_channels_only_by_config(self):
        first_profile = self._publish_profile(version=1, rules=[
            {
                "field_definition": self.direction,
                "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
                "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                "position": 1,
                "label_style": PromptProfileFieldRule.LabelStyle.BRACKETED,
            },
            {
                "field_definition": self.camera,
                "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_CAMERA,
                "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                "position": 2,
                "label_style": PromptProfileFieldRule.LabelStyle.LABEL_BLOCK,
                "separator": PromptProfileFieldRule.Separator.SEMICOLON,
            },
            {
                "field_definition": self.exclusion,
                "source_selector": PromptProfileFieldRule.SourceSelector.FIXED_VALUE,
                "channel": PromptProfileFieldRule.Channel.NEGATIVE_PROMPT,
                "position": 3,
                "label_style": PromptProfileFieldRule.LabelStyle.NONE,
            },
        ])

        first = self._compile(first_profile)
        self.assertEqual(
            first.assembly["channels"]["positive_prompt"],
            "[qa_configured_direction] EXTRA; qa_configured_camera:\nTRACKING",
        )
        self.assertEqual(first.assembly["channels"]["negative_prompt"], "AVOID")
        second_profile = self._publish_profile(version=2, rules=[
            {
                "field_definition": self.exclusion,
                "source_selector": PromptProfileFieldRule.SourceSelector.FIXED_VALUE,
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
                "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_CAMERA,
                "channel": PromptProfileFieldRule.Channel.NEGATIVE_PROMPT,
                "position": 3,
                "label_style": PromptProfileFieldRule.LabelStyle.BRACKETED,
            },
        ])

        second = self._compile(second_profile)
        self.assertEqual(
            second.assembly["channels"]["positive_prompt"],
            "qa_configured_exclusion:\nAVOID EXTRA",
        )
        self.assertEqual(second.assembly["channels"]["negative_prompt"], "[qa_configured_camera] TRACKING")

    def test_profile_field_max_chars_blocks_without_model_specific_code(self):
        profile = self._publish_profile(version=1, rules=[{
            "field_definition": self.direction,
            "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            "position": 1,
            "label_style": PromptProfileFieldRule.LabelStyle.NONE,
            "max_chars": 5,
        }])

        with self.assertRaises(PromptProfileError) as error:
            self._compile(profile, extra_direction="TOO-LONG")
        self.assertEqual(error.exception.code, "prompt_profile_field_too_long")

    def test_direct_v1_accepts_fictional_model_family_and_mode_from_configuration(self):
        profile = self._publish_profile(
            version=1,
            mode=self.CUSTOM_MODE,
            rules=[
                {
                    "field_definition": self.camera,
                    "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_CAMERA,
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

        result = compile_prompt_profile_facts(
            profile,
            facts=profile_compilation_fixture(sources={"request.text": "CONFIGURED DIRECTION", "request.camera": "TRACKING"}),
        )

        self.assertEqual(profile.model_family, "nebula_motion")
        self.assertEqual(profile.mode, self.CUSTOM_MODE)
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

    def test_direct_v1_fingerprint_uses_only_explicit_facts(self):
        from dataclasses import replace
        from studio.services.prompt_profile_compiler import PromptProfileCompilationFacts
        facts = PromptProfileCompilationFacts(sources={"request.extra_direction": "FICTIONAL DIRECT INPUT"},
            source_language="English", target_language="English", reference_context=None,
            reviewed_values={}, generation_contributions={})
        with patch("studio.services.prompt_profile_compiler.compile_reference_context",
                   side_effect=AssertionError("direct facts do not invoke the renderer")) as renderer:
            first = prompt_profile_facts_fingerprint(facts)
            second = prompt_profile_facts_fingerprint(replace(facts, sources={"request.extra_direction": "CHANGED"}))
        self.assertNotEqual(first, second)
        self.assertEqual(first, prompt_profile_facts_fingerprint(facts))
        renderer.assert_not_called()

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
        create_profile_field_rule(
            profile_version=profile,
            field_definition=FieldDefinition.objects.get(key="subject_definitions"),
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
        create_profile_field_rule(
            profile_version=profile,
            field_definition=self.camera,
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_CAMERA,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            label_style=PromptProfileFieldRule.LabelStyle.NONE,
            max_chars=100,
            generation_mode=PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
            generation_source_selector=(
                PromptProfileFieldRule.SourceSelector.REQUEST_TEXT
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

        reviewed = "LOCKED REVIEWED CAMERA"
        result = compile_prompt_profile_facts(
            profile,
            facts=profile_compilation_fixture(sources={"request.camera": "TRACKING"},
                reviewed_values={self.camera.key: reviewed}),
        )

        self.assertEqual(result.assembly["channels"]["positive_prompt"], reviewed)
        self.assertNotIn("camera", {field.name for field in self.shot._meta.get_fields()})
        self.assertIsNone(result.reviewed_field_evidence)  # Pure compilation does not invent selection evidence.

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
            definition = create_field_definition(
                key=f"qa_value_contract_{suffix}",
                label=f"Value contract {suffix}",
                category="qa",
                description="Schema v2 runtime value contract",
                impact="Rejects invalid configured prompt values",
                value_type=FieldDefinitionVersion.ValueType.LONG_TEXT,
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
            rule = create_profile_field_rule(
                profile_version=profile,
                field_definition=definition,
                source_selector=source_selector,
                fixed_value=default_value if source_selector == "fixed.value" else None,
                has_default=default_value is not None and source_selector != "fixed.value",
                default_value=default_value if source_selector != "fixed.value" else None,
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=1,
                label_style=PromptProfileFieldRule.LabelStyle.NONE,
                required=False,
                omit_if_empty=True,
                max_chars=1000,
                generation_mode=generation_mode,
                generation_source_selector=(
                    PromptProfileFieldRule.SourceSelector.REQUEST_TEXT
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
            selected_values=None,
        ):
            return compile_prompt_profile_facts(
                profile,
                facts=profile_compilation_fixture(sources={"request.text": extra_direction, "request.camera": "TRACKING"},
                    reviewed_values=selected_values),
            )

        required_profile, _required_rule = publish_single_rule(
            "server-required",
            source_selector=PromptProfileFieldRule.SourceSelector.FIXED_VALUE,
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
            source_selector=PromptProfileFieldRule.SourceSelector.FIXED_VALUE,
            default_value="{{pending}}",
            generation_mode=PromptProfileFieldRule.GenerationMode.SERVER_LOCKED,
            generation_required=True,
            language_policy="any",
            validators=[
                PromptProfileFieldRule.GenerationValidator.NO_UNRESOLVED_PLACEHOLDERS,
            ],
        )
        self.assertIn("{{pending}}", str(compile_direct(validator_profile).assembly))

        language_profile, _language_rule = publish_single_rule(
            "passthrough-language",
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            generation_mode=PromptProfileFieldRule.GenerationMode.PASSTHROUGH,
            generation_required=True,
            language_policy="en",
        )
        self.assertIn("仅中文说明", str(compile_direct(language_profile, extra_direction="仅中文说明").assembly))

        preserved_profile, preserved_rule = publish_single_rule(
            "passthrough-preserved",
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            generation_mode=PromptProfileFieldRule.GenerationMode.PASSTHROUGH,
            generation_required=True,
            language_policy="preserve_source",
            validators=[PromptProfileFieldRule.GenerationValidator.NO_UNRESOLVED_PLACEHOLDERS],
        )
        # The Chinese project can consume an unchanged English or mixed source.
        self.project.language = "Chinese"
        for text in ("Maintain natural light and balanced exposure.", "Natural light，保留原文"):
            with self.subTest(preserved=text):
                result = compile_direct(preserved_profile, extra_direction=text)
                self.assertEqual(result.assembly["structured_fields"][preserved_rule.field_definition.key], text)
        self.assertIn("{{unresolved}}", str(compile_direct(preserved_profile, extra_direction="{{unresolved}}").assembly))

        final_profile, final_rule = publish_single_rule(
            "reviewed-final-length",
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_CAMERA,
            generation_mode=PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
            generation_required=True,
            language_policy="en",
            length_unit=PromptProfileFieldRule.LengthUnit.CHARACTERS,
            generation_min=10,
            generation_max=20,
            validators=[PromptProfileFieldRule.GenerationValidator.NON_EMPTY],
        )

        reviewed = "short"
        compiled = compile_direct(final_profile, selected_values={final_rule.field_definition.key: reviewed})
        self.assertEqual(compiled.generation_length_audit[0]["status"], "warning")
        self.assertEqual(compiled.generation_length_audit[0]["enforcement"], "advisory")

        advisory_profile, advisory_rule = publish_single_rule(
            "reviewed-advisory-length",
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_CAMERA,
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

        compiled = compile_direct(
            advisory_profile,

            selected_values={advisory_rule.field_definition.key: reviewed},
        )
        audit = next(
            row
            for row in compiled.generation_length_audit
            if row["field_key"] == advisory_rule.field_definition.key
        )
        self.assertEqual(audit["status"], "warning")
        self.assertEqual(audit["enforcement"], "advisory")

    def test_explicit_english_reference_declaration_with_chinese_context_preserves_difference(self):
        """Language instructions do not block new template text or rewrite source facts."""
        from copy import deepcopy

        config = rendering_config()
        rendering = config["primitive_options"]["reference_context_ir_v1"]["rendering"]
        rendering["templates"]["retention"] = "Reference image {label}: {relationship}. {usage}"
        profile = PromptProfileVersion.objects.create(
            profile_key="qa.explicit-reference-language", version=1,
            name="Explicit English reference declaration", model_family="nebula_motion",
            operation="video.generate", mode="Ref2VA",
            source_projection_key=DECLARATIVE_SOURCE_PROJECTION_KEY,
            source_projection_config=config, review_state=PromptProfileVersion.ReviewState.READY,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        declaration = create_profile_field_rule(
            profile_version=profile, field_definition=self.direction,
            source_selector="projection.output", projection_primitive="reference_context.retention_analysis",
            channel="positive_prompt", position=1, label_style="none", separator="newline",
            required=True, max_chars=2000, generation_mode="passthrough",
            generation_source_selector="projection.output", language_policy="en",
        )
        create_profile_field_rule(
            profile_version=profile, field_definition=self.camera, source_selector="request.subject",
            channel="positive_prompt", position=2, label_style="none", separator="newline",
            required=True, max_chars=2000, generation_mode="passthrough",
            generation_source_selector="request.subject", language_policy="preserve_source",
        )
        create_profile_field_rule(
            profile_version=profile, field_definition=self.exclusion, source_selector="fixed.value",
            fixed_value="", channel="negative_prompt", position=3, label_style="none", separator="newline",
            required=False, omit_if_empty=True, max_chars=2000, generation_mode="passthrough",
            generation_source_selector="fixed.value", language_policy="preserve_source",
        )
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        binding = {
            "category": "character", "entity_id": "fixture-person", "entity_name": "蓝夹克人物",
            "asset_id": "fixture-primary", "id": "fixture-primary", "name": "中文素材名称",
            "description": "明确的中文素材说明", "prompt_hint": "", "media_metadata": {},
            "input_slot": 1, "input_label": "<Picture 1>", "entity_label": "<Subject 1>",
        }
        for difference in (
            "Change the blue jacket to a bright orange jacket. Preserve the same person.",
            "Change the blue jacket to a green jacket. Preserve the same person.",
        ):
            with self.subTest(difference=difference):
                facts = profile_compilation_fixture(
                    sources={"request.subject": difference}, source_language="Chinese", target_language="Chinese",
                    reference_context=self._reference_facts(sources={}, reference_bindings=(binding,)),
                )
                before = deepcopy(facts)
                compiled = compile_prompt_profile_facts(profile, facts=facts)
                self.assertEqual(compiled.assembly["structured_fields"][self.camera.key], difference)
                self.assertIn("<Picture 1>", compiled.assembly["structured_fields"][self.direction.key])
                self.assertNotIn("中文", compiled.assembly["channels"]["positive_prompt"])
                self.assertFalse(compiled.assembly["channels"]["negative_prompt"])
                self.assertEqual(facts, before)

        # Negative controls also follow immutable successor authoring.
        from studio.services.prompt_profiles import clone_prompt_profile_successor
        negative = clone_prompt_profile_successor(profile,
            expected_source_contract_sha256=profile.contract_sha256, name="Inherited projection control")
        declaration = negative.field_rules.get(field_definition=self.direction)
        declaration.language_policy = "preserve_source"
        declaration.save(update_fields=("language_policy",))
        negative.review_state = PromptProfileVersion.ReviewState.READY
        negative.save(update_fields=("review_state",))
        refresh_prompt_profile_contract(negative)
        negative = publish_prompt_profile(negative, actor=self.actor)
        self.assertEqual(compile_prompt_profile_facts(negative, facts=facts).assembly["channels"],
                         compile_prompt_profile_facts(profile, facts=facts).assembly["channels"])
        changed = clone_prompt_profile_successor(negative,
            expected_source_contract_sha256=negative.contract_sha256, name="Non-English declaration control")
        declaration = changed.field_rules.get(field_definition=self.direction)
        declaration.language_policy = "en"
        declaration.save(update_fields=("language_policy",))
        rendering["templates"]["retention"] = "参考图 {label}: {relationship}. {usage}"
        changed.source_projection_config = config
        changed.review_state = PromptProfileVersion.ReviewState.READY
        changed.save(update_fields=("source_projection_config", "review_state"))
        refresh_prompt_profile_contract(changed)
        changed = publish_prompt_profile(changed, actor=self.actor)
        self.assertIn("参考图", compile_prompt_profile_facts(changed, facts=facts).assembly["channels"]["positive_prompt"])

    def test_reviewed_contributions_are_projected_into_complete_configured_sections(self):
        self.shot.conditioning_mode = "Ref2VA"
        self.shot.save(update_fields=(
            "conditioning_mode",
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
                PromptProfileFieldRule.SourceSelector.REQUEST_STRUCTURED_INPUT,
                "Write the reviewed visible chronological detail.",
            ),
            "overall_soundscape": (
                PromptProfileFieldRule.SourceSelector.REQUEST_STRUCTURED_INPUT,
                "Write the reviewed diegetic sound contribution.",
            ),
            "non_diegetic_music": (
                PromptProfileFieldRule.SourceSelector.REQUEST_STRUCTURED_INPUT,
                "Write the reviewed non-diegetic music contribution.",
            ),
        }
        for position, (selector, field_key, label_style) in enumerate(
            H3_REF2VA_OFFICIAL_RULE_SPECS,
            start=1,
        ):
            definition = FieldDefinition.objects.get(key=field_key)
            generated = contribution_sources.get(field_key)
            create_profile_field_rule(
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
                generation_source_selector=("request.structured_input" if generated else "projection.output"),
                source_path=[field_key] if generated else None,
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
        from studio.tests.reference_rendering_fixtures import generation_structure_fixture
        generation_structure_fixture(profile, properties=tuple(contribution_sources))
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        reviewed_values = {
            "detailed_description": "REVIEWED VISIBLE DETAIL",
            "overall_soundscape": "REVIEWED DIEGETIC SOUND",
            "non_diegetic_music": "REVIEWED NON-DIEGETIC MUSIC",
        }
        # Contribution receivers are primitive-owned slots, not authorable source selectors.
        from studio.services.source_projection_primitives import contribution_target
        contributions = {
            (str(self.shot.id), contribution_target(f"reference_context.{key}")): value
            for key, value in reviewed_values.items()
        }
        character = self.character
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
        result = compile_prompt_profile_facts(profile, facts=profile_compilation_fixture(sources={}, reference_context=self._reference_facts(
                sources={"description": "ORIGINAL DETAIL", "sound_effects": "ORIGINAL SOUND",
                         "music": "ORIGINAL MUSIC"},
                reference_bindings=(reference_binding,),
                subject_facts=(ReferenceSubjectFacts("character", str(character.id), ("Explicit character anchor",)),),
                subject_keys=(("character", str(character.id)),)),
            generation_contributions=contributions))

        prompt = result.assembly["channels"]["positive_prompt"]
        self.assertIn("detailed_description:", prompt)
        self.assertIn("REVIEWED VISIBLE DETAIL", prompt)
        self.assertIn("REVIEWED DIEGETIC SOUND", prompt)
        self.assertIn("REVIEWED NON-DIEGETIC MUSIC", prompt)
        self.assertNotIn("ORIGINAL DETAIL", prompt)
        self.assertNotIn("ORIGINAL SOUND", prompt)
        self.assertNotIn("ORIGINAL MUSIC", prompt)
        self.assertEqual(
            result.model_prompt_contract["format"],
            "reference_context_ir_v2",
        )
        self.assertIsNone(result.reviewed_field_evidence)

    def test_declarative_projection_builds_six_sections_and_consumes_three_contributions(self):
        self.shot.conditioning_mode = "Ref2VA"
        self.shot.save(update_fields=(
            "conditioning_mode",
            "updated_at",
        ))
        profile, rules = self._reviewed_context_profile()
        reviewed_detail = " ".join(
            ["reviewed visible timeline"] * 120,
        )
        reviewed_values = {
            "detailed_description": reviewed_detail,
            "overall_soundscape": "REVIEWED DECLARATIVE SOUND",
            "non_diegetic_music": "REVIEWED DECLARATIVE MUSIC",
        }
        contributions = self._contribution_values(
            rules=rules,

            values=reviewed_values,
        )
        character = self.character
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

        result = compile_prompt_profile_facts(profile, facts=profile_compilation_fixture(sources={}, reference_context=self._reference_facts(
                sources={"description": "ORIGINAL DECLARATIVE DETAIL",
                         "sound_effects": "ORIGINAL DECLARATIVE SOUND", "music": "ORIGINAL DECLARATIVE MUSIC"},
                reference_bindings=(reference_binding,),
                subject_facts=(ReferenceSubjectFacts("character", str(character.id), ("Explicit character anchor",)),),
                subject_keys=(("character", str(character.id)),)),
            generation_contributions=contributions))

        expected_fields = [
            field_key
            for _selector, field_key, _label_style
            in H3_REF2VA_OFFICIAL_RULE_SPECS
        ]
        model_contract = result.model_prompt_contract
        self.assertEqual(model_contract["format"], "reference_context_ir_v2")
        self.assertEqual(model_contract["section_order"], expected_fields)
        self.assertTrue(
            set(expected_fields).issubset(result.structured_fields),
        )
        self.assertIn(reviewed_detail, result.assembly["channels"]["positive_prompt"])
        self.assertIn("REVIEWED DECLARATIVE SOUND", result.assembly["channels"]["positive_prompt"])
        self.assertIn("REVIEWED DECLARATIVE MUSIC", result.assembly["channels"]["positive_prompt"])
        self.assertNotIn("ORIGINAL DECLARATIVE DETAIL", result.assembly["channels"]["positive_prompt"])
        self.assertNotIn("ORIGINAL DECLARATIVE SOUND", result.assembly["channels"]["positive_prompt"])
        self.assertNotIn("ORIGINAL DECLARATIVE MUSIC", result.assembly["channels"]["positive_prompt"])
        self.assertIsNone(result.reviewed_field_evidence)

    def test_profile_motion_advisory_reaches_projection_quality_without_blocking(self):
        profile, _rules = self._reviewed_context_profile()
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

        compiled = compile_prompt_profile_facts(profile, facts=profile_compilation_fixture(sources={}, reference_context=self._reference_facts(
                sources={"description": "Explicit motion sequence",
                         "sound_effects": "Rain against glass", "music": "Low strings"},
                reference_bindings=(reference_binding,))))
        quality = compiled.assembly
        self.assertTrue(any(
            "运镜参考说明未达到本策略建议值" in warning
            for warning in quality.get("warnings", [])
        ))
        self.assertTrue(compiled.assembly["channels"]["positive_prompt"])

    def test_reviewed_detail_contribution_owns_creative_prose_without_repeating_locked_sections(self):
        from dataclasses import replace
        from copy import deepcopy

        reference_bindings = tuple({
            "category": kind, "entity_id": identity, "entity_name": identity,
            "asset_id": "fixture-" + identity, "id": "fixture-" + identity,
            "name": "explicit " + kind + " reference", "description": "", "prompt_hint": "",
            "media_metadata": {}, "input_slot": index, "input_label": f"<Picture {index}>",
            "entity_label": f"<Subject {index}>",
        } for index, (kind, identity) in enumerate(
            (("character", "character-1"), ("scene", "scene-1"), ("prop", "prop-1")), 1)) + ({
            "category": "video", "entity_id": "", "entity_name": "",
            "asset_id": "fixture-motion-video", "id": "fixture-motion-video",
            "name": "same-story camera reference", "description": "",
            "prompt_hint": "Use the slow forward dolly with restrained handheld sway and one measured rack focus near the end",
            "media_metadata": {"fps": 24}, "purpose": "motion_camera_reference",
            "input_slot": 1, "input_label": "<Video 1>", "entity_label": "<Video 1>",
        },)
        profile, rules = self._reviewed_context_profile()
        subjects = (ReferenceSubjectFacts("character", "character-1", ("Original character",)),
                    ReferenceSubjectFacts("scene", "scene-1", ("Original archive room",)),
                    ReferenceSubjectFacts("prop", "prop-1", ("Original charred letter",)))
        context = self._reference_facts(
            sources={"visual_prompt": "Restrained detective noir",
                     "environment": "Original archive room",
                     "description": "ORIGINAL DETERMINISTIC DETAIL",
                     "sound_effects": "ORIGINAL DETERMINISTIC SOUND", "music": "ORIGINAL DETERMINISTIC MUSIC"},
            subject_facts=subjects, subject_keys=(("character", "character-1"), ("scene", "scene-1"), ("prop", "prop-1")),
            reference_bindings=reference_bindings, continuity_notes="ORIGINAL VISIBLE CONTINUITY CHECKLIST",
            requires_first_frame_continuity=True, requires_last_frame_continuity=True,
            dialogue=({"speaker_id": "S1", "text": "这封信不是昨晚烧的。", "delivery": "controlled whisper",
                       "language": "Chinese", "speaker_subject_key": ["character", "character-1"],
                       "voiceover": False, "start_ms": 123, "end_ms": 987, "subject": "<Subject 1>",
                       "voice_profile": "low calm voice"},), voice_profiles=("low calm voice",),
        )
        # Explicit source rendering is not a claim that a missing required
        # candidate can pass the Preparation/selection boundary.
        baseline = compile_prompt_profile_facts(profile, facts=profile_compilation_fixture(sources={}, reference_context=context))
        baseline_detail = baseline.assembly["structured_fields"]["detailed_description"]
        self.assertIn("Environment:", baseline_detail)
        self.assertIn("ORIGINAL DETERMINISTIC SOUND", baseline.assembly["structured_fields"]["overall_soundscape"])
        self.assertIn("ORIGINAL VISIBLE CONTINUITY CHECKLIST", baseline_detail)

        anchor_clause = (
            "precise locked visual evidence with asymmetric weathering, stable "
            "material response, controlled amber highlights, measured scale, "
            "repeatable silhouette, and production continuity markers"
        )
        context = replace(context, subject_facts=tuple(
            replace(subject, description_parts=(kind.upper() + "_LOCKED_ANCHOR " + " ".join([anchor_clause] * 2),))
            for kind, subject in zip(("character", "scene", "prop"), subjects, strict=True)
        ))
        contribution_words = (
            "camera observes deliberate motion through rain as the investigator "
            "opens evidence and follows a controlled visual beat with precise "
            "framing natural posture restrained expression and readable action"
        ).split()
        reviewed_detail = " ".join((contribution_words * 24)[:389])
        self.assertEqual(len(re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)?", reviewed_detail)), 389)
        reviewed_sound = "Muted rain and paper friction stay synchronized to the visible hand movement."
        reviewed_music = "A sparse low cello pulse remains non-diegetic and restrained."
        contributions = self._contribution_values(rules=rules, values={
            "detailed_description": reviewed_detail,
            "overall_soundscape": reviewed_sound, "non_diegetic_music": reviewed_music,
        })
        facts = profile_compilation_fixture(sources={}, reference_context=context, generation_contributions=contributions)
        before = deepcopy(facts)
        result = compile_prompt_profile_facts(profile, facts=facts)
        self.assertEqual(facts, before)

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
