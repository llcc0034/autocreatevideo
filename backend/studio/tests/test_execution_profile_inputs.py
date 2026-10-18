from studio.tests.field_definition_fixtures import create_field_definition
"""Profile source wiring is exact, typed and independent of Binding lookup."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import FieldDefinition, PromptProfileFieldRule, PromptProfileVersion
from studio.services.execution_definitions import build_execution_definition, execution_authoring_options, verify_current_execution_definition
from studio.services.execution_instances import create_execution_draft, clone_execution_draft, execution_version_configuration, publish_execution_version, update_execution_draft
from studio.services.execution_profile_inputs import profile_input_targets, reconcile_profile_inputs
from studio.services.execution_prompts import normalize_compiled_profile_channels
from studio.services.prompt_profile_compiler import PromptProfileCompilationFacts, compile_prompt_profile_facts
from studio.services.prompt_profiles import PromptProfileError, publish_prompt_profile, refresh_prompt_profile_contract
from studio.tests import test_execution_definitions as definition_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionProfileInputTests(TestCase):
    write_contract = staticmethod(definition_fixtures.ExecutionDefinitionTests.write_contract)
    setUpTestData = classmethod(definition_fixtures.ExecutionDefinitionTests.setUpTestData.__func__)
    setUp = definition_fixtures.ExecutionDefinitionTests.setUp

    def profile(self, *, value_type="text", default=None, options=None, selector="request.theme", publish=True):
        field = create_field_definition(key="source_theme", label="Theme", category="test", value_type=value_type,
                                                      value_options=options or [])
        profile = PromptProfileVersion.objects.create(profile_key="source-profile", version=1, name="Source profile", model_family="async_cloud",
                                                      operation="video.generate", mode="T2VA", review_state="ready", contract_schema_version=8)
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector=selector,
            fixed_value=default if selector == "fixed.value" else None,
            has_default=default is not None and selector != "fixed.value", default_value=default if selector != "fixed.value" else None,
            generation_source_selector=selector, channel="positive_prompt", position=1, required=True, max_chars=4000)
        refresh_prompt_profile_contract(profile)
        return publish_prompt_profile(profile, actor=self.actor) if publish else profile

    def logical_input(self, key="request.theme", *, target="request.theme", kind="text", required=True):
        return {"key": key, "position": 0, "kind": kind, "required": required, "min_items": int(required), "max_items": 1,
                "semantic_contract": {"schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": [],
                    "allowed_scope_kinds": ["request"], "paired_with_input_key": None, "allow_repeated_asset": False},
                "targets": [{"target_kind": "profile_input", "target_key": target, "is_primary": False}]}

    def configure(self, profile, inputs):
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision,
            changes={"prompt_mode": "profile", "prompt_profile_version": profile, "inputs": inputs}, actor=self.actor)

    def test_complete_profile_definition_publishes_without_binding_or_smoke(self):
        profile = self.profile()
        self.configure(profile, [self.logical_input()])
        definition = build_execution_definition(self.version.pk)
        self.assertEqual(definition.value["resources"]["execution"]["operation"], "video.text_to_video")
        self.assertEqual(definition.value["interface"]["prompt"]["prompt_operation"], "video.generate")
        target = definition.value["interface"]["profile_inputs"]["request.theme"]
        self.assertTrue(target["required"])
        self.assertEqual(target["kind"], "text")
        published = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        self.assertEqual(verify_current_execution_definition(published.contract).sha256, definition.sha256)

    def test_required_request_source_is_derived_without_repeated_wiring(self):
        self.configure(self.profile(), [])
        definition = build_execution_definition(self.version.pk)
        self.assertEqual(definition.value["configuration"]["inputs"], [self.logical_input()])

    def test_appearance_difference_reuses_exact_profile_projection_without_copying_text(self):
        from studio.services.execution_profile_inputs import project_execution_profile_sources
        from studio.services.media_generation_appearances import media_appearance_difference
        from studio.services.json_contracts import json_contract
        self.configure(self.profile(), [])
        definition = build_execution_definition(self.version.pk)
        values = {"request.theme": "Change the outer coat only."}
        prompt = definition.value["interface"]["prompt"]
        facts = {"inputs": {"logical_values": values}, "profile_sources": {
            "profile": {"version_id": prompt["profile_id"], "contract_sha256": prompt["profile_sha256"]},
            "projection": project_execution_profile_sources(definition, values)}}
        source = {"kind": "logical_input", "key": "request.theme"}
        result = media_appearance_difference(source, definition=definition, resolved_inputs=facts, request={})
        self.assertEqual(result, {**source, "value_sha256": json_contract(values["request.theme"]).sha256})
        self.assertNotIn(values["request.theme"], str(result))
        # This verifies the real Profile mapping only, not image-reference
        # execution: the ordinary direct Profile media restriction is retained.
        for change in ({"projection": {}}, {"profile": {"version_id": "other", "contract_sha256": prompt["profile_sha256"]}}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                media_appearance_difference(source, definition=definition,
                    resolved_inputs={**facts, "profile_sources": {**facts["profile_sources"], **change}}, request={})
        for value in ("", "   ", {"body": "No implicit stringification"}):
            changed_values = {"request.theme": value}
            with self.subTest(value=value), self.assertRaises(ValidationError):
                projection = project_execution_profile_sources(definition, changed_values)
                media_appearance_difference(source, definition=definition, resolved_inputs={
                    "inputs": {"logical_values": changed_values},
                    "profile_sources": {**facts["profile_sources"], "projection": projection}}, request={})
        with self.assertRaises(ValidationError):
            media_appearance_difference({**source, "key": "not_mapped"}, definition=definition, resolved_inputs=facts, request={})

    def test_derived_inputs_cannot_gain_a_second_structure_even_during_clone(self):
        self.configure(self.profile(), [])
        row = self.logical_input()
        row["data_structure_version_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        for cloning in (False, True):
            with self.subTest(cloning=cloning), self.assertRaisesMessage(ValidationError, "策略派生输入的结构由策略管理"):
                reconcile_profile_inputs(self.version, [row], cloning=cloning)
        row["data_structure_version_id"] = None
        self.assertEqual(reconcile_profile_inputs(self.version, [row]), [self.logical_input()])

    def test_project_source_derives_port_and_frozen_reader_without_reading_business_values(self):
        self.configure(self.profile(selector="project.logline"), [])
        definition = build_execution_definition(self.version.pk).value
        target = definition["interface"]["profile_inputs"]["project.logline"]
        self.assertEqual(target["source_reader"]["protocol"], "project_snapshot/v1")
        self.assertEqual(target["source_kinds"], ["project_snapshot"])
        spec = definition["configuration"]["inputs"][0]
        self.assertEqual(spec["key"], "project.logline")
        self.assertEqual(spec["semantic_contract"]["allowed_scope_kinds"], ["project"])

    def test_create_draft_derives_once_from_exact_strategy(self):
        profile = self.profile()
        self.instance.refresh_from_db()
        version = create_execution_draft(instance_id=self.instance.pk, expected_revision=self.instance.revision,
            actor=self.actor, selection={"deployment": self.deployment, "operation": "video.text_to_video", "mode": "T2VA",
                "prompt_mode": "profile", "prompt_profile_version": profile, "workflow_version": None})
        self.assertEqual(execution_version_configuration(version)["inputs"], [self.logical_input()])
        self.assertEqual(execution_authoring_options(version.pk)["derived_input_keys"], ["request.theme"])
        self.version = version
        self.configure(profile, [])
        self.assertEqual(execution_version_configuration(self.version)["inputs"], [self.logical_input()])

    def test_derived_contract_cannot_be_renamed_weakened_or_given_project_scope(self):
        self.configure(self.profile(), [])
        before = execution_version_configuration(self.version)
        revisions = self.version.revision
        changes = [{"key": "alias"}, {"kind": "json"}, {"required": False}, {"required": 1},
            {"position": False}, {"min_items": 0}, {"max_items": 2},
            {"semantic_contract": {**self.logical_input()["semantic_contract"], "allowed_scope_kinds": ["project"]}},
            {"targets": [{"target_kind": "profile_input", "target_key": ["request.theme"], "is_primary": False}]}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                update_execution_draft(version_id=self.version.pk, expected_revision=revisions,
                    changes={"inputs": [{**self.logical_input(), **change}]}, actor=self.actor)
            self.version.refresh_from_db()
            self.assertEqual(self.version.revision, revisions)
            self.assertEqual(execution_version_configuration(self.version), before)

    def test_physical_mapping_is_explicit_not_a_second_generated_port(self):
        self.configure(self.profile(), [])
        physical = self.logical_input("api_story")
        physical["targets"].append({"target_kind": "api_input", "target_key": "provider_story", "is_primary": True})
        # Reconciliation is not physical adapter validation or authorization.
        result = reconcile_profile_inputs(self.version, [self.logical_input(), physical])
        self.assertEqual(result, [physical])
        self.assertEqual(result[0]["semantic_contract"]["allowed_scope_kinds"], ["request"])

    def test_successor_rederives_request_alias_without_rewriting_published_snapshot(self):
        self.configure(self.profile(), [])
        # Model a previously published authoring fact; do not add alias compatibility to new writes.
        self.version.inputs.update(key="old_theme")
        definition = build_execution_definition(self.version.pk)
        published = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        before = deepcopy(definition.value)
        successor = clone_execution_draft(source_id=published.pk, expected_revision=published.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        self.assertEqual(execution_version_configuration(successor)["inputs"], [self.logical_input()])
        self.assertEqual(verify_current_execution_definition(published.contract).value, before)
        with self.assertRaises(ValidationError):
            update_execution_draft(version_id=published.pk, expected_revision=published.revision,
                changes={"inputs": []}, actor=self.actor)

    def test_image_profile_uses_declared_inputs_without_the_removed_bible_normalization_route(self):
        selectors = [PromptProfileFieldRule.SourceSelector.PROJECT_TITLE, PromptProfileFieldRule.SourceSelector.PROJECT_LOGLINE,
            PromptProfileFieldRule.SourceSelector.PROJECT_SCRIPT, PromptProfileFieldRule.SourceSelector.STYLE_NAME,
            PromptProfileFieldRule.SourceSelector.STYLE_DESCRIPTION]
        profile = PromptProfileVersion.objects.create(profile_key="explicit-image-sources", version=1, name="Explicit image sources",
            model_family="async_cloud", operation="image.generate", mode="", review_state="ready", contract_schema_version=8)
        for position, selector in enumerate(selectors, start=1):
            field = create_field_definition(key=f"explicit_image_source_{position}", label=f"Source {position}", value_type="text")
            create_profile_field_rule(profile_version=profile, field_definition=field, source_selector=selector,
                generation_source_selector=selector, generation_mode="passthrough", channel="positive_prompt",
                position=position, required=True, max_chars=4000)
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        targets = profile_input_targets(profile)
        self.assertEqual(set(targets), set(selectors))
        self.assertTrue(all(row["required"] and row["kind"] == "text" for row in targets.values()))
        sources = {selector: f"Explicit reviewed source {position}" for position, selector in enumerate(selectors, start=1)}
        def compile_sources(values):
            return compile_prompt_profile_facts(profile, facts=PromptProfileCompilationFacts(sources=values,
                source_language="English", target_language="English", reference_context=None,
                reviewed_values={}, generation_contributions={}))
        compiled = compile_sources(sources)
        for text in sources.values():
            self.assertIn(text, compiled.assembly["channels"]["positive_prompt"])
        with self.assertRaises(PromptProfileError):
            compile_sources({key: value for key, value in sources.items() if key != selectors[0]})

    def test_foreign_selector_wrong_type_optional_and_duplicate_source_rejected(self):
        profile = self.profile()
        cases = ([self.logical_input(target="request.text")], [self.logical_input(kind="json")],
                 [self.logical_input(required=False)], [self.logical_input(), {**self.logical_input("another"), "position": 1}])
        for inputs in cases:
            with self.subTest(inputs=inputs):
                with self.assertRaises(ValidationError):
                    self.configure(profile, inputs)
                    build_execution_definition(self.version.pk)

    def test_explicit_profile_default_stays_per_field_not_instance_default(self):
        profile = self.profile(default="Explicit authored theme")
        self.configure(profile, [])
        definition = build_execution_definition(self.version.pk)
        source = definition.value["interface"]["profile_inputs"]["request.theme"]
        self.assertFalse(source["required"])
        self.assertEqual(source["fields"][0]["default"], "Explicit authored theme")
        self.assertNotIn("default", source)

    def test_definition_default_is_not_a_runtime_source(self):
        profile = self.profile(selector="fixed.value", default="Declared instruction")
        self.assertEqual(profile_input_targets(profile), {})
        self.configure(profile, [])
        self.assertEqual(build_execution_definition(self.version.pk).value["interface"]["profile_inputs"], {})

    def test_json_and_enum_types_are_not_silently_flattened(self):
        profile = self.profile(value_type="boolean")
        target = profile_input_targets(profile)["request.theme"]
        self.assertEqual((target["kind"], target["value_domain"]), ("json", "boolean"))
        self.configure(profile, [self.logical_input(kind="json")])
        build_execution_definition(self.version.pk)

    def test_manual_configuration_cannot_keep_hidden_profile_mappings(self):
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision,
            changes={"inputs": [self.logical_input()]}, actor=self.actor)
        with self.assertRaisesMessage(ValidationError, "策略没有声明"):
            build_execution_definition(self.version.pk)

    def test_conflicting_field_types_on_same_source_fail_at_definition_time(self):
        profile = self.profile(publish=False)
        field = create_field_definition(key="second_type", label="Other", value_type="boolean")
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector="request.theme",
            generation_source_selector="request.theme", channel="positive_prompt", position=2)
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        with self.assertRaisesMessage(ValidationError, "冲突的类型"):
            profile_input_targets(profile)

    def test_compiled_channels_use_exact_profile_and_recheck_target_limits(self):
        profile = self.profile()
        self.configure(profile, [self.logical_input()])
        configuration = build_execution_definition(self.version.pk).value["interface"]["prompt"]
        compilation = compile_prompt_profile_facts(profile, facts=PromptProfileCompilationFacts(
            sources={"request.theme": "Actual facts"}, source_language="English", target_language="English",
            reference_context=None, reviewed_values={}, generation_contributions={},
        ))
        result = normalize_compiled_profile_channels(configuration, compilation, declared_reference_labels=[])
        self.assertIn("Actual facts", result["values"]["positive_prompt"])
        with self.assertRaisesMessage(ValidationError, "精确策略"):
            normalize_compiled_profile_channels({**configuration, "profile_sha256": "0" * 64}, compilation, declared_reference_labels=[])
        with self.assertRaisesMessage(ValidationError, "正式编译结果"):
            normalize_compiled_profile_channels(configuration, compilation.assembly, declared_reference_labels=[])
        compilation.assembly["channels"]["unregistered"] = "Do not silently discard"
        with self.assertRaisesMessage(ValidationError, "非空通道"):
            normalize_compiled_profile_channels(configuration, compilation, declared_reference_labels=[])
