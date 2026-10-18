"""M2 configuration through the original output registry; isolated DB only."""
import json
from copy import deepcopy

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import BusinessOutputVersion, PlatformLifecycleAuditEvent
from studio.services.business_outputs import output_reference, verify_output_reference, write_output
from studio.services.json_contracts import json_contract
from studio.services.script_semantics import SEMANTICS_KEY, freeze_script_semantics, preview_script_semantics
from studio.tests.output_structure_fixtures import response_structure
from studio.tests.script_semantics_fixtures import script_example


class ScriptSemanticOutputTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="script-config", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.base = "/api/v1/business-output-versions/"

    def create(self, layout="a"):
        raw, _, mapping = script_example(layout)
        structure = response_structure(example=raw)
        data = {"name": "Editable script " + layout, "parser_key": SEMANTICS_KEY,
            "data_structure_version": str(structure.pk), "semantic_mapping": mapping}
        response = self.client.post(self.base, data, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return BusinessOutputVersion.objects.get(pk=response.data["id"]), raw, mapping

    def preview(self, row, raw, **changes):
        return self.client.post(f"{self.base}{row.pk}/preview-script-semantics/", {
            "expected_revision": row.revision, "expected_contract_sha256": row.contract_sha256,
            "response_text": json.dumps(raw, ensure_ascii=False), **changes}, format="json")

    def test_two_layouts_save_reopen_and_preview_without_side_effects(self):
        reports = []
        for layout in ("a", "b"):
            row, raw, mapping = self.create(layout)
            detail = self.client.get(f"{self.base}{row.pk}/")
            self.assertEqual(detail.status_code, 200, detail.data)
            self.assertEqual(detail.data["parser_contract"]["mapping"], mapping)
            before = PlatformLifecycleAuditEvent.objects.count()
            response = self.preview(row, raw)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            report = response.data
            self.assertTrue(report["valid"], report)
            self.assertEqual(report["raw_value"], raw)
            self.assertEqual(report["source_value"], raw)
            self.assertEqual(report["validation_scope"], "structure_and_mapping")
            self.assertEqual(report["output_version_id"], str(row.pk))
            self.assertEqual(report["output_contract_sha256"], row.contract_sha256)
            self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)
            reports.append(report["value"])
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(len(reports[0]["entities"][0]["variants"]), 2)
        self.assertEqual(len(reports[0]["episodes"]), 2)

    def test_lifecycle_freezes_mapping_and_keeps_original_reference(self):
        row, raw, mapping = self.create()
        published = write_output(actor=self.actor, output_id=row.pk, expected_revision=row.revision, action="publish")
        frozen = output_reference(published)
        successor = write_output(actor=self.actor, output_id=row.pk, expected_revision=published.revision, action="successor")
        self.assertEqual(successor.parser_contract["mapping"], mapping)
        new_mapping = deepcopy(mapping)
        new_mapping["node"]["fields"]["episodes"]["item"]["fields"]["title"] = {"kind": "literal", "value": "配置标题"}
        saved = self.client.patch(f"{self.base}{successor.pk}/", {"expected_revision": successor.revision,
            "semantic_mapping": new_mapping}, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertNotEqual(saved.data["contract_sha256"], successor.contract_sha256)
        self.assertEqual(frozen, output_reference(published))
        with self.assertNumQueries(0):
            reference = verify_output_reference(frozen)
            configuration = freeze_script_semantics(source_contract=reference["data_structure"],
                mapping=reference["parser"]["mapping"], max_bytes=1000000)
            self.assertTrue(preview_script_semantics(configuration=configuration,
                response_text=json.dumps(raw), max_bytes=1000000)["valid"])
        self.assertEqual(self.client.patch(f"{self.base}{published.pk}/", {
            "expected_revision": published.revision, "semantic_mapping": new_mapping}, format="json").status_code, 400)

    def test_invalid_mapping_or_structure_change_rolls_back(self):
        row, _, mapping = self.create()
        wrong = deepcopy(mapping)
        wrong["node"]["fields"]["entities"]["source"]["path"] = ["missing"]
        for data in ({"semantic_mapping": wrong}, {"semantic_mapping": {}}, {"semantic_mapping": None},
                {"data_structure_version": str(response_structure(example={"different": "shape"}).pk)},
                {"allowed_values": {}}, {"field_sources": {}}, {"response_keys": {}}, {"quality_advisories": {"unexpected": 1}}):
            response = self.client.patch(f"{self.base}{row.pk}/", {"expected_revision": row.revision, **data}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        row.refresh_from_db()
        self.assertEqual(row.revision, 1)
        self.assertEqual(row.parser_contract["mapping"], mapping)

    def test_saved_and_successor_contracts_compile_rules_without_reading_current_registry(self):
        from studio.services.script_semantic_requests import script_semantic_messages

        assembly = {"channels": {"system_instruction": "配置的写作规则", "positive_prompt": "本次创意"},
            "structured_fields": {"audit": "不得再次发送的字段快照"}}
        compiled = []
        for layout in ("a", "b"):
            row, _raw, _mapping = self.create(layout)
            row = write_output(actor=self.actor, output_id=row.pk, expected_revision=row.revision, action="publish")
            frozen = output_reference(row)
            with self.assertNumQueries(0):
                messages = script_semantic_messages(operation="script.generate", output_reference=frozen,
                    inputs={"idea": "隔离样例"}, prompt_profile_assembly=assembly, max_bytes=1_000_000)
            body = messages[0]["content"]
            self.assertIn('"motion"' if layout == "b" else '"action"', body)
            self.assertEqual(body.count("配置的写作规则"), 1)
            self.assertNotIn("不得再次发送的字段快照", str(messages))
            successor = write_output(actor=self.actor, output_id=row.pk, expected_revision=row.revision, action="successor")
            mapping = deepcopy(successor.parser_contract["mapping"])
            mapping["node"]["fields"]["episodes"]["item"]["fields"]["title"] = {
                "kind": "literal", "value": "仅后继采用的固定分集标题"}
            successor = write_output(actor=self.actor, output_id=successor.pk,
                expected_revision=successor.revision, data={"semantic_mapping": mapping})
            successor = write_output(actor=self.actor, output_id=successor.pk,
                expected_revision=successor.revision, action="publish")
            newer = output_reference(successor)
            with self.assertNumQueries(0):
                self.assertEqual(script_semantic_messages(operation="script.generate", output_reference=frozen,
                    inputs={"idea": "隔离样例"}, prompt_profile_assembly=assembly, max_bytes=1_000_000), messages)
                new_messages = script_semantic_messages(operation="script.generate", output_reference=newer,
                    inputs={"idea": "隔离样例"}, prompt_profile_assembly=assembly, max_bytes=1_000_000)
            self.assertNotIn("仅后继采用的固定分集标题", body)
            self.assertIn("仅后继采用的固定分集标题", new_messages[0]["content"])
            compiled.append(messages)
        self.assertNotEqual(compiled[0], compiled[1])

    def test_published_output_builds_unsent_chat_payload_and_reads_frozen_contract_without_queries(self):
        from studio.services.chat_body import assemble_chat_body
        from studio.services.script_semantic_requests import build_script_semantic_request, verify_script_semantic_request

        row, _raw, _mapping = self.create("b")
        row = write_output(actor=self.actor, output_id=row.pk, expected_revision=row.revision, action="publish")
        frozen = output_reference(row)
        source = " 原稿\r\n保留对白和镜头要求 😀 "
        payload, envelope = build_script_semantic_request(operation="script.structure", output_reference=frozen,
            inputs={"source_text": source}, parameters={"max_tokens": 3072, "seed": 0},
            transport_policy={"timeout_seconds": 45, "chat_request_options": {"temperature": 0}},
            prompt_profile_assembly={"channels": {"positive_prompt": "本次结构化要求"}, "structured_fields": {}},
            plan_max_bytes=1_000_000, request_max_bytes=1_000_000)
        body, _layout = assemble_chat_body("isolated-config-test", payload)
        self.assertEqual(json.loads(body["messages"][1]["content"])["inputs"]["source_text"], source)
        self.assertIn('"motion"', body["messages"][0]["content"])
        write_output(actor=self.actor, output_id=row.pk, expected_revision=row.revision, action="retire")
        with self.assertNumQueries(0):
            self.assertEqual(verify_script_semantic_request(envelope, payload, max_bytes=1_000_000,
                expected_output=frozen), frozen)
        self.assertNotIn("system_message_layout", body)

    def test_create_requires_mapping_and_does_not_accept_arbitrary_parser(self):
        structure = response_structure(example={"note": "x"})
        count = BusinessOutputVersion.objects.count()
        for extra in ({}, {"semantic_mapping": {"schema_version": 1, "node": {"kind": "eval", "code": "x"}}}):
            response = self.client.post(self.base, {"name": "invalid", "parser_key": SEMANTICS_KEY,
                "data_structure_version": str(structure.pk), **extra}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(BusinessOutputVersion.objects.count(), count)

    def test_wrong_protocol_and_revision_do_not_preview_or_reinterpret(self):
        row, raw, _ = self.create()
        for change in ({"expected_revision": 2}, {"expected_revision": True},
                {"expected_contract_sha256": "e" * 64}, {"response_text": 5}, {"extra": "ignored?"}):
            self.assertEqual(self.preview(row, raw, **change).status_code, 400)
        self.assertEqual(self.client.post(f"{self.base}{row.pk}/preview-json/", {
            "expected_revision": row.revision, "expected_contract_sha256": row.contract_sha256,
            "response_text": json.dumps(raw)}, format="json").status_code, 400)
        generic = write_output(actor=self.actor, data={"name": "generic", "parser_key": "structured_json.v1",
            "data_structure_version": row.data_structure_version_id})
        self.assertEqual(self.preview(generic, raw).status_code, 400)
        self.assertEqual(self.client.patch(f"{self.base}{generic.pk}/", {"expected_revision": generic.revision,
            "semantic_mapping": {}}, format="json").status_code, 400)

    def test_invalid_sample_keeps_original_without_claiming_relation_validation(self):
        row, raw, _ = self.create()
        raw["episodes"][0]["shots"][0]["shot"] = -1
        response = self.preview(row, raw)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["valid"])
        self.assertEqual(response.data["raw_value"], raw)
        self.assertEqual(response.data["value"]["episodes"][0]["shots"][0]["shot"], -1)
        broken = self.preview(row, raw, response_text='{"duplicate":1,"duplicate":2}')
        self.assertFalse(broken.data["parsed"])

    def test_options_permissions_and_frozen_checksum(self):
        options = self.client.get(self.base + "parsers/").data
        self.assertIn(SEMANTICS_KEY, [item["key"] for item in options["parsers"]])
        self.assertEqual(options["script_semantics"]["key"], SEMANTICS_KEY)
        row, _, _ = self.create()
        published = write_output(actor=self.actor, output_id=row.pk, expected_revision=1, action="publish")
        reference = output_reference(published)
        from studio.services.execution_output_contracts import verify_operation_output
        for operation in ("text.structured", "visual_rules.generate", "script.generate", "script.structure"):
            with self.assertRaises(ValidationError):
                verify_operation_output(operation, reference)
        reference["parser"]["mapping"]["node"]["fields"]["entities"]["source"]["path"] = ["wrong"]
        reference["sha256"] = json_contract({k:v for k,v in reference.items() if k != "sha256"}, schema_version=2).sha256
        with self.assertRaises(ValidationError):
            verify_output_reference(reference)
        self.client.force_authenticate(get_user_model().objects.create_user(username="ordinary"))
        self.assertEqual(self.preview(published, {}).status_code, 403)
        self.assertEqual(self.client.get(self.base + "parsers/").status_code, 403)
