from types import SimpleNamespace

from django.test import TestCase

from studio.models import (
    ModelProfileVersion,
    PromptArtifact,
    PromptFieldDefinition,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProviderProfile,
    Shot,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.production_pipeline import compile_prompt_input
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.services.prompt_quality_gate import evaluate_prompt_language_gate
from studio.tests.factories import build_story, conditioning_contract
from studio.tests.reference_rendering_fixtures import FRAME_POLICY


def _prompt(
    *, prompt_type="image", fields=(), hard_fields=(),
    mode="block",
):
    return SimpleNamespace(
        prompt_type=prompt_type,
        structured_payload={
            "quality": {
                "fields_requiring_english_rewrite": list(fields),
                "hard_block_fields": list(hard_fields),
                "production_language_gate": {"mode": mode},
            },
        },
    )


class PromptLanguageGatePolicyTests(TestCase):
    def setUp(self):
        self.user, self.project, _, _, self.episode, self.shot, _ = build_story(
            username="language-gate-policy",
        )
        self.model_profiles = {}
        self._bind_profile(
            key="language-gate-image",
            family="sdxl",
            operation="image.generate",
            mode="",
            selector=PromptProfileFieldRule.SourceSelector.SHOT_NARRATIVE,
        )
        self._bind_profile(
            key="language-gate-video",
            family="minimax_h3",
            operation="video.generate",
            mode="T2VA",
            selector=PromptProfileFieldRule.SourceSelector.SHOT_CAMERA,
        )

    def _bind_profile(self, *, key, family, operation, mode, selector):
        provider = ProviderProfile(
            key=f"qa-{family}-provider",
            name=f"QA {family} provider",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image,video",
            operations="health,object_info,submit,poll,cancel,collect",
        )
        assign_json_contract(provider, "driver_config", {})
        provider.save()
        model = ModelProfileVersion(
            profile_key=f"qa-{family}-model",
            version=1,
            provider=provider,
            model_identifier=f"configured-{family}-model",
            modality=(
                ModelProfileVersion.Modality.IMAGE
                if operation == "image.generate"
                else ModelProfileVersion.Modality.VIDEO
            ),
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        specification = {
            "schema_version": 1,
            "model_family": family,
        }
        if operation == "video.generate":
            specification["conditioning_contract"] = conditioning_contract(mode)
        assign_json_contract(model, "specification", specification)
        model.save()
        self.model_profiles[operation] = model
        definition = PromptFieldDefinition.objects.create(
            key=f"qa_{key.replace('-', '_')}",
            label=key,
            category="qa",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
            description="Explicit language gate fixture",
            impact="Controls the emitted prompt in this test",
        )
        profile = PromptProfileVersion.objects.create(
            profile_key=f"qa.{key}",
            version=1,
            name=key,
            model_family=family,
            operation=operation,
            mode=mode,
            review_state=PromptProfileVersion.ReviewState.READY,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=definition,
            source_selector=selector,
            generation_source_selector=selector,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            label_style=PromptProfileFieldRule.LabelStyle.NONE,
            max_chars=4000,
        )
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.user)
        PromptProfileBinding.objects.create(
            key=f"qa.{key}",
            name=key,
            operation=operation,
            model_family=family,
            mode=mode,
            prompt_profile_version=profile,
        )

    def test_real_prompt_compiler_emits_blocking_quality_and_stable_fingerprint(self):
        self.shot.media_type = Shot.MediaType.IMAGE
        self.shot.save(update_fields=("media_type", "updated_at"))

        compiled = compile_prompt_input(
            project=self.project,
            shots=[self.shot],
            prompt_type=PromptArtifact.PromptType.IMAGE,
            mode="T2VA",
            include_audio=False,
            model_profile_version=self.model_profiles["image.generate"],
        )
        quality = compiled.structured_payload["quality"]
        report = evaluate_prompt_language_gate(SimpleNamespace(
            prompt_type=compiled.prompt_type,
            structured_payload=compiled.structured_payload,
        ))

        self.assertEqual(len(compiled.input_fingerprint), 64)
        self.assertEqual(
            compiled.input_fingerprint,
            compiled.structured_payload["input_fingerprint"],
        )
        self.assertTrue(quality["fields_requiring_english_rewrite"])
        self.assertTrue(report.blocks_enqueue)
        self.assertEqual(report.as_dict()["status"], "blocked")

        repeated = compile_prompt_input(
            project=self.project,
            shots=[self.shot],
            prompt_type=PromptArtifact.PromptType.IMAGE,
            mode="T2VA",
            include_audio=False,
            model_profile_version=self.model_profiles["image.generate"],
        )
        self.assertEqual(repeated.input_fingerprint, compiled.input_fingerprint)
        self.assertEqual(repeated.content_hash, compiled.content_hash)

    def test_prompt_input_edit_changes_fingerprint(self):
        first = compile_prompt_input(
            project=self.project,
            shots=[self.shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode="T2VA",
            include_audio=True,
            language_gate_mode="warn",
            model_profile_version=self.model_profiles["video.generate"],
            frozen_frame_policy=FRAME_POLICY,
        )
        self.shot.camera = f"{self.shot.camera} slow lateral track"
        self.shot.save(update_fields=("camera", "updated_at"))

        changed = compile_prompt_input(
            project=self.project,
            shots=[self.shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode="T2VA",
            include_audio=True,
            language_gate_mode="warn",
            model_profile_version=self.model_profiles["video.generate"],
            frozen_frame_policy=FRAME_POLICY,
        )

        self.assertNotEqual(changed.input_fingerprint, first.input_fingerprint)
        self.assertNotEqual(changed.content_hash, first.content_hash)

    def test_warn_allows_normal_rewrite_but_never_hard_integrity_failure(self):
        warning = evaluate_prompt_language_gate(_prompt(fields=("shot.camera",), mode="warn"))
        hard_failure = evaluate_prompt_language_gate(_prompt(
            fields=("shot.camera",),
            hard_fields=("canonical_target_collision",),
            mode="warn",
        ))

        self.assertFalse(warning.blocks_enqueue)
        self.assertEqual(warning.as_dict()["status"], "warning")
        self.assertTrue(hard_failure.blocks_enqueue)
        self.assertEqual(hard_failure.as_dict()["status"], "blocked")

    def test_retired_optimizer_marker_cannot_bypass_current_field_gate(self):
        historical = _prompt(
            prompt_type=PromptArtifact.PromptType.VIDEO,
            fields=("shot.action",),
        )
        historical.structured_payload["quality"]["optimizer"] = {"status": "optimized"}
        report = evaluate_prompt_language_gate(historical)
        self.assertTrue(report.requires_english_rewrite)
        self.assertTrue(report.blocks_enqueue)
        self.assertNotIn("optimizer_status", report.as_dict())

    def test_reviewed_and_non_media_prompts_do_not_report_false_blocks(self):
        reviewed = evaluate_prompt_language_gate(_prompt(prompt_type="video"))
        text = evaluate_prompt_language_gate(_prompt(
            prompt_type="text",
            fields=("script",),
        ))

        self.assertFalse(reviewed.requires_english_rewrite)
        self.assertFalse(reviewed.blocks_enqueue)
        self.assertFalse(text.applies)
        self.assertEqual(text.as_dict()["status"], "not_applicable")
