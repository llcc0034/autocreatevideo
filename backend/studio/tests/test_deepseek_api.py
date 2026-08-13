import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings
import requests
from rest_framework.test import APIClient

from studio.models import EpisodeCreativeDraft
from studio.services.deepseek_api import (
    DeepSeekAPIError,
    DeepSeekConfigurationError,
    get_deepseek_capability,
    run_structured_operation,
    run_translation,
)
from studio.services.prompt_translation import TranslationResult
from studio.tests.factories import build_story


CONFIG = {
    "enabled": True,
    "base_url": "https://api.deepseek.com",
    "api_key": "test-secret-key",
    "model": "deepseek-v4-flash",
    "timeout_seconds": 12,
    "max_retries": 0,
    "max_tokens": 4096,
}


def response(content, *, status=200, finish_reason="stop", usage=None):
    value = Mock(status_code=status)
    value.json.return_value = {
        "choices": [{
            "message": {"content": content},
            "finish_reason": finish_reason,
        }],
        "usage": usage or {
            "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
        },
    }
    return value


def shot_request():
    shot_id = "11111111-1111-4111-8111-111111111111"
    return {
        "operation": "shot_high_detail",
        "request_id": "shot-test",
        "source_fingerprint": "a" * 64,
        "project": {
            "title": "", "logline": "", "continuity_summary": "",
            "color_script": "", "aspect_ratio": "9:16",
        },
        "style": "cinematic realism",
        "glossary": {},
        "target_shot": {
            "id": shot_id, "number": 1, "title": "", "narrative": "",
            "detailed_description": "", "camera": "slow push in",
            "action": "S1 watches the door", "dialogue": [],
            "sound_effects": "", "music": "", "continuity_notes": "",
            "duration_ms": 5000, "media_type": "video", "h3_mode": "T2VA",
            "include_audio": False, "scene_id": None,
            "character_ids": [], "prop_ids": [], "reference_bindings": {},
        },
        "boundaries": {"previous": None, "next": None},
        "bible": {"characters": [], "scenes": [], "props": []},
        "attempt": 1, "seed": 44, "max_new_tokens": 2304, "feedback": "",
    }


@override_settings(DEEPSEEK_API=CONFIG)
class DeepSeekCapabilityTests(SimpleTestCase):
    def test_capability_is_remote_and_never_contains_secret_or_endpoint(self):
        capability = get_deepseek_capability()

        self.assertEqual(capability, {
            "id": "deepseek_api", "provider": "deepseek_api", "enabled": True,
            "configured": True, "model": "deepseek-v4-flash",
            "transport": "remote", "data_egress": True,
        })
        serialized = json.dumps(capability)
        self.assertNotIn(CONFIG["api_key"], serialized)
        self.assertNotIn(CONFIG["base_url"], serialized)

    @override_settings(DEEPSEEK_API={**CONFIG, "enabled": False})
    @patch("studio.services.deepseek_api.requests.post")
    def test_disabled_provider_never_requests(self, post):
        with self.assertRaises(DeepSeekConfigurationError) as raised:
            run_translation({"text": "你好"})

        post.assert_not_called()
        self.assertEqual(raised.exception.code, "deepseek_api_disabled")

    @override_settings(
        DEEPSEEK_API={**CONFIG, "base_url": "https://user:pass@api.deepseek.com"},
    )
    @patch("studio.services.deepseek_api.requests.post")
    def test_url_credentials_are_rejected_without_request(self, post):
        self.assertFalse(get_deepseek_capability()["configured"])
        with self.assertRaises(DeepSeekConfigurationError):
            run_translation({"text": "你好"})
        post.assert_not_called()

    @override_settings(
        DEEPSEEK_API={**CONFIG, "base_url": "https://api.deepseek.com.evil.example"},
    )
    @patch("studio.services.deepseek_api.requests.post")
    def test_non_official_host_is_rejected_without_request(self, post):
        self.assertFalse(get_deepseek_capability()["configured"])
        with self.assertRaises(DeepSeekConfigurationError):
            run_translation({"text": "你好"})
        post.assert_not_called()


@override_settings(DEEPSEEK_API=CONFIG)
class DeepSeekTranslationTests(SimpleTestCase):
    @patch("studio.services.deepseek_api.requests.post")
    def test_success_uses_bearer_non_streaming_and_disables_thinking(self, post):
        post.return_value = response("A person waits by the door.")

        result = run_translation({
            "request_id": "one", "text": "一个人在门边等待。",
            "source_lang": "Chinese", "target_lang": "English",
            "max_new_tokens": 128,
        })

        self.assertTrue(result["ok"])
        self.assertEqual(result["translation"], "A person waits by the door.")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.deepseek.com/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-secret-key")
        self.assertFalse(kwargs["json"]["stream"])
        self.assertEqual(kwargs["json"]["thinking"], {"type": "disabled"})
        self.assertEqual(kwargs["json"]["max_tokens"], 128)
        self.assertNotIn("response_format", kwargs["json"])
        serialized = json.dumps(result)
        self.assertNotIn("test-secret-key", serialized)
        self.assertNotIn("api.deepseek.com", serialized)

    @patch("studio.services.deepseek_api.requests.post")
    def test_single_401_raises_safe_error_without_body_url_or_key(self, post):
        denied = response("secret response body", status=401)
        denied.text = "secret response body with test-secret-key"
        post.return_value = denied

        with self.assertRaises(DeepSeekAPIError) as raised:
            run_translation({"text": "你好"})

        self.assertEqual(raised.exception.code, "deepseek_api_authentication_failed")
        serialized = json.dumps(raised.exception.runtime_metadata)
        combined = f"{raised.exception} {serialized}"
        self.assertNotIn("test-secret-key", combined)
        self.assertNotIn("api.deepseek.com", combined)
        self.assertNotIn("secret response body", combined)

    @override_settings(DEEPSEEK_API={**CONFIG, "max_retries": 1})
    @patch("studio.services.deepseek_api.time.sleep")
    @patch("studio.services.deepseek_api.requests.post")
    def test_429_retries_once_then_is_a_safe_item_failure(self, post, sleep):
        post.side_effect = [response("", status=429), response("", status=429)]

        result = run_translation({"items": [{"request_id": "one", "text": "你好"}]})

        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(0.25)
        self.assertTrue(result["ok"])
        self.assertFalse(result["items"][0]["ok"])
        self.assertEqual(result["items"][0]["error_code"], "deepseek_api_rate_limited")

    @patch("studio.services.deepseek_api.requests.post")
    def test_empty_and_length_outputs_fail_closed(self, post):
        post.side_effect = [
            response("", finish_reason="stop"),
            response("partial", finish_reason="length"),
        ]

        empty = run_translation({"items": [{"request_id": "empty", "text": "你好"}]})
        length = run_translation({"items": [{"request_id": "long", "text": "你好"}]})

        self.assertEqual(empty["items"][0]["error_code"], "translation_empty_output")
        self.assertEqual(length["items"][0]["error_code"], "translation_output_budget_exhausted")

    @patch("studio.services.deepseek_api.requests.post")
    def test_protected_token_mismatch_isolated_and_batch_order_preserved(self, post):
        post.side_effect = [
            response("Translation without the protected token."),
            response("Second translation."),
        ]
        token = "__AUTOPROMPT_LITERAL_ABCDEF012345_0001__"

        result = run_translation({"items": [
            {"request_id": "first", "text": f"前文 {token}", "protected_tokens": [token]},
            {"request_id": "second", "text": "第二项"},
        ]})

        self.assertEqual([row["request_id"] for row in result["items"]], ["first", "second"])
        self.assertFalse(result["items"][0]["ok"])
        self.assertEqual(result["items"][0]["error_code"], "protected_token_mismatch")
        self.assertTrue(result["items"][1]["ok"])

    @patch("studio.services.deepseek_api.requests.post")
    def test_residual_cjk_gets_exactly_one_repair_pass(self, post):
        post.side_effect = [response("The 人 waits."), response("The person waits.")]

        result = run_translation({"text": "一个人在等待。", "max_new_tokens": 64})

        self.assertTrue(result["ok"])
        self.assertEqual(result["translation"], "The person waits.")
        self.assertTrue(result["metadata"]["repair_attempted"])
        self.assertEqual(post.call_count, 2)


@override_settings(DEEPSEEK_API=CONFIG)
class DeepSeekStructuredTests(SimpleTestCase):
    @patch("studio.services.deepseek_api.requests.post")
    def test_shot_result_is_validated_and_fingerprint_is_server_injected(self, post):
        post.return_value = response(json.dumps({
            "schema_version": 1,
            "detail_zh": "镜头缓慢推近，S1 注视门边阴影，最后保持静止。",
            "detail_en": "The camera slowly pushes in as S1 watches the shadow by the door and finally remains still.",
        }))

        result = run_structured_operation(shot_request())

        self.assertTrue(result["ok"])
        self.assertEqual(result["source_fingerprint"], "a" * 64)
        self.assertNotIn("source_fingerprint", result["result"])
        self.assertEqual(result["result"]["schema_version"], 1)
        request_body = post.call_args.kwargs["json"]
        self.assertEqual(request_body["response_format"], {"type": "json_object"})
        self.assertIn("JSON_SCHEMA", request_body["messages"][-1]["content"])
        self.assertNotIn("test-secret-key", json.dumps(result))

    @patch("studio.services.deepseek_api.requests.post")
    def test_structured_empty_or_length_output_is_rejected(self, post):
        post.side_effect = [
            response("", finish_reason="stop"),
            response("{}", finish_reason="length"),
        ]

        with self.assertRaises(DeepSeekAPIError) as empty:
            run_structured_operation(shot_request())
        with self.assertRaises(DeepSeekAPIError) as length:
            run_structured_operation(shot_request())

        self.assertEqual(empty.exception.code, "shot_high_detail_empty_output")
        self.assertEqual(length.exception.code, "shot_high_detail_output_budget_exhausted")

    @patch("studio.services.deepseek_api.requests.post")
    def test_structured_transport_never_exceeds_configured_max_tokens(self, post):
        post.return_value = response(json.dumps({
            "schema_version": 1, "detail_zh": "镜头推近。", "detail_en": "The camera pushes in.",
        }))

        with override_settings(DEEPSEEK_API={**CONFIG, "max_tokens": 2100}):
            run_structured_operation(shot_request())

        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 2100)

    @patch("studio.services.deepseek_api._protocol_module")
    def test_remote_creative_schema_uses_1000_without_widening_local_schema(
        self, protocol_module,
    ):
        local_schema = {
            "properties": {"shots": {"items": {"properties": {
                "narrative_en": {"type": "string", "maxLength": 280},
                "action_en": {"type": "string", "maxLength": 180},
            }}}},
        }
        protocol_module.return_value = SimpleNamespace(
            _creative_response_schema=lambda _request: local_schema,
        )
        from studio.services.deepseek_api import _deepseek_creative_response_schema

        remote_schema = _deepseek_creative_response_schema(
            protocol_module.return_value, object(),
        )

        remote_fields = remote_schema["properties"]["shots"]["items"]["properties"]
        local_fields = local_schema["properties"]["shots"]["items"]["properties"]
        self.assertEqual(remote_fields["narrative_en"]["maxLength"], 1000)
        self.assertEqual(remote_fields["action_en"]["maxLength"], 1000)
        self.assertEqual(local_fields["narrative_en"]["maxLength"], 280)
        self.assertEqual(local_fields["action_en"]["maxLength"], 180)

    @patch("studio.services.deepseek_api._protocol_module")
    def test_remote_creative_prompt_overrides_local_concision_contract(
        self, protocol_module,
    ):
        captured = {}

        def build(_request, *, character_limits=None):
            captured.update(character_limits or {})
            return [{"role": "user", "content": json.dumps(character_limits)}]

        protocol_module.return_value = SimpleNamespace(
            CREATIVE_MODEL_FIELD_CHARACTER_BUDGETS={
                "narrative_en": 280, "action_en": 180, "title_en": 72,
            },
            _build_creative_messages=build,
        )
        from studio.services.deepseek_api import _deepseek_creative_messages

        messages = _deepseek_creative_messages(protocol_module.return_value, object())

        self.assertEqual(captured["narrative_en"], 1000)
        self.assertEqual(captured["action_en"], 1000)
        self.assertEqual(captured["title_en"], 72)
        serialized = json.dumps(messages)
        self.assertNotIn('"narrative_en": 280', serialized)
        self.assertNotIn('"action_en": 180', serialized)

    def test_remote_creative_token_headroom_scales_by_shot_count(self):
        from studio.services.deepseek_api import _deepseek_creative_max_tokens

        self.assertEqual(
            _deepseek_creative_max_tokens(SimpleNamespace(max_tokens=1664, shot_count=2)),
            2432,
        )
        self.assertEqual(
            _deepseek_creative_max_tokens(SimpleNamespace(max_tokens=2304, shot_count=4)),
            3840,
        )

    @patch("studio.services.deepseek_api._protocol_module")
    @patch("studio.services.deepseek_api.requests.post")
    def test_creative_overlong_field_gets_one_bounded_field_only_repair(
        self, post, protocol_module,
    ):
        request = SimpleNamespace(
            request_id="creative-test", source_fingerprint="b" * 64,
            max_tokens=1664, shot_count=1,
        )
        decoded = {
            "schema_version": 1,
            "shots": [{"narrative_en": "x" * 1001, "locked_fact": "KEEP"}],
        }

        def validate(candidate, _request, *, review_issues):
            value = candidate["shots"][0]["narrative_en"]
            if len(value) > 1000:
                raise RuntimeError(
                    "result.shots[0].narrative_en exceeds 1000 characters"
                )
            self.assertEqual(candidate["shots"][0]["locked_fact"], "KEEP")
            return candidate

        protocol_module.return_value = SimpleNamespace(
            CREATIVE_OPERATION="creative_storyboard",
            SHOT_DETAIL_OPERATION="shot_high_detail",
            CREATIVE_MODEL_FIELD_CHARACTER_BUDGETS={"narrative_en": 280},
            CREATIVE_FINAL_REVIEW_FIELD_CHARACTER_BUDGETS={"narrative_en": 1000},
            _normalise_creative_request=lambda _payload: request,
            _build_creative_messages=lambda _request, **_kwargs: [
                {"role": "user", "content": "create"},
            ],
            _creative_response_schema=lambda _request: {
                "type": "object", "properties": {
                    "shots": {"items": {"properties": {
                        "narrative_en": {"type": "string", "maxLength": 280},
                        "action_en": {"type": "string", "maxLength": 180},
                    }}},
                },
            },
            _validate_creative_storyboard=validate,
            _clean_translation=lambda value: value,
        )
        post.side_effect = [
            response(json.dumps(decoded)),
            response(json.dumps({
                "targets": [{
                    "field_path": "result.shots[0].narrative_en",
                    "value": "A concise preserved action.",
                }],
            })),
        ]

        result = run_structured_operation({"operation": "creative_storyboard"})

        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            result["result"]["shots"][0]["narrative_en"],
            "A concise preserved action.",
        )
        self.assertEqual(result["result"]["shots"][0]["locked_fact"], "KEEP")
        self.assertTrue(result["metadata"]["repair_attempted"])
        self.assertTrue(result["metadata"]["repair_succeeded"])
        self.assertEqual(result["metadata"]["repair_reason_code"], "field_too_long")
        primary_instruction = post.call_args_list[0].kwargs["json"]["messages"][-1]["content"]
        self.assertIn("maxLength is a hard character limit", primary_instruction)
        repair_body = post.call_args_list[1].kwargs["json"]
        self.assertEqual(repair_body["temperature"], 0.0)
        self.assertEqual(repair_body["response_format"], {"type": "json_object"})
        repair_prompt = repair_body["messages"][-1]["content"]
        self.assertIn('"target_characters":500', repair_prompt)
        self.assertIn('"maximum_words":62', repair_prompt)
        self.assertIn('"maxLength":500', repair_prompt)

    @patch("studio.services.deepseek_api._protocol_module")
    @patch("studio.services.deepseek_api.requests.post")
    def test_creative_repairs_all_overlong_fields_in_one_api_call(
        self, post, protocol_module,
    ):
        request = SimpleNamespace(
            request_id="creative-test", source_fingerprint="c" * 64,
            max_tokens=1664, shot_count=2,
        )
        decoded = {
            "schema_version": 1,
            "shots": [
                {"narrative_en": "a" * 1001, "locked_fact": "FIRST"},
                {"narrative_en": "b" * 1002, "locked_fact": "SECOND"},
            ],
        }

        def validate(candidate, _request, *, review_issues):
            for index, shot in enumerate(candidate["shots"]):
                if len(shot["narrative_en"]) > 1000:
                    raise RuntimeError(
                        f"result.shots[{index}].narrative_en exceeds 1000 characters"
                    )
            self.assertEqual(
                [shot["locked_fact"] for shot in candidate["shots"]],
                ["FIRST", "SECOND"],
            )
            return candidate

        protocol_module.return_value = SimpleNamespace(
            CREATIVE_OPERATION="creative_storyboard",
            SHOT_DETAIL_OPERATION="shot_high_detail",
            CREATIVE_MODEL_FIELD_CHARACTER_BUDGETS={"narrative_en": 280},
            CREATIVE_FINAL_REVIEW_FIELD_CHARACTER_BUDGETS={"narrative_en": 1000},
            _normalise_creative_request=lambda _payload: request,
            _build_creative_messages=lambda _request, **_kwargs: [],
            _creative_response_schema=lambda _request: {
                "type": "object", "properties": {
                    "shots": {"items": {"properties": {
                        "narrative_en": {"type": "string", "maxLength": 280},
                        "action_en": {"type": "string", "maxLength": 180},
                    }}},
                },
            },
            _validate_creative_storyboard=validate,
            _clean_translation=lambda value: value,
        )
        post.side_effect = [
            response(json.dumps(decoded)),
            response(json.dumps({"targets": [
                {
                    "field_path": "result.shots[0].narrative_en",
                    "value": "First concise narrative.",
                },
                {
                    "field_path": "result.shots[1].narrative_en",
                    "value": "Second concise narrative.",
                },
            ]})),
        ]

        result = run_structured_operation({"operation": "creative_storyboard"})

        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            [shot["narrative_en"] for shot in result["result"]["shots"]],
            ["First concise narrative.", "Second concise narrative."],
        )
        repair_prompt = post.call_args_list[1].kwargs["json"]["messages"][-1]["content"]
        self.assertIn("result.shots[0].narrative_en", repair_prompt)
        self.assertIn("result.shots[1].narrative_en", repair_prompt)
        self.assertTrue(result["metadata"]["repair_succeeded"])

    @patch("studio.services.deepseek_api._protocol_module")
    @patch("studio.services.deepseek_api.requests.post")
    def test_creative_unrepairable_validation_exposes_only_safe_code_and_path(
        self, post, protocol_module,
    ):
        request = SimpleNamespace(
            request_id="creative-test", source_fingerprint="b" * 64,
            max_tokens=1664, shot_count=1,
        )
        secret_prose = "private candidate prose"

        def validate(_candidate, _request, *, review_issues):
            raise RuntimeError(
                "result.shots[1].scene_id must match " + secret_prose
            )

        protocol_module.return_value = SimpleNamespace(
            CREATIVE_OPERATION="creative_storyboard",
            SHOT_DETAIL_OPERATION="shot_high_detail",
            _normalise_creative_request=lambda _payload: request,
            CREATIVE_MODEL_FIELD_CHARACTER_BUDGETS={
                "narrative_en": 280, "action_en": 180,
            },
            _build_creative_messages=lambda _request, **_kwargs: [],
            _creative_response_schema=lambda _request: {
                "type": "object", "properties": {
                    "shots": {"items": {"properties": {
                        "narrative_en": {"type": "string", "maxLength": 280},
                        "action_en": {"type": "string", "maxLength": 180},
                    }}},
                },
            },
            _validate_creative_storyboard=validate,
            _clean_translation=lambda value: value,
        )
        post.return_value = response('{"shots": [{}, {}]}')

        with self.assertRaises(DeepSeekAPIError) as raised:
            run_structured_operation({"operation": "creative_storyboard"})

        self.assertEqual(post.call_count, 1)
        self.assertEqual(
            raised.exception.runtime_metadata["validation_error_code"],
            "structured_validation_failed",
        )
        self.assertEqual(
            raised.exception.runtime_metadata["validation_path"],
            "shots[1].scene_id",
        )
        serialized = json.dumps(raised.exception.runtime_metadata)
        self.assertNotIn(secret_prose, serialized)
        self.assertNotIn(CONFIG["api_key"], serialized)
        self.assertNotIn(CONFIG["base_url"], serialized)


@override_settings(DEEPSEEK_API=CONFIG)
class DeepSeekProviderRoutingTests(TestCase):
    def setUp(self):
        (
            self.user, self.project, _character, _scene, self.episode,
            self.shot, _next_shot,
        ) = build_story(username="deepseek-routing")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch("studio.views._ensure_translation_resources_available")
    @patch("studio.views.translate_texts_once")
    def test_remote_translation_skips_local_resource_gate_and_persists_provider(
        self, translate, local_gate,
    ):
        translate.return_value = [TranslationResult(
            text="A slow push-in ends on the letter.",
            provider="deepseek_api", model="deepseek-v4-flash",
            metadata={"transport": "remote_api"},
        )]

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/translations/translate/",
            {
                "provider": "deepseek_api",
                "entries": [{
                    "scope_type": "shot", "scope_id": str(self.shot.id),
                    "field_key": "shot.camera", "source_text": self.shot.camera,
                }],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        local_gate.assert_not_called()
        self.assertEqual(translate.call_args.kwargs["provider"], "deepseek_api")
        self.assertEqual(response.data["entries"][0]["provider"], "deepseek_api")

    @patch("studio.services.creative_drafts.ensure_creative_resources_available")
    @patch("studio.services.creative_drafts._require_runtime")
    @patch("studio.services.creative_drafts.run_deepseek_structured_operation")
    def test_remote_creative_skips_local_qwen_checks_even_with_active_generation(
        self, remote, local_runtime, local_resources,
    ):
        candidate = {
            "schema_version": 1,
            "shots": [{
                "client_key": "shot-001", "order": 1,
                "title_zh": "证物", "title_en": "Evidence",
                "narrative_zh": "林默站在旧档案室内。", "narrative_en": "Lin Mo stands in the old archive.",
                "camera_zh": "中景平视，镜头缓慢推近。", "camera_en": "Eye-level medium shot with a slow push-in.",
                "action_zh": "林默展开信纸后静止。", "action_en": "Lin Mo unfolds the letter and then holds still.",
                "continuity_zh": "炭灰风衣保持不变。", "continuity_en": "The charcoal-gray coat remains unchanged.",
                "soundscape_zh": "", "soundscape_en": "", "music_zh": "", "music_en": "",
                "duration_ms": 5000, "media_type": "video", "h3_mode": "T2VA",
                "include_audio": False, "scene_id": None, "character_ids": [], "prop_ids": [],
                "dialogue": [],
            }],
        }

        def remote_response(request):
            return {
                "ok": True, "result": {**candidate, "source_fingerprint": request["context_hash"]},
                "model": "deepseek-v4-flash",
                "metadata": {"provider": "deepseek_api", "transport": "remote_api"},
            }

        remote.side_effect = remote_response
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
            {
                "episode_id": str(self.episode.id), "provider": "deepseek_api",
                "target_shot_count": 1, "creativity": "balanced", "variation_note": "",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        local_resources.assert_not_called()
        local_runtime.assert_not_called()
        self.assertEqual(response.data["provider"], "deepseek_api")
        self.assertEqual(response.data["model"], "deepseek-v4-flash")
        self.assertEqual(EpisodeCreativeDraft.objects.get(pk=response.data["id"]).provider, "deepseek_api")
