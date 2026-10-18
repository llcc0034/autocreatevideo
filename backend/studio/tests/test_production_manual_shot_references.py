"""Formal single-shot selection commands, isolated media and no execution."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from studio.models import ApprovalDecision, ExecutionPreparation, GenerationAttempt, ProductionManualCommand, Shot, ShotReferenceUse
from studio.tests import test_execution_instances as registry
from studio.tests import test_production_manual_storyboard as storyboard
from studio.tests import test_shot_reference_sources as media


class ManualShotReferenceTests(TestCase):
    create = storyboard.ProductionManualStoryboardTests.create
    read = storyboard.ProductionManualStoryboardTests.read
    request_body = storyboard.ProductionManualStoryboardTests.request_body
    post = storyboard.ProductionManualStoryboardTests.post
    media = media.ShotReferenceSourceTests.media
    setUpTestData = classmethod(registry.ExecutionRegistryTests.setUpTestData.__func__)

    def setUp(self):
        storyboard.ProductionManualStoryboardTests.setUp(self)
        self.owner = self.actor
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(override_settings(MEDIA_ROOT=str(self.root), ASSET_LIBRARY_ROOT=self.root / "library"))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("No network")))
        self.asset = self.media()
        self.uses = [ShotReferenceUse.objects.create(shot=shot, use_key="costume", position=0,
            media_kind="image", purpose="explicit costume") for shot in self.shots]
        adopted = self.post(self.request_body())
        self.assertEqual(adopted.status_code, 201, adopted.data)
        self.detail_url = f"/api/v1/production-runs/{self.run.pk}/manual-shot-references/"

    def detail(self, shot=None):
        result = self.client.get(self.detail_url, {"shot_id": str((shot or self.shots[0]).pk)})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result["Cache-Control"], "private, no-store")
        return result.data

    def selection(self, index=0, key="choose", asset=None):
        state = self.detail(self.shots[index])
        asset = asset or self.asset
        return {**self.body, "action": "select", "stage_key": "storyboard_review",
            "unit_key": f"shot:{self.shots[index].pk}", "scope": {"kind": "shot", "id": str(self.shots[index].pk)},
            "idempotency_key": key, "expected_selection_revision": state["storyboard_adoption_revision"],
            "payload": {"expected_storyboard_adoption_sha256": state["storyboard_adoption_sha256"],
                "expected_uses_sha256": state["uses_sha256"], "choices": [{"use_id": str(self.uses[index].pk),
                    "source_mode": "upload", "reference_version_id": None,
                    "asset_id": str(asset.pk), "asset_sha256": asset.checksum}]}}

    def preparation_fixture(self, **changes):
        # Storage-only fixture to exercise readonly scope, not executed media.
        version = registry.ExecutionRegistryTests.version(self)
        version.save()
        return ExecutionPreparation.objects.create(stage_run=self.run.stages.get(stage_key="media_generation"),
            execution_instance_version=version, purpose="production", unit_key=f"shot:{self.shots[0].pk}",
            request_key="isolated-existing-prep", created_by=self.actor, intent_sha256="a" * 64,
            **{"status": "awaiting_input", **changes})

    def test_catalog_and_read_baseline_do_not_probe_or_write(self):
        with CaptureQueriesContext(connection) as queries, patch(
                "studio.services.shot_reference_sources.resolve_execution_asset_media", side_effect=AssertionError("No file reads")):
            state = self.detail()
        self.assertTrue(state["editability"]["allowed"])
        self.assertEqual(state["state"]["uses"][0]["source_mode"], "")
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        options = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-action-options/")
        self.assertEqual(options.status_code, 200, options.data)
        action = next(row for row in options.data["actions"] if row["action"] == "select")
        self.assertEqual((action["stage_key"], action["scope_kinds"]), ("storyboard_review", ["shot"]))

    def test_choice_is_not_approval_or_advance_and_replay_does_not_rewrite_later_choice(self):
        original = list(self.run.stages.values())
        body = self.selection()
        first = self.post(body)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual((first.data["result_kind"], str(first.data["target_id"])), ("shot_reference_choices", str(self.shots[0].pk)))
        changed = self.selection(key="change", asset=self.media())
        self.assertEqual(self.post(changed).status_code, 201)
        with patch("studio.services.production_manual_shot_references.apply_shot_reference_choices", side_effect=AssertionError("No replay write")):
            replay = self.post(body)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data, first.data)
        self.uses[0].refresh_from_db(); self.uses[1].refresh_from_db()
        self.assertEqual(str(self.uses[0].asset_id), changed["payload"]["choices"][0]["asset_id"])
        self.assertIsNone(self.uses[1].asset_id)
        self.assertEqual(list(self.run.stages.values()), original)
        self.assertEqual(self.read()["adoption"]["status"], "current")
        self.assertEqual(ApprovalDecision.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_old_baseline_new_key_and_reused_key_changed_payload_conflict(self):
        body = self.selection()
        self.assertEqual(self.post(body).status_code, 201)
        self.assertEqual(self.post({**body, "idempotency_key": "old-new-key"}).status_code, 409)
        changed = deepcopy(body); changed["payload"]["choices"][0]["source_mode"] = "existing"
        self.assertEqual(self.post(changed).status_code, 409)
        self.assertEqual(ProductionManualCommand.objects.filter(action="select").count(), 1)

    def test_receipt_failure_rolls_back_selection(self):
        body = self.selection()
        before = self.detail()["uses_sha256"]
        original = ProductionManualCommand.save
        def fail(receipt, *args, **kwargs):
            if receipt.action == "select":
                raise RuntimeError("isolated receipt failure")
            return original(receipt, *args, **kwargs)
        with patch.object(ProductionManualCommand, "save", fail), self.assertRaisesRegex(RuntimeError, "receipt failure"):
            self.post(body)
        self.assertEqual(self.detail()["uses_sha256"], before)
        self.assertFalse(ProductionManualCommand.objects.filter(action="select").exists())

    def test_stale_creative_adoption_and_revision_do_not_choose(self):
        body = self.selection()
        self.assertEqual(self.post({**body, "expected_selection_revision": 0}).status_code, 409)
        Shot.objects.filter(pk=self.shots[0].pk).update(title="changed authored content")
        self.assertEqual(self.detail()["editability"]["code"], "storyboard_not_current")
        self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(ProductionManualCommand.objects.filter(action="select").exists())

    def test_pending_preparation_only_locks_its_shot(self):
        body = self.selection()
        self.preparation_fixture()
        self.assertEqual(self.detail()["editability"]["code"], "shot_prepared")
        self.assertEqual(self.post(body).status_code, 400)
        self.assertTrue(self.detail(self.shots[1])["editability"]["allowed"])
        second = self.post(self.selection(1))
        self.assertEqual(second.status_code, 201, second.data)

    def test_explicitly_stopped_unused_preparation_does_not_block_reselection(self):
        self.preparation_fixture(status="cancelled", cancelled_at=timezone.now(), cancelled_by=self.actor)
        result = self.post(self.selection())
        self.assertEqual(result.status_code, 201, result.data)

    def test_cancelled_frozen_preparation_remains_readonly(self):
        self.preparation_fixture(status="cancelled", cancelled_at=timezone.now(),
            request_frozen_at=timezone.now(), request_sha256="b" * 64)
        self.assertFalse(self.detail()["editability"]["allowed"])
        self.assertEqual(self.post(self.selection()).status_code, 400)

    def test_scope_payload_asset_and_use_identity_are_not_client_override_permissions(self):
        body = self.selection()
        bad_cases = []
        for key, value in (("expected_storyboard_adoption_sha256", "0" * 64), ("approve", True)):
            bad = deepcopy(body); bad["payload"][key] = value; bad_cases.append(bad)
        bad = deepcopy(body); bad["payload"]["choices"][0]["use_id"] = str(self.uses[1].pk); bad_cases.append(bad)
        bad = deepcopy(body); bad["payload"]["choices"][0]["asset_sha256"] = "0" * 64; bad_cases.append(bad)
        bad = deepcopy(body); bad["scope"] = {"kind": "project", "id": str(self.project.pk)}; bad_cases.append(bad)
        bad = deepcopy(body); bad["unit_key"] = "untrusted"; bad_cases.append(bad)
        for bad in bad_cases:
            with self.subTest(payload=bad):
                self.assertIn(self.post(bad).status_code, (400, 409))
        self.assertFalse(ProductionManualCommand.objects.filter(action="select").exists())

    def test_query_owner_and_frozen_episode_scope_are_enforced(self):
        for query in ("", "?shot_id=bad", f"?shot_id={self.shots[0].pk}&shot_id={self.shots[1].pk}",
                f"?shot_id={self.shots[0].pk}&allow_override=true", f"?shot_id={uuid4()}"):
            self.assertEqual(self.client.get(self.detail_url + query).status_code, 400)
        outside = Shot.objects.create(episode=self.outside, number=1, duration_ms=6000,
            media_type="image", include_audio=False)
        self.assertEqual(self.client.get(self.detail_url, {"shot_id": str(outside.pk)}).status_code, 400)
        body = self.selection()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.detail_url, {"shot_id": str(self.shots[0].pk)}).status_code, 404)
        self.assertEqual(self.post(body).status_code, 404)

    def test_selection_alone_does_not_block_re_adopting_changed_creative_content(self):
        self.assertEqual(self.post(self.selection()).status_code, 201)
        Shot.objects.filter(pk=self.shots[0].pk).update(title="explicit new authored content")
        body = {**self.request_body(), "idempotency_key": "new-adoption"}
        result = self.post(body)
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.detail()["storyboard_adoption_revision"], 2)
        self.uses[0].refresh_from_db()
        self.assertEqual(self.uses[0].asset_id, self.asset.pk)
        self.assertEqual(ApprovalDecision.objects.count(), 2)

    def test_closed_run_blocks_new_choice_but_keeps_original_replay_receipt(self):
        body = self.selection()
        self.assertEqual(self.post(body).status_code, 201)
        type(self.run).objects.filter(pk=self.run.pk).update(status="cancelled")
        self.assertEqual(self.detail()["editability"]["code"], "run_closed")
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.post(self.selection(key="new")).status_code, 400)

    def test_missing_file_fails_new_selection_without_receipt(self):
        body = self.selection()
        Path(self.asset.file.path).unlink()  # Only this test's disposable upload.
        result = self.post(body)
        self.assertEqual(result.status_code, 400, result.data)
        self.assertFalse(ProductionManualCommand.objects.filter(action="select").exists())
        self.uses[0].refresh_from_db()
        self.assertIsNone(self.uses[0].asset_id)
