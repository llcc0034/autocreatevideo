import json
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient

from studio.models import BusinessOutputVersion, PlatformLifecycleAuditEvent
from studio.services.business_outputs import JSON_PARSER_KEY, output_reference, write_output
from studio.services.data_structures import data_structure_snapshot
from studio.services.structured_output_parsing import parse_structured_output
from studio.services.structure_mapping import preview_structure_mapping
from studio.tests.test_data_structures import StructureFixture


class StructuredOutputTests(StructureFixture):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="json-output-owner", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.base = "/api/v1/business-output-versions/"

    def structure(self, group="chapters", note="mood.note"):
        return self.publish(self.create([
            self.node("root", "object"),
            self.node("group", "list", "root", property_name=group, required=True),
            self.node("item", "object", "group"),
            self.node("note", "text", "item", property_name=note, required=True),
            self.node("flag", "boolean", "item", property_name="active", has_default=True, default_value=False),
        ]))

    def output(self, structure=None):
        return write_output(actor=self.actor, data={"name": "Configurable JSON", "parser_key": JSON_PARSER_KEY,
            "data_structure_version": (structure or self.structure()).pk})

    def preview(self, row, text, **changes):
        return self.client.post(f"{self.base}{row.pk}/preview-json/", {
            "expected_revision": row.revision, "expected_contract_sha256": row.contract_sha256,
            "response_text": text, **changes}, format="json")

    def test_authoring_lifecycle_and_exact_structure_catalog(self):
        structure = self.structure()
        created = self.client.post(self.base, {"name": "JSON output", "parser_key": JSON_PARSER_KEY,
            "data_structure_version": str(structure.pk)}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        row = BusinessOutputVersion.objects.get(pk=created.data["id"])
        self.assertEqual(row.parser_contract, {"key": JSON_PARSER_KEY, "format": "json"})
        published = write_output(actor=self.actor, output_id=row.pk, expected_revision=row.revision, action="publish")
        frozen = output_reference(published)
        report = self.preview(published, '{"chapters":[{"mood.note":"calm"}]}')
        self.assertEqual(report.status_code, 200, report.data)
        self.assertTrue(report.data["valid"])
        self.assertIn(["chapters", {"list_item": True}, "mood.note"], [r["path"] for r in report.data["fields"]])
        next_version = write_output(actor=self.actor, output_id=published.pk, expected_revision=published.revision, action="successor")
        changed = write_output(actor=self.actor, output_id=next_version.pk, expected_revision=next_version.revision,
            data={"data_structure_version": self.structure("scenes", "情绪").pk})
        self.assertNotEqual(changed.contract_sha256, published.contract_sha256)
        retired = write_output(actor=self.actor, output_id=published.pk, expected_revision=published.revision, action="retire")
        self.assertEqual(output_reference(retired), frozen)
        with self.assertNumQueries(0):
            parsed = parse_structured_output(reference=frozen, response_text='{"chapters":[]}', max_bytes=100000)
        self.assertTrue(parsed["valid"])
        self.assertNotEqual(changed.data_structure_version_id, structure.pk)

    def test_result_keeps_unknown_values_defaults_and_actual_invalid_content(self):
        row = self.output()
        before_events = PlatformLifecycleAuditEvent.objects.count()
        source = {"chapters": [{"mood.note": "", "extra": {"raw": 0}}], "unmapped": None}
        result = self.preview(row, json.dumps(source)).data
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["raw_value"], source)
        self.assertIs(result["value"]["chapters"][0]["active"], False)
        self.assertEqual(result["defaults"][0]["source"], "configured_default")
        self.assertEqual(len(result["warnings"]), 2)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before_events)
        invalid = {"chapters": [{"mood.note": None, "active": "false"}]}
        report = self.preview(row, json.dumps(invalid))
        self.assertFalse(report.data["valid"])
        self.assertTrue(report.data["parsed"])
        self.assertEqual(report.data["value"], invalid)
        self.assertIn('response.chapters[0]["mood.note"]', report.data["errors"])
        for text in ('{"chapters":[],"chapters":[]}', 'not JSON', '{"chapters":NaN}'):
            report = self.preview(row, text)
            self.assertFalse(report.data["parsed"])
            self.assertFalse(report.data["valid"])
            self.assertTrue(report.data["errors"])

    def test_generic_output_does_not_accept_visual_semantics_or_execute_a_business_action(self):
        row = self.output()
        for changes in ({"quality_advisories": {"schema_version": 1}}, {"allowed_values": {}},
                {"field_sources": {}}, {"response_keys": {}}, {"parser_key": "visual_rules.v2"}):
            response = self.client.patch(f"{self.base}{row.pk}/", {"expected_revision": row.revision, **changes}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.client.post(self.base + "preview-visual-rules-output/", {
            "output_version_id": str(row.pk), "expected_revision": row.revision, "response_text": "{}"}, format="json").status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.revision, 1)
        published = write_output(actor=self.actor, output_id=row.pk, expected_revision=1, action="publish")
        frozen = output_reference(published)
        from studio.services.business_outputs import visual_rule_review_contract
        from studio.services.production_visual_rules import finalize_visual_rules
        with self.assertRaises(ValidationError):
            visual_rule_review_contract(frozen)
        with self.assertRaises(ValidationError):
            finalize_visual_rules(plan={"business_output": frozen}, payload={"chapters": []})

    def test_preview_identity_access_and_configuration_errors_are_not_success_reports(self):
        row = self.output()
        for changes in ({"expected_revision": True}, {"expected_revision": row.revision + 1},
                {"expected_contract_sha256": "e" * 64}, {"run_id": "not allowed"}):
            self.assertEqual(self.preview(row, "{}", **changes).status_code, 400)
        for raw in (0, 1.5, False, None, []):
            self.assertEqual(self.preview(row, raw).status_code, 400)
        self.client.force_authenticate(get_user_model().objects.create_user(username="not-staff"))
        self.assertEqual(self.preview(row, "{}").status_code, 403)

    def test_deep_unknown_value_reports_failure_without_losing_parsed_original(self):
        row = self.output()
        source = '{"chapters":[],"extra":' + '[' * 600 + '0' + ']' * 600 + '}'
        response = self.preview(row, source)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["valid"])
        self.assertTrue(response.data["parsed"])
        self.assertIn("嵌套过深", str(response.data["errors"]))
        self.assertIn("extra", response.data["raw_value"])

    def test_layout_a_and_b_use_same_parser_and_shared_downstream_mapping(self):
        target = data_structure_snapshot(self.publish(self.create([
            self.node("root", "list"), self.node("item", "text", "root"),
        ])))
        for group, note in (("chapters", "mood.note"), ("scenes", "情绪")):
            row = self.output(self.structure(group, note))
            published = write_output(actor=self.actor, output_id=row.pk, expected_revision=1, action="publish")
            ref = output_reference(published)
            original = deepcopy(ref)
            text = json.dumps({group: [{note: "calm"}, {note: "tense", "active": False}]})
            parsed = parse_structured_output(reference=ref, response_text=text, max_bytes=100000)
            self.assertTrue(parsed["valid"])
            mapping = {"kind": "each", "source": {"kind": "path", "scope": "root", "path": [group]},
                "item": {"kind": "path", "scope": "item", "path": [note]}}
            report = preview_structure_mapping(source_contract=ref["data_structure"], target_contract=target,
                source_text=text, mapping={"schema_version": 1, "node": mapping}, max_bytes=100000)
            self.assertTrue(report["valid"], report)
            self.assertEqual(report["value"], ["calm", "tense"])
            self.assertEqual(ref, original)

    def test_any_json_root_and_limits_use_declared_structure_not_visual_object_rules(self):
        for kind, text, expected in (("boolean", "false", False), ("integer", "0", 0), ("text", '""', "")):
            row = self.output(self.publish(self.create([self.node("root", kind)])))
            report = self.preview(row, text)
            self.assertTrue(report.data["valid"], report.data)
            self.assertEqual(report.data["value"], expected)
        row = self.output(self.publish(self.create([self.node("root", "text", allow_null=True)])))
        report = self.preview(row, "null")
        self.assertTrue(report.data["valid"])
        self.assertTrue(report.data["parsed"])
        self.assertIsNone(report.data["value"])
        with patch("studio.services.security_boundaries.boundary_int", return_value=8):
            response = self.preview(row, '"too much text"')
        self.assertFalse(response.data["valid"])
