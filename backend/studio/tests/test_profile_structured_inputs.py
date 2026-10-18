from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import override_settings
from rest_framework.test import APIClient

from studio.models import PromptProfileVersion
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_instances import update_execution_draft
from studio.services.execution_profile_inputs import project_execution_profile_sources
from studio.services.execution_profile_preparation import prepare_execution_profile
from studio.services.prompt_profiles import assemble_prompt_profile, refresh_prompt_profile_contract, publish_prompt_profile, clone_prompt_profile_successor
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR as SOURCE
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_execution_definitions as execution_fixtures
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ProfileStructuredInputTests(StructureFixture):
    write_contract = staticmethod(execution_fixtures.ExecutionDefinitionTests.write_contract)
    setUp = execution_fixtures.ExecutionDefinitionTests.setUp

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        execution_fixtures.ExecutionDefinitionTests.setUpTestData.__func__(cls)

    def profile(self):
        structure = self.publish(self.create([
            self.node("root", "object"), self.node("nested", "object", "root", property_name="自定义.输入", required=True),
            self.node("motion", "text", "nested", property_name="motion", position=0, has_default=True, default_value="wave"),
            self.node("flag", "boolean", "nested", property_name="flag", position=1),
            self.node("count", "integer", "nested", property_name="count", position=2),
        ]))
        profile = PromptProfileVersion.objects.create(profile_key="structured-profile", version=1, name="Structured profile",
            model_family="async_cloud", operation="video.generate", mode="T2VA", review_state="ready", input_structure_version=structure)
        for index, (key, kind) in enumerate((("motion", "text"), ("flag", "boolean"), ("count", "integer")), 1):
            field = create_field_definition(key="custom_" + key, label=key, value_type=kind)
            create_profile_field_rule(profile_version=profile, field_definition=field, source_selector=SOURCE,
                source_path=["自定义.输入", key], generation_source_selector=SOURCE, channel="positive_prompt",
                position=index, required=True, max_chars=4000)
        refresh_prompt_profile_contract(profile)
        return publish_prompt_profile(profile, actor=self.actor)

    def test_same_root_multiple_typed_paths_freeze_prepare_and_compile_without_new_fields(self):
        profile = self.profile()
        version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision,
            changes={"prompt_mode": "profile", "prompt_profile_version": profile, "inputs": []}, actor=self.actor)
        definition = build_execution_definition(version.pk)
        spec = definition.value["configuration"]["inputs"][0]
        self.assertEqual(spec["data_structure_version_id"], str(profile.input_structure_version_id))
        self.assertEqual(len(definition.value["configuration"]["inputs"]), 1)
        raw = {"自定义.输入": {"flag": False, "count": 0}, "unknown": "keep"}
        original = deepcopy(raw)
        resolved = resolve_execution_input_sources(definition, {SOURCE: {"kind": "literal", "value": raw}},
            owner=self.actor, subject_facts=[], max_media_bytes=65536)
        projection = project_execution_profile_sources(definition, resolved.value["logical_values"])
        prepared = prepare_execution_profile(definition, resolved, profile=profile, unit_key="fixture",
            source_language="zh", target_language="en")
        self.assertEqual(prepared.value["profile_sources"]["projection"], projection)
        result = assemble_prompt_profile(profile, sources=projection["assembly_sources"])
        self.assertEqual(result["structured_fields"], {"custom_motion": "wave", "custom_flag": False, "custom_count": 0})
        self.assertEqual(raw, original)
        self.assertEqual(projection["assembly_sources"][SOURCE]["unknown"], "keep")
        self.assertEqual(result["trace"][0]["source_path"], ["自定义.输入", "motion"])
        self.assertTrue(resolved.value["input_provenance"][0]["structure_resolution"]["defaults"])

    def test_missing_null_wrong_type_and_invalid_path_are_not_replaced(self):
        profile = self.profile()
        for raw in ({}, {"自定义.输入": None}, {"自定义.输入": {"flag": "false", "count": 0}}):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                assemble_prompt_profile(profile, sources={SOURCE: raw})
        rule = profile.field_rules.first()
        rule.source_path = ["自定义.输入", "flag"]
        from studio.services.prompt_profile_structures import validate_structured_rule
        with self.assertRaisesMessage(ValidationError, "类型与规则字段不符"):
            validate_structured_rule(rule)
        rule.source_path = ["undeclared"]
        with self.assertRaisesMessage(ValidationError, "路径不在所选结构目录"):
            validate_structured_rule(rule)

    def test_mixed_input_inherits_structure_without_second_choice_and_rejects_conflict(self):
        from studio.services.execution_profile_inputs import derived_profile_inputs, reconcile_profile_inputs
        profile = self.profile()
        self.version.prompt_mode, self.version.prompt_profile_version = "profile", profile
        pure = derived_profile_inputs(profile)[0]
        mixed = {**deepcopy(pure), "key": "physical_facts"}
        mixed.pop("data_structure_version_id")
        mixed["targets"].append({"target_kind": "api_input", "target_key": "facts", "is_primary": True})
        original = deepcopy(mixed)
        for cloning in (False, True):
            for supplied in (None, str(profile.input_structure_version_id)):
                rows = reconcile_profile_inputs(self.version, [pure, {**mixed, "data_structure_version_id": supplied}], cloning=cloning)
                self.assertEqual(rows, [{**mixed, "data_structure_version_id": str(profile.input_structure_version_id)}])
            with self.assertRaisesMessage(ValidationError, "不能另选结构版本"):
                reconcile_profile_inputs(self.version, [{**mixed, "data_structure_version_id": "00000000-0000-4000-8000-000000000000"}], cloning=cloning)
        self.assertEqual(reconcile_profile_inputs(self.version, [mixed])[0]["data_structure_version_id"], str(profile.input_structure_version_id))
        self.assertEqual(mixed, original)

    def test_form_keeps_one_identical_schema_and_all_distinct_target_constraints(self):
        from studio.services.execution_input_catalog import execution_input_form_options
        from studio.services.execution_input_structures import input_structure_schemas
        profile = self.profile()
        version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision,
            changes={"prompt_mode": "profile", "prompt_profile_version": profile, "inputs": []}, actor=self.actor)
        definition = build_execution_definition(version.pk).value
        schema = input_structure_schemas(definition)[SOURCE]
        self.assertEqual(execution_input_form_options(definition)[SOURCE]["schemas"], [schema])
        target = definition["interface"]["profile_inputs"][SOURCE]
        target["value_schema"] = deepcopy(schema)
        self.assertEqual(execution_input_form_options(definition)[SOURCE]["schemas"], [schema])
        constraint = {"type": "object", "maxProperties": 2}
        definition["interface"]["input_targets"]["facts"] = {"value_schema": constraint}
        definition["configuration"]["inputs"][0]["targets"].append({"target_kind": "api_input", "target_key": "facts", "is_primary": True})
        before = deepcopy(definition)
        self.assertEqual(execution_input_form_options(definition)[SOURCE]["schemas"], [schema, constraint])
        self.assertEqual(definition, before)
        # JSON true and 1 are distinct constraints even though Python compares them equal.
        definition["interface"]["input_targets"]["facts"]["value_schema"] = {"const": True}
        definition["interface"]["profile_inputs"][SOURCE]["value_schema"] = {"const": 1}
        self.assertEqual(execution_input_form_options(definition)[SOURCE]["schemas"], [schema, {"const": True}, {"const": 1}])

    def test_successor_and_api_preserve_structure_and_paths_without_changing_original(self):
        profile = self.profile()
        original_sha = profile.contract_sha256
        successor = clone_prompt_profile_successor(profile, expected_source_contract_sha256=original_sha, name="Successor")
        self.assertEqual(successor.input_structure_version_id, profile.input_structure_version_id)
        self.assertEqual(list(successor.field_rules.values_list("source_path", flat=True)), list(profile.field_rules.values_list("source_path", flat=True)))
        client = APIClient(); client.force_authenticate(self.actor)
        result = client.get(f"/api/v1/prompt-profile-versions/{successor.pk}/")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(str(result.data["input_structure_version"]), str(profile.input_structure_version_id))
        profile.refresh_from_db(); self.assertEqual(profile.contract_sha256, original_sha)

    def test_api_preview_candidate_path_changes_do_not_write_and_invalid_updates_roll_back(self):
        profile = self.profile()
        draft = clone_prompt_profile_successor(profile, expected_source_contract_sha256=profile.contract_sha256, name="Preview")
        client = APIClient(); client.force_authenticate(self.actor)
        base = f"/api/v1/prompt-profile-versions/{draft.pk}/"
        raw = {"自定义.输入": {"motion": "turn", "flag": False, "count": 0}}
        preview = client.post(base + "preview/", {"sources": {SOURCE: raw}}, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["structured_fields"]["custom_motion"], "turn")
        from studio.platform_serializers import PromptProfileFieldRuleSerializer
        fields = ("field_definition", "field_definition_version", "source_selector", "source_path",
                  "projection_primitive", "channel", "position", "label_style", "separator", "required",
                  "fixed_value", "has_default", "default_value", "omit_if_empty", "max_chars", "generation_mode",
                  "generation_source_selector", "generation_instruction", "generation_output_semantics", "generation_required",
                  "language_policy", "length_unit", "generation_min", "generation_max", "generation_validator_keys", "generation_context_selectors")
        candidate = {key: getattr(draft, key) for key in ("model_family", "operation", "mode", "source_projection_key", "source_projection_config", "channel_limits")}
        candidate["input_structure_version"] = str(draft.input_structure_version_id)
        candidate["field_rules"] = [{key: PromptProfileFieldRuleSerializer(rule).data[key] for key in fields} for rule in draft.field_rules.all()]
        result = client.post(base + "preview/", {"sources": {SOURCE: raw}, "candidate": candidate,
                            "expected_base_contract_sha256": draft.contract_sha256}, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        candidate["field_rules"][0]["source_path"] = ["unknown"]
        bad = client.post(base + "preview/", {"sources": {SOURCE: raw}, "candidate": candidate,
                         "expected_base_contract_sha256": draft.contract_sha256}, format="json")
        self.assertEqual(bad.status_code, 400, bad.data)
        original_sha = draft.contract_sha256
        bad = client.patch(base, {"input_structure_version": None}, format="json")
        self.assertEqual(bad.status_code, 400, bad.data)
        rule = draft.field_rules.first()
        bad = client.patch(f"/api/v1/prompt-profile-field-rules/{rule.pk}/", {"source_path": ["unknown"]}, format="json")
        self.assertEqual(bad.status_code, 400, bad.data)
        draft.refresh_from_db(); rule.refresh_from_db()
        self.assertEqual(draft.contract_sha256, original_sha)
        self.assertEqual(draft.input_structure_version_id, profile.input_structure_version_id)
        self.assertEqual(rule.source_path, ["自定义.输入", "motion"])

    def test_contract_writer_checks_paths_against_its_exact_structure(self):
        from studio.services.prompt_profiles import prompt_profile_contract_payload, verify_prompt_profile_contract, validate_profile_rule
        profile = self.profile()
        contract = prompt_profile_contract_payload(profile)
        self.assertEqual(verify_prompt_profile_contract(profile), contract)
        for change in ("no_structure", "wrong_type", "unknown", "model_generated", "nontext_generation"):
            altered = deepcopy(profile)
            rules = list(altered.field_rules.order_by("position"))
            if change == "no_structure": altered.input_structure_version = None
            elif change == "wrong_type": rules[0].source_path = ["自定义.输入", "flag"]
            elif change == "unknown": rules[0].source_path = ["absent"]
            elif change == "model_generated": rules[0].generation_mode = change
            else: rules[1].generation_mode = "model_generated"
            for rule in rules:
                rule.profile_version = altered
            with self.subTest(change=change), self.assertRaises(ValidationError):
                for rule in rules:
                    validate_profile_rule(rule)
                prompt_profile_contract_payload(altered, rules=rules)

    def test_nullable_list_and_explicit_array_offset_share_declared_path_reader(self):
        from studio.services.prompt_profile_structures import validate_structured_path, read_profile_path
        from studio.services.data_structures import data_structure_snapshot
        structure = self.publish(self.create([
            self.node("root", "object"), self.node("names", "list", "root", property_name="names", allow_null=True),
            self.node("item", "text", "names"),
        ]))
        snapshot = data_structure_snapshot(structure)
        validate_structured_path(snapshot, ["names"], "list")
        validate_structured_path(snapshot, ["names", 0], "text")
        self.assertEqual(read_profile_path({"names": ["a"]}, ["names", 0], "first")[0], "a")
        self.assertIsNone(read_profile_path({"names": None}, ["names"], "all")[0])
        for raw in ({"names": []}, {"names": None}):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                read_profile_path(raw, ["names", 0], "first")
