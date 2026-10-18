from copy import deepcopy
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from studio.models import ApprovalDecision, Episode, ExecutionPreparation, ExecutionPreparationContract, GenerationAttempt, ProductionManualCommand, Shot, StageRun
from studio.services.production_manual_change_impact import recorded_input_references, started_content_dependents
from studio.services.execution_production_cancellation import cancel_production_preparation
from studio.services.production_script_baselines import read_script_adoption_baseline
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
from studio.tests import test_production_manual_preparations as fixtures


class RecordedInputReferenceTests(SimpleTestCase):
    def setUp(self):
        self.stage, self.script = str(uuid4()), str(uuid4())

    def read(self, origins, values=None, **facts):
        return recorded_input_references({"inputs": {"input_provenance": origins, "logical_values": values or {}}, **facts},
            source_stage_id=self.stage, script_revision_id=self.script)

    def test_structured_automatic_shot_bible_and_speaker_sources_are_explained(self):
        evidence = {"kind": "context_field", "path": "context.shots[0]", "selector_key": "shot.dialogue_with_speakers",
            "content_revision_id": str(uuid4()), "review": {"stage_run_id": str(uuid4())},
            "related_reviews": {"bible": {"stage_run_id": self.stage}}}
        self.assertEqual(self.read([{"input_key": "context", "kind": "structured", "leaves": [evidence]}]),
            [{"input_path": "context.shots[0]", "kind": "related_review"}])
        evidence["review"]["stage_run_id"] = self.stage
        self.assertEqual({r["kind"] for r in self.read([{"input_key": "context", "kind": "structured", "leaves": [evidence]}])},
            {"related_review", "reviewed_content"})

    def test_literal_content_is_not_scanned_for_fake_source_keys_and_baseline_is_distinct(self):
        fake = {"origin": {"kind": "selected_output", "stage_run_id": self.stage, "decision_id": str(uuid4())},
            "source_sha256": "a" * 64, "input_key": "text", "item_key": "fake"}
        self.assertEqual(self.read([{"input_key": "text", "kind": "literal"}], {"text": [fake]}), [])
        self.assertEqual(self.read([], script_adoption_baseline={"script_revision_id": self.script}),
            [{"input_path": "script_adoption_baseline", "kind": "adoption_baseline"}])

    def test_direct_script_selected_text_and_media_use_registered_positions(self):
        context = {"input_key": "script", "kind": "context_field", "selector_key": "script.content", "content_revision_id": self.script}
        selected = {"kind": "selected_output", "stage_run_id": str(uuid4()), "decision_id": str(uuid4()),
            "approval_stage_id": self.stage, "context": context}
        refs = self.read([context, {"input_key": "text", **selected}], {"images": [{"input_key": "images", "item_key": "hero",
            "source_sha256": "b" * 64, "origin": {**selected, "context": None}}]})
        self.assertIn({"input_path": "script", "kind": "script_input"}, refs)
        self.assertIn({"input_path": "text", "kind": "selected_output"}, refs)
        self.assertIn({"input_path": "images/hero", "kind": "selected_output"}, refs)

    def test_incomplete_and_unknown_origins_are_not_reported_as_no_reference(self):
        for item in [{"kind": "new_type"}, {"kind": "structured"}, {"kind": "context_field"}, {"kind": "selected_output"}]:
            with self.subTest(item=item), self.assertRaises(ValidationError):
                self.read([{"input_key": "value", **item}])
        with self.assertRaises(ValidationError):
            self.read([], {"unrecorded": "value"})


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ManualChangeImpactApiTests(TestCase):
    setUpTestData = classmethod(fixtures.ManualPreparationTests.setUpTestData.__func__)
    post = fixtures.ManualPreparationTests.post
    preparation = fixtures.ManualPreparationTests.preparation

    def setUp(self):
        fixtures.ManualPreparationTests.setUp(self)
        self.impact_url = f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/"
        _, preview = create_pasted_script_source(project=self.project, user=self.actor, content="用于影响核对的已确认剧本")
        self.script = confirm_script_revision(preview, user=self.actor)
        self.adopt = {"action": "adopt", "stage_key": "script_input", "unit_key": f"project:{self.project.pk}",
            "scope": {"kind": "project", "id": str(self.project.pk)}, "expected_run_plan_sha256": self.body["expected_run_plan_sha256"],
            "expected_selection_revision": 0, "idempotency_key": "adopt-before-generation", "payload": {"comment": "",
                "content": {"kind": "script_revision", "id": str(self.script.pk), "expected_sha256": self.script.content_sha256}}}
        response = self.post(self.adopt)
        self.assertEqual(response.status_code, 201, response.data)

    def get(self, **query):
        return self.client.get(self.impact_url, {"stage_key": "script_input", **query})

    def stop(self, prep):
        prep.refresh_from_db()
        return cancel_production_preparation(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor)

    def replacement(self):
        _, preview = create_pasted_script_source(project=self.project, user=self.actor, content="明确改选的新剧本")
        script = confirm_script_revision(preview, user=self.actor)
        body = deepcopy(self.adopt)
        body.update(expected_selection_revision=1, idempotency_key="replace-script")
        body["payload"]["content"].update(id=str(script.pk), expected_sha256=script.content_sha256)
        return script, body

    def storyboard_request(self, shot):
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-storyboard/")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        return data, {**self.adopt, "stage_key": "storyboard", "idempotency_key": str(uuid4()),
            "expected_selection_revision": data["adoption"]["revision"], "payload": {
                "source": {"kind": "existing_storyboard"}, "expected_content_sha256": data["content_sha256"],
                "reviewed_shot_ids": [str(shot.pk)], "comment": "人工逐项核对分镜"}}

    def adopt_authored_storyboard(self):
        episode = Episode.objects.create(project=self.project, number=1, title="人工分镜集")
        shot = Shot.objects.create(episode=episode, number=1,
            duration_ms=5000, media_type="video", include_audio=False)
        _, body = self.storyboard_request(shot)
        result = self.post(body)
        self.assertEqual(result.status_code, 201, result.data)
        return shot

    def test_mixed_reviews_and_stopped_preparations_require_explicit_readoption_and_new_preparation(self):
        shot = self.adopt_authored_storyboard()
        prep = self.preparation()
        script, body = self.replacement()
        self.assertEqual(self.get().data["replacement"]["code"], "active_preparations")
        self.assertEqual(self.post(body).status_code, 400)
        self.stop(prep)
        old_inputs = deepcopy(prep.contract.resolved_inputs)
        old_baseline = read_script_adoption_baseline(prep)
        old_review = ApprovalDecision.objects.get(stage_run__production_run=self.run, stage_run__stage_key="storyboard")
        before = (list(ApprovalDecision.objects.values()), list(ProductionManualCommand.objects.values()),
            list(ExecutionPreparation.objects.values()))
        with CaptureQueriesContext(connection) as queries:
            impact = self.get()
        self.assertEqual(impact.status_code, 200, impact.data)
        self.assertEqual(impact.data["replacement"]["code"], "review_and_stopped")
        self.assertTrue(impact.data["replacement"]["allowed"])
        self.assertFalse(any("prepared_request" in row["sql"] for row in queries.captured_queries))
        self.assertEqual(before, (list(ApprovalDecision.objects.values()), list(ProductionManualCommand.objects.values()),
            list(ExecutionPreparation.objects.values())))
        response = self.post(body)
        self.assertEqual(response.status_code, 201, response.data)
        self.run.refresh_from_db()
        self.assertEqual(self.run.script_revision_id, script.pk)
        data, rereview = self.storyboard_request(shot)
        self.assertEqual(data["adoption"]["status"], "changed")
        self.assertEqual(data["adoption"]["decision_id"], str(old_review.pk))
        prep.refresh_from_db()
        self.assertEqual(prep.contract.resolved_inputs, old_inputs)
        self.assertEqual(read_script_adoption_baseline(prep), old_baseline)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertEqual(self.post(rereview).status_code, 201)
        self.assertEqual(self.storyboard_request(shot)[0]["adoption"]["status"], "current")
        replay = self.post()
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["target_id"], str(prep.pk))
        self.body["idempotency_key"] = "explicit-mixed-rework"
        new = self.preparation()
        self.assertNotEqual(new.pk, prep.pk)
        self.assertEqual(read_script_adoption_baseline(new)["script_revision_id"], str(script.pk))
        self.assertIsNone(new.next_check_at)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_mixed_reviews_recheck_new_work_at_the_write_boundary(self):
        self.adopt_authored_storyboard()
        self.stop(self.preparation())
        self.assertEqual(self.get().data["replacement"]["code"], "review_and_stopped")
        self.body["idempotency_key"] = "new-after-mixed-impact"
        self.preparation()
        _, body = self.replacement()
        before = (ApprovalDecision.objects.count(), ProductionManualCommand.objects.count())
        self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(before, (ApprovalDecision.objects.count(), ProductionManualCommand.objects.count()))
        self.run.refresh_from_db()
        self.assertEqual(self.run.script_revision_id, self.script.pk)

    def test_mixed_reviews_do_not_hide_unknown_or_successful_original_calls(self):
        self.adopt_authored_storyboard()
        prep = self.preparation()
        self.stop(prep)
        # Persisted protocol observations only; no remote execution is simulated
        # as real acceptance. A stopped parent cannot vouch for a remote outcome.
        attempt = GenerationAttempt.objects.create(stage_run=self.stage, preparation=prep,
            execution_instance_version=self.version, role="primary", unit_key=prep.unit_key,
            attempt_number=1, operation="script.generate", request_sha256="a" * 64,
            status="failed", error_code="uncertain_remote_response", finished_at=timezone.now())
        _, body = self.replacement()
        for status, error, expected in (("failed", "uncertain_remote_response", "unresolved_calls"),
                ("succeeded", "", "dependent_results")):
            attempt.status, attempt.error_code = status, error
            attempt.save(update_fields=("status", "error_code"))
            impact = self.get()
            self.assertEqual(impact.data["replacement"]["code"], expected)
            self.assertFalse(impact.data["replacement"]["allowed"])
            self.assertEqual(self.post(body).status_code, 400)
        attempt.status, attempt.error_code = "cancelled", "execution_cancelled_before_submit"
        attempt.save(update_fields=("status", "error_code"))
        self.assertEqual(self.get().data["replacement"]["code"], "review_and_stopped")

    def test_mixed_reviews_validate_both_original_decisions_and_stopped_receipts(self):
        self.adopt_authored_storyboard()
        prep = self.preparation()
        self.stop(prep)
        stage = self.run.stages.get(stage_key="storyboard")
        StageRun.objects.filter(pk=stage.pk).update(output_snapshot_sha256="0" * 64)
        _, body = self.replacement()
        self.assertEqual(self.get().data["replacement"]["code"], "inconsistent_records")
        self.assertEqual(self.post(body).status_code, 400)
        StageRun.objects.filter(pk=stage.pk).update(output_snapshot_sha256=stage.output_snapshot_sha256)
        receipt = ProductionManualCommand.objects.get(target_id=prep.pk)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_productionmanualcommand SET result_refs_sha256=%s WHERE id=%s",
                ["0" * 64, receipt._meta.pk.get_db_prep_value(receipt.pk, connection)])
        self.assertEqual(self.get().data["replacement"]["code"], "inconsistent_records")
        self.assertEqual(self.post(body).status_code, 400)

    def test_empty_scope_and_registered_choices_are_real_and_read_only(self):
        response = self.get()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response.data["results"], [])
        self.assertEqual(response.data["stages"], [])
        self.assertEqual(response.data["replacement"]["code"], "no_dependents")
        self.assertTrue(response.data["replacement"]["allowed"])
        self.assertEqual(response.data["source"]["script_revision_id"], str(self.script.pk))
        options = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-action-options/")
        self.assertIn("bible_image_review", options.data["content_change_stage_keys"])
        self.assertEqual(ApprovalDecision.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())
        for key in options.data["content_change_stage_keys"]:
            self.assertEqual(self.get(stage_key=key).status_code, 200)

    def test_real_preparation_is_paginated_with_baseline_and_never_silently_changed(self):
        first = self.preparation()
        self.body["idempotency_key"] = "second-candidate"
        second = self.preparation()
        before = (list(ExecutionPreparation.objects.values()), list(ProductionManualCommand.objects.values()))
        with CaptureQueriesContext(connection) as captured:
            response = self.get(limit=1)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(response.data["next_offset"], 1)
        row = response.data["results"][0]
        self.assertEqual(row["id"], str(first.pk))
        self.assertEqual(row["reference_state"], "recorded_reference")
        self.assertEqual(row["references"], [{"input_path": "script_adoption_baseline", "kind": "adoption_baseline"}])
        self.assertEqual(row["attempts"], [])
        self.assertNotIn("positive_prompt", str(response.data))
        self.assertFalse(any("prepared_request" in query["sql"] for query in captured.captured_queries))
        next_page = self.get(limit=1, offset=1)
        self.assertEqual(next_page.data["results"][0]["id"], str(second.pk))
        self.assertIsNone(next_page.data["next_offset"])
        self.assertEqual(before, (list(ExecutionPreparation.objects.values()), list(ProductionManualCommand.objects.values())))
        self.assertFalse(GenerationAttempt.objects.exists())
        refused = self.post({**self.adopt, "expected_selection_revision": 1, "idempotency_key": "replace"})
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("改选影响", str(refused.data))
        self.assertEqual(ApprovalDecision.objects.count(), 1)

    def test_stop_replace_and_explicit_new_preparation_preserve_old_inputs_and_receipts(self):
        first = self.preparation()
        self.body["idempotency_key"] = "second-candidate"
        second = self.preparation()
        original = (first.intent_sha256, first.contract.resolved_inputs_sha256, read_script_adoption_baseline(first))
        script, body = self.replacement()
        self.stop(first)
        self.assertEqual(self.get().data["replacement"]["code"], "active_preparations")
        self.assertEqual(self.post(body).status_code, 400)
        self.stop(second)
        self.assertEqual(self.get().data["replacement"]["code"], "stopped_unused")
        with CaptureQueriesContext(connection) as queries:
            response = self.post(body)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(any("prepared_request" in row["sql"] for row in queries.captured_queries))
        self.run.refresh_from_db()
        self.assertEqual(self.run.script_revision_id, script.pk)
        self.assertEqual(str(self.run.script_adoption_decision_id), response.data["target_id"])
        self.assertEqual(ApprovalDecision.objects.count(), 2)
        first.refresh_from_db()
        self.assertEqual(original, (first.intent_sha256, first.contract.resolved_inputs_sha256, read_script_adoption_baseline(first)))
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())
        # Replaying the original command returns the stopped preparation, never
        # refreshes its inputs or queues a new task after the upstream change.
        replay = self.post()
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["target_id"], str(second.pk))
        self.assertEqual(self.post({**body, "idempotency_key": "stale-replacement"}).status_code, 409)
        self.body["idempotency_key"] = "explicit-after-replacement"
        new = self.preparation()
        self.assertNotIn(new.pk, (first.pk, second.pk))
        self.assertEqual(read_script_adoption_baseline(new)["script_revision_id"], str(script.pk))
        self.assertIsNone(new.next_check_at)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_writer_rechecks_new_work_after_read_only_impact_and_rolls_back(self):
        first = self.preparation()
        self.stop(first)
        self.assertTrue(self.get().data["replacement"]["allowed"])
        self.body["idempotency_key"] = "started-after-reading-impact"
        self.preparation()
        _, body = self.replacement()
        before = list(ProductionManualCommand.objects.values_list("pk", flat=True))
        response = self.post(body)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(list(ProductionManualCommand.objects.values_list("pk", flat=True)), before)
        self.run.refresh_from_db()
        self.assertEqual(self.run.script_revision_id, self.script.pk)
        self.assertEqual(ApprovalDecision.objects.count(), 1)

    def test_stopped_preparation_is_not_remote_terminal_or_successful_result_permission(self):
        prep = self.preparation()
        self.stop(prep)
        # Explicit persisted observations exercise the gate only; these rows
        # are not executable requests, transport evidence, or provider Smoke.
        attempt = GenerationAttempt.objects.create(stage_run=self.stage, preparation=prep,
            execution_instance_version=self.version, role="primary", unit_key=prep.unit_key,
            attempt_number=1, operation="script.generate", request_sha256="a" * 64,
            status="cancel_requested", error_code="cancel_requested", finished_at=None)
        self.assertEqual(self.get().data["replacement"]["code"], "unresolved_calls")
        attempt.status = "failed"
        attempt.error_code = "uncertain_remote_response"
        attempt.finished_at = timezone.now()
        attempt.save(update_fields=("status", "error_code", "finished_at"))
        self.assertEqual(self.get().data["replacement"]["code"], "unresolved_calls")
        _, body = self.replacement()
        self.assertEqual(self.post(body).status_code, 400)
        attempt.status, attempt.error_code = "succeeded", ""
        attempt.save(update_fields=("status", "error_code"))
        self.assertEqual(self.get().data["replacement"]["code"], "dependent_results")
        attempt.status, attempt.error_code = "cancelled", "execution_cancelled_before_submit"
        attempt.save(update_fields=("status", "error_code"))
        self.assertEqual(self.get().data["replacement"]["code"], "stopped_unused")

    def test_cancelled_status_without_explicit_stop_and_corrupt_receipt_do_not_allow_replacement(self):
        prep = self.preparation()
        prep.status = "cancelled"
        prep.save(update_fields=("status",))
        self.assertEqual(self.get().data["replacement"]["code"], "active_preparations")
        prep.status = "preparing"
        prep.save(update_fields=("status",))
        self.stop(prep)
        receipt = ProductionManualCommand.objects.get(target_id=prep.pk)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_productionmanualcommand SET result_refs_sha256=%s WHERE id=%s",
                ["0" * 64, receipt._meta.pk.get_db_prep_value(receipt.pk, connection)])
        self.assertEqual(self.get().data["replacement"]["code"], "inconsistent_records")
        _, body = self.replacement()
        self.assertEqual(self.post(body).status_code, 400)

    def test_corrupt_provenance_is_visible_and_other_valid_rows_remain_readable(self):
        prep = self.preparation()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_executionpreparationcontract SET resolved_inputs_sha256=%s WHERE preparation_id=%s",
                ["0" * 64, ExecutionPreparationContract._meta.get_field("preparation").target_field.get_db_prep_value(prep.pk, connection)])
        response = self.get()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["results"][0]["reference_state"], "unverifiable")
        self.assertEqual(response.data["count"], 1)

    def test_query_contract_permissions_and_terminal_run(self):
        self.assertEqual(self.client.get(self.impact_url + "?stage_key=script_input&stage_key=storyboard").status_code, 400)
        for query in [{}, {"stage_key": "media_generation"}, {"stage_key": "script_input", "limit": 51},
                {"stage_key": "script_input", "offset": -1}, {"stage_key": "script_input", "unknown": "ignored"}]:
            self.assertEqual(self.client.get(self.impact_url, query).status_code, 400)
        self.run.status = "cancelled"
        self.run.save(update_fields=("status",))
        self.assertTrue(self.get().data["run_read_only"])
        self.assertEqual(self.get().data["replacement"]["code"], "run_read_only")
        other = get_user_model().objects.create_user(username="impact-other", password="isolated")
        self.client.force_authenticate(other)
        self.assertEqual(self.get().status_code, 404)
        self.client.force_authenticate(None)
        self.assertIn(self.get().status_code, (401, 403))

    def test_started_scope_uses_indexed_run_boundary_and_counts_existing_decisions(self):
        stage = self.run.stages.get(stage_key="script_input")
        later = self.run.stages.get(stage_key="storyboard")
        ApprovalDecision.objects.create(stage_run=later, revision=1, decision="approve", actor="human", decided_by=self.actor,
            output_snapshot_sha256="a" * 64)
        rows = started_content_dependents(self.run, stage)
        self.assertEqual(list(rows.values_list("stage_key", flat=True)), ["storyboard"])
        self.assertIn("INDEX", rows.explain().upper())
        report = self.get().data
        self.assertEqual(report["count"], 0)
        self.assertEqual(len(report["stages"]), 1)
        # A decision without its original adoption snapshot is corruption, not
        # permission to rereview or evidence of a valid downstream result.
        self.assertEqual(report["replacement"]["code"], "inconsistent_records")
        _, body = self.replacement()
        self.assertEqual(self.post(body).status_code, 400)
