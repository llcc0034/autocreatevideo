from studio.tests.field_definition_fixtures import get_or_create_field_definition
"""Isolated human decisions through the same frozen field services; no models."""
from uuid import uuid4
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import Episode, SeriesProject, Shot, ProductionRun, StageRun, ExecutionFieldSelection
from studio.services.execution_preparation_fields import create_manual_execution_field
from studio.tests import test_execution_preparation_fields as fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class ExecutionBulkFieldReviewTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionPreparationFieldTests.setUpTestData.__func__)
    smoke = fixtures.ExecutionPreparationFieldTests.smoke
    preparation = fixtures.ExecutionPreparationFieldTests.preparation
    prepared = fixtures.ExecutionPreparationFieldTests.prepared
    create = fixtures.ExecutionPreparationFieldTests.create
    review = fixtures.ExecutionPreparationFieldTests.review
    select = fixtures.ExecutionPreparationFieldTests.select

    def setUp(self):
        fixtures.ExecutionPreparationFieldTests.setUp(self)
        # These rows belong only to this rollback database, never the active Run.
        self.version.prompt_profile_version = self.profile
        self.version.prompt_mode = "profile"
        self.version.operation = "video.reference_to_video"
        self.version.save()
        self.project = SeriesProject.objects.create(owner=self.actor, title="Bulk review fixture", **PROJECT_CONFIG)
        self.episode = Episode.objects.create(project=self.project, number=1, title="Test")
        self.run = ProductionRun.objects.create(owner=self.actor, project=self.project, status="running", plan_source="manual",
            idempotency_key=str(uuid4()), request_sha256="a" * 64)
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", mode="existing_pipeline",
            status="running", ordinal=1, idempotency_key=str(uuid4()))
        self.preps = [self.shot_preparation(1), self.shot_preparation(2)]
        self.client = APIClient(); self.client.force_authenticate(self.actor)
        self.base = "/api/v1/execution-preparations/"

    def shot_preparation(self, number, *, keys=("prepared_direction",), with_values=True):
        from studio.models import ExecutionPreparationContract
        from studio.services.execution_field_contracts import ExecutionFieldInput, freeze_execution_field_request
        from studio.services.execution_preparation_fields import frozen_preparation_field_requests
        from studio.services.json_contracts import assign_json_contract
        shot = Shot.objects.create(episode=self.episode, number=number, title="Review", duration_ms=3000,
            media_type="video", include_audio=True)
        unit = f"shot:{shot.pk}"
        prep = self.preparation(deployment_smoke=None, stage_run=self.stage, purpose="production",
            execution_instance_version=self.version, unit_key=unit)
        request = freeze_execution_field_request(operation="prompt.fields.generate", profile=self.profile,
            inputs=[ExecutionFieldInput(request_id=f"field-{key}", unit_key=unit, field_key=key,
                source_text="A red cube turns.", source_language="English", target_language="English", context_facts={},
                reference_facts={"protected_tokens": [], "logical_inputs": [], "subject_facts": []}) for key in keys])
        contract = ExecutionPreparationContract(preparation=prep)
        for name, value in (("intent", {"fixture": True}),
            ("resolved_inputs", {"schema_version": 1, "field_requests": frozen_preparation_field_requests([request])}),
            ("review_contract", {"schema_version": 1, "mode": "manual"})):
            assign_json_contract(contract, name, value, max_bytes=65536)
        contract.save()
        if with_values:
            for key in keys:
                self.add_value(prep, key=key)
        return prep

    def add_value(self, prep, *, revision=1, text="A red cube turns.", key="prepared_direction"):
        prep.refresh_from_db()
        return create_manual_execution_field(preparation_id=prep.pk, unit_key=prep.unit_key,
            field_key=key, text=text, revision=revision,
            expected_preparation_revision=prep.revision, actor=self.actor)

    def preview(self, preps=None):
        return self.client.post(self.base + "bulk-field-preview/", {
            "preparation_ids": [str(row.pk) for row in (preps or self.preps)]}, format="json")

    def receipts(self):
        response = self.preview()
        self.assertEqual(response.status_code, 200, response.data)
        return [{key: row[key] for key in ("preparation_id", "expected_preparation_revision", "preview_sha256")}
            for row in response.data["items"]]

    def confirm(self, items):
        return self.client.post(self.base + "bulk-field-confirm/", {"items": items}, format="json")

    def test_preview_readonly_and_atomic_confirmation_keeps_human_audit(self):
        from studio.models import FieldDefinition
        from studio.tests.field_definition_fixtures import publish_test_field_version
        publish_test_field_version(FieldDefinition.objects.get(key="prepared_direction").pk,
            {"label": "后续版本名称", "category": "test", "value_type": "long_text"})
        before = [list(row.field_values.values()) for row in self.preps]
        preview = self.preview()
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview["Cache-Control"], "private, no-store")
        self.assertTrue(all(row["ready"] for row in preview.data["items"]))
        self.assertEqual(preview.data["items"][0]["label"], "第1集 · 镜头1")
        self.assertEqual(preview.data["items"][0]["fields"][0]["label"], "Direction")
        self.assertEqual(before, [list(row.field_values.values()) for row in self.preps])
        self.assertFalse(ExecutionFieldSelection.objects.exists())
        items = self.receipts()
        response = self.confirm(items)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["confirmed_field_count"], 2)
        for prep in self.preps:
            value = prep.field_values.get()
            self.assertEqual((value.status, value.approval_kind, value.reviewed_by), ("confirmed", "human", self.actor))
            self.assertEqual(prep.field_selections.get().selected_by, self.actor)
            prep.refresh_from_db(); self.assertIsNone(prep.request_frozen_at)
        self.assertEqual(self.confirm(items).status_code, 409)
        self.assertEqual(ExecutionFieldSelection.objects.count(), 2)

    def test_stale_second_shot_rolls_back_entire_batch(self):
        receipts = self.receipts()
        self.add_value(self.preps[1], revision=2, text="A new revision.")
        self.assertEqual(self.confirm(receipts).status_code, 409)
        self.assertFalse(ExecutionFieldSelection.objects.exists())
        self.assertEqual(self.preps[0].field_values.get().status, "pending")

    def test_single_shot_confirms_all_three_fields_in_one_transaction(self):
        from studio.models import PromptProfileVersion, FieldDefinition, PromptProfileFieldRule
        from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
        # Build a separate immutable three-field contract before saving any text.
        profile = PromptProfileVersion.objects.create(profile_key="bulk-three-fields", name="Three fields",
            version=1, model_family="fixture", operation="video.generate", mode="T2VA",
            review_state="ready", contract_schema_version=8)
        keys = ("prepared_direction", "prepared_sound", "prepared_music")
        for position, key in enumerate(keys, 1):
            field, _ = get_or_create_field_definition(key=key, defaults={"label": key, "value_type": "long_text"})
            create_profile_field_rule(profile_version=profile, field_definition=field,
                source_selector="request.text", generation_source_selector="request.action",
                generation_mode="model_generated", generation_instruction="Describe only supplied facts.",
                generation_context_selectors=[], channel="positive_prompt", position=position, required=True, max_chars=1000)
        refresh_prompt_profile_contract(profile)
        self.profile = publish_prompt_profile(profile, actor=self.actor)
        self.version.prompt_profile_version = self.profile; self.version.save(update_fields=("prompt_profile_version",))
        prep = self.shot_preparation(3, keys=keys)
        self.preps = [prep]
        response = self.confirm(self.receipts())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["confirmed_field_count"], 3)
        self.assertEqual(prep.field_selections.count(), 3)
        self.assertEqual(prep.field_values.filter(status="confirmed", approval_kind="human", reviewed_by=self.actor).count(), 3)
        prep.refresh_from_db(); self.assertIsNone(prep.request_frozen_at)

    def test_failure_after_first_write_rolls_back_approval_and_selection(self):
        from studio.services.execution_preparation_fields import confirm_execution_field
        from django.core.exceptions import ValidationError
        calls = []
        def fail_second(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise ValidationError("isolated persistence failure")
            return confirm_execution_field(**kwargs)
        receipts = self.receipts()
        with patch("studio.services.execution_field_bulk_review.confirm_execution_field", side_effect=fail_second):
            self.assertEqual(self.confirm(receipts).status_code, 400)
        self.assertFalse(ExecutionFieldSelection.objects.exists())
        self.assertTrue(all(row.field_values.get().status == "pending" for row in self.preps))

    def test_existing_selection_is_not_replaced_or_reapproved(self):
        first = self.receipts()[0]
        self.assertEqual(self.confirm([first]).status_code, 200)
        prep = self.preps[0]
        audit = list(prep.field_values.values())
        chosen = list(prep.field_selections.values())
        self.add_value(prep, revision=2, text="Unselected later text.")
        result = self.preview().data["items"][0]
        self.assertFalse(result["ready"])
        self.assertEqual(result["fields"][0]["state"], "selected")
        self.assertEqual(list(prep.field_selections.values()), chosen)
        self.assertEqual(prep.field_values.filter(revision=1).values().get(), audit[0])

    def test_multiple_candidates_are_explicit_not_latest_wins(self):
        self.add_value(self.preps[0], revision=2, text="A second unselected revision.")
        result = self.preview().data["items"][0]
        self.assertFalse(result["ready"])
        self.assertIn("多个", result["fields"][0]["reason"])
        self.assertIsNone(result["fields"][0]["value_id"])

    def test_missing_frozen_cancelled_and_invalid_content_are_not_ready(self):
        prep = self.shot_preparation(3, with_values=False)
        self.assertFalse(self.preview([prep]).data["items"][0]["ready"])
        frozen, cancelled = self.preps
        frozen.request_frozen_at = timezone.now(); frozen.request_sha256 = "d"*64; frozen.status = "ready"; frozen.save()
        cancelled.status = "cancelled"; cancelled.save(update_fields=("status",))
        self.assertTrue(all(not item["ready"] for item in self.preview().data["items"]))
        value = self.add_value(prep)
        # Simulate damaged storage only in the isolated test database.
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_executionfieldvalue SET text_sha256 = %s WHERE id = %s", ["b"*64, value.pk.hex])
        result = self.preview([prep]).data["items"][0]
        self.assertFalse(result["ready"])
        self.assertIn("校验", result["fields"][0]["reason"])

    def test_changed_receipt_rejected_and_no_unknown_flags(self):
        receipts = self.receipts()
        receipts[0]["preview_sha256"] = "c" * 64
        self.assertEqual(self.confirm(receipts).status_code, 409)
        receipts = self.receipts(); receipts[0]["replace"] = True
        self.assertEqual(self.confirm(receipts).status_code, 400)
        self.assertFalse(ExecutionFieldSelection.objects.exists())

    def test_owner_login_duplicates_and_bounds(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.preview().status_code, 404)
        self.client.force_authenticate(None)
        self.assertIn(self.preview().status_code, (401, 403))
        self.client.force_authenticate(self.actor)
        for ids in ([], [str(self.preps[0].pk)]*2, [str(uuid4()) for _ in range(51)]):
            self.assertEqual(self.client.post(self.base + "bulk-field-preview/", {"preparation_ids": ids}, format="json").status_code, 400)

    def test_cross_stage_auxiliary_duplicate_shot_and_finished_run_rejected(self):
        other = StageRun.objects.create(production_run=self.run, stage_key="artifact_selection", mode="review", ordinal=2, idempotency_key=str(uuid4()))
        for stage, unit in ((other, self.preps[1].unit_key), (self.stage, self.preps[0].unit_key), (self.stage, "field:auxiliary")):
            prep = self.preparation(deployment_smoke=None, stage_run=stage, purpose="production",
                execution_instance_version=self.version, unit_key=unit)
            self.assertEqual(self.preview([self.preps[0], prep]).status_code, 400)
        self.run.status = "cancelled"; self.run.save(update_fields=("status",))
        self.assertEqual(self.preview([self.preps[0]]).status_code, 409)
