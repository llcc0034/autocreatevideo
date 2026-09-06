from django.contrib.auth import get_user_model
from django.test import TestCase

from studio.models import Episode, SeriesProject, Shot
from studio.services.script_breakdown import breakdown_script
from studio.services.series_concept_drafts import (
    SERIES_CONCEPT_DRAFT_SCHEMA_VERSION,
    SeriesConceptDraftError,
    SeriesConceptDraftValidationError,
    build_series_concept_plan,
    finalize_series_concept_draft,
)
from studio.services.text_generation_queue import provider_payload
from studio.tests.factories import (
    series_concept_field_contract,
    series_concept_resource_advisory,
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
    "[0-9秒] 动作：阿夜推门而入。\n"
    "[2.25-6.75秒] 阿夜（in a low voice）：有人吗？\n\n"
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
            duration_ms = durations[episode_index][shot_index]
            dialogue = (
                {
                    "speaker": "阿夜",
                    "delivery": "in a low voice",
                    "text": "有人吗？",
                    "start_ms": duration_ms // 4,
                    "end_ms": duration_ms * 3 // 4,
                }
                if episode_index == 0 and shot_index == 0
                else {
                    "speaker": "",
                    "delivery": "",
                    "text": "",
                    "start_ms": 0,
                    "end_ms": 0,
                }
            )
            shots.append({
                "setting": settings[episode_index][shot_index],
                "action": actions[episode_index][shot_index],
                "duration_ms": duration_ms,
                "dialogue": dialogue,
            })
        episodes.append({
            "title": ("雨夜来客", "旧案重现")[episode_index],
            "shots": shots,
        })
    return {
        "source_fingerprint": plan["source_fingerprint"],
        "result": {
            "schema_version": SERIES_CONCEPT_DRAFT_SCHEMA_VERSION,
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
            "chat_request_options": {
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
            },
            "configured_output_token_hint": 16384,
            "field_contract": series_concept_field_contract(),
            "resource_advisory": series_concept_resource_advisory(),
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

    def test_field_contract_is_frozen_listed_for_provider_and_used_by_finalize(self):
        contract = series_concept_field_contract()
        contract["title"]["max_chars"] = 4
        contract["bible.entity.description"]["max_chars"] = 4
        plan = self._plan(field_contract=contract)
        contract["title"]["max_chars"] = 100
        contract["bible.entity.description"]["max_chars"] = 100

        self.assertEqual(plan["field_contract"]["title"]["max_chars"], 4)
        system_prompt = provider_payload(plan)["messages"][0]["content"]
        self.assertIn("FIELD_CONTRACT", system_prompt)
        self.assertIn(
            "- title: required/non-empty; single-line; max 4 characters",
            system_prompt,
        )
        self.assertIn(
            "- bible.entity.description: optional; single-line; max 4 characters",
            system_prompt,
        )

        invalid_title = runtime_payload(plan)
        invalid_title["result"]["title"] = "雨夜归人录"
        invalid_bible = runtime_payload(plan)
        for payload in (invalid_title, invalid_bible):
            with self.subTest(field=("title" if payload is invalid_title else "bible")):
                with self.assertRaises(SeriesConceptDraftError) as raised:
                    finalize_series_concept_draft(plan=plan, runtime_payload=payload)
                self.assertEqual(raised.exception.code, "series_concept_invalid_candidate")

    def test_build_plan_rejects_invalid_and_physically_impossible_requests(self):
        cases = (
            ({"idea": ""}, "series_concept_idea_required"),
            ({"episode_count": 0}, "series_concept_episode_count_invalid"),
            ({"shots_per_episode": 13, "episode_duration_seconds": 60}, "series_concept_episode_duration_infeasible"),
            ({"minimum_shot_duration_ms": 16000, "maximum_shot_duration_ms": 15000}, "series_concept_duration_policy_invalid"),
        )
        for overrides, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(SeriesConceptDraftValidationError) as raised:
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
        self.assertEqual(
            result["format"]["template_id"],
            "deterministic-script-breakdown-v4",
        )
        self.assertEqual(
            result["candidate"]["master_script"].count("[2.25-6.75秒]"),
            1,
        )
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
                with self.assertRaises(SeriesConceptDraftError):
                    finalize_series_concept_draft(plan=plan, runtime_payload=payload)

    def test_unattended_candidate_rejects_unused_bible_canonical_name(self):
        plan = self._plan(require_all_bible_entities_used=True)
        payload = runtime_payload(plan)
        payload["result"]["episodes"][1]["shots"][0]["action"] = (
            "阿夜发现一件染血的饰物。"
        )

        with self.assertRaises(SeriesConceptDraftError) as raised:
            finalize_series_concept_draft(plan=plan, runtime_payload=payload)

        self.assertEqual(
            raised.exception.code,
            "series_concept_bible_entity_unused",
        )
        self.assertIn("道具“染血剑穗”", str(raised.exception))

    def test_invalid_known_response_reports_safe_nested_field_path(self):
        plan = self._plan()
        payload = runtime_payload(plan)
        payload["result"]["episodes"][0]["shots"][0]["dialogue"]["delivery"] = "低声"

        with self.assertRaises(SeriesConceptDraftError) as raised:
            finalize_series_concept_draft(plan=plan, runtime_payload=payload)

        self.assertEqual(raised.exception.code, "series_concept_invalid_candidate")
        self.assertIn(
            "result.episodes[0].shots[0].dialogue",
            str(raised.exception),
        )
        self.assertNotIn("低声", str(raised.exception))

    def test_finalize_rejects_missing_out_of_range_and_nonzero_silent_dialogue_clocks(self):
        plan = self._plan()
        cases = []

        missing = runtime_payload(plan)
        missing["result"]["episodes"][0]["shots"][0]["dialogue"].pop("start_ms")
        cases.append(missing)

        outside = runtime_payload(plan)
        outside["result"]["episodes"][0]["shots"][0]["dialogue"]["end_ms"] = 9_001
        cases.append(outside)

        silent = runtime_payload(plan)
        silent["result"]["episodes"][0]["shots"][1]["dialogue"]["end_ms"] = 1
        cases.append(silent)

        for payload in cases:
            with self.subTest(dialogue=payload["result"]["episodes"][0]["shots"]):
                with self.assertRaises(SeriesConceptDraftError) as raised:
                    finalize_series_concept_draft(plan=plan, runtime_payload=payload)
                self.assertEqual(raised.exception.code, "series_concept_invalid_candidate")
                self.assertIn("dialogue", str(raised.exception))

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
        first_dialogue = result["candidate"]["master_script"]
        self.assertIn("[2.916-8.751秒] 阿夜", first_dialogue)

    def test_finalize_reports_breakdown_budget_as_warning_without_truncation(self):
        advisory = series_concept_resource_advisory()
        advisory["script_breakdown_reference_chars"] = 100
        plan = self._plan(resource_advisory=advisory)

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
            width=768, height=1344, fps=24, default_shot_duration_ms=5000,
            continuity_seed=1,
            language="Chinese", aspect_ratio="9:16",
            master_script=result["candidate"]["master_script"],
            production_settings={"default_conditioning_mode": "T2VA"},
        )

        breakdown = breakdown_script(
            project,
            project.master_script,
            episode_count_hint=2,
            multi_shot=True,
            include_audio=True,
            default_shot_media_type=Shot.MediaType.VIDEO,
        )

        self.assertEqual([episode.title for episode in breakdown.episodes], ["雨夜来客", "旧案重现"])
        self.assertEqual([episode.shots.count() for episode in breakdown.episodes], [2, 2])
        self.assertEqual(
            [
                [shot.duration_ms for shot in episode.shots.order_by("number")]
                for episode in breakdown.episodes
            ],
            [[9000, 6000], [12000, 5000]],
        )
