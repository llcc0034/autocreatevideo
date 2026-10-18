"""Task-center read-only contract tests. All model replies are isolated fixtures."""
from copy import deepcopy
from dataclasses import asdict
import json
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from studio.tests import test_execution_smoke_evidence as fixtures
from studio.services.execution_smoke_evidence import finish_prepared_smoke
from studio.services.json_contracts import json_contract
from studio.task_center_details import bound_content, inspect_output, prompt_rows, parameter_rows
from studio.task_center_serializers import TaskCenterDetailQuerySerializer


class SmokePendingOutputTests(SimpleTestCase):
    """Exercise actual model defaults without creating or calling a provider."""

    def test_absent_default_output_is_not_invalid_or_success(self):
        from studio.models import ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt
        for model, kind in ((ModelDeploymentSmokeAttempt, "deployment_smoke"), (WorkflowSmokeAttempt, "workflow_smoke")):
            for status in ("preparing", "queued", "submitting", "submitted", "running", "cancel_requested",
                           "failed", "cancelled", "uncertain"):
                with self.subTest(model=model.__name__, status=status):
                    source = model(status=status)
                    before = deepcopy({f.attname: getattr(source, f.attname) for f in source._meta.concrete_fields})
                    with patch("studio.services.execution_smoke_evidence.read_prepared_smoke_fact",
                               side_effect=AssertionError("No result to read")):
                        output, paging = inspect_output(source, kind, {}, None)
                    self.assertEqual(output["state"], "unavailable")
                    self.assertIsNone(output["result"])
                    self.assertEqual(output["media"], [])
                    self.assertEqual(paging["output_count"], 0)
                    self.assertIn("尚未记录输出", output["notes"][0])
                    self.assertEqual({f.attname: getattr(source, f.attname) for f in source._meta.concrete_fields}, before)

    def test_missing_digest_still_rejects_success_or_inconsistent_metadata(self):
        from django.core.exceptions import ValidationError
        from studio.models import ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt
        for model, kind in ((ModelDeploymentSmokeAttempt, "deployment_smoke"), (WorkflowSmokeAttempt, "workflow_smoke")):
            for values in ({"status": "succeeded"}, {"result": {"evidence": {}}}, {"result": []},
                           {"result": None}, {"result_size_bytes": 0}, {"result_size_bytes": 3},
                           {"result_schema_version": 2}):
                with self.subTest(model=model.__name__, values=values):
                    source = model(**{"status": "running", **values})
                    with self.assertRaisesMessage(ValidationError, "Smoke 输出缺少结果摘要"):
                        inspect_output(source, kind, {}, None)

@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class TaskCenterTests(TestCase):
    setUpTestData = fixtures.ExecutionSmokeEvidenceTests.__dict__["setUpTestData"]
    version = fixtures.ExecutionSmokeEvidenceTests.version
    attempt = fixtures.ExecutionSmokeEvidenceTests.attempt
    finish = fixtures.ExecutionSmokeEvidenceTests.finish

    def setUp(self):
        fixtures.ExecutionSmokeEvidenceTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.base = "/api/v1/task-center/"

    def detail(self, prep, **query):
        response = self.client.get(f"{self.base}preparation/{prep.pk}/", query)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data.get("data", response.data)

    def test_smoke_actual_parameters_messages_and_output_not_template(self):
        attempt, prep = self.attempt()
        finish_prepared_smoke(attempt, attempt.claim_token, status="succeeded",
            evidence=json_contract({"result": {"text": "isolated accepted output"},
                "usage": {"prompt_tokens": 12, "completion_tokens": 0, "total_tokens": 12, "api_token": "TOP-SECRET"}}))
        before = deepcopy(prep.contract.prepared_request)
        with patch("studio.services.execution_smoke_worker.driver_for", side_effect=AssertionError("read cannot invoke")):
            detail = self.detail(prep)
        self.assertEqual(detail["purpose"], "instance_smoke")
        self.assertEqual(detail["status"], "succeeded")
        self.assertEqual(detail["input"]["state"], "frozen")
        self.assertTrue(any("text.prompt_draft/v1" in note for note in detail["input"]["notes"]))
        self.assertTrue(any("不是本次正文校验" in note for note in detail["input"]["notes"]))
        self.assertIn(detail["input"]["prompt_trace"]["state"], {"recorded", "unavailable"})
        self.assertTrue(any(p["key"] == "seed" and p["value"] == 20 for p in detail["input"]["parameters"]))
        self.assertTrue(any("Describe a blue box." in p["text"] for p in detail["input"]["prompts"]))
        self.assertEqual(detail["output"]["text"], "isolated accepted output")
        self.assertEqual(detail["output"]["result"]["evidence"]["usage"]["completion_tokens"], 0)
        self.assertEqual(detail["output"]["result"]["evidence"]["usage"]["prompt_tokens"], 12)
        self.assertNotIn("TOP-SECRET", json.dumps(detail, default=str))
        self.assertEqual(detail["call_count"], 1)
        self.assertEqual(str(detail["calls"][0]["id"]), str(attempt.pk))
        prep.contract.refresh_from_db()
        self.assertEqual(prep.contract.prepared_request, before)

    def test_submission_fields_are_exact_readonly_local_evidence_without_extra_queries(self):
        from datetime import timedelta
        from django.utils import timezone
        from hashlib import sha256
        attempt, prep = self.attempt()
        with CaptureQueriesContext(connection) as baseline:
            initial = self.detail(prep)
        self.assertIsNone(initial["submission"]["registered_at"])
        start = timezone.now()
        type(attempt).objects.filter(pk=attempt.pk).update(submission_started_at=start,
            submitted_at=start + timedelta(seconds=1), provider_request_id="private-submit-id",
            provider_request_id_sha256=sha256(b"private-submit-id").hexdigest())
        before = type(attempt).objects.values().get(pk=attempt.pk)
        with CaptureQueriesContext(connection) as queries:
            detail = self.detail(prep)
        self.assertEqual(len(queries), len(baseline))
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries))
        evidence = detail["submission"]
        self.assertEqual(evidence["state"], "recorded")
        self.assertEqual(evidence["identifier_state"], "recorded")
        self.assertEqual(detail["source_id"], str(attempt.pk))
        self.assertNotIn("private-submit-id", json.dumps(detail, default=str))
        self.assertEqual(before, type(attempt).objects.values().get(pk=attempt.pk))
        type(attempt).objects.filter(pk=attempt.pk).update(provider_request_id_sha256="0" * 64)
        inconsistent = self.detail(prep)
        self.assertEqual(inconsistent["submission"]["state"], "inconsistent")
        self.assertEqual(inconsistent["status"], detail["status"])

    def test_unfrozen_input_never_fakes_actual_prompt(self):
        _, prep = self.attempt(frozen=False)
        detail = self.detail(prep)
        self.assertEqual(detail["input"]["state"], "not_frozen")
        self.assertEqual(detail["input"]["prompts"], [])
        self.assertFalse(any("冻结协议" in note for note in detail["input"]["notes"]))
        self.assertIsNone(detail["input"]["prompt_trace"])
        self.assertIsNone(detail["request_frozen_at"])
        self.assertTrue(detail["actions"]["cancel_smoke"])
        self.assertEqual(detail["output"]["state"], "unavailable")
        self.assertEqual(detail["validation"]["error_code"], "")
        response = self.client.get(f"{self.base}preparation/{prep.pk}/")
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_bad_smoke_digest_is_explicit_not_empty_success(self):
        attempt, prep = self.attempt()
        self.finish(attempt)
        attempt.refresh_from_db()
        attempt.result["evidence"]["injected"] = "tampered"
        attempt.save(update_fields=["result"])
        detail = self.detail(prep)
        self.assertEqual(detail["output"]["state"], "error")
        self.assertEqual(detail["validation"]["status"], "invalid")
        self.assertIsNone(detail["output"]["result"])
        self.assertFalse(detail["actions"]["cancel_smoke"])

    def test_cursor_is_stable_and_scoped_no_full_text_on_list(self):
        ids = []
        for _ in range(3):
            attempt, prep = self.attempt()
            self.finish(attempt)
            ids.append(str(prep.pk))
        with CaptureQueriesContext(connection) as queries:
            first = self.client.get(self.base, {"category": "smoke", "page_size": 1}).data
        self.assertEqual(first["count"], 3)
        self.assertEqual(first["counts"]["smoke"], 3)
        self.assertEqual(len(first["results"]), 1)
        self.assertNotIn("input", first["results"][0])
        self.assertNotIn("resolved_inputs", " ".join(row["sql"] for row in queries))
        self.assertNotIn("prepared_request", " ".join(row["sql"] for row in queries))
        second = self.client.get(first["next"]).data
        third = self.client.get(second["next"]).data
        self.assertEqual({r["results"][0]["id"] for r in (first,second,third)}, set(ids))
        self.assertIsNone(third["next"])
        previous = self.client.get(second["previous"]).data
        self.assertEqual(previous["results"][0]["id"], first["results"][0]["id"])
        wrong = parse_qs(urlsplit(first["next"]).query)
        wrong["status"] = ["failed"]
        response = self.client.get(self.base, {k:v[0] for k,v in wrong.items()})
        self.assertEqual(response.status_code, 400)

    def test_filter_counts_ignore_only_category_and_source_link_resolves(self):
        attempt, prep = self.attempt(frozen=False)
        response = self.client.get(self.base, {"category": "asset"}).data
        self.assertEqual(response["count"], 0)
        self.assertEqual(response["counts"]["smoke"], 1)
        response = self.client.get(self.base, {"source_id": str(attempt.pk)}).data
        self.assertEqual(response["results"][0]["id"], str(prep.pk))
        self.assertEqual(self.client.get(self.base, {"execution_instance_version_id": str(self.execution.pk)}).data["count"], 1)

    def test_resource_deployment_history_link_is_exact_read_only_and_staff_only(self):
        from django.utils import timezone
        from studio.models import ExecutionPreparation, ModelDeploymentSmokeAttempt
        preparations = []
        attempts = []
        for status in ("failed", "succeeded"):
            attempt = ModelDeploymentSmokeAttempt.objects.create(
                deployment=self.deployment, scope="resource", operation="prompt.draft",
                status=status, request_sha256="b" * 64, target_sha256=self.identity.target_fingerprint)
            preparations.append(ExecutionPreparation.objects.create(
                deployment_smoke=attempt, purpose="resource_smoke", unit_key="smoke",
                request_key="main", status="dispatched", created_by=self.actor,
                intent_sha256="a" * 64, request_sha256=attempt.request_sha256,
                request_frozen_at=timezone.now()))
            attempts.append(attempt)
        query = {"category": "smoke", "source_id": str(attempts[0].pk)}
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.base, query)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["counts"]["smoke"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(preparations[0].pk))
        self.assertEqual(response.data["results"][0]["source_id"], str(attempts[0].pk))
        self.assertEqual(response.data["results"][0]["status"], "failed")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE")) for row in queries))
        missing = self.client.get(self.base, {**query, "source_id": "00000000-0000-0000-0000-000000000001"})
        self.assertEqual(missing.data["count"], 0)
        self.assertEqual(missing.data["counts"]["smoke"], 0)
        self.client.force_authenticate(get_user_model().objects.create_user(username="resource-history-member"))
        hidden = self.client.get(self.base, query)
        self.assertEqual(hidden.data["count"], 0)
        self.assertEqual(self.client.get(f"{self.base}preparation/{preparations[0].pk}/").status_code, 404)
        for attempt, status in zip(attempts, ("failed", "succeeded")):
            attempt.refresh_from_db()
            self.assertEqual(attempt.status, status)

    def test_resource_workflow_operation_uses_related_json_not_foreign_key(self):
        from studio.models import ExecutionPreparation, WorkflowSmokeAttempt, WorkflowTemplate, WorkflowTemplateVersion
        from studio.task_center import detail_row
        template = WorkflowTemplate.objects.create(name="Resource workflow", slug="task-center-resource", output_kind="image")
        version = WorkflowTemplateVersion.objects.create(template=template, version=1)
        expected = {}
        for operation in ("image.text_to_image", None):
            fixture = {"schema_version": 2}
            if operation:
                fixture["resource_definition"] = asdict(json_contract({"configuration": {"operation": operation}}))
            smoke = WorkflowSmokeAttempt.objects.create(workflow_version=version, deployment=self.deployment,
                created_by=self.actor, scope="resource", status="failed", fixture_values=fixture)
            prep = ExecutionPreparation.objects.create(workflow_smoke=smoke, purpose="resource_smoke",
                unit_key="smoke", request_key="main", status="preparing", created_by=self.actor, intent_sha256="a" * 64)
            expected[str(prep.pk)] = operation or ""
            self.assertEqual(detail_row(self.actor, "preparation", prep.pk)["operation"], operation or "")
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual({row["id"]: row["operation"] for row in response.data["results"]}, expected)
        filtered = self.client.get(self.base, {"category": "smoke", "operation": "image.text_to_image"}).data
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["results"][0]["operation"], "image.text_to_image")

    def test_smoke_hidden_from_member_and_read_only(self):
        _, prep = self.attempt(frozen=False)
        member = get_user_model().objects.create_user(username="task-center-member")
        self.client.force_authenticate(member)
        self.assertEqual(self.client.get(self.base).data["count"], 0)
        self.assertEqual(self.client.get(f"{self.base}preparation/{prep.pk}/").status_code, 404)
        self.assertEqual(self.client.post(self.base, {}).status_code, 405)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.base).status_code, (401,403))

    def test_options_are_paginated_and_closed(self):
        from studio.models import GenerationAttempt
        self.attempt(frozen=False)
        data = self.client.get(self.base+"options/", {"domain":"execution_versions","page_size":1}).data
        data = data.get("data",data)
        self.assertEqual(data["execution_versions"][0]["value"], str(self.execution.pk))
        self.assertEqual(data["pagination"]["execution_versions"]["count"],1)
        statuses = {row["value"]: row["label"] for row in data["statuses"]}
        for value, label in GenerationAttempt.Status.choices:
            self.assertEqual(statuses[value], label)
        self.assertEqual(self.client.get(self.base, {"made_up":"x"}).status_code,400)
        self.assertEqual(self.client.get(self.base, {"page_size":101}).status_code,400)
        self.assertEqual(self.client.get(self.base, {"category":"smoke&status=succeeded"}).status_code,400)

    def test_large_content_explicit_complete_chunks_and_unicode_progress(self):
        value={"input":{"state":"frozen","submitted":{},"resolved":{},"parameters":[],"prompts":[],"prompt_trace":None,"field_batch_trace":None,"media_trace":None,"notes":[]},
               "output":{"state":"available","result":None,"text":"中文😀"*25000,"media":[],"notes":[]}}
        expected=json.dumps(value["output"]["text"],ensure_ascii=False,separators=(",",":"))
        query=TaskCenterDetailQuerySerializer(data={})
        self.assertTrue(query.is_valid())
        bounded=bound_content(deepcopy(value),query.validated_data)
        self.assertEqual(bounded["output"]["text"],"")
        self.assertEqual(bounded["deferred_fields"][0]["field"],"output.text")
        offset=0
        parts=[]
        while True:
            query=TaskCenterDetailQuerySerializer(data={"content_field":"output.text","content_offset":offset,"content_limit":16384})
            self.assertTrue(query.is_valid())
            content=bound_content(deepcopy(value),query.validated_data)["content"]
            parts.append(content["text"])
            self.assertLessEqual(len(content["text"].encode()),16384)
            if content["next_offset"] is None:break
            self.assertGreater(content["next_offset"],offset)
            offset=content["next_offset"]
        self.assertEqual("".join(parts),expected)
        query=TaskCenterDetailQuerySerializer(data={"content_limit":1})
        self.assertFalse(query.is_valid())

    def test_sensitive_values_redacted_without_truncating_normal_prompt(self):
        transport={"parameters":{"values":{"seed":0},"sources":{"seed":"request"}},
            "payload":{"messages":[{"role":"system","content":"actual system prompt"},{"role":"user","content":"story password=secret123"}],"max_tokens":4096},
            "prompt_channels":{"values":{"positive_prompt":"not the final message"}}}
        prompts=prompt_rows(transport)
        self.assertEqual(prompts[0]["text"],"actual system prompt")
        self.assertNotIn("secret123",json.dumps(prompts))
        self.assertNotIn("not the final message",json.dumps(prompts))
        self.assertTrue(any(p["key"]=="max_tokens" and p["value"]==4096 for p in parameter_rows(transport)))

    def test_fixed_workflow_widgets_and_actual_channels_are_preserved(self):
        from django.core.exceptions import ValidationError
        definition = {"interface": {"prompt": {"channels": {
            "positive_prompt": {"semantic_key": "prompt.positive"},
            "negative_prompt": {"semantic_key": "prompt.negative"}}}}}
        transport = {"parameters": {"values": {"sampling.seed": 0}, "sources": {"sampling.seed": "request"}},
            "prompt_channels": {"values": {"positive_prompt": "Actual prompt", "negative_prompt": ""},
                "sources": {"positive_prompt": "manual", "negative_prompt": "manual"}},
            "payload": {"compiled_workflow": {"resolved_semantics": {"prompt.positive": "Actual prompt", "prompt.negative": ""},
                "graph": {"5": {"class_type": "KSampler", "inputs": {"steps": 10, "cfg": 5.0,
                    "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0, "model": ["1", 0],
                    "api_key": "do-not-leak", "path": "/root/private/model", "noise_token": "refresh_token=do-not-leak"}}}}}}
        rows = parameter_rows(transport, definition)
        for key, value in {"steps": 10, "cfg": 5.0, "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}.items():
            self.assertTrue(any(p["source"] == "graph.5.inputs."+key and p["value"] == value for p in rows), key)
        self.assertNotIn("do-not-leak", json.dumps(rows))
        self.assertFalse(any(p["source"] == "graph.5.inputs.model" for p in rows))
        self.assertEqual([p["text"] for p in prompt_rows(transport, definition)], ["Actual prompt", ""])
        transport["payload"]["compiled_workflow"]["resolved_semantics"]["prompt.positive"] = "drift"
        with self.assertRaises(ValidationError):
            prompt_rows(transport, definition)

    def test_smoke_identity_is_verified_even_with_a_recomputed_digest(self):
        from studio.services.json_contracts import assign_json_contract
        attempt, prep = self.attempt()
        self.finish(attempt)
        attempt.refresh_from_db()
        altered = deepcopy(attempt.result)
        altered["attempt_id"] = "00000000-0000-0000-0000-000000000001"
        assign_json_contract(attempt, "result", altered)
        attempt.save()
        self.assertEqual(self.detail(prep)["validation"]["status"], "invalid")

from studio.tests import test_execution_production_dispatch as production_fixtures
from studio.tests import test_execution_production_media as media_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class TaskCenterMediaHistoryTests(TestCase):
    setUpTestData = classmethod(media_fixtures.ProductionMediaTests.setUpTestData.__func__)
    write_contract = staticmethod(media_fixtures.ProductionMediaTests.write_contract)
    job_row = media_fixtures.ProductionMediaTests.job_row
    resolve = media_fixtures.ProductionMediaTests.resolve
    setUp = media_fixtures.ProductionMediaTests.setUp

    def test_async_rest_original_preparation_is_visible_without_workflow_task_or_write(self):
        client = APIClient(); client.force_authenticate(self.actor)
        before = self.attempt.request_sha256
        with CaptureQueriesContext(connection) as queries:
            response = client.get(f"/api/v1/task-center/preparation/{self.preparation.pk}/")
            page = client.get("/api/v1/task-center/", {"project_id": str(self.project.pk), "unit_key": "media-unit"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["input"]["state"], "frozen")
        self.assertIn("原请求没有媒体槽位绑定", " ".join(response.data["input"]["notes"]))
        self.assertIsNone(response.data["links"]["generation_task_id"])
        self.assertEqual(response.data["request_sha256"], before)
        self.assertEqual([row["id"] for row in page.data["results"]], [str(self.preparation.pk)])
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))

@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class TaskCenterProductionTests(TestCase):
    def test_unit_filter_uses_original_preparation_membership_across_drivers_and_owner_scope(self):
        from studio.models import ExecutionPreparation
        prep = self.prepare()
        client = APIClient(); client.force_authenticate(self.actor)
        filters = {"category": "production", "project_id": str(self.run.project_id), "unit_key": prep.unit_key}
        before = list(ExecutionPreparation.objects.values("id", "revision", "status"))
        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/v1/task-center/", filters)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data["results"]], [str(prep.pk)])
        self.assertTrue(all(row["kind"] == "preparation" for row in response.data["results"]))
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertFalse(any("studio_executionpreparationcontract" in q["sql"].lower() for q in queries))
        self.assertEqual(client.get("/api/v1/task-center/", {**filters, "unit_key": prep.unit_key + ":other"}).data["count"], 0)
        self.assertEqual(client.get("/api/v1/task-center/", {**filters, "stage_run_id": str(self.run.pk)}).data["count"], 0)
        other = get_user_model().objects.create_user(username="unit-history-other")
        client.force_authenticate(other)
        self.assertEqual(client.get("/api/v1/task-center/", filters).data["count"], 0)
        self.assertEqual(before, list(ExecutionPreparation.objects.values("id", "revision", "status")))

    def test_nonworkflow_history_reads_original_request_without_manufacturing_media_binding(self):
        prep, attempt = self.queued()
        client = APIClient(); client.force_authenticate(self.actor)
        with CaptureQueriesContext(connection) as queries:
            response = client.get(f"/api/v1/task-center/preparation/{prep.pk}/", {"attempt_id": str(attempt.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["input"]["state"], "frozen")
        self.assertIn("原请求没有媒体槽位绑定", " ".join(response.data["input"]["notes"]))
        self.assertIsNone(response.data["input"]["media_trace"])
        self.assertEqual(response.data["request_sha256"], prep.request_sha256)
        self.assertEqual(response.data["source_id"], str(attempt.pk))
        self.assertIsNone(response.data["links"]["generation_task_id"])
        self.assertTrue(response.data["input"]["prompts"])
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))

    setUpTestData = classmethod(production_fixtures.ProductionDispatchTests.setUpTestData.__func__)
    profile_instance = production_fixtures.ProductionDispatchTests.profile_instance
    root = production_fixtures.ProductionDispatchTests.root
    prepare = production_fixtures.ProductionDispatchTests.prepare
    structure_instance = production_fixtures.ProductionDispatchTests.structure_instance
    setUp = production_fixtures.ProductionDispatchTests.setUp
    pass_smoke = production_fixtures.ProductionDispatchTests.pass_smoke
    advance = production_fixtures.ProductionDispatchTests.advance
    queued = production_fixtures.ProductionDispatchTests.queued
    process = production_fixtures.ProductionDispatchTests.process
    response = production_fixtures.ProductionDispatchTests.response

    def test_outcome_uses_selected_attempt_not_latest_or_auxiliary_and_read_does_not_write(self):
        from studio.models import GenerationAttempt, GeneratedArtifact
        from studio.services.execution_preparation_presentation import attempt_artifact_counts
        prep, first = self.queued()
        result, _ = self.process()
        self.assertEqual(result.status, "succeeded")
        latest = GenerationAttempt.objects.create(stage_run=self.stage, preparation=prep,
            execution_instance_version=first.execution_instance_version, role="primary", unit_key=first.unit_key,
            attempt_number=2, status="failed", operation=first.operation, request_sha256=first.request_sha256,
            request_snapshot_key=first.request_snapshot_key, error_code="fixture_failure")
        client = APIClient(); client.force_authenticate(self.actor)
        url = f"/api/v1/task-center/preparation/{prep.pk}/"
        before = list(GenerationAttempt.objects.values("id", "status", "updated_at"))
        with CaptureQueriesContext(connection) as queries:
            current = client.get(url).data
            historical = client.get(url, {"attempt_id": str(first.pk)}).data
        self.assertEqual(current["source_id"], str(latest.pk))
        self.assertIn("主调用失败", current["outcome"]["message"])
        self.assertEqual(current["outcome"]["artifact_count"], 0)
        self.assertEqual(historical["source_id"], str(first.pk))
        self.assertIn("已记录 1 项产物", historical["outcome"]["message"])
        self.assertEqual(historical["outcome"]["selected_count"], 0)
        self.assertEqual(historical["output_count"], 1)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual(before, list(GenerationAttempt.objects.values("id", "status", "updated_at")))
        with CaptureQueriesContext(connection) as counts:
            self.assertEqual(attempt_artifact_counts([first.pk, latest.pk], self.stage.pk, self.run.pk)[first.pk]["count"], 1)
        self.assertEqual(len(counts), 1)
        self.assertNotIn("manifest_metadata", counts[0]["sql"])
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + counts[0]["sql"])
            plan = str(cursor.fetchall())
        self.assertIn("INDEX", plan.upper())
        self.assertEqual(GeneratedArtifact.objects.count(), 1)

    def test_exact_run_and_stage_filters_keep_cursor_and_count_scope_without_dispatch(self):
        original_run, original_stage = self.run, self.stage
        first = self.prepare()
        second = self.prepare(candidate_number=2)
        self.run = self.root()  # Same project and stage name, different real round.
        other = self.prepare()
        client = APIClient(); client.force_authenticate(self.actor)
        url = "/api/v1/task-center/"
        scope = {"production_run_id": str(original_run.pk), "stage_run_id": str(original_stage.pk), "page_size": 1}
        with CaptureQueriesContext(connection) as queries:
            page = client.get(url, scope)
        self.assertEqual(page.status_code, 200, page.data)
        self.assertEqual(page.data["count"], 2)
        self.assertEqual(page.data["counts"], {"all": 2, "production": 2, "asset": 0, "smoke": 0})
        following = client.get(page.data["next"]).data
        self.assertEqual({page.data["results"][0]["id"], following["results"][0]["id"]}, {str(first.pk), str(second.pk)})
        self.assertNotIn(str(other.pk), str(page.data["results"]))
        self.assertEqual(client.get(url, {**scope, "stage_run_id": str(self.stage.pk)}).data["count"], 0)
        counts = client.get(url, {**scope, "category": "smoke"}).data
        self.assertEqual((counts["count"], counts["counts"]["production"]), (0, 2))
        wrong = parse_qs(urlsplit(page.data["next"]).query)
        wrong["production_run_id"] = [str(self.run.pk)]
        self.assertEqual(client.get(url, {key: value[0] for key, value in wrong.items()}).status_code, 400)
        for key in ("production_run_id", "stage_run_id"):
            self.assertEqual(client.get(url, {key: "not-a-uuid"}).status_code, 400)
            self.assertEqual(client.get(url + f"?{key}={original_stage.pk}&{key}={original_stage.pk}").status_code, 400)
        self.assertNotIn("prepared_request", " ".join(row["sql"] for row in queries))
        self.assertNotIn("resolved_inputs", " ".join(row["sql"] for row in queries))
        from studio.models import GenerationAttempt
        self.assertEqual(GenerationAttempt.objects.count(), 0)
        original_stage.refresh_from_db()
        self.assertEqual(original_stage.status, "running")

    def test_scope_does_not_grant_other_owners_task_access(self):
        prep = self.prepare()
        member = get_user_model().objects.create_user(username="scoped-reader")
        client = APIClient(); client.force_authenticate(member)
        self.assertEqual(client.get("/api/v1/task-center/", {"stage_run_id": str(self.stage.pk)}).data["count"], 0)
        self.assertEqual(client.get(f"/api/v1/task-center/preparation/{prep.pk}/").status_code, 404)
        self.run.owner = member; self.run.save(update_fields=["owner"])
        self.assertEqual(client.get("/api/v1/task-center/", {"production_run_id": str(self.run.pk)}).data["count"], 1)

    def test_assembly_scope_uses_real_membership_without_duplicates_or_project_fallback(self):
        from studio.models import AssemblyJob, Episode, GenerationTask, ProductionAssemblyLink, StageRun
        from uuid import uuid4
        stage = StageRun.objects.create(production_run=self.run, stage_key="assembly", ordinal=2,
            mode="manual", status="running", idempotency_key=str(uuid4()))
        job = AssemblyJob.objects.create(project=self.project, owner=self.actor, scope="series", max_attempts=1,
            consumer_stage=stage, consumer_input_sha256="a" * 64)
        unbound = AssemblyJob.objects.create(project=self.project, owner=self.actor, scope="series", max_attempts=1)
        orphan = GenerationTask.objects.create(project=self.project, owner=self.actor, kind="image")
        for number in (1, 2):
            episode = Episode.objects.create(project=self.project, number=number, title=f"Episode {number}")
            ProductionAssemblyLink.objects.create(production_run=self.run, stage_run=stage, episode=episode, assembly_job=job)
        client = APIClient(); client.force_authenticate(self.actor)
        for filters in ({"production_run_id": str(self.run.pk)}, {"stage_run_id": str(stage.pk)}):
            page = client.get("/api/v1/task-center/", filters).data
            self.assertEqual(page["count"], 1)
            self.assertEqual([row["id"] for row in page["results"]], [str(job.pk)])
            self.assertEqual(page["counts"]["production"], 1)
        all_ids = {row["id"] for row in client.get("/api/v1/task-center/").data["results"]}
        self.assertTrue({str(job.pk), str(unbound.pk), str(orphan.pk)} <= all_ids)

    def test_native_text_is_visible_without_generation_task_and_never_adopted(self):
        prep, attempt = self.queued()
        outcome, _ = self.process()
        self.assertEqual(outcome.status, "succeeded", outcome.error_code)
        self.assertIsNone(outcome.generation_task_id)
        self.stage.refresh_from_db()
        previous_stage_status = self.stage.status
        client = APIClient()
        client.force_authenticate(self.actor)
        page = client.get("/api/v1/task-center/", {"category":"production"}).data
        self.assertEqual(page["count"], 1)
        self.assertEqual(page["results"][0]["status"], "succeeded")
        response = client.get(f"/api/v1/task-center/preparation/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data.get("data",response.data)
        self.assertEqual(data["input"]["state"], "frozen",data["validation"])
        self.assertTrue(data["input"]["prompts"])
        self.assertTrue(data["output"]["text"],data)
        self.assertIsNone(data["links"]["generation_task_id"])
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, previous_stage_status)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.script_revision_id)

        # Rejection affects adoption, not the ability to audit the original bytes.
        from studio.models import GeneratedArtifact
        from studio.services.execution_production_candidates import read_production_text_candidate
        from django.core.exceptions import ValidationError
        artifact = GeneratedArtifact.objects.get(generation_attempt=outcome)
        artifact.status = "rejected"
        artifact.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            read_production_text_candidate(artifact)
        response = client.get(f"/api/v1/task-center/preparation/{prep.pk}/", {"attempt_id": str(outcome.pk)})
        data = response.data.get("data", response.data)
        self.assertEqual(data["output"]["state"], "available", data["validation"])
        self.assertTrue(data["output"]["text"])
        self.assertEqual(data["output"]["result"]["artifacts"][0]["status"], "rejected")
        self.assertEqual(client.get("/api/v1/task-center/", {"source_id": str(outcome.pk)}).data["count"], 1)

    def test_default_lists_orphan_and_assembly_with_no_absent_boolean_filter(self):
        from studio.models import GenerationTask, AssemblyJob
        from django.http import QueryDict
        from studio.task_center_serializers import TaskCenterQuerySerializer
        query = TaskCenterQuerySerializer(data=QueryDict(""))
        self.assertTrue(query.is_valid())
        self.assertNotIn("anomaly",query.validated_data)
        orphan = GenerationTask.objects.create(project=self.run.project,owner=self.actor,kind="image")
        assembly = AssemblyJob.objects.create(project=self.run.project,owner=self.actor,scope="series",max_attempts=1)
        client=APIClient()
        client.force_authenticate(self.actor)
        page=client.get("/api/v1/task-center/").data
        self.assertTrue({str(orphan.pk),str(assembly.pk)}.issubset({r["id"] for r in page["results"]}))
        page=client.get("/api/v1/task-center/",{"anomaly":"true"}).data
        self.assertEqual([r["id"] for r in page["results"]],[str(orphan.pk)])
        for kind,identifier in (("generation_task",orphan.pk),("assembly",assembly.pk)):
            response=client.get(f"/api/v1/task-center/{kind}/{identifier}/")
            self.assertEqual(response.status_code,200,response.data)

    def test_invalid_production_response_is_visible_before_and_after_human_correction(self):
        import tempfile
        from studio.models import ExecutionTextResponseReview
        from studio.services.execution_text_response_review import complete_text_response
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            prep, _ = self.queued()
            result, driver = self.process(response={"text": '{"incomplete":'})
            review = ExecutionTextResponseReview.objects.get(attempt=result)
            client = APIClient()
            client.force_authenticate(self.actor)
            url = f"/api/v1/task-center/preparation/{prep.pk}/"
            response = client.get(url)
            self.assertEqual(response.status_code, 200, response.data)
            detail = response.data.get("data", response.data)
            self.assertEqual(detail["output"]["state"], "awaiting_review")
            self.assertEqual(detail["output"]["text"], '{"incomplete":')
            diagnostic = detail["output"]["result"]["response_review"]
            self.assertEqual(diagnostic["validation_code"], "script_semantic_result_invalid")
            self.assertIn("JSON", str(diagnostic["structure_errors"]))
            prep.refresh_from_db()
            complete_text_response(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
                review_id=review.pk, original_sha256=review.original_sha256, actor=self.actor, text=self.response()["text"])
            response = client.get(url)
            detail = response.data.get("data", response.data)
            self.assertEqual(detail["output"]["state"], "available")
            self.assertTrue(detail["output"]["text"])
            self.assertIn("human_response_recovery", str(detail["output"]["result"]))
            driver.invoke.assert_called_once()
