"""Public field actions use real frozen definitions and owner-bound preparation."""
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import AssetGenerationJob, ExecutionFieldValue
from studio.tests import test_execution_profile_preparation as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionPreparationApiTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionProfilePreparationTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ExecutionProfilePreparationTests.write_contract)
    input = staticmethod(fixtures.ExecutionProfilePreparationTests.input)
    bind = fixtures.ExecutionProfilePreparationTests.bind
    resolve = fixtures.ExecutionProfilePreparationTests.resolve
    prepare = fixtures.ExecutionProfilePreparationTests.prepare

    def setUp(self):
        fixtures.ExecutionProfilePreparationTests.setUp(self)
        self.prepare()
        self.actor.is_staff = False
        self.actor.save(update_fields=("is_staff",))
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.base = "/api/v1/execution-field-values/"
        self.preparation_url = f"/api/v1/execution-preparations/{self.prep.pk}/"

    def revision(self):
        self.prep.refresh_from_db()
        return self.prep.revision

    def create(self, revision=1, text="A courier opens a blue parcel in soft daylight.", **overrides):
        return self.client.post(self.base, {"preparation_id": str(self.prep.pk), "unit_key": "asset-unit",
            "field_key": "composite_direction", "text": text, "revision": revision,
            "expected_preparation_revision": self.revision(), **overrides}, format="json")

    def review(self, field, action="confirm", **overrides):
        return self.client.post(self.base + str(field["id"]) + f"/{action}/", {
            "expected_text_sha256": field["text_sha256"], "expected_source_sha256": field["source_sha256"],
            "expected_preparation_revision": self.revision(), **overrides}, format="json")

    def test_read_and_explicit_confirm_select_do_not_dispatch_or_freeze(self):
        response = self.client.get(self.preparation_url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["resolved_inputs"]["field_requests"][0]["value"]["operation"], "prompt.fields.generate")
        self.assertIsNone(response.data["prepared_request"])
        self.assertIsNone(response.data["response_diagnostic"])
        self.assertEqual(response.data["review_summary"]["mode"], "manual")
        self.assertEqual(response.data["review_summary"]["label"], "人工确认字段")
        created = self.create()
        self.assertEqual(created.status_code, 201, created.data)
        value = created.data["field"]
        self.assertEqual(value["status"], "pending")
        self.assertIsNone(created.data["selection"])
        confirmed = self.review(value)
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["field"]["approval_kind"], "human")
        self.assertEqual(confirmed.data["field"]["approval_label"], "人工审核")
        self.assertFalse(self.prep.field_selections.exists())
        selected = self.review(value, select_for_execution=True)
        self.assertEqual(selected.status_code, 200, selected.data)
        self.assertEqual(selected.json()["selection"]["field_value"], value["id"])
        reloaded = self.client.get(self.preparation_url)
        self.assertEqual(reloaded.status_code, 200, reloaded.data)
        self.assertEqual(reloaded.json()["field_selections"], [selected.json()["selection"]])
        self.assertEqual(self.prep.field_values.count(), 1)
        self.assertEqual(AssetGenerationJob.objects.count(), 1)
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.status, "preparing")
        self.assertIsNone(self.prep.request_frozen_at)

    def test_list_is_scoped_and_excludes_text_detail_includes_it(self):
        field = self.create().data["field"]
        response = self.client.get(self.base, {"preparation_id": str(self.prep.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertNotIn("text", response.data["results"][0])
        detail = self.client.get(self.base + field["id"] + "/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["text"], field["text"])
        self.assertEqual(self.client.get(self.base).status_code, 400)
        self.assertEqual(self.client.get(self.base, {"preparation_id": "bad-uuid"}).status_code, 400)

    def test_asset_outcome_matches_task_center_without_writes_or_dispatch(self):
        from hashlib import sha256
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from studio.models import ExecutionPreparation
        center_url = f"/api/v1/task-center/preparation/{self.prep.pk}/"
        for prep_status, status, message in (
            ("blocked", "preparing", "输入准备受阻"),
            ("preparing", "failed", "主调用失败"),
            ("preparing", "succeeded", "未读取到关联产物"),
            ("preparing", "uncertain", "远端结果尚不确定"),
            ("cancelled", "submitted", "停止准备不等于远端已经结束"),
            ("cancelled", "cancelled", "不会继续准备"),
        ):
            with self.subTest(status=status, prep=prep_status):
                # Isolated stored-state matrix: no claim, remote request or adoption.
                AssetGenerationJob.objects.filter(pk=self.job.pk).update(status=status)
                ExecutionPreparation.objects.filter(pk=self.prep.pk).update(status=prep_status)
                before = AssetGenerationJob.objects.values().get(pk=self.job.pk)
                prep_before = ExecutionPreparation.objects.values().get(pk=self.prep.pk)
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(self.preparation_url)
                    center = self.client.get(center_url)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(center.status_code, 200, center.data)
                self.assertEqual(response.data["outcome"], center.data["outcome"])
                self.assertIn(message, center.data["outcome"]["message"])
                self.assertEqual(center.data["status"], status)
                self.assertEqual(center.data["outcome"]["artifact_count"], 0)
                self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
                self.assertEqual(before, AssetGenerationJob.objects.values().get(pk=self.job.pk))
                self.assertEqual(prep_before, ExecutionPreparation.objects.values().get(pk=self.prep.pk))
        text = "isolated text result"
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(status="succeeded", result_text=text,
            result_text_size_bytes=len(text.encode()), result_text_sha256=sha256(text.encode()).hexdigest())
        ExecutionPreparation.objects.filter(pk=self.prep.pk).update(status="preparing")
        center = self.client.get(center_url).data
        self.assertIn("已保存文本结果", center["outcome"]["message"])
        self.assertEqual(center["output"]["text"], text)
        self.assertEqual(center["outcome"]["artifact_count"], 0)
        self.assertEqual(AssetGenerationJob.objects.count(), 1)

    def test_field_form_catalog_reads_frozen_rules_and_server_next_revision_without_creating_values(self):
        before = self.client.get(self.preparation_url)
        slot = next(row for row in before.data["field_slots"] if row["field_key"] == "composite_direction")
        self.assertEqual(slot["unit_key"], "asset-unit")
        self.assertEqual(slot["next_revision"], 1)
        self.assertIsNone(slot["latest_value_id"])
        self.assertTrue(slot["field_label"])
        rule = before.data["resolved_inputs"]["field_requests"][0]["value"]["items"][0]["field_contract"]
        self.assertEqual(slot["field_contract"], rule)
        self.assertEqual(self.prep.field_values.count(), 0)
        created = self.create()
        self.assertEqual(created.status_code, 201)
        second = self.create(revision=2)
        self.assertEqual(second.status_code, 201)
        after = self.client.get(self.preparation_url)
        updated = next(row for row in after.data["field_slots"] if row["field_key"] == "composite_direction")
        self.assertEqual(updated["next_revision"], 3)
        self.assertEqual(str(updated["latest_value_id"]), second.data["field"]["id"])
        self.assertEqual(updated["field_contract"], slot["field_contract"])
        self.assertEqual(after.data["field_selections"], [])
        self.assertIsNone(after.data["prepared_request"])
        self.assertEqual(self.prep.field_values.count(), 2)

    def test_field_language_display_obeys_frozen_policy_not_project_target(self):
        from studio.services.execution_preparation_fields import preparation_field_language
        for policy, expected in [("en", "en"), ("inherit", "Chinese"), ("preserve_source", "Japanese"),
                                 ("any", "any"), ("bilingual:zh-CN+en", "bilingual:zh-CN+en")]:
            with self.subTest(policy=policy):
                self.assertEqual(preparation_field_language({"field_contract": {"language_policy": policy},
                    "source_language": "Japanese", "target_language": "Chinese"}), expected)

    def test_invalid_manual_text_is_a_form_error_not_500_or_a_saved_revision(self):
        before = self.revision()
        result = self.create(text="A courier \x01 opens the blue parcel.")
        self.assertEqual(result.status_code, 400, result.data)
        self.assertEqual(self.revision(), before)
        self.assertFalse(self.prep.field_values.exists())

    def test_other_owner_cannot_read_or_write_even_with_known_identifiers(self):
        field = self.create().data["field"]
        other = get_user_model().objects.create_user(username="other-field-owner")
        self.client.force_authenticate(other)
        for url in (self.preparation_url, self.base + field["id"] + "/"):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(self.base, {"preparation_id": str(self.prep.pk)}).status_code, 404)
        self.assertEqual(self.create().status_code, 404)
        self.assertEqual(self.review(field, select_for_execution=True).status_code, 404)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.preparation_url).status_code, (401, 403))
        self.client.force_authenticate(self.actor)
        self.actor.is_active = False
        self.assertEqual(self.create().status_code, 403)

    def test_closed_payload_cannot_forge_origin_or_approval(self):
        for override in ({"origin_kind": "ai"}, {"status": "confirmed"}, {"origin_attempt": str(uuid4())},
                {"revision": True}, {"expected_preparation_revision": "1"}):
            with self.subTest(override=override):
                self.assertEqual(self.create(**override).status_code, 400)
        self.assertFalse(self.prep.field_values.exists())
        field = self.create().data["field"]
        for override in ({"select_for_execution": "true"}, {"approval_kind": "policy"}, {"replace": True},
                {"expected_selection_revision": 1}):
            with self.subTest(override=override):
                self.assertEqual(self.review(field, **override).status_code, 400)
        self.assertFalse(self.prep.field_selections.exists())

    def test_same_revision_receipt_and_exact_confirm_selection_are_idempotent(self):
        created = self.create()
        field = created.data["field"]
        old_revision = created.data["preparation"]["revision"]
        response = self.review(field, select_for_execution=True)
        self.assertEqual(response.status_code, 200, response.data)
        chosen = response.data["selection"]
        revision = self.revision()
        replay = self.review(field, select_for_execution=True, expected_preparation_revision=old_revision)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["selection"], chosen)
        self.assertEqual(self.revision(), revision)
        self.assertEqual(self.create(expected_preparation_revision=1).status_code, 200)
        self.assertEqual(self.create(text="Different text.").status_code, 409)
        self.assertEqual(self.prep.field_values.count(), 1)

    def test_confirmed_but_unselected_cannot_use_stale_revision_for_new_selection(self):
        field = self.create().data["field"]
        stale = self.revision()
        self.assertEqual(self.review(field).status_code, 200)
        response = self.review(field, select_for_execution=True, expected_preparation_revision=stale)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(self.prep.field_selections.exists())

    def test_replacement_conflict_rolls_back_confirmation_and_requires_explicit_revision(self):
        first = self.create().data["field"]
        selected = self.review(first, select_for_execution=True).data["selection"]
        second = self.create(revision=2, text="A courier cautiously unwraps a blue parcel.").data["field"]
        revision = self.revision()
        for extra in ({}, {"replace": True, "expected_selection_revision": 2}):
            response = self.review(second, select_for_execution=True, **extra)
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(ExecutionFieldValue.objects.get(pk=second["id"]).status, "pending")
            self.assertEqual(self.revision(), revision)
        response = self.review(second, select_for_execution=True, replace=True,
            expected_selection_revision=selected["revision"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["selection"]["revision"], 2)
        self.assertEqual(response.json()["selection"]["field_value"], second["id"])

    def test_reject_is_explicit_and_does_not_rewrite_approval(self):
        field = self.create().data["field"]
        mismatch = self.review(field, expected_source_sha256="0" * 64)
        self.assertEqual(mismatch.status_code, 409)
        response = self.review(field, action="reject")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["field"]["status"], "rejected")
        self.assertIsNone(response.data["field"]["approval_kind"])
        self.assertEqual(self.review(field, action="reject").status_code, 200)
        self.assertEqual(self.review(field).status_code, 409)

    def test_no_public_delete_patch_or_preparation_creation(self):
        field = self.create().data["field"]
        self.assertEqual(self.client.delete(self.base + field["id"] + "/").status_code, 405)
        self.assertEqual(self.client.patch(self.base + field["id"] + "/", {"text": "changed"}, format="json").status_code, 405)
        self.assertEqual(self.client.post(self.preparation_url, {}, format="json").status_code, 405)
