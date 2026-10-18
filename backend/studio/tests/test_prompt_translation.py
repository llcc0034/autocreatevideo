from unittest.mock import patch
from studio.tests.primary_setting_fixtures import create_primary_setting
import json
import uuid

from django.test import SimpleTestCase, TestCase
from django.urls import NoReverseMatch, reverse
from rest_framework.test import APIClient

from studio.models import PromptFieldTranslation, PropBible, Shot, ShotCharacterRelation
from studio.services.prompt_generation_sources import (
    MODEL_GENERATION_MODE_VALUES,
    prompt_generation_source_values,
)
from studio.services.prompt_normalization_contract import MODEL_PROCESSING_MODES
from studio.services.prompt_translation import (
    ALLOWED_FIELD_KEYS,
    field_key_allowed,
    resolve_current_source,
    translation_entry_context_current,
    build_translation_context,
    PromptTranslationError,
    TRANSLATION_CONTEXT_VERSION,
    TranslationValidationError,
    source_text_hash,
    _protect_translation_contract,
    _restore_authored_blocks,
    translation_context_hash,
    validate_manual_translation,
)
from studio.tests.factories import (
    build_story,
)


class PromptGenerationSourceRegistryTests(SimpleTestCase):
    def test_translation_editable_fields_are_not_generation_reader_options(self):
        self.assertIn("character.face_identity", ALLOWED_FIELD_KEYS["character"])
        self.assertEqual(ALLOWED_FIELD_KEYS["shot"], set())
        self.assertTrue(field_key_allowed("shot", "shot.dialogue.0.delivery"))
        self.assertNotIn("character.face_identity", prompt_generation_source_values())
        self.assertNotIn("shot.camera", prompt_generation_source_values())
        self.assertNotIn("request.action", set().union(*ALLOWED_FIELD_KEYS.values()))
        self.assertIn("request.action", prompt_generation_source_values())
        self.assertIs(MODEL_PROCESSING_MODES, MODEL_GENERATION_MODE_VALUES)


class PromptTranslationProtectionTests(TestCase):
    # Content safety ceilings are database-owned security boundaries.

    def test_manual_translation_must_preserve_authored_blocks_verbatim(self):
        source = "她说 <d>[Chinese] 别动。</d>，门上写着 <screen_text>客栈</screen_text>。"
        valid = (
            "She speaks <d>[Chinese] 别动。</d>, while the door displays "
            "<screen_text>客栈</screen_text>."
        )
        self.assertEqual(validate_manual_translation(source, valid), valid)
        with self.assertRaises(TranslationValidationError):
            validate_manual_translation(source, "She speaks <d>[English] Stop.</d>.")
        self.assertEqual(validate_manual_translation(source, valid + " 中文"), valid + " 中文")
        self.assertEqual(validate_manual_translation("缓慢推近。", "カメラがゆっくり前進します。"), "カメラがゆっくり前進します。")
        self.assertEqual(validate_manual_translation("缓慢推近。", "---"), "---")
        for deceptive in (
            "Slow push \u202e hidden.",
            "Slow\u200b push.",
            "Slow push.\u0007",
        ):
            with self.subTest(deceptive=deceptive), self.assertRaises(
                TranslationValidationError,
            ):
                validate_manual_translation("缓慢推近。", deceptive)

    def test_source_protection_restores_literals_and_exact_canonical_target(self):
        context = json.dumps({"canonical_identity_mappings":{"断刃":"Configured Blade"}},ensure_ascii=False)
        source = "左手持有断刃。她说 <d>[Chinese] 别动。</d>"
        protected, blocks = _protect_translation_contract(source,context)
        self.assertNotIn("断刃",protected)
        self.assertNotIn("<d>",protected)
        values = list(blocks.values())
        self.assertIn("Configured Blade",values)
        self.assertIn("<d>[Chinese] 别动。</d>",values)
        translated = "She holds " + next(key for key,value in blocks.items() if value=="Configured Blade") + ". She speaks " + next(key for key,value in blocks.items() if value.startswith("<d>"))
        restored = _restore_authored_blocks(translated,blocks)
        self.assertIn("Configured Blade",restored)
        self.assertIn("<d>[Chinese] 别动。</d>",restored)
        self.assertEqual(validate_manual_translation(source,restored),restored)


class PromptTranslationApiTests(TestCase):
    def setUp(self):
        (
            self.user, self.project, self.character, _, self.episode,
            self.shot, _,
        ) = build_story()
        ShotCharacterRelation.objects.bulk_create([ShotCharacterRelation(shot=self.shot, character=self.character,
            role=role, position=0) for role in ("visible", "speaker")])
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.project.continuity_summary = "她拔剑挡在门前，雨水沿剑锋滴落。"
        self.project.save(update_fields=("continuity_summary", "updated_at"))

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
            scope_type="shot" if field_key.startswith("shot.") else "project",
            scope_id=self.shot.id if field_key.startswith("shot.") else self.project.id,
            field_key=field_key,
            source_text=source,
            source_hash=source_text_hash(source),
            translated_text=translated,
            status=status or PromptFieldTranslation.Status.DRAFT_TRANSLATION,
            runtime_metadata=metadata or self._context_metadata(
                "shot" if field_key.startswith("shot.") else "project",
                self.shot.id if field_key.startswith("shot.") else self.project.id, field_key,
            ),
        )

    def test_dialogue_source_is_narrow_and_scoped_without_creative_column_fallback(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.shot.dialogue = [{"speaker_id": "S1", "text": " 原对白 ", "delivery": "  低声\n"}]
        self.shot.save(update_fields=("dialogue",))
        with CaptureQueriesContext(connection) as queries:
            value = resolve_current_source(project=self.project, scope_type="shot",
                scope_id=self.shot.pk, field_key="shot.dialogue.0.delivery")
        self.assertEqual(value, "  低声\n")
        self.assertEqual(len(queries), 1)
        selected = queries[0]["sql"].split(" FROM ")[0]
        self.assertIn('"studio_shot"."dialogue"', selected)
        self.assertNotIn('"studio_shot"."metadata"', selected)
        self.assertNotIn('"studio_shot"."title"', selected)
        for key, scope_id in (("shot.dialogue.1.delivery", self.shot.pk),
                ("shot.dialogue.0.delivery", uuid.uuid4())):
            with self.assertRaises(TranslationValidationError):
                resolve_current_source(project=self.project, scope_type="shot", scope_id=scope_id, field_key=key)

    def test_translation_list_rejects_non_uuid_scope_filters_as_bad_request(self):
        url = reverse("project-translations", args=[self.project.id])

        for field in ("episode", "shot"):
            with self.subTest(field=field):
                response = self.client.get(url, {field: "shot-1787632938052-1"})

                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["error"]["code"], "invalid")
                self.assertIn(field, response.data["error"]["details"])

    @patch("studio.services.provider_registry.OpenAICompatibleDriver.invoke")
    def test_manual_project_negative_prompt_candidate_uses_no_model(
        self, translate,
    ):
        source = "人物身份漂移；多余肢体；文字水印；低清晰度"
        translated = "identity drift; extra limbs; text watermark; low resolution"
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "negative_prompt": source,
        }
        self.project.save(update_fields=("production_settings", "updated_at"))

        created = self.client.post(
            reverse("project-create-manual-prompt-fields", args=[self.project.id]),
            {"entries": [{
                "scope_type": "project",
                "scope_id": str(self.project.id),
                "field_key": "project.production_settings.negative_prompt",
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
            scope_type=PromptFieldTranslation.ScopeType.PROJECT,
            scope_id=self.project.id,
            field_key="project.production_settings.negative_prompt",
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
        self.assertNotIn("promoted_fields", confirmed.data)
        self.assertEqual(entry.translated_text, translated)
        self.assertEqual(entry.source_text, source)
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings["negative_prompt"], source)

    @patch("studio.services.provider_registry.OpenAICompatibleDriver.invoke")
    def test_confirm_and_revoke_preserve_authored_source_and_shot(self, invoke):
        original = self.project.continuity_summary
        before = Shot.objects.values().get(pk=self.shot.pk)
        entry = self._translation_entry("project.continuity_summary", original,
            "She draws her sword and stands before the doorway.")
        source_metadata = entry.runtime_metadata
        confirmed = self.client.post(reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertNotIn("promoted_fields", confirmed.data)
        self.assertTrue(confirmed.data["context_current"])
        entry.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, original)
        self.assertEqual(Shot.objects.values().get(pk=self.shot.pk), before)
        self.assertEqual(entry.source_text, original)
        self.assertEqual(entry.source_hash, source_text_hash(original))
        self.assertEqual(entry.runtime_metadata, source_metadata)
        self.assertEqual(entry.revision_history[-1]["event"], "translation_confirmed")
        self.assertEqual(entry.revision_history[-1]["confirmed_by"], str(self.user.pk))
        self.assertEqual(entry.revision_history[-1]["source_text"], original)
        retry = self.client.post(reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": 1}, format="json")
        self.assertEqual(retry.status_code, 409, retry.data)
        self.assertEqual(retry.data["error"]["details"]["code"], "revision_conflict")
        entry.refresh_from_db()
        self.assertEqual(entry.revision, 2)
        self.assertEqual(len(entry.revision_history), 1)
        revoked = self.client.post(reverse("translation-revoke", args=[entry.id]), {}, format="json")
        self.assertEqual(revoked.status_code, 200, revoked.data)
        self.assertEqual(Shot.objects.values().get(pk=self.shot.pk), before)
        entry.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, original)
        self.assertEqual(entry.source_text, original)
        self.assertEqual(entry.status, PromptFieldTranslation.Status.DRAFT_TRANSLATION)
        invoke.assert_not_called()

    def test_retired_shot_promotion_is_not_a_write_endpoint(self):
        before = Shot.objects.values().get(pk=self.shot.pk)
        with self.assertRaises(NoReverseMatch):
            reverse("translation-promote-shot")
        result = self.client.post("/api/v1/translations/promote-shot/",
            {"shot_id": str(self.shot.pk), "entries": []}, format="json")
        self.assertIn(result.status_code, (404, 405))
        self.assertEqual(Shot.objects.values().get(pk=self.shot.pk), before)

    def test_dialogue_review_preserves_original_delivery_text_and_milliseconds(self):
        self.shot.dialogue = [{"speaker_id": self.character.stable_speaker_id,
            "text": "  别动。  ", "delivery": "压低声音", "voiceover": True,
            "start_ms": 123, "end_ms": 456}]
        self.shot.save(update_fields=("dialogue", "updated_at"))
        before = Shot.objects.values().get(pk=self.shot.pk)
        response = self.client.post(reverse("project-create-manual-prompt-fields", args=[self.project.pk]),
            {"entries": [{"scope_type": "shot", "scope_id": str(self.shot.pk),
                "field_key": "shot.dialogue.0.delivery", "source_text": "压低声音"}]}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        entry = PromptFieldTranslation.objects.get(project=self.project, field_key="shot.dialogue.0.delivery")
        response = self.client.post(reverse("translation-confirm", args=[entry.pk]),
            {"expected_revision": entry.revision, "translation": "in a low voice"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Shot.objects.values().get(pk=self.shot.pk), before)
        entry.refresh_from_db()
        self.assertEqual(entry.source_text, "压低声音")
        self.assertEqual(entry.source_hash, source_text_hash("压低声音"))
        self.assertEqual(entry.translated_text, "in a low voice")

    def test_retired_shot_sources_cannot_be_created_or_confirmed(self):
        before = Shot.objects.values().get(pk=self.shot.pk)
        for name in ("narrative", "detailed_description", "camera", "action",
                "sound_effects", "music", "continuity_notes", "negative_prompt"):
            key = f"shot.{name}"
            source = "Historical source only."
            with self.subTest(key=key):
                with self.assertNumQueries(0), self.assertRaises(TranslationValidationError):
                    resolve_current_source(project=self.project, scope_type="shot",
                        scope_id=self.shot.pk, field_key=key)
                with self.assertNumQueries(0), self.assertRaises(TranslationValidationError):
                    build_translation_context(self.project, scope_type="shot",
                        scope_id=self.shot.pk, field_key=key, source_text=source)
                created = self.client.post(reverse("project-create-manual-prompt-fields", args=[self.project.pk]),
                    {"entries": [{"scope_type": "shot", "scope_id": str(self.shot.pk),
                        "field_key": key, "source_text": source}]}, format="json")
                self.assertEqual(created.status_code, 400, created.data)
                entry = PromptFieldTranslation.objects.create(project=self.project, scope_type="shot",
                    scope_id=self.shot.pk, field_key=key, source_text=source, source_hash=source_text_hash(source),
                    translated_text="An old result.", status=PromptFieldTranslation.Status.CONFIRMED,
                    runtime_metadata={"promotion": {"promoted": True, "field_key": key}})
                before_entry = PromptFieldTranslation.objects.values().get(pk=entry.pk)
                self.assertFalse(translation_entry_context_current(entry, project=self.project, current_source=source))
                response = self.client.get(reverse("translation-detail", args=[entry.pk]))
                self.assertEqual(response.status_code, 200, response.data)
                self.assertFalse(response.data["context_current"])
                self.assertNotIn("promoted_fields", response.data)
                self.assertTrue(response.data["runtime_metadata"]["promotion"]["promoted"])
                response = self.client.post(reverse("translation-confirm", args=[entry.pk]),
                    {"expected_revision": entry.revision}, format="json")
                self.assertEqual(response.status_code, 409, response.data)
                self.assertEqual(response.data["error"]["details"]["code"], "source_unavailable")
                self.assertEqual(PromptFieldTranslation.objects.values().get(pk=entry.pk), before_entry)
        self.assertEqual(Shot.objects.values().get(pk=self.shot.pk), before)

    def test_dialogue_review_rejects_reordered_occurrence_identity(self):
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


    def test_confirmation_rejects_stale_source(self):
        entry = PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type="project",
            scope_id=self.project.id,
            field_key="project.continuity_summary",
            source_text=self.project.continuity_summary,
            source_hash=source_text_hash(self.project.continuity_summary),
            translated_text="A low-angle slow push ends on the sword tip.",
            status=PromptFieldTranslation.Status.DRAFT_TRANSLATION,
        )
        self.project.continuity_summary = "固定中景。"
        self.project.save(update_fields=("continuity_summary", "updated_at"))
        response = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )
        self.assertEqual(response.status_code, 409)
        entry.refresh_from_db()
        self.assertNotEqual(entry.status, PromptFieldTranslation.Status.CONFIRMED)

    def test_confirmed_candidate_becomes_stale_without_rewriting_its_source(self):
        source = self.project.continuity_summary
        translated = "She draws her sword and blocks the doorway."
        entry = self._translation_entry("project.continuity_summary", source, translated,
            status=PromptFieldTranslation.Status.CONFIRMED)
        before = PromptFieldTranslation.objects.values().get(pk=entry.pk)
        url = reverse("translation-detail", args=[entry.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["context_current"])
        changed = "她收剑后转身看向窗外。"
        self.project.continuity_summary = changed
        self.project.save(update_fields=("continuity_summary", "updated_at"))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["context_current"])
        self.assertEqual(PromptFieldTranslation.objects.values().get(pk=entry.pk), before)
        self.project.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, changed)

    def test_visual_candidate_confirmation_preserves_exact_primary_setting(self):
        original_visual = "三十岁短发女侦探，左眼下小痣"
        primary = create_primary_setting(self.character, original_visual)
        translated_visual = "A woman with short dark hair and a small mole below the left eye."
        key = "character.primary_setting.identity_anchor"
        created = self.client.post(reverse("project-create-manual-prompt-fields", args=[self.project.pk]), {
            "entries": [{"scope_type": "character", "scope_id": str(self.character.pk),
                "field_key": key, "source_text": original_visual}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        entry = PromptFieldTranslation.objects.get(project=self.project, scope_type="character",
            scope_id=self.character.pk, field_key=key)
        self.assertEqual(entry.status, PromptFieldTranslation.Status.DRAFT_TRANSLATION)
        self.assertEqual(entry.translated_text, "")
        self.assertEqual(entry.source_text, original_visual)
        confirmed = self.client.post(reverse("translation-confirm", args=[entry.pk]),
            {"expected_revision": entry.revision, "translation": translated_visual}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        entry.refresh_from_db()
        self.assertEqual(entry.status, PromptFieldTranslation.Status.CONFIRMED)
        self.assertEqual(entry.translated_text, translated_visual)
        self.assertEqual(entry.source_text, original_visual)
        self.assertTrue(confirmed.data["context_current"])
        self.assertEqual(primary.revisions.get(revision=1).identity_anchor, original_visual)
        self.assertEqual(primary.revisions.count(), 1)

    def test_glossary_change_invalidates_confirmed_translation_context(self):
        self.project.continuity_summary = "她拔出玄纹剑挡在门前。"
        self.project.save(update_fields=("continuity_summary", "updated_at"))
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {"玄纹剑": "Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        entry = PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type="project",
            scope_id=self.project.id,
            field_key="project.continuity_summary",
            source_text=self.project.continuity_summary,
            source_hash=source_text_hash(self.project.continuity_summary),
            translated_text="She draws the Xuanwen Sword and blocks the doorway.",
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata=self._context_metadata("project", self.project.id, "project.continuity_summary"),
        )
        self.assertTrue(translation_entry_context_current(entry, project=self.project))

        self.project.production_settings["translation_glossary"]["玄纹剑"] = "Mystic Pattern Sword"
        self.project.save(update_fields=("production_settings", "updated_at"))
        listed = self.client.get(
            reverse("project-translations", args=[self.project.id]),
        )
        self.assertEqual(listed.status_code, 200, listed.data)
        listed_entry = next(row for row in listed.data if row["id"] == str(entry.id))
        self.assertFalse(listed_entry["context_current"])
        self.assertFalse(translation_entry_context_current(entry, project=self.project))
        response = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)

    def test_unrelated_glossary_addition_keeps_confirmed_translation_current(self):
        context_before = build_translation_context(
            self.project, scope_type="project", scope_id=self.project.id,
            field_key="project.continuity_summary",
        )
        entry = PromptFieldTranslation.objects.create(
            project=self.project, scope_type="project", scope_id=self.project.id,
            field_key="project.continuity_summary", source_text=self.project.continuity_summary,
            source_hash=source_text_hash(self.project.continuity_summary),
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
            self.project, scope_type="project", scope_id=self.project.id,
            field_key="project.continuity_summary",
        )

        self.assertEqual(context_before, context_after)
        listed = self.client.get(reverse("project-translations", args=[self.project.id]))
        row = next(item for item in listed.data if item["id"] == str(entry.id))
        self.assertTrue(row["context_current"])
        self.assertTrue(translation_entry_context_current(entry, project=self.project))

    def test_confirm_rejects_candidate_that_drops_relevant_canonical_target(self):
        original = "她拔出玄纹剑挡在门前。"
        self.project.continuity_summary = original
        self.project.save(update_fields=("continuity_summary", "updated_at"))
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {"玄纹剑": "Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        entry = self._translation_entry(
            "project.continuity_summary", original,
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
        self.project.refresh_from_db()
        entry.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, original)
        self.assertEqual(entry.revision, 1)

    def test_character_visual_edit_requires_glossary_for_fallback_prop_identity(self):
        prop = PropBible.objects.create(
            project=self.project,
            slug="broken-blade",
            name="断刃",
        )
        original_visual = "左手常持一把短柄断刃，五官和断刃样式不得改变。"
        primary = create_primary_setting(self.character, original_visual)
        canonical_target = f"prop:{prop.slug}"
        entry = PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type=PromptFieldTranslation.ScopeType.CHARACTER,
            scope_id=self.character.id,
            field_key="character.primary_setting.identity_anchor",
            source_text=original_visual,
            source_hash=source_text_hash(original_visual),
            translated_text=f"He holds the short-handled {canonical_target} in his left hand.",
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata=self._context_metadata(
                PromptFieldTranslation.ScopeType.CHARACTER,
                self.character.id,
                "character.primary_setting.identity_anchor",
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
        self.assertEqual(primary.revisions.get(revision=1).identity_anchor, original_visual)

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
        self.assertEqual(primary.revisions.get(revision=1).identity_anchor, original_visual)

    def test_confirm_hard_blocks_cross_entity_canonical_target_collision(self):
        prop = PropBible.objects.create(
            project=self.project, slug="xuanwen-collision", name="玄纹剑",
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
        self.project.continuity_summary = original
        self.project.save(update_fields=("continuity_summary", "updated_at"))
        entry = self._translation_entry(
            "project.continuity_summary", original, original,
        )

        response = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        details = response.data["error"]["details"]
        self.assertEqual(details["code"], "canonical_target_collision")
        self.assertEqual(len(details["collisions"]), 1)
        self.project.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, original)

    def test_context_version_must_be_explicit_current_integer_even_with_matching_hash(self):
        from studio.services.prompt_translation import translation_entry_context_current
        entry = PromptFieldTranslation(project=self.project, scope_type="project", scope_id=self.project.id,
            field_key="project.continuity_summary", source_text=self.project.continuity_summary, source_hash=source_text_hash(self.project.continuity_summary),
            translated_text="She watches the doorway.", runtime_metadata=self._context_metadata("project", self.project.id, "project.continuity_summary"))
        self.assertTrue(translation_entry_context_current(entry, project=self.project))
        for version in (None, True, float(TRANSLATION_CONTEXT_VERSION), str(TRANSLATION_CONTEXT_VERSION), 1, 3):
            with self.subTest(version=version):
                entry.runtime_metadata["translation_context_version"] = version
                self.assertFalse(translation_entry_context_current(entry, project=self.project))

    def test_legacy_context_is_rejected_even_when_relevant_target_is_present(self):
        self.project.continuity_summary = "她握住玄纹剑挡在门前。"
        self.project.save(update_fields=("continuity_summary", "updated_at"))
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
            "scope": "project", "scope_id": str(self.project.id),
            "field_key": "project.continuity_summary",
            "canonical_identity_mappings": mappings,
            "rule": "When a source term is present, reuse its mapped target verbatim.",
        }, ensure_ascii=False, sort_keys=True)
        entry = PromptFieldTranslation.objects.create(
            project=self.project, scope_type="project", scope_id=self.project.id,
            field_key="project.continuity_summary", source_text=self.project.continuity_summary,
            source_hash=source_text_hash(self.project.continuity_summary),
            translated_text="She holds the Xuanwen Sword and blocks the doorway.",
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata={
                "translation_context_hash": translation_context_hash(legacy_context),
            },
        )
        PropBible.objects.create(
            project=self.project, slug="unrelated-jade", name="无关玉佩",
        )

        listed = self.client.get(reverse("project-translations", args=[self.project.id]))
        row = next(item for item in listed.data if item["id"] == str(entry.id))
        self.assertFalse(row["context_current"])
        self.assertFalse(translation_entry_context_current(entry, project=self.project))
        confirmed = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )
        self.assertEqual(confirmed.status_code, 409, confirmed.data)

        self.project.production_settings["translation_glossary"]["玄纹剑"] = (
            "Mystic Pattern Sword"
        )
        self.project.save(update_fields=("production_settings", "updated_at"))
        listed = self.client.get(reverse("project-translations", args=[self.project.id]))
        row = next(item for item in listed.data if item["id"] == str(entry.id))
        self.assertFalse(row["context_current"])
