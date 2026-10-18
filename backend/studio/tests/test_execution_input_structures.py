"""Exact input structures through authoring, freezing, forms and preparation reads."""
from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.services.data_structures import data_structure_snapshot, transition_data_structure
from studio.services.execution_definitions import build_execution_definition, verify_execution_definition_payload
from studio.services.execution_input_catalog import execution_input_form_options
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_input_structures import input_structure_schemas, resolve_input_structure
from studio.services.execution_instances import execution_version_configuration, update_execution_draft, clone_execution_draft
from studio.services.json_contracts import json_contract
from studio.services.production_recipe_contracts import validate_recipe_input_sources
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_execution_text_contracts as text_fixtures
from studio.tests import test_production_reviewed_references as production_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionInputStructureTests(StructureFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        text_fixtures.ExecutionTextContractTests.setUpTestData.__func__(cls)

    setUp = text_fixtures.ExecutionTextContractTests.setUp
    version = text_fixtures.ExecutionTextContractTests.version
    approve = production_fixtures.ReviewedReferenceTests.approve

    def production_context(self):
        production_fixtures.ReviewedReferenceTests.setUp(self)
        return {"project_id": self.project.pk, "production_run_id": str(self.run.pk),
            "production_scope": {"kind": "shot", "id": str(self.shot.pk)}, "unit_key": f"shot:{self.shot.pk}"}

    def configure(self, nodes=None):
        structure = self.publish(self.create(nodes or [
            self.node("root", "object"),
            self.node("custom", "text", "root", property_name="自定义动作", required=True,
                has_default=True, default_value="wave"),
        ]))
        version = self.version("prompt.fields.generate")
        inputs = execution_version_configuration(version)["inputs"]
        inputs[0]["data_structure_version_id"] = str(structure.pk)
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision,
            actor=self.actor, changes={"inputs": inputs})
        return version, structure

    def resolve(self, definition, value):
        return resolve_execution_input_sources(definition, {"fields": {"kind": "literal", "value": value}},
            owner=self.actor, subject_facts=[], max_media_bytes=65536).value

    def test_authoring_freeze_form_and_port_use_one_exact_structure_without_registry_reads(self):
        version, structure = self.configure()
        definition = build_execution_definition(version.pk)
        self.assertEqual(definition.value["configuration"]["inputs"][0]["data_structure_version_id"], str(structure.pk))
        self.assertEqual(definition.value["interface"]["input_structures"]["fields"], data_structure_snapshot(structure))
        raw = {"unknown": {"raw": False}}
        with CaptureQueriesContext(connection) as queries:
            verify_execution_definition_payload(definition)
            forms = execution_input_form_options(definition.value)
            self.assertEqual(forms["fields"]["schemas"][0]["properties"]["自定义动作"]["default"], "wave")
            result = self.resolve(definition, raw)
        self.assertFalse(any('studio_datastructure' in row['sql'] or 'studio_fielddefinition' in row['sql'] for row in queries))
        expected = {**raw, "自定义动作": "wave"}
        self.assertEqual(result["logical_values"]["fields"], expected)
        self.assertEqual(result["operation_inputs"]["field_request"], expected)
        self.assertEqual(raw, {"unknown": {"raw": False}})
        origin = result["input_provenance"][0]
        self.assertEqual(origin["kind"], "literal")
        self.assertEqual(origin["structure_resolution"]["source_value_sha256"], json_contract(raw).sha256)
        self.assertEqual(origin["value_sha256"], json_contract(expected).sha256)
        self.assertEqual(origin["structure_resolution"]["defaults"][0]["structure_version_id"], str(structure.pk))

    def test_explicit_null_blank_zero_false_and_unknown_fields_are_not_replaced(self):
        version, _ = self.configure([
            self.node("root", "object"),
            self.node("text", "text", "root", property_name="text", position=0, allow_null=True, has_default=True, default_value="default"),
            self.node("count", "integer", "root", property_name="count", position=1, has_default=True, default_value=4),
            self.node("flag", "boolean", "root", property_name="flag", position=2, has_default=True, default_value=True),
        ])
        definition = build_execution_definition(version.pk)
        for text in (None, ""):
            raw = {"text": text, "count": 0, "flag": False, "extra": [1]}
            result = self.resolve(definition, raw)
            self.assertEqual(result["operation_inputs"]["field_request"], raw)
            self.assertEqual(result["input_provenance"][0]["structure_resolution"]["defaults"], [])

    def test_generic_output_cannot_be_misread_as_visual_rules_in_a_frozen_execution(self):
        from studio.services.business_outputs import output_reference, write_output
        version, structure = self.configure()
        output = write_output(actor=self.actor, data={"name": "Structured result", "parser_key": "structured_json.v1",
            "data_structure_version": structure.pk})
        output = write_output(actor=self.actor, output_id=output.pk, expected_revision=1, action="publish")
        value = deepcopy(build_execution_definition(version.pk).value)
        value["configuration"]["output_version_id"] = str(output.pk)
        value["interface"]["business_output"] = output_reference(output)
        with self.assertRaises(ValidationError):
            verify_execution_definition_payload(json_contract(value))

    def test_precise_errors_and_native_target_constraints_apply_after_defaults(self):
        version, _ = self.configure()
        definition = build_execution_definition(version.pk)
        with self.assertRaises(ValidationError) as error:
            self.resolve(definition, {"自定义动作": 5})
        self.assertIn('inputs.fields["自定义动作"]', error.exception.message_dict)
        value = deepcopy(definition.value)
        value["interface"]["input_targets"]["field_request"]["value_schema"] = {
            "type": "object", "properties": {"自定义动作": {"type": "string", "enum": ["wave"]}}, "required": ["自定义动作"],
        }
        self.assertEqual(self.resolve(json_contract(value), {})["operation_inputs"]["field_request"], {"自定义动作": "wave"})
        with self.assertRaises(ValidationError):
            self.resolve(json_contract(value), {"自定义动作": "other"})

    def test_frozen_identity_missing_or_mismatched_fails_and_retired_does_not_rewrite(self):
        version, structure = self.configure()
        frozen = build_execution_definition(version.pk)
        for mutate in (lambda value: value["interface"].pop("input_structures"),
                lambda value: value["interface"]["input_structures"]["fields"].update(id=str(uuid4())),
                lambda value: value["configuration"]["inputs"][0].pop("data_structure_version_id")):
            value = deepcopy(frozen.value)
            mutate(value)
            with self.assertRaises(ValidationError):
                verify_execution_definition_payload(json_contract(value))
            with self.assertRaises(ValidationError):
                self.resolve(json_contract(value), {})
        transition_data_structure(version_id=structure.pk, status="retired")
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.resolve(frozen, {})["logical_values"]["fields"], {"自定义动作": "wave"})
        self.assertFalse(any('studio_datastructure' in row['sql'] or 'studio_fielddefinition' in row['sql'] for row in queries))
        with self.assertRaises(ValidationError):
            build_execution_definition(version.pk)

    def test_api_round_trip_clone_clear_and_invalid_reference_roll_back(self):
        version, structure = self.configure()
        client = APIClient()
        client.force_authenticate(self.actor)
        url = f"/api/v1/model-execution-instance-versions/{version.pk}/"
        response = client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        inputs = response.data["configuration"]["inputs"]
        self.assertEqual(inputs[0]["data_structure_version_id"], str(structure.pk))
        clone = clone_execution_draft(source_id=version.pk, expected_revision=version.revision, actor=self.actor)
        self.assertEqual(execution_version_configuration(clone)["inputs"], inputs)
        for identity in (str(uuid4()), str(self.create().pk)):
            invalid = deepcopy(inputs)
            invalid[0]["data_structure_version_id"] = identity
            response = client.patch(url, {"expected_revision": version.revision, "inputs": invalid}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(execution_version_configuration(version)["inputs"], inputs)
        inputs[0]["data_structure_version_id"] = None
        response = client.patch(url, {"expected_revision": version.revision, "inputs": inputs}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("data_structure_version_id", response.data["configuration"]["inputs"][0])

    def test_text_rejects_non_string_root_media_rejects_structure(self):
        version, structure = self.configure()
        spec = version.inputs.get()
        for kind in ("text", "image", "video", "audio"):
            spec.kind = kind
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                spec.full_clean()
        value = build_execution_definition(version.pk).value
        value["interface"]["input_structures"]["fields"] = data_structure_snapshot(
            self.publish(self.create([self.node("root", "text", allow_null=True)])))
        value["configuration"]["inputs"][0].update(kind="text", data_structure_version_id=value["interface"]["input_structures"]["fields"]["id"])
        with self.assertRaises(ValidationError):
            input_structure_schemas(value)

    def test_non_expanding_input_is_charged_once_not_twice(self):
        structure = self.publish(self.create([self.node("root", "text")]))
        definition = {"interface": {"input_structures": {"custom": data_structure_snapshot(structure)}}}
        raw = "x" * 40000
        with patch("studio.services.execution_request_storage.execution_request_max_bytes", return_value=65536):
            with self.assertNumQueries(0):
                resolved, origin = resolve_input_structure(definition, "custom", raw)
                self.assertEqual(resolved, raw)
                self.assertEqual(origin["defaults"], [])
            with self.assertRaises(ValidationError):
                resolve_input_structure(definition, "custom", "x" * 65536)

    def test_recipe_preflight_and_runtime_share_defaults_without_changing_template(self):
        from studio.services.script_structure import script_structure_planning_schema
        version, _ = self.configure([
            self.node("root", "object"),
            self.node("episodes", "integer", "root", property_name="episode_count", position=0, required=True),
            self.node("shots", "integer", "root", property_name="shots_per_episode", position=1, required=True),
            self.node("duration", "integer", "root", property_name="episode_duration_seconds", position=2,
                required=True, has_default=True, default_value=60),
        ])
        payload = build_execution_definition(version.pk).value
        payload["interface"]["input_targets"]["field_request"]["value_schema"] = script_structure_planning_schema()
        definition = json_contract(payload)
        production = self.production_context()
        raw = {"episode_count": 1, "shots_per_episode": 2}
        for source in ({"kind": "literal", "value": raw}, {"kind": "structured", "template": {
                "kind": "object", "fields": {key: {"kind": "literal", "value": value} for key, value in raw.items()}}}):
            with self.subTest(source=source["kind"]):
                rules = {"fields": source}
                original = deepcopy(rules)
                validate_recipe_input_sources(definition, rules, prior_stages=[])
                result = resolve_execution_input_sources(definition, rules,
                    owner=self.actor, subject_facts=[], max_media_bytes=65536, **production).value
                self.assertEqual(result["operation_inputs"]["field_request"], {**raw, "episode_duration_seconds": 60})
                self.assertEqual(result["input_provenance"][0]["structure_resolution"]["defaults"][0]["value"], 60)
                self.assertEqual(rules, original)
        for value in (None, 0, False, "", []):
            rules = {"fields": {"kind": "structured", "template": {"kind": "literal", "value": {
                **raw, "episode_duration_seconds": value}}}}
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_recipe_input_sources(definition, rules, prior_stages=[])
        payload["configuration"]["inputs"][0].pop("data_structure_version_id")
        payload["interface"].pop("input_structures")
        payload["interface"]["input_targets"]["field_request"]["value_schema"]["properties"]["episode_duration_seconds"]["default"] = 60
        with self.assertRaisesMessage(ValidationError, "必需字段"):
            validate_recipe_input_sources(json_contract(payload), {"fields": {"kind": "structured", "template": {
                "kind": "object", "fields": {key: {"kind": "literal", "value": value} for key, value in raw.items()}}}}, prior_stages=[])

    def test_recipe_nested_array_defaults_keep_parent_missing_and_null_distinct(self):
        version, _ = self.configure([
            self.node("root", "object"),
            self.node("rows", "list", "root", property_name="rows", required=True),
            self.node("row", "object", "rows", allow_null=True),
            self.node("flag", "boolean", "row", property_name="flag", required=True, has_default=True, default_value=False),
            self.node("settings", "object", "root", property_name="settings", position=1,
                has_default=True, default_value={}),
            self.node("count", "integer", "settings", property_name="count", required=True, has_default=True, default_value=0),
        ])
        definition = build_execution_definition(version.pk)
        template = {"kind": "object", "fields": {"rows": {"kind": "array", "items": [
            {"kind": "object", "fields": {}}, {"kind": "literal", "value": None}]}}}
        rules = {"fields": {"kind": "structured", "template": template}}
        original = deepcopy(rules)
        validate_recipe_input_sources(definition, rules, prior_stages=[])
        production = self.production_context()
        result = resolve_execution_input_sources(definition, rules, owner=self.actor, subject_facts=[], max_media_bytes=65536, **production).value
        self.assertEqual(result["logical_values"]["fields"], {"rows": [{"flag": False}, None], "settings": {"count": 0}})
        self.assertEqual(rules, original)
        for bad in ({"kind": "object", "fields": {}}, {"kind": "literal", "value": {}},
                {"kind": "object", "fields": {"rows": {}}}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                validate_recipe_input_sources(definition, {"fields": {"kind": "structured", "template": bad}}, prior_stages=[])
        invalid = deepcopy(rules)
        invalid["fields"]["template"]["fields"]["rows"]["items"][0]["fields"]["flag"] = {"kind": "literal", "value": 0}
        with self.assertRaises(ValidationError):
            validate_recipe_input_sources(definition, invalid, prior_stages=[])

    def test_recipe_checks_default_against_native_contract_and_exact_frozen_pair(self):
        version, structure = self.configure()
        payload = build_execution_definition(version.pk).value
        payload["interface"]["input_targets"]["field_request"]["value_schema"] = {
            "type": "object", "properties": {"自定义动作": {"type": "string", "enum": ["walk"]}}, "required": ["自定义动作"]}
        rules = {"fields": {"kind": "structured", "template": {"kind": "object", "fields": {}}}}
        with self.assertRaises(ValidationError):
            validate_recipe_input_sources(json_contract(payload), rules, prior_stages=[])
        payload["interface"]["input_targets"]["field_request"]["value_schema"]["properties"]["自定义动作"]["enum"] = ["wave"]
        transition_data_structure(version_id=structure.pk, status="retired")
        with CaptureQueriesContext(connection) as queries:
            validate_recipe_input_sources(json_contract(payload), rules, prior_stages=[])
        self.assertFalse(any('studio_datastructure' in row['sql'] or 'studio_fielddefinition' in row['sql'] for row in queries))
        payload["interface"]["input_structures"]["fields"]["id"] = str(uuid4())
        with self.assertRaises(ValidationError):
            validate_recipe_input_sources(json_contract(payload), rules, prior_stages=[])

    def test_recipe_each_defaults_wait_for_real_reviewed_items_and_leave_source_unchanged(self):
        from studio.services.production_recipe_media_preparations import resolve_recipe_media_sources
        from studio.services.production_run_plans import get_run_execution_plan
        from studio.tests import test_production_shot_context as shot_fixtures
        version, _ = self.configure([
            self.node("root", "list"), self.node("item", "object", "root"),
            self.node("content", "text", "item", property_name="content", position=0, required=True),
            self.node("flag", "boolean", "item", property_name="flag", position=1, required=True, has_default=True, default_value=False),
        ])
        payload = build_execution_definition(version.pk).value
        payload["configuration"]["inputs"][0]["semantic_contract"]["allowed_scope_kinds"] = ["shot"]
        definition = json_contract(payload)
        production = self.production_context()
        original = [{"speaker_id": "S1", "text": "真实测试对白"}]
        self.shot.dialogue = deepcopy(original)
        self.shot.save(update_fields=("dialogue",))
        shot_fixtures.ShotContextTests.reapprove(self)
        rules = {"fields": {"kind": "structured", "template": {"kind": "each", "source": {
            "kind": "context_field", "selector_key": "shot.dialogue", "scope": {"kind": "shot"}},
            "item": {"kind": "object", "fields": {"content": {"kind": "item_field", "path": ["text"]}}}}}}
        before = deepcopy(rules)
        validate_recipe_input_sources(definition, rules, prior_stages=["storyboard_review"])
        bound = resolve_recipe_media_sources(definition=definition, route={"input_sources": rules}, stage=self.stage,
            plan=get_run_execution_plan(self.run), scope=production["production_scope"])
        result = resolve_execution_input_sources(definition, bound["inputs"], owner=self.actor, subject_facts=[],
            max_media_bytes=65536, eligible_stage_ids=bound["eligible_stage_ids"], **production).value
        self.assertEqual(result["logical_values"]["fields"], [{"content": "真实测试对白", "flag": False}])
        self.assertEqual(result["input_provenance"][0]["structure_resolution"]["defaults"][0]["path"], "inputs.fields[0].flag")
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.dialogue, original)
        self.assertEqual(rules, before)

    def test_recipe_non_expanding_literal_does_not_pay_template_size_twice(self):
        version, _ = self.configure([self.node("root", "text")])
        definition = build_execution_definition(version.pk)
        rules = {"fields": {"kind": "structured", "template": {"kind": "literal", "value": "x" * 40000}}}
        with patch("studio.services.execution_instances.execution_configuration_max_bytes", return_value=65536):
            validate_recipe_input_sources(definition, rules, prior_stages=[])

    def test_template_nullable_branch_does_not_skip_outer_target_constraints(self):
        from studio.services.execution_structure_mapping import validate_template_schema
        schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}},
            "anyOf": [{"type": "object"}, {"type": "null"}]}
        with self.assertRaisesMessage(ValidationError, "必需字段"):
            validate_template_schema({"kind": "object", "fields": {}}, schema, "input")
