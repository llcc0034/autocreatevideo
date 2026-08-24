from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

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
from studio.services.json_contracts import assign_json_contract
from studio.services.production_pipeline import compile_prompt_input
from studio.services.prompt_profiles import (
    PromptProfileError,
    PromptProfileResolutionError,
    assemble_prompt_profile,
    media_profile_sources,
    model_family_for_profile,
    publish_prompt_profile,
    refresh_prompt_profile_contract,
    resolve_prompt_profile_binding,
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
from studio.tests.factories import build_story


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
        review_state=PromptProfileVersion.ReviewState.READY,
    ):
        profile = PromptProfileVersion.objects.create(
            profile_key=key,
            version=1,
            name=key,
            model_family=family,
            operation=operation,
            mode=mode,
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
            "capabilities": list(capabilities),
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
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
            label_style=PromptProfileFieldRule.LabelStyle.LABEL_COLON,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=negative,
            source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
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
            mode=Shot.H3Mode.T2VA,
            prompt_profile_version=profile,
        )

        compiled = compile_prompt_input(
            project=project,
            shots=[shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode=Shot.H3Mode.T2VA,
            include_audio=True,
            language_gate_mode="block",
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
            configured_output_token_hint=4096,
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

    def test_media_compile_requires_binding_and_profile_is_the_only_semantic_path(self):
        user, project, _character, _scene, _episode, shot, _shot2 = build_story(
            username="prompt-profile-story",
        )
        with self.assertRaises(PromptProfileResolutionError) as context:
            compile_prompt_input(
                project=project,
                shots=[shot],
                prompt_type="video",
                mode=Shot.H3Mode.T2VA,
                include_audio=True,
                language_gate_mode="warn",
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
            mode=Shot.H3Mode.T2VA,
            include_audio=True,
            language_gate_mode="warn",
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
                mode=Shot.H3Mode.T2VA,
                model_profile_version=selected_model,
                prompt_profile_version=profile,
            )
            expected.append((selected_model, binding, profile))

        compiled = [
            compile_prompt_input(
                project=project,
                shots=[shot],
                prompt_type="video",
                mode=Shot.H3Mode.T2VA,
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
            mode=Shot.H3Mode.T2VA,
            prompt_profile_version=profile,
            priority=10_000,
        )
        compiled = compile_prompt_input(
            project=project,
            shots=[shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode=Shot.H3Mode.T2VA,
            include_audio=True,
            language_gate_mode="block",
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
            mode=Shot.H3Mode.T2VA,
            prompt_profile_version=profile,
        )

        compiled = compile_prompt_input(
            project=project,
            shots=[shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode=Shot.H3Mode.T2VA,
            include_audio=True,
            extra_direction="PROFILE-FIRST",
            language_gate_mode="warn",
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
        shot.h3_mode = Shot.H3Mode.I2VA
        shot.reference_bindings = {"first_frame_asset_id": str(first_frame.id)}
        shot.save(update_fields=("h3_mode", "reference_bindings", "updated_at"))
        shot.reference_assets.set([first_frame])

        reference_field = self._field(
            key="qa_reference_binding",
            label="Reference",
            value_type=PromptFieldDefinition.ValueType.STRING_LIST,
        )
        profile = self._profile("qa.minimax.reference", mode=Shot.H3Mode.I2VA)
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
            mode=Shot.H3Mode.I2VA,
            prompt_profile_version=profile,
        )

        compiled = compile_prompt_input(
            project=project,
            shots=[shot],
            prompt_type=PromptArtifact.PromptType.VIDEO,
            mode=Shot.H3Mode.I2VA,
            include_audio=True,
            language_gate_mode="warn",
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

    def test_crud_preview_publish_binding_and_retire_lifecycle(self):
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
            "is_active": True,
            "display_order": 1,
        }, format="json")
        self.assertEqual(created_field.status_code, 201, created_field.data)

        created_profile = self.client.post("/api/v1/prompt-profile-versions/", {
            "profile_key": "api.minimax.t2va",
            "version": 1,
            "name": "API MiniMax T2VA",
            "description": "API managed strategy",
            "model_family": "minimax_h3",
            "operation": "video.generate",
            "mode": "T2VA",
            "official_reference": "",
            "review_state": "needs_review",
        }, format="json")
        self.assertEqual(created_profile.status_code, 201, created_profile.data)
        profile_id = created_profile.data["id"]

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
        }, format="json")
        self.assertEqual(created_rule.status_code, 201, created_rule.data)

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

        binding = self.client.post("/api/v1/prompt-profile-bindings/", {
            "key": "api.minimax.t2va",
            "name": "API binding",
            "operation": "video.generate",
            "model_family": "minimax_h3",
            "mode": "T2VA",
            "model_profile_version": None,
            "prompt_profile_version": profile_id,
            "priority": 0,
            "is_active": True,
        }, format="json")
        self.assertEqual(binding.status_code, 201, binding.data)

        options = self.client.get("/api/v1/prompt-profile-versions/options/")
        self.assertEqual(options.status_code, 200, options.data)
        self.assertIn("video.generate", options.data["operations"])
        self.assertIn("minimax_h3", options.data["model_families"])
        self.assertIn("T2VA", options.data["modes"])
        self.assertTrue(any(
            item["value"] == "request.theme" for item in options.data["source_selectors"]
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
