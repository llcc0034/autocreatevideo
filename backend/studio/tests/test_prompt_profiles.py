from __future__ import annotations

from studio.tests.reference_rendering_fixtures import LEGACY_H3_SELECTOR_TO_PRIMITIVE

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from studio.tests.reference_rendering_fixtures import FRAME_POLICY
from studio.models import (
    Asset,
    CharacterBible,
    ModelProfileVersion,
    PlatformLifecycleAuditEvent,
    PromptArtifact,
    PromptFieldDefinition,
    PromptFieldTranslation,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    PropBible,
    ProviderProfile,
    Shot,
)
from studio.platform_serializers import PromptProfileFieldRuleSerializer
from studio.services.configuration import (
    create_config_revision,
    publish_config_revision,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.production_pipeline import compile_prompt_input
from studio.services.prompt_generation_sources import (
    PROMPT_GENERATION_SOURCE_VALUES,
)
from studio.services.prompt_profiles import (
    CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY,
    DECLARATIVE_SOURCE_PROJECTION_KEY,
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    PromptProfileError,
    PromptProfileResolutionError,
    assemble_prompt_profile,
    media_profile_sources,
    model_family_for_profile,
    publish_prompt_profile,
    prompt_profile_contract_payload,
    refresh_prompt_profile_contract,
    require_prompt_profile_binding,
    resolve_prompt_profile_binding,
    validate_prompt_profile_binding,
    validate_prompt_profile_source_projection,
    validate_profile_rule,
    verify_frozen_profile_reference,
    verify_prompt_profile_contract,
)
from studio.services.source_projection_primitives import (
    REFERENCE_CONTEXT_ENGINE_KEY,
)
from studio.services.prompt_quality_gate import evaluate_prompt_language_gate
from studio.services.prompt_translation import (
    TRANSLATION_CONTEXT_VERSION,
    build_translation_context,
    source_text_hash,
    translation_context_hash,
)
from studio.services.series_concept_drafts import build_series_concept_plan
from studio.services.text_generation_queue import provider_payload
from studio.tests.factories import (
    H3_REF2VA_OFFICIAL_RULE_SPECS,
    build_story,
    conditioning_contract,
    series_concept_field_contract,
    series_concept_resource_advisory,
)


def _video_conditioning_contract() -> dict:
    modes = [
        conditioning_contract("T2VA")["modes"][0],
        conditioning_contract(
            "I2VA", input_kind="frame_slots", first_frame=(0, 1),
        )["modes"][0],
        conditioning_contract(
            "L2VA", input_kind="frame_slots", last_frame=(0, 1),
        )["modes"][0],
        conditioning_contract(
            "FL2VA", input_kind="frame_slots",
            first_frame=(0, 1), last_frame=(0, 1),
        )["modes"][0],
        conditioning_contract(
            "Ref2VA",
            input_kind="classified_references",
            requires_bible=True,
            entity_images=(0, 9),
            reference_videos=(0, 3),
            paired_video_audios=(0, 3),
            reference_audios=(0, 3),
        )["modes"][0],
    ]
    return {
        "schema_version": 1,
        "adapter_key": "media_conditioning_v1",
        "modes": modes,
    }


def _static_reference_config_v2() -> dict:
    return {
        "schema_version": 2,
        "static_reference_policy": {
            "snapshot_selector": "initial",
            "default_target_kind": "character",
            "allow_context": {"default": False, "target_overrides": {}},
            "context_source_selectors": ["request.environment"],
            "transition_markers": [" changes to "],
            "transition_patterns": [],
            "context_clause_policy": {
                "delimiters": [",", ";"],
                "discard_markers": [],
            },
            "negative_token_exclusions": {},
            "forbidden_markers": {
                "always": [],
                "when_context_disallowed": [],
            },
            "violation_action": "block",
            "initial_snapshot_clause_policy": {
                "delimiters": [",", ";"],
                "initial_state_markers": [" initially ", " at first "],
                "continuity_source_selector": "request.action",
                "trailing_clause_action": "relocate_to_continuity",
            },
        },
    }


def _h3_quality_projection_config() -> dict:
    return {
        "schema_version": 1,
        "classified_reference_context_policy": {
            "retention_analysis": {
                "include_subject_shot_usage": True,
                "assignment_separator": " - ",
            },
            "detailed_description": {
                "style_opening_placement": "before_first_shot",
                "style_opening_max_sentences": 2,
                "subject_reference_placement": "first_clear_appearance",
            },
        },
        "quality_advisories": {
            "schema_version": 1,
            "positive_negative_token_overlap": {
                "minimum_shared_tokens": 6,
                "minimum_overlap_ratio": 0.65,
            },
            "short_shot_action_density": {
                "maximum_duration_ms": 5_500,
                "maximum_action_characters": 420,
                "minimum_clause_separators": 6,
            },
            "motion_camera_reference_guidance": {
                "minimum_english_words": 5,
                "generic_values": ["reference video", "workflow smoke fixture"],
            },
        },
    }


def _h3_declarative_projection_config() -> dict:
    from studio.tests.reference_rendering_fixtures import rendering_config
    return rendering_config()


class PromptProfileServiceTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(
            username="prompt-profile-admin",
            password="StrongPass123!",
            email="prompt-profile@example.test",
        )

    def _field(
        self,
        key="qa_theme",
        *,
        label="QA Theme",
        default=None,
        value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
    ):
        return PromptFieldDefinition.objects.create(
            key=key,
            label=label,
            category="qa",
            description="Test-only field",
            impact="Affects the assembled test prompt",
            value_type=value_type,
            default_value=default,
            value_options=[],
            is_active=True,
        )

    def _profile(
        self,
        key="qa.minimax.t2va",
        *,
        family="minimax_h3",
        operation="video.generate",
        mode="T2VA",
        source_projection_key="direct_v1",
        review_state=PromptProfileVersion.ReviewState.READY,
    ):
        profile = PromptProfileVersion.objects.create(
            profile_key=key,
            version=1,
            name=key,
            model_family=family,
            operation=operation,
            mode=mode,
            source_projection_key=source_projection_key,
            review_state=review_state,
        )
        refresh_prompt_profile_contract(profile)
        return profile

    def _rule(
        self,
        profile,
        definition,
        *,
        selector="request.theme",
        channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
        position=1,
    ):
        rule = PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=definition,
            source_selector=selector,
            generation_source_selector=selector,
            channel=channel,
            position=position,
            max_chars=4000,
        )
        refresh_prompt_profile_contract(profile)
        return rule

    def _publish(self, profile):
        return publish_prompt_profile(profile, actor=self.actor)

    def _confirmed_overlay(
        self, *, project, entity, scope_type, field_key, source, translated,
    ):
        return PromptFieldTranslation.objects.create(
            project=project,
            scope_type=scope_type,
            scope_id=entity.id,
            field_key=field_key,
            source_text=source,
            source_hash=source_text_hash(source),
            translated_text=translated,
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata={
                "translation_context_hash": translation_context_hash(
                    build_translation_context(
                        project,
                        scope_type=scope_type,
                        scope_id=entity.id,
                        field_key=field_key,
                        source_text=source,
                    )
                ),
                "translation_context_version": TRANSLATION_CONTEXT_VERSION,
            },
            confirmed_by=self.actor,
        )

    def _model_profile(self, *, capabilities=("script.generate",)):
        provider = ProviderProfile(
            key="qa-deepseek-provider",
            name="QA DeepSeek Provider",
            driver_type=ProviderProfile.DriverType.DEEPSEEK_API,
            modalities="text",
            operations="chat_completion,script.generate",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {})
        provider.save()
        model = ModelProfileVersion(
            profile_key="qa-deepseek-model",
            version=1,
            provider=provider,
            model_identifier="deepseek-qa",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(model, "specification", {
            "schema_version": 1,
            "model_family": "deepseek",
            "capabilities": list(capabilities),
        })
        model.save()
        return model

    def _video_model_profile(self):
        provider = ProviderProfile(
            key="qa-explicit-video-provider",
            name="QA Explicit Video Provider",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="video",
            operations="health,object_info,submit,poll,cancel,collect",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {})
        provider.save()
        model = ModelProfileVersion(
            profile_key="qa-explicit-video-model",
            version=1,
            provider=provider,
            model_identifier="qa-configured-video-v1",
            modality=ModelProfileVersion.Modality.VIDEO,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(model, "specification", {
            "schema_version": 1,
            "model_family": "minimax_h3",
            "capabilities": ["video.generate"],
            "conditioning_contract": _video_conditioning_contract(),
        })
        model.save()
        return model

    def test_seeded_profiles_are_review_only_drafts_and_never_auto_bind(self):
        expected = {
            "minimax_h3.video.t2va",
            "minimax_h3.video.i2va",
            "minimax_h3.video.fl2va",
            "minimax_h3.video.l2va",
            "minimax_h3.video.ref2va",
            "sdxl.image.general",
            "deepseek.script.generate",
            "qwen.script.generate",
            "deepseek.script.structure",
            "qwen.script.structure",
            "deepseek.prompt.normalize",
            "qwen.prompt.normalize",
        }
        rows = PromptProfileVersion.objects.filter(profile_key__in=expected)
        self.assertEqual(set(rows.values_list("profile_key", flat=True)), expected)
        self.assertFalse(rows.exclude(status=PromptProfileVersion.Status.DRAFT).exists())
        self.assertFalse(rows.exclude(
            review_state=PromptProfileVersion.ReviewState.NEEDS_REVIEW,
        ).exists())
        self.assertFalse(rows.filter(official_reference="").exists())
        self.assertFalse(PromptProfileBinding.objects.filter(
            prompt_profile_version__in=rows,
        ).exists())
        structure_rows = rows.filter(operation="script.structure")
        self.assertEqual(structure_rows.count(), 2)
        for profile in structure_rows:
            self.assertEqual(
                set(profile.field_rules.values_list("field_definition__key", flat=True)),
                {"system_guidance", "theme", "narrative", "language"},
            )
            self.assertEqual(profile.field_rules.count(), 4)
            self.assertTrue(profile.contract_sha256)
        self.assertGreaterEqual(PromptFieldDefinition.objects.count(), 30)

    def test_assembly_uses_allow_list_defaults_and_required_fields(self):
        definition = self._field(default="default theme")
        profile = self._profile()
        self._rule(profile, definition)
        profile = self._publish(profile)

        assembled = assemble_prompt_profile(profile, sources={})
        self.assertEqual(assembled["structured_fields"], {"qa_theme": "default theme"})
        self.assertEqual(assembled["channels"]["positive_prompt"], "qa_theme: default theme")
        self.assertTrue(assembled["trace"][0]["defaulted"])

        with self.assertRaisesRegex(ValidationError, "未授权 selector"):
            assemble_prompt_profile(profile, sources={"__class__": "unsafe"})

    def test_custom_operation_publish_requires_capability_and_exact_binding(self):
        operation = "storyboard.render_v2"
        family = "fictional_story_model"
        definition = self._field(key="qa_custom_operation_theme")
        profile = self._profile(
            "qa.fictional.storyboard-render",
            family=family,
            operation=operation,
            mode="",
        )
        self._rule(profile, definition)

        with self.assertRaises(PromptProfileError) as missing_evidence:
            self._publish(profile)
        self.assertEqual(
            missing_evidence.exception.code,
            "prompt_profile_operation_evidence_missing",
        )

        model = self._model_profile(capabilities=(operation,))
        assign_json_contract(model, "specification", {
            "schema_version": 1,
            "model_family": family,
            "capabilities": [operation],
        })
        model.save()
        profile = self._publish(profile)

        generic_binding = PromptProfileBinding(
            key="qa.fictional.storyboard-render.generic",
            name="Unsafe generic custom binding",
            operation=operation,
            model_family=family,
            mode="",
            prompt_profile_version=profile,
            is_active=True,
        )
        with self.assertRaises(PromptProfileError) as generic_rejected:
            validate_prompt_profile_binding(generic_binding)
        self.assertEqual(
            generic_rejected.exception.code,
            "prompt_profile_custom_operation_exact_model_required",
        )

        exact_binding = PromptProfileBinding.objects.create(
            key="qa.fictional.storyboard-render.exact",
            name="Exact custom binding",
            operation=operation,
            model_family=family,
            mode="",
            model_profile_version=model,
            prompt_profile_version=profile,
            is_active=True,
        )
        validate_prompt_profile_binding(exact_binding)
        self.assertIsNone(resolve_prompt_profile_binding(
            operation=operation,
            model_family=family,
        ))
        self.assertEqual(
            require_prompt_profile_binding(
                operation=operation,
                model_family=family,
                model_profile_version=model,
            ).binding.pk,
            exact_binding.pk,
        )

    def test_provider_description_is_not_evidence_but_registered_driver_capability_is(self):
        operation = "narrative.direct_v1"
        provider = ProviderProfile(
            key="qa-operation-evidence-provider",
            name="QA operation evidence provider",
            driver_type=ProviderProfile.DriverType.DEEPSEEK_API,
            modalities="text",
            operations=operation,
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {})
        provider.save()
        profile = self._profile(
            "qa.provider-operation-evidence",
            family="fictional_provider_family",
            operation=operation,
            mode="",
        )
        self._rule(profile, self._field(key="qa_provider_operation_theme"))

        with self.assertRaises(PromptProfileError) as descriptive_only:
            self._publish(profile)
        self.assertEqual(
            descriptive_only.exception.code,
            "prompt_profile_operation_evidence_missing",
        )

        registered_driver = SimpleNamespace(
            capabilities=lambda: {
                "schema_version": 1,
                "operations": [operation],
            },
        )
        with patch(
            "studio.services.provider_registry.driver_for",
            return_value=registered_driver,
        ):
            published = self._publish(profile)
        self.assertEqual(published.status, PromptProfileVersion.Status.PUBLISHED)

    def test_classified_projection_is_read_only_and_cannot_be_executed_or_published(self):
        subject = self._field(key="qa_ref2va_subject")
        profile = self._profile(
            "qa.minimax.ref2va.context-ir",
            mode="Ref2VA",
            source_projection_key="classified_reference_context_ir_v1",
        )
        self._rule(profile, subject, selector="request.subject", position=1)

        with self.assertRaises(PromptProfileError) as missing:
            validate_prompt_profile_source_projection(profile)
        self.assertEqual(
            missing.exception.code,
            "prompt_profile_source_projection_unregistered",
        )

        profile.field_rules.all().delete()
        for position, (selector, field_key, label_style) in enumerate(
            H3_REF2VA_OFFICIAL_RULE_SPECS,
            start=1,
        ):
            definition = PromptFieldDefinition.objects.get(key=field_key)
            PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector=selector,
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=position,
                label_style=label_style,
                separator=PromptProfileFieldRule.Separator.NEWLINE,
                required=True,
                omit_if_empty=False,
                max_chars=7000,
            )
        refresh_prompt_profile_contract(profile)

        with self.assertRaises(PromptProfileError) as unavailable:
            validate_prompt_profile_source_projection(profile)
        self.assertEqual(unavailable.exception.code, "prompt_profile_source_projection_unregistered")
        with self.assertRaises(PromptProfileError) as runtime_only:
            self._publish(profile)
        self.assertEqual(
            runtime_only.exception.code,
            "prompt_profile_source_projection_runtime_only",
        )

    def test_declarative_projection_final_shape_and_generation_split_are_configured(self):
        profile = self._profile(
            "qa.configured.declarative-context",
            mode="Ref2VA",
            source_projection_key=DECLARATIVE_SOURCE_PROJECTION_KEY,
        )
        profile.contract_schema_version = PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        profile.source_projection_config = _h3_declarative_projection_config()
        profile.save(update_fields=(
            "contract_schema_version",
            "source_projection_config",
            "updated_at",
        ))
        configured = list(reversed(H3_REF2VA_OFFICIAL_RULE_SPECS))
        projected_values: dict[str, str] = {}
        for position, (selector, _canonical_key, _canonical_label) in enumerate(
            configured,
            start=1,
        ):
            field_key = f"qa_configured_section_{position}"
            definition = self._field(key=field_key, label=f"Configured {position}")
            model_generated = position in {1, 4}
            PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector=(
                    PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
                ),
                projection_primitive=(
                    LEGACY_H3_SELECTOR_TO_PRIMITIVE[selector]
                ),
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=position,
                label_style=(
                    PromptProfileFieldRule.LabelStyle.BRACKETED
                    if position % 2 else PromptProfileFieldRule.LabelStyle.NONE
                ),
                separator=(
                    PromptProfileFieldRule.Separator.SEMICOLON
                    if position % 2 else PromptProfileFieldRule.Separator.SPACE
                ),
                required=position != 2,
                omit_if_empty=position == 2,
                max_chars=7000,
                generation_mode=(
                    PromptProfileFieldRule.GenerationMode.MODEL_GENERATED
                    if model_generated
                    else PromptProfileFieldRule.GenerationMode.SERVER_LOCKED
                ),
                generation_source_selector=(
                    "shot.narrative"
                    if model_generated
                    else PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
                ),
                generation_instruction=(
                    "Return this configured field from the frozen source facts."
                    if model_generated else ""
                ),
                generation_output_semantics=(
                    PromptProfileFieldRule.GenerationOutputSemantics.FINAL
                ),
                generation_required=model_generated,
                language_policy="any",
            )
            projected_values[field_key] = f"VALUE-{position}"

        first_rule = profile.field_rules.get(position=1)
        first_rule.generation_output_semantics = (
            PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
        )
        first_rule.generation_source_selector = (
            PromptProfileFieldRule.SourceSelector.REQUEST_THEME
        )
        first_rule.save(update_fields=(
            "generation_output_semantics",
            "generation_source_selector",
            "updated_at",
        ))
        with self.assertRaises(PromptProfileError) as unsupported:
            validate_prompt_profile_source_projection(profile)
        self.assertEqual(
            unsupported.exception.code,
            "prompt_profile_projection_contribution_unsupported",
        )
        first_rule.generation_output_semantics = (
            PromptProfileFieldRule.GenerationOutputSemantics.FINAL
        )
        first_rule.generation_source_selector = "shot.narrative"
        first_rule.save(update_fields=(
            "generation_output_semantics",
            "generation_source_selector",
            "updated_at",
        ))
        refresh_prompt_profile_contract(profile)

        published = self._publish(profile)
        assembled = assemble_prompt_profile(
            published,
            sources={
                PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT: (
                    projected_values
                ),
            },
        )

        self.assertEqual(
            [row["field_key"] for row in assembled["trace"]],
            [f"qa_configured_section_{index}" for index in range(1, 7)],
        )
        self.assertTrue(assembled["channels"]["positive_prompt"].startswith(
            "[qa_configured_section_1] VALUE-1",
        ))
        self.assertIn("VALUE-2", assembled["channels"]["positive_prompt"])
        self.assertEqual(
            list(assembled["structured_fields"]),
            [f"qa_configured_section_{index}" for index in range(1, 7)],
        )

    def test_direct_projection_rejects_contribution_rule_before_publish(self):
        profile = self._profile("qa.direct.contribution")
        rule = self._rule(profile, self._field(key="qa_direct_contribution"))
        rule.generation_output_semantics = (
            PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
        )

        with self.assertRaises(PromptProfileError) as blocked:
            validate_profile_rule(rule)

        self.assertEqual(
            blocked.exception.code,
            "prompt_profile_source_projection_semantics_invalid",
        )

    def test_ref2va_context_ir_guard_does_not_affect_other_modes_or_operations(self):
        video_field = self._field(key="qa_other_video_mode_subject")
        t2va = self._profile("qa.minimax.other-mode", mode="T2VA")
        self._rule(t2va, video_field, selector="request.subject")

        image_field = self._field(key="qa_other_operation_theme")
        image = self._profile(
            "qa.sdxl.other-operation",
            family="sdxl",
            operation="image.generate",
            mode="",
        )
        self._rule(image, image_field, selector="request.theme")

        self.assertEqual(
            self._publish(t2va).status,
            PromptProfileVersion.Status.PUBLISHED,
        )
        self.assertEqual(
            self._publish(image).status,
            PromptProfileVersion.Status.PUBLISHED,
        )

    def test_historical_ref2va_profile_remains_auditable_and_binding_can_deactivate(self):
        subject = self._field(key="qa_historical_ref2va_subject")
        profile = self._profile(
            "qa.minimax.historical-ref2va",
            mode="Ref2VA",
            source_projection_key="classified_reference_context_ir_v1",
        )
        self._rule(profile, subject, selector="request.subject")
        profile.status = PromptProfileVersion.Status.PUBLISHED
        profile.save(update_fields=("status", "updated_at"))
        binding = PromptProfileBinding.objects.create(
            key="qa.minimax.historical-ref2va",
            name="Historical Ref2VA binding",
            operation="video.generate",
            model_family="minimax_h3",
            mode="Ref2VA",
            prompt_profile_version=profile,
            is_active=True,
        )

        verify_frozen_profile_reference(
            profile,
            binding_key=binding.key,
            contract_sha256=profile.contract_sha256,
        )
        with self.assertRaises(PromptProfileError) as incompatible:
            validate_prompt_profile_binding(binding)
        self.assertEqual(
            incompatible.exception.code,
            "prompt_profile_source_projection_unregistered",
        )

        binding.is_active = False
        validate_prompt_profile_binding(binding)

    def test_model_channels_use_stable_keys_not_localized_display_labels(self):
        theme = self._field(
            key="qa_localized_cinematic_theme",
            label="电影主题",
            default="rainy abandoned station",
        )
        negative = self._field(
            key="qa_localized_negative_constraints",
            label="负面约束",
            default="watermark, subtitles",
        )
        profile = self._profile("qa.minimax.localized-labels")
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=theme,
            source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
            generation_source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            label_style=PromptProfileFieldRule.LabelStyle.LABEL_COLON,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=negative,
            source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
            generation_source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
            channel=PromptProfileFieldRule.Channel.NEGATIVE_PROMPT,
            position=2,
            label_style=PromptProfileFieldRule.LabelStyle.BRACKETED,
        )
        refresh_prompt_profile_contract(profile)
        profile = self._publish(profile)

        assembled = assemble_prompt_profile(profile, sources={})

        self.assertEqual(
            assembled["channels"]["positive_prompt"],
            "qa_localized_cinematic_theme: rainy abandoned station",
        )
        self.assertEqual(
            assembled["channels"]["negative_prompt"],
            "[qa_localized_negative_constraints] watermark, subtitles",
        )
        rendered_channels = "\n".join(assembled["channels"].values())
        self.assertNotIn("电影主题", rendered_channels)
        self.assertNotIn("负面约束", rendered_channels)

    def test_display_label_change_does_not_change_prompt_assembly(self):
        definition = self._field(
            key="qa_label_invariant_camera_movement",
            label="镜头运动",
            default="slow lateral tracking shot",
        )
        profile = self._profile("qa.minimax.display-label-invariance")
        rule = self._rule(
            profile,
            definition,
            selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
        )
        rule.label_style = PromptProfileFieldRule.LabelStyle.BRACKETED
        rule.save(update_fields=("label_style", "updated_at"))
        refresh_prompt_profile_contract(profile)
        before = assemble_prompt_profile(profile, sources={}, allow_draft=True)
        contract_before = profile.contract_sha256

        definition.label = "Camera movement display name"
        definition.save(update_fields=("label", "updated_at"))
        after = assemble_prompt_profile(profile, sources={}, allow_draft=True)

        self.assertEqual(after, before)
        self.assertEqual(profile.contract_sha256, contract_before)
        self.assertEqual(
            after["channels"]["positive_prompt"],
            "[qa_label_invariant_camera_movement] slow lateral tracking shot",
        )

    def test_schema_v5_contract_hash_includes_generation_context_and_projection_policy(self):
        definition = self._field(key="qa_generation_contract")
        profile = self._profile("qa.generic.generation-contract")
        profile.contract_schema_version = PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        profile.channel_limits = {
            "positive_prompt": {"max_chars": 7000, "warn_at_chars": 6200},
        }
        rule = PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=definition,
            source_selector=PromptProfileFieldRule.SourceSelector.SHOT_DETAIL,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            generation_mode=PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
            generation_source_selector="shot.narrative",
            generation_instruction="Expand the frozen narrative into visible shot prose.",
            generation_required=True,
            language_policy="en",
            length_unit=PromptProfileFieldRule.LengthUnit.ENGLISH_WORDS,
            generation_min=140,
            generation_max=220,
            generation_validator_keys=[
                PromptProfileFieldRule.GenerationValidator.NON_EMPTY,
                PromptProfileFieldRule.GenerationValidator.NO_CONTROL_CHARACTERS,
            ],
            generation_context_selectors=[
                PromptProfileFieldRule.GenerationContextSelector.PROJECT_STYLE,
                PromptProfileFieldRule.GenerationContextSelector.TARGET_SHOT_CORE,
            ],
        )
        validate_profile_rule(rule)
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        first_hash = profile.contract_sha256
        payload = prompt_profile_contract_payload(profile)

        self.assertEqual(
            payload["schema_version"],
            PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        self.assertEqual(payload["source_projection_config"], {})
        self.assertEqual(
            payload["channel_limits"]["positive_prompt"],
            {"max_chars": 7000, "warn_at_chars": 6200},
        )
        self.assertEqual(
            payload["rules"][0]["generation_mode"],
            "model_generated",
        )
        self.assertEqual(
            payload["rules"][0]["generation_validator_keys"],
            ["non_empty", "no_control_characters"],
        )
        self.assertEqual(
            payload["rules"][0]["generation_context_selectors"],
            ["project.style", "target_shot.core"],
        )

        rule.generation_max = 240
        rule.save(update_fields=("generation_max", "updated_at"))
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        self.assertNotEqual(profile.contract_sha256, first_hash)

        second_hash = profile.contract_sha256
        rule.generation_context_selectors = ["target_shot.core"]
        rule.save(update_fields=("generation_context_selectors", "updated_at"))
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        self.assertNotEqual(profile.contract_sha256, second_hash)

    def test_quality_advisories_are_strict_and_change_frozen_contract_hash(self):
        profile = self._profile(
            "qa.minimax.ref2va-quality-advisories",
            mode="Ref2VA",
            source_projection_key=CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY,
        )
        profile.contract_schema_version = PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        profile.source_projection_config = _h3_quality_projection_config()
        profile.save(update_fields=(
            "contract_schema_version",
            "source_projection_config",
            "updated_at",
        ))
        refresh_prompt_profile_contract(profile)
        first_hash = profile.contract_sha256
        payload = prompt_profile_contract_payload(profile)
        self.assertEqual(
            payload["source_projection_config"]["quality_advisories"],
            _h3_quality_projection_config()["quality_advisories"],
        )

        profile.source_projection_config[
            "quality_advisories"
        ]["short_shot_action_density"]["maximum_action_characters"] = 421
        profile.save(update_fields=("source_projection_config", "updated_at"))
        refresh_prompt_profile_contract(profile)
        self.assertNotEqual(profile.contract_sha256, first_hash)

        profile.source_projection_config[
            "quality_advisories"
        ]["short_shot_action_density"]["unknown_threshold"] = 1
        with self.assertRaises(PromptProfileError) as unknown:
            refresh_prompt_profile_contract(profile)
        self.assertEqual(
            unknown.exception.code,
            "prompt_profile_source_projection_config_invalid",
        )

        profile.source_projection_config = _h3_quality_projection_config()
        profile.source_projection_config[
            "quality_advisories"
        ]["positive_negative_token_overlap"]["minimum_overlap_ratio"] = "0.65"
        with self.assertRaises(PromptProfileError) as invalid_type:
            refresh_prompt_profile_contract(profile)
        self.assertEqual(
            invalid_type.exception.code,
            "prompt_profile_source_projection_config_invalid",
        )

    def test_generation_instruction_has_no_arbitrary_field_limit_but_contract_is_bounded(self):
        definition = self._field(key="qa_long_generation_instruction")
        profile = self._profile("qa.generic.long-generation-instruction")
        long_instruction = (
            "Use only the frozen source facts and preserve every declared constraint. "
            + " ".join(
                ["Describe the requested field deterministically."] * 110,
            )
        )
        self.assertGreater(len(long_instruction), 4_000)
        payload = {
            "profile_version": profile.pk,
            "field_definition": definition.pk,
            "source_selector": PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            "channel": PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            "position": 1,
            "label_style": PromptProfileFieldRule.LabelStyle.LABEL_BLOCK,
            "separator": PromptProfileFieldRule.Separator.NEWLINE,
            "required": True,
            "omit_if_empty": False,
            "max_chars": 7_000,
            "generation_mode": PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
            "generation_source_selector": "shot.detailed_description",
            "generation_instruction": long_instruction,
            "generation_output_semantics": (
                PromptProfileFieldRule.GenerationOutputSemantics.FINAL
            ),
            "generation_required": True,
            "language_policy": "en",
            "length_unit": PromptProfileFieldRule.LengthUnit.NONE,
            "generation_min": None,
            "generation_max": None,
            "generation_validator_keys": [
                PromptProfileFieldRule.GenerationValidator.NON_EMPTY,
            ],
            "generation_context_selectors": [],
        }
        serializer = PromptProfileFieldRuleSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        rule = serializer.save()
        rule.full_clean()
        profile.refresh_from_db()

        self.assertEqual(rule.generation_instruction, long_instruction)
        self.assertGreater(profile.contract_size_bytes, 4_000)
        self.assertLessEqual(
            profile.contract_size_bytes,
            64 * 1024,
        )
        verify_prompt_profile_contract(profile)

        oversized_definition = self._field(
            key="qa_oversized_generation_instruction",
        )
        oversized_profile = self._profile(
            "qa.generic.oversized-generation-instruction",
        )
        oversized_payload = {
            **payload,
            "profile_version": oversized_profile.pk,
            "field_definition": oversized_definition.pk,
            "generation_instruction": "A" * (64 * 1024),
        }
        oversized_serializer = PromptProfileFieldRuleSerializer(
            data=oversized_payload,
        )
        self.assertTrue(oversized_serializer.is_valid(), oversized_serializer.errors)
        with self.assertRaisesRegex(ValidationError, "JSON 载荷超过 65536 字节上限"):
            oversized_serializer.save()
        self.assertFalse(
            PromptProfileFieldRule.objects.filter(
                profile_version=oversized_profile,
            ).exists(),
        )

    def test_channel_limits_warn_and_block_in_generic_final_assembler(self):
        definition = self._field(key="qa_channel_limit")
        profile = self._profile("qa.generic.channel-limit")
        profile.contract_schema_version = PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        profile.channel_limits = {
            "positive_prompt": {"max_chars": 100, "warn_at_chars": 20},
        }
        profile.save(update_fields=(
            "contract_schema_version", "channel_limits", "updated_at",
        ))
        rule = self._rule(profile, definition)
        rule.generation_mode = PromptProfileFieldRule.GenerationMode.PASSTHROUGH
        rule.generation_source_selector = rule.source_selector
        rule.generation_required = True
        rule.language_policy = "any"
        rule.length_unit = PromptProfileFieldRule.LengthUnit.NONE
        rule.save(update_fields=(
            "generation_mode", "generation_source_selector",
            "generation_required", "language_policy", "length_unit", "updated_at",
        ))
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )

        assembled = assemble_prompt_profile(
            profile,
            sources={"request.theme": "x" * 25},
            allow_draft=True,
        )
        audit = assembled["channel_audit"]["positive_prompt"]
        self.assertEqual(audit["status"], "warning")
        self.assertEqual(audit["max_chars"], 100)
        self.assertEqual(len(assembled["warnings"]), 1)

        profile.channel_limits = {
            "positive_prompt": {"max_chars": 10, "warn_at_chars": 8},
        }
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        with self.assertRaises(PromptProfileError) as blocked:
            assemble_prompt_profile(
                profile,
                sources={"request.theme": "x" * 25},
                allow_draft=True,
            )
        self.assertEqual(blocked.exception.code, "prompt_profile_channel_too_long")

    def test_channel_limits_reject_unknown_channel_and_invalid_warning(self):
        profile = self._profile("qa.generic.channel-limit-invalid")
        profile.contract_schema_version = PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        profile.channel_limits = {
            "vendor_prompt": {"max_chars": 100, "warn_at_chars": 90},
        }
        with self.assertRaises(PromptProfileError) as unknown:
            refresh_prompt_profile_contract(
                profile,
                schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
            )
        self.assertEqual(
            unknown.exception.code,
            "prompt_profile_channel_limits_channel_unknown",
        )

        profile.channel_limits = {
            "positive_prompt": {"max_chars": 100, "warn_at_chars": 101},
        }
        with self.assertRaises(PromptProfileError) as warning:
            refresh_prompt_profile_contract(
                profile,
                schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
            )
        self.assertEqual(
            warning.exception.code,
            "prompt_profile_channel_limit_warning_invalid",
        )

    def test_static_reference_policy_selects_initial_snapshot_and_target_context(self):
        profile = self._profile(
            "qa.image.static-reference",
            family="qa-image",
            operation="image.generate",
            mode="",
        )
        profile.contract_schema_version = PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        profile.source_projection_config = {
            "schema_version": 1,
            "static_reference_policy": {
                "snapshot_selector": "initial",
                "default_target_kind": "character",
                "allow_context": {
                    "default": False,
                    "target_overrides": {"scene": True},
                },
                "context_source_selectors": [
                    "request.environment",
                    "request.lighting",
                    "request.color_palette",
                ],
                "transition_markers": [
                    " and changes to ",
                    " and changes once to ",
                    "→",
                ],
                "transition_patterns": [{
                    "kind": "from_to",
                    "from_marker": " changes from ",
                    "to_marker": " to ",
                }],
                "context_clause_policy": {
                    "delimiters": [",", ";"],
                    "discard_markers": [" held in ", " holding "],
                },
                "negative_token_exclusions": {
                    "request.negative_constraints": ["readable text", "text"],
                },
                "forbidden_markers": {
                    "always": [" both states "],
                    "when_context_disallowed": [" holding ", " inside "],
                },
                "violation_action": "block",
            },
        }
        profile.save(update_fields=(
            "contract_schema_version",
            "source_projection_config",
            "updated_at",
        ))
        for position, (key, selector) in enumerate((
            ("qa_static_subject", "request.subject"),
            ("qa_static_environment", "request.environment"),
            ("qa_static_lighting", "request.lighting"),
            ("qa_static_color", "request.color_palette"),
            ("qa_static_negative", "request.negative_constraints"),
        ), start=1):
            rule = self._rule(
                profile,
                self._field(key=key),
                selector=selector,
                position=position,
            )
            rule.generation_mode = PromptProfileFieldRule.GenerationMode.PASSTHROUGH
            rule.generation_source_selector = selector
            rule.generation_output_semantics = (
                PromptProfileFieldRule.GenerationOutputSemantics.FINAL
            )
            rule.language_policy = "inherit"
            rule.length_unit = PromptProfileFieldRule.LengthUnit.NONE
            if selector == "request.negative_constraints":
                rule.channel = PromptProfileFieldRule.Channel.NEGATIVE_PROMPT
            rule.save(update_fields=(
                "channel",
                "generation_mode",
                "generation_source_selector",
                "generation_output_semantics",
                "language_policy",
                "length_unit",
                "updated_at",
            ))
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        sources = {
            "request.subject": (
                "worn black access card, held in Zhou Lan's right hand; "
                "one red numeral that begins as 03 and changes once to 02"
            ),
            "request.environment": "rainy office elevator",
            "request.lighting": "cold white ceiling light",
            "request.color_palette": "slate blue and red",
            "request.negative_constraints": (
                "readable text, subtitles, watermark, logo, text"
            ),
        }

        prop = assemble_prompt_profile(
            profile,
            sources=sources,
            projection_context={
                "purpose": "static_reference",
                "target_kind": "prop",
            },
            allow_draft=True,
        )
        self.assertEqual(
            prop["structured_fields"],
            {
                "qa_static_subject": (
                    "worn black access card; one red numeral that begins as 03"
                ),
                "qa_static_negative": "subtitles, watermark, logo",
            },
        )
        self.assertEqual(
            prop["source_projection"]["trimmed_selectors"],
            ["request.subject"],
        )
        self.assertEqual(
            prop["source_projection"]["removed_context_clauses"],
            {"request.subject": ["held in"]},
        )
        self.assertEqual(
            prop["source_projection"]["removed_negative_tokens"],
            {"request.negative_constraints": ["readable text", "text"]},
        )
        self.assertEqual(
            prop["source_projection"]["dropped_context_selectors"],
            [
                "request.environment",
                "request.lighting",
                "request.color_palette",
            ],
        )

        scene = assemble_prompt_profile(
            profile,
            sources=sources,
            projection_context={
                "purpose": "static_reference",
                "target_kind": "scene",
            },
            allow_draft=True,
        )
        self.assertTrue(scene["source_projection"]["context_allowed"])
        self.assertEqual(
            scene["structured_fields"]["qa_static_environment"],
            "rainy office elevator",
        )
        self.assertNotIn("02", scene["channels"]["positive_prompt"])
        self.assertEqual(
            prompt_profile_contract_payload(profile)["source_projection_config"],
            profile.source_projection_config,
        )

        from_to = assemble_prompt_profile(
            profile,
            sources={
                **sources,
                "request.subject": (
                    "one red numeral that changes from '03' to '02'"
                ),
            },
            projection_context={"target_kind": "prop"},
            allow_draft=True,
        )
        self.assertIn("'03'", from_to["channels"]["positive_prompt"])
        self.assertNotIn("'02'", from_to["channels"]["positive_prompt"])

    def test_static_reference_policy_blocks_unresolved_relation_without_retrying(self):
        profile = self._profile(
            "qa.image.static-reference-block",
            family="qa-image",
            operation="image.generate",
            mode="",
        )
        profile.contract_schema_version = PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        profile.source_projection_config = {
            "schema_version": 1,
            "static_reference_policy": {
                "snapshot_selector": "initial",
                "default_target_kind": "character",
                "allow_context": {"default": False, "target_overrides": {}},
                "context_source_selectors": ["request.environment"],
                "transition_markers": [" changes to "],
                "transition_patterns": [],
                "context_clause_policy": {
                    "delimiters": [",", ";"],
                    "discard_markers": [],
                },
                "negative_token_exclusions": {},
                "forbidden_markers": {
                    "always": [],
                    "when_context_disallowed": [" holding "],
                },
                "violation_action": "block",
            },
        }
        profile.save(update_fields=(
            "contract_schema_version",
            "source_projection_config",
            "updated_at",
        ))
        subject_rule = self._rule(
            profile,
            self._field(key="qa_static_block_subject"),
            selector="request.subject",
            position=1,
        )
        environment_rule = self._rule(
            profile,
            self._field(key="qa_static_block_environment"),
            selector="request.environment",
            position=2,
        )
        for rule in (subject_rule, environment_rule):
            rule.generation_mode = PromptProfileFieldRule.GenerationMode.PASSTHROUGH
            rule.generation_source_selector = rule.source_selector
            rule.generation_output_semantics = (
                PromptProfileFieldRule.GenerationOutputSemantics.FINAL
            )
            rule.language_policy = "inherit"
            rule.length_unit = PromptProfileFieldRule.LengthUnit.NONE
            rule.save(update_fields=(
                "generation_mode",
                "generation_source_selector",
                "generation_output_semantics",
                "language_policy",
                "length_unit",
                "updated_at",
            ))
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )

        with self.assertRaises(PromptProfileError) as blocked:
            assemble_prompt_profile(
                profile,
                sources={
                    "request.subject": "engineer holding an access card",
                    "request.environment": "rainy elevator",
                },
                projection_context={"target_kind": "character"},
                allow_draft=True,
            )
        self.assertEqual(
            blocked.exception.code,
            "static_reference_semantic_validation_failed",
        )
        self.assertIn("不会再次调用模型或无限重试", str(blocked.exception))

    def test_static_reference_initial_clause_is_relocated_to_continuity_fact(self):
        profile = self._profile(
            "qa.image.static-reference-initial-clause",
            family="qa-image",
            operation="image.generate",
            mode="",
        )
        for position, (key, selector) in enumerate((
            ("qa_static_clause_subject", "request.subject"),
            ("qa_static_clause_environment", "request.environment"),
        ), start=1):
            rule = self._rule(
                profile,
                self._field(key=key),
                selector=selector,
                position=position,
            )
            rule.generation_mode = PromptProfileFieldRule.GenerationMode.PASSTHROUGH
            rule.generation_source_selector = selector
            rule.generation_output_semantics = (
                PromptProfileFieldRule.GenerationOutputSemantics.FINAL
            )
            rule.language_policy = "inherit"
            rule.length_unit = PromptProfileFieldRule.LengthUnit.NONE
            rule.save(update_fields=(
                "generation_mode",
                "generation_source_selector",
                "generation_output_semantics",
                "language_policy",
                "length_unit",
                "updated_at",
            ))
        profile.contract_schema_version = PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        profile.source_projection_config = _static_reference_config_v2()
        profile.save(update_fields=(
            "contract_schema_version",
            "source_projection_config",
            "updated_at",
        ))
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        sources = {
            "request.subject": (
                "Palm-sized cyan-green seed pod, initially dark, "
                "pulses once with glow"
            ),
            "request.environment": "underground seed vault",
            "request.action": "carried through the second shot",
        }

        result = assemble_prompt_profile(
            profile,
            sources=sources,
            projection_context={
                "purpose": "static_reference",
                "target_kind": "prop",
            },
            allow_draft=True,
        )

        self.assertEqual(
            result["structured_fields"]["qa_static_clause_subject"],
            "Palm-sized cyan-green seed pod; initially dark",
        )
        self.assertNotIn("pulses once", result["channels"]["positive_prompt"])
        self.assertEqual(
            sources["request.subject"],
            "Palm-sized cyan-green seed pod, initially dark, pulses once with glow",
        )
        projection = result["source_projection"]
        self.assertEqual(projection["trimmed_selectors"], ["request.subject"])
        self.assertEqual(
            projection["continuity_facts"],
            {
                "request.action": (
                    "carried through the second shot; pulses once with glow"
                ),
            },
        )
        self.assertEqual(
            projection["initial_clause_projections"],
            {
                "request.subject": {
                    "matched_initial_markers": [" initially "],
                    "relocated_clause_count": 1,
                    "continuity_source_selector": "request.action",
                    "continuity_facts": ["pulses once with glow"],
                    "continuity_facts_sha256": projection[
                        "initial_clause_projections"
                    ]["request.subject"]["continuity_facts_sha256"],
                },
            },
        )
        self.assertRegex(
            projection["initial_clause_projections"]["request.subject"][
                "continuity_facts_sha256"
            ],
            r"^[0-9a-f]{64}$",
        )

    def test_static_reference_profile_cannot_consume_continuity_fact_selector(self):
        profile = self._profile(
            "qa.image.static-reference-continuity-consumer",
            family="qa-image",
            operation="image.generate",
            mode="",
        )
        for position, (key, selector) in enumerate((
            ("qa_static_consumer_subject", "request.subject"),
            ("qa_static_consumer_environment", "request.environment"),
            ("qa_static_consumer_action", "request.action"),
        ), start=1):
            rule = self._rule(
                profile,
                self._field(key=key),
                selector=selector,
                position=position,
            )
            rule.generation_mode = PromptProfileFieldRule.GenerationMode.PASSTHROUGH
            rule.generation_source_selector = selector
            rule.generation_output_semantics = (
                PromptProfileFieldRule.GenerationOutputSemantics.FINAL
            )
            rule.language_policy = "inherit"
            rule.length_unit = PromptProfileFieldRule.LengthUnit.NONE
            rule.save(update_fields=(
                "generation_mode",
                "generation_source_selector",
                "generation_output_semantics",
                "language_policy",
                "length_unit",
                "updated_at",
            ))
        profile.contract_schema_version = PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        profile.source_projection_config = _static_reference_config_v2()
        profile.save(update_fields=(
            "contract_schema_version",
            "source_projection_config",
            "updated_at",
        ))

        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        with self.assertRaises(PromptProfileError) as blocked:
            validate_prompt_profile_source_projection(profile)
        self.assertEqual(
            blocked.exception.code,
            (
                "prompt_profile_source_projection_config_"
                "continuity_selector_consumed"
            ),
        )

    def test_generation_contract_rejects_unsafe_source_and_unknown_validator(self):
        definition = self._field(key="qa_generation_rejection")
        profile = self._profile("qa.generic.generation-rejection")
        profile.contract_schema_version = PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        rule = PromptProfileFieldRule(
            profile_version=profile,
            field_definition=definition,
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            generation_mode=PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
            generation_source_selector="shot.items[*].text",
            generation_instruction="Expand the frozen narrative into visible shot prose.",
            generation_required=True,
            language_policy="en",
            length_unit=PromptProfileFieldRule.LengthUnit.CHARACTERS,
            generation_max=500,
            generation_validator_keys=["non_empty"],
        )
        with self.assertRaises(PromptProfileError) as unsafe:
            validate_profile_rule(rule)
        self.assertEqual(unsafe.exception.code, "prompt_profile_generation_source_invalid")

        rule.generation_source_selector = "shot.unregistered_source"
        with self.assertRaises(PromptProfileError) as unregistered:
            validate_profile_rule(rule)
        self.assertEqual(
            unregistered.exception.code,
            "prompt_profile_generation_source_unknown",
        )

        rule.generation_source_selector = "shot.narrative"
        profile.operation = "script.generate"
        with self.assertRaises(PromptProfileError) as unsupported_operation:
            validate_profile_rule(rule)
        self.assertEqual(
            unsupported_operation.exception.code,
            "prompt_profile_generation_mode_operation_unsupported",
        )
        profile.operation = "video.generate"

        rule.generation_mode = PromptProfileFieldRule.GenerationMode.SERVER_LOCKED
        with self.assertRaises(PromptProfileError) as locked_mismatch:
            validate_profile_rule(rule)
        self.assertEqual(
            locked_mismatch.exception.code,
            "prompt_profile_generation_source_mismatch",
        )

        rule.generation_mode = PromptProfileFieldRule.GenerationMode.MODEL_GENERATED
        rule.generation_context_selectors = "project.core"
        with self.assertRaises(PromptProfileError) as bad_context_type:
            validate_profile_rule(rule)
        self.assertEqual(
            bad_context_type.exception.code,
            "prompt_profile_generation_context_selectors_invalid",
        )

        rule.generation_context_selectors = ["project.core", "project.core"]
        with self.assertRaises(PromptProfileError) as duplicate_context:
            validate_profile_rule(rule)
        self.assertEqual(
            duplicate_context.exception.code,
            "prompt_profile_generation_context_selectors_invalid",
        )

        rule.generation_context_selectors = ["target_shot.everything"]
        with self.assertRaises(PromptProfileError) as unknown_context:
            validate_profile_rule(rule)
        self.assertEqual(
            unknown_context.exception.code,
            "prompt_profile_generation_context_selector_unknown",
        )

        rule.generation_context_selectors = []
        rule.generation_validator_keys = ["custom_regex"]
        with self.assertRaises(PromptProfileError) as unknown:
            validate_profile_rule(rule)
        self.assertEqual(
            unknown.exception.code,
            "prompt_profile_generation_validator_unknown",
        )

        rule.generation_validator_keys = [
            "protected_tokens_preserve_count",
            "protected_tokens_at_least_once",
        ]
        with self.assertRaises(PromptProfileError) as conflicting:
            validate_profile_rule(rule)
        self.assertEqual(
            conflicting.exception.code,
            "prompt_profile_generation_validators_inconsistent",
        )

        rule.generation_validator_keys = ["length_advisory"]
        rule.length_unit = PromptProfileFieldRule.LengthUnit.NONE
        rule.generation_min = None
        rule.generation_max = None
        with self.assertRaises(PromptProfileError) as advisory_without_range:
            validate_profile_rule(rule)
        self.assertEqual(
            advisory_without_range.exception.code,
            "prompt_profile_generation_validators_inconsistent",
        )

        # Direct ORM writes cannot bypass the release gate: publication
        # revalidates the complete frozen combination and fails closed.
        rule.generation_validator_keys = ["custom_regex"]
        rule.save()
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        with self.assertRaises(PromptProfileError) as publish_error:
            publish_prompt_profile(profile, actor=self.actor)
        self.assertEqual(
            publish_error.exception.code,
            "prompt_profile_generation_validator_unknown",
        )

    def test_contribution_requires_projection_output_and_compatible_primitive(self):
        definition = self._field(key="qa_projection_contribution")
        profile = self._profile(
            "qa.configured.contribution-guard",
            mode="Ref2VA",
            source_projection_key=DECLARATIVE_SOURCE_PROJECTION_KEY,
        )
        profile.contract_schema_version = PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        profile.source_projection_config = _h3_declarative_projection_config()
        profile.save(update_fields=(
            "contract_schema_version",
            "source_projection_config",
            "updated_at",
        ))
        rule = PromptProfileFieldRule(
            profile_version=profile,
            field_definition=definition,
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            max_chars=4000,
            generation_mode=PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
            generation_source_selector="shot.detailed_description",
            generation_instruction="Produce the reviewed detailed direction contribution.",
            generation_output_semantics=(
                PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
            ),
            generation_required=True,
            language_policy="en",
            length_unit=PromptProfileFieldRule.LengthUnit.CHARACTERS,
            generation_max=2000,
            generation_validator_keys=["non_empty"],
        )

        with self.assertRaises(PromptProfileError) as missing_consumer:
            validate_profile_rule(rule)
        self.assertEqual(
            missing_consumer.exception.code,
            "prompt_profile_projection_contribution_requires_projection_output",
        )

        rule.source_selector = PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
        rule.projection_primitive = "reference_context.detailed_description"
        rule.generation_source_selector = "shot.music"
        with self.assertRaises(PromptProfileError) as incompatible_source:
            validate_profile_rule(rule)
        self.assertEqual(
            incompatible_source.exception.code,
            "prompt_profile_projection_contribution_unsupported",
        )

        rule.generation_source_selector = "shot.detailed_description"
        validate_profile_rule(rule)

    def test_legacy_draft_is_runtime_readable_but_current_write_and_publish_are_strict(self):
        definition = self._field(key="qa_legacy_generation_source")
        profile = self._profile("qa.legacy.generation-source")
        self.assertLess(
            profile.contract_schema_version,
            PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        rule = self._rule(profile, definition)
        rule.generation_source_selector = ""
        rule.save(update_fields=("generation_source_selector", "updated_at"))

        validate_profile_rule(rule)
        with self.assertRaises(PromptProfileError) as current_write:
            validate_profile_rule(
                rule,
                enforce_current_generation_source=True,
            )
        self.assertEqual(
            current_write.exception.code,
            "prompt_profile_generation_source_mismatch",
        )

        with self.assertRaises(PromptProfileError) as publish_error:
            self._publish(profile)
        self.assertEqual(
            publish_error.exception.code,
            "prompt_profile_generation_source_mismatch",
        )

    def test_localized_display_label_does_not_block_english_media_prompt(self):
        _user, project, _character, _scene, _episode, shot, _shot2 = build_story(
            username="prompt-profile-localized-label-gate",
        )
        shot.camera = "Slow lateral tracking shot toward the platform lamp"
        shot.save(update_fields=("camera", "updated_at"))
        definition = self._field(
            key="qa_gate_camera_movement",
            label="镜头运动",
        )
        profile = self._profile("qa.minimax.localized-label-gate")
        self._rule(
            profile,
            definition,
            selector=PromptProfileFieldRule.SourceSelector.SHOT_CAMERA,
        )
        profile = self._publish(profile)
        PromptProfileBinding.objects.create(
            key="qa.minimax.localized-label-gate",
            name="localized display label gate",
            operation="video.generate",
            model_family="minimax_h3",
            mode="T2VA",
            prompt_profile_version=profile,
        )

        compiled = compile_prompt_input(
            project=project,
            shots=[shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode="T2VA",
            include_audio=True,
            language_gate_mode="block",
            model_profile_version=self._video_model_profile(),
        )
        report = evaluate_prompt_language_gate(SimpleNamespace(
            prompt_type=compiled.prompt_type,
            structured_payload=compiled.structured_payload,
        ))

        self.assertEqual(
            compiled.prompt_text,
            "qa_gate_camera_movement: Slow lateral tracking shot toward the platform lamp",
        )
        self.assertNotIn("镜头运动", compiled.prompt_text)
        self.assertFalse(report.requires_english_rewrite)
        self.assertFalse(report.blocks_enqueue)
        self.assertEqual(report.as_dict()["status"], "passed")

    def test_untrusted_project_or_request_value_cannot_become_system_instruction(self):
        definition = self._field(default="trusted default")
        profile = self._profile()
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=definition,
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            channel=PromptProfileFieldRule.Channel.SYSTEM_INSTRUCTION,
            position=1,
        )
        refresh_prompt_profile_contract(profile)

        with self.assertRaisesRegex(ValidationError, "不得提升为系统指令"):
            publish_prompt_profile(profile, actor=self.actor)

    def test_media_profile_must_affect_a_model_prompt_channel(self):
        definition = self._field()
        profile = self._profile()
        self._rule(
            profile,
            definition,
            channel=PromptProfileFieldRule.Channel.STRUCTURED_INPUT,
        )

        with self.assertRaisesRegex(ValidationError, "positive_prompt"):
            publish_prompt_profile(profile, actor=self.actor)

    def test_script_payload_keeps_profile_values_literal_and_separates_trust_channels(self):
        plan = build_series_concept_plan(
            idea="一封匿名信。",
            style_hint="悬疑",
            episode_count=1,
            shots_per_episode=1,
            episode_duration_seconds=6,
            minimum_shot_duration_ms=6000,
            maximum_shot_duration_ms=6000,
            model_identifier="deepseek-test",
            timeout_seconds=30,
            chat_request_options={
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
            },
            configured_output_token_hint=4096,
            field_contract=series_concept_field_contract(),
            resource_advisory=series_concept_resource_advisory(),
        )
        plan["prompt_profile_assembly"] = {
            "channels": {
                "system_instruction": "Only reviewed constraint",
                "positive_prompt": "{{request.idea}}",
                "negative_prompt": "no watermark",
                "structured_input": "",
            },
            "structured_fields": {"theme": "{{request.idea}}"},
        }

        payload = provider_payload(plan)
        system = payload["messages"][0]["content"]
        user = json.loads(payload["messages"][1]["content"])
        self.assertIn("REVIEWED_PROMPT_PROFILE=Only reviewed constraint", system)
        self.assertEqual(user["prompt_profile"]["positive_prompt"], "{{request.idea}}")
        self.assertEqual(
            user["prompt_profile"]["structured_fields"]["theme"],
            "{{request.idea}}",
        )

    def test_exact_model_binding_beats_family_and_equal_top_ties_fail_closed(self):
        field = self._field()
        generic = self._profile(
            "qa.deepseek.generic",
            family="deepseek",
            operation="script.generate",
            mode="",
        )
        self._rule(generic, field, selector="request.theme")
        generic = self._publish(generic)
        exact = self._profile(
            "qa.deepseek.exact",
            family="deepseek",
            operation="script.generate",
            mode="",
        )
        self._rule(exact, field, selector="request.theme")
        exact = self._publish(exact)
        model = self._model_profile()
        PromptProfileBinding.objects.create(
            key="qa.deepseek.family",
            name="family",
            operation="script.generate",
            model_family="deepseek",
            prompt_profile_version=generic,
            priority=999,
        )
        PromptProfileBinding.objects.create(
            key="qa.deepseek.exact",
            name="exact",
            operation="script.generate",
            model_family="deepseek",
            model_profile_version=model,
            prompt_profile_version=exact,
            priority=0,
        )

        resolved = resolve_prompt_profile_binding(
            operation="script.generate",
            model_family="deepseek",
            model_profile_version=model,
        )
        self.assertEqual(resolved.profile_version.id, exact.id)

        PromptProfileBinding.objects.create(
            key="qa.deepseek.exact.tie",
            name="exact tie",
            operation="script.generate",
            model_family="deepseek",
            model_profile_version=model,
            prompt_profile_version=exact,
            priority=0,
        )
        with self.assertRaises(PromptProfileResolutionError) as context:
            resolve_prompt_profile_binding(
                operation="script.generate",
                model_family="deepseek",
                model_profile_version=model,
            )
        self.assertEqual(context.exception.code, "prompt_profile_binding_ambiguous")

    def test_active_exact_text_binding_requires_declared_model_capability(self):
        field = self._field(key="qa_structure_theme")
        profile = self._profile(
            "qa.deepseek.structure",
            family="deepseek",
            operation="script.structure",
            mode="",
        )
        self._rule(profile, field, selector="request.theme")
        profile = self._publish(profile)
        model = self._model_profile(capabilities=("script.generate", "prompt.normalize"))
        PromptProfileBinding.objects.create(
            key="qa.deepseek.structure.invalid-exact",
            name="invalid exact structure binding",
            operation="script.structure",
            model_family="deepseek",
            model_profile_version=model,
            prompt_profile_version=profile,
            is_active=True,
        )

        with self.assertRaises(PromptProfileError) as context:
            resolve_prompt_profile_binding(
                operation="script.structure",
                model_family="deepseek",
                model_profile_version=model,
            )

        self.assertEqual(
            context.exception.code,
            "prompt_profile_model_capability_missing",
        )

    def test_model_family_prefers_reviewed_model_specification(self):
        model = self._model_profile()
        assign_json_contract(model, "specification", {
            "schema_version": 1,
            "model_family": "vendor_next_video",
        })
        model.save(update_fields=(
            "specification", "specification_schema_version",
            "specification_size_bytes", "specification_sha256", "updated_at",
        ))

        self.assertEqual(model_family_for_profile(model), "vendor_next_video")

    def test_model_family_never_infers_from_vendor_or_model_identifier(self):
        model = self._model_profile()
        assign_json_contract(model, "specification", {
            "schema_version": 1,
            "capabilities": ["script.generate"],
        })
        model.save(update_fields=(
            "specification", "specification_schema_version",
            "specification_size_bytes", "specification_sha256", "updated_at",
        ))

        with self.assertRaises(PromptProfileResolutionError) as context:
            model_family_for_profile(model)

        self.assertEqual(
            context.exception.code,
            "prompt_profile_model_family_required",
        )

    def test_media_compile_requires_binding_and_profile_is_the_only_semantic_path(self):
        user, project, _character, _scene, _episode, shot, _shot2 = build_story(
            username="prompt-profile-story",
        )
        model = self._video_model_profile()
        with self.assertRaises(PromptProfileResolutionError) as context:
            compile_prompt_input(
                project=project,
                shots=[shot],
                prompt_type="video",
                mode="T2VA",
                include_audio=True,
                language_gate_mode="warn",
                model_profile_version=model,
            )
        self.assertEqual(context.exception.code, "prompt_profile_binding_missing")

        field = self._field(label="Reviewed theme")
        profile = self._profile("qa.minimax.compile")
        self._rule(profile, field)
        profile = self._publish(profile)
        binding = PromptProfileBinding.objects.create(
            key="qa.minimax.compile",
            name="compile binding",
            operation="video.generate",
            model_family="minimax_h3",
            mode="T2VA",
            prompt_profile_version=profile,
        )
        compiled = compile_prompt_input(
            project=project,
            shots=[shot],
            prompt_type="video",
            mode="T2VA",
            include_audio=True,
            language_gate_mode="warn",
            model_profile_version=model,
        )
        self.assertEqual(compiled.prompt_profile_version.id, profile.id)
        self.assertEqual(compiled.prompt_profile_binding_key, binding.key)
        self.assertEqual(compiled.prompt_profile_contract_sha256, profile.contract_sha256)
        self.assertIn("qa_theme:", compiled.prompt_text)
        self.assertNotIn(shot.narrative, compiled.prompt_text)
        self.assertNotIn(project.style.visual_prompt, compiled.prompt_text)
        self.assertEqual(
            compiled.structured_payload["prompt_profile"]["contract_sha256"],
            profile.contract_sha256,
        )

    def test_ref2va_profile_compiles_official_context_ir_with_dialogue_and_voiceover(self):
        _user, project, character, scene, _episode, shot, _shot2 = build_story(
            username="prompt-profile-official-ref2va",
        )
        project.style.visual_prompt = "restrained rain-soaked detective noir"
        project.style.save(update_fields=("visual_prompt", "updated_at"))
        character.visual_identity = "a short-haired female detective with a mole below her left eye"
        character.wardrobe = "a charcoal trench coat"
        character.voice_profile = "a calm, low voice"
        character.save(update_fields=(
            "visual_identity", "wardrobe", "voice_profile", "updated_at",
        ))
        scene.visual_identity = "a narrow archive room with tall shelves and drifting dust"
        scene.lighting = "a green desk lamp and intermittent lightning"
        scene.ambient_sound = "rain against glass and a ticking wall clock"
        scene.save(update_fields=(
            "visual_identity", "lighting", "ambient_sound", "updated_at",
        ))
        shot.narrative = "The detective unfolds a burned letter on the desk."
        shot.camera = "A slow overhead move settles into a close view of her face."
        shot.sound_effects = "Dry paper scraping across the wooden desk."
        shot.music = "Low restrained strings."
        shot.conditioning_mode = "Ref2VA"
        shot.dialogue = [
            {
                "speaker_id": "S1",
                "text": "别碰那封信。",
                "delivery": "in a controlled low whisper",
                "voiceover": False,
                "start_ms": 300,
                "end_ms": 1500,
            },
            {
                "speaker_id": "S1",
                "text": "那晚的火并非意外。",
                "delivery": "in a measured, reflective tone",
                "voiceover": True,
                "start_ms": 1800,
                "end_ms": 3300,
            },
        ]
        shot.save(update_fields=(
            "narrative", "camera", "sound_effects", "music", "conditioning_mode", "dialogue",
            "updated_at",
        ))

        profile = self._profile(
            "qa.minimax.official-ref2va",
            mode="Ref2VA",
            source_projection_key=DECLARATIVE_SOURCE_PROJECTION_KEY,
        )
        profile.source_projection_config = _h3_declarative_projection_config()
        profile.save(update_fields=("source_projection_config", "updated_at"))
        for position, (selector, field_key, label_style) in enumerate(
            H3_REF2VA_OFFICIAL_RULE_SPECS,
            start=1,
        ):
            PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=PromptFieldDefinition.objects.get(key=field_key),
                source_selector=(
                    PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
                ),
                projection_primitive=(
                    LEGACY_H3_SELECTOR_TO_PRIMITIVE[selector]
                ),
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=position,
                label_style=label_style,
                separator=PromptProfileFieldRule.Separator.NEWLINE,
                required=True,
                omit_if_empty=False,
                max_chars=7000,
                generation_mode=(
                    PromptProfileFieldRule.GenerationMode.SERVER_LOCKED
                ),
                generation_source_selector=(
                    PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
                ),
                generation_output_semantics=(
                    PromptProfileFieldRule.GenerationOutputSemantics.FINAL
                ),
                generation_required=True,
                language_policy="any",
            )
        refresh_prompt_profile_contract(profile)
        profile = self._publish(profile)
        PromptProfileBinding.objects.create(
            key="qa.minimax.official-ref2va",
            name="official Ref2VA",
            operation="video.generate",
            model_family="minimax_h3",
            mode="Ref2VA",
            prompt_profile_version=profile,
        )

        resolved_character_binding = {
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
            return_value=[resolved_character_binding],
        ):
            compiled = compile_prompt_input(
                project=project,
                shots=[shot],
                prompt_type=PromptArtifact.PromptType.VIDEO,
                mode="Ref2VA",
                include_audio=True,
                language_gate_mode="warn",
                model_profile_version=self._video_model_profile(),
                frozen_frame_policy=FRAME_POLICY,
            )

        expected_headers = [
            "subject_definitions:",
            "summary:",
            "retention_analysis:",
            "detailed_description:",
            "overall_soundscape:",
            "non_diegetic_music:",
        ]
        positions = [compiled.prompt_text.index(header) for header in expected_headers]
        self.assertEqual(positions, sorted(positions))
        for fact in ("<Subject 1> (S1)", "300–1500 ms", "1800–3300 ms",
                     "a controlled low whisper", "a measured, reflective tone",
                     "<d>[Chinese] 别碰那封信。</d>", "<d>[Chinese] 那晚的火并非意外。</d>",
                     "Off-screen voiceover", "No on-screen lip movement",
                     "rain against glass and a ticking wall clock",
                     "Dry paper scraping across the wooden desk.", "Low restrained strings."):
            self.assertIn(fact, compiled.prompt_text)
        self.assertEqual(compiled.negative_prompt, "")
        self.assertEqual(
            compiled.structured_payload["model_prompt_contract"]["format"],
            "reference_context_ir_v2",
        )
        self.assertFalse(
            compiled.structured_payload["model_prompt_contract"][
                "negative_prompt_submitted"
            ]
        )

    def test_media_compile_uses_exact_selected_model_across_different_families(self):
        _user, project, _character, _scene, _episode, shot, _shot2 = build_story(
            username="prompt-profile-exact-media-models",
        )
        field = self._field(key="qa_exact_media_theme", label="Exact media theme")

        def model(key: str, identifier: str, family: str) -> ModelProfileVersion:
            provider = ProviderProfile(
                key=f"qa-{key}-provider",
                name=f"QA {key} provider",
                driver_type=ProviderProfile.DriverType.COMFYUI,
                modalities="video",
                operations="health,object_info,submit,poll,cancel,collect",
            )
            assign_json_contract(provider, "driver_config", {})
            provider.save()
            row = ModelProfileVersion(
                profile_key=f"qa-{key}-model",
                version=1,
                provider=provider,
                model_identifier=identifier,
                modality=ModelProfileVersion.Modality.VIDEO,
                status=ModelProfileVersion.Status.PUBLISHED,
            )
            assign_json_contract(row, "specification", {
                "schema_version": 1,
                "model_family": family,
                "conditioning_contract": _video_conditioning_contract(),
            })
            row.save()
            return row

        minimax_model = model("minimax", "minimax-h3-video", "minimax_h3")
        qwen_model = model("qwen", "qwen-video", "qwen")
        expected = []
        for key, family, selected_model in (
            ("qa.minimax.exact-media", "minimax_h3", minimax_model),
            ("qa.qwen.exact-media", "qwen", qwen_model),
        ):
            profile = self._profile(key, family=family)
            self._rule(profile, field)
            profile = self._publish(profile)
            binding = PromptProfileBinding.objects.create(
                key=key,
                name=key,
                operation="video.generate",
                model_family=family,
                mode="T2VA",
                model_profile_version=selected_model,
                prompt_profile_version=profile,
            )
            expected.append((selected_model, binding, profile))

        compiled = [
            compile_prompt_input(
                project=project,
                shots=[shot],
                prompt_type="video",
                mode="T2VA",
                include_audio=True,
                language_gate_mode="warn",
                model_profile_version=selected_model,
            )
            for selected_model, _binding, _profile in expected
        ]
        for result, (selected_model, binding, profile) in zip(compiled, expected):
            self.assertEqual(result.prompt_profile_version.id, profile.id)
            self.assertEqual(result.prompt_profile_binding_key, binding.key)
            self.assertEqual(
                result.structured_payload["target_model_profile"]["model_profile_version_id"],
                str(selected_model.id),
            )
            self.assertEqual(
                result.request_options["model_profile_version_id"],
                str(selected_model.id),
            )
        self.assertNotEqual(compiled[0].input_fingerprint, compiled[1].input_fingerprint)

    def test_media_sources_expose_scene_lighting_and_palette_to_reviewed_profiles(self):
        _user, project, _character, scene, _episode, shot, _shot2 = build_story(
            username="prompt-profile-scene-sources",
        )
        scene.lighting = "soft window light"
        scene.color_palette = "muted cyan and amber"
        scene.save(update_fields=("lighting", "color_palette", "updated_at"))
        shot.scene = scene
        shot.save(update_fields=("scene", "updated_at"))

        sources = media_profile_sources(project=project, shot=shot)

        self.assertEqual(sources["request.lighting"], "soft window light")
        self.assertEqual(sources["request.color_palette"], "muted cyan and amber")

    def test_media_subject_uses_confirmed_canonical_names_and_visual_overlays(self):
        _user, project, character, _scene, _episode, shot, _shot2 = build_story(
            username="prompt-profile-canonical-subject",
        )
        prop = PropBible.objects.create(
            project=project,
            slug="copper-key",
            name="铜钥匙",
            visual_identity="磨损的铜钥匙，匙齿有三角缺口",
        )
        shot.props.add(prop)
        project.production_settings = {
            **(project.production_settings or {}),
            "translation_glossary": {
                "林默": "Lin Mo",
                "铜钥匙": "Copper Key",
            },
        }
        project.save(update_fields=("production_settings", "updated_at"))
        for entity, scope_type, field_key, source, translated in (
            (character, "character", "character.name", character.name, "Lin Mo"),
            (
                character,
                "character",
                "character.visual_identity",
                character.visual_identity,
                "thirty-year-old short-haired female detective with a mole below her left eye",
            ),
            (
                character,
                "character",
                "character.wardrobe",
                character.wardrobe,
                "charcoal trench coat",
            ),
            (prop, "prop", "prop.name", prop.name, "Copper Key"),
            (
                prop,
                "prop",
                "prop.visual_identity",
                prop.visual_identity,
                "worn Copper Key with a triangular notch",
            ),
        ):
            self._confirmed_overlay(
                project=project,
                entity=entity,
                scope_type=scope_type,
                field_key=field_key,
                source=source,
                translated=translated,
            )

        subject = media_profile_sources(project=project, shot=shot)["request.subject"]

        self.assertEqual(
            subject,
            (
                "Lin Mo: thirty-year-old short-haired female detective with a mole below her left eye: "
                "charcoal trench coat; Copper Key: worn Copper Key with a triangular notch"
            ),
        )
        self.assertNotIn(character.stable_speaker_id, subject)
        self.assertNotRegex(subject, r"[\u3400-\u4dbf\u4e00-\u9fff]")

    def test_missing_or_stale_canonical_name_overlay_remains_visible_to_language_gate(self):
        _user, project, character, _scene, _episode, shot, _shot2 = build_story(
            username="prompt-profile-stale-subject",
        )
        missing_subject = media_profile_sources(project=project, shot=shot)["request.subject"]
        self.assertIn(character.name, missing_subject)
        self.assertNotIn(character.stable_speaker_id, missing_subject)

        project.production_settings = {
            **(project.production_settings or {}),
            "translation_glossary": {character.name: "Lin Mo"},
        }
        project.save(update_fields=("production_settings", "updated_at"))
        self._confirmed_overlay(
            project=project,
            entity=character,
            scope_type="character",
            field_key="character.name",
            source=character.name,
            translated="Lin Mo",
        )
        for field_key, source, translated in (
            (
                "character.visual_identity",
                character.visual_identity,
                "thirty-year-old short-haired female detective with a mole below her left eye",
            ),
            ("character.wardrobe", character.wardrobe, "charcoal trench coat"),
        ):
            self._confirmed_overlay(
                project=project,
                entity=character,
                scope_type="character",
                field_key=field_key,
                source=source,
                translated=translated,
            )
        project.production_settings["translation_glossary"][character.name] = "Lin Morrow"
        project.save(update_fields=("production_settings", "updated_at"))

        subject = media_profile_sources(project=project, shot=shot)["request.subject"]
        self.assertTrue(subject.startswith(f"{character.name}: "))
        self.assertNotIn(character.stable_speaker_id, subject)

        definition = self._field(key="qa_subject_language_gate", label="Subject")
        profile = self._profile("qa.minimax.subject-language-gate")
        self._rule(profile, definition, selector="request.subject")
        profile = self._publish(profile)
        PromptProfileBinding.objects.create(
            key="qa.minimax.subject-language-gate",
            name="subject language gate",
            operation="video.generate",
            model_family="minimax_h3",
            mode="T2VA",
            prompt_profile_version=profile,
            priority=10_000,
        )
        compiled = compile_prompt_input(
            project=project,
            shots=[shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode="T2VA",
            include_audio=True,
            language_gate_mode="block",
            model_profile_version=self._video_model_profile(),
        )
        report = evaluate_prompt_language_gate(SimpleNamespace(
            prompt_type=PromptArtifact.PromptType.VIDEO,
            structured_payload=compiled.structured_payload,
        ))

        self.assertIn("qa_subject_language_gate", report.fields_requiring_english_rewrite)
        self.assertTrue(report.blocks_enqueue)

    def test_each_shot_keeps_its_own_confirmed_subject_source(self):
        _user, project, first, _scene, _episode, shot1, shot2 = build_story(
            username="prompt-profile-distinct-shot-subjects",
        )
        second = CharacterBible.objects.create(
            project=project,
            slug="zhou-yan",
            name="周岩",
            stable_speaker_id="S2",
            visual_identity="四十岁高个男维修工，右眉有疤",
            wardrobe="反光维修夹克",
        )
        shot2.characters.clear()
        shot2.characters.add(second)
        project.production_settings = {
            **(project.production_settings or {}),
            "translation_glossary": {
                first.name: "Lin Mo",
                second.name: "Zhou Yan",
            },
        }
        project.save(update_fields=("production_settings", "updated_at"))
        for entity, name, identity, wardrobe in (
            (
                first,
                "Lin Mo",
                "short-haired female detective with a mole below her left eye",
                "charcoal trench coat",
            ),
            (
                second,
                "Zhou Yan",
                "tall forty-year-old male repair worker with a scar over his right eyebrow",
                "reflective maintenance jacket",
            ),
        ):
            for field_key, source, translated in (
                ("character.name", entity.name, name),
                ("character.visual_identity", entity.visual_identity, identity),
                ("character.wardrobe", entity.wardrobe, wardrobe),
            ):
                self._confirmed_overlay(
                    project=project,
                    entity=entity,
                    scope_type="character",
                    field_key=field_key,
                    source=source,
                    translated=translated,
                )

        first_subject = media_profile_sources(project=project, shot=shot1)["request.subject"]
        second_subject = media_profile_sources(project=project, shot=shot2)["request.subject"]
        grouped_subject = media_profile_sources(
            project=project, shots=[shot1, shot2],
        )["request.subject"]

        self.assertTrue(first_subject.startswith("Lin Mo: "))
        self.assertNotIn("Zhou Yan", first_subject)
        self.assertTrue(second_subject.startswith("Zhou Yan: "))
        self.assertNotIn("Lin Mo", second_subject)
        self.assertIn(first_subject, grouped_subject)
        self.assertIn(second_subject, grouped_subject)
        self.assertNotEqual(first_subject, second_subject)

    def test_profile_can_remove_and_reorder_legacy_semantics_without_leakage(self):
        _user, project, _character, _scene, _episode, shot, _shot2 = build_story(
            username="prompt-profile-single-semantic-path",
        )
        project.style.visual_prompt = "LEGACY-STYLE-MUST-NOT-LEAK"
        project.style.negative_prompt = "LEGACY-STYLE-NEGATIVE-MUST-NOT-LEAK"
        project.style.save(update_fields=("visual_prompt", "negative_prompt", "updated_at"))
        shot.narrative = "LEGACY-NARRATIVE-MUST-NOT-LEAK"
        shot.camera = "PROFILE-SECOND"
        shot.dialogue = [{"speaker_id": "S1", "text": "LEGACY-DIALOGUE-MUST-NOT-LEAK"}]
        shot.metadata = {"negative_prompt": "LEGACY-SHOT-NEGATIVE-MUST-NOT-LEAK"}
        shot.save(update_fields=("narrative", "camera", "dialogue", "metadata", "updated_at"))

        first = self._field(key="qa_profile_first", label="First")
        second = self._field(key="qa_profile_second", label="Second")
        negative = self._field(
            key="qa_profile_negative",
            label="Negative",
            default="PROFILE-NEGATIVE",
        )
        profile = self._profile("qa.minimax.single-semantic-path")
        for definition, selector, channel, position in (
            (first, "request.text", PromptProfileFieldRule.Channel.POSITIVE_PROMPT, 1),
            (second, "shot.camera", PromptProfileFieldRule.Channel.POSITIVE_PROMPT, 2),
            (negative, "definition.default", PromptProfileFieldRule.Channel.NEGATIVE_PROMPT, 3),
        ):
            PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector=selector,
                generation_source_selector=selector,
                channel=channel,
                position=position,
                label_style=PromptProfileFieldRule.LabelStyle.NONE,
                separator=PromptProfileFieldRule.Separator.NEWLINE,
                max_chars=4000,
            )
        refresh_prompt_profile_contract(profile)
        profile = self._publish(profile)
        PromptProfileBinding.objects.create(
            key="qa.minimax.single-semantic-path",
            name="single semantic path",
            operation="video.generate",
            model_family="minimax_h3",
            mode="T2VA",
            prompt_profile_version=profile,
        )

        compiled = compile_prompt_input(
            project=project,
            shots=[shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode="T2VA",
            include_audio=True,
            extra_direction="PROFILE-FIRST",
            language_gate_mode="warn",
            model_profile_version=self._video_model_profile(),
        )

        self.assertEqual(compiled.prompt_text, "PROFILE-FIRST\nPROFILE-SECOND")
        self.assertEqual(compiled.negative_prompt, "PROFILE-NEGATIVE")
        for sentinel in (
            "LEGACY-STYLE-MUST-NOT-LEAK",
            "LEGACY-STYLE-NEGATIVE-MUST-NOT-LEAK",
            "LEGACY-NARRATIVE-MUST-NOT-LEAK",
            "LEGACY-DIALOGUE-MUST-NOT-LEAK",
            "LEGACY-SHOT-NEGATIVE-MUST-NOT-LEAK",
        ):
            self.assertNotIn(sentinel, compiled.prompt_text)
            self.assertNotIn(sentinel, compiled.negative_prompt)
        self.assertEqual(compiled.structured_payload["semantic_assembler"], "prompt_profile")

    def test_dialogue_delivery_and_voiceover_sources_are_not_conflated(self):
        _user, project, _character, _scene, _episode, shot, _shot2 = build_story(
            username="prompt-profile-dialogue-facts",
        )
        shot.dialogue = [
            {
                "speaker_id": "S1",
                "text": "ON-CAMERA-LINE",
                "delivery": "controlled whisper",
                "voiceover": False,
            },
            {
                "speaker_id": "S1",
                "text": "VOICEOVER-LINE",
                "delivery": "measured narration",
                "voiceover": True,
            },
        ]
        shot.save(update_fields=("dialogue", "updated_at"))

        sources = media_profile_sources(project=project, shot=shot)

        self.assertIn("ON-CAMERA-LINE", sources["shot.dialogue"])
        self.assertNotIn("VOICEOVER-LINE", sources["shot.dialogue"])
        self.assertIn("VOICEOVER-LINE", sources["request.voiceover"])
        self.assertNotIn("ON-CAMERA-LINE", sources["request.voiceover"])
        self.assertIn("controlled whisper", sources["request.dialogue_delivery"])
        self.assertIn("measured narration", sources["request.dialogue_delivery"])
        self.assertNotIn("ON-CAMERA-LINE", sources["request.dialogue_delivery"])
        self.assertNotIn("VOICEOVER-LINE", sources["request.dialogue_delivery"])

    def test_profile_reference_source_uses_normalized_shot_binding(self):
        user, project, _character, _scene, _episode, shot, _shot2 = build_story(
            username="prompt-profile-reference-facts",
        )
        content = b"reviewed-first-frame-fixture"
        first_frame = Asset.objects.create(
            owner=user,
            kind=Asset.Kind.REFERENCE,
            name="reviewed-first-frame.png",
            file=SimpleUploadedFile("reviewed-first-frame.png", content),
            checksum=hashlib.sha256(content).hexdigest(),
            mime_type="image/png",
        )
        self.addCleanup(first_frame.file.delete, False)
        shot.conditioning_mode = "I2VA"
        shot.reference_bindings = {"first_frame_asset_id": str(first_frame.id)}
        shot.save(update_fields=("conditioning_mode", "reference_bindings", "updated_at"))
        shot.reference_assets.set([first_frame])

        reference_field = self._field(
            key="qa_reference_binding",
            label="Reference",
            value_type=PromptFieldDefinition.ValueType.STRING_LIST,
        )
        profile = self._profile("qa.minimax.reference", mode="I2VA")
        rule = self._rule(
            profile,
            reference_field,
            selector=PromptProfileFieldRule.SourceSelector.REQUEST_REFERENCE,
        )
        rule.label_style = PromptProfileFieldRule.LabelStyle.NONE
        rule.save(update_fields=("label_style", "updated_at"))
        refresh_prompt_profile_contract(profile)
        profile = self._publish(profile)
        PromptProfileBinding.objects.create(
            key="qa.minimax.reference",
            name="reference binding",
            operation="video.generate",
            model_family="minimax_h3",
            mode="I2VA",
            prompt_profile_version=profile,
        )

        compiled = compile_prompt_input(
            project=project,
            shots=[shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode="I2VA",
            include_audio=True,
            language_gate_mode="warn",
            model_profile_version=self._video_model_profile(),
        )

        self.assertIn("<Picture 1>", compiled.prompt_text)
        self.assertIn("reviewed-first-frame.png", compiled.prompt_text)
        self.assertEqual(
            compiled.structured_payload["reference_bindings"][0]["asset_id"],
            str(first_frame.id),
        )


class PromptProfileApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="prompt-profile-api-admin",
            password="StrongPass123!",
            email="prompt-profile-api@example.test",
        )
        self.user = get_user_model().objects.create_user(
            username="prompt-profile-api-user",
            password="StrongPass123!",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_preview_candidate_uses_unsaved_rules_without_database_writes(self):
        field = PromptFieldDefinition.objects.create(
            key="preview_theme",
            label="Preview theme",
            category="preview",
            description="Preview-only theme value",
            impact="Changes final prompt text",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        profile = PromptProfileVersion.objects.create(
            profile_key="preview.candidate",
            version=1,
            name="Preview candidate",
            model_family="minimax_h3",
            operation="video.generate",
            mode="T2VA",
            source_projection_key="direct_v1",
            source_projection_config={},
            channel_limits={
                "positive_prompt": {
                    "max_chars": 1000,
                    "warn_at_chars": 900,
                },
            },
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        rule = PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=field,
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_THEME,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            label_style=PromptProfileFieldRule.LabelStyle.LABEL_COLON,
            separator=PromptProfileFieldRule.Separator.NEWLINE,
            required=True,
            omit_if_empty=True,
            max_chars=500,
            generation_mode=PromptProfileFieldRule.GenerationMode.PASSTHROUGH,
            generation_source_selector=(
                PromptProfileFieldRule.SourceSelector.REQUEST_THEME
            ),
            generation_output_semantics=(
                PromptProfileFieldRule.GenerationOutputSemantics.FINAL
            ),
            generation_required=True,
            language_policy="inherit",
            length_unit=PromptProfileFieldRule.LengthUnit.NONE,
            generation_validator_keys=[
                PromptProfileFieldRule.GenerationValidator.NON_EMPTY,
            ],
            generation_context_selectors=[],
        )
        refresh_prompt_profile_contract(profile)
        profile.refresh_from_db()
        base_hash = profile.contract_sha256
        base_updated_at = profile.updated_at
        candidate = {
            "model_family": profile.model_family,
            "operation": profile.operation,
            "mode": profile.mode,
            "source_projection_key": profile.source_projection_key,
            "source_projection_config": {},
            "channel_limits": profile.channel_limits,
            "field_rules": [{
                "id": str(rule.id),
                "field_definition": str(field.id),
                "source_selector": rule.source_selector,
                "projection_primitive": "",
                "channel": rule.channel,
                "position": 1,
                "label_style": "bracketed",
                "separator": "newline",
                "required": True,
                "omit_if_empty": True,
                "max_chars": 500,
                "generation_mode": rule.generation_mode,
                "generation_source_selector": rule.generation_source_selector,
                "generation_context_selectors": [],
                "generation_instruction": "",
                "generation_output_semantics": rule.generation_output_semantics,
                "generation_required": True,
                "language_policy": "inherit",
                "length_unit": "none",
                "generation_min": None,
                "generation_max": None,
                "generation_validator_keys": ["non_empty"],
            }],
        }

        response = self.client.post(
            f"/api/v1/prompt-profile-versions/{profile.id}/preview/",
            {
                "expected_base_contract_sha256": base_hash,
                "candidate": candidate,
                "sources": {"request.theme": "fog harbor"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["channels"]["positive_prompt"],
            "[preview_theme] fog harbor",
        )
        self.assertTrue(response.data["ephemeral"])
        self.assertEqual(response.data["preview_scope"], "sample_final_assembly")
        self.assertFalse(response.data["executed_stages"]["field_generation"])
        self.assertFalse(response.data["executed_stages"]["source_projection"])
        self.assertTrue(response.data["executed_stages"]["final_assembly"])
        self.assertEqual(response.data["base_contract_sha256"], base_hash)
        self.assertNotEqual(response.data["contract_sha256"], base_hash)
        profile.refresh_from_db()
        self.assertEqual(profile.contract_sha256, base_hash)
        self.assertEqual(profile.updated_at, base_updated_at)
        self.assertEqual(profile.field_rules.count(), 1)

        stale = self.client.post(
            f"/api/v1/prompt-profile-versions/{profile.id}/preview/",
            {
                "expected_base_contract_sha256": "0" * 64,
                "candidate": candidate,
                "sources": {"request.theme": "fog harbor"},
            },
            format="json",
        )
        self.assertEqual(stale.status_code, 409, stale.data)
        self.assertEqual(stale.data["code"], "prompt_profile_preview_base_stale")

    def test_crud_preview_publish_binding_and_retire_lifecycle(self):
        previous_display_order = (
            PromptFieldDefinition.objects.order_by("-display_order")
            .values_list("display_order", flat=True)
            .first()
            or 0
        )
        created_field = self.client.post("/api/v1/prompt-field-definitions/", {
            "key": "api_theme",
            "label": "API Theme",
            "category": "api",
            "description": "Theme used by API test",
            "impact": "Changes positive prompt",
            "value_type": "long_text",
            "default_value": None,
            "value_options": [],
            "is_required": False,
            "is_active": False,
            "display_order": 999,
        }, format="json")
        self.assertEqual(created_field.status_code, 201, created_field.data)
        self.assertEqual(
            created_field.data["display_order"], previous_display_order + 1,
        )
        self.assertTrue(created_field.data["is_active"])
        self.assertEqual(
            created_field.data["preview_example"],
            "A neutral example used only to preview the configured prompt structure.",
        )
        self.assertIsNone(created_field.data["default_value"])

        created_profile = self.client.post("/api/v1/prompt-profile-versions/", {
            "profile_key": "api.minimax.t2va",
            "version": 99,
            "name": "API MiniMax T2VA",
            "description": "API managed strategy",
            "model_family": "minimax_h3",
            "operation": "video.generate",
            "mode": "vendor_x_v1",
            "source_projection_key": "direct_v1",
            "source_projection_config": {},
            "official_reference": "",
            "channel_limits": {
                "positive_prompt": {
                    "max_chars": 7000,
                    "warn_at_chars": 6200,
                },
            },
            "review_state": "ready",
        }, format="json")
        self.assertEqual(created_profile.status_code, 201, created_profile.data)
        self.assertEqual(created_profile.data["version"], 1)
        self.assertEqual(created_profile.data["review_state"], "needs_review")
        profile_id = created_profile.data["id"]
        duplicate_profile = self.client.post(
            "/api/v1/prompt-profile-versions/",
            {
                "profile_key": "api.minimax.t2va",
                "version": 100,
                "name": "Must use successor",
                "model_family": "minimax_h3",
                "operation": "video.generate",
                "mode": "vendor_x_v1",
                "source_projection_key": "direct_v1",
                "review_state": "needs_review",
            },
            format="json",
        )
        self.assertEqual(duplicate_profile.status_code, 400, duplicate_profile.data)
        version_edit = self.client.patch(
            f"/api/v1/prompt-profile-versions/{profile_id}/",
            {"version": 42},
            format="json",
        )
        self.assertEqual(version_edit.status_code, 400, version_edit.data)

        created_rule = self.client.post("/api/v1/prompt-profile-field-rules/", {
            "profile_version": profile_id,
            "field_definition": created_field.data["id"],
            "source_selector": "request.theme",
            "channel": "positive_prompt",
            "position": 1,
            "label_style": "label_colon",
            "separator": "newline",
            "required": True,
            "omit_if_empty": True,
            "max_chars": 4000,
            "generation_mode": "passthrough",
            "generation_source_selector": "request.theme",
            "generation_instruction": "",
            "generation_output_semantics": "final",
            "generation_required": True,
            "language_policy": "inherit",
            "length_unit": "none",
            "generation_min": None,
            "generation_max": None,
            "generation_validator_keys": ["non_empty"],
            "generation_context_selectors": [],
        }, format="json")
        self.assertEqual(created_rule.status_code, 201, created_rule.data)
        self.assertEqual(created_rule.data["generation_mode"], "passthrough")
        self.assertEqual(
            created_profile.data["contract_schema_version"],
            PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        self.assertEqual(created_profile.data["source_projection_config"], {})

        blocked_publish = self.client.post(
            f"/api/v1/prompt-profile-versions/{profile_id}/publish/", {}, format="json",
        )
        self.assertEqual(blocked_publish.status_code, 400)
        self.assertIn("needs_review", str(blocked_publish.data))

        preview = self.client.post(
            f"/api/v1/prompt-profile-versions/{profile_id}/preview/",
            {"sources": {"request.theme": "fog harbor"}},
            format="json",
        )
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(
            preview.data["channels"]["positive_prompt"],
            "api_theme: fog harbor",
        )
        self.assertEqual(
            preview.data["channel_audit"]["positive_prompt"]["max_chars"],
            7000,
        )

        ready = self.client.patch(
            f"/api/v1/prompt-profile-versions/{profile_id}/",
            {"review_state": "ready"},
            format="json",
        )
        self.assertEqual(ready.status_code, 200, ready.data)
        published = self.client.post(
            f"/api/v1/prompt-profile-versions/{profile_id}/publish/", {}, format="json",
        )
        self.assertEqual(published.status_code, 200, published.data)
        self.assertEqual(published.data["status"], "published")
        frozen_contract_sha256 = published.data["contract_sha256"]
        late_patch = self.client.patch(
            f"/api/v1/prompt-profile-field-rules/{created_rule.data['id']}/",
            {"max_chars": 3999},
            format="json",
        )
        self.assertEqual(late_patch.status_code, 400, late_patch.data)
        late_field = self.client.post("/api/v1/prompt-field-definitions/", {
            "key": "api_theme_late",
            "label": "Late API theme",
            "category": "api",
            "description": "Must not enter a frozen profile",
            "impact": "Would change the published contract",
            "value_type": "long_text",
        }, format="json")
        self.assertEqual(late_field.status_code, 201, late_field.data)
        late_rule = self.client.post("/api/v1/prompt-profile-field-rules/", {
            "profile_version": profile_id,
            "field_definition": late_field.data["id"],
            "source_selector": "request.text",
            "channel": "positive_prompt",
            "position": 2,
            "label_style": "label_colon",
            "separator": "newline",
            "required": False,
            "omit_if_empty": True,
            "max_chars": 2000,
            "generation_mode": "passthrough",
            "generation_source_selector": "request.text",
            "generation_instruction": "",
            "generation_output_semantics": "final",
            "generation_required": False,
            "language_policy": "inherit",
            "length_unit": "none",
            "generation_min": None,
            "generation_max": None,
            "generation_validator_keys": [],
            "generation_context_selectors": [],
        }, format="json")
        self.assertEqual(late_rule.status_code, 400, late_rule.data)
        self.assertEqual(
            PromptProfileVersion.objects.get(pk=profile_id).contract_sha256,
            frozen_contract_sha256,
        )

        binding = self.client.post("/api/v1/prompt-profile-bindings/", {
            "key": "api.minimax.t2va",
            "name": "API binding",
            "operation": "video.generate",
            "model_family": "minimax_h3",
            "mode": "vendor_x_v1",
            "model_profile_version": None,
            "prompt_profile_version": profile_id,
            "priority": 0,
            "is_active": True,
        }, format="json")
        self.assertEqual(binding.status_code, 201, binding.data)

        listing = self.client.get("/api/v1/prompt-profile-bindings/", {
            "model_family": "minimax_h3", "page_size": 50,
        })
        self.assertEqual(listing.status_code, 200, listing.data)
        associated = next(row for row in listing.data["results"] if row["id"] == binding.data["id"])
        self.assertEqual(associated["prompt_profile_name"], published.data["name"])
        self.assertEqual(associated["prompt_profile_version_number"], published.data["version"])
        self.assertEqual(associated["prompt_profile_status"], "published")
        self.assertEqual(associated["prompt_profile_contract_sha256"], frozen_contract_sha256)

        options = self.client.get("/api/v1/prompt-profile-versions/options/")
        self.assertEqual(options.status_code, 200, options.data)
        self.assertIn("video.generate", options.data["operations"])
        self.assertIn("minimax_h3", options.data["model_families"])
        self.assertIn("vendor_x_v1", options.data["modes"])
        self.assertIn("long_text", {
            item["value"] for item in options.data["field_value_types"]
        })
        self.assertIn("api", {
            item["value"] for item in options.data["field_categories"]
        })
        self.assertEqual(
            options.data["field_system_defaults"]["display_order"],
            "append",
        )
        self.assertEqual(
            options.data["rule_system_defaults"]["max_chars"],
            2000,
        )
        self.assertIn("operation", options.data["custom_entry_fields"])
        field_options = self.client.get("/api/v1/prompt-field-definitions/options/")
        self.assertEqual(field_options.status_code, 200, field_options.data)
        self.assertIn("long_text", {
            item["value"] for item in field_options.data["value_types"]
        })
        self.assertIn("api", {
            item["value"] for item in field_options.data["categories"]
        })
        self.assertEqual(field_options.data["custom_entry_fields"], ["category"])
        self.assertEqual(
            created_profile.data["source_projection_key"],
            "direct_v1",
        )
        self.assertTrue(any(
            item["value"] == "request.theme" for item in options.data["source_selectors"]
        ))
        self.assertTrue(any(
            item["value"] == "model_generated"
            for item in options.data["generation_modes"]
        ))
        self.assertTrue(any(
            item["value"] == "english_words"
            for item in options.data["length_units"]
        ))
        self.assertTrue(any(
            item["value"] == "non_empty"
            for item in options.data["generation_validators"]
        ))
        context_options = options.data["generation_context_selectors"]
        self.assertEqual(
            {item["value"] for item in context_options},
            set(PromptProfileFieldRule.GenerationContextSelector.values),
        )
        self.assertTrue(all(
            item.get("label") and item.get("description")
            for item in context_options
        ))
        generation_source_options = options.data["generation_source_selectors"]
        self.assertEqual(
            {item["value"] for item in generation_source_options},
            set(PROMPT_GENERATION_SOURCE_VALUES),
        )
        self.assertEqual(len(generation_source_options), 35)
        self.assertTrue(all(
            item.get("label")
            and item.get("scope_type")
            and item.get("group")
            and item.get("group_label")
            and item.get("description")
            and item.get("availability")
            and item.get("scope")
            and item.get("status") == "active"
            and item.get("generation_modes") == [
                "model_generated", "translate_only",
            ]
            for item in generation_source_options
        ))

        blocked_retire = self.client.post(
            f"/api/v1/prompt-profile-versions/{profile_id}/retire/", {}, format="json",
        )
        self.assertEqual(blocked_retire.status_code, 400)
        self.client.patch(
            f"/api/v1/prompt-profile-bindings/{binding.data['id']}/",
            {"is_active": False},
            format="json",
        )
        retired = self.client.post(
            f"/api/v1/prompt-profile-versions/{profile_id}/retire/", {}, format="json",
        )
        self.assertEqual(retired.status_code, 200, retired.data)
        self.assertEqual(retired.data["status"], "retired")
        successor = self.client.post(
            f"/api/v1/prompt-profile-versions/{profile_id}/clone-successor/",
            {
                "expected_source_contract_sha256": retired.data["contract_sha256"],
                "name": "API MiniMax T2VA successor",
            },
            format="json",
        )
        self.assertEqual(successor.status_code, 201, successor.data)
        self.assertEqual(successor.data["version"], 2)
        self.assertEqual(successor.data["status"], "draft")
        self.assertEqual(successor.data["review_state"], "needs_review")
        successor_conflict = self.client.post(
            f"/api/v1/prompt-profile-versions/{profile_id}/clone-successor/",
            {
                "expected_source_contract_sha256": retired.data["contract_sha256"],
                "name": "Must reuse existing successor",
            },
            format="json",
        )
        self.assertEqual(successor_conflict.status_code, 409, successor_conflict.data)
        self.assertEqual(
            successor_conflict.data["error"]["code"],
            "prompt_profile_clone_draft_exists",
        )
        frozen_field_update = self.client.patch(
            f"/api/v1/prompt-field-definitions/{created_field.data['id']}/",
            {"label": "Must not rewrite retired contract"},
            format="json",
        )
        self.assertEqual(frozen_field_update.status_code, 400, frozen_field_update.data)
        self.assertIn("published/retired", str(frozen_field_update.data))
        self.assertTrue(PlatformLifecycleAuditEvent.objects.filter(
            object_type="prompt_profile_version",
            object_id=profile_id,
            action="publish",
        ).exists())

    def test_non_staff_cannot_read_or_write_control_plane_profiles(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(
            self.client.get("/api/v1/prompt-profile-versions/").status_code,
            403,
        )
        self.assertEqual(
            self.client.post("/api/v1/prompt-field-definitions/", {}, format="json").status_code,
            403,
        )

    def test_prompt_profile_api_uses_current_effective_limits(self):
        definition = PromptFieldDefinition.objects.create(
            key="api_effective_limit_text",
            label="API effective limit text",
            category="api",
            description="Exercises runtime-owned PromptProfile limits",
            impact="Changes one prompt field",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        profile = PromptProfileVersion.objects.create(
            profile_key="api.effective-limits",
            version=1,
            name="API effective limits",
            model_family="vendor_next_video",
            operation="video.generate",
            mode="custom_mode",
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        lowered = create_config_revision(
            scope_type="system",
            values={
                "security.prompt_profile.field_text_max_characters": 40_000,
                "security.contract_complexity.prompt_profile.max_validators": 1,
                "security.contract_complexity.prompt_profile.max_context_selectors": 1,
            },
            created_by=self.admin,
        )
        publish_config_revision(lowered, published_by=self.admin)
        options = self.client.get("/api/v1/prompt-profile-versions/options/")
        self.assertEqual(options.status_code, 200, options.data)
        self.assertEqual(options.data["effective_limits"], {
            "field_text_max_characters": 40_000,
            "max_validators": 1,
            "max_context_selectors": 1,
        })
        payload = {
            "profile_version": str(profile.id),
            "field_definition": str(definition.id),
            "source_selector": "request.text",
            "channel": "positive_prompt",
            "position": 1,
            "label_style": "label_colon",
            "separator": "newline",
            "required": True,
            "omit_if_empty": False,
            "max_chars": 35_000,
            "generation_mode": "passthrough",
            "generation_source_selector": "request.text",
            "generation_instruction": "",
            "generation_output_semantics": "final",
            "generation_required": True,
            "language_policy": "inherit",
            "length_unit": "characters",
            "generation_min": 1,
            "generation_max": 35_000,
            "generation_validator_keys": ["non_empty", "single_line"],
            "generation_context_selectors": ["project.core", "project.style"],
        }
        rejected = self.client.post(
            "/api/v1/prompt-profile-field-rules/",
            payload,
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)

        raised = create_config_revision(
            scope_type="system",
            values={
                "security.contract_complexity.prompt_profile.max_validators": 16,
                "security.contract_complexity.prompt_profile.max_context_selectors": 8,
            },
            created_by=self.admin,
        )
        publish_config_revision(raised, published_by=self.admin)
        accepted = self.client.post(
            "/api/v1/prompt-profile-field-rules/",
            payload,
            format="json",
        )
        self.assertEqual(accepted.status_code, 201, accepted.data)
        self.assertEqual(accepted.data["generation_max"], 35_000)

    def test_profile_draft_accepts_new_safe_operation_key_but_rejects_code_like_input(self):
        payload = {
            "profile_key": "api.future.operation",
            "version": 1,
            "name": "Future operation profile",
            "description": "Draft registration does not grant execution",
            "model_family": "fictional_story_model",
            "operation": "storyboard.render_v2",
            "mode": "custom_mode",
            "source_projection_key": "direct_v1",
            "official_reference": "",
            "review_state": "needs_review",
        }

        created = self.client.post(
            "/api/v1/prompt-profile-versions/", payload, format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["operation"], "storyboard.render_v2")
        options = self.client.get("/api/v1/prompt-profile-versions/options/")
        self.assertEqual(options.status_code, 200, options.data)
        self.assertIn("storyboard.render_v2", options.data["operations"])

        rejected = self.client.post(
            "/api/v1/prompt-profile-versions/",
            {
                **payload,
                "profile_key": "api.future.operation.invalid",
                "operation": "__import__('os').system",
            },
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("operation", str(rejected.data))

    def test_generation_contract_api_rejects_omission_unknown_keys_and_bad_combinations(self):
        definition = PromptFieldDefinition.objects.create(
            key="api_generation_contract",
            label="Generation contract",
            category="api",
            description="Generic Layer-2 field contract",
            impact="Constrains a generated field before deterministic assembly",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        profile = PromptProfileVersion.objects.create(
            profile_key="api.generic.generation-contract",
            version=1,
            name="API generic generation contract",
            model_family="vendor_next_video",
            operation="video.generate",
            mode="custom_mode",
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        refresh_prompt_profile_contract(
            profile,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        base = {
            "profile_version": str(profile.id),
            "field_definition": str(definition.id),
            "source_selector": "request.text",
            "channel": "positive_prompt",
            "position": 1,
            "label_style": "label_colon",
            "separator": "newline",
            "required": True,
            "omit_if_empty": False,
            "max_chars": 4000,
            "generation_mode": "model_generated",
            "generation_source_selector": "shot.narrative",
            "generation_instruction": "Expand the frozen narrative into visible shot prose.",
            "generation_output_semantics": "final",
            "generation_required": True,
            "language_policy": "en",
            "length_unit": "english_words",
            "generation_min": 120,
            "generation_max": 240,
            "generation_validator_keys": ["non_empty"],
            "generation_context_selectors": [
                "project.style",
                "target_shot.core",
            ],
        }

        omitted = self.client.post(
            "/api/v1/prompt-profile-field-rules/",
            {key: value for key, value in base.items() if key != "generation_mode"},
            format="json",
        )
        self.assertEqual(omitted.status_code, 400, omitted.data)
        self.assertIn("generation_mode", str(omitted.data))

        context_omitted = self.client.post(
            "/api/v1/prompt-profile-field-rules/",
            {
                key: value for key, value in base.items()
                if key != "generation_context_selectors"
            },
            format="json",
        )
        self.assertEqual(context_omitted.status_code, 400, context_omitted.data)
        self.assertIn("generation_context_selectors", str(context_omitted.data))

        unknown_context = self.client.post(
            "/api/v1/prompt-profile-field-rules/",
            {**base, "generation_context_selectors": ["target_shot.everything"]},
            format="json",
        )
        self.assertEqual(unknown_context.status_code, 400, unknown_context.data)
        self.assertIn("target_shot.everything", str(unknown_context.data))

        unknown = self.client.post(
            "/api/v1/prompt-profile-field-rules/",
            {**base, "generation_validator_keys": ["custom_regex"]},
            format="json",
        )
        self.assertEqual(unknown.status_code, 400, unknown.data)
        self.assertIn("custom_regex", str(unknown.data))

        inconsistent = self.client.post(
            "/api/v1/prompt-profile-field-rules/",
            {
                **base,
                "length_unit": "none",
                "generation_min": 120,
                "generation_max": 240,
            },
            format="json",
        )
        self.assertEqual(inconsistent.status_code, 400, inconsistent.data)
        self.assertIn("length_unit=none", str(inconsistent.data))

        unsafe = self.client.post(
            "/api/v1/prompt-profile-field-rules/",
            {**base, "generation_source_selector": "shot.items[*].text"},
            format="json",
        )
        self.assertEqual(unsafe.status_code, 400, unsafe.data)
        self.assertIn("generation_source_selector", str(unsafe.data))

        unregistered = self.client.post(
            "/api/v1/prompt-profile-field-rules/",
            {**base, "generation_source_selector": "shot.unregistered_source"},
            format="json",
        )
        self.assertEqual(unregistered.status_code, 400, unregistered.data)
        self.assertEqual(
            unregistered.data["error"]["code"],
            "prompt_profile_generation_source_unknown",
        )
        self.assertIn("generation_source_selectors", str(unregistered.data))

    def test_exact_binding_cannot_be_active_without_declared_model_capability(self):
        provider = ProviderProfile(
            key="api-capability-provider",
            name="API capability provider",
            driver_type=ProviderProfile.DriverType.DEEPSEEK_API,
            modalities="text",
            operations="chat_completion",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {})
        provider.save()
        model = ModelProfileVersion(
            profile_key="api-capability-model",
            version=1,
            provider=provider,
            model_identifier="deepseek-capability-test",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(model, "specification", {
            "schema_version": 1,
            "model_family": "deepseek",
            "capabilities": ["script.generate", "prompt.normalize"],
        })
        model.save()
        definition = PromptFieldDefinition.objects.create(
            key="api_structure_theme",
            label="Structure theme",
            category="api",
            description="Structure test field",
            impact="Affects structured script input",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        profile = PromptProfileVersion.objects.create(
            profile_key="api.deepseek.structure",
            version=1,
            name="API DeepSeek structure",
            model_family="deepseek",
            operation="script.structure",
            review_state=PromptProfileVersion.ReviewState.READY,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=definition,
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_THEME,
            generation_source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_THEME,
            channel=PromptProfileFieldRule.Channel.STRUCTURED_INPUT,
            position=1,
        )
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.admin)
        payload = {
            "name": "API invalid exact structure binding",
            "operation": "script.structure",
            "model_family": "deepseek",
            "mode": "",
            "model_profile_version": str(model.id),
            "prompt_profile_version": str(profile.id),
            "priority": 0,
            "is_active": True,
        }

        rejected = self.client.post(
            "/api/v1/prompt-profile-bindings/",
            {**payload, "key": "api.deepseek.structure.invalid"},
            format="json",
        )

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("script.structure", str(rejected.data))
        inactive_history = self.client.post(
            "/api/v1/prompt-profile-bindings/",
            {
                **payload,
                "key": "api.deepseek.structure.inactive-history",
                "is_active": False,
            },
            format="json",
        )
        self.assertEqual(inactive_history.status_code, 201, inactive_history.data)

    def test_options_include_explicit_family_from_published_model_specification(self):
        provider = ProviderProfile(
            key="api-future-family-provider",
            name="Future family provider",
            driver_type=ProviderProfile.DriverType.OPENAI_COMPATIBLE,
            modalities="video",
            operations="video.generate",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {})
        provider.save()
        model = ModelProfileVersion(
            profile_key="api-future-family-model",
            version=1,
            provider=provider,
            model_identifier="vendor-model-2030",
            modality=ModelProfileVersion.Modality.VIDEO,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(model, "specification", {
            "schema_version": 1,
            "model_family": "vendor_next_video",
        })
        model.save()

        response = self.client.get("/api/v1/prompt-profile-versions/options/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("vendor_next_video", response.data["model_families"])
