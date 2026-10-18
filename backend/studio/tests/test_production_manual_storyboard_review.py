"""Real manual review API and original source resolver; isolated local media."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
import wave

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from PIL import Image

from studio.models import ApprovalDecision, Asset, ExecutionPreparation, GenerationAttempt, ProductionManualCommand, PropBible, ShotReferenceUse
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.production_reviewed_content import read_reviewed_storyboard
from studio.services.production_reviewed_references import read_reviewed_shot_references
from studio.services.json_contracts import assign_json_contract
from studio.tests import test_production_manual_storyboard as fixtures
from studio.tests import test_production_shot_context as context_fixtures
from studio.tests.structured_bible_fixtures import confirmed_structured_bible_source, entities_fixture


class ManualStoryboardReferenceTests(TestCase):
    create = fixtures.ProductionManualStoryboardTests.create
    read = fixtures.ProductionManualStoryboardTests.read
    post = fixtures.ProductionManualStoryboardTests.post
    request_body = fixtures.ProductionManualStoryboardTests.request_body

    def setUp(self):
        fixtures.ProductionManualStoryboardTests.setUp(self)
        self.review_url = f"/api/v1/production-runs/{self.run.pk}/manual-storyboard-review/"
        self.media = TemporaryDirectory(); self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name, ASSET_LIBRARY_ROOT=self.media.name)
        setting.enable(); self.addCleanup(setting.disable)

    def review(self):
        response = self.client.get(self.review_url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        return response.data

    def adopt(self):
        response = self.post(self.request_body())
        self.assertEqual(response.status_code, 201, response.data)

    def approve_body(self):
        detail = self.review()
        self.assertTrue(detail["ready"], detail["reason"])
        return {**self.body, "action": "approve", "stage_key": "storyboard_review", "idempotency_key": str(uuid4()),
            "expected_selection_revision": detail["approval"]["revision"], "payload": {
                "expected_contract_sha256": detail["contract_sha256"],
                "reviewed_shot_ids": [row["shot_id"] for row in detail["references"]], "comment": "逐镜核对"}}

    def asset(self, *, audio=False, owner=None):
        stream = BytesIO()
        if audio:
            with wave.open(stream, "wb") as writer:
                writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(8000); writer.writeframes(b"\0\0" * 80)
        else:
            Image.new("RGB", (8, 8), "blue").save(stream, format="PNG")
        data = stream.getvalue()
        asset = Asset(owner=owner or self.actor, kind="reference_audio" if audio else "reference", name="Local review fixture",
            mime_type="audio/wav" if audio else "image/png", checksum=sha256(data).hexdigest())
        asset.file.save("fixture.wav" if audio else "fixture.png", ContentFile(data))
        return asset

    def use(self, asset, *, purpose="configured_style", setting=None):
        return ShotReferenceUse.objects.create(shot=self.shots[0], use_key="selected-reference", position=0,
            media_kind="audio" if asset.mime_type.startswith("audio/") else "image", purpose=purpose,
            setting_revision=setting, source_mode="existing", asset=asset, asset_sha256=asset.checksum)

    def test_content_adoption_is_not_reference_approval_and_original_resolver_reads_review(self):
        self.assertIsNone(self.review()["display"])
        self.assertFalse(self.review()["ready"])
        self.assertFalse(ApprovalDecision.objects.exists())
        self.adopt()
        detail = self.review()
        self.assertEqual(detail["display"]["name_basis"], "current_bible_by_id")
        self.assertEqual([row["shot_id"] for row in detail["display"]["shots"]], [str(shot.pk) for shot in self.shots])
        self.assertNotIn("display", detail["contract"])
        self.assertEqual(detail["approval"]["status"], "not_reviewed")
        self.assertEqual(len(detail["references"]), 2)
        self.assertTrue(all(row["items"] == [] for row in detail["references"]))
        stage = self.run.stages.get(stage_key="storyboard_review")
        with self.assertRaises(ValidationError):
            read_reviewed_storyboard(run=self.run, eligible_stage_ids=[stage.pk])
        body = self.approve_body(); response = self.post(body)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.post(body).data, response.data)
        saved = read_reviewed_storyboard(run=self.run, eligible_stage_ids=[stage.pk])
        parsed = resolve_execution_input_sources(context_fixtures.ShotContextTests.definition(self), {"brief": {
            "kind": "context_field", "selector_key": "shot.duration_ms", "scope": {"kind": "shot", "id": str(self.shots[0].pk)},
            "content_revision_id": saved["review"]["decision_id"]}}, owner=self.actor, subject_facts=[], max_media_bytes=65536,
            project_id=self.project.pk, production_run_id=str(self.run.pk), eligible_stage_ids=[stage.pk],
            production_scope={"kind": "shot", "id": str(self.shots[0].pk)}).value
        self.assertEqual(parsed["logical_values"]["brief"], self.shots[0].duration_ms)
        self.assertEqual(parsed["input_provenance"][0]["review"], saved["review"])
        self.assertEqual(ApprovalDecision.objects.count(), 2)
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(self.run.stages.filter(ordinal__gt=stage.ordinal).exclude(status="pending").exists())
        for eligible, decision in (([], None), ([stage.pk], str(uuid4()))):
            with self.assertRaises(ValidationError):
                read_reviewed_storyboard(run=self.run, eligible_stage_ids=eligible, decision_id=decision)

    def test_audio_identity_and_unused_attachment_are_not_implicit_model_inputs(self):
        audio = self.asset(audio=True); unused = self.asset()
        self.use(audio, purpose="custom_voice_style")
        self.adopt()
        shot = self.shots[0]
        detail = self.review()
        self.assertTrue(detail["ready"], detail["reason"])
        self.assertEqual([row["id"] for row in detail["contract"]["assets"]], [str(audio.pk)])
        snapshot_sha = detail["contract_sha256"]
        unused.is_active = False; unused.save(update_fields=("is_active",))
        self.assertEqual(self.review()["contract_sha256"], snapshot_sha)
        self.assertEqual(detail["references"][0]["items"][0]["source"]["asset_id"], str(audio.pk))
        self.assertEqual(len(detail["references"][0]["items"]), 1)
        self.assertEqual(self.post(self.approve_body()).status_code, 201)
        stage = self.run.stages.get(stage_key="media_generation")
        actual = read_reviewed_shot_references(stage=stage, shot_id=str(shot.pk))
        self.assertEqual(actual["records"][0]["purpose"], "custom_voice_style")
        self.assertNotIn(str(unused.pk), str(actual["records"]))
        original = deepcopy(self.run.stages.get(stage_key="storyboard_review").output_snapshot)
        Path(audio.file.path).write_bytes(b"changed fixture bytes")
        self.assertFalse(self.review()["ready"])
        with self.assertRaises(ValidationError):
            read_reviewed_shot_references(stage=stage, shot_id=str(shot.pk))
        self.assertEqual(self.run.stages.get(stage_key="storyboard_review").output_snapshot, original)

    def test_direct_image_choice_does_not_fabricate_or_require_bible_image_review(self):
        image = self.asset(); self.use(image); self.adopt()
        shot = self.shots[0]
        detail = self.review()
        self.assertTrue(detail["ready"], detail["reason"])
        self.assertIsNone(detail["contract"]["manual_basis"]["bible_image_review"])
        self.assertEqual(detail["references"][0]["items"][0]["source_key"], "images")
        self.assertEqual(detail["approval"]["status"], "not_reviewed")
        labels = {item["value"]: item["label"] for item in detail["source_options"]}
        self.assertEqual(labels["images"], "镜头用途图片")
        self.assertTrue(all("审核" not in label for label in labels.values()))
        self.assertEqual(ApprovalDecision.objects.count(), 1)
        self.assertEqual(self.post(self.approve_body()).status_code, 201)
        reviewed = self.review()
        self.assertEqual(reviewed["approval"]["status"], "current")
        self.assertEqual(reviewed["source_options"], detail["source_options"])
        records = read_reviewed_shot_references(stage=self.run.stages.get(stage_key="media_generation"),
            shot_id=str(shot.pk))["records"]
        self.assertEqual(records[0]["source"]["asset_id"], str(image.pk))
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__stage_key="bible_image_review").exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_pending_use_is_visible_and_cannot_be_approved_as_empty(self):
        ShotReferenceUse.objects.create(shot=self.shots[0], use_key="missing", position=0,
            media_kind="image", purpose="configured_style")
        self.adopt()
        detail = self.review()
        self.assertFalse(detail["ready"])
        self.assertIn("尚未选择确切素材", detail["reason"])
        self.assertEqual(ApprovalDecision.objects.count(), 1)

    def test_picture_review_reuses_formal_bible_and_image_approval_without_model_execution(self):
        entities = entities_fixture()
        entities.append({"key": "box", "kind": "prop", "name": "盒子", "identity_anchor": "方形木盒",
            "primary": {"reference_name": "木盒·主图", "anchor": "深色木盒"}, "variants": []})
        revision = confirmed_structured_bible_source(self, entities=entities)
        self.assertIsNone(revision.origin_generation_attempt_id)
        self.assertFalse(GenerationAttempt.objects.exists())
        base = {**self.body, "stage_key": "script_input", "idempotency_key": "initial-script", "payload": {
            "content": {"kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256}, "comment": "确认"}}
        response = self.post(base); self.assertEqual(response.status_code, 201, response.data)
        bible_url = f"/api/v1/production-runs/{self.run.pk}/bible-review/"
        detail = self.client.get(bible_url).data
        response = self.post({**base, "stage_key": "bible_materialize", "idempotency_key": "bible-source",
            "payload": {"expected_current_script": detail["current_script"], "comment": "读取声明"}})
        self.assertEqual(response.status_code, 201, response.data)
        detail = self.client.get(bible_url).data
        response = self.post({**base, "action": "approve", "stage_key": "bible_review", "idempotency_key": "bible-review",
            "payload": {"expected_current_script": detail["current_script"], "expected_source_snapshot_sha256": detail["source"]["snapshot_sha256"],
                "expected_project_bible_sha256": detail["project_bible_sha256"], "reviewed_project_bible": True,
                "reviewed_candidate_ids": [row["id"] for row in detail["candidates"]],
                "reviewed_setting_keys": [row["review_key"] for row in detail["settings"]], "comment": "确认全部内容"}})
        self.assertEqual(response.status_code, 201, response.data)
        prop = PropBible.objects.get(project=self.project); self.shots[0].props.add(prop)
        image = self.asset(); image.kind = "prop"; image.save(update_fields=("kind",))
        setting = prop.variants.get(appearance_role="primary").revisions.get(revision=1)
        self.use(image, setting=setting)
        self.adopt()
        image_url = f"/api/v1/production-runs/{self.run.pk}/manual-bible-image-review/"
        response = self.client.get(image_url); self.assertEqual(response.status_code, 200, response.data)
        detail = response.data
        response = self.post({**base, "action": "approve", "stage_key": "bible_image_review", "idempotency_key": "image-review",
            "payload": {"expected_candidates_sha256": detail["candidates_sha256"], "expected_targets_sha256": detail["targets_sha256"],
                "expected_content_basis_sha256": detail["content_basis_sha256"],
                "comment": "核对真实图片", "bindings": [{"entity_type": "prop", "entity_id": str(prop.pk), "selected_artifact_ids": [],
                    "selected_library_assets": [{"asset_id": str(image.pk), "expected_sha256": image.checksum}],
                    "anchor_artifact_id": None, "anchor_library_asset_id": str(image.pk)}]}})
        self.assertEqual(response.status_code, 201, response.data)
        shot = self.shots[0]
        detail = self.review(); self.assertTrue(detail["ready"], detail["reason"])
        self.assertIsNotNone(detail["contract"]["manual_basis"]["bible_review"])
        self.assertIsNone(detail["contract"]["manual_basis"]["bible_image_review"])
        self.assertEqual(self.post(self.approve_body()).status_code, 201)
        records = read_reviewed_shot_references(stage=self.run.stages.get(stage_key="media_generation"), shot_id=str(shot.pk))["records"]
        self.assertEqual(records[0]["subject_ref"], {"kind": "prop", "id": str(prop.pk)})
        self.assertEqual(records[0]["source"]["asset_id"], str(image.pk))
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run__production_run=self.run).exists())
        PropBible.objects.filter(pk=prop.pk).update(material="New material after review")
        self.assertFalse(self.review()["ready"])
        image_state = self.client.get(image_url).data
        self.assertEqual(image_state["approval"]["status"], "changed")
        self.assertIn("人物、场景或道具设定", image_state["approval"]["changes"])
        image_stage = self.run.stages.get(stage_key="bible_image_review")
        original_image_review = deepcopy(image_stage.output_snapshot)
        impact = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/", {"stage_key": "script_input"})
        self.assertEqual(impact.status_code, 200, impact.data)
        self.assertEqual(impact.data["replacement"]["code"], "review_only")
        response = self.post({**base, "expected_selection_revision": 1, "idempotency_key": "readopt-after-library-image"})
        self.assertEqual(response.status_code, 201, response.data)
        image_state = self.client.get(image_url).data
        self.assertIn("本轮采用剧本", image_state["approval"]["changes"])
        image_stage.refresh_from_db()
        self.assertEqual(image_stage.output_snapshot, original_image_review)
        with self.assertRaises(ValidationError):
            read_reviewed_shot_references(stage=self.run.stages.get(stage_key="media_generation"), shot_id=str(shot.pk))
        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run__production_run=self.run).exists())

    def test_stale_incomplete_and_receipt_failure_do_not_leave_approval(self):
        use = self.use(self.asset())
        self.adopt(); body = self.approve_body()
        for value in ([], body["payload"]["reviewed_shot_ids"] * 2):
            bad = deepcopy(body); bad["payload"]["reviewed_shot_ids"] = value
            self.assertEqual(self.post(bad).status_code, 400)
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=ValidationError("receipt failed")):
            self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(ApprovalDecision.objects.count(), 1)
        self.assertEqual(ProductionManualCommand.objects.count(), 1)
        replacement = self.asset()
        ShotReferenceUse.objects.filter(pk=use.pk).update(asset=replacement, asset_sha256=replacement.checksum)
        self.assertEqual(self.post(body).status_code, 409)
        fresh = self.approve_body()
        self.run.stages.filter(stage_key="media_generation").update(status="running")
        self.assertEqual(self.post(fresh).status_code, 400)
        self.assertEqual(self.run.stages.get(stage_key="storyboard_review").status, "pending")

    def test_changed_creative_content_requires_readoption_and_preserves_original_review(self):
        self.adopt(); self.assertEqual(self.post(self.approve_body()).status_code, 201)
        stage = self.run.stages.get(stage_key="storyboard_review"); original = deepcopy(stage.output_snapshot)
        self.shots[0].title = "Changed creative facts"; self.shots[0].save(update_fields=("title",))
        detail = self.review()
        self.assertFalse(detail["ready"]); self.assertEqual(detail["approval"]["status"], "changed")
        self.assertIn("采用", detail["reason"])
        stage.refresh_from_db(); self.assertEqual(stage.output_snapshot, original)
        self.assertEqual(self.client.get(self.review_url, {"page": 1}).status_code, 400)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.review_url).status_code, 404)

    def test_review_only_storyboard_can_be_readopted_then_explicitly_rereviewed(self):
        self.adopt(); first_review = self.post(self.approve_body())
        self.assertEqual(first_review.status_code, 201, first_review.data)
        stage = self.run.stages.get(stage_key="storyboard_review")
        original_snapshot = deepcopy(stage.output_snapshot)
        original_decision = stage.approval_decisions.get()
        old_request = {**self.approve_body(), "idempotency_key": str(uuid4())}
        self.shots[0].title = "用户重新核对后的手工分镜"
        self.shots[0].save(update_fields=("title",))
        url = f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/"
        impact = self.client.get(url, {"stage_key": "storyboard"})
        self.assertEqual(impact.status_code, 200, impact.data)
        self.assertEqual(impact.data["replacement"]["code"], "review_only")
        self.assertTrue(impact.data["replacement"]["allowed"])
        changed = self.post({**self.request_body(), "idempotency_key": str(uuid4())})
        self.assertEqual(changed.status_code, 201, changed.data)
        state = self.review()
        self.assertTrue(state["ready"])
        self.assertEqual(state["approval"]["status"], "changed")
        stage.refresh_from_db(); self.assertEqual(stage.output_snapshot, original_snapshot)
        original_decision.refresh_from_db()
        self.assertEqual(original_decision.output_snapshot_sha256, stage.output_snapshot_sha256)
        with self.assertRaises(ValidationError):
            read_reviewed_storyboard(run=self.run, eligible_stage_ids=[stage.pk])
        self.assertEqual(self.post(old_request).status_code, 409)
        new_body = self.approve_body()
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=ValidationError("receipt failed")):
            self.assertEqual(self.post(new_body).status_code, 400)
        stage.refresh_from_db(); self.assertEqual(stage.output_snapshot, original_snapshot)
        self.assertEqual(self.post(new_body).status_code, 201)
        current = self.review()
        self.assertEqual(current["approval"]["status"], "current")
        self.assertEqual(current["approval"]["revision"], 2)
        result = read_reviewed_storyboard(run=self.run, eligible_stage_ids=[stage.pk])
        self.assertEqual(result["review"]["decision_id"], current["approval"]["decision_id"])
        self.assertEqual(result["canonical"]["episodes"][0]["shots"][0]["title"], self.shots[0].title)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_review_only_permission_is_rechecked_at_write_and_rejects_corrupt_basis(self):
        self.adopt(); self.assertEqual(self.post(self.approve_body()).status_code, 201)
        self.shots[0].title = "Changed"; self.shots[0].save(update_fields=("title",))
        body = {**self.request_body(), "idempotency_key": str(uuid4())}
        impact_url = f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/"
        self.assertTrue(self.client.get(impact_url, {"stage_key": "storyboard"}).data["replacement"]["allowed"])
        # New downstream activity after the read does not inherit that permission.
        self.run.stages.filter(stage_key="media_generation").update(status="running")
        self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(self.read()["adoption"]["revision"], 1)
        self.run.stages.filter(stage_key="media_generation").update(status="pending")
        stage = self.run.stages.get(stage_key="storyboard_review")
        saved = deepcopy(stage.output_snapshot)
        saved["manual_basis"]["storyboard_adoption"]["decision_id"] = str(uuid4())
        assign_json_contract(stage, "output_snapshot", saved, schema_version=1); stage.save()
        stage.approval_decisions.update(output_snapshot_sha256=stage.output_snapshot_sha256)
        impact = self.client.get(impact_url, {"stage_key": "storyboard"})
        self.assertEqual(impact.status_code, 200, impact.data)
        self.assertEqual(impact.data["replacement"]["code"], "inconsistent_records")
        self.assertFalse(impact.data["replacement"]["allowed"])
        self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(self.read()["adoption"]["revision"], 1)

    def test_missing_or_unowned_original_review_basis_is_not_repaired(self):
        self.adopt(); self.assertEqual(self.post(self.approve_body()).status_code, 201)
        stage = self.run.stages.get(stage_key="storyboard_review")
        original = deepcopy(stage.output_snapshot)
        for ref in ({}, {**original["manual_basis"]["storyboard_adoption"], "decision_id": str(uuid4())},
                {**original["manual_basis"]["storyboard_adoption"], "revision": 100}):
            value = deepcopy(original); value["manual_basis"]["storyboard_adoption"] = ref
            assign_json_contract(stage, "output_snapshot", value, schema_version=1); stage.save()
            stage.approval_decisions.update(output_snapshot_sha256=stage.output_snapshot_sha256)
            response = self.client.get(self.review_url); self.assertEqual(response.status_code, 400, response.data)
            stage.refresh_from_db(); self.assertEqual(stage.output_snapshot, value)
        self.assertEqual(ApprovalDecision.objects.count(), 2)
