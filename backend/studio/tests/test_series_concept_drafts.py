from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from studio.models import Episode, SeriesProject, Shot
from studio.services.creative_drafts import CreativeDraftError, CreativeValidationError
from studio.services.script_breakdown import breakdown_script
from studio.services.series_concept_drafts import (
    build_series_concept_plan,
    finalize_series_concept_draft,
)


EXPECTED_RENDERED_MASTER_SCRIPT = (
    "【SCRIPT_BIBLE_V1】\n"
    "【人物】阿夜｜失忆的年轻剑客｜young swordsman in a rain-darkened robe\n"
    "【场景】客栈门外｜暴雨中的旧客栈入口｜old inn entrance under cold heavy rain\n"
    "【场景】昏暗柜台｜客栈内光线昏暗的木柜台｜dim wooden counter lit by one amber lamp\n"
    "【场景】后院水井｜潮湿后院里的石井｜mossy stone well in a wet courtyard\n"
    "【场景】屋脊之上｜雨幕中的黑瓦屋脊｜black tiled rooftop under driving rain\n"
    "【道具】染血剑穗｜牵连旧案的剑穗｜blood-stained sword tassel with frayed threads\n"
    "【/SCRIPT_BIBLE_V1】\n\n"
    "第1集：雨夜来客\n"
    "镜头1【时长=9秒】【首帧连续=否】【尾帧连续=是】：客栈门外\n"
    "[0-4.5秒] 动作：阿夜推门而入。\n"
    "[4.5-9秒] 阿夜（in a low voice）：有人吗？\n\n"
    "镜头2【时长=6秒】【首帧连续=是】【尾帧连续=否】：昏暗柜台\n"
    "[0-6秒] 动作：掌柜缓慢抬头。\n\n"
    "第2集：旧案重现\n"
    "镜头1【时长=12秒】【首帧连续=否】【尾帧连续=是】：后院水井\n"
    "[0-12秒] 动作：阿夜发现染血剑穗。\n\n"
    "镜头2【时长=5秒】【首帧连续=是】【尾帧连续=否】：屋脊之上\n"
    "[0-5秒] 动作：黑衣人跃入雨幕。"
)


def runtime_payload(plan, *, durations=((9000, 6000), (12000, 5000)), issues=()):
    settings = (("客栈门外", "昏暗柜台"), ("后院水井", "屋脊之上"))
    actions = (("阿夜推门而入。", "掌柜缓慢抬头。"), ("阿夜发现染血剑穗。", "黑衣人跃入雨幕。"))
    episodes = []
    for episode_index in range(plan["episode_count"]):
        shots = []
        for shot_index in range(plan["shots_per_episode"]):
            dialogue = (
                {"speaker": "阿夜", "delivery": "in a low voice", "text": "有人吗？"}
                if episode_index == 0 and shot_index == 0
                else {"speaker": "", "delivery": "", "text": ""}
            )
            shots.append({
                "setting": settings[episode_index][shot_index],
                "action": actions[episode_index][shot_index],
                "duration_ms": durations[episode_index][shot_index],
                "dialogue": dialogue,
            })
        episodes.append({
            "title": ("雨夜来客", "旧案重现")[episode_index],
            "shots": shots,
        })
    return {
        "source_fingerprint": plan["source_fingerprint"],
        "result": {
            "schema_version": 2,
            "title": "雨夜归人",
            "logline": "失忆剑客在两场雨夜追查师门旧案。",
            "bible": {
                "characters": [{
                    "name": "阿夜",
                    "description": "失忆的年轻剑客",
                    "visual_identity_en": "young swordsman in a rain-darkened robe",
                }],
                "scenes": [
                    {"name": "客栈门外", "description": "暴雨中的旧客栈入口", "visual_identity_en": "old inn entrance under cold heavy rain"},
                    {"name": "昏暗柜台", "description": "客栈内光线昏暗的木柜台", "visual_identity_en": "dim wooden counter lit by one amber lamp"},
                    {"name": "后院水井", "description": "潮湿后院里的石井", "visual_identity_en": "mossy stone well in a wet courtyard"},
                    {"name": "屋脊之上", "description": "雨幕中的黑瓦屋脊", "visual_identity_en": "black tiled rooftop under driving rain"},
                ],
                "props": [{
                    "name": "染血剑穗",
                    "description": "牵连旧案的剑穗",
                    "visual_identity_en": "blood-stained sword tassel with frayed threads",
                }],
            },
            "episodes": episodes,
        },
        "review_issues": list(issues),
        "model": "frozen-provider-model",
        "elapsed_ms": 19,
    }


class SeriesConceptProductionServicesTests(TestCase):
    def _plan(self, **overrides):
        values = {
            "idea": "一个失忆剑客在雨夜追查师门旧案",
            "style_hint": "冷峻雨夜武侠",
            "episode_count": 2,
            "shots_per_episode": 2,
            "episode_duration_seconds": 60,
            "minimum_shot_duration_ms": 5000,
            "maximum_shot_duration_ms": 15000,
            "model_identifier": "qwen-frozen",
            "timeout_seconds": 900,
            "configured_output_token_hint": 16384,
            "attempt": 1,
        }
        values.update(overrides)
        return build_series_concept_plan(**values)

    def test_build_plan_is_deterministic_frozen_and_attempt_scoped(self):
        first = self._plan()
        repeated = self._plan()
        retry = self._plan(attempt=2)

        self.assertEqual(first, repeated)
        self.assertEqual(first["source_fingerprint"], retry["source_fingerprint"])
        self.assertNotEqual(first["request"]["request_id"], retry["request"]["request_id"])
        self.assertNotEqual(first["request"]["seed"], retry["request"]["seed"])
        self.assertEqual(first["request"]["model_id"], "qwen-frozen")
        self.assertEqual(first["request"]["timeout_seconds"], 900.0)
        self.assertNotIn("max_new_tokens", first["request"])
        self.assertNotIn("credential", str(first).lower())

    def test_build_plan_rejects_invalid_and_physically_impossible_requests(self):
        cases = (
            ({"idea": ""}, "series_concept_idea_required"),
            ({"episode_count": 0}, "series_concept_episode_count_invalid"),
            ({"shots_per_episode": 13, "episode_duration_seconds": 60}, "series_concept_episode_duration_infeasible"),
            ({"minimum_shot_duration_ms": 16000, "maximum_shot_duration_ms": 15000}, "series_concept_duration_policy_invalid"),
        )
        for overrides, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(CreativeValidationError) as raised:
                    self._plan(**overrides)
                self.assertEqual(raised.exception.code, code)

    def test_finalize_strictly_renders_review_candidate_without_database_writes(self):
        plan = self._plan()

        result = finalize_series_concept_draft(
            plan=plan,
            runtime_payload=runtime_payload(plan),
        )

        self.assertEqual(result["candidate"]["master_script"], EXPECTED_RENDERED_MASTER_SCRIPT)
        self.assertEqual(result["format"]["shot_durations_ms"], [[9000, 6000], [12000, 5000]])
        self.assertEqual(result["format"]["total_duration_ms"], 32000)
        self.assertTrue(result["format"]["compatible"])
        self.assertTrue(any("候选包含对白" in item for item in result["warnings"]))
        self.assertFalse(SeriesProject.objects.exists())
        self.assertFalse(Episode.objects.exists())
        self.assertFalse(Shot.objects.exists())

    def test_finalize_rejects_fingerprint_drift_and_invalid_nested_contract(self):
        plan = self._plan()
        drifted = runtime_payload(plan)
        drifted["source_fingerprint"] = "0" * 64
        invalid = runtime_payload(plan)
        invalid["result"]["episodes"][0]["shots"][0].pop("action")

        for payload in (drifted, invalid):
            with self.subTest(payload=payload):
                with self.assertRaises(CreativeDraftError):
                    finalize_series_concept_draft(plan=plan, runtime_payload=payload)

    def test_invalid_known_response_reports_safe_nested_field_path(self):
        plan = self._plan()
        payload = runtime_payload(plan)
        payload["result"]["episodes"][0]["shots"][0]["dialogue"]["delivery"] = "低声"

        with self.assertRaises(CreativeDraftError) as raised:
            finalize_series_concept_draft(plan=plan, runtime_payload=payload)

        self.assertEqual(raised.exception.code, "series_concept_invalid_candidate")
        self.assertIn(
            "result.episodes[0].shots[0].dialogue",
            str(raised.exception),
        )
        self.assertNotIn("低声", str(raised.exception))

    def test_finalize_compresses_advisory_duration_overrun_without_truncating_shots(self):
        plan = self._plan(episode_duration_seconds=20)
        payload = runtime_payload(
            plan,
            durations=((15000, 10000), (15000, 10000)),
            issues=({
                "code": "series_concept_episode_duration_over_budget",
                "path": "result.episodes[0].shots",
                "message": "建议时长超出单集预算。",
                "severity": "risk",
            },),
        )

        result = finalize_series_concept_draft(plan=plan, runtime_payload=payload)

        self.assertEqual(result["format"]["shot_count"], 4)
        for durations in result["format"]["shot_durations_ms"]:
            self.assertEqual(sum(durations), 20000)
            self.assertTrue(all(value >= 5000 for value in durations))
        self.assertTrue(any("相对比例压缩" in item for item in result["warnings"]))

    @override_settings(SCRIPT_BREAKDOWN_MAX_SOURCE_CHARS=100)
    def test_finalize_reports_breakdown_budget_as_warning_without_truncation(self):
        plan = self._plan()

        result = finalize_series_concept_draft(
            plan=plan,
            runtime_payload=runtime_payload(plan),
        )

        self.assertEqual(result["candidate"]["master_script"], EXPECTED_RENDERED_MASTER_SCRIPT)
        self.assertTrue(any(
            "候选已完整返回，未截断、未分批" in item
            for item in result["warnings"]
        ))

    def test_finalized_script_round_trips_through_authoritative_breakdown(self):
        plan = self._plan()
        result = finalize_series_concept_draft(
            plan=plan,
            runtime_payload=runtime_payload(plan),
        )
        user = get_user_model().objects.create_user(username="series-roundtrip")
        project = SeriesProject.objects.create(
            owner=user,
            title=result["candidate"]["title"],
            master_script=result["candidate"]["master_script"],
        )

        breakdown = breakdown_script(project, project.master_script, episode_count_hint=2)

        self.assertEqual([episode.title for episode in breakdown.episodes], ["雨夜来客", "旧案重现"])
        self.assertEqual([episode.shots.count() for episode in breakdown.episodes], [2, 2])
        self.assertEqual(
            [
                [shot.duration_ms for shot in episode.shots.order_by("number")]
                for episode in breakdown.episodes
            ],
            [[9000, 6000], [12000, 5000]],
        )
