from copy import deepcopy
from studio.tests.primary_setting_fixtures import create_primary_setting
import json
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, IntegrityError, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import Asset, AssetGenerationJob, CharacterBible, SceneBible, PropBible, GenerationAttempt, ProjectTranslationRequest, ProjectTranslationSource, PromptFieldTranslation, SeriesProject
from studio.services.asset_generation import claim_next_text_asset_job, process_text_asset_job, request_asset_generation_cancel, retry_asset_generation_job
from studio.services.project_translation_requests import create_project_translation_request, read_project_translation_candidate, adopt_project_translation_candidate
from studio.services.prompt_translation import source_text_hash, build_translation_context
from studio.services.provider_registry import ProviderDriverError
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests import test_execution_normalization_text as normalization_fixtures
from studio.tests.test_execution_asset_requests import freeze, isolated_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ProjectTranslationRequestTests(TestCase):
    setUpTestData = classmethod(normalization_fixtures.ExecutionNormalizationTextTests.setUpTestData.__func__)
    _version = normalization_fixtures.ExecutionNormalizationTextTests._version

    def setUp(self):
        normalization_fixtures.ExecutionNormalizationTextTests.setUp(self)
        self.project = SeriesProject.objects.create(owner=self.actor, title="Source project", continuity_summary="她说 <d>[Chinese] 别动。</d>。", **PROJECT_CONFIG)
        self.entry = {"scope_type": "project", "scope_id": str(self.project.pk), "field_key": "project.continuity_summary",
            "source_text": self.project.continuity_summary, "expected_entry_id": None, "expected_revision": 0}
        self.request = deepcopy(self.fixture)
        self.request["inputs"] = {}
        self.enterContext(patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission", side_effect=isolated_observations))

    def create(self, **changes):
        return create_project_translation_request(**{"project_id": self.project.pk, "entries": [self.entry],
            "source_lang": "Chinese", "target_lang": "English", "actor": self.actor,
            "execution_instance_version_id": self.version.pk, "expected_definition_sha256": self.definition.sha256,
            "request": self.request, "field_values": [], "allow_external_calls": True,
            "client_idempotency_key": "project-source", **changes})

    def complete(self, saved=None, *, expected_status="succeeded"):
        if saved is None:
            saved, created = self.create()
            self.assertTrue(created)
        job = saved.job
        prep = freeze(job, self.actor)
        job.status = "queued"
        job.save(update_fields=["status"])
        transport = prep.contract.prepared_request["transport"]
        payload = json.loads(transport["payload"]["messages"][-1]["content"])
        token = payload["items"][0]["protected_tokens"][0]
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps({"schema_version": 1, "items": [{"request_id": "0", "translation": f"She says {token}."}]}),
            "request_id": "isolated-translation"}
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            job = process_text_asset_job(claim_next_text_asset_job("project-translation-worker"))
        self.assertEqual(job.status, expected_status, job.error_message)
        self.assertEqual(peer.invoke.call_count, 1)
        return saved

    def test_retired_shot_sources_cannot_allocate_a_job_or_call_a_model(self):
        from studio.models import Episode, Shot
        from studio.services.prompt_translation import TranslationValidationError
        episode = Episode.objects.create(project=self.project, number=1)
        shot = Shot.objects.create(episode=episode, number=1, duration_ms=1000,
            media_type="image", include_audio=False)
        before = (AssetGenerationJob.objects.count(), ProjectTranslationRequest.objects.count(),
            ProjectTranslationSource.objects.count(), GenerationAttempt.objects.count())
        with patch("studio.services.provider_registry.OpenAICompatibleDriver.invoke") as invoke:
            for key in ("narrative", "detailed_description", "camera", "action",
                    "sound_effects", "music", "continuity_notes", "negative_prompt"):
                with self.subTest(key=key), self.assertRaises(TranslationValidationError):
                    self.create(entries=[{**self.entry, "scope_type": "shot", "scope_id": str(shot.pk),
                        "field_key": f"shot.{key}", "source_text": "Historical value"}])
            invoke.assert_not_called()
        self.assertEqual(before, (AssetGenerationJob.objects.count(), ProjectTranslationRequest.objects.count(),
            ProjectTranslationSource.objects.count(), GenerationAttempt.objects.count()))

    def test_creation_freezes_real_sources_without_model_call_or_fabricated_asset(self):
        counts = (Asset.objects.count(), GenerationAttempt.objects.count(), PromptFieldTranslation.objects.count())
        saved, created = self.create()
        self.assertTrue(created)
        self.assertEqual((saved.job.job_type, saved.job.media_type, saved.job.status), ("prompt", "text", "preparing"))
        source = saved.sources.get()
        self.assertEqual(source.source_text, self.entry["source_text"])
        self.assertEqual(source.source_sha256, source_text_hash(source.source_text))
        self.assertEqual(source.expected_revision, 0)
        self.assertIsNone(source.expected_entry_id)
        self.assertEqual(counts, (Asset.objects.count(), GenerationAttempt.objects.count(), PromptFieldTranslation.objects.count()))
        self.assertEqual(saved.job.execution_preparations.count(), 1)

    def test_project_and_bible_scopes_are_exact_and_do_not_create_review_records(self):
        for scope, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible)):
            obj = model.objects.create(project=self.project, slug=scope, name=f"Explicit {scope}")
            create_primary_setting(obj, "明确的视觉事实")
            entry = {**self.entry, "scope_type": scope, "scope_id": str(obj.pk), "field_key": f"{scope}.primary_setting.identity_anchor", "source_text": "明确的视觉事实"}
            saved, _ = self.create(entries=[entry], client_idempotency_key=f"scope-{scope}")
            source = saved.sources.get()
            self.assertEqual((source.scope_type, source.scope_id, source.field_key), (scope, obj.pk, f"{scope}.primary_setting.identity_anchor"))
            self.assertEqual(source.source_text, "明确的视觉事实")
            with self.assertRaises(ValidationError):
                self.create(entries=[{**entry, "scope_id": str(self.project.pk)}], client_idempotency_key=f"bad-{scope}")
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_bad_multi_item_response_fails_original_job_without_partial_review_writes(self):
        scene = SceneBible.objects.create(project=self.project, slug="two", name="Explicit scene")
        create_primary_setting(scene, "空房间")
        saved, _ = self.create(entries=[self.entry, {**self.entry, "scope_type": "scene", "scope_id": str(scene.pk),
            "field_key": "scene.primary_setting.identity_anchor", "source_text": "空房间"}])
        freeze(saved.job, self.actor)
        AssetGenerationJob.objects.filter(pk=saved.job_id).update(status="queued")
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps({"schema_version": 1, "items": [{"request_id": "0", "translation": "Incomplete batch"}]}), "request_id": "isolated-bad-batch"}
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            job = process_text_asset_job(claim_next_text_asset_job("bad-translation-batch"))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "asset_text_response_invalid")
        self.assertEqual(job.remote_status, "response_rejected")
        self.assertEqual(job.provider_request_id, "isolated-bad-batch")
        self.assertEqual(job.result_text, "")
        self.assertIsNone(claim_next_text_asset_job("must-not-automatically-retry"))
        self.assertEqual(peer.invoke.call_count, 1)
        self.assertFalse(PromptFieldTranslation.objects.exists())
        self.assertEqual(saved.sources.count(), 2)
        for source in saved.sources.all():
            with self.assertRaises(ValidationError):
                read_project_translation_candidate(request_id=saved.pk, source_id=source.pk, actor=self.actor)

    def test_name_translation_freezes_entity_facts_and_rejects_adoption_after_description_change(self):
        prop = PropBible.objects.create(project=self.project, slug="named-source", name="玄纹剑",
            description="传家宝剑，不是人物")
        with CaptureQueriesContext(connection) as queries:
            build_translation_context(self.project, scope_type="prop", scope_id=prop.pk, field_key="prop.name", source_text=prop.name)
        detail_queries = [row["sql"] for row in queries if 'FROM "studio_propbible"' in row["sql"] and '"description"' in row["sql"].split(" FROM ")[0]]
        self.assertEqual(len(detail_queries), 1)
        self.assertNotIn('"created_at"', detail_queries[0].split(" FROM ")[0])
        self.assertNotIn('"*"', detail_queries[0])
        self.assertNotIn('"visual_identity"', detail_queries[0])
        self.assertIn("SEARCH", PropBible.objects.filter(project=self.project, pk=prop.pk).values("name", "description").explain().upper())
        saved, _ = self.create(entries=[{**self.entry, "scope_type": "prop", "scope_id": str(prop.pk),
            "field_key": "prop.name", "source_text": prop.name}])
        prep = freeze(saved.job, self.actor)
        wire = json.loads(prep.contract.prepared_request["transport"]["payload"]["messages"][-1]["content"])
        context = json.loads(wire["items"][0]["context"])
        self.assertEqual(context["entity"], {"source_name": prop.name, "description": prop.description, "primary_setting": None})
        self.assertEqual(context["canonical_identity_mappings"], {})
        self.assertEqual(wire["items"][0]["protected_tokens"], [])
        self.assertNotIn("rules", context)
        AssetGenerationJob.objects.filter(pk=saved.job_id).update(status="queued")
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps({"schema_version": 1, "items": [{"request_id": "0", "translation": "Xuanwen Sword"}]}),
            "request_id": "isolated-name-source"}
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            job = process_text_asset_job(claim_next_text_asset_job("name-source-worker"))
        self.assertEqual(job.status, "succeeded", job.error_message)
        source = saved.sources.get()
        candidate = lambda: read_project_translation_candidate(request_id=saved.pk, source_id=source.pk, actor=self.actor)
        self.assertTrue(candidate()["current"])
        PropBible.objects.create(project=self.project, slug="unrelated", name="无关道具", description="不影响这个名称")
        self.assertTrue(candidate()["current"])
        prop.description = "另一种道具设定"
        prop.save(update_fields=["description"])
        self.assertFalse(candidate()["current"])
        with self.assertRaises(ValidationError):
            adopt_project_translation_candidate(request_id=saved.pk, source_id=source.pk, actor=self.actor)
        self.assertEqual(peer.invoke.call_count, 1)
        self.project.refresh_from_db()
        self.assertNotIn(prop.name, self.project.production_settings.get("translation_glossary", {}))
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_name_candidate_uses_original_worker_then_explicit_glossary_confirmation(self):
        prop = PropBible.objects.create(project=self.project, slug="confirmed-source", name="玄纹剑",
            description="传家宝剑")
        saved, _ = self.create(entries=[{**self.entry, "scope_type": "prop", "scope_id": str(prop.pk),
            "field_key": "prop.name", "source_text": prop.name}])
        freeze(saved.job, self.actor)
        AssetGenerationJob.objects.filter(pk=saved.job_id).update(status="queued")
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps({"schema_version": 1, "items": [
            {"request_id": "0", "translation": "Xuanwen Sword"}]}), "request_id": "isolated-confirmed-name"}
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            job = process_text_asset_job(claim_next_text_asset_job("name-confirmation-worker"))
        self.assertEqual(job.status, "succeeded", job.error_message)
        source, created = adopt_project_translation_candidate(request_id=saved.pk,
            source_id=saved.sources.get().pk, actor=self.actor)
        self.assertTrue(created)
        entry = source.adopted_entry
        self.assertEqual(entry.status, "draft_translation")
        self.project.refresh_from_db()
        self.assertNotIn(prop.name, self.project.production_settings.get("translation_glossary", {}))
        client = APIClient()
        client.force_authenticate(self.actor)
        result = client.post(f"/api/v1/translations/{entry.pk}/confirm-canonical/", {
            "expected_revision": entry.revision, "expected_canonical": None,
            "expected_project_updated_at": self.project.updated_at.isoformat(), "translation": entry.translated_text,
        }, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data["entry"]["context_current"], result.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings["translation_glossary"][prop.name], "Xuanwen Sword")
        entry.refresh_from_db()
        self.assertEqual(entry.runtime_metadata["asset_generation_job_id"], str(saved.job_id))
        self.assertEqual(entry.runtime_metadata["translation_source_id"], str(source.pk))
        self.assertIn("configuration_sha256", entry.runtime_metadata["canonical_adoption"])
        self.assertEqual(peer.invoke.call_count, 1)
        self.assertEqual(AssetGenerationJob.objects.count(), 1)
        self.assertIsNone(job.output_asset_id)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_three_entity_names_freeze_exact_primary_and_setting_changes_block_adoption(self):
        from studio.services.bible_variants import create_variant, revise_variant, setting_payload
        from studio.services.execution_preparations import read_execution_preparation_detail

        for kind, model, translated in (("character", CharacterBible, "Traveler"),
                ("scene", SceneBible, "Quiet Room"), ("prop", PropBible, "Bronze Key")):
            with self.subTest(kind=kind):
                entity = model.objects.create(project=self.project, slug=f"primary-{kind}", name=f"名称-{kind}",
                    description="已保存的实体说明")
                primary = create_variant(actor=self.actor, project_id=self.project.pk, entity_kind=kind,
                    entity_id=entity.pk, appearance_role="primary", primary_setting_revision_id=None,
                    primary_setting_sha256="", value={"name": entity.name,
                        "reference_name": f"主图-{kind}", "identity_anchor": f"新共同身份-{kind}",
                        "appearance_anchor": f"新基础外观-{kind}", "basis_asset_id": None, "basis_asset_sha256": ""})
                setting = primary.revisions.get(revision=1)
                saved, _ = self.create(entries=[{**self.entry, "scope_type": kind, "scope_id": str(entity.pk),
                    "field_key": f"{kind}.name", "source_text": entity.name}], client_idempotency_key=f"name-primary-{kind}")
                preparation = freeze(saved.job, self.actor)
                frozen = deepcopy(preparation.contract.prepared_request)
                wire = json.loads(frozen["transport"]["payload"]["messages"][-1]["content"])
                context_text = wire["items"][0]["context"]
                context = json.loads(context_text)["entity"]
                self.assertEqual(context["primary_setting"], {"appearance_id": str(primary.pk),
                    "setting_revision_id": str(setting.pk), "revision": 1, "content_sha256": setting.content_sha256,
                    "identity_anchor": setting.identity_anchor, "appearance_anchor": setting.appearance_anchor})
                self.assertNotIn("visual_identity", context)
                AssetGenerationJob.objects.filter(pk=saved.job_id).update(status="queued")
                peer = Mock()
                peer.invoke.return_value = {"text": json.dumps({"schema_version": 1, "items": [
                    {"request_id": "0", "translation": translated}]}), "request_id": f"isolated-primary-name-{kind}"}
                with patch("studio.services.asset_generation.driver_for", return_value=peer):
                    job = process_text_asset_job(claim_next_text_asset_job(f"primary-name-{kind}"))
                self.assertEqual(job.status, "succeeded", job.error_message)
                source = saved.sources.get()
                candidate = lambda: read_project_translation_candidate(request_id=saved.pk, source_id=source.pk, actor=self.actor)
                self.assertTrue(candidate()["current"])
                revise_variant(actor=self.actor, variant=primary, expected_revision=1,
                    value={**setting_payload(setting), "appearance_anchor": "本次新外观"})
                result = candidate()
                self.assertFalse(result["current"])
                self.assertEqual(result["text"], translated)
                with self.assertRaises(ValidationError):
                    adopt_project_translation_candidate(request_id=saved.pk, source_id=source.pk, actor=self.actor)
                preparation.refresh_from_db()
                self.assertEqual(read_execution_preparation_detail(preparation).prepared_request, frozen)
                self.assertEqual(primary.revisions.get(revision=1).appearance_anchor, setting.appearance_anchor)
                self.assertEqual(peer.invoke.call_count, 1)
        self.assertFalse(PromptFieldTranslation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings.get("translation_glossary", {}), {})

    def test_replay_does_not_refresh_source_or_recreate_preparation(self):
        saved, _ = self.create()
        self.project.continuity_summary = "后续已修改的原文"
        self.project.save(update_fields=["continuity_summary"])
        replay, created = self.create()
        self.assertFalse(created)
        self.assertEqual(replay.pk, saved.pk)
        self.assertEqual(replay.sources.get().source_text, self.entry["source_text"])
        with self.assertRaises(ValidationError):
            self.create(entries=[{**self.entry, "source_text": self.project.continuity_summary}])
        self.assertEqual(ProjectTranslationRequest.objects.count(), 1)
        self.assertEqual(AssetGenerationJob.objects.count(), 1)

    def test_unknown_submission_stays_uncertain_without_creating_another_call(self):
        saved, _ = self.create()
        freeze(saved.job, self.actor)
        AssetGenerationJob.objects.filter(pk=saved.job_id).update(status="queued")
        peer = Mock()
        peer.invoke.side_effect = ProviderDriverError("response lost", submission_uncertain=True)
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            job = process_text_asset_job(claim_next_text_asset_job("unknown-response"))
        self.assertEqual((job.status, job.error_code), ("uncertain", "uncertain_remote_response"))
        self.assertEqual(peer.invoke.call_count, 1)
        self.assertIsNone(claim_next_text_asset_job("no-automatic-resubmission"))
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_post_response_storage_failure_is_not_misreported_as_model_rejection(self):
        with patch("studio.services.asset_generation._finish_text_asset_result", side_effect=ValidationError("isolated storage failure")):
            self.complete(expected_status="uncertain")
        job = AssetGenerationJob.objects.get()
        self.assertEqual(job.status, "uncertain")
        self.assertNotEqual(job.error_code, "asset_text_response_invalid")
        self.assertEqual(job.provider_request_id, "isolated-translation")
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_late_invalid_response_cannot_overwrite_new_claim(self):
        saved, _ = self.create()
        freeze(saved.job, self.actor)
        AssetGenerationJob.objects.filter(pk=saved.job_id).update(status="queued")
        new_claim = uuid4()
        def late_response(*args):
            AssetGenerationJob.objects.filter(pk=saved.job_id).update(claim_token=new_claim, worker_id="new-owner")
            return {"text": "not a valid batch", "request_id": "late-response"}
        peer = Mock()
        peer.invoke.side_effect = late_response
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            job = process_text_asset_job(claim_next_text_asset_job("old-owner"))
        self.assertEqual(job.claim_token, new_claim)
        self.assertEqual(job.status, "submitting")
        self.assertEqual(job.error_code, "")
        self.assertEqual(job.provider_request_id, "")
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_current_source_revision_language_and_injected_items_rejected_atomically(self):
        for changes in ({"entries": [{**self.entry, "source_text": "wrong"}]}, {"target_lang": ""},
                {"entries": [{**self.entry, "expected_revision": True}]}, {"entries": [self.entry, self.entry]},
                {"request": {**self.request, "inputs": self.fixture["inputs"]}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.create(**changes)
        self.assertFalse(ProjectTranslationRequest.objects.exists())
        self.assertFalse(AssetGenerationJob.objects.exists())

    def test_existing_manual_revision_is_required_and_not_overwritten_by_submit(self):
        entry = PromptFieldTranslation.objects.create(project=self.project, scope_type="project", scope_id=self.project.pk,
            field_key=self.entry["field_key"], source_text=self.entry["source_text"], source_hash=source_text_hash(self.entry["source_text"]),
            translated_text="Previous manually edited candidate", revision=3)
        with self.assertRaises(ValidationError):
            self.create()
        saved, _ = self.create(entries=[{**self.entry, "expected_entry_id": str(entry.pk), "expected_revision": 3}])
        self.assertEqual(saved.sources.get().expected_entry_id, entry.pk)
        entry.refresh_from_db()
        self.assertEqual(entry.translated_text, "Previous manually edited candidate")
        self.assertEqual(entry.revision, 3)

    def test_original_worker_candidate_restores_literals_but_does_not_adopt(self):
        saved = self.complete()
        value = read_project_translation_candidate(request_id=saved.pk, source_id=saved.sources.get().pk, actor=self.actor)
        self.assertEqual(value["text"], "She says <d>[Chinese] 别动。</d>.")
        self.assertTrue(value["current"], value["reason"])
        self.assertFalse(PromptFieldTranslation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertIsNone(saved.job.output_asset_id)
        self.project.continuity_summary = "另一个原文"
        self.project.save(update_fields=["continuity_summary"])
        value = read_project_translation_candidate(request_id=saved.pk, source_id=saved.sources.get().pk, actor=self.actor)
        self.assertFalse(value["current"])
        self.assertIn("原文", value["reason"])

    def test_cannot_read_another_project_or_modified_candidate(self):
        other = get_user_model().objects.create_user(username="translation-other")
        with self.assertRaises(PermissionDenied):
            self.create(actor=other)
        saved = self.complete()
        with self.assertRaises(PermissionDenied):
            read_project_translation_candidate(request_id=saved.pk, source_id=saved.sources.get().pk, actor=other)
        AssetGenerationJob.objects.filter(pk=saved.job_id).update(result_text="different")
        with self.assertRaisesMessage(ValidationError, "摘要不一致"):
            read_project_translation_candidate(request_id=saved.pk, source_id=saved.sources.get().pk, actor=self.actor)

    def test_source_uniqueness_and_adoption_facts_have_database_constraints(self):
        saved, _ = self.create()
        source = saved.sources.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectTranslationSource.objects.filter(pk=source.pk).update(expected_revision=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectTranslationSource.objects.filter(pk=source.pk).update(adopted_revision=1)

    def test_adoption_is_review_candidate_only_and_receipt_replay_preserves_later_edit(self):
        saved = self.complete()
        source, created = adopt_project_translation_candidate(request_id=saved.pk, source_id=saved.sources.get().pk, actor=self.actor)
        self.assertTrue(created)
        entry = source.adopted_entry
        self.assertEqual(entry.status, "draft_translation")
        self.assertIsNone(entry.confirmed_at)
        self.assertEqual(entry.runtime_metadata["asset_generation_job_id"], str(saved.job_id))
        self.assertIsNone(entry.origin_generation_attempt_id)
        entry.translated_text = "Later manual candidate"
        entry.revision += 1
        entry.save()
        replay, created = adopt_project_translation_candidate(request_id=saved.pk, source_id=source.pk, actor=self.actor)
        self.assertFalse(created)
        self.assertEqual(replay.adopted_revision, 1)
        entry.refresh_from_db()
        self.assertEqual(entry.translated_text, "Later manual candidate")
        self.assertEqual(entry.revision, 2)

    def test_changed_source_or_new_manual_candidate_cannot_be_overwritten(self):
        saved = self.complete()
        entry = PromptFieldTranslation.objects.create(project=self.project, scope_type="project", scope_id=self.project.pk,
            field_key=self.entry["field_key"], source_text=self.entry["source_text"], source_hash=source_text_hash(self.entry["source_text"]),
            translated_text="Manual while model was running")
        with self.assertRaisesMessage(ValidationError, "人工候选已变更"):
            adopt_project_translation_candidate(request_id=saved.pk, source_id=saved.sources.get().pk, actor=self.actor)
        entry.refresh_from_db()
        self.assertEqual(entry.translated_text, "Manual while model was running")
        self.assertIsNone(saved.sources.get().adopted_at)

    def test_public_create_list_original_preparation_and_explicit_adoption(self):
        client = APIClient()
        client.force_authenticate(self.actor)
        path = f"/api/v1/projects/{self.project.pk}/translations/requests/"
        options = client.get(f"/api/v1/projects/{self.project.pk}/translations/options/")
        self.assertEqual(options.status_code, 200, options.data)
        self.assertEqual(options.data["source_lang"], "Chinese")
        self.assertEqual(options.data["target_lang"], "English")
        self.assertEqual(options.data["results"][0]["execution_instance_version_id"], str(self.version.pk))
        body = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "entries": [self.entry], "source_lang": "Chinese", "target_lang": "English", "request": self.request,
            "field_values": [], "allow_external_calls": True}
        first = client.post(path, body, format="json", HTTP_IDEMPOTENCY_KEY="api-project-source")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["status"], "preparing")
        repeat = client.post(path, body, format="json", HTTP_IDEMPOTENCY_KEY="api-project-source")
        self.assertEqual(repeat.status_code, 200, repeat.data)
        self.assertEqual(repeat.data, first.data)
        with CaptureQueriesContext(connection) as queries:
            page = client.get(path)
        self.assertEqual(page.status_code, 200, page.data)
        self.assertEqual(page.data["count"], 1)
        self.assertEqual(page.data["results"][0], first.data)
        order = ProjectTranslationRequest.objects.filter(project=self.project).order_by("-created_at", "-id").values("id")
        self.assertIn("idx_translation_request", order.explain())
        reads = [row["sql"].lower() for row in queries if row["sql"].lower().startswith("select")]
        self.assertFalse(any('"result_text"' in sql or '"literals"' in sql or '"master_script"' in sql for sql in reads))
        sources_path = f"{path}{first.data['id']}/sources/"
        sources = client.get(sources_path)
        self.assertEqual(sources.status_code, 200, sources.data)
        source = sources.data["results"][0]
        candidate_path = f"{sources_path}{source['id']}/"
        self.assertNotIn("source_text", source)
        source_query = ProjectTranslationSource.objects.filter(request_id=first.data["id"]).order_by("position").values("id")
        self.assertIn("INDEX", source_query.explain().upper())
        self.assertEqual(client.get(candidate_path).status_code, 400)
        saved = self.complete(ProjectTranslationRequest.objects.get(pk=first.data["id"]))
        candidate = client.get(candidate_path)
        self.assertEqual(candidate.status_code, 200, candidate.data)
        self.assertTrue(candidate.data["current"])
        self.assertEqual(client.post(candidate_path, {"confirmed": True}, format="json").status_code, 400)
        adopted = client.post(candidate_path, {}, format="json")
        self.assertEqual(adopted.status_code, 200, adopted.data)
        self.assertEqual(adopted.data["adopted_revision"], 1)
        self.assertEqual(PromptFieldTranslation.objects.get(pk=adopted.data["adopted_entry_id"]).status, "draft_translation")
        self.assertEqual(client.get(f"{path}not-a-uuid/sources/").status_code, 400)
        self.assertEqual(client.get(path, {"unknown": "x"}).status_code, 400)
        other = get_user_model().objects.create_user(username="api-translation-other")
        client.force_authenticate(other)
        self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(client.post(candidate_path, {}, format="json").status_code, 404)

    def test_original_retry_copies_frozen_source_without_refresh_or_duplicate_relation(self):
        saved, _ = self.create()
        original = saved.sources.get()
        request_asset_generation_cancel(saved.job)
        self.project.continuity_summary = "changed while cancelled"
        self.project.save(update_fields=["continuity_summary"])
        retry, created = retry_asset_generation_job(saved.job, client_idempotency_key="source-retry")
        self.assertTrue(created)
        source = retry.translation_request.sources.get()
        self.assertNotEqual(source.pk, original.pk)
        for field in ("source_text", "source_sha256", "context_sha256", "item_sha256", "literals", "expected_revision"):
            self.assertEqual(getattr(source, field), getattr(original, field))
        self.assertIsNone(source.adopted_at)
        replay, created = retry_asset_generation_job(saved.job, client_idempotency_key="source-retry")
        self.assertFalse(created)
        self.assertEqual(replay.pk, retry.pk)
        self.assertEqual(ProjectTranslationRequest.objects.count(), 2)

    def test_project_deletion_waits_for_actual_jobs_then_removes_source_relations(self):
        saved, _ = self.create()
        client = APIClient()
        client.force_authenticate(self.actor)
        path = f"/api/v1/projects/{self.project.pk}/"
        for state in ("preparing", "queued", "running", "uncertain", "cancel_requested"):
            AssetGenerationJob.objects.filter(pk=saved.job_id).update(status=state)
            result = client.delete(path)
            self.assertEqual(result.status_code, 409, result.data)
            self.assertTrue(SeriesProject.objects.filter(pk=self.project.pk).exists())
        AssetGenerationJob.objects.filter(pk=saved.job_id).update(status="preparing")
        self.complete(saved)
        adopt_project_translation_candidate(request_id=saved.pk, source_id=saved.sources.get().pk, actor=self.actor)
        deleted = client.delete(path)
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "data", None))
        self.assertFalse(ProjectTranslationSource.objects.exists())
        self.assertFalse(ProjectTranslationRequest.objects.exists())
        # Standalone Job/Preparation retain their original lifecycle. Deleting a
        # project does not delete a provider request or recreate an empty result.
        self.assertTrue(AssetGenerationJob.objects.filter(pk=saved.job_id).exists())

    def test_result_structure_and_source_position_are_not_silently_repaired(self):
        import hashlib
        saved = self.complete()
        source = saved.sources.get()
        original = json.loads(AssetGenerationJob.objects.get(pk=saved.job_id).result_text)
        for change in ({"schema_version": True}, {"items": [{"request_id": "9", "translation": "Other source"}]},
                {"items": [{"request_id": "0", "translation": "text", "unexpected": True}]}):
            text = json.dumps({**original, **change})
            encoded = text.encode("utf-8")
            AssetGenerationJob.objects.filter(pk=saved.job_id).update(result_text=text,
                result_text_size_bytes=len(encoded), result_text_sha256=hashlib.sha256(encoded).hexdigest())
            with self.assertRaises(ValidationError):
                read_project_translation_candidate(request_id=saved.pk, source_id=source.pk, actor=self.actor)
        ProjectTranslationSource.objects.filter(pk=source.pk).update(position=100)
        with self.assertRaisesMessage(ValidationError, "条目位置"):
            read_project_translation_candidate(request_id=saved.pk, source_id=source.pk, actor=self.actor)
