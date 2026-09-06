import json
from unittest.mock import patch

from django.urls import reverse
from rest_framework.test import APIClient
from django.test import TestCase

from studio.models import (
    CharacterBible,
    ConfigRevision,
    PromptFieldTranslation,
    PropBible,
    SceneBible,
)
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.canonical_glossary import validate_canonical_english
from studio.services.prompt_translation import (
    build_translation_context,
    PromptTranslationError,
    TRANSLATION_CONTEXT_VERSION,
    source_text_hash,
    TranslationResult,
    TranslationValidationError,
    translation_context_hash,
)
from studio.tests.factories import (
    build_story,
    create_ready_prompt_normalization_test_deployment,
)


def translated(value: str) -> TranslationResult:
    return TranslationResult(
        text=value, provider="third-local-normalizer", model="third-local-model",
        metadata={"elapsed_ms": 10},
    )


class CanonicalGlossaryApiTests(TestCase):
    def setUp(self):
        self.user, self.project, self.character, self.scene, _, self.shot, _ = build_story(
            "canonical-owner",
        )
        self.prop = PropBible.objects.create(
            project=self.project, slug="xuanwen-sword", name="玄纹剑",
            description="凌霜随身佩剑", visual_identity="银灰剑身带有暗纹",
        )
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
        self.url = reverse("project-ensure-canonical-glossary", args=[self.project.pk])

    def selectors(self, *entities):
        scopes = {
            CharacterBible: "character", SceneBible: "scene", PropBible: "prop",
        }
        return {
            "entries": [{
                "scope_type": scopes[type(entity)], "scope_id": str(entity.pk),
            } for entity in entities],
        }

    def context_metadata(self, entity, scope_type, field_key):
        return {
            "translation_context_hash": translation_context_hash(
                build_translation_context(
                    self.project, scope_type=scope_type,
                    scope_id=entity.pk, field_key=field_key,
                )
            ),
            "translation_context_version": TRANSLATION_CONTEXT_VERSION,
        }

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_generates_missing_names_in_one_batch_and_persists_auditable_defaults(self, translate):
        translate.return_value = [translated("Lin Mo"), translated("Xuanwen Sword")]

        response = self.client.post(
            self.url, self.selectors(self.character, self.prop), format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["summary"], {
            "requested": 2, "generated": 2, "reused": 0,
            "conflict": 0, "failed": 0,
        })
        self.project.refresh_from_db()
        self.assertEqual(
            self.project.production_settings["translation_glossary"],
            {"林默": "Lin Mo", "玄纹剑": "Xuanwen Sword"},
        )
        self.assertEqual(translate.call_count, 1)
        self.assertTrue(response.data["runtime"]["invocation_attempted"])
        self.assertFalse(response.data["runtime"]["submission_uncertain"])
        self.assertEqual(
            response.data["runtime"]["model_deployment_id"],
            str(self.deployment_id),
        )
        self.assertEqual(response.data["runtime"]["provider"], "third-local-normalizer")
        self.assertFalse(response.data["runtime"]["data_egress"])
        call = translate.call_args
        self.assertEqual(call.args[0], ["林默", "玄纹剑"])
        self.assertEqual(call.kwargs["field_keys"], [
            "canonical_glossary.character_name", "canonical_glossary.prop_name",
        ])
        self.assertNotIn("S1", call.kwargs["contexts"][0])
        rows = PromptFieldTranslation.objects.filter(
            project=self.project, field_key__in=("character.name", "prop.name"),
        )
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all(row.status == "confirmed" for row in rows))
        self.assertTrue(all(row.provider == "third-local-normalizer" for row in rows))
        self.assertTrue(all(row.confirmed_by_id == self.user.id for row in rows))
        self.assertTrue(all(row.runtime_metadata["auto_glossary"] for row in rows))
        self.assertTrue(all(row.runtime_metadata["inference_context_hash"] for row in rows))
        self.assertTrue(all(
            row.runtime_metadata["inference_context_hash"]
            != row.runtime_metadata["translation_context_hash"]
            for row in rows
        ))
        listed = self.client.get(reverse("project-translations", args=[self.project.pk]))
        name_rows = [row for row in listed.data if row["field_key"].endswith(".name")]
        self.assertEqual(len(name_rows), 2)
        self.assertTrue(all(row["context_current"] for row in name_rows))

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_ensure_rejects_generated_cross_entity_target_collision_atomically(self, translate):
        translate.return_value = [translated("Ming Shao"), translated("ming   shao")]

        response = self.client.post(
            self.url, self.selectors(self.character, self.prop), format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        details = response.data["error"]["details"]
        self.assertEqual(details["code"], "canonical_target_collision")
        self.project.refresh_from_db()
        self.assertFalse(
            (self.project.production_settings or {}).get("translation_glossary")
        )
        self.assertFalse(PromptFieldTranslation.objects.filter(
            project=self.project, field_key__in=("character.name", "prop.name"),
        ).exists())

    def test_project_settings_update_rejects_cross_entity_target_collision(self):
        response = self.client.patch(
            reverse("project-detail", args=[self.project.id]),
            {
                "production_settings": {
                    **(self.project.production_settings or {}),
                    "translation_glossary": {
                        self.character.name: "Ming Shao",
                        self.prop.name: "Ｍｉｎｇ Shao",
                    },
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.project.refresh_from_db()
        self.assertFalse(
            (self.project.production_settings or {}).get("translation_glossary")
        )

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_existing_manual_mapping_is_reused_without_model_load_or_overwrite(self, translate):
        self.project.production_settings = {
            "translation_glossary": {"玄纹剑": "The User's Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))

        response = self.client.post(self.url, self.selectors(self.prop), format="json")

        self.assertEqual(response.status_code, 200, response.data)
        translate.assert_not_called()
        self.project.refresh_from_db()
        self.assertEqual(
            self.project.production_settings["translation_glossary"]["玄纹剑"],
            "The User's Xuanwen Sword",
        )
        self.assertEqual(response.data["entries"][0]["status"], "reused")
        self.assertEqual(response.data["entries"][0]["origin"], "existing")

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_invalid_existing_manual_mapping_is_reported_but_never_overwritten(self, translate):
        self.project.production_settings = {"translation_glossary": {"玄纹剑": "玄纹 Sword"}}
        self.project.save(update_fields=("production_settings", "updated_at"))

        response = self.client.post(self.url, self.selectors(self.prop), format="json")

        self.assertEqual(response.status_code, 200, response.data)
        translate.assert_not_called()
        self.assertEqual(response.data["entries"][0]["status"], "conflict")
        self.assertEqual(response.data["entries"][0]["error_code"], "invalid_existing_mapping")
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings["translation_glossary"]["玄纹剑"], "玄纹 Sword")

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_partial_model_failure_keeps_successful_sibling(self, translate):
        translate.return_value = [
            translated("Lin Mo"),
            PromptTranslationError("候选仍含中文", code="residual_cjk_after_repair"),
        ]

        response = self.client.post(
            self.url, self.selectors(self.character, self.prop), format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["summary"]["generated"], 1)
        self.assertEqual(response.data["summary"]["failed"], 1)
        self.assertTrue(next(row for row in response.data["entries"] if row["name"] == "玄纹剑")["retryable"])
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings["translation_glossary"], {"林默": "Lin Mo"})

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_provider_resource_busy_is_a_retryable_row_not_an_http_failure(self, translate):
        translate.side_effect = PromptTranslationError(
            "共享计算资源正忙", code="translation_resource_busy",
        )

        response = self.client.post(self.url, self.selectors(self.prop), format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["entries"][0]["status"], "failed")
        self.assertEqual(response.data["entries"][0]["error_code"], "translation_resource_busy")
        self.assertTrue(response.data["entries"][0]["retryable"])
        self.project.refresh_from_db()
        self.assertNotIn("玄纹剑", self.project.production_settings.get("translation_glossary", {}))

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_concurrent_human_value_wins_final_row_lock_merge(self, translate):
        def human_writes_while_model_runs(*_args, **_kwargs):
            current = self.project.__class__.objects.get(pk=self.project.pk)
            current.production_settings = {
                **(current.production_settings or {}),
                "translation_glossary": {"玄纹剑": "Human Approved Sword"},
            }
            current.save(update_fields=("production_settings", "updated_at"))
            return [translated("Model Sword")]

        translate.side_effect = human_writes_while_model_runs
        response = self.client.post(self.url, self.selectors(self.prop), format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["entries"][0]["status"], "reused")
        self.project.refresh_from_db()
        self.assertEqual(
            self.project.production_settings["translation_glossary"]["玄纹剑"],
            "Human Approved Sword",
        )

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_duplicate_names_share_one_model_item(self, translate):
        duplicate = PropBible.objects.create(
            project=self.project, slug="spare-xuanwen", name="玄纹剑",
        )
        translate.return_value = [translated("Xuanwen Sword")]

        response = self.client.post(
            self.url, self.selectors(self.prop, duplicate), format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(translate.call_args.args[0], ["玄纹剑"])
        self.assertEqual(response.data["summary"]["generated"], 1)
        self.assertEqual(response.data["summary"]["reused"], 1)

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_rename_replaces_only_unchanged_auto_mapping(self, translate):
        self.project.production_settings = {"translation_glossary": {"玄纹剑": "Xuanwen Sword"}}
        self.project.save(update_fields=("production_settings", "updated_at"))
        PromptFieldTranslation.objects.create(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.name", source_text="玄纹剑",
            source_hash="old", translated_text="Xuanwen Sword", status="confirmed",
            provider="local_qwen_auto_glossary",
            runtime_metadata={"auto_glossary": True},
        )
        self.prop.name = "听雨剑"
        self.prop.save(update_fields=("name", "updated_at"))
        translate.return_value = [translated("Listening Rain Sword")]

        response = self.client.post(self.url, self.selectors(self.prop), format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings["translation_glossary"], {
            "听雨剑": "Listening Rain Sword",
        })
        entry = PromptFieldTranslation.objects.get(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.name",
        )
        self.assertEqual(entry.revision, 2)
        self.assertEqual(entry.source_text, "听雨剑")
        self.assertEqual(entry.revision_history[0]["translated_text"], "Xuanwen Sword")

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_rename_keeps_old_mapping_after_human_changed_auto_value(self, translate):
        self.project.production_settings = {"translation_glossary": {"玄纹剑": "My Custom Sword"}}
        self.project.save(update_fields=("production_settings", "updated_at"))
        PromptFieldTranslation.objects.create(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.name", source_text="玄纹剑",
            source_hash="old", translated_text="Xuanwen Sword", status="confirmed",
            provider="local_qwen_auto_glossary",
        )
        self.prop.name = "听雨剑"
        self.prop.save(update_fields=("name", "updated_at"))
        translate.return_value = [translated("Listening Rain Sword")]

        response = self.client.post(self.url, self.selectors(self.prop), format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings["translation_glossary"], {
            "玄纹剑": "My Custom Sword", "听雨剑": "Listening Rain Sword",
        })

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_rename_keeps_old_auto_mapping_while_another_bible_still_uses_name(self, translate):
        PropBible.objects.create(
            project=self.project, slug="shared-xuanwen", name="玄纹剑",
        )
        self.project.production_settings = {"translation_glossary": {"玄纹剑": "Xuanwen Sword"}}
        self.project.save(update_fields=("production_settings", "updated_at"))
        PromptFieldTranslation.objects.create(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.name", source_text="玄纹剑", source_hash="old",
            translated_text="Xuanwen Sword", status="confirmed",
            provider="local_qwen_auto_glossary",
        )
        self.prop.name = "听雨剑"
        self.prop.save(update_fields=("name", "updated_at"))
        translate.return_value = [translated("Listening Rain Sword")]

        response = self.client.post(self.url, self.selectors(self.prop), format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings["translation_glossary"], {
            "玄纹剑": "Xuanwen Sword", "听雨剑": "Listening Rain Sword",
        })

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_name_changed_during_model_run_returns_conflict_and_writes_nothing(self, translate):
        def rename_while_model_runs(*_args, **_kwargs):
            PropBible.objects.filter(pk=self.prop.pk).update(name="听雨剑")
            return [translated("Xuanwen Sword")]

        translate.side_effect = rename_while_model_runs
        response = self.client.post(self.url, self.selectors(self.prop), format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["entries"][0]["status"], "conflict")
        self.assertEqual(response.data["entries"][0]["error_code"], "entity_changed")
        self.project.refresh_from_db()
        self.assertNotIn("玄纹剑", self.project.production_settings.get("translation_glossary", {}))

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_include_all_missing_backfills_current_project_only(self, translate):
        translate.return_value = [
            translated("Lin Mo"), translated("Old Archive Room"), translated("Xuanwen Sword"),
        ]

        response = self.client.post(
            self.url, {"include_all_missing": True}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["summary"]["generated"], 3)
        self.assertEqual(set(translate.call_args.args[0]), {"林默", "旧档案室", "玄纹剑"})

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_optional_visual_translation_shares_name_batch_and_stays_review_gated(self, translate):
        translate.return_value = [
            translated("Xuanwen Sword"),
            translated("A silver-gray blade with subtle dark patterns."),
        ]
        payload = self.selectors(self.prop)
        payload["ensure_visual_identity_translation"] = True

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(translate.call_count, 1)
        self.assertEqual(translate.call_args.args[0], [
            "玄纹剑", "银灰剑身带有暗纹",
        ])
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.visual_identity, "银灰剑身带有暗纹")
        visual = PromptFieldTranslation.objects.get(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.visual_identity",
        )
        self.assertEqual(visual.status, PromptFieldTranslation.Status.DRAFT_TRANSLATION)
        self.assertEqual(
            visual.translated_text,
            "A silver-gray blade with subtle dark patterns.",
        )
        self.assertTrue(visual.runtime_metadata["auto_visual_identity_candidate"])
        self.assertIn("inference_context_hash", visual.runtime_metadata)
        self.assertNotEqual(
            visual.runtime_metadata["inference_context_hash"],
            visual.runtime_metadata["translation_context_hash"],
        )
        self.assertEqual(response.data["visual_summary"], {
            "requested": 1, "generated": 1, "reused": 0,
            "not_required": 0, "conflict": 0, "failed": 0,
        })
        self.assertTrue(response.data["visual_translations"][0]["context_current"])
        listed = self.client.get(reverse("project-translations", args=[self.project.pk]))
        listed_visual = next(
            row for row in listed.data if row["id"] == str(visual.pk)
        )
        self.assertTrue(listed_visual["context_current"])

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_visual_references_all_same_batch_missing_names_via_canonical_tokens(self, translate):
        self.prop.visual_identity = "玄纹剑由林默随身携带，银灰剑身带有暗纹"
        self.prop.save(update_fields=("visual_identity", "updated_at"))

        def translate_batch(texts, **kwargs):
            self.assertEqual(texts[:2], ["林默", "玄纹剑"])
            prop_source = texts[3]
            self.assertNotIn("林默", prop_source)
            self.assertNotIn("玄纹剑", prop_source)
            self.assertIn("<screen_text data-autoprompt-canonical=", prop_source)
            context = __import__("json").loads(kwargs["contexts"][3])
            tokens = {
                item["source"]: item["token"]
                for item in context["canonical_identity_tokens"]
            }
            self.assertEqual(set(tokens), {"林默", "玄纹剑"})
            return [
                translated("Lin Mo"),
                translated("Xuanwen Sword"),
                translated("A woman in her early thirties with short dark hair."),
                translated(
                    f"{tokens['玄纹剑']} is carried by "
                    f"{tokens['林默']}, with a silver-gray blade and subtle dark patterns."
                ),
            ]

        translate.side_effect = translate_batch
        payload = self.selectors(self.character, self.prop)
        payload["ensure_visual_identity_translation"] = True

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        visual = PromptFieldTranslation.objects.get(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.visual_identity",
        )
        self.assertEqual(
            visual.translated_text,
            "Xuanwen Sword is carried by Lin Mo, with a silver-gray blade and subtle dark patterns.",
        )
        self.assertEqual(
            visual.runtime_metadata["canonical_identity_placeholder_count"], 2,
        )
        listed = self.client.get(reverse("project-translations", args=[self.project.pk]))
        listed_visual = next(
            row for row in listed.data if row["id"] == str(visual.pk)
        )
        self.assertTrue(listed_visual["context_current"])

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_current_draft_and_confirmed_visual_candidates_are_reused(self, translate):
        self.project.production_settings = {
            "translation_glossary": {"林默": "Lin Mo", "玄纹剑": "Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        for entity, scope_type, field_key, status, candidate in (
            (
                self.character, "character", "character.visual_identity",
                PromptFieldTranslation.Status.CONFIRMED,
                "A woman in her early thirties with short dark hair.",
            ),
            (
                self.prop, "prop", "prop.visual_identity",
                PromptFieldTranslation.Status.DRAFT_TRANSLATION,
                "A silver-gray blade with subtle dark patterns.",
            ),
        ):
            PromptFieldTranslation.objects.create(
                project=self.project, scope_type=scope_type, scope_id=entity.pk,
                field_key=field_key, source_text=entity.visual_identity,
                source_hash=source_text_hash(entity.visual_identity),
                translated_text=candidate, status=status,
                runtime_metadata=self.context_metadata(entity, scope_type, field_key),
            )
        payload = self.selectors(self.character, self.prop)
        payload["ensure_visual_identity_translation"] = True

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        translate.assert_not_called()
        self.assertEqual(response.data["visual_summary"]["reused"], 2)
        self.assertTrue(all(
            row["context_current"] for row in response.data["visual_translations"]
        ))

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_visual_source_change_creates_new_draft_revision(self, translate):
        self.project.production_settings = {
            "translation_glossary": {"玄纹剑": "Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        old_source = self.prop.visual_identity
        old = PromptFieldTranslation.objects.create(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.visual_identity", source_text=old_source,
            source_hash=source_text_hash(old_source),
            translated_text="A silver-gray blade.",
            status=PromptFieldTranslation.Status.DRAFT_TRANSLATION,
            runtime_metadata=self.context_metadata(
                self.prop, "prop", "prop.visual_identity",
            ),
        )
        self.prop.visual_identity = "黑色剑鞘，银灰剑身带有暗纹"
        self.prop.save(update_fields=("visual_identity", "updated_at"))
        translate.return_value = [translated(
            "A black scabbard and a silver-gray blade with subtle dark patterns.",
        )]
        payload = self.selectors(self.prop)
        payload["ensure_visual_identity_translation"] = True

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        old.refresh_from_db()
        self.assertEqual(old.revision, 2)
        self.assertEqual(old.source_text, self.prop.visual_identity)
        self.assertEqual(old.revision_history[0]["source_text"], old_source)
        self.assertEqual(old.status, PromptFieldTranslation.Status.DRAFT_TRANSLATION)
        self.assertTrue(response.data["visual_translations"][0]["context_current"])

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_unrelated_visual_context_change_reuses_candidate(self, translate):
        self.project.production_settings = {
            "translation_glossary": {"玄纹剑": "Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        entry = PromptFieldTranslation.objects.create(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.visual_identity",
            source_text=self.prop.visual_identity,
            source_hash=source_text_hash(self.prop.visual_identity),
            translated_text="A silver-gray blade.",
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata=self.context_metadata(
                self.prop, "prop", "prop.visual_identity",
            ),
        )
        self.project.production_settings["translation_glossary"]["听雨客栈"] = "Listening Rain Inn"
        self.project.save(update_fields=("production_settings", "updated_at"))
        payload = self.selectors(self.prop)
        payload["ensure_visual_identity_translation"] = True

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        entry.refresh_from_db()
        translate.assert_not_called()
        self.assertEqual(entry.revision, 1)
        self.assertEqual(entry.status, PromptFieldTranslation.Status.CONFIRMED)
        self.assertEqual(response.data["visual_summary"]["reused"], 1)
        self.assertTrue(response.data["visual_translations"][0]["context_current"])

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_relevant_visual_mapping_change_regenerates_candidate(self, translate):
        self.prop.visual_identity = "玄纹剑的银灰剑身带有暗纹"
        self.prop.save(update_fields=("visual_identity", "updated_at"))
        self.project.production_settings = {
            "translation_glossary": {"玄纹剑": "Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        entry = PromptFieldTranslation.objects.create(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.visual_identity",
            source_text=self.prop.visual_identity,
            source_hash=source_text_hash(self.prop.visual_identity),
            translated_text="The Xuanwen Sword has a silver-gray blade with subtle dark patterns.",
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata=self.context_metadata(
                self.prop, "prop", "prop.visual_identity",
            ),
        )
        self.project.production_settings["translation_glossary"]["玄纹剑"] = (
            "Mystic Pattern Sword"
        )
        self.project.save(update_fields=("production_settings", "updated_at"))

        def translate_visual(texts, **kwargs):
            context = __import__("json").loads(kwargs["contexts"][0])
            token = context["canonical_identity_tokens"][0]["token"]
            return [translated(
                f"The {token} has a silver-gray blade with subtle dark patterns."
            )]

        translate.side_effect = translate_visual
        payload = self.selectors(self.prop)
        payload["ensure_visual_identity_translation"] = True
        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        entry.refresh_from_db()
        self.assertEqual(entry.revision, 2)
        self.assertEqual(entry.status, PromptFieldTranslation.Status.DRAFT_TRANSLATION)
        self.assertIn("Mystic Pattern Sword", entry.translated_text)
        self.assertTrue(response.data["visual_translations"][0]["context_current"])

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_visual_partial_failure_does_not_rollback_canonical_or_bible(self, translate):
        translate.return_value = [
            translated("Xuanwen Sword"),
            PromptTranslationError(
                "候选仍含中文", code="residual_cjk_after_repair",
            ),
        ]
        payload = self.selectors(self.prop)
        payload["ensure_visual_identity_translation"] = True

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.project.refresh_from_db()
        self.prop.refresh_from_db()
        self.assertEqual(
            self.project.production_settings["translation_glossary"]["玄纹剑"],
            "Xuanwen Sword",
        )
        self.assertEqual(self.prop.visual_identity, "银灰剑身带有暗纹")
        self.assertEqual(response.data["summary"]["generated"], 1)
        self.assertEqual(response.data["visual_summary"]["failed"], 1)
        failed = PromptFieldTranslation.objects.get(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.visual_identity",
        )
        self.assertEqual(failed.status, PromptFieldTranslation.Status.FAILED)

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_visual_changed_during_inference_returns_conflict(self, translate):
        self.project.production_settings = {
            "translation_glossary": {"玄纹剑": "Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))

        def edit_while_model_runs(*_args, **_kwargs):
            PropBible.objects.filter(pk=self.prop.pk).update(
                visual_identity="用户并发修改后的视觉锚点",
            )
            return [translated("A silver-gray blade with subtle dark patterns.")]

        translate.side_effect = edit_while_model_runs
        payload = self.selectors(self.prop)
        payload["ensure_visual_identity_translation"] = True

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["visual_summary"]["conflict"], 1)
        self.assertFalse(PromptFieldTranslation.objects.filter(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.visual_identity",
        ).exists())

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_visual_translation_uses_full_untruncated_source(self, translate):
        self.project.production_settings = {
            "translation_glossary": {"玄纹剑": "Xuanwen Sword"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        self.prop.visual_identity = ("银色暗纹" * 220) + "完整剑身末端"
        self.prop.save(update_fields=("visual_identity", "updated_at"))
        translate.return_value = [translated(
            "A complete silver blade covered in repeating dark patterns.",
        )]
        payload = self.selectors(self.prop)
        payload["ensure_visual_identity_translation"] = True

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(translate.call_args.args[0][0], self.prop.visual_identity)
        entry = PromptFieldTranslation.objects.get(
            project=self.project, scope_type="prop", scope_id=self.prop.pk,
            field_key="prop.visual_identity",
        )
        self.assertEqual(entry.source_text, self.prop.visual_identity)
        self.assertEqual(entry.source_hash, source_text_hash(self.prop.visual_identity))

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_full_model_context_is_not_truncated_and_advisories_are_audited(self, translate):
        long_description = ("完整描述" * 220) + "描述末端"
        long_visual = "玄纹剑" + ("银色暗纹" * 220) + "视觉末端"
        long_source = "长术语" + ("甲" * 170)
        long_value = "LongCanonicalTerm" + ("A" * 170)
        glossary = {
            f"术语{index}": f"Canonical Term {index}"
            for index in range(80)
        }
        glossary[long_source] = long_value
        self.project.production_settings = {"translation_glossary": glossary}
        self.project.save(update_fields=("production_settings", "updated_at"))
        self.prop.description = long_description
        self.prop.visual_identity = long_visual
        self.prop.save(update_fields=(
            "description", "visual_identity", "updated_at",
        ))

        def translate_both(_texts, **kwargs):
            visual_context = json.loads(kwargs["contexts"][1])
            token = visual_context["canonical_identity_tokens"][0]["token"]
            return [
                translated("Xuanwen Sword"),
                translated(f"The {token} has a complete silver patterned blade."),
            ]

        translate.side_effect = translate_both
        payload = self.selectors(self.prop)
        payload["ensure_visual_identity_translation"] = True

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        call = translate.call_args
        proper_context = json.loads(call.kwargs["contexts"][0])
        visual_context = json.loads(call.kwargs["contexts"][1])
        self.assertEqual(proper_context["entity"]["description"], long_description)
        self.assertEqual(proper_context["entity"]["visual_identity"], long_visual)
        self.assertEqual(
            proper_context["existing_canonical_glossary"][long_source],
            long_value,
        )
        self.assertEqual(len(proper_context["existing_canonical_glossary"]), 81)
        self.assertEqual(
            visual_context["entity_description_reference"],
            long_description,
        )
        self.assertEqual(
            visual_context["canonical_glossary_at_inference"][long_source],
            long_value,
        )
        self.assertIn("视觉末端", call.args[0][1])

        advisory_codes = {
            item["code"]
            for item in response.data["runtime"]["canonical_context_advisories"]
        }
        self.assertTrue({
            "proper_name_glossary_entries_above_advisory",
            "visual_identity_glossary_entries_above_advisory",
            "description_characters_above_advisory",
            "visual_identity_characters_above_advisory",
            "glossary_term_characters_above_advisory",
        } <= advisory_codes)
        self.assertTrue(
            response.data["runtime"]["canonical_policy_resolution_sha256"],
        )
        rows = PromptFieldTranslation.objects.filter(
            project=self.project,
            scope_type="prop",
            scope_id=self.prop.pk,
        )
        self.assertEqual(rows.count(), 2)
        for row in rows:
            self.assertEqual(
                row.runtime_metadata["canonical_context_policy"]["schema_version"],
                1,
            )
            self.assertEqual(
                {
                    item["code"]
                    for item in row.runtime_metadata["canonical_context_advisories"]
                },
                advisory_codes,
            )

    def test_foreign_or_missing_entity_returns_non_leaking_conflict(self):
        _, foreign_project, foreign_character, *_ = build_story("canonical-foreign")

        response = self.client.post(
            self.url,
            {"entries": [{"scope_type": "character", "scope_id": str(foreign_character.pk)}]},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["entries"][0]["status"], "conflict")
        self.assertEqual(response.data["entries"][0]["error_code"], "entity_not_found")
        self.assertNotIn(foreign_project.title, str(response.data))

    def test_request_requires_entries_or_explicit_backfill(self):
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400)

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_ordinary_entity_and_runtime_capacities_warn_without_blocking(self, translate):
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=self.project,
            values={
                "production.canonical_name_policy": {
                    "schema_version": 1,
                    "max_characters": 80,
                    "max_words": 8,
                    "max_entities_per_request": 1,
                    "max_entities_with_visual_identity_translation": 1,
                    "max_runtime_items_per_request": 1,
                },
            },
            created_by=self.user,
        )
        publish_config_revision(revision, published_by=self.user)
        translate.return_value = [translated("Lin Mo"), translated("Xuanwen Sword")]

        response = self.client.post(
            self.url,
            self.selectors(self.character, self.prop),
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(translate.call_count, 1)
        self.assertEqual(response.data["summary"]["generated"], 2)
        advisory_codes = {
            item["code"]
            for item in response.data["runtime"]["canonical_context_advisories"]
        }
        self.assertIn("canonical_entity_count_above_advisory", advisory_codes)
        self.assertIn("canonical_runtime_item_count_above_advisory", advisory_codes)

    @patch("studio.services.canonical_glossary.translate_texts_once")
    def test_visual_backfill_rejects_runtime_batch_over_safe_limit(self, translate):
        PropBible.objects.bulk_create([
            PropBible(
                project=self.project, slug=f"capacity-prop-{index}",
                name=f"容量道具{index}", visual_identity=f"银色道具表面{index}",
            )
            for index in range(48)
        ])

        response = self.client.post(self.url, {
            "include_all_missing": True,
            "ensure_visual_identity_translation": True,
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("100", str(response.data))
        translate.assert_not_called()


class CanonicalEnglishValidationTests(TestCase):
    def test_accepts_short_ascii_proper_names(self):
        self.assertEqual(validate_canonical_english("  Listening  Rain Inn "), "Listening Rain Inn")
        self.assertEqual(validate_canonical_english("Shen Yan"), "Shen Yan")

    def test_rejects_mixed_language_prose_and_technical_ids(self):
        for value in ("Xuanwen 剑", "The sword used by Ling Shuang.", "prop:xuanwen", "S1"):
            with self.subTest(value=value), self.assertRaises(TranslationValidationError):
                validate_canonical_english(value)
