"""Real manual API, confirmed script and original Bible writer; no model call."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import (ApprovalDecision, Asset, CharacterBible, Episode, ExecutionPreparation, GeneratedArtifact,
    GenerationAttempt, ProductionManualCommand, ProductionRun, SceneBible, PropBible, Shot)
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
from studio.services.production_reviewed_content import read_reviewed_bible, read_reviewed_storyboard, reviewed_entity_row
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.json_contracts import json_contract
from studio.tests import test_production_manual_creation as creation
from studio.tests.structured_bible_fixtures import confirmed_structured_bible_source


def confirmed_bible_source(testcase, *, episodes=None):
    entities = [{"key": key, "kind": kind, "name": name, "description": name + "的设定",
        "identity_anchor": name + "的共同身份", "primary": {"reference_name": name + "·主图", "anchor": "明确主图外观"},
        "variants": []} for key, kind, name in (("traveler", "character", "旅人"), ("room", "scene", "房间"), ("box", "prop", "盒子"))]
    entities[0]["variants"] = [{"key": "raincoat", "name": "雨衣", "reference_name": "旅人·雨衣", "anchor": "黄色雨衣"}]
    return confirmed_structured_bible_source(testcase, entities=entities, episodes=episodes)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ProductionManualBibleTests(TestCase):
    create = creation.ProductionManualCreationTests.create

    def setUp(self):
        creation.ProductionManualCreationTests.setUp(self)
        result = self.create()
        self.assertEqual(result.status_code, 201, result.data)
        self.run = ProductionRun.objects.get(pk=result.data["id"])
        self.url = f"/api/v1/production-runs/{self.run.pk}/manual-actions/"
        self.read_url = f"/api/v1/production-runs/{self.run.pk}/bible-review/"
        self.revision = confirmed_bible_source(self)
        self.body = {"action": "adopt", "stage_key": "script_input", "unit_key": f"project:{self.project.pk}",
            "scope": {"kind": "project", "id": str(self.project.pk)}, "expected_run_plan_sha256": result.data["execution_plan"]["sha256"],
            "expected_selection_revision": 0, "idempotency_key": "initial-script", "payload": {"comment": "已确认来源",
                "content": {"kind": "script_revision", "id": str(self.revision.pk), "expected_sha256": self.revision.content_sha256}}}
        self.assertEqual(self.post(self.body).status_code, 201)

    def post(self, body):
        return self.client.post(self.url, body, format="json")

    def read(self):
        result = self.client.get(self.read_url)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result["Cache-Control"], "private, no-store")
        return result.data

    def adopt_source(self):
        state = self.read()
        request = {**self.body, "stage_key": "bible_materialize", "idempotency_key": "bible-source",
            "expected_selection_revision": state["source"]["revision"],
            "payload": {"expected_current_script": state["current_script"], "comment": "采用本轮剧本声明"}}
        response = self.post(request)
        self.assertEqual(response.status_code, 201, response.data)
        return request, response

    def approval(self):
        state = self.read()
        return {**self.body, "action": "approve", "stage_key": "bible_review", "idempotency_key": "bible-review",
            "expected_selection_revision": state["approval"]["revision"], "payload": {
                "expected_current_script": state["current_script"],
                "expected_source_snapshot_sha256": state["source"]["snapshot_sha256"],
                "expected_project_bible_sha256": state["project_bible_sha256"],
                "reviewed_candidate_ids": [item["id"] for item in state["candidates"]],
                "reviewed_setting_keys": [item["review_key"] for item in state["settings"]],
                "reviewed_project_bible": True, "comment": "逐项确认"}}

    def assert_no_bibles(self):
        self.assertFalse(CharacterBible.objects.exists())
        self.assertFalse(SceneBible.objects.exists())
        self.assertFalse(PropBible.objects.exists())

    def test_script_readoption_requires_sequential_bible_and_storyboard_rereview_without_generation(self):
        self.adopt_source(); self.assertEqual(self.post(self.approval()).status_code, 201)
        episode = Episode.objects.create(project=self.project, number=1, title="人工核对集")
        shot = Shot.objects.create(episode=episode, number=1, duration_ms=1000,
            media_type="image", include_audio=False)
        shot.props.add(PropBible.objects.get(project=self.project))

        def storyboard_adopt():
            response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-storyboard/")
            self.assertEqual(response.status_code, 200, response.data)
            data = response.data
            return self.post({**self.body, "stage_key": "storyboard", "idempotency_key": str(uuid4()),
                "expected_selection_revision": data["adoption"]["revision"], "payload": {
                    "expected_content_sha256": data["content_sha256"], "source": {"kind": "existing_storyboard"},
                    "reviewed_shot_ids": [str(shot.pk)], "comment": "明确采用"}})

        def reference_review():
            response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-storyboard-review/")
            self.assertEqual(response.status_code, 200, response.data)
            data = response.data
            self.assertTrue(data["ready"], data["reason"])
            return self.post({**self.body, "action": "approve", "stage_key": "storyboard_review", "idempotency_key": str(uuid4()),
                "expected_selection_revision": data["approval"]["revision"], "payload": {
                    "expected_contract_sha256": data["contract_sha256"], "reviewed_shot_ids": [str(shot.pk)], "comment": "明确复核"}})

        self.assertEqual(storyboard_adopt().status_code, 201)
        result = reference_review(); self.assertEqual(result.status_code, 201, result.data)
        stages = {row.stage_key: row for row in self.run.stages.all()}
        originals = {key: deepcopy(stages[key].output_snapshot) for key in ("bible_review", "storyboard", "storyboard_review")}
        counts = {model: model.objects.count() for model in (CharacterBible, SceneBible, PropBible)}
        impact = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/", {"stage_key": "script_input"})
        self.assertEqual(impact.status_code, 200, impact.data)
        self.assertEqual(impact.data["replacement"]["code"], "review_only")
        changed = self.post({**self.body, "expected_selection_revision": 1, "idempotency_key": str(uuid4())})
        self.assertEqual(changed.status_code, 201, changed.data)
        self.assertEqual(self.read()["source"]["status"], "changed")
        self.assertEqual(self.read()["approval"]["status"], "changed")
        for key, saved in originals.items():
            stages[key].refresh_from_db(); self.assertEqual(stages[key].output_snapshot, saved)
        with self.assertRaises(ValidationError):
            read_reviewed_bible(run=self.run, eligible_stage_ids=[stages["bible_review"].pk])
        with self.assertRaises(ValidationError):
            read_reviewed_storyboard(run=self.run, eligible_stage_ids=[stages["storyboard_review"].pk])
        current = self.read()
        result = self.post({**self.body, "stage_key": "bible_materialize", "idempotency_key": str(uuid4()),
            "expected_selection_revision": current["source"]["revision"], "payload": {
                "expected_current_script": current["current_script"], "comment": "重新采用Bible来源"}})
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.post({**self.approval(), "idempotency_key": str(uuid4())}).status_code, 201)
        self.assertEqual(storyboard_adopt().status_code, 201)
        result = reference_review(); self.assertEqual(result.status_code, 201, result.data)
        for model, count in counts.items():
            self.assertEqual(model.objects.count(), count)
        result = read_reviewed_storyboard(run=self.run, eligible_stage_ids=[stages["storyboard_review"].pk])
        self.assertEqual(result["review"]["revision"], 2)
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(GeneratedArtifact.objects.filter(stage_run__production_run=self.run).exists())

    def test_read_adopt_and_approve_are_separate_real_facts_and_idempotent(self):
        before = ProductionManualCommand.objects.count()
        initial = self.read()
        self.assertEqual(initial["source"]["status"], "not_adopted")
        self.assertEqual(initial["candidates"], [])
        self.assertEqual(ProductionManualCommand.objects.count(), before)
        request, first = self.adopt_source()
        self.assert_no_bibles()
        self.assertEqual(self.post(request).data, first.data)
        state = self.read()
        self.assertEqual(state["source"]["status"], "current")
        self.assertEqual(state["approval"]["status"], "not_reviewed")
        self.assertEqual(len(state["candidates"]), 3)
        self.assertTrue(all(row["action"] == "create" for row in state["candidates"]))
        body = self.approval()
        result = self.post(body)
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.post(body).data, result.data)
        self.assertEqual([model.objects.count() for model in (CharacterBible, SceneBible, PropBible)], [1, 1, 1])
        state = self.read()
        self.assertEqual(state["approval"]["status"], "current")
        self.assertTrue(all(row["action"] == "reuse" and row["existing"]["is_locked"] for row in state["candidates"]))
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "awaiting_input")
        self.assertFalse(self.run.stages.exclude(stage_key__in=("script_input", "bible_materialize", "bible_review")).exclude(status="pending").exists())
        for model in (ExecutionPreparation, GenerationAttempt, GeneratedArtifact):
            self.assertFalse(model.objects.filter(stage_run__production_run=self.run).exists())
        self.assertEqual(ApprovalDecision.objects.count(), 3)
        CharacterBible.objects.update(description="用户随后明确编辑的内容")
        state = self.read()
        self.assertEqual(state["approval"]["status"], "changed")
        self.assertEqual(next(row for row in state["candidates"] if row["entity_type"] == "character")["existing"]["description"], "用户随后明确编辑的内容")

    def test_incomplete_stale_or_downstream_requests_do_not_materialize(self):
        self.adopt_source()
        body = self.approval()
        for key, value in (("reviewed_candidate_ids", []), ("reviewed_candidate_ids", body["payload"]["reviewed_candidate_ids"] * 2),
                ("expected_current_script", None), ("expected_source_snapshot_sha256", "f" * 64),
                ("expected_project_bible_sha256", "f" * 64), ("reviewed_project_bible", False),
                ("reviewed_project_bible", 1), ("reviewed_project_bible", "true")):
            bad = deepcopy(body); bad["payload"][key] = value
            self.assertIn(self.post(bad).status_code, (400, 409))
        self.run.stages.filter(stage_key="visual_rules_generate").update(status="running")
        self.assertEqual(self.post(body).status_code, 400)
        self.assert_no_bibles()
        self.assertEqual(ProductionManualCommand.objects.count(), 2)

    def test_project_drift_conflict_and_receipt_failure_leave_no_partial_records(self):
        self.adopt_source()
        body = self.approval()
        scene = SceneBible.objects.create(project=self.project, slug="new-scene", name="另一个已有人物场景", description="新内容")
        self.assertEqual(self.post(body).status_code, 409)
        body = self.approval()
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=ValidationError("receipt failed")):
            self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(CharacterBible.objects.exists())
        self.assertFalse(PropBible.objects.exists())
        self.assertEqual(list(SceneBible.objects.values_list("id", flat=True)), [scene.pk])
        self.assertEqual(self.run.stages.get(stage_key="bible_review").status, "pending")
        self.assertEqual(ApprovalDecision.objects.count(), 2)
        self.assertEqual(ProductionManualCommand.objects.count(), 2)
        from studio.services.bible_variants import create_variant
        create_variant(actor=self.actor, project_id=self.project.pk, entity_kind="scene", entity_id=scene.pk,
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="",
            value={"name": "占用引用名", "reference_name": "旅人·主图", "identity_anchor": "另一实体", "appearance_anchor": "不同外观",
                "basis_asset_id": None, "basis_asset_sha256": ""})
        body = self.approval()
        self.assertEqual(self.post(body).status_code, 409)
        self.assertFalse(CharacterBible.objects.exists())

    def test_source_integrity_scope_and_permissions_are_not_inferred(self):
        state = self.read()
        body = {**self.body, "stage_key": "bible_materialize", "idempotency_key": "bible-source",
            "payload": {"expected_current_script": state["current_script"], "comment": ""}}
        self.assertEqual(self.post({**body, "unit_key": "project:unrelated"}).status_code, 400)
        self.project.master_script = "可变母稿不是来源"
        self.project.save(update_fields=("master_script",))
        self.adopt_source()
        self.assertEqual(self.read()["source"]["content"]["id"], str(self.revision.pk))
        self.assertEqual(self.client.get(self.read_url, {"page": 1}).status_code, 400)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.read_url).status_code, 404)
        self.assertEqual(self.post(body).status_code, 404)

    def test_shared_reader_uses_full_manual_review_and_rejects_creative_drift(self):
        self.adopt_source()
        # An existing, non-candidate character is part of the explicitly shown review too.
        character = CharacterBible.objects.create(project=self.project, slug="reviewed-narrator", name="旁白",
            stable_speaker_id="narrator", description="旁白设定",
            voice_profile="Warm measured voice.", personality="Patient.", wardrobe="Grey coat.",
            negative_constraints="No masks.", metadata={"creative_note": "保持音色一致"})
        detail = self.read()
        fact = next(row for row in detail["project_bible"]["characters"] if row["id"] == str(character.pk))
        self.assertEqual(fact["voice_profile"], character.voice_profile)
        self.assertEqual(fact["stable_speaker_id"], "narrator")
        stage = self.run.stages.get(stage_key="bible_review")
        with self.assertRaises(ValidationError):
            read_reviewed_bible(run=self.run, eligible_stage_ids=[stage.pk])
        result = self.post(self.approval())
        self.assertEqual(result.status_code, 201, result.data)
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot_schema_version, 3)
        saved = deepcopy(stage.output_snapshot)
        reviewed = read_reviewed_bible(run=self.run, eligible_stage_ids=[stage.pk])
        self.assertEqual(reviewed["canonical"], saved["project_bible"])
        self.assertEqual(reviewed["review"]["decision_id"], self.read()["approval"]["decision_id"])
        row = reviewed_entity_row(reviewed, {"scope": {"kind": "character", "id": str(character.pk)}})
        self.assertEqual(row["voice_profile"], character.voice_profile)
        self.assertEqual(row["wardrobe"], "Grey coat.")
        definition = json_contract({"schema_version": 1, "configuration": {"inputs": [{"key": "entity",
            "kind": "json", "required": True, "min_items": 1, "max_items": 1, "targets": [],
            "semantic_contract": {"schema_version": 1, "allowed_scope_kinds": ["request"],
                "allowed_purposes": [], "allowed_subject_kinds": [], "paired_with_input_key": None,
                "allow_repeated_asset": False}}]}, "resources": {"conditioning_mode": None},
            "interface": {"input_targets": {}}})
        inputs = {"entity": {"kind": "context_field", "selector_key": "production.entity.content",
            "scope": {"kind": "request"}, "content_revision_id": reviewed["review"]["decision_id"]}}
        def resolve():
            return resolve_execution_input_sources(definition, inputs, owner=self.actor, subject_facts=[],
                max_media_bytes=65536, project_id=self.project.pk, production_run_id=str(self.run.pk),
                eligible_stage_ids=[stage.pk], production_scope={"kind": "character", "id": str(character.pk)}).value
        parsed = resolve()
        self.assertEqual(parsed["logical_values"]["entity"],
            {**row, "primary_setting": None})
        self.assertEqual(row["appearance_settings"], [])
        self.assertIsNone(parsed["input_provenance"][0]["setting"])
        self.assertNotIn("visual_identity", row)
        self.assertEqual(self.read()["approval"]["status"], "current")
        self.assertEqual(resolve()["logical_values"]["entity"], parsed["logical_values"]["entity"])
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot, saved)
        self.assertEqual(parsed["input_provenance"][0]["review"], reviewed["review"])
        self.assertNotIn("anchor_asset_id", row)
        self.assertNotIn("reference_asset_ids", row)
        # Relationship-only fixture: this is not evidence that media passed review.
        asset = Asset.objects.create(owner=self.actor, kind="reference", name="Separate image binding")
        character.reference_assets.add(asset)
        CharacterBible.objects.filter(pk=character.pk).update(anchor_asset=asset)
        self.assertEqual(read_reviewed_bible(run=self.run, eligible_stage_ids=[stage.pk])["canonical"], reviewed["canonical"])
        for eligible, decision_id in (([], None), ([stage.pk], str(uuid4()))):
            with self.assertRaises(ValidationError):
                read_reviewed_bible(run=self.run, eligible_stage_ids=eligible, decision_id=decision_id)
        before = ApprovalDecision.objects.count()
        for field, value in (("voice_profile", "New voice."), ("wardrobe", "Blue suit."),
                ("personality", "Impatient."), ("metadata", {"creative_note": "另一个设定"})):
            original = getattr(character, field)
            CharacterBible.objects.filter(pk=character.pk).update(**{field: value})
            self.assertEqual(self.read()["approval"]["status"], "changed")
            with self.assertRaises(ValidationError):
                resolve()
            CharacterBible.objects.filter(pk=character.pk).update(**{field: original})
        self.assertEqual(self.read()["approval"]["status"], "current")
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot, saved)
        self.assertEqual(ApprovalDecision.objects.count(), before)

    def test_rehashed_old_entity_visual_snapshot_is_not_silently_stripped_or_upgraded(self):
        from studio.services.json_contracts import assign_json_contract
        self.adopt_source()
        self.assertEqual(self.post(self.approval()).status_code, 201)
        stage = self.run.stages.get(stage_key="bible_review")
        value = deepcopy(stage.output_snapshot)
        value["project_bible"]["characters"][0]["visual_identity"] = "Old duplicated body"
        value["project_bible_sha256"] = json_contract(value["project_bible"]).sha256
        assign_json_contract(stage, "output_snapshot", value, schema_version=3)
        stage.save()
        stage.approval_decisions.update(output_snapshot_sha256=stage.output_snapshot_sha256)
        before = deepcopy(stage.output_snapshot)
        count = ApprovalDecision.objects.count()
        self.assertEqual(self.read()["approval"]["status"], "changed")
        with self.assertRaises(ValidationError):
            read_reviewed_bible(run=self.run, eligible_stage_ids=[stage.pk])
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot, before)
        self.assertEqual(ApprovalDecision.objects.count(), count)

    def test_full_creative_content_change_invalidates_a_pending_confirmation(self):
        self.adopt_source()
        character = CharacterBible.objects.create(project=self.project, slug="unmatched", name="另一个人物", voice_profile="Old voice.")
        body = self.approval()
        CharacterBible.objects.filter(pk=character.pk).update(voice_profile="Changed before confirmation.")
        self.assertEqual(self.post(body).status_code, 409)
        self.assertEqual(self.run.stages.get(stage_key="bible_review").status, "pending")
        self.assertEqual(ApprovalDecision.objects.count(), 2)

    def test_bible_source_requires_the_original_script_adoption_decision(self):
        from studio.services.json_contracts import assign_json_contract

        self.adopt_source()
        stage = self.run.stages.get(stage_key="bible_materialize")
        original = deepcopy(stage.output_snapshot)
        self.run.refresh_from_db()
        self.assertEqual(original["script_decision_id"], str(self.run.script_adoption_decision_id))
        for basis in (None, "bad-id", str(uuid4())):
            value = {**original, "script_decision_id": basis}
            assign_json_contract(stage, "output_snapshot", value, schema_version=2)
            stage.save()
            stage.approval_decisions.update(output_snapshot_sha256=stage.output_snapshot_sha256)
            self.assertEqual(self.client.get(self.read_url).status_code, 400)
            stage.refresh_from_db()
            self.assertEqual(stage.output_snapshot, value)
        self.assertEqual(ApprovalDecision.objects.count(), 2)
