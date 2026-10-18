"""Formal manual create/replay with real reviews, exact references and local PNG.

The reused workflow fixture records an isolated fake transport. No live calls.
"""
from copy import deepcopy
from uuid import uuid4

from django.test import TestCase, override_settings

from studio.models import AssetReferenceSeries, ExecutionPreparation, GenerationAttempt, GenerationTask, ShotReferenceUse
from studio.services.asset_references import attach_asset
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import clone_execution_draft, update_execution_draft, publish_execution_version
from studio.services.production_reference_mapping import reviewed_reference_item_key
from studio.tests import test_production_manual_media as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ManualGenerationShotSourcesTests(TestCase):
    resolve = fixtures.ManualMediaPreparationTests.resolve
    mark_submitted = fixtures.ManualMediaPreparationTests.mark_submitted
    post = fixtures.ManualMediaPreparationTests.post

    def setUp(self):
        fixtures.ManualMediaPreparationTests.setUp(self)
        draft = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        spec = deepcopy(self.definition.value["configuration"]["inputs"][0])
        spec["semantic_contract"]["allowed_scope_kinds"] = ["shot"]
        draft = update_execution_draft(version_id=draft.pk, expected_revision=draft.revision,
            changes={"inputs": [spec]}, actor=self.user)
        definition = build_execution_definition(draft.pk)
        published = publish_execution_version(version_id=draft.pk, expected_revision=draft.revision,
            expected_definition_sha256=definition.sha256, actor=self.user)
        self.body["payload"].update(execution_instance_version_id=str(published.pk), expected_definition_sha256=definition.sha256)
        series = AssetReferenceSeries.objects.create(owner=self.user, name="Isolated exact manual source", role="primary")
        self.reference, _ = attach_asset(actor=self.user, series_id=series.pk, asset_id=self.asset.pk, source="existing")
        self.use = ShotReferenceUse.objects.create(shot=self.shot, use_key="原貌参考", position=0,
            media_kind="image", purpose="创作描述不等于首帧", source_mode="existing", reference_version=self.reference,
            asset_sha256=self.asset.checksum)
        self.body["payload"]["inputs"]["picture"] = {"items": [{"kind": "asset", "asset_id": str(self.asset.pk),
            "item_key": reviewed_reference_item_key(input_key="picture", shot_id=str(self.shot.pk), reference_key="shot-use:原貌参考"),
            "purpose": None, "scope_ref": deepcopy(self.body["scope"])}]}
        detail = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-storyboard/").data
        response = self.post({**deepcopy(self.body), "action": "adopt", "stage_key": "storyboard",
            "unit_key": f"project:{self.project.pk}", "scope": {"kind": "project", "id": str(self.project.pk)},
            "idempotency_key": "adopt-isolated-shots", "expected_selection_revision": detail["adoption"]["revision"],
            "payload": {"source": {"kind": "existing_storyboard"}, "expected_content_sha256": detail["content_sha256"],
                "reviewed_shot_ids": [str(self.shot.pk)], "comment": "Isolated agent fixture"}})
        self.assertEqual(response.status_code, 201, response.data)
        self.review_url = f"/api/v1/production-runs/{self.run.pk}/manual-storyboard-review/"

    def approve(self):
        detail = self.client.get(self.review_url).data
        self.assertTrue(detail["ready"], detail["reason"])
        response = self.post({**deepcopy(self.body), "action": "approve", "stage_key": "storyboard_review",
            "unit_key": f"project:{self.project.pk}", "scope": {"kind": "project", "id": str(self.project.pk)},
            "idempotency_key": str(uuid4()), "expected_selection_revision": detail["approval"]["revision"],
            "payload": {"expected_contract_sha256": detail["contract_sha256"],
                "reviewed_shot_ids": [str(self.shot.pk)], "comment": "Isolated agent fixture"}})
        self.assertEqual(response.status_code, 201, response.data)
        approval = self.client.get(self.review_url).data["approval"]
        self.body["payload"]["expected_storyboard_review"] = {key: approval[key] for key in ("decision_id", "snapshot_sha256")}

    def test_formal_create_freezes_exact_version_and_replay_does_not_reread_current_choice(self):
        self.approve()
        response = self.post()
        self.assertEqual(response.status_code, 202, response.data)
        prep = ExecutionPreparation.objects.get(pk=response.data["target_id"])
        original = deepcopy(prep.contract.resolved_inputs)
        item = original["inputs"]["logical_values"]["picture"][0]
        self.assertEqual(item["shot_reference"]["source"]["reference_version_id"], str(self.reference.pk))
        self.assertEqual(item["shot_reference"]["purpose"], self.use.purpose)
        self.assertIsNone(item["purpose"])
        self.assertEqual(prep.contract.intent["request"]["requested"]["expected_storyboard_review"], self.body["payload"]["expected_storyboard_review"])
        self.assertIsNone(prep.next_check_at)
        self.use.reference_version = None; self.use.asset = self.asset
        self.use.save(update_fields=("reference_version", "asset"))
        replay = self.post()
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data, response.data)
        prep.contract.refresh_from_db()
        self.assertEqual(prep.contract.resolved_inputs, original)
        fresh = deepcopy(self.body); fresh["idempotency_key"] = "fresh-after-source-change"
        self.assertEqual(self.post(fresh).status_code, 400)
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), self.before)

    def test_missing_unreviewed_stale_forged_and_cross_scope_pins_fail_without_new_preparations(self):
        before = ExecutionPreparation.objects.count()
        self.assertEqual(self.post().status_code, 400)
        self.body["payload"]["expected_storyboard_review"] = {"decision_id": str(uuid4()), "snapshot_sha256": "a" * 64}
        self.assertEqual(self.post().status_code, 400)
        self.approve()
        bads = []
        for pin in (None, {"decision_id": str(uuid4()), "snapshot_sha256": "a" * 64},
                {**self.body["payload"]["expected_storyboard_review"], "snapshot_sha256": "0" * 64},
                {**self.body["payload"]["expected_storyboard_review"], "approved": True}):
            bad = deepcopy(self.body); bad["payload"]["expected_storyboard_review"] = pin; bads.append(bad)
        for scope in ({"kind": "request"}, {"kind": "shot", "id": str(uuid4())}):
            bad = deepcopy(self.body); bad["payload"]["inputs"]["picture"]["items"][0]["scope_ref"] = scope; bads.append(bad)
        bad = deepcopy(self.body); bad["payload"]["inputs"]["picture"]["items"][0]["item_key"] = "unreviewed"; bads.append(bad)
        for bad in bads:
            with self.subTest(payload=bad["payload"]):
                self.assertEqual(self.post(bad).status_code, 400)
        self.assertEqual(ExecutionPreparation.objects.count(), before)
        self.use.purpose = "changed with the same Asset"
        self.use.save(update_fields=("purpose",))
        self.assertEqual(self.post().status_code, 400)
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), self.before)
