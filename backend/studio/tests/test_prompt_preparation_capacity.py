"""M6-07: measure actual preparation writes, not prompt text alone.

Fixture admission is isolated; resolution, JSON serialization, ORM writes,
finalization, private request files and authorized detail reads are real.
This does not change any runtime/Schema boundary or certify model capacity.
"""
import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.test import TestCase, override_settings

from studio.services.execution_instances import (
    execution_configuration_max_bytes, execution_resolved_inputs_max_bytes,
)
from studio.services.execution_preparations import create_execution_instance_preparation, read_execution_preparation_detail
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.json_contracts import canonical_json_bytes, json_contract
from studio.tests import test_prompt_final_assembly_contract as fixtures


class CapacityAssertions(fixtures.AssemblyAssertions):
    def start_write_capture(self):
        self.sql_json_writes = []

        def record(execute, sql, params, many, context):
            if ('"studio_executionpreparationcontract"' in sql
                    and sql.lstrip().split()[0].upper() in {"INSERT", "UPDATE"}):
                self.assertFalse(many)
                self.sql_json_writes.extend(value for value in params if isinstance(value, str))
            return execute(sql, params, many, context)

        self.enterContext(connection.execute_wrapper(record))

    def capacity_report(self, sample):
        self.finalize()
        self.assertEqual(self.prep.status, "ready", self.prep.reason)
        detail = read_execution_preparation_detail(self.prep)
        fields = ("intent", "resolved_inputs", "review_contract", "evaluation")
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT "intent", "resolved_inputs", "review_contract", "evaluation" '
                'FROM "studio_executionpreparationcontract" WHERE "id" = %s',
                [detail.pk.hex],
            )
            raw_values = cursor.fetchone()
        report = {"sample": sample, "database_json": {}, "resolved_sections": {}}
        for name, stored_text in zip(fields, raw_values, strict=True):
            value = getattr(detail, name)
            self.assertIsInstance(stored_text, str)
            self.assertIn(stored_text, self.sql_json_writes)  # exact bound SQL value, not a re-encoding estimate
            self.assertEqual(json.loads(stored_text), value)
            canonical = canonical_json_bytes(value)
            self.assertEqual(len(canonical), getattr(detail, f"{name}_size_bytes"))
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), getattr(detail, f"{name}_sha256"))
            cap = execution_resolved_inputs_max_bytes() if name == "resolved_inputs" else execution_configuration_max_bytes()
            self.assertLessEqual(len(canonical), cap)
            report["database_json"][name] = {
                "canonical_utf8": len(canonical), "sqlite_bound_utf8": len(stored_text.encode("utf-8")),
                "canonical_limit": cap,
            }
        facts = detail.resolved_inputs
        for key, value in facts.items():
            report["resolved_sections"][key] = len(canonical_json_bytes(value))
        self.assertTrue({"inputs", "profile_sources", "parameters", "field_requests"}.issubset(facts))
        report["input_sections"] = {key: len(canonical_json_bytes(value)) for key, value in facts["inputs"].items()}
        report["field_request_count"] = len(facts["field_requests"])
        report["field_request_bytes"] = [len(canonical_json_bytes(item)) for item in facts["field_requests"]]
        report["field_text_utf8"] = sum(len(value.text.encode("utf-8")) for value in self.prep.field_values.all())
        path = Path(settings.MEDIA_ROOT).resolve() / detail.prepared_request_key
        self.assertTrue(path.resolve().is_relative_to(Path(settings.MEDIA_ROOT).resolve()))
        actual_file = path.read_bytes()
        request = detail.prepared_request
        report["final_prompt_channels_utf8"] = {
            key: len(text.encode("utf-8"))
            for key, text in request["transport"]["prompt_channels"]["values"].items()
        }
        self.assertEqual(actual_file, canonical_json_bytes(request))
        self.assertEqual(len(actual_file), detail.prepared_request_size_bytes)
        self.assertEqual(hashlib.sha256(actual_file).hexdigest(), detail.prepared_request_sha256)
        self.assertLessEqual(len(actual_file), execution_request_max_bytes())
        report["request_file_utf8"] = len(actual_file)
        report["request_file_limit"] = execution_request_max_bytes()
        report["request_sections"] = {key: len(canonical_json_bytes(value)) for key, value in request.items()}
        response = self.client.get(f"/api/v1/execution-preparations/{self.prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(json.loads(response.content)["prepared_request"], request)
        self.assertEqual(json.loads(response.content)["resolved_inputs"], facts)
        report["complete_detail_http_utf8"] = len(response.content)
        metrics = self.client.get(f"/api/v1/execution-preparations/{self.prep.pk}/storage-metrics/",
            {"expected_preparation_revision": self.prep.revision})
        self.assertEqual(metrics.status_code, 200, metrics.data)
        rows = {item["key"]: item for item in metrics.json()["items"]}
        self.assertEqual(set(rows), {*fields, "prepared_request"})
        for name in fields:
            self.assertEqual(rows[name]["state"], "recorded")
            self.assertEqual(rows[name]["size_bytes"], report["database_json"][name]["canonical_utf8"])
            self.assertEqual(rows[name]["sha256"], getattr(detail, f"{name}_sha256"))
        self.assertEqual(rows["prepared_request"]["size_bytes"], len(actual_file))
        self.assertEqual(rows["prepared_request"]["sha256"], hashlib.sha256(actual_file).hexdigest())
        self.assertLess(len(metrics.content), report["complete_detail_http_utf8"])
        report["metadata_http_utf8"] = len(metrics.content)
        report["database_json_total_bound_utf8"] = sum(row["sqlite_bound_utf8"] for row in report["database_json"].values())
        self.assertGreater(report["complete_detail_http_utf8"], report["field_text_utf8"])
        self.assert_no_dispatch()
        print("M6_CAPACITY " + json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return report


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class ImagePreparationCapacityTests(CapacityAssertions, TestCase):
    positive_text = "中😀" * 1000
    extra_fragments = (("system_section", "positive_prompt", "系统条件" * 300),)
    channel_limits = {"positive_prompt": {"max_chars": 4000, "warn_at_chars": None}}

    def setUp(self):
        self.start_write_capture()
        fixtures.MultiChannelFinalAssemblyTests.setUp(self)

    def test_image_multichannel_complete_preparation_round_trip(self):
        report = self.capacity_report("image_fixed_multichannel_2000_codepoints")
        self.assertEqual(report["field_request_count"], 0)
        self.assertGreater(report["database_json"]["resolved_inputs"]["canonical_utf8"], 1000)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class VideoPreparationCapacityTests(CapacityAssertions, TestCase):
    _create_version = fixtures.ReferenceFinalAssemblyTests._create_version
    selection = fixtures.ReferenceFinalAssemblyTests.selection
    configure = fixtures.ReferenceFinalAssemblyTests.configure
    candidate = fixtures.ReferenceFinalAssemblyTests.candidate

    def setUp(self):
        self.start_write_capture()
        fixtures.ReferenceFinalAssemblyTests.setUp(self)

    def test_two_reference_video_with_field_request_and_original_dialogue_round_trip(self):
        request = {"inputs": self.sources, "parameters": self.parameter_values,
            "manual_channels": {}, "subject_facts": [], "source_language": "English", "target_language": "English"}

        def resolve(definition, owner):
            return resolve_execution_request_facts(definition, request, owner=owner.owner, unit_key="composition",
                max_media_bytes=65536, review_contract=json_contract({"schema_version": 1, "mode": "manual"}),
                parameter_policies=[], scope_units={},
                resolve_parameter_source=lambda *_: self.fail("No implicit parameter source"))

        self.prep = create_execution_instance_preparation(owner_ref=("asset_job_id", self.job.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            unit_key="composition", request_key="capacity-1", intent=json_contract(request),
            resolve_facts=resolve, actor=self.user)
        selected = self.candidate("The courier turns toward the parcel. " * 55)
        report = self.capacity_report("video_two_references_reviewed_field_and_dialogue")
        self.assertGreater(report["field_request_count"], 0)
        self.assertGreater(report["resolved_sections"]["profile_sources"], 100)
        self.assertGreater(report["resolved_sections"]["parameters"], 100)
        self.assertGreater(report["resolved_sections"]["field_requests"], report["field_text_utf8"])
        final_text = self.prep.contract.prepared_request["transport"]["prompt_channels"]["values"]["positive_prompt"]
        for original in (selected.text, self.dialogue, self.voice, self.sound):
            self.assertEqual(final_text.count(original), 1)
