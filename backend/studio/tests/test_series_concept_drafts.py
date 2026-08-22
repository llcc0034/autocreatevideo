from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import Episode, SeriesProject, Shot
from studio.services.creative_drafts import (
    CreativeDraftError,
    CreativeResourceBusy,
    CreativeRuntimeUnavailable,
    CreativeValidationError,
    _runtime_metadata,
)
from studio.services.script_breakdown import breakdown_script


ENDPOINT = "/api/v1/series-concept-drafts/generate/"

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


def valid_runtime_envelope(request, _config, _provider) -> dict:
    episodes = [
        {
            "title": "雨夜来客",
            "shots": [
                        {
                            "setting": "客栈门外",
                            "action": "阿夜推门而入。",
                            "duration_ms": 9000,
                            "dialogue": {
                                "speaker": "阿夜",
                                "delivery": "in a low voice",
                                "text": "有人吗？",
                            },
                        },
                        {
                            "setting": "昏暗柜台",
                            "action": "掌柜缓慢抬头。",
                            "duration_ms": 6000,
                            "dialogue": {
                                "speaker": "",
                                "delivery": "",
                                "text": "",
                            },
                        },
            ],
        },
        {
            "title": "旧案重现",
            "shots": [
                        {
                            "setting": "后院水井",
                            "action": "阿夜发现染血剑穗。",
                            "duration_ms": 12000,
                            "dialogue": {
                                "speaker": "",
                                "delivery": "",
                                "text": "",
                            },
                        },
                        {
                            "setting": "屋脊之上",
                            "action": "黑衣人跃入雨幕。",
                            "duration_ms": 5000,
                            "dialogue": {
                                "speaker": "",
                                "delivery": "",
                                "text": "",
                            },
                        },
            ],
        },
    ]
    return {
        "source_fingerprint": request["source_fingerprint"],
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
                    {
                        "name": "客栈门外", "description": "暴雨中的旧客栈入口",
                        "visual_identity_en": "old inn entrance under cold heavy rain",
                    },
                    {
                        "name": "昏暗柜台", "description": "客栈内光线昏暗的木柜台",
                        "visual_identity_en": "dim wooden counter lit by one amber lamp",
                    },
                    {
                        "name": "后院水井", "description": "潮湿后院里的石井",
                        "visual_identity_en": "mossy stone well in a wet courtyard",
                    },
                    {
                        "name": "屋脊之上", "description": "雨幕中的黑瓦屋脊",
                        "visual_identity_en": "black tiled rooftop under driving rain",
                    },
                ],
                "props": [{
                    "name": "染血剑穗", "description": "牵连旧案的剑穗",
                    "visual_identity_en": "blood-stained sword tassel with frayed threads",
                }],
            },
            "episodes": episodes,
        },
        "review_issues": [],
        "model": "qwen-integration-double",
        "elapsed_ms": 19,
}


def scalable_runtime_envelope(request, _config, _provider) -> dict:
    options = request["options"]
    episodes = []
    for episode_index in range(1, options["episode_count"] + 1):
        shots = []
        for shot_index in range(1, options["shots_per_episode"] + 1):
            duration_ms = min(
                options["maximum_duration_ms"],
                options["minimum_duration_ms"] + ((shot_index - 1) % 3) * 1000,
            )
            shots.append({
                "setting": f"场景{episode_index}-{shot_index}",
                "action": f"人物完成动作{episode_index}-{shot_index}。",
                "duration_ms": duration_ms,
                "dialogue": {"speaker": "", "delivery": "", "text": ""},
            })
        episodes.append({"title": f"第{episode_index}章", "shots": shots})
    return {
        "source_fingerprint": request["source_fingerprint"],
        "result": {
            "schema_version": 2,
            "title": "长篇雨夜",
            "logline": "人物在多集连续事件中查明真相。",
            "bible": {
                "characters": [{
                    "name": "人物", "description": "贯穿全剧的调查者",
                    "visual_identity_en": "determined investigator in practical dark clothing",
                }],
                "scenes": [
                    {
                        "name": f"场景{episode_index}-{shot_index}",
                        "description": "连续事件发生地点",
                        "visual_identity_en": "cinematic investigation location",
                    }
                    for episode_index in range(1, options["episode_count"] + 1)
                    for shot_index in range(1, options["shots_per_episode"] + 1)
                ],
                "props": [],
            },
            "episodes": episodes,
        },
        "review_issues": [],
        "model": "qwen-scalable-double",
        "elapsed_ms": 25,
    }


def over_budget_duration_runtime_envelope(request, _config, _provider) -> dict:
    return {
        "source_fingerprint": request["source_fingerprint"],
        "result": {
            "schema_version": 2,
            "title": "一夜追凶",
            "logline": "侦探必须在倒计时结束前找到凶手。",
            "bible": {
                "characters": [{
                    "name": "林默", "description": "追查案件的侦探",
                    "visual_identity_en": "urgent detective in a dark coat",
                }],
                "scenes": [
                    {
                        "name": "废弃车站", "description": "废弃的站厅",
                        "visual_identity_en": "abandoned station hall under cold lights",
                    },
                    {
                        "name": "站台尽头", "description": "空旷站台尽头",
                        "visual_identity_en": "empty platform edge in deep shadow",
                    },
                ],
                "props": [{
                    "name": "箱子", "description": "被遗弃的线索箱",
                    "visual_identity_en": "weathered abandoned case",
                }],
            },
            "episodes": [{
                "title": "倒计时",
                "shots": [
                    {
                        "setting": "废弃车站",
                        "action": "侦探冲进空旷站厅寻找线索。",
                        "duration_ms": 15000,
                        "dialogue": {
                            "speaker": "林默",
                            "delivery": "urgently",
                            "text": "只剩二十秒。",
                        },
                    },
                    {
                        "setting": "站台尽头",
                        "action": "钟声响起，他发现被遗弃的箱子。",
                        "duration_ms": 10000,
                        "dialogue": {
                            "speaker": "",
                            "delivery": "",
                            "text": "",
                        },
                    },
                ],
            }],
        },
        "review_issues": [{
            "code": "series_concept_episode_duration_over_budget",
            "path": "result.episodes[0].shots",
            "message": "建议时长超出单集预算。",
            "severity": "risk",
        }],
        "model": "duration-integration-double",
        "elapsed_ms": 17,
    }


def repaired_runtime_envelope(request, config, provider) -> dict:
    payload = valid_runtime_envelope(request, config, provider)
    payload["review_issues"] = [
        {"code": "series_concept_structure_repaired"},
        {"code": "series_concept_structure_whitespace_recovered"},
    ]
    return payload


def valid_result() -> dict:
    return {
        "source_fingerprint": "a" * 64,
        "candidate": {
            "title": "雨夜归人",
            "logline": "失忆剑客在雨夜追查旧案。",
            "bible": {
                "characters": [{
                    "name": "剑客",
                    "description": "追查旧案的剑客",
                    "visual_identity_en": "swordsman in a dark rain robe",
                }],
                "scenes": [{
                    "name": "客栈外",
                    "description": "雨夜客栈入口",
                    "visual_identity_en": "old inn entrance under rain",
                }],
                "props": [],
            },
            "master_script": (
                "【SCRIPT_BIBLE_V1】\n"
                "【人物】剑客｜追查旧案的剑客｜swordsman in a dark rain robe\n"
                "【场景】客栈外｜雨夜客栈入口｜old inn entrance under rain\n"
                "【/SCRIPT_BIBLE_V1】\n\n"
                "第1集：雨夜来客\n"
                "镜头1【时长=5秒】【首帧连续=否】【尾帧连续=否】：客栈外。\n"
                "[0-5秒] 动作：剑客推门进入。"
            ),
        },
        "format": {
            "schema_version": 2,
            "template_id": "deterministic-script-breakdown-v3",
            "compatible": True,
            "episode_count": 1,
            "shot_count": 1,
            "episode_duration_seconds": 60,
            "total_duration_ms": 5000,
            "shot_durations_ms": [[5000]],
        },
        "resource_estimate": {
            "total_shots": 1,
            "estimated_input_chars": 2060,
            "estimated_output_tokens": 1024,
            "configured_output_token_hint": 3072,
            "output_hint_utilization_percent": 34,
        },
        "warnings": [],
        "runtime": {
            "provider": "local_qwen",
            "model": "Qwen/Qwen3-4B-GGUF",
            "transport": "local_process",
            "elapsed_ms": 12,
            "review_required": True,
            "persisted": False,
        },
    }


class SeriesConceptDraftAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="series-concept-owner",
            password="StrongPass123!",
        )

    def authenticate(self) -> None:
        self.client.force_authenticate(self.user)

    def request(self, **overrides):
        payload = {
            "idea": "一个失忆剑客在雨夜追查师门旧案",
            "provider": "local_qwen",
            "episode_count": 1,
            "shots_per_episode": 1,
        }
        payload.update(overrides)
        return self.client.post(ENDPOINT, payload, format="json")

    def test_runtime_metadata_preserves_series_operation(self):
        metadata = _runtime_metadata({
            "operation": "series_concept_draft",
            "metadata": {
                "operation": "creative_storyboard",
                "failure_phase": "inference",
            },
        })

        self.assertEqual(metadata["operation"], "series_concept_draft")
        self.assertEqual(metadata["failure_phase"], "inference")

    def test_authentication_is_required(self):
        response = self.request()

        self.assertEqual(response.status_code, 401, response.data)

    @patch(
        "studio.series_concept_draft_views.generate_series_concept_draft",
        return_value=valid_result(),
    )
    def test_success_returns_validated_detached_candidate_without_project_write(
        self, generate,
    ):
        self.authenticate()

        response = self.request(style_hint="冷峻武侠", attempt=2)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["candidate"]["title"], "雨夜归人")
        self.assertEqual(
            response.data["candidate"]["bible"]["characters"][0]["visual_identity_en"],
            "swordsman in a dark rain robe",
        )
        self.assertTrue(response.data["format"]["compatible"])
        self.assertTrue(response.data["runtime"]["review_required"])
        self.assertFalse(response.data["runtime"]["persisted"])
        self.assertEqual(SeriesProject.objects.count(), 0)
        generate.assert_called_once_with(
            idea="一个失忆剑客在雨夜追查师门旧案",
            provider="local_qwen",
            episode_count=1,
            shots_per_episode=1,
            episode_duration_seconds=60,
            style_hint="冷峻武侠",
            attempt=2,
        )

    @patch("studio.series_concept_draft_views.generate_series_concept_draft")
    def test_unknown_input_is_400_and_does_not_call_provider(self, generate):
        self.authenticate()

        response = self.request(unexpected="never forward this")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(generate.called)
        self.assertEqual(SeriesProject.objects.count(), 0)

    @patch("studio.series_concept_draft_views.generate_series_concept_draft")
    def test_service_input_validation_is_400(self, generate):
        self.authenticate()
        generate.side_effect = CreativeValidationError(
            "单集时长无效", code="series_concept_episode_duration_infeasible",
        )

        response = self.request()

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "series_concept_episode_duration_infeasible",
        )
        self.assertEqual(SeriesProject.objects.count(), 0)

    @patch("studio.series_concept_draft_views.generate_series_concept_draft")
    def test_local_resource_busy_is_409_without_raw_exception_text(self, generate):
        self.authenticate()
        secret = r"shared lock C:\private\local-qwen.lock"
        generate.side_effect = CreativeResourceBusy(secret)

        response = self.request()

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "series_concept_resource_busy",
        )
        self.assertNotIn(secret, str(response.data))
        self.assertEqual(SeriesProject.objects.count(), 0)

    @patch("studio.series_concept_draft_views.generate_series_concept_draft")
    def test_local_runtime_unavailable_is_503_without_raw_exception_text(
        self, generate,
    ):
        self.authenticate()
        secret = r"model missing at C:\private\models\qwen.gguf"
        generate.side_effect = CreativeRuntimeUnavailable(secret)

        response = self.request()

        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "series_concept_runtime_unavailable",
        )
        self.assertNotIn(secret, str(response.data))

    @patch("studio.series_concept_draft_views.generate_series_concept_draft")
    def test_deepseek_disabled_and_misconfigured_are_503(self, generate):
        self.authenticate()
        for code in ("deepseek_api_disabled", "deepseek_api_misconfigured"):
            with self.subTest(code=code):
                secret = f"raw upstream body for {code}"
                generate.side_effect = CreativeDraftError(secret, code=code)

                response = self.request(provider="deepseek_api")

                self.assertEqual(response.status_code, 503, response.data)
                self.assertEqual(response.data["error"]["code"], code)
                self.assertNotIn(secret, str(response.data))
                self.assertEqual(SeriesProject.objects.count(), 0)

    @patch("studio.series_concept_draft_views.generate_series_concept_draft")
    def test_other_upstream_failure_is_502_and_does_not_leak_content(self, generate):
        self.authenticate()
        secret = "Authorization: Bearer secret-token; raw provider response"
        generate.side_effect = CreativeDraftError(
            secret,
            code="deepseek_api_invalid_response",
            runtime_metadata={
                "raw_body": secret,
                "model": secret,
                "api_key": secret,
                "failure_phase": "inference",
                "finish_reason": "length",
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 45,
                    "total_tokens": 165,
                    "raw_provider_content": secret,
                },
            },
        )

        with patch("studio.series_concept_draft_views.logger") as logger:
            response = self.request(provider="deepseek_api")

        self.assertEqual(response.status_code, 502, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "series_concept_provider_invalid_response",
        )
        self.assertNotIn(secret, str(response.data))
        diagnostic = logger.warning.call_args.kwargs["extra"][
            "series_concept_diagnostic"
        ]
        self.assertRegex(diagnostic["diagnostic_id"], r"^[0-9a-f]{32}$")
        self.assertEqual({
            key: value for key, value in diagnostic.items()
            if key != "diagnostic_id"
        }, {
            "provider": "deepseek_api",
            "error_code": "series_concept_provider_invalid_response",
            "failure_phase": "inference",
            "finish_reason": "length",
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 45,
                "total_tokens": 165,
            },
            "details_available": False,
            "details": {},
        })
        public_diagnostic = response.data["error"]["details"]["diagnostic"]
        self.assertEqual(public_diagnostic["error_code"], diagnostic["error_code"])
        self.assertEqual(public_diagnostic["usage"], diagnostic["usage"])
        self.assertNotIn(secret, str(logger.warning.call_args))
        self.assertEqual(SeriesProject.objects.count(), 0)

    @patch("studio.series_concept_draft_views.generate_series_concept_draft")
    def test_deepseek_length_failure_exposes_only_expandable_redacted_diagnostic(
        self, generate,
    ):
        self.authenticate()
        secret = "PRIVATE-AUTHORED-STORY-SENTINEL"
        safe_shape = (
            '{"title":"<redacted_text>","episodes":['
            '{"shots":[{"action":"<unterminated_text>"'
        )
        generate.side_effect = CreativeDraftError(
            secret,
            code="series_concept_draft_output_truncated",
            runtime_metadata={
                "failure_phase": "provider_output",
                "finish_reason": "length",
                "usage": {
                    "prompt_tokens": 781,
                    "completion_tokens": 8192,
                    "total_tokens": 8973,
                },
                "attempts": 1,
                "elapsed_ms": 115000,
                "incomplete": True,
                "requested_max_tokens": 384000,
                "configured_max_tokens": 384000,
                "configured_timeout_seconds": 900,
                "output_chars": 32768,
                "output_utf8_bytes": 65536,
                "output_sha256": "a" * 64,
                "redacted_output_shape": safe_shape,
                "raw_body": secret,
                "api_key": secret,
            },
        )

        with patch("studio.series_concept_draft_views.logger") as logger:
            response = self.request(provider="deepseek_api")

        self.assertEqual(response.status_code, 502, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "series_concept_provider_output_truncated",
        )
        details = response.data["error"]["details"]
        self.assertIn("未截断、未分批", details["detail"])
        diagnostic = details["diagnostic"]
        self.assertRegex(diagnostic["diagnostic_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(diagnostic["finish_reason"], "length")
        self.assertEqual(diagnostic["usage"]["completion_tokens"], 8192)
        self.assertTrue(diagnostic["details_available"])
        self.assertEqual(diagnostic["details"]["configured_max_tokens"], 384000)
        self.assertEqual(diagnostic["details"]["configured_timeout_seconds"], 900)
        self.assertEqual(
            diagnostic["details"]["output"],
            {
                "character_count": 32768,
                "utf8_byte_count": 65536,
                "sha256": "a" * 64,
                "redacted_json_shape": safe_shape,
            },
        )
        self.assertEqual(
            logger.warning.call_args.kwargs["extra"]["series_concept_diagnostic"],
            diagnostic,
        )
        self.assertNotIn(secret, str(response.data))
        self.assertNotIn(secret, str(logger.warning.call_args))
        self.assertEqual(SeriesProject.objects.count(), 0)

    @patch("studio.series_concept_draft_views.generate_series_concept_draft")
    def test_runtime_transport_failures_have_stable_series_stage_codes(
        self, generate,
    ):
        self.authenticate()
        cases = (
            (
                "creative_parent_timeout",
                {},
                "parent_process",
                "series_concept_parent_timeout",
            ),
            (
                "creative_invalid_runtime_json",
                {},
                "transport_decode",
                "series_concept_runtime_protocol_failed",
            ),
            (
                "creative_runtime_exit",
                {},
                "child_process_exit",
                "series_concept_runtime_exit",
            ),
            (
                "series_concept_model_load_failed",
                {"failure_phase": "model_load"},
                "model_load",
                "series_concept_model_load_failed",
            ),
            (
                "series_concept_draft_output_invalid_json",
                {},
                "provider_decode",
                "series_concept_draft_output_invalid_json",
            ),
        )
        with patch("studio.series_concept_draft_views.logger") as logger:
            for raw_code, runtime_metadata, failure_phase, public_code in cases:
                with self.subTest(raw_code=raw_code):
                    logger.reset_mock()
                    generate.side_effect = CreativeDraftError(
                        "private runtime detail",
                        code=raw_code,
                        runtime_metadata=runtime_metadata,
                    )

                    response = self.request()

                    self.assertEqual(response.status_code, 502, response.data)
                    self.assertEqual(response.data["error"]["code"], public_code)
                    diagnostic = logger.warning.call_args.kwargs["extra"][
                        "series_concept_diagnostic"
                    ]
                    self.assertRegex(diagnostic["diagnostic_id"], r"^[0-9a-f]{32}$")
                    self.assertEqual({
                        key: value for key, value in diagnostic.items()
                        if key != "diagnostic_id"
                    }, {
                        "provider": "local_qwen",
                        "error_code": public_code,
                        "failure_phase": failure_phase,
                        "details_available": False,
                        "details": {},
                    })
                    self.assertNotIn("private runtime detail", str(response.data))
        self.assertEqual(SeriesProject.objects.count(), 0)

    @patch(
        "studio.services.series_concept_drafts._run_provider_runtime",
        side_effect=lambda request, _config, _provider: {
            "source_fingerprint": request["source_fingerprint"],
            "result": {
                "schema_version": 1,
                "title": "雨夜归人",
                "logline": "失忆剑客追查旧案。",
                "episodes": [{"title": "雨夜来客", "shots": [{"setting": "客栈"}]}],
            },
            "review_issues": [],
            "model": "drifted-runtime",
            "elapsed_ms": 1,
        },
    )
    def test_drifted_success_envelope_with_invalid_nested_ir_is_safe_502(
        self, runtime,
    ):
        self.authenticate()

        response = self.request()

        self.assertEqual(response.status_code, 502, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "series_concept_invalid_candidate",
        )
        self.assertNotIn("drifted-runtime", str(response.data))
        self.assertEqual(SeriesProject.objects.count(), 0)
        runtime.assert_called_once()

    @patch(
        "studio.series_concept_draft_views.generate_series_concept_draft",
        return_value={
            **valid_result(),
            "candidate": {
                **valid_result()["candidate"],
                "title": "x" * 61,
            },
        },
    )
    def test_invalid_service_response_is_502_not_a_false_success(self, _generate):
        self.authenticate()

        response = self.request()

        self.assertEqual(response.status_code, 502, response.data)
        self.assertEqual(
            response.data["error"]["code"], "series_concept_invalid_response",
        )
        self.assertNotIn("x" * 61, str(response.data))
        self.assertEqual(SeriesProject.objects.count(), 0)

    @patch(
        "studio.services.series_concept_drafts._run_provider_runtime",
        side_effect=valid_runtime_envelope,
    )
    def test_real_service_renders_exact_review_only_script(self, runtime):
        self.authenticate()

        response = self.request(
            episode_count=2,
            shots_per_episode=2,
            style_hint="冷峻雨夜武侠",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["candidate"]["master_script"],
            EXPECTED_RENDERED_MASTER_SCRIPT,
        )
        self.assertEqual(response.data["format"]["episode_count"], 2)
        self.assertEqual(response.data["format"]["shot_count"], 4)
        self.assertEqual(response.data["format"]["episode_duration_seconds"], 60)
        self.assertEqual(response.data["format"]["total_duration_ms"], 32000)
        self.assertEqual(
            response.data["format"]["shot_durations_ms"],
            [[9000, 6000], [12000, 5000]],
        )
        self.assertEqual(
            response.data["warnings"],
            [
                "候选包含对白；创建项目后请先人工确认并应用 SCRIPT_BIBLE_V1 人物候选，"
                "再执行自动分集分镜，否则未确认的实体会明确阻止拆解。"
            ],
        )
        runtime.assert_called_once()
        runtime_request = runtime.call_args.args[0]
        self.assertEqual(runtime_request["options"]["episode_count"], 2)
        self.assertEqual(runtime_request["options"]["shots_per_episode"], 2)
        self.assertEqual(runtime_request["options"]["minimum_duration_ms"], 5000)
        self.assertEqual(runtime_request["options"]["maximum_duration_ms"], 15000)
        self.assertEqual(runtime_request["options"]["episode_duration_ms"], 60000)
        self.assertNotIn("max_new_tokens", runtime_request)
        self.assertEqual(SeriesProject.objects.count(), 0)
        self.assertEqual(Episode.objects.count(), 0)
        self.assertEqual(Shot.objects.count(), 0)

    @override_settings(SCRIPT_BREAKDOWN_MAX_SOURCE_CHARS=100)
    @patch(
        "studio.services.series_concept_drafts._run_provider_runtime",
        side_effect=valid_runtime_envelope,
    )
    def test_breakdown_budget_is_warning_and_never_truncates_candidate(
        self, runtime,
    ):
        self.authenticate()

        response = self.request(episode_count=2, shots_per_episode=2)

        self.assertEqual(response.status_code, 200, response.data)
        runtime.assert_called_once()
        self.assertEqual(
            response.data["candidate"]["master_script"],
            EXPECTED_RENDERED_MASTER_SCRIPT,
        )
        self.assertTrue(response.data["format"]["compatible"])
        self.assertTrue(any(
            "超过当前拆解服务配置的 100 字符参考预算" in warning
            and "候选已完整返回，未截断、未分批" in warning
            for warning in response.data["warnings"]
        ), response.data["warnings"])
        self.assertEqual(SeriesProject.objects.count(), 0)

    @patch(
        "studio.services.series_concept_drafts._run_provider_runtime",
        side_effect=repaired_runtime_envelope,
    )
    def test_runtime_structure_recovery_is_visible_for_human_review(
        self, runtime,
    ):
        self.authenticate()

        response = self.request(episode_count=2, shots_per_episode=2)

        self.assertEqual(response.status_code, 200, response.data)
        runtime.assert_called_once()
        self.assertTrue(any(
            "运行时已完成一次结构修复" in warning
            for warning in response.data["warnings"]
        ), response.data["warnings"])
        self.assertTrue(any(
            "可确定恢复的字符串空白" in warning
            for warning in response.data["warnings"]
        ), response.data["warnings"])
        self.assertEqual(SeriesProject.objects.count(), 0)

    @patch(
        "studio.services.series_concept_drafts._run_provider_runtime",
        side_effect=valid_runtime_envelope,
    )
    def test_rendered_script_round_trips_through_authoritative_breakdown(
        self, runtime,
    ):
        self.authenticate()

        response = self.request(episode_count=2, shots_per_episode=2)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(SeriesProject.objects.count(), 0)
        project = SeriesProject.objects.create(
            owner=self.user,
            title=response.data["candidate"]["title"],
            logline=response.data["candidate"]["logline"],
            master_script=response.data["candidate"]["master_script"],
        )
        result = breakdown_script(
            project,
            project.master_script,
            episode_count_hint=2,
        )

        self.assertEqual(len(result.episodes), 2)
        self.assertEqual(
            [episode.title for episode in result.episodes],
            ["雨夜来客", "旧案重现"],
        )
        self.assertEqual(
            [episode.shots.count() for episode in result.episodes],
            [2, 2],
        )
        self.assertEqual(Episode.objects.filter(project=project).count(), 2)
        self.assertEqual(
            Shot.objects.filter(episode__project=project).count(),
            4,
        )
        for episode, expected_durations in zip(
            result.episodes,
            ([9000, 6000], [12000, 5000]),
        ):
            parsed_shots = list(episode.shots.order_by("number"))
            self.assertEqual(
                [shot.duration_ms for shot in parsed_shots],
                expected_durations,
            )
            self.assertEqual(
                [
                    (
                        shot.requires_first_frame_continuity,
                        shot.requires_last_frame_continuity,
                    )
                    for shot in parsed_shots
                ],
                [(False, True), (True, False)],
            )
        self.assertTrue(
            any("对白人物“阿夜”未在角色 bible 中找到" in warning for warning in result.warnings),
            result.warnings,
        )
        runtime.assert_called_once()

    @override_settings(
        LOCAL_QWEN_CREATIVE={
            "context_size": 8192,
            "min_context_size": 8192,
            "max_context_size": 32768,
        },
    )
    @patch(
        "studio.services.series_concept_drafts._run_provider_runtime",
        side_effect=valid_runtime_envelope,
    )
    def test_long_idea_uses_configured_physical_context_without_output_cap(
        self, runtime,
    ):
        self.authenticate()

        response = self.request(
            idea="雨" * 5900,
            episode_count=2,
            shots_per_episode=2,
        )

        self.assertEqual(response.status_code, 200, response.data)
        runtime.assert_called_once()
        runtime_request, runtime_config, provider = runtime.call_args.args
        self.assertEqual(provider, "local_qwen")
        self.assertEqual(
            runtime_request["context_size"],
            runtime_config["context_size"],
        )
        self.assertNotIn("max_new_tokens", runtime_request)

    @override_settings(
        LOCAL_QWEN_CREATIVE={
            "max_new_tokens": 3072,
            "context_size": 8192,
            "min_context_size": 8192,
            "max_context_size": 8192,
        },
    )
    @patch(
        "studio.services.series_concept_drafts._run_provider_runtime",
        side_effect=scalable_runtime_envelope,
    )
    def test_large_scale_is_one_untruncated_call_despite_configured_hints(
        self, runtime,
    ):
        self.authenticate()

        response = self.request(
            episode_count=10,
            shots_per_episode=10,
            episode_duration_seconds=60,
        )

        self.assertEqual(response.status_code, 200, response.data)
        runtime.assert_called_once()
        runtime_request, runtime_config, provider = runtime.call_args.args
        self.assertEqual(provider, "local_qwen")
        self.assertEqual(runtime_request["options"]["episode_count"], 10)
        self.assertEqual(runtime_request["options"]["shots_per_episode"], 10)
        self.assertNotIn("batch_context", runtime_request)
        self.assertNotIn("max_new_tokens", runtime_request)
        self.assertEqual(
            runtime_request["context_size"],
            runtime_config["max_context_size"],
        )
        self.assertEqual(response.data["format"]["episode_count"], 10)
        self.assertEqual(response.data["format"]["shot_count"], 100)
        self.assertEqual(len(response.data["format"]["shot_durations_ms"]), 10)
        self.assertTrue(all(
            len(row) == 10
            and sum(row) <= 60000
            for row in response.data["format"]["shot_durations_ms"]
        ))
        self.assertEqual(response.data["resource_estimate"], {
            "total_shots": 100,
            "estimated_input_chars": (
                len("一个失忆剑客在雨夜追查师门旧案") + 2048
            ),
            "estimated_output_tokens": 20512,
            "configured_output_token_hint": 3072,
            "output_hint_utilization_percent": 668,
        })
        self.assertTrue(any(
            "本次候选按一次完整业务请求处理，不截断、不按集/镜分批" in warning
            and "实际能否完成由所选模型和运行时决定" in warning
            for warning in response.data["warnings"]
        ), response.data["warnings"])
        self.assertEqual(SeriesProject.objects.count(), 0)
        self.assertEqual(Episode.objects.count(), 0)
        self.assertEqual(Shot.objects.count(), 0)

    @override_settings(
        DEEPSEEK_API={
            "enabled": True,
            "model": "deepseek-v4-flash",
            "timeout_seconds": 180,
            "series_concept_timeout_seconds": 900,
            "max_tokens": 16384,
            "series_concept_max_tokens": 384000,
        },
    )
    @patch(
        "studio.services.series_concept_drafts._run_provider_runtime",
        side_effect=scalable_runtime_envelope,
    )
    def test_large_deepseek_scale_uses_model_capability_and_long_timeout_once(
        self, runtime,
    ):
        self.authenticate()

        response = self.request(
            provider="deepseek_api",
            episode_count=10,
            shots_per_episode=10,
            episode_duration_seconds=60,
        )

        self.assertEqual(response.status_code, 200, response.data)
        runtime.assert_called_once()
        runtime_request, runtime_config, provider = runtime.call_args.args
        self.assertEqual(provider, "deepseek_api")
        self.assertEqual(runtime_request["options"]["episode_count"], 10)
        self.assertEqual(runtime_request["options"]["shots_per_episode"], 10)
        self.assertEqual(runtime_request["timeout_seconds"], 900)
        self.assertEqual(runtime_config["series_concept_max_tokens"], 384000)
        self.assertNotIn("batch_context", runtime_request)
        self.assertNotIn("max_new_tokens", runtime_request)
        self.assertEqual(response.data["resource_estimate"], {
            "total_shots": 100,
            "estimated_input_chars": (
                len("一个失忆剑客在雨夜追查师门旧案") + 2048
            ),
            "estimated_output_tokens": 20512,
            "configured_output_token_hint": 384000,
            "output_hint_utilization_percent": 6,
        })
        self.assertFalse(response.data["runtime"]["persisted"])
        self.assertEqual(SeriesProject.objects.count(), 0)
        self.assertEqual(Episode.objects.count(), 0)
        self.assertEqual(Shot.objects.count(), 0)

    @patch(
        "studio.services.series_concept_drafts._run_provider_runtime",
        side_effect=over_budget_duration_runtime_envelope,
    )
    def test_episode_duration_is_compressed_and_time_ranges_are_recomputed(
        self, runtime,
    ):
        self.authenticate()

        response = self.request(
            episode_count=1,
            shots_per_episode=2,
            episode_duration_seconds=20,
        )

        self.assertEqual(response.status_code, 200, response.data)
        runtime.assert_called_once()
        self.assertEqual(
            response.data["format"]["shot_durations_ms"],
            [[11667, 8333]],
        )
        self.assertEqual(response.data["format"]["total_duration_ms"], 20000)
        script = response.data["candidate"]["master_script"]
        self.assertIn("镜头1【时长=11.667秒】", script)
        self.assertIn("[0-5.833秒] 动作：", script)
        self.assertIn("[5.833-11.667秒] 林默（urgently）：", script)
        self.assertIn("镜头2【时长=8.333秒】", script)
        self.assertIn("[0-8.333秒] 动作：", script)
        self.assertTrue(any(
            "服务端已按各镜建议时长的相对比例压缩" in warning
            for warning in response.data["warnings"]
        ), response.data["warnings"])
        self.assertEqual(SeriesProject.objects.count(), 0)

    @patch("studio.services.series_concept_drafts._run_provider_runtime")
    def test_episode_duration_rejects_only_physically_impossible_shot_count(
        self, runtime,
    ):
        self.authenticate()

        response = self.request(
            episode_count=1,
            shots_per_episode=13,
            episode_duration_seconds=60,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(runtime.called)
        self.assertIn(
            "episode_duration_seconds",
            response.data["error"]["details"],
        )
        self.assertEqual(SeriesProject.objects.count(), 0)
