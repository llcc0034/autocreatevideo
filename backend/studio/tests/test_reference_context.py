from copy import deepcopy
from importlib import import_module
from unittest.mock import patch
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase

from studio.models import PromptProfileVersion, PromptProfileBinding
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.prompt_profile_compiler import MediaPromptCompilationContext
from studio.services.reference_context import compile_reference_context, rendering_options, validate_rendering_policy, render_reference_template, RENDERING_FIELDS
from studio.services.prompt_profiles import normalize_prompt_profile_source_projection_config
from studio.tests.factories import build_story, conditioning_contract


class ReferenceContextTests(TestCase):
    def test_primitive_template_catalog_covers_the_renderer_without_saved_defaults(self):
        from studio.services.source_projection_primitives import SOURCE_PROJECTION_PRIMITIVES, source_projection_primitive_options
        from studio.platform_serializers import PromptProfileSourceProjectionPrimitiveOptionSerializer
        from dataclasses import replace

        rows = source_projection_primitive_options()
        self.assertEqual(PromptProfileSourceProjectionPrimitiveOptionSerializer(rows, many=True).data, rows)
        self.assertEqual({key for row in rows for key in row['rendering_template_keys']}, set(RENDERING_FIELDS))
        policy = deepcopy(self.policy)
        # Keep mandatory tokens; marker flow checks that each consumed template is
        # attributed to the correct primitive, including conditional empty/audio branches.
        policy['templates'] = {key: f'[{key}] {value}' for key, value in policy['templates'].items()}
        contexts = [self.context, replace(self.context, include_audio=False),
                    replace(self.context, extra_direction='Keep continuity', reference_bindings=({'category': 'video', 'input_label': '<Video 1>', 'purpose': 'motion_camera_reference'},))]
        for context in contexts:
            result = compile_reference_context(context, policy)
            for primitive in SOURCE_PROJECTION_PRIMITIVES.values():
                output = str(result['fields'][primitive.result_field])
                used = {key for key in RENDERING_FIELDS if f'[{key}]' in output}
                self.assertLessEqual(used, set(primitive.rendering_template_keys))
        rows[0]['rendering_template_keys'].append('not_a_template')
        self.assertNotIn('not_a_template', source_projection_primitive_options()[0]['rendering_template_keys'])

    def setUp(self):
        self.actor, self.project, self.character, self.scene, self.episode, self.shot, _ = build_story(username="reference-rendering")
        from studio.tests.reference_rendering_fixtures import rendering_config
        self.policy = rendering_config()["primitive_options"]["reference_context_ir_v1"]["rendering"]
        self.mode = "VendorAnyReference"
        self.context = MediaPromptCompilationContext(
            project=self.project, shots=(self.shot,), prompt_type="video", mode=self.mode, include_audio=True,
            conditioning_mode_contract=conditioning_contract(self.mode)["modes"][0],
            frame_policy={"fps": 30, "minimum_frames": 1, "maximum_frames": 999, "alignment_modulus": 4, "alignment_remainder": 1},
        )

    def test_any_model_mode_uses_explicit_templates_and_frame_policy(self):
        result = compile_reference_context(self.context, self.policy)
        self.assertIn(self.mode, result["fields"]["summary"])
        self.assertIn("0 supplied references", result["fields"]["summary"])
        self.assertEqual(result["execution_facts"]["effective_conditioning_mode"], self.mode)
        self.policy["templates"]["summary"] = "CUSTOM {mode} {duration_seconds} {task_types} {reference_relationships}"
        self.assertTrue(compile_reference_context(self.context, self.policy)["fields"]["summary"].startswith("CUSTOM "))

    def test_missing_frozen_contracts_never_use_h3_defaults(self):
        from dataclasses import replace
        for change in ({"conditioning_mode_contract": None}, {"frame_policy": None}, {"conditioning_mode_contract": {"key": "wrong"}}):
            with self.subTest(change=change), self.assertRaises(PromptBuildError):
                compile_reference_context(replace(self.context, **change), self.policy)

    def test_previous_compiler_artifacts_are_not_current(self):
        from studio.serializers import PromptArtifactSerializer
        from studio.services.prompt_compiler_contract import PROMPT_COMPILER_VERSION
        self.assertFalse(PromptArtifactSerializer.get_compiler_current(SimpleNamespace(
            structured_payload={"prompt_compiler_version": PROMPT_COMPILER_VERSION - 1})))
        self.assertTrue(PromptArtifactSerializer.get_compiler_current(SimpleNamespace(
            structured_payload={"prompt_compiler_version": PROMPT_COMPILER_VERSION})))

    def test_templates_are_scalar_only_and_catalog_complete(self):
        self.assertEqual(set(self.policy["templates"]), {row["key"] for row in rendering_options()})
        for unsafe in ("{mode.__class__}", "{mode[0]}", "{mode!r}", "{mode:20}", "{unknown}", "{"):
            policy = deepcopy(self.policy)
            policy["templates"]["summary"] = unsafe
            with self.subTest(unsafe=unsafe), self.assertRaises(PromptBuildError):
                validate_rendering_policy(policy)
        policy = deepcopy(self.policy)
        policy["templates"]["reference"] = "reference without label"
        with self.assertRaises(PromptBuildError):
            validate_rendering_policy(policy)

    def test_rendering_help_is_complete_and_examples_use_the_real_scalar_renderer(self):
        from string import Formatter
        from studio.platform_serializers import ReferenceRenderingOptionSerializer
        before = deepcopy(self.policy)
        rows = rendering_options()
        self.assertEqual(len(rows), len(RENDERING_FIELDS))
        for row in rows:
            with self.subTest(key=row["key"]):
                self.assertEqual(set(row["placeholder_help"]), set(row["placeholders"]))
                for name in ("description", "assembly", "requirement"):
                    self.assertTrue(row[name])
                for help_row in row["placeholder_help"].values():
                    self.assertTrue(help_row["label"])
                    self.assertTrue(help_row["description"])
                    self.assertIsInstance(help_row["example"], str)
                example = row["example"]
                used = {name for _, name, _, _ in Formatter().parse(example["template"]) if name is not None}
                self.assertTrue(used.issubset(row["placeholders"]))
                self.assertEqual(example["result"], render_reference_template(example["template"], row["placeholders"],
                    {key: item["example"] for key, item in row["placeholder_help"].items()}))
                self.assertEqual(dict(ReferenceRenderingOptionSerializer(row).data), row)
        self.assertEqual(self.policy, before)
        rows[0]["placeholder_help"]["label"]["label"] = "mutated"
        self.assertNotEqual(rendering_options()[0]["placeholder_help"]["label"]["label"], "mutated")

    def test_scalar_rendering_preserves_literal_braces_empty_facts_and_does_not_recurse(self):
        self.assertEqual(render_reference_template("{{literal}} {label}; paired={parent_label}",
            ("label", "parent_label"), {"label": "{not_another_token}"}), "{literal} {not_another_token}; paired=")

    def test_reference_identity_guidance_paired_audio_and_no_paths(self):
        from dataclasses import replace
        reference = {"category": "character", "entity_id": str(self.character.pk), "entity_label": "<Subject 1>",
                     "input_label": "<Picture 1>", "prompt_hint": "KEEP GUIDANCE", "local_path": "private-path",
                     "audio_input_label": "<Audio 1>", "audio_purpose": "audio_reference"}
        result = compile_reference_context(replace(self.context, reference_bindings=(reference,)), self.policy)
        text = str(result["fields"])
        self.assertIn("<Picture 1>", text)
        self.assertIn("<Audio 1>", text)
        self.assertIn("<Subject 1>", text)
        self.assertIn("KEEP GUIDANCE", text)
        self.assertNotIn("private-path", text)
        with self.assertRaises(PromptBuildError):
            compile_reference_context(replace(self.context, reference_bindings=(reference, reference)), self.policy)

    def test_contribution_replaces_source_and_cannot_be_silently_discarded(self):
        from dataclasses import replace
        self.shot.detailed_description = "ORIGINAL-DETAIL"
        context = replace(self.context, generation_contributions={(str(self.shot.id), "shot.detailed_description"): "REVIEWED-DETAIL"})
        result = compile_reference_context(context, self.policy)
        text = str(result["fields"])
        self.assertIn("REVIEWED-DETAIL", text)
        self.assertNotIn("ORIGINAL-DETAIL", text)
        self.assertEqual(result["consumed_contributions"], frozenset(context.generation_contributions))
        self.policy["templates"]["reviewed_shot"] = "{narrative}"
        with self.assertRaises(PromptBuildError):
            compile_reference_context(context, self.policy)

    def test_subject_identity_uses_project_terminology_without_guessing_names(self):
        from studio.services.prompt_profiles import media_profile_sources
        self.project.production_settings = {"translation_glossary": {self.character.name: "Reviewed Canonical Name"}}
        self.project.save(update_fields=("production_settings", "updated_at"))
        self.assertIn("Reviewed Canonical Name", media_profile_sources(project=self.project, shots=[self.shot])["request.subject"])
        self.project.production_settings = {}
        self.project.save(update_fields=("production_settings", "updated_at"))
        self.assertTrue(media_profile_sources(project=self.project, shots=[self.shot])["request.subject"].startswith(self.character.stable_speaker_id + ":"))

    def test_translation_overlay_is_used_without_mutating_source(self):
        original = self.shot.narrative
        with patch("studio.services.prompt_profiles.confirmed_translation", side_effect=lambda **kw: "REVIEWED NARRATIVE" if kw["field_key"] == "shot.narrative" else kw["source_text"]):
            result = compile_reference_context(self.context, self.policy)
        self.assertIn("REVIEWED NARRATIVE", str(result["fields"]))
        self.assertEqual(self.shot.narrative, original)

    def test_old_policy_is_readable_but_not_implicitly_converted(self):
        old = normalize_prompt_profile_source_projection_config(self.old_config())
        self.assertNotIn("rendering", old["primitive_options"]["reference_context_ir_v1"])

    def test_runtime_audio_echo_uses_exact_reviewed_translation_only(self):
        from dataclasses import replace
        self.shot.sound_effects = "已保存的声音"
        self.shot.music = "已保存的音乐"
        context = replace(self.context, overall_soundscape=self.shot.sound_effects, non_diegetic_music=self.shot.music)
        translations = {"shot.sound_effects": "REVIEWED SOUND", "shot.music": "REVIEWED MUSIC"}
        with patch("studio.services.prompt_profiles.confirmed_translation", side_effect=lambda **kw: translations.get(kw["field_key"], kw["source_text"])):
            result = compile_reference_context(context, self.policy)
            self.assertNotIn(self.shot.sound_effects, str(result["fields"]))
            self.assertNotIn(self.shot.music, str(result["fields"]))
            self.assertIn("REVIEWED SOUND", result["fields"]["overall_soundscape"])
            self.assertIn("REVIEWED MUSIC", result["fields"]["non_diegetic_music"])
            changed = compile_reference_context(replace(context, overall_soundscape="A new explicit instruction"), self.policy)
            self.assertIn("A new explicit instruction", changed["fields"]["overall_soundscape"])

    def test_runtime_duration_override_changes_generic_fingerprint(self):
        from dataclasses import replace
        from studio.services.prompt_profile_compiler import media_prompt_input_fingerprint
        self.assertNotEqual(media_prompt_input_fingerprint(self.context),
            media_prompt_input_fingerprint(replace(self.context, duration_overrides={str(self.shot.id): 2000})))

    def test_audio_disabled_cannot_discard_reviewed_audio_and_dialogue_requires_slot(self):
        from dataclasses import replace
        for selector in ("shot.sound_effects", "shot.music"):
            context = replace(self.context, include_audio=False,
                generation_contributions={(str(self.shot.id), selector): "REVIEWED AUDIO"})
            with self.subTest(selector=selector), self.assertRaises(PromptBuildError):
                compile_reference_context(context, self.policy)
        self.shot.dialogue = [{"speaker_id": "S1", "text": "Verbatim", "voiceover": True}]
        self.policy["templates"]["shot"] = "{shot_marker} {reference_usage} {narrative}"
        with self.assertRaisesMessage(PromptBuildError, "{dialogue}"):
            compile_reference_context(self.context, self.policy)

    def test_paired_audio_uses_its_own_guidance_and_explicit_parent(self):
        from dataclasses import replace
        reference = {"category": "video", "input_label": "<Video 1>", "purpose": "motion_camera_reference",
            "prompt_hint": "VIDEO GUIDANCE", "audio_input_label": "<Audio 1>", "audio_purpose": "audio_reference",
            "audio_prompt_hint": "AUDIO GUIDANCE", "audio_description": "AUDIO DESCRIPTION"}
        result = compile_reference_context(replace(self.context, reference_bindings=(reference,)), self.policy)
        paired = result["fields"]["subject_definitions"][1]
        self.assertIn("AUDIO GUIDANCE", paired)
        self.assertIn("AUDIO DESCRIPTION", paired)
        self.assertIn("paired video=<Video 1>", paired)
        self.assertNotIn("VIDEO GUIDANCE", paired)
        self.assertIn("<Video 1>", result["fields"]["detailed_description"][-1])

    def test_current_rules_preserve_paired_audio_and_video_after_reviewed_contribution(self):
        from dataclasses import replace
        reference = {"category": "video", "input_label": "<Video 1>", "purpose": "video_continuation",
            "prompt_hint": "VIDEO REQUIREMENT", "audio_input_label": "<Audio 2>",
            "audio_purpose": "audio_reuse", "audio_prompt_hint": "AUDIO REQUIREMENT", "use_embedded_audio": True}
        context = replace(self.context, reference_bindings=(reference,),
            generation_contributions={(str(self.shot.id), "shot.detailed_description"): "REVIEWED PROSE"})
        fields = compile_reference_context(context, self.policy)["fields"]
        self.assertTrue(fields["summary"].startswith("[video continuation + audio reuse]"))
        for field in ("subject_definitions", "retention_analysis", "detailed_description", "summary"):
            with self.subTest(field=field):
                self.assertIn("<Video 1>", str(fields[field]))
                self.assertIn("<Audio 2>", str(fields[field]))
                self.assertIn("AUDIO REQUIREMENT", str(fields[field]))
        self.assertIn("partially_copy", str(fields["retention_analysis"]))
        self.assertIn("REVIEWED PROSE", str(fields["detailed_description"]))
        self.assertNotIn("[Shot 1] At", str(fields["detailed_description"]))

    def test_task_mapping_is_version_configuration_not_model_or_asset_kind_inference(self):
        from dataclasses import replace
        ref = {"category": "video", "input_label": "<Video 1>", "purpose": "vendor_custom"}
        context = replace(self.context, reference_bindings=(ref,))
        with self.assertRaisesMessage(PromptBuildError, "参考用途未配置"):
            compile_reference_context(context, self.policy)
        self.policy["reference_rules"].append({"category": "video", "purpose": "vendor_custom",
            "task": "Configured custom task", "relationship": "configured_retention", "usage": "Configured custom usage."})
        result = compile_reference_context(context, self.policy)["fields"]
        self.assertTrue(result["summary"].startswith("[Configured custom task]"))
        self.assertIn("configured_retention", str(result["retention_analysis"]))
        self.assertIn("Configured custom usage.", str(result["detailed_description"]))
        self.policy["reference_rules"].append(deepcopy(self.policy["reference_rules"][-1]))
        with self.assertRaisesMessage(PromptBuildError, "不能重复"):
            compile_reference_context(context, self.policy)

    def test_frame_and_reference_tasks_are_deduplicated_and_timecodes_use_effective_durations(self):
        from copy import copy
        from dataclasses import replace
        import uuid
        second = copy(self.shot)
        second.id = uuid.uuid4()
        refs = ({"category": "first_frame", "input_label": "<Picture 1>"},
                {"category": "video", "input_label": "<Video 1>", "purpose": "motion_camera_reference"},
                {"category": "audio", "input_label": "<Audio 1>", "purpose": "audio_reference"},
                {"category": "audio", "input_label": "<Audio 2>", "purpose": "audio_reference"})
        context = replace(self.context, shots=(self.shot, second), reference_bindings=refs,
            duration_overrides={str(self.shot.id): 5123})
        fields = compile_reference_context(context, self.policy)["fields"]
        self.assertTrue(fields["summary"].startswith("[keyframe completion + reference generation + audio reference]"))
        self.assertIn("[Shot 2] At 00:05.123,", str(fields["detailed_description"]))
        self.assertNotIn("[Shot 1] At", str(fields["detailed_description"]))

    def test_v4_rendering_remains_readable_but_cannot_execute(self):
        from studio.services.prompt_profiles import validate_prompt_profile_source_projection, PromptProfileError
        old = {"schema_version": 4, "primitive_options": {"reference_context_ir_v1": {
            "rendering": deepcopy(import_module("studio.migrations.0140_explicit_reference_rendering_drafts").RENDERING)}}}
        self.assertEqual(normalize_prompt_profile_source_projection_config(old), old)
        profile = PromptProfileVersion(source_projection_key="declarative_v1", source_projection_config=old)
        with self.assertRaises(PromptProfileError):
            validate_prompt_profile_source_projection(profile, rules=[])

    def old_config(self):
        return {"schema_version": 3, "primitive_options": {"reference_context_ir_v1": {"reference_context_policy": {
            "retention_analysis": {"include_subject_shot_usage": True, "assignment_separator": " - "},
            "detailed_description": {"style_opening_placement": "before_first_shot", "style_opening_max_sentences": 2, "subject_reference_placement": "first_clear_appearance"},
        }}}}

    def test_migration_preserves_published_content_and_active_bindings(self):
        migration = import_module("studio.migrations.0140_explicit_reference_rendering_drafts")
        source = PromptProfileVersion.objects.create(profile_key="test.old-rendering", version=1, name="Old rendering",
            model_family="vendor", operation="video.generate", mode=self.mode, status="published",
            source_projection_key="declarative_v1", source_projection_config=self.old_config())
        before = list(PromptProfileVersion.objects.filter(status="published").values("id", "source_projection_config", "contract_sha256"))
        bindings = list(PromptProfileBinding.objects.values("id", "is_active", "prompt_profile_version_id"))
        migration.prepare_review_drafts(apps, SimpleNamespace(connection=connection))
        count = PromptProfileVersion.objects.count()
        migration.prepare_review_drafts(apps, SimpleNamespace(connection=connection))
        self.assertEqual(PromptProfileVersion.objects.count(), count)
        draft = PromptProfileVersion.objects.get(profile_key=source.profile_key, version=2)
        self.assertEqual((draft.status, draft.review_state), ("draft", "needs_review"))
        self.assertEqual(draft.source_projection_config["schema_version"], 4)
        self.assertEqual(list(PromptProfileVersion.objects.filter(status="published").values("id", "source_projection_config", "contract_sha256")), before)
        self.assertEqual(list(PromptProfileBinding.objects.values("id", "is_active", "prompt_profile_version_id")), bindings)
