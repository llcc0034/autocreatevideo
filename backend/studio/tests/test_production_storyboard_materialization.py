"""Original manual command adopts exact M2/M3 declarations; isolated, no provider."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import (ApprovalDecision, Asset, CharacterBible, Episode, ExecutionPreparation,
    GenerationAttempt, ProductionManualCommand, ProductionRun, PropBible, SceneBible, Shot,
    ShotCharacterRelation, ShotReferenceUse, ShotScriptSource, StageRun)
from studio.services.bible_setting_evidence import primary_setting_content_reference
from studio.services.bible_variants import create_variant, revise_variant, setting_payload
from studio.services.script_bible_materialization import script_bible_review_inventory
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_storyboard_materialization import read_storyboard_materialization_plan
from studio.services.security_boundaries import boundary_int
from studio.tests import test_production_manual_creation as creation
from studio.tests import test_storyboard_materialization_plan as fixtures


class StoryboardBibleInventoryTests(TestCase):
    # This shared Bible reader is still used by M3. Keep its query/empty-value
    # coverage, not the obsolete assumption that any current Bible is a source.
    def setUp(self):
        creation.ProductionManualCreationTests.setUp(self)

    def entities(self):
        return [(kind, model.objects.create(project=self.project, name=kind, slug=kind,
            description=f"description {kind}"))
            for kind, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible))]


    def primary(self, kind, entity, *, blank=False):
        return create_variant(actor=self.actor, project_id=self.project.pk, entity_kind=kind, entity_id=entity.pk,
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="", value={
                "name": "主图", "reference_name": f"{kind} 主图", "identity_anchor": "" if blank else f"identity {kind}",
                "appearance_anchor": "" if blank else f"appearance {kind}", "basis_asset_id": None, "basis_asset_sha256": ""})


    def test_bible_inventory_batches_exact_references_without_duplicate_body_or_media(self):
        entities = self.entities()
        primaries = [self.primary(*entity) for entity in entities]
        with CaptureQueriesContext(connection) as queries:
            facts = script_bible_review_inventory(self.project)
        self.assertEqual(len(queries), 5)  # Three entity sets and two shared primary-reader queries.
        for query in queries:
            self.assertNotIn('"visual_identity"', query["sql"])
            self.assertNotIn('"studio_scriptrevision"."content"', query["sql"])
            self.assertNotIn('"studio_asset"', query["sql"])
            self.assertNotIn("SELECT *", query["sql"].upper())
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                for query in queries:
                    cursor.execute("EXPLAIN QUERY PLAN " + query["sql"])
                    plan = " ".join(str(row[-1]) for row in cursor.fetchall()).upper()
                    self.assertIn("SEARCH", plan)
                    self.assertNotIn("SCAN STUDIO_", plan)
        for section, primary in zip(("characters", "scenes", "props"), primaries):
            row = facts[section][0]
            self.assertEqual(row["primary_setting_ref"], primary_setting_content_reference(primary.revisions.get(revision=1)))
            self.assertNotIn("identity_anchor", str(row))
            self.assertNotIn("appearance_anchor", str(row))
            self.assertNotIn("visual_identity", row)
            self.assertNotIn("review", row["primary_setting_ref"])
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())


    def test_absent_and_explicit_blank_primary_remain_distinct(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(script_bible_review_inventory(self.project), {"characters": [], "scenes": [], "props": []})
        self.assertEqual(len(queries), 3)
        entities = self.entities()
        absent = script_bible_review_inventory(self.project)
        self.assertTrue(all(row["primary_setting_ref"] is None for rows in absent.values() for row in rows))
        primary = self.primary(*entities[0], blank=True)
        changed = script_bible_review_inventory(self.project)
        self.assertEqual(changed["characters"][0]["primary_setting_ref"]["setting_revision_id"],
            str(primary.revisions.get(revision=1).pk))
        self.assertNotEqual(json_contract(absent).sha256, json_contract(changed).sha256)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ProductionStoryboardMaterializationTests(TestCase):
    create = fixtures.ManualStoryboardMaterializationPlanTests.create
    post = fixtures.ManualStoryboardMaterializationPlanTests.post
    read = fixtures.ManualStoryboardMaterializationPlanTests.read
    adopt_source = fixtures.ManualStoryboardMaterializationPlanTests.adopt_source
    approval = fixtures.ManualStoryboardMaterializationPlanTests.approval

    def setUp(self):
        fixtures.ManualStoryboardMaterializationPlanTests.setUp(self)
        self.storyboard_url = f"/api/v1/production-runs/{self.run.pk}/manual-storyboard/"
        for target in ("studio.services.script_revision_structures.parse_script_semantic_output",):
            guard = patch(target, side_effect=AssertionError("No old writer, parse or network"))
            guard.start()
            self.addCleanup(guard.stop)

    def detail(self):
        result = self.client.get(self.storyboard_url)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result["Cache-Control"], "private, no-store")
        return result.data

    def adoption_request(self):
        detail = self.detail()
        self.assertTrue(detail["materialization"]["ready"], detail["materialization"])
        return {**self.body, "stage_key": "storyboard", "idempotency_key": "declared-storyboard",
            "expected_selection_revision": detail["adoption"]["revision"], "payload": {
                "source": {"kind": "confirmed_script", "expected_materialization_sha256": detail["materialization"]["sha256"]},
                "expected_content_sha256": detail["content_sha256"], "comment": "核对结构化声明"}}

    def facts(self):
        return {model.__name__: list(model.objects.order_by("pk").values()) for model in (
            ProductionRun, StageRun, ApprovalDecision, ProductionManualCommand, Episode, Shot,
            ShotScriptSource, ShotCharacterRelation, ShotReferenceUse, ExecutionPreparation, GenerationAttempt, Asset)}

    def test_read_only_contract_is_the_same_as_recipe_and_has_no_split_or_body_copy(self):
        before = self.facts()
        with CaptureQueriesContext(connection) as queries:
            first, repeated = self.detail(), self.detail()
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        self.assertEqual(first, repeated)
        self.assertEqual(self.facts(), before)
        expected = read_storyboard_materialization_plan(run=self.run, stage=self.stage).contract
        self.assertEqual(first["materialization"]["contract"], expected.value)
        self.assertEqual(first["materialization"]["sha256"], expected.sha256)
        self.assertEqual(expected.schema_version, 3)
        for removed in ("episode_count_hint", "auto_split_trigger_chars", "prop_alias_policy", "Bonjour"):
            self.assertNotIn(removed, str(expected.value))

    def test_original_command_creates_typed_facts_exact_binding_and_real_human_receipt(self):
        request = self.adoption_request()
        before_decisions, before_commands = ApprovalDecision.objects.count(), ProductionManualCommand.objects.count()
        self.project.master_script = "Later manuscript, not this Run's adopted source"
        self.project.save(update_fields=("master_script",))
        result = self.post(request)
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual((Episode.objects.count(), Shot.objects.count(), ShotScriptSource.objects.count()), (1, 3, 3))
        episode = Episode.objects.get()
        self.assertEqual(episode.prompt_settings["include_audio"],
            self.run.config_snapshot["values"]["production.default_include_audio"])
        self.assertIs(type(episode.prompt_settings["include_audio"]), bool)
        public_episode = self.client.get(f"/api/v1/episodes/{episode.pk}/")
        self.assertEqual(public_episode.status_code, 200, public_episode.data)
        self.assertEqual(public_episode.data["prompt_settings"], episode.prompt_settings)
        self.assertEqual(set(Shot.objects.values_list("number", flat=True)), {8, 2, 9})
        self.assertEqual(set(ShotScriptSource.objects.values_list("episode_number", flat=True)), {4})
        self.assertEqual(set(ShotCharacterRelation.objects.values_list("role", flat=True)), {"speaker"})
        voice = Shot.objects.get(number=8)
        self.assertTrue(voice.dialogue[0]["voiceover"])
        self.assertNotIn("is_voiceover", voice.dialogue[0])
        from studio.services.dialogue import normalize_dialogue
        self.assertEqual(normalize_dialogue(voice.dialogue), voice.dialogue)
        public_shot = self.client.get(f"/api/v1/shots/{voice.pk}/")
        self.assertEqual(public_shot.status_code, 200, public_shot.data)
        self.assertEqual(public_shot.data["dialogue"], voice.dialogue)
        self.assertEqual(voice.dialogue[0]["text"], " 原文\nBonjour。 ")
        self.assertFalse(voice.character_relations.filter(role="visible").exists())
        self.assertFalse({"narrative", "action", "detailed_description"} & {field.name for field in Shot._meta.get_fields()})
        self.assertEqual(voice.conditioning_mode, "")
        use = ShotReferenceUse.objects.get()
        self.assertIsNone(use.asset_id)
        self.assertEqual(use.source_mode, "")
        self.assertIsNotNone(use.setting_revision_id)
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, "Later manuscript, not this Run's adopted source")
        stage = self.run.stages.get(stage_key="storyboard")
        self.assertEqual(stage.output_snapshot_schema_version, 5)
        self.assertEqual(set(stage.output_snapshot["source"]), {"kind", "contract", "sha256"})
        self.assertEqual(stage.output_snapshot["source"]["sha256"], request["payload"]["source"]["expected_materialization_sha256"])
        self.assertEqual((ApprovalDecision.objects.count(), ProductionManualCommand.objects.count()), (before_decisions+1, before_commands+1))
        decision = stage.approval_decisions.get()
        self.assertEqual((decision.actor, decision.decided_by_id), ("human", self.actor.pk))
        self.assertEqual(str(decision.pk), result.data["target_id"])
        detail = self.detail()
        self.assertEqual(detail["adoption"]["status"], "current")
        self.assertEqual(detail["content"], stage.output_snapshot["content"])
        self.assertFalse(detail["materialization"]["ready"])
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertEqual(self.run.stages.get(stage_key="storyboard_review").status, "pending")

    def test_exact_command_replay_is_read_only_and_new_key_cannot_duplicate(self):
        request = self.adoption_request()
        result = self.post(request)
        self.assertEqual(result.status_code, 201, result.data)
        before = self.facts()
        with CaptureQueriesContext(connection) as queries:
            replay = self.post(request)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data, result.data)
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        self.assertEqual(self.post({**request, "idempotency_key": "different"}).status_code, 409)
        self.assertEqual(self.facts(), before)

    def test_config_drift_and_client_injection_do_not_create_anything(self):
        request = self.adoption_request()
        bad = deepcopy(request)
        bad["payload"]["source"]["parameters"] = {"default_shot_duration_ms": 6000}
        self.assertEqual(self.post(bad).status_code, 400)
        self.run.refresh_from_db()
        value = deepcopy(self.run.config_snapshot)
        value["values"]["production.default_include_audio"] = not value["values"]["production.default_include_audio"]
        assign_json_contract(self.run, "config_snapshot", value, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        materialization = self.detail()["materialization"]
        self.assertTrue(materialization["ready"], materialization["reason"])
        self.assertNotEqual(materialization["sha256"], request["payload"]["source"]["expected_materialization_sha256"])
        before = self.facts()
        self.assertEqual(self.post(request).status_code, 409)
        self.assertEqual(self.facts(), before)

    def test_existing_content_is_never_overwritten(self):
        request = self.adoption_request()
        episode = Episode.objects.create(project=self.project, number=5, title="User content")
        before = self.facts()
        self.assertFalse(self.detail()["materialization"]["ready"])
        self.assertEqual(self.post(request).status_code, 409)
        self.assertEqual(self.facts(), before)
        self.assertTrue(Episode.objects.filter(pk=episode.pk, title="User content").exists())

    def test_changed_bible_setting_requires_real_upstream_review_not_new_name_scan(self):
        request = self.adoption_request()
        primary = self.project.characters.get().variants.get(appearance_role="primary")
        revision = primary.revisions.get(revision=primary.revision)
        revise_variant(actor=self.actor, variant=primary, expected_revision=primary.revision,
            value={**setting_payload(revision), "appearance_anchor": "Changed setting"})
        before = self.facts()
        detail = self.detail()
        self.assertFalse(detail["materialization"]["ready"])
        self.assertTrue(detail["materialization"]["reason"])
        self.assertEqual(self.post(request).status_code, 400)
        self.assertEqual(self.facts(), before)

    def test_missing_bible_receipt_blocks_only_creation(self):
        request = self.adoption_request()
        StageRun.objects.filter(production_run=self.run, stage_key="bible_review").update(output_snapshot_sha256="f"*64)
        before = self.facts()
        detail = self.detail()
        self.assertFalse(detail["materialization"]["ready"])
        self.assertIsNone(detail["materialization"]["contract"])
        self.assertEqual(self.post(request).status_code, 400)
        self.assertEqual(self.facts(), before)

    def test_source_pointer_corruption_is_not_repaired(self):
        request = self.adoption_request()
        ProductionRun.objects.filter(pk=self.run.pk).update(script_revision=None)
        before = self.facts()
        self.assertEqual(self.client.get(self.storyboard_url).status_code, 400)
        self.assertIn(self.post(request).status_code, (400, 409))
        self.assertEqual(self.facts(), before)

    def test_binding_approval_and_command_receipt_failures_roll_back_whole_write(self):
        request = self.adoption_request()
        before = self.facts()
        original_save = StageRun.save
        def fail_output(instance, *args, **kwargs):
            if instance.pk == self.stage.pk and instance.output_snapshot:
                raise ValidationError("isolated original approval snapshot failure")
            return original_save(instance, *args, **kwargs)
        for target, effect in (
                ("studio.services.production_storyboard_materialization.ShotScriptSource.objects.bulk_create", ValidationError("isolated binding failure")),
                ("studio.services.production_manual_commands.StageRun.save", fail_output),
                ("studio.services.production_manual_commands.ProductionManualCommand.save", ValidationError("isolated command receipt failure"))):
            with self.subTest(target=target), patch(target, side_effect=effect, autospec=True):
                result = self.post(request)
                self.assertEqual(result.status_code, 400, result.data)
            self.assertEqual(self.facts(), before)
        self.assertEqual(self.post(request).status_code, 201)

    def test_saved_source_or_relations_drift_is_visible_without_rewriting_adoption(self):
        self.assertEqual(self.post(self.adoption_request()).status_code, 201)
        stage = self.run.stages.get(stage_key="storyboard")
        original = deepcopy(stage.output_snapshot)
        source = ShotScriptSource.objects.get(shot__number=8)
        ShotScriptSource.objects.filter(pk=source.pk).update(document_sha256="e"*64)
        self.assertEqual(self.detail()["adoption"]["status"], "changed")
        ShotScriptSource.objects.filter(pk=source.pk).update(document_sha256=source.document_sha256)
        ShotCharacterRelation.objects.filter(shot__number=8).update(role="visible")
        invalid_relations = self.client.get(self.storyboard_url)
        self.assertEqual(invalid_relations.status_code, 400, invalid_relations.data)
        self.assertIn("发言者关系不完整", str(invalid_relations.data))
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot, original)

    def test_old_receipt_contract_is_rejected_without_conversion(self):
        self.assertEqual(self.post(self.adoption_request()).status_code, 201)
        self.stage.refresh_from_db()
        value = deepcopy(self.stage.output_snapshot)
        value["source"]["contract"]["schema_version"] = 2
        value["source"]["sha256"] = json_contract(value["source"]["contract"]).sha256
        assign_json_contract(self.stage, "output_snapshot", value, schema_version=5)
        self.stage.save()
        self.stage.approval_decisions.update(output_snapshot_sha256=self.stage.output_snapshot_sha256)
        before = self.facts()
        self.assertEqual(self.client.get(self.storyboard_url).status_code, 400)
        self.assertEqual(self.facts(), before)

    def test_owner_and_authentication_boundaries_leave_no_targets(self):
        request = self.adoption_request()
        self.client.force_authenticate(get_user_model().objects.create_user(username="other-storyboard-owner"))
        self.assertEqual(self.client.get(self.storyboard_url).status_code, 404)
        self.assertEqual(self.post(request).status_code, 404)
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get(self.storyboard_url).status_code, (401, 403))
        self.assertIn(self.post(request).status_code, (401, 403))
        self.assertFalse(Episode.objects.exists())

    def test_unstructured_confirmed_text_is_not_parsed_or_written_as_a_storyboard(self):
        result = self.create({**self.payload, "idempotency_key": "create-unstructured-storyboard"})
        self.assertEqual(result.status_code, 201, result.data)
        url = f"/api/v1/production-runs/{result.data['id']}"
        _, preview = create_pasted_script_source(project=self.project, user=self.actor, content="第1集：出发。只确认原稿。")
        revision = confirm_script_revision(preview, user=self.actor)
        request = {**self.body, "expected_run_plan_sha256": result.data["execution_plan"]["sha256"],
            "idempotency_key": "raw-source", "payload": {"comment": "仅确认原文", "content": {
                "kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256}}}
        adopted = self.client.post(url + "/manual-actions/", request, format="json")
        self.assertEqual(adopted.status_code, 201, adopted.data)
        response = self.client.get(url + "/manual-storyboard/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["materialization"]["ready"])
        self.assertTrue(response.data["materialization"]["reason"])
        self.assertFalse(Episode.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        revision.refresh_from_db()
        self.assertEqual(revision.content, "第1集：出发。只确认原稿。")
