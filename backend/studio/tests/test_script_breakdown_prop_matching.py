from django.contrib.auth import get_user_model
from django.test import TestCase

from studio.models import Asset, PropBible, SeriesProject, Shot
from studio.services.script_breakdown import (
    _prop_core_alias_candidates,
    breakdown_script,
)


PROP_ALIAS_POLICY = {
    "schema_version": 1,
    "minimum_core_characters": 2,
    "maximum_prefix_trim_characters": 2,
    "generic_aliases": ["东西", "物件", "物品", "设备", "工具", "道具"],
}


class ScriptBreakdownPropMatchingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="script-prop-matching",
            password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16", 
            owner=self.user,
            title="道具简称拆稿",
            width=768, height=1344, fps=24, default_shot_duration_ms=5000,
            continuity_seed=1,
            production_settings={"default_conditioning_mode": "Ref2VA"},
        )

    def _prop(self, *, slug: str, name: str, checksum: str) -> PropBible:
        asset = Asset.objects.create(
            owner=self.user,
            kind=Asset.Kind.PROP,
            name=f"{name}参考图",
            local_path=f"C:/fixtures/{slug}.png",
            checksum=checksum,
            mime_type="image/png",
        )
        return PropBible.objects.create(
            project=self.project,
            slug=slug,
            name=name,
            anchor_asset=asset,
        )

    def _breakdown(self, script: str):
        return breakdown_script(
            self.project,
            script,
            episode_count_hint=1,
            multi_shot=True,
            default_shot_media_type=Shot.MediaType.VIDEO,
            default_shot_duration_ms=1_000,
            default_first_frame_continuity=False,
            default_last_frame_continuity=False,
            video_shot_min_duration_ms=100,
            video_shot_max_duration_ms=10_000,
            auto_split_trigger_chars=12_000,
            auto_split_target_chars=6_000,
            include_audio=False,
        )

    def test_core_alias_derivation_is_conservative(self):
        self.assertEqual(
            _prop_core_alias_candidates("发光怀表", PROP_ALIAS_POLICY),
            ("怀表",),
        )
        self.assertEqual(
            _prop_core_alias_candidates("发光录音机", PROP_ALIAS_POLICY),
            ("录音机",),
        )
        self.assertNotIn(
            "音机",
            _prop_core_alias_candidates("发光录音机", PROP_ALIAS_POLICY),
        )
        self.assertEqual(
            _prop_core_alias_candidates("USB-钥匙", PROP_ALIAS_POLICY),
            ("钥匙",),
        )
        self.assertEqual(
            _prop_core_alias_candidates("发光道具", PROP_ALIAS_POLICY),
            (),
        )

    def test_unique_core_name_links_same_prop_without_guessing_reference_anchor(self):
        prop = self._prop(
            slug="glowing-pocket-watch",
            name="发光怀表",
            checksum="fixture-glowing-pocket-watch",
        )

        result = self._breakdown(
            "第1集：雨夜车站\n"
            "镜头1：林默从水洼中捡起发光怀表。\n"
            "镜头2：林默将怀表举至眼前。"
        )

        shots = list(result.episodes[0].shots.order_by("number"))
        self.assertEqual(result.method, "deterministic-script-breakdown-v4")
        for shot in shots:
            with self.subTest(shot=shot.number):
                self.assertEqual(list(shot.props.values_list("id", flat=True)), [prop.id])
                self.assertNotIn("props", shot.reference_bindings)

    def test_ambiguous_core_name_fails_closed_but_full_name_still_binds(self):
        glowing = self._prop(
            slug="glowing-pocket-watch",
            name="发光怀表",
            checksum="fixture-glowing-pocket-watch",
        )
        self._prop(
            slug="antique-pocket-watch",
            name="古董怀表",
            checksum="fixture-antique-pocket-watch",
        )

        result = self._breakdown(
            "第1集：双表疑云\n"
            "镜头1：林默将怀表举至眼前。\n"
            "镜头2：林默捡起发光怀表。"
        )

        ambiguous, exact = result.episodes[0].shots.order_by("number")
        self.assertFalse(ambiguous.props.exists())
        self.assertNotIn("props", ambiguous.reference_bindings)
        self.assertEqual(list(exact.props.values_list("id", flat=True)), [glowing.id])
        self.assertNotIn("props", exact.reference_bindings)
