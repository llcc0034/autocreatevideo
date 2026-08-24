from unittest.mock import patch

from django.test import TestCase

from studio.models import Asset, PropBible, Shot
from studio.services.h3_prompts import (
    H3PromptOptions,
    PromptBuildError,
    SDXL_BASE_MODEL_ID,
    build_consistent_image_prompt,
    build_h3_video_prompt,
    build_negative_prompt,
    prompt_input_fingerprint,
    sync_consistency_locks,
)
from studio.tests.factories import build_story


class H3PromptTests(TestCase):
    def setUp(self):
        self.user, self.project, self.character, _, self.episode, self.shot1, self.shot2 = build_story()
        self.shots = [self.shot1, self.shot2]

    @staticmethod
    def _options(**kwargs):
        # These unit tests inspect deterministic prompt composition. Production
        # resolves the block/warn policy from the frozen stage configuration.
        kwargs.setdefault("language_gate_mode", "warn")
        return H3PromptOptions(**kwargs)

    @staticmethod
    def _image(*args, **kwargs):
        kwargs.setdefault("language_gate_mode", "warn")
        return build_consistent_image_prompt(*args, **kwargs)

    def test_base_h3_field_order_timestamps_and_dialogue(self):
        result = build_h3_video_prompt(self.project, self.shots, self._options(mode=Shot.H3Mode.T2VA))
        text = result.text
        self.assertLess(text.index("integrated_multimodal_description:"), text.index("overall_soundscape:"))
        self.assertLess(text.index("overall_soundscape:"), text.index("non_diegetic_music:"))
        self.assertIn("[Shot 1]", text)
        self.assertNotIn("At 00:00.000 [Shot 1]", text)
        self.assertIn("[Shot 2] At 00:03.500,", text)
        self.assertIn(
            "The on-screen character with 冷静低声 (S1) says: "
            "<d>[Chinese] 这封信不是昨晚烧的。</d>",
            text,
        )
        self.assertEqual(result.total_duration_ms, 5600)
        self.assertEqual(result.payload["model"], "minimax-h3")

    def test_image_prompt_uses_sdxl_identity_and_keeps_library_assets_as_text_anchors(self):
        result = self._image(self.project, self.shot1)

        self.assertEqual(result.payload["model"], SDXL_BASE_MODEL_ID)
        self.assertEqual(result.payload["effective_conditioning_mode"], "TEXT_ONLY")
        self.assertEqual(result.payload["prompt_template_key"], "sdxl_txt2img_bible_v1")
        self.assertFalse(result.payload["pixel_reference_inputs"])
        self.assertEqual(result.payload["reference_bindings"], [])
        self.assertEqual(result.payload["reference_assets"], [])
        self.assertGreaterEqual(len(result.payload["continuity_assets"]), 2)
        self.assertEqual(result.payload["quality"]["optimizer"]["status"], "not_configured")
        self.assertTrue(result.payload["quality"]["fields_requiring_english_rewrite"])
        self.assertNotIn("MiniMax H3", result.text)
        self.assertNotIn("purpose:", result.text)
        self.assertNotIn("style:", result.text)
        self.assertNotIn("composition:", result.text)
        self.assertNotIn("subjects:", result.text)
        self.assertNotIn("props:", result.text)
        self.assertNotIn("\n", result.text)
        self.assertNotIn(".,", result.text)
        self.assertNotIn(";,", result.text)

    def test_applied_high_detail_is_video_only_once_and_invalidates_prompt_cache(self):
        sentinel = (
            "A rain-lit detective pauses at the archive desk, then turns toward the door."
        )
        options = self._options(mode=Shot.H3Mode.T2VA)
        before = prompt_input_fingerprint(
            self.project, [self.shot1], options,
            prompt_type="video", consistency_snapshot={}, reference_bindings=[],
        )
        self.shot1.detailed_description = sentinel
        self.shot1.save(update_fields=("detailed_description", "updated_at"))
        after = prompt_input_fingerprint(
            self.project, [self.shot1], options,
            prompt_type="video", consistency_snapshot={}, reference_bindings=[],
        )

        video = build_h3_video_prompt(self.project, [self.shot1], options)
        image = self._image(self.project, self.shot1)
        self.assertNotEqual(before, after)
        self.assertEqual(video.text.count(sentinel), 1)
        self.assertNotIn(sentinel, build_negative_prompt(self.project, [self.shot1]))
        self.assertNotIn(sentinel, image.text)

    def test_image_prompt_filters_empty_subject_and_prop_sections(self):
        self.shot2.characters.clear()
        self.shot2.props.clear()

        result = self._image(self.project, self.shot2)

        self.assertEqual(result.payload["fields"]["subjects"], [])
        self.assertEqual(result.payload["fields"]["props"], [])
        self.assertNotIn("subjects:", result.text)
        self.assertNotIn("props:", result.text)
        self.assertNotIn(", ,", result.text)

    def test_image_prompt_prioritizes_continuity_before_style_budget(self):
        self.shot1.metadata = {"prompt_field_version": 2, "negative_prompt": ""}
        self.shot1.continuity_notes = "the same charcoal coat and one sheathed sword"
        self.shot1.save(update_fields=("metadata", "continuity_notes", "updated_at"))

        result = self._image(self.project, self.shot1)

        self.assertLess(
            result.text.index("the same charcoal coat and one sheathed sword"),
            result.text.index(self.project.style.visual_prompt.rstrip(" .。;；,，")),
        )

    def test_image_language_report_preserves_authored_screen_text_but_flags_chinese_direction(self):
        self.shot1.narrative = (
            "A locked-off medium shot of the detective holding a placard "
            "<screen_text>雨夜客栈</screen_text>."
        )
        self.shot1.camera = "缓慢推近她的面部"
        self.shot1.save(update_fields=("narrative", "camera", "updated_at"))

        result = self._image(self.project, self.shot1)
        fields = result.payload["quality"]["fields_requiring_english_rewrite"]

        self.assertIn("<screen_text>雨夜客栈</screen_text>", result.text)
        self.assertIn("镜头 1 机位与构图", fields)
        self.assertNotIn("镜头 1 静态构图", fields)

    def test_first_last_workflow_with_zero_frames_uses_distinct_i2v_zero_template(self):
        self.shot1.h3_mode = Shot.H3Mode.FL2VA
        self.shot1.reference_bindings = {}
        self.shot1.save(update_fields=("h3_mode", "reference_bindings", "updated_at"))
        result = build_h3_video_prompt(self.project, [self.shot1], self._options(mode=Shot.H3Mode.FL2VA))
        self.assertEqual(result.payload["effective_conditioning_mode"], Shot.H3Mode.T2VA)
        self.assertEqual(result.payload["prompt_template_key"], "h3_i2v_zero_frame_v1")
        self.assertNotIn("<Picture", result.text)

    def test_ref2va_field_order(self):
        result = build_h3_video_prompt(self.project, self.shots, self._options(mode=Shot.H3Mode.REF2VA))
        field_names = [
            "subject_definitions:", "summary:", "retention_analysis:",
            "detailed_description:", "overall_soundscape:", "non_diegetic_music:",
        ]
        positions = [result.text.index(name) for name in field_names]
        self.assertEqual(positions, sorted(positions))

    def test_ref2va_single_shot_summary_excludes_episode_and_other_shots(self):
        self.episode.synopsis = "第一镜（0:00-0:04）发现证物，第二镜（0:04-0:08）追逐嫌疑人。"
        self.episode.save(update_fields=("synopsis", "updated_at"))

        result = build_h3_video_prompt(
            self.project, [self.shot2], self._options(mode=Shot.H3Mode.REF2VA),
        )

        self.assertTrue(result.payload["fields"]["summary"].startswith("[reference generation]"))
        self.assertNotIn(self.episode.synopsis, result.text)
        self.assertNotIn(self.shot1.title, result.text)
        self.assertNotIn(self.shot1.narrative, result.text)
        self.assertEqual(result.payload["shot_durations_ms"], [2100])
        self.assertEqual(result.total_duration_ms, 2100)
        self.assertEqual(len(result.payload["fields"]["detailed_description"]), 2)
        self.assertTrue(
            result.payload["fields"]["detailed_description"][0].startswith(
                "The target video uses "
            )
        )
        self.assertIn(
            self.shot2.narrative.rstrip("。"),
            result.payload["fields"]["detailed_description"][1],
        )

    def test_duration_override_drives_timestamp(self):
        result = build_h3_video_prompt(self.project, self.shots, self._options(
            duration_overrides={str(self.shot1.id): 1250}
        ))
        self.assertIn("[Shot 2] At 00:01.250,", result.text)

    def test_t2va_prompt_keeps_official_three_sections_and_no_picture_labels(self):
        result = build_h3_video_prompt(self.project, self.shots, self._options(mode=Shot.H3Mode.T2VA))
        self.assertEqual(
            [line.split(":", 1)[0] for line in result.text.splitlines() if line.endswith(":")],
            ["integrated_multimodal_description"],
        )
        self.assertIn("overall_soundscape:", result.text)
        self.assertIn("non_diegetic_music:", result.text)
        self.assertNotIn("<Picture", result.text)

    def test_audio_disabled_uses_na_fields(self):
        result = build_h3_video_prompt(self.project, [self.shot1], self._options(include_audio=False))
        self.assertIn("overall_soundscape: N/A", result.text)
        self.assertIn("non_diegetic_music: N/A", result.text)

    def test_ref2va_zero_reference_uses_bible_text_definitions(self):
        # Character anchor wins even if other references are later added.
        alternate_portrait = Asset.objects.create(
            owner=self.user, style=self.project.style, kind=Asset.Kind.CHARACTER,
            name="alternate portrait", local_path="C:/fixtures/alternate.png",
        )
        self.character.reference_assets.add(alternate_portrait)
        # This prop exercises the deterministic first-reference fallback when
        # there is no anchor asset.
        prop_reference = Asset.objects.create(
            owner=self.user, style=self.project.style, kind=Asset.Kind.PROP,
            name="case file", local_path="C:/fixtures/case-file.png",
        )
        prop = PropBible.objects.create(
            project=self.project, slug="case-file", name="case file",
            visual_identity="worn brown evidence folder", material="paperboard",
            distinctive_marks="blue wax seal", continuity_state="closed",
            anchor_asset=None,
        )
        prop.reference_assets.add(prop_reference)
        self.shot1.props.add(prop)

        sync_consistency_locks(self.project)
        first = build_h3_video_prompt(self.project, self.shots, self._options(mode=Shot.H3Mode.REF2VA))
        self.assertEqual(first.payload["reference_bindings"], [])
        self.assertEqual(first.payload["prompt_template_key"], "h3_r2v_zero_reference_v1")
        self.assertTrue(any("text-defined from the continuity Bible" in line for line in first.payload["fields"]["character_definitions"]))
        self.assertTrue(any("text-defined from the continuity Bible" in line for line in first.payload["fields"]["environment_definitions"]))
        self.assertTrue(any("text-defined from the continuity Bible" in line for line in first.payload["fields"]["prop_definitions"]))
        self.assertNotIn("<Picture", first.text)

        # The saved lock preserves the original mappings after new alternatives
        # appear in the material library.
        late_reference = Asset.objects.create(
            owner=self.user, style=self.project.style, kind=Asset.Kind.PROP,
            name="later case file", local_path="C:/fixtures/later-case-file.png",
        )
        prop.reference_assets.add(late_reference)
        sync_consistency_locks(self.project)
        second = build_h3_video_prompt(self.project, self.shots, self._options(mode=Shot.H3Mode.REF2VA))
        self.assertEqual(second.payload["reference_bindings"], [])

    def test_negative_prompt_includes_project_setting_and_continuity_notes(self):
        self.project.production_settings = {"negative_prompt": "no watermark"}
        self.project.save(update_fields=("production_settings",))
        self.shot1.continuity_notes = "do not change the grey coat"
        self.shot1.save(update_fields=("continuity_notes",))

        negative = build_negative_prompt(self.project, [self.shot1])
        self.assertEqual(
            negative,
            "no watermark; face drift; costume drift; do not change the grey coat",
        )

    def test_action_and_global_extra_direction_are_deduplicated(self):
        direction = "Raise the silver lantern slowly"
        self.shot1.narrative = direction
        self.shot1.action = f"  {direction}.  "

        result = build_h3_video_prompt(
            self.project,
            [self.shot1],
            self._options(
                mode=Shot.H3Mode.T2VA,
                extra_direction=f"{direction.upper()}.",
            ),
        )

        self.assertEqual(result.text.casefold().count(direction.casefold()), 1)
        self.assertNotIn("Across all shots", result.text)

    def test_legacy_negative_constraints_never_enter_positive_prompt_and_are_deduplicated(self):
        self.project.production_settings = {
            "negative_prompt": "extra limbs; no watermark",
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        self.project.style.negative_prompt = "extra limbs; face drift"
        self.project.style.save(update_fields=("negative_prompt", "updated_at"))
        self.shot1.metadata = {}
        self.shot1.continuity_notes = "no watermark, extra limbs；extra limbs，face drift"

        result = build_h3_video_prompt(
            self.project,
            [self.shot1],
            self._options(mode=Shot.H3Mode.T2VA),
        )
        negative = build_negative_prompt(self.project, [self.shot1])

        self.assertNotIn("extra limbs", result.text)
        self.assertNotIn("no watermark", result.text)
        self.assertNotIn("face drift", result.text)
        self.assertEqual(negative, "extra limbs; no watermark; face drift")

    def test_multi_shot_prompt_emits_style_only_once(self):
        style_direction = "cinematic silver-halide nocturne"
        self.project.style.visual_prompt = style_direction
        self.project.style.save(update_fields=("visual_prompt", "updated_at"))

        result = build_h3_video_prompt(
            self.project,
            self.shots,
            self._options(mode=Shot.H3Mode.T2VA),
        )

        self.assertEqual(result.text.count(style_direction), 1)
        self.assertIn("[Shot 2] At 00:03.500,", result.text)

    def test_framing_and_color_script_enter_opening_shot(self):
        self.shot1.metadata = {"framing": "特写"}
        self.project.production_settings = {
            "color_script": "teal shadows and restrained amber highlights",
        }
        self.project.save(update_fields=("production_settings", "updated_at"))

        result = build_h3_video_prompt(
            self.project,
            [self.shot1],
            self._options(mode=Shot.H3Mode.T2VA),
        )

        first_shot = result.payload["fields"]["integrated_multimodal_description"][0]
        self.assertIn("a close-up establishes the opening composition", first_shot)
        self.assertIn(
            "The color and lighting design follows teal shadows and restrained amber highlights",
            first_shot,
        )

    def test_project_visual_rules_keep_positive_and_negative_channels_separate(self):
        color_script = "restrained teal palette with soft amber lighting"
        continuity_summary = "Maintain stable project-wide continuity sentinel"
        negative_prompt = "visual-negative-sentinel"
        self.project.production_settings = {
            "color_script": color_script,
            "negative_prompt": negative_prompt,
        }
        self.project.continuity_summary = continuity_summary
        self.project.save(update_fields=(
            "production_settings", "continuity_summary", "updated_at",
        ))

        video = build_h3_video_prompt(
            self.project, [self.shot1], self._options(mode=Shot.H3Mode.T2VA),
        )
        reference_video = build_h3_video_prompt(
            self.project, [self.shot1], self._options(mode=Shot.H3Mode.REF2VA),
        )
        image = self._image(self.project, self.shot1)

        for result in (video, reference_video, image):
            with self.subTest(mode=result.payload["mode"]):
                self.assertEqual(result.text.count(color_script), 1)
                self.assertNotIn(negative_prompt, result.text)
        self.assertNotIn(continuity_summary, video.text)
        self.assertNotIn(continuity_summary, reference_video.text)
        self.assertIn(continuity_summary, image.text)
        self.assertIn(negative_prompt, build_negative_prompt(self.project, [self.shot1]))
    def test_positive_story_in_shot_negative_hard_blocks_video_and_image_in_warn_mode(self):
        narrative = (
            "The detective unfolds the sealed letter beneath the green desk lamp "
            "and studies its burned edge."
        )
        self.shot1.narrative = narrative
        self.shot1.action = ""
        self.shot1.metadata = {
            "prompt_field_version": 2,
            "negative_prompt": narrative,
        }
        self.shot1.save(update_fields=(
            "narrative", "action", "metadata", "updated_at",
        ))

        video = build_h3_video_prompt(
            self.project, [self.shot1], self._options(mode=Shot.H3Mode.T2VA),
        )
        image = self._image(self.project, self.shot1)

        for result in (video, image):
            with self.subTest(mode=result.payload["mode"]):
                quality = result.payload["quality"]
                self.assertEqual(quality["production_language_gate"]["mode"], "warn")
                self.assertTrue(
                    quality["production_language_gate"]["would_block_enqueue"]
                )
                self.assertTrue(any(
                    "疑似混入正向" in field
                    for field in quality["fields_requiring_english_rewrite"]
                ))
    def test_legitimate_english_negative_does_not_hard_block_warn_mode(self):
        self.shot1.metadata = {
            "prompt_field_version": 2,
            "negative_prompt": "no modern objects",
        }
        self.shot1.save(update_fields=("metadata", "updated_at"))

        video = build_h3_video_prompt(
            self.project, [self.shot1], self._options(mode=Shot.H3Mode.T2VA),
        )
        image = self._image(self.project, self.shot1)

        self.assertFalse(
            video.payload["quality"]["production_language_gate"]["would_block_enqueue"],
            video.payload["quality"],
        )
        self.assertFalse(
            image.payload["quality"]["production_language_gate"]["would_block_enqueue"],
            image.payload["quality"],
        )
    def test_chinese_shot_negative_hard_blocks_video_and_image_in_warn_mode(self):
        self.shot1.metadata = {
            "prompt_field_version": 2,
            "negative_prompt": "不要出现现代车辆或水印",
        }
        self.shot1.save(update_fields=("metadata", "updated_at"))

        video = build_h3_video_prompt(
            self.project, [self.shot1], self._options(mode=Shot.H3Mode.T2VA),
        )
        image = self._image(self.project, self.shot1)

        self.assertTrue(
            video.payload["quality"]["production_language_gate"]["would_block_enqueue"]
        )
        self.assertTrue(
            image.payload["quality"]["production_language_gate"]["would_block_enqueue"]
        )
    def test_canonical_target_collision_hard_blocks_video_and_image(self):
        prop = PropBible.objects.create(
            project=self.project, slug="xuanwen-sword", name="玄纹剑",
            visual_identity="a silver-gray sword with subtle dark patterns",
        )
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {
                self.character.name: "Ming   Shao",
                prop.name: "Ｍｉｎｇ Shao",
            },
        }
        self.project.save(update_fields=("production_settings", "updated_at"))

        video = build_h3_video_prompt(
            self.project, [self.shot1], self._options(mode=Shot.H3Mode.T2VA),
        )
        image = self._image(self.project, self.shot1)

        for result in (video, image):
            with self.subTest(mode=result.payload["mode"]):
                quality = result.payload["quality"]
                self.assertEqual(
                    quality["integrity_errors"][0]["code"],
                    "canonical_target_collision",
                )
                self.assertIn(
                    "canonical_target_collision",
                    quality["fields_requiring_english_rewrite"],
                )
                self.assertTrue(
                    quality["production_language_gate"]["would_block_enqueue"]
                )

    def test_quality_warns_for_chinese_director_prose_but_not_chinese_dialogue(self):
        self.project.style.visual_prompt = "live-action cinematic noir"
        self.project.style.save(update_fields=("visual_prompt", "updated_at"))
        self.character.age_appearance = "a woman in her thirties"
        self.character.visual_identity = "a short-haired detective with a mole under her left eye"
        self.character.face_identity = "high cheekbones and dark-brown eyes"
        self.character.wardrobe = "a charcoal-grey trench coat"
        self.character.voice_profile = "a calm low voice"
        self.character.save(update_fields=(
            "age_appearance", "visual_identity", "face_identity", "wardrobe",
            "voice_profile", "updated_at",
        ))
        self.shot1.scene.visual_identity = "a narrow archive room with floating dust"
        self.shot1.scene.location = "an old municipal records building"
        self.shot1.scene.time_of_day = "midnight"
        self.shot1.scene.lighting = "a green desk lamp and lightning through the window"
        self.shot1.scene.color_palette = "deep teal, charcoal, and muted amber"
        self.shot1.scene.ambient_sound = "rain on glass and an old clock ticking"
        self.shot1.scene.save(update_fields=(
            "visual_identity", "location", "time_of_day", "lighting", "color_palette",
            "ambient_sound", "updated_at",
        ))
        self.shot1.narrative = "The detective unfolds a charred letter."
        self.shot1.action = "She studies the burn pattern without changing position."
        self.shot1.camera = "The camera pushes in slowly toward the letter."
        self.shot1.sound_effects = "Paper rustles against the wooden desk."
        self.shot1.music = "Sparse low strings at a slow tempo."

        dialogue_only = build_h3_video_prompt(
            self.project,
            [self.shot1],
            self._options(mode=Shot.H3Mode.T2VA, language="Chinese"),
        )
        self.assertIn("<d>[Chinese] 这封信不是昨晚烧的。</d>", dialogue_only.text)
        self.assertEqual(dialogue_only.payload["quality"]["status"], "ready")
        self.assertFalse(
            dialogue_only.payload["quality"]["production_language_gate"]["would_block_enqueue"]
        )
        self.assertEqual(
            dialogue_only.payload["quality"]["fields_requiring_english_rewrite"],
            [],
        )

        self.shot1.action = "她慢慢推开铁门。"
        chinese_direction = build_h3_video_prompt(
            self.project,
            [self.shot1],
            self._options(mode=Shot.H3Mode.T2VA, language="Chinese"),
        )
        quality = chinese_direction.payload["quality"]
        self.assertEqual(quality["status"], "needs_attention")
        self.assertIn("镜头 1 主体动作", quality["fields_requiring_english_rewrite"])
        self.assertFalse(any("对白" in field for field in quality["fields_requiring_english_rewrite"]))

    def test_quality_does_not_repeat_identical_audio_override_labels(self):
        self.shot1.sound_effects = "雨水敲击瓦片。"
        self.shot1.music = "低沉古琴缓慢进入。"
        self.shot1.save(update_fields=("sound_effects", "music", "updated_at"))

        result = build_h3_video_prompt(
            self.project,
            [self.shot1],
            self._options(
                mode=Shot.H3Mode.T2VA,
                language="Chinese",
                overall_soundscape=self.shot1.sound_effects.rstrip("。"),
                non_diegetic_music=self.shot1.music.rstrip("。"),
            ),
        )

        fields = result.payload["quality"]["fields_requiring_english_rewrite"]
        self.assertIn("镜头 1 声景", fields)
        self.assertIn("镜头 1 非叙事音乐", fields)
        self.assertNotIn("本次整体声景", fields)
        self.assertNotIn("本次非叙事音乐", fields)

    def test_quality_does_not_repeat_identical_narrative_and_action_labels(self):
        direction = "林默缓慢拔出长剑"
        self.shot1.narrative = direction
        self.shot1.action = f"  {direction}。  "

        result = build_h3_video_prompt(
            self.project,
            [self.shot1],
            self._options(mode=Shot.H3Mode.T2VA, language="Chinese"),
        )

        fields = result.payload["quality"]["fields_requiring_english_rewrite"]
        self.assertIn("镜头 1 动作与画面描述", fields)
        self.assertNotIn("镜头 1 主体动作", fields)
        self.assertEqual(result.text.count(direction), 1)

    def test_audio_disabled_omits_audio_only_sources_from_quality_gate(self):
        self.shot1.sound_effects = "剑刃划过石阶"
        self.shot1.music = "低沉古琴"
        self.character.voice_profile = "克制的低声"

        result = build_h3_video_prompt(
            self.project,
            [self.shot1],
            self._options(
                mode=Shot.H3Mode.T2VA,
                include_audio=False,
                overall_soundscape="雷声贴近屋檐",
                non_diegetic_music="战鼓渐强",
            ),
        )

        fields = result.payload["quality"]["fields_requiring_english_rewrite"]
        self.assertNotIn("镜头 1 声景", fields)
        self.assertNotIn("镜头 1 非叙事音乐", fields)
        self.assertNotIn("人物 S1 声线", fields)
        self.assertNotIn("本次整体声景", fields)
        self.assertNotIn("本次非叙事音乐", fields)
        self.assertIn("overall_soundscape: N/A", result.text)
        self.assertIn("non_diegetic_music: N/A", result.text)

    def test_unclassified_chinese_reference_role_enters_language_gate(self):
        self.character.age_appearance = "a woman in her thirties"
        self.character.visual_identity = "a short-haired detective"
        self.character.face_identity = "high cheekbones and dark-brown eyes"
        self.character.wardrobe = "a charcoal-grey trench coat"
        self.character.voice_profile = "a calm low voice"
        self.character.save(update_fields=(
            "age_appearance", "visual_identity", "face_identity", "wardrobe",
            "voice_profile", "updated_at",
        ))
        self.shot1.scene.visual_identity = "a narrow archive room"
        self.shot1.scene.location = "an old municipal records building"
        self.shot1.scene.time_of_day = "midnight"
        self.shot1.scene.lighting = "a green desk lamp"
        self.shot1.scene.color_palette = "deep teal and muted amber"
        self.shot1.scene.ambient_sound = "rain on glass"
        self.shot1.scene.save(update_fields=(
            "visual_identity", "location", "time_of_day", "lighting",
            "color_palette", "ambient_sound", "updated_at",
        ))
        self.shot1.narrative = "The detective opens the archive door."
        self.shot1.action = "She steps inside and stops beside the desk."
        self.shot1.camera = "The camera pushes in slowly."
        self.shot1.sound_effects = "The door hinge creaks once."
        self.shot1.music = "Sparse low strings at a slow tempo."
        binding = {
            "category": "video",
            "entity_id": None,
            "input_label": "<Video 1>",
            "entity_label": "",
            "prompt_hint": "只参考这个视频的运镜节奏",
            "description": "",
            "audio_input_label": None,
        }

        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=[binding],
        ):
            result = build_h3_video_prompt(
                self.project,
                [self.shot1],
                self._options(mode=Shot.H3Mode.REF2VA),
            )

        quality = result.payload["quality"]
        self.assertEqual(
            quality["fields_requiring_english_rewrite"],
            ["未归类导演描述（参考素材职责或绑定说明）"],
        )
        self.assertFalse(quality["production_language_gate"]["would_block_enqueue"])
        self.assertFalse(any("对白" in item for item in quality["fields_requiring_english_rewrite"]))

        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=[binding],
        ), self.assertRaisesRegex(PromptBuildError, "prompt.normalize"):
            build_h3_video_prompt(
                self.project,
                [self.shot1],
                self._options(
                    mode=Shot.H3Mode.REF2VA,
                    language_gate_mode="block",
                ),
            )

    def test_prompt_over_official_7000_character_limit_is_rejected(self):
        self.shot1.narrative = "A" * 7_100

        with self.assertRaisesRegex(PromptBuildError, "7000 字符上限"):
            build_h3_video_prompt(
                self.project,
                [self.shot1],
                self._options(mode=Shot.H3Mode.T2VA),
            )

    def test_keyframe_modes_use_actual_17k_plus_5_aligned_duration(self):
        cases = (
            (
                Shot.H3Mode.I2VA,
                [{"category": "first_frame", "input_label": "<Picture 1>"}],
                "at 0.00 seconds into the target video",
            ),
            (
                Shot.H3Mode.FL2VA,
                [
                    {"category": "first_frame", "input_label": "<Picture 1>"},
                    {"category": "last_frame", "input_label": "<Picture 2>"},
                ],
                "aligns with the 5.17-second mark of the target video",
            ),
            (
                Shot.H3Mode.L2VA,
                [{"category": "last_frame", "input_label": "<Picture 1>"}],
                "aligns with the 5.17-second mark of the target video",
            ),
        )
        for mode, bindings, expected in cases:
            with self.subTest(mode=mode), patch(
                "studio.services.h3_prompts.resolve_group_reference_bindings",
                return_value=bindings,
            ):
                result = build_h3_video_prompt(
                    self.project,
                    [self.shot1],
                    self._options(
                        mode=mode,
                        duration_overrides={str(self.shot1.id): 5_000},
                    ),
                )
                self.assertEqual(result.payload["effective_conditioning_mode"], mode)
                self.assertIn(expected, result.text)
                self.assertNotIn("5.00-second", result.text)

    def test_ref2va_uses_six_fields_and_separates_subject_from_picture_source(self):
        binding = {
            "category": "character",
            "entity_id": str(self.character.id),
            "entity_name": self.character.name,
            "input_label": "<Picture 1>",
            "entity_label": "<Subject 1>",
            "name": "canonical detective portrait",
        }
        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=[binding],
        ):
            result = build_h3_video_prompt(
                self.project,
                [self.shot1],
                self._options(mode=Shot.H3Mode.REF2VA),
            )

        headers = [
            "subject_definitions:",
            "summary:",
            "retention_analysis:",
            "detailed_description:",
            "overall_soundscape:",
            "non_diegetic_music:",
        ]
        positions = [result.text.index(header) for header in headers]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(
            "<Subject 1> is the on-screen character defined by <Picture 1>",
            result.text,
        )
        self.assertIn(
            "The opening composition positions the visible subjects as <Subject 1>",
            result.text,
        )
        self.assertIn("<Subject 1> (S1) says:", result.text)
        self.assertIn("<Subject 1>: fully_preserved", result.text)
        self.assertNotIn("<Picture 1> is the on-screen character", result.text)

    def test_ref2va_summary_uses_only_official_task_types_and_concrete_roles(self):
        binding = {
            "category": "character",
            "entity_id": str(self.character.id),
            "entity_name": self.character.name,
            "input_label": "<Picture 1>",
            "entity_label": "<Subject 1>",
            "name": "canonical detective portrait",
        }
        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=[binding],
        ):
            result = build_h3_video_prompt(
                self.project,
                [self.shot1],
                self._options(mode=Shot.H3Mode.REF2VA),
            )

        summary = result.payload["fields"]["summary"]
        self.assertTrue(summary.startswith("[reference generation]"))
        self.assertIn("<Subject 1> is sourced from <Picture 1>", summary)
        self.assertNotIn("image reference", summary)
        self.assertNotIn("motion/camera reference", summary)

    def test_ref2va_video_editing_and_audio_reuse_roles_drive_prompt_contract(self):
        binding = {
            "category": "video",
            "entity_id": None,
            "input_label": "<Video 1>",
            "entity_label": "",
            "purpose": "video_editing",
            "prompt_hint": "Replace only the detective's final hand movement.",
            "audio_input_label": "<Audio 1>",
            "audio_purpose": "audio_reuse",
            "audio_asset_id": None,
            "use_embedded_audio": True,
        }
        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=[binding],
        ):
            result = build_h3_video_prompt(
                self.project,
                [self.shot1],
                self._options(mode=Shot.H3Mode.REF2VA),
            )

        summary = result.payload["fields"]["summary"]
        retention = result.payload["fields"]["media_retention"]
        self.assertTrue(summary.startswith("[video editing + audio reuse]"))
        self.assertIn(
            "The target video is an edited version of <Video 1>.",
            summary,
        )
        self.assertIn(
            "<Audio 1> is the enabled embedded soundtrack of <Video 1> and is reused",
            summary,
        )
        self.assertTrue(any(
            line.startswith("<Video 1> (source video edit): partially_preserved")
            for line in retention
        ))
        self.assertTrue(any(
            line.startswith("<Audio 1>: partially_copy")
            for line in retention
        ))

    def test_ref2va_external_paired_audio_is_not_claimed_as_video_soundtrack(self):
        binding = {
            "category": "video",
            "entity_id": None,
            "input_label": "<Video 1>",
            "entity_label": "",
            "purpose": "motion_camera_reference",
            "audio_input_label": "<Audio 1>",
            "audio_purpose": "audio_reference",
            "audio_asset_id": "external-audio-id",
            "audio_name": "measured rain rhythm",
            "use_embedded_audio": False,
        }
        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=[binding],
        ):
            result = build_h3_video_prompt(
                self.project,
                [self.shot1],
                self._options(mode=Shot.H3Mode.REF2VA),
            )

        definitions = result.payload["fields"]["media_definitions"]
        summary = result.payload["fields"]["summary"]
        self.assertTrue(any(
            "the separately supplied soundtrack asset measured rain rhythm paired with <Video 1>"
            in line for line in definitions
        ))
        self.assertIn(
            "<Audio 1> is the separately supplied audio paired with <Video 1>",
            summary,
        )
        self.assertNotIn("embedded soundtrack", summary)

    def test_ref2va_video_continuation_role_preserves_ending_state(self):
        binding = {
            "category": "video",
            "entity_id": None,
            "input_label": "<Video 1>",
            "entity_label": "",
            "purpose": "video_continuation",
            "prompt_hint": "Continue the same rain direction and camera path.",
            "audio_input_label": None,
        }
        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=[binding],
        ):
            result = build_h3_video_prompt(
                self.project,
                [self.shot1],
                self._options(mode=Shot.H3Mode.REF2VA),
            )

        summary = result.payload["fields"]["summary"]
        retention = result.payload["fields"]["media_retention"]
        self.assertTrue(summary.startswith("[video continuation]"))
        self.assertIn("The target video continues from <Video 1>.", summary)
        self.assertTrue(any(
            line.startswith("<Video 1> (continuation source): fully_preserved")
            for line in retention
        ))

    def test_ref2va_unknown_media_purpose_fails_safe(self):
        binding = {
            "category": "video",
            "entity_id": None,
            "input_label": "<Video 1>",
            "entity_label": "",
            "purpose": "copy_everything",
            "audio_input_label": None,
        }
        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=[binding],
        ), self.assertRaisesRegex(
            PromptBuildError,
            "Unsupported Ref2VA video purpose: copy_everything",
        ):
            build_h3_video_prompt(
                self.project,
                [self.shot1],
                self._options(mode=Shot.H3Mode.REF2VA),
            )

    def test_ref2va_rejects_ambiguous_editing_and_continuation_mix(self):
        bindings = [
            {
                "category": "video",
                "entity_id": None,
                "input_label": "<Video 1>",
                "entity_label": "",
                "purpose": "video_editing",
                "audio_input_label": None,
            },
            {
                "category": "video",
                "entity_id": None,
                "input_label": "<Video 2>",
                "entity_label": "",
                "purpose": "video_continuation",
                "audio_input_label": None,
            },
        ]
        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=bindings,
        ), self.assertRaisesRegex(
            PromptBuildError,
            "不支持在同一任务中混用 video_editing 与 video_continuation",
        ):
            build_h3_video_prompt(
                self.project,
                [self.shot1],
                self._options(mode=Shot.H3Mode.REF2VA),
            )

    def test_ref2va_style_precedes_first_shot_and_first_appearance_is_explicit(self):
        binding = {
            "category": "character",
            "entity_id": str(self.character.id),
            "entity_name": self.character.name,
            "input_label": "<Picture 1>",
            "entity_label": "<Subject 1>",
            "name": "canonical detective portrait",
        }
        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=[binding],
        ):
            result = build_h3_video_prompt(
                self.project,
                [self.shot1],
                self._options(mode=Shot.H3Mode.REF2VA),
            )

        detail = result.payload["fields"]["detailed_description"]
        self.assertTrue(detail[0].startswith("The target video uses "))
        self.assertTrue(detail[1].startswith("[Shot 1]"))
        self.assertIn(
            "The opening composition positions the visible subjects as <Subject 1>, preserving",
            detail[1],
        )
        self.assertIn("<Subject 1> (S1) says:", detail[1])

    def test_synchronized_sound_effects_are_written_into_the_shot_timeline(self):
        result = build_h3_video_prompt(
            self.project,
            [self.shot1],
            self._options(mode=Shot.H3Mode.T2VA),
        )

        timeline = result.payload["fields"]["integrated_multimodal_description"][0]
        self.assertIn(
            f"Synchronized diegetic sound: {self.shot1.sound_effects}",
            timeline,
        )
        self.assertIn(self.shot1.sound_effects, result.payload["fields"]["overall_soundscape"])
