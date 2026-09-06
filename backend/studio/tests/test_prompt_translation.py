from unittest.mock import patch
import json
import uuid

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from studio.models import ConfigRevision, PromptFieldTranslation, PropBible, Shot
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.tests.media_prompt_fixtures import (
    PromptBuildError,
    MediaPromptOptions,
    build_test_video_prompt,
    build_test_negative_prompt,
)
from studio.services.prompt_generation_sources import (
    MODEL_GENERATION_MODE_VALUES,
    PROMPT_GENERATION_SOURCE_KEYS_BY_SCOPE,
)
from studio.services.prompt_normalization_contract import MODEL_PROCESSING_MODES
from studio.services.prompt_translation import (
    ALLOWED_FIELD_KEYS,
    build_translation_context,
    confirmed_translation,
    PromptTranslationError,
    TRANSLATION_CONTEXT_VERSION,
    TranslationResult,
    TranslationValidationError,
    deterministic_prompt_normalization_materialization,
    source_text_hash,
    translate_text_once,
    translate_texts_once,
    translation_context_hash,
    validate_manual_translation,
    _safe_runtime_error,
)
from studio.tests.factories import (
    build_story,
    create_ready_prompt_normalization_test_deployment,
)


class PromptGenerationSourceRegistryTests(SimpleTestCase):
    def test_runtime_allowlist_is_derived_from_generation_source_registry(self):
        self.assertEqual(
            {scope: frozenset(values) for scope, values in ALLOWED_FIELD_KEYS.items()},
            PROMPT_GENERATION_SOURCE_KEYS_BY_SCOPE,
        )
        self.assertEqual(sum(map(len, ALLOWED_FIELD_KEYS.values())), 35)
        self.assertIs(MODEL_PROCESSING_MODES, MODEL_GENERATION_MODE_VALUES)


class PromptTranslationProtectionTests(SimpleTestCase):
    # Content safety ceilings are database-owned security boundaries.
    databases = {"default"}

    def test_protected_translation_materializes_locally_with_signed_proof(self):
        token = "__AUTOPROMPT_LITERAL_52D702248A19_0001__"
        item = {
            "request_id": "001-name",
            "text": token,
            "protected_tokens": [token],
        }
        target = {
            "request_id": "001-name",
            "source_text": "周岚",
            "model_input_sha256": source_text_hash(token),
            "protected_blocks": {token: "S1"},
        }

        result = deterministic_prompt_normalization_materialization(
            item=item,
            target=target,
            request_payload={"schema_version": 1, "items": [item]},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["translated_text"], token)
        self.assertEqual(result["restored_candidate"], "S1")
        self.assertEqual(result["proof"]["execution_kind"], "server_deterministic")
        self.assertEqual(result["proof"]["materialization_kind"], "protected_tokens_only")
        self.assertRegex(result["proof_sha256"], r"^[0-9a-f]{64}$")

    def test_target_language_text_with_canonical_token_materializes_locally(self):
        token = "__AUTOPROMPT_LITERAL_52D702248A19_0001__"
        item = {
            "request_id": "001-name",
            "text": f"Subject {token}",
            "protected_tokens": [token],
        }
        target = {
            "request_id": "001-name",
            "source_text": "Subject 周岚",
            "model_input_sha256": source_text_hash(item["text"]),
            "protected_blocks": {token: "S1"},
        }

        materialization = deterministic_prompt_normalization_materialization(
            item=item,
            target=target,
            request_payload={"schema_version": 1, "items": [item]},
        )

        self.assertIsNotNone(materialization)
        self.assertEqual(materialization["translated_text"], item["text"])
        self.assertEqual(materialization["restored_candidate"], "Subject S1")
        self.assertEqual(
            materialization["proof"]["materialization_kind"],
            "source_already_satisfies_target_contract",
        )
        duplicate = {**item, "text": f"{token} {token}"}
        duplicate_target = {
            **target,
            "model_input_sha256": source_text_hash(duplicate["text"]),
        }
        with self.assertRaises(TranslationValidationError):
            deterministic_prompt_normalization_materialization(
                item=duplicate,
                target=duplicate_target,
                request_payload={"schema_version": 1, "items": [duplicate]},
            )

    def test_target_language_text_without_token_materializes_locally(self):
        item = {
            "request_id": "001-summary",
            "text": "Maintain the dark coat and stable cyan lighting.",
            "protected_tokens": [],
        }
        target = {
            "request_id": "001-summary",
            "source_text": item["text"],
            "model_input_sha256": source_text_hash(item["text"]),
            "protected_blocks": {},
        }

        materialization = deterministic_prompt_normalization_materialization(
            item=item,
            target=target,
            request_payload={"schema_version": 1, "items": [item]},
        )

        self.assertIsNotNone(materialization)
        self.assertEqual(materialization["restored_candidate"], item["text"])
        self.assertEqual(materialization["warnings"], [])

    def test_non_target_language_or_control_character_stays_on_provider_route(self):
        for source in ("保持深色外套。", "Maintain\u200b the dark coat."):
            with self.subTest(source=source):
                item = {
                    "request_id": "001-summary",
                    "text": source,
                    "protected_tokens": [],
                }
                target = {
                    "request_id": "001-summary",
                    "source_text": source,
                    "model_input_sha256": source_text_hash(source),
                    "protected_blocks": {},
                }
                self.assertIsNone(
                    deterministic_prompt_normalization_materialization(
                        item=item,
                        target=target,
                        request_payload={"schema_version": 1, "items": [item]},
                    ),
                )

    def test_manual_translation_must_preserve_authored_blocks_verbatim(self):
        source = "她说 <d>[Chinese] 别动。</d>，门上写着 <screen_text>客栈</screen_text>。"
        valid = (
            "She speaks <d>[Chinese] 别动。</d>, while the door displays "
            "<screen_text>客栈</screen_text>."
        )
        self.assertEqual(validate_manual_translation(source, valid), valid)
        with self.assertRaises(TranslationValidationError):
            validate_manual_translation(source, "She speaks <d>[English] Stop.</d>.")
        with self.assertRaises(TranslationValidationError):
            validate_manual_translation(source, valid + " 中文")
        with self.assertRaises(TranslationValidationError):
            validate_manual_translation("缓慢推近。", "カメラがゆっくり前進します。")
        with self.assertRaises(TranslationValidationError):
            validate_manual_translation("缓慢推近。", "---")
        for deceptive in (
            "Slow push \u202e hidden.",
            "Slow\u200b push.",
            "Slow push.\u0007",
        ):
            with self.subTest(deceptive=deceptive), self.assertRaises(
                TranslationValidationError,
            ):
                validate_manual_translation("缓慢推近。", deceptive)

    @patch(
        "studio.services.prompt_translation._frozen_translation_batch_size",
        return_value=100,
    )
    @patch("studio.services.prompt_normalization.invoke_prompt_normalization")
    def test_batch_loads_runtime_once_and_restores_protected_blocks(
        self, invoke, batch_size,
    ):
        def response(*, deployment_id, payload):
            request = payload
            self.assertEqual(len(request["items"]), 2)
            protected = request["items"][1]["text"]
            token = next(part for part in protected.split() if part.startswith("__AUTOPROMPT_"))
            self.assertEqual(request["items"][1]["protected_tokens"], [token])
            return {
                "schema_version": 1,
                "items": [
                    {
                        "request_id": request["items"][0]["request_id"],
                        "ok": True, "translation": "A slow push-in ends on the blade.",
                    },
                    {
                        "request_id": request["items"][1]["request_id"],
                        "ok": True, "translation": f"The subject speaks {token}.",
                    },
                ],
                "metadata": {
                    "provider": "third-local",
                    "model": "third-local-model",
                    "model_deployment_id": str(deployment_id),
                },
            }

        invoke.side_effect = response
        results = translate_texts_once(
            ["缓慢推近，停在剑锋。", "她说 <d>[Chinese] 别动。</d>"],
            field_keys=["shot.camera", "shot.action"],
            labels=["镜头", "动作"], contexts=["{}", "{}"],
            model_deployment_id=uuid.uuid4(),
        )
        batch_size.assert_called_once()
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(results[0].text, "A slow push-in ends on the blade.")
        self.assertIn("<d>[Chinese] 别动。</d>", results[1].text)

    @patch("studio.services.prompt_normalization.invoke_prompt_normalization")
    def test_single_translation_deterministically_preserves_canonical_target(
        self, invoke,
    ):
        context = json.dumps({
            "canonical_identity_mappings": {
                "断刃": "prop:broken-blade",
            },
        }, ensure_ascii=False)

        def response(*, deployment_id, payload):
            item = payload["items"][0]
            self.assertNotIn("断刃", item["text"])
            token = item["protected_tokens"][0]
            self.assertEqual(item["text"].count(token), 1)
            return {
                "schema_version": 1,
                "items": [{
                    "request_id": item["request_id"],
                    "ok": True,
                    "translation": f"The left hand always holds {token}.",
                }],
                "metadata": {
                    "provider": "third-local",
                    "model": "third-local-model",
                    "model_deployment_id": str(deployment_id),
                },
            }

        invoke.side_effect = response
        result = translate_text_once(
            "左手始终持有断刃。",
            field_key="character.visual_identity",
            label="人物视觉锚点",
            context=context,
            model_deployment_id=uuid.uuid4(),
        )

        self.assertEqual(
            result.text,
            "The left hand always holds prop:broken-blade.",
        )


class PromptTranslationApiTests(TestCase):
    def setUp(self):
        (
            self.user, self.project, self.character, _, self.episode,
            self.shot, _,
        ) = build_story()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.deployment_id = create_ready_prompt_normalization_test_deployment().id
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=self.project,
            values={
                "production.prompt_normalization_deployment_id": str(self.deployment_id),
            },
            created_by=self.user,
        )
        publish_config_revision(revision, published_by=self.user)
        self.shot.action = "她拔剑挡在门前，雨水沿剑锋滴落。"
        self.shot.camera = "低机位缓慢推近，停在剑锋特写。"
        self.shot.save(update_fields=("action", "camera", "updated_at"))

    def _context_metadata(self, scope_type, scope_id, field_key):
        return {
            "translation_context_hash": translation_context_hash(
                build_translation_context(
                    self.project,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    field_key=field_key,
                )
            ),
            "translation_context_version": TRANSLATION_CONTEXT_VERSION,
        }

    def _translation_entry(self, field_key, source, translated, *, status=None, metadata=None):
        return PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type=PromptFieldTranslation.ScopeType.SHOT,
            scope_id=self.shot.id,
            field_key=field_key,
            source_text=source,
            source_hash=source_text_hash(source),
            translated_text=translated,
            status=status or PromptFieldTranslation.Status.DRAFT_TRANSLATION,
            runtime_metadata=metadata or self._context_metadata(
                PromptFieldTranslation.ScopeType.SHOT, self.shot.id, field_key,
            ),
        )

    def test_translation_list_rejects_non_uuid_scope_filters_as_bad_request(self):
        url = reverse("project-translations", args=[self.project.id])

        for field in ("episode", "shot"):
            with self.subTest(field=field):
                response = self.client.get(url, {field: "shot-1787632938052-1"})

                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["error"]["code"], "invalid")
                self.assertIn(field, response.data["error"]["details"])

    @patch("studio.views.translate_texts_once")
    def test_manual_negative_prompt_candidate_uses_no_model_and_unblocks_overlay(
        self, translate,
    ):
        source = "人物身份漂移；多余肢体；文字水印；低清晰度"
        translated = "identity drift; extra limbs; text watermark; low resolution"
        self.shot.metadata = {
            **(self.shot.metadata or {}),
            "prompt_field_version": 2,
            "negative_prompt": source,
        }
        self.shot.save(update_fields=("metadata", "updated_at"))

        created = self.client.post(
            reverse("project-create-manual-prompt-fields", args=[self.project.id]),
            {"entries": [{
                "scope_type": "shot",
                "scope_id": str(self.shot.id),
                "field_key": "shot.negative_prompt",
                "source_text": source,
            }]},
            format="json",
        )

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["summary"], {
            "requested": 1, "created": 1, "external_calls": 0,
        })
        translate.assert_not_called()
        entry = PromptFieldTranslation.objects.get(
            project=self.project,
            scope_type=PromptFieldTranslation.ScopeType.SHOT,
            scope_id=self.shot.id,
            field_key="shot.negative_prompt",
        )
        self.assertEqual(entry.translated_text, "")
        self.assertEqual(entry.provider, "manual_review")
        self.assertTrue(entry.runtime_metadata["manual_candidate"])

        confirmed = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision, "translation": translated},
            format="json",
        )

        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        entry.refresh_from_db()
        self.assertEqual(entry.status, PromptFieldTranslation.Status.CONFIRMED)
        self.assertTrue(confirmed.data["context_current"])
        self.assertEqual(confirmed.data["promoted_fields"], [])
        negative_prompt = build_test_negative_prompt(self.project, [self.shot])
        self.assertIn(translated, negative_prompt)
        self.assertNotIn("人物身份漂移", negative_prompt)

    def test_confirm_promotes_canonical_narrative_and_transport_retry_is_idempotent(self):
        original = "她推开院门走入雨中。"
        translated = "She opens the courtyard door and steps into the rain."
        original_title = self.shot.title
        original_dialogue = self.shot.dialogue
        self.shot.narrative = original
        self.shot.save(update_fields=("narrative", "updated_at"))
        entry = self._translation_entry("shot.narrative", original, translated)

        confirmed = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )

        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["promoted_fields"], [{
            "field_key": "shot.narrative", "value": translated,
            "dialogue_index": None,
        }])
        self.shot.refresh_from_db()
        entry.refresh_from_db()
        self.assertEqual(self.shot.narrative, translated)
        self.assertEqual(self.shot.title, original_title)
        self.assertEqual(self.shot.dialogue, original_dialogue)
        self.assertEqual(entry.source_text, translated)
        self.assertEqual(entry.source_hash, source_text_hash(translated))
        self.assertEqual(entry.revision_history[-1]["source_text"], original)
        self.assertEqual(
            entry.revision_history[-1]["event"], "translation_promoted",
        )
        self.assertEqual(
            entry.revision_history[-1]["confirmed_by"], str(self.user.id),
        )
        self.assertTrue(entry.runtime_metadata["promotion"]["promoted"])
        promotion = entry.runtime_metadata["promotion"]
        self.assertEqual(promotion["event"], "translation_promoted")
        self.assertEqual(promotion["promoted_by"], str(self.user.id))
        self.assertEqual(
            promotion["original_source_text_sha256"], source_text_hash(original),
        )
        self.assertEqual(
            promotion["promoted_value_sha256"], source_text_hash(translated),
        )

        retry = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": 1, "translation": translated}, format="json",
        )
        self.assertEqual(retry.status_code, 200, retry.data)
        entry.refresh_from_db()
        self.assertEqual(entry.revision, 2)
        self.assertEqual(len(entry.revision_history), 1)

        revoked = self.client.post(
            reverse("translation-revoke", args=[entry.id]), {}, format="json",
        )
        self.assertEqual(revoked.status_code, 200, revoked.data)
        self.shot.refresh_from_db()
        entry.refresh_from_db()
        self.assertEqual(self.shot.narrative, translated)
        self.assertEqual(entry.source_text, translated)
        self.assertTrue(revoked.data["context_current"])

    def test_promote_shot_is_atomic_and_requires_expected_revisions(self):
        narrative_source = "她走入雨中。"
        camera_source = "低机位缓慢推近。"
        narrative_en = "She steps into the rain."
        camera_en = "A low-angle camera pushes in slowly."
        self.shot.narrative = narrative_source
        self.shot.camera = camera_source
        self.shot.save(update_fields=("narrative", "camera", "updated_at"))
        narrative = self._translation_entry(
            "shot.narrative", narrative_source, narrative_en,
            status=PromptFieldTranslation.Status.CONFIRMED,
        )
        camera = self._translation_entry(
            "shot.camera", camera_source, camera_en,
            status=PromptFieldTranslation.Status.CONFIRMED,
        )
        url = reverse("translation-promote-shot")
        empty = self.client.post(url, {
            "shot_id": str(self.shot.id),
            "entries": [],
        }, format="json")
        self.assertEqual(empty.status_code, 400, empty.data)

        conflict = self.client.post(url, {
            "shot_id": str(self.shot.id),
            "entries": [
                {"entry_id": str(narrative.id), "expected_revision": 1},
                {"entry_id": str(camera.id), "expected_revision": 99},
            ],
        }, format="json")
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.shot.refresh_from_db()
        narrative.refresh_from_db()
        self.assertEqual(self.shot.narrative, narrative_source)
        self.assertEqual(self.shot.camera, camera_source)
        self.assertEqual(narrative.revision, 1)

        promoted = self.client.post(url, {
            "shot_id": str(self.shot.id),
            "entries": [
                {"entry_id": str(narrative.id), "expected_revision": 1},
                {"entry_id": str(camera.id), "expected_revision": 1},
            ],
        }, format="json")
        self.assertEqual(promoted.status_code, 200, promoted.data)
        self.assertEqual(
            [item["field_key"] for item in promoted.data["promoted_fields"]],
            ["shot.narrative", "shot.camera"],
        )
        self.assertEqual(
            set(promoted.data),
            {"shot_id", "promoted_fields", "entries"},
        )
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.narrative, narrative_en)
        self.assertEqual(self.shot.camera, camera_en)

    def test_dialogue_promotion_rejects_reordered_occurrence_identity(self):
        delivery = "低声而警觉"
        self.shot.dialogue = [
            {"speaker_id": "S1", "text": "谁在那里？", "delivery": delivery},
            {"speaker_id": "S1", "text": "别动。", "delivery": delivery},
        ]
        self.shot.save(update_fields=("dialogue", "updated_at"))
        identity = {
            "source_order": 0,
            "speaker_id": "S1",
            "text_sha256": source_text_hash("谁在那里？"),
            "voiceover": False,
        }
        entry = self._translation_entry(
            "shot.dialogue.0.delivery", delivery, "in a low, alert voice",
            metadata={
                **self._context_metadata(
                    PromptFieldTranslation.ScopeType.SHOT,
                    self.shot.id, "shot.dialogue.0.delivery",
                ),
                "dialogue_identity": identity,
            },
        )
        self.shot.dialogue = list(reversed(self.shot.dialogue))
        self.shot.save(update_fields=("dialogue", "updated_at"))

        response = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["details"]["code"],
            "dialogue_identity_changed",
        )

    @patch("studio.views.translate_texts_once")
    def test_project_character_scene_and_prop_fields_keep_distinct_scopes(self, translate):
        scene = self.shot.scene
        prop = PropBible.objects.create(
            project=self.project, slug="sealed-letter", name="密封信",
            visual_identity="暗红蜡封的旧信纸",
        )
        self.project.continuity_summary = "全剧保持冷青雨夜和克制的琥珀灯光。"
        self.project.save(update_fields=("continuity_summary", "updated_at"))
        sources = [
            self.project.continuity_summary,
            self.character.visual_identity,
            scene.visual_identity,
            prop.visual_identity,
        ]
        translate.return_value = [TranslationResult(
            text=value, provider="local_qwen", model="Qwen/Qwen3-4B-GGUF",
            metadata={"elapsed_ms": 1},
        ) for value in (
            "Keep a cool rain-night palette with restrained amber practical light.",
            "A thirty-year-old short-haired detective with a mole below the left eye.",
            "A narrow elevated archive room with drifting dust.",
            "An old letter sealed with dark red wax.",
        )]

        response = self.client.post(
            reverse("project-translate-prompt-fields", args=[self.project.id]),
            {"model_deployment_id": str(self.deployment_id), "entries": [
                {"scope_type": "project", "scope_id": str(self.project.id),
                 "field_key": "project.continuity_summary", "source_text": sources[0]},
                {"scope_type": "character", "scope_id": str(self.character.id),
                 "field_key": "character.visual_identity", "source_text": sources[1]},
                {"scope_type": "scene", "scope_id": str(scene.id),
                 "field_key": "scene.visual_identity", "source_text": sources[2]},
                {"scope_type": "prop", "scope_id": str(prop.id),
                 "field_key": "prop.visual_identity", "source_text": sources[3]},
            ]}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["summary"], {"requested": 4, "succeeded": 4, "failed": 0})
        self.assertEqual(
            {(row["scope_type"], row["field_key"]) for row in response.data["entries"]},
            {
                ("project", "project.continuity_summary"),
                ("character", "character.visual_identity"),
                ("scene", "scene.visual_identity"),
                ("prop", "prop.visual_identity"),
            },
        )

    @patch("studio.views.translate_texts_once")
    def test_batch_partial_failure_persists_safe_error_code_and_keeps_sibling(self, translate):
        scene = self.shot.scene
        translate.return_value = [
            TranslationResult(
                text="A thirty-year-old short-haired detective.",
                provider="local_qwen", model="Qwen/Qwen3-4B-GGUF",
                metadata={"elapsed_ms": 10},
            ),
            PromptTranslationError(
                "英文候选经一次确定性修复后仍含中文字符，已拒绝采用；请重试或人工填写英文译文。",
                code="residual_cjk_after_repair",
                metadata={
                    "error_code": "residual_cjk_after_repair",
                    "repair_attempted": True,
                    "residual_cjk": ["旧档案室"],
                },
            ),
        ]
        response = self.client.post(
            reverse("project-translate-prompt-fields", args=[self.project.id]),
            {"model_deployment_id": str(self.deployment_id), "entries": [
                {"scope_type": "character", "scope_id": str(self.character.id),
                 "field_key": "character.visual_identity",
                 "source_text": self.character.visual_identity},
                {"scope_type": "scene", "scope_id": str(scene.id),
                 "field_key": "scene.visual_identity", "source_text": scene.visual_identity},
            ]}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["summary"], {"requested": 2, "succeeded": 1, "failed": 1})
        successful, failed = response.data["entries"]
        self.assertEqual(successful["status"], PromptFieldTranslation.Status.DRAFT_TRANSLATION)
        self.assertEqual(failed["status"], PromptFieldTranslation.Status.FAILED)
        self.assertEqual(failed["runtime_metadata"]["error_code"], "residual_cjk_after_repair")
        self.assertEqual(failed["runtime_metadata"]["residual_cjk"], ["旧档案室"])
        self.assertNotIn("E:\\", failed["error_message"])

    @patch("studio.views.translate_texts_once")
    def test_batch_candidate_requires_confirmation_before_compiler_uses_it(self, translate):
        translated = "She draws her sword and blocks the doorway as rain runs down the blade."
        translate.return_value = [TranslationResult(
            text=translated,
            provider="local_qwen",
            model="Qwen/Qwen3-4B-GGUF",
            metadata={"elapsed_ms": 12, "unloaded_after_request": True},
        )]
        response = self.client.post(
            reverse("project-translate-prompt-fields", args=[self.project.id]),
            {
                "model_deployment_id": str(self.deployment_id),
                "entries": [{
                    "scope_type": "shot",
                    "scope_id": str(self.shot.id),
                    "field_key": "shot.action",
                    "source_text": self.shot.action,
                }],
                "source_lang": "Chinese",
                "target_lang": "English",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        entry = PromptFieldTranslation.objects.get(pk=response.data["entries"][0]["id"])
        self.assertEqual(entry.status, PromptFieldTranslation.Status.DRAFT_TRANSLATION)

        preview = build_test_video_prompt(
            self.project, [self.shot],
            MediaPromptOptions(
                model_identifier="minimax-h3-test", mode="T2VA", language_gate_mode="warn",
            ),
        )
        self.assertIn(self.shot.action.rstrip("。"), preview.text)
        self.assertNotIn(translated, preview.text)

        confirmed = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        compiled = build_test_video_prompt(
            self.project, [self.shot],
            MediaPromptOptions(
                model_identifier="minimax-h3-test", mode="T2VA", language_gate_mode="warn",
            ),
        )
        self.assertIn(translated, compiled.text)
        self.assertNotIn(self.shot.action.rstrip("。"), compiled.text)
        self.assertEqual(confirmed.data["provider"], "local_qwen")

    @patch("studio.views.translate_texts_once")
    def test_batch_rejects_non_chinese_to_english_contract(self, translate):
        response = self.client.post(
            reverse("project-translate-prompt-fields", args=[self.project.id]),
            {
                "model_deployment_id": str(self.deployment_id),
                "entries": [{
                    "scope_type": "shot", "scope_id": str(self.shot.id),
                    "field_key": "shot.action", "source_text": self.shot.action,
                }],
                "source_lang": "Japanese", "target_lang": "English",
            }, format="json",
        )
        self.assertEqual(response.status_code, 400)
        translate.assert_not_called()

    @patch("studio.views.translate_text_once")
    def test_failed_retranslation_preserves_confirmed_candidate_and_revision(self, translate):
        entry = PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type="shot",
            scope_id=self.shot.id,
            field_key="shot.action",
            source_text=self.shot.action,
            source_hash=source_text_hash(self.shot.action),
            translated_text="She draws her sword in front of the doorway.",
            status=PromptFieldTranslation.Status.CONFIRMED,
            revision=3,
            provider="local_qwen",
            model="Qwen/Qwen3-4B-GGUF",
            confirmed_by=self.user,
            runtime_metadata=self._context_metadata("shot", self.shot.id, "shot.action"),
        )
        translate.side_effect = PromptTranslationError("runtime stopped")
        response = self.client.post(
            reverse("translation-retranslate", args=[entry.id]),
            {"model_deployment_id": str(self.deployment_id)}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        entry.refresh_from_db()
        self.assertEqual(entry.status, PromptFieldTranslation.Status.CONFIRMED)
        self.assertEqual(entry.revision, 3)
        self.assertEqual(entry.translated_text, "She draws her sword in front of the doorway.")
        self.assertEqual(entry.runtime_metadata["last_attempt_error"], "runtime stopped")

    @patch("studio.views.translate_text_once")
    def test_successful_retranslation_returns_current_canonical_candidate(self, translate):
        source = "她左手始终握住断刃。"
        canonical_target = "Broken Blade"
        self.shot.narrative = source
        self.shot.save(update_fields=("narrative", "updated_at"))
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {"断刃": canonical_target},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        entry = self._translation_entry(
            "shot.narrative", source, "She keeps hold of a broken blade.",
            status=PromptFieldTranslation.Status.CONFIRMED,
        )
        entry.runtime_metadata = {
            **(entry.runtime_metadata or {}),
            "model_deployment_id": "previous-deployment",
            "provider": "previous-provider",
        }
        entry.save(update_fields=("runtime_metadata", "updated_at"))
        translate.return_value = TranslationResult(
            text=f"She keeps her left hand on the {canonical_target}.",
            provider="local_qwen",
            model="Qwen/Qwen3-4B-GGUF",
            metadata={"elapsed_ms": 20},
        )

        response = self.client.post(
            reverse("translation-retranslate", args=[entry.id]),
            {"model_deployment_id": str(self.deployment_id)}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], PromptFieldTranslation.Status.DRAFT_TRANSLATION)
        self.assertEqual(response.data["revision"], entry.revision + 1)
        self.assertTrue(response.data["context_current"])
        self.assertIn(canonical_target, response.data["translated_text"])
        self.assertEqual(response.data["error_message"], "")
        entry.refresh_from_db()
        self.assertEqual(entry.translated_text, response.data["translated_text"])
        self.assertEqual(
            entry.revision_history[-1]["runtime_metadata"]["model_deployment_id"],
            "previous-deployment",
        )
        self.assertEqual(
            entry.revision_history[-1]["runtime_metadata"]["provider"],
            "previous-provider",
        )

    def test_confirmation_rejects_stale_source(self):
        entry = PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type="shot",
            scope_id=self.shot.id,
            field_key="shot.camera",
            source_text=self.shot.camera,
            source_hash=source_text_hash(self.shot.camera),
            translated_text="A low-angle slow push ends on the sword tip.",
            status=PromptFieldTranslation.Status.DRAFT_TRANSLATION,
        )
        self.shot.camera = "固定中景。"
        self.shot.save(update_fields=("camera", "updated_at"))
        response = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )
        self.assertEqual(response.status_code, 409)
        entry.refresh_from_db()
        self.assertNotEqual(entry.status, PromptFieldTranslation.Status.CONFIRMED)

    def test_dialogue_delivery_overlay_does_not_translate_dialogue_text(self):
        self.shot.dialogue = [{
            "speaker_id": self.character.stable_speaker_id,
            "text": "他们追上来了。",
            "delivery": "压低声音，急促地",
        }]
        self.shot.save(update_fields=("dialogue", "updated_at"))
        PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type="shot",
            scope_id=self.shot.id,
            field_key="shot.dialogue.0.delivery",
            source_text="压低声音，急促地",
            source_hash=source_text_hash("压低声音，急促地"),
            translated_text="in a hushed, urgent voice",
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata=self._context_metadata(
                "shot", self.shot.id, "shot.dialogue.0.delivery",
            ),
        )
        compiled = build_test_video_prompt(
            self.project, [self.shot],
            MediaPromptOptions(
                model_identifier="minimax-h3-test", mode="T2VA", language_gate_mode="warn",
            ),
        )
        self.assertIn("in a hushed, urgent voice", compiled.text)
        self.assertIn("<d>[Chinese] 他们追上来了。</d>", compiled.text)
        self.assertNotIn("They have caught up", compiled.text)

    def test_confirmed_overlays_unblock_final_english_prompt_and_source_edit_blocks_again(self):
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "color_script": "Cool rain shadows with restrained amber practical lights.",
            "translation_glossary": {"林默": "Lin Mo"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        self.character.age_appearance = "A woman in her early thirties."
        self.character.visual_identity = "Short dark hair and a small mole below the left eye."
        self.character.face_identity = "High cheekbones and dark brown eyes."
        self.character.wardrobe = "A charcoal raincoat over a pale linen shirt."
        self.character.voice_profile = "A controlled low voice."
        self.character.save()
        scene = self.shot.scene
        scene.visual_identity = "A narrow archive room beneath an elevated railway."
        scene.location = "Old riverside district."
        scene.time_of_day = "Night."
        scene.lighting = "One green desk lamp and distant lightning."
        scene.color_palette = "Slate blue, charcoal, and dim amber."
        scene.ambient_sound = "Rain strikes the window while an old clock ticks."
        scene.save()
        for prop in self.shot.props.all():
            prop.visual_identity = "A scorched letter with a wax seal."
            prop.material = "Charred paper and dark red wax."
            prop.distinctive_marks = "One broken circular seal."
            prop.continuity_state = "Held open in the right hand."
            prop.save()
        self.shot.narrative = "侦探把烧焦的信举到台灯旁。"
        self.shot.action = "她突然拔剑挡住门口。"
        self.shot.camera = "低机位缓慢推近，最终停在剑锋特写。"
        self.shot.sound_effects = "纸张轻响，窗外雨声持续。"
        self.shot.music = "稀疏低音弦乐在切镜前淡出。"
        self.shot.dialogue = [{
            "speaker_id": self.character.stable_speaker_id,
            "text": "别动。",
            "delivery": "克制而低沉地耳语",
        }]
        self.shot.save()
        for key, source, translated in (
            (
                "shot.narrative", self.shot.narrative,
                "The detective holds the scorched letter near the desk lamp.",
            ),
            (
                "shot.action", self.shot.action,
                "She suddenly draws her sword and blocks the doorway.",
            ),
            (
                "shot.camera", self.shot.camera,
                "A low-angle slow push ends in a close-up of the sword tip.",
            ),
            (
                "shot.sound_effects", self.shot.sound_effects,
                "Paper rustles once as rain continues outside.",
            ),
            (
                "shot.music", self.shot.music,
                "Sparse low strings fade before the cut.",
            ),
            (
                "shot.dialogue.0.delivery", self.shot.dialogue[0]["delivery"],
                "in a controlled whisper",
            ),
        ):
            PromptFieldTranslation.objects.create(
                project=self.project, scope_type="shot", scope_id=self.shot.id,
                field_key=key, source_text=source, source_hash=source_text_hash(source),
                translated_text=translated, status=PromptFieldTranslation.Status.CONFIRMED,
                runtime_metadata=self._context_metadata("shot", self.shot.id, key),
            )

        compiled = build_test_video_prompt(
            self.project,
            [self.shot],
            MediaPromptOptions(
                model_identifier="minimax-h3-test",
                mode="T2VA",
                language_gate_mode="block",
                # The editor transports the saved values again.  Confirmed
                # English overlays must replace, not append to, these sources.
                overall_soundscape=self.shot.sound_effects,
                non_diegetic_music=self.shot.music,
            ),
        )
        self.assertFalse(
            compiled.payload["quality"]["production_language_gate"]["would_block_enqueue"]
        )
        self.assertNotIn("optimizer", compiled.payload["quality"])
        self.assertIn("<d>[Chinese] 别动。</d>", compiled.text)
        self.assertNotIn(self.shot.sound_effects, compiled.text)
        self.assertNotIn(self.shot.music, compiled.text)
        self.assertIn("Rain strikes the window while an old clock ticks.", compiled.text)
        self.assertIn("Paper rustles once as rain continues outside.", compiled.text)
        self.assertEqual(
            compiled.text.count("Paper rustles once as rain continues outside."), 2,
            # Once in the shot timeline and once in the aggregate soundscape.
        )
        self.assertIn("non_diegetic_music: Sparse low strings fade before the cut.", compiled.text)

        self.shot.action = "她收剑后转身看向窗外。"
        self.shot.save(update_fields=("action", "updated_at"))
        with self.assertRaisesRegex(PromptBuildError, "prompt.normalize"):
            build_test_video_prompt(
                self.project, [self.shot],
                MediaPromptOptions(
                    model_identifier="minimax-h3-test", mode="T2VA", language_gate_mode="block",
                ),
            )

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_auto_visual_draft_blocks_until_human_confirmation(self, translate):
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "color_script": "Cool rain shadows with restrained amber practical lights.",
            "translation_glossary": {"林默": "Lin Mo"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        original_visual = self.character.visual_identity
        translated_visual = (
            "A woman in her early thirties with short dark hair and a small mole "
            "below her left eye."
        )
        self.character.age_appearance = "A woman in her early thirties."
        self.character.face_identity = "High cheekbones and dark brown eyes."
        self.character.wardrobe = "A charcoal raincoat over a pale linen shirt."
        self.character.voice_profile = "A controlled low voice."
        self.character.save()
        scene = self.shot.scene
        scene.visual_identity = "A narrow archive room beneath an elevated railway."
        scene.location = "Old riverside district."
        scene.time_of_day = "Night."
        scene.lighting = "One green desk lamp and distant lightning."
        scene.color_palette = "Slate blue, charcoal, and dim amber."
        scene.ambient_sound = "Rain strikes the window while an old clock ticks."
        scene.save()
        self.shot.narrative = "The detective studies a scorched letter near the desk lamp."
        self.shot.action = "She raises the letter and studies its broken seal."
        self.shot.camera = "A slow eye-level push ends on the broken seal."
        self.shot.continuity_notes = "The letter remains in her right hand."
        self.shot.sound_effects = "Paper rustles once as rain continues outside."
        self.shot.music = "Sparse low strings at 72 BPM, fading before the cut."
        self.shot.dialogue = [{
            "speaker_id": self.character.stable_speaker_id,
            "text": "别动。",
            "delivery": "in a controlled whisper",
        }]
        self.shot.save()
        translate.return_value = [TranslationResult(
            text=translated_visual, provider="local_qwen",
            model="Qwen/Qwen3-4B-GGUF", metadata={"elapsed_ms": 10},
        )]

        ensured = self.client.post(
            reverse("project-ensure-canonical-glossary", args=[self.project.id]),
            {
                "entries": [{
                    "scope_type": "character", "scope_id": str(self.character.id),
                }],
                "ensure_visual_identity_translation": True,
            }, format="json",
        )

        self.assertEqual(ensured.status_code, 200, ensured.data)
        entry = PromptFieldTranslation.objects.get(
            project=self.project, scope_type="character",
            scope_id=self.character.id, field_key="character.visual_identity",
        )
        self.assertEqual(entry.status, PromptFieldTranslation.Status.DRAFT_TRANSLATION)
        self.character.refresh_from_db()
        self.assertEqual(self.character.visual_identity, original_visual)
        with self.assertRaisesRegex(PromptBuildError, "prompt.normalize"):
            build_test_video_prompt(
                self.project, [self.shot],
                MediaPromptOptions(
                    model_identifier="minimax-h3-test", mode="T2VA", language_gate_mode="block",
                ),
            )

        confirmed = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        confirmed_compiled = build_test_video_prompt(
            self.project, [self.shot],
            MediaPromptOptions(
                model_identifier="minimax-h3-test", mode="T2VA", language_gate_mode="block",
            ),
        )
        self.assertIn(translated_visual, confirmed_compiled.text)
        self.assertNotIn(original_visual, confirmed_compiled.text)
        self.assertFalse(
            confirmed_compiled.payload["quality"]["production_language_gate"]["would_block_enqueue"]
        )

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_auto_glossary_response_returns_project_updated_at(self, translate):
        self.character.name = "沈砚"
        self.character.save(update_fields=("name", "updated_at"))
        translate.return_value = [TranslationResult(
            text="Shen Yan", provider="local_qwen",
            model="Qwen/Qwen3-4B-GGUF", metadata={"elapsed_ms": 10},
        )]

        response = self.client.post(
            reverse("project-ensure-canonical-glossary", args=[self.project.id]),
            {
                "entries": [{
                    "scope_type": "character", "scope_id": str(self.character.id),
                }],
            }, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("updated_at", response.data)
        self.project.refresh_from_db()
        self.assertEqual(response.data["translation_glossary"]["沈砚"], "Shen Yan")
        self.assertEqual(response.data["updated_at"], self.project.updated_at)

    def test_glossary_change_invalidates_confirmed_translation_context(self):
        self.shot.action = "她拔出玄纹剑挡在门前。"
        self.shot.save(update_fields=("action", "updated_at"))
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {"玄纹剑": "Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        entry = PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type="shot",
            scope_id=self.shot.id,
            field_key="shot.action",
            source_text=self.shot.action,
            source_hash=source_text_hash(self.shot.action),
            translated_text="She draws the Xuanwen Sword and blocks the doorway.",
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata=self._context_metadata("shot", self.shot.id, "shot.action"),
        )
        self.assertEqual(
            confirmed_translation(
                project_id=self.project.id,
                scope_type="shot",
                scope_id=self.shot.id,
                field_key="shot.action",
                source_text=self.shot.action,
            ),
            entry.translated_text,
        )

        self.project.production_settings["translation_glossary"]["玄纹剑"] = "Mystic Pattern Sword"
        self.project.save(update_fields=("production_settings", "updated_at"))
        listed = self.client.get(
            reverse("project-translations", args=[self.project.id]),
        )
        self.assertEqual(listed.status_code, 200, listed.data)
        listed_entry = next(row for row in listed.data if row["id"] == str(entry.id))
        self.assertFalse(listed_entry["context_current"])
        self.assertEqual(
            confirmed_translation(
                project_id=self.project.id,
                scope_type="shot",
                scope_id=self.shot.id,
                field_key="shot.action",
                source_text=self.shot.action,
            ),
            self.shot.action,
        )
        response = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)

    def test_unrelated_glossary_addition_keeps_confirmed_translation_current(self):
        context_before = build_translation_context(
            self.project, scope_type="shot", scope_id=self.shot.id,
            field_key="shot.action",
        )
        entry = PromptFieldTranslation.objects.create(
            project=self.project, scope_type="shot", scope_id=self.shot.id,
            field_key="shot.action", source_text=self.shot.action,
            source_hash=source_text_hash(self.shot.action),
            translated_text="She draws her blade and blocks the doorway.",
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata={
                "translation_context_hash": translation_context_hash(context_before),
                "translation_context_version": TRANSLATION_CONTEXT_VERSION,
            },
        )

        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {"未出现在源文的玉佩": "Jade Pendant"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        context_after = build_translation_context(
            self.project, scope_type="shot", scope_id=self.shot.id,
            field_key="shot.action",
        )

        self.assertEqual(context_before, context_after)
        listed = self.client.get(reverse("project-translations", args=[self.project.id]))
        row = next(item for item in listed.data if item["id"] == str(entry.id))
        self.assertTrue(row["context_current"])
        self.assertEqual(
            confirmed_translation(
                project_id=self.project.id, scope_type="shot",
                scope_id=self.shot.id, field_key="shot.action",
                source_text=self.shot.action,
            ),
            entry.translated_text,
        )

    def test_confirm_rejects_candidate_that_drops_relevant_canonical_target(self):
        original = "她拔出玄纹剑挡在门前。"
        self.shot.narrative = original
        self.shot.save(update_fields=("narrative", "updated_at"))
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {"玄纹剑": "Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        entry = self._translation_entry(
            "shot.narrative", original,
            "She draws the Xuanwen Sword and blocks the doorway.",
        )

        response = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {
                "expected_revision": entry.revision,
                "translation": "She draws a sword and blocks the doorway.",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["details"]["code"], "canonical_target_missing",
        )
        self.assertEqual(
            response.data["error"]["details"]["missing_mappings"],
            [{"source_term": "玄纹剑", "canonical_target": "Xuanwen Sword"}],
        )
        self.shot.refresh_from_db()
        entry.refresh_from_db()
        self.assertEqual(self.shot.narrative, original)
        self.assertEqual(entry.revision, 1)

    def test_character_visual_edit_requires_glossary_for_fallback_prop_identity(self):
        prop = PropBible.objects.create(
            project=self.project,
            slug="broken-blade",
            name="断刃",
            visual_identity="锈迹斑斑的短柄断刃。",
        )
        original_visual = "左手常持一把短柄断刃，五官和断刃样式不得改变。"
        self.character.visual_identity = original_visual
        self.character.save(update_fields=("visual_identity", "updated_at"))
        canonical_target = f"prop:{prop.slug}"
        entry = PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type=PromptFieldTranslation.ScopeType.CHARACTER,
            scope_id=self.character.id,
            field_key="character.visual_identity",
            source_text=original_visual,
            source_hash=source_text_hash(original_visual),
            translated_text=f"He holds the short-handled {canonical_target} in his left hand.",
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata=self._context_metadata(
                PromptFieldTranslation.ScopeType.CHARACTER,
                self.character.id,
                "character.visual_identity",
            ),
        )

        rejected = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {
                "expected_revision": entry.revision,
                "translation": "He holds a short-handled broken blade in his left hand.",
            },
            format="json",
        )

        self.assertEqual(rejected.status_code, 409, rejected.data)
        details = rejected.data["error"]["details"]
        self.assertEqual(details["code"], "canonical_target_missing")
        self.assertEqual(
            details["missing_mappings"],
            [{"source_term": "断刃", "requires_glossary": True}],
        )
        self.assertNotIn("entry_id", details)
        self.assertNotIn("field_key", details)
        entry.refresh_from_db()
        self.character.refresh_from_db()
        self.assertEqual(entry.revision, 1)
        self.assertEqual(self.character.visual_identity, original_visual)

        accepted = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {
                "expected_revision": entry.revision,
                "translation": (
                    f"A young swordsman keeps the short-handled {canonical_target} "
                    "in his left hand at all times."
                ),
            },
            format="json",
        )

        self.assertEqual(accepted.status_code, 200, accepted.data)
        entry.refresh_from_db()
        self.character.refresh_from_db()
        self.assertEqual(entry.status, PromptFieldTranslation.Status.CONFIRMED)
        self.assertEqual(entry.revision, 2)
        self.assertEqual(entry.revision_history[-1]["event"], "translation_confirmed")
        self.assertEqual(self.character.visual_identity, original_visual)

    def test_confirm_hard_blocks_cross_entity_canonical_target_collision(self):
        prop = PropBible.objects.create(
            project=self.project, slug="xuanwen-collision", name="玄纹剑",
            visual_identity="a silver-gray sword with subtle dark patterns",
        )
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {
                self.character.name: "Ming Shao",
                prop.name: "ming   shao",
            },
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        original = "She steps into the rain."
        self.shot.narrative = original
        self.shot.save(update_fields=("narrative", "updated_at"))
        entry = self._translation_entry(
            "shot.narrative", original, original,
        )

        response = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        details = response.data["error"]["details"]
        self.assertEqual(details["code"], "canonical_target_collision")
        self.assertEqual(len(details["collisions"]), 1)
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.narrative, original)

    def test_legacy_whole_map_hash_is_safe_only_when_relevant_target_is_present(self):
        self.shot.action = "她握住玄纹剑挡在门前。"
        self.shot.save(update_fields=("action", "updated_at"))
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {"玄纹剑": "Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        mappings = dict(self.project.production_settings["translation_glossary"])
        for character in self.project.characters.all():
            mappings.setdefault(character.name, character.stable_speaker_id)
        for scene in self.project.scenes.all():
            mappings.setdefault(scene.name, f"scene:{scene.slug}")
        legacy_context = json.dumps({
            "purpose": "MiniMax H3 prompt field",
            "scope": "shot", "scope_id": str(self.shot.id),
            "field_key": "shot.action",
            "canonical_identity_mappings": mappings,
            "rule": "When a source term is present, reuse its mapped target verbatim.",
        }, ensure_ascii=False, sort_keys=True)
        entry = PromptFieldTranslation.objects.create(
            project=self.project, scope_type="shot", scope_id=self.shot.id,
            field_key="shot.action", source_text=self.shot.action,
            source_hash=source_text_hash(self.shot.action),
            translated_text="She holds the Xuanwen Sword and blocks the doorway.",
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata={
                "translation_context_hash": translation_context_hash(legacy_context),
            },
        )
        PropBible.objects.create(
            project=self.project, slug="unrelated-jade", name="无关玉佩",
            visual_identity="暗绿色玉佩",
        )

        listed = self.client.get(reverse("project-translations", args=[self.project.id]))
        row = next(item for item in listed.data if item["id"] == str(entry.id))
        self.assertTrue(row["context_current"])
        self.assertEqual(
            confirmed_translation(
                project_id=self.project.id, scope_type="shot",
                scope_id=self.shot.id, field_key="shot.action",
                source_text=self.shot.action,
            ), entry.translated_text,
        )
        confirmed = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        self.project.production_settings["translation_glossary"]["玄纹剑"] = (
            "Mystic Pattern Sword"
        )
        self.project.save(update_fields=("production_settings", "updated_at"))
        listed = self.client.get(reverse("project-translations", args=[self.project.id]))
        row = next(item for item in listed.data if item["id"] == str(entry.id))
        self.assertFalse(row["context_current"])
