"""Original model queue and approvals feed precise downstream preparation."""
from copy import deepcopy
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import ApprovalDecision, ArtifactSelectionDecision, ExecutionPreparation, GeneratedArtifact
from studio.services.execution_selected_text import resolve_selected_execution_text
from studio.services.generated_artifacts import select_artifact
from studio.services.production_recipe_preparations import prepare_recipe_project_stage
from studio.tests import test_production_text_review as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class SelectedTextTests(TestCase):
    setUpTestData = classmethod(fixtures.TextCandidateReviewTests.setUpTestData.__func__)
    setUp = fixtures.TextCandidateReviewTests.setUp
    profile_instance = fixtures.TextCandidateReviewTests.profile_instance
    root = fixtures.TextCandidateReviewTests.root
    prepare = fixtures.TextCandidateReviewTests.prepare
    pass_smoke = fixtures.TextCandidateReviewTests.pass_smoke
    advance = fixtures.TextCandidateReviewTests.advance
    response = fixtures.TextCandidateReviewTests.response
    process = fixtures.TextCandidateReviewTests.process
    visual_root = fixtures.TextCandidateReviewTests.visual_root
    candidates = fixtures.TextCandidateReviewTests.candidates
    approve = fixtures.TextCandidateReviewTests.approve

    def read(self, source=None, **changes):
        return resolve_selected_execution_text(source or self.source,
            **{"spec": {"key": "script", "kind": "text", "semantic_contract": {"allowed_scope_kinds": ["project"]}},
                "owner": self.actor, "project_id": self.project.pk, "production_run_id": str(self.run.pk),
                "eligible_stage_ids": [self.stage.pk], **changes})

    def test_exact_adoption_not_candidate_literal_or_latest_selection(self):
        self.candidates()
        self.run = self.approve()
        decision = ArtifactSelectionDecision.objects.get(artifact=self.artifact)
        self.source = {"kind": "selected_output", "decision_id": str(decision.pk), "artifact_id": str(self.artifact.pk),
            "output_key": "generated_script.script.content", "scope": {"kind": "project", "id": str(self.project.pk)}}
        value, origin = self.read()
        # M2 stores the original structured text on its exact ScriptRevision;
        # the Artifact result contains a revision pointer, not a duplicate body.
        self.assertEqual(value, self.response()["text"])
        self.assertEqual(str(self.run.script_revision_id), self.artifact.manifest_metadata["script_revision_id"])
        self.assertEqual(value, self.run.script_revision.content)
        self.assertEqual(origin["decision_id"], str(decision.pk))
        self.assertEqual(origin["approval_id"], str(ApprovalDecision.objects.get(stage_run=self.review).pk))
        self.assertEqual(origin["context"]["content_revision_id"], str(self.run.script_revision_id))
        style, style_origin = self.read({**self.source, "output_key": "generated_script.script.project_style"},
            spec={"key": "style", "kind": "json", "semantic_contract": {"allowed_scope_kinds": ["project"]}})
        self.assertIsNone(style)
        self.assertEqual(style_origin["approval_id"], origin["approval_id"])
        self.assertEqual(style_origin["context"]["selector_key"], "script.project_style")
        from studio.services.execution_native_text import native_text_source_reference
        reference = native_text_source_reference({"configuration": {"operation": "script.structure", "inputs": [{
            "key": "script", "targets": [{"target_kind": "operation_input", "target_key": "source_text"}]}]}},
            {"input_provenance": [{"input_key": "script", **origin}]})
        self.assertEqual(reference["id"], str(self.run.script_revision_id))
        for changes in ({"output_key": "unknown"}, {"value": value}, {"scope": {"kind": "project", "id": str(uuid4())}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.read({**self.source, **changes})
        with self.assertRaises(ValidationError):
            self.read(eligible_stage_ids=[])
        # A later low-level selection cannot retroactively become the approved script.
        other = GeneratedArtifact.objects.filter(stage_run=self.stage).exclude(pk=self.artifact.pk).get()
        select_artifact(other, user=self.actor)
        with self.assertRaises(ValidationError):
            self.read()
        other_decision = ArtifactSelectionDecision.objects.get(artifact=other)
        with self.assertRaisesMessage(ValidationError, "审核采用记录"):
            self.read({**self.source, "artifact_id": str(other.pk), "decision_id": str(other_decision.pk)})

    def test_recipe_can_explicitly_configure_adopted_text_as_profile_input_and_replay_frozen_facts(self):
        self._assert_adopted_text_preparation()

    def test_unstarted_visual_amendment_reuses_successful_script_and_exact_selection(self):
        self._assert_adopted_text_preparation(amend=True)

    def _assert_adopted_text_preparation(self, amend=False):
        from studio.services.execution_definitions import build_execution_definition
        from studio.services.execution_instances import clone_execution_draft, update_execution_draft, publish_execution_version
        from studio.services.production_recipe_contracts import validate_recipe_input_sources
        from studio.services.prompt_profiles import clone_prompt_profile_successor, refresh_prompt_profile_contract, publish_prompt_profile

        script_state = self.version, self.definition, deepcopy(self.fixture)
        self.visual_root()
        declaration = deepcopy(self.plan.value["recipe"]["definition"]["stages"][0])
        selected_source = {
            "kind": "selected_output", "stage_key": "script_generate", "output_key": "generated_script.script.content",
            "scope": {"kind": "project"}, "selection_policy": "reviewed_selected"}
        style_source = {"kind": "literal", "value": "Restrained cool realism."}
        # Request-only ports cannot silently read project outputs. This test
        # intentionally opts into an adopted script, not the default logline.
        with self.assertRaisesMessage(ValidationError, "实例输入没有开放该上下文范围"):
            validate_recipe_input_sources(self.definition, {"request.theme": selected_source, "request.style": style_source},
                prior_stages=["script_generate", "script_review"])
        original_profile = self.version.prompt_profile_version
        profile = clone_prompt_profile_successor(original_profile,
            expected_source_contract_sha256=original_profile.contract_sha256, name="Explicit adopted script input")
        rule = profile.field_rules.get(source_selector="request.theme")
        rule.source_selector = rule.generation_source_selector = "project.master_script"
        rule.save(update_fields=("source_selector", "generation_source_selector"))
        profile.review_state = "ready"
        profile.save(update_fields=("review_state",))
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        version = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"prompt_profile_version": profile, "inputs": [{"key": "adopted_script", "position": 0,
                "kind": "text", "required": True, "min_items": 1, "max_items": 1,
                "semantic_contract": {"schema_version": 1, "allowed_scope_kinds": ["project"], "allowed_subject_kinds": [],
                    "allowed_purposes": [], "paired_with_input_key": None, "allow_repeated_asset": False},
                "targets": [{"target_kind": "profile_input", "target_key": "project.master_script", "is_primary": False}]}]})
        definition = build_execution_definition(version.pk)
        version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        route = declaration["routing"]["routes"][0]
        route.update(execution_instance_version_id=str(version.pk), expected_definition_sha256=definition.sha256,
            input_sources={"adopted_script": selected_source, "request.style": style_source})
        if amend:
            route["input_sources"]["adopted_script"] = {"kind": "context_field", "selector_key": "script.content", "scope": {"kind": "project"}}
        self.version, self.definition, self.fixture = script_state
        self.candidates(following=[declaration])
        downstream = self.run.stages.get(stage_key="visual_rules_generate")
        self.run = self.approve()
        if amend:
            from studio.models import GenerationAttempt
            from studio.services.production_stage_input_amendments import amend_stage_inputs, TEXT_SOURCE_POLICY
            from studio.services.production_runs import retry_production_run
            from studio.services.production_run_plans import get_run_execution_plan
            self.assertEqual(self.run.status, "blocked", self.run.blocked_reason)
            self.assertIn("未选择已确认剧本", self.run.blocked_reason)
            self.assertFalse(ExecutionPreparation.objects.filter(stage_run=downstream).exists())
            downstream.refresh_from_db()
            base = deepcopy((downstream.config_snapshot, downstream.input_snapshot))
            successful = list(GenerationAttempt.objects.values_list("pk", "status"))
            approved = list(ApprovalDecision.objects.values())
            args = {"stage_run_id": downstream.pk, "expected_run_plan_sha256": self.plan.sha256,
                "policy": TEXT_SOURCE_POLICY, "source_changes": [{"route_index": 0, "input_key": "adopted_script", "source": selected_source}], "actor": self.actor}
            proposal = amend_stage_inputs(**args, action="preview", expected_amendment_sha256=None)
            applied = amend_stage_inputs(**args, action="apply", expected_amendment_sha256=proposal["sha256"])
            self.run = retry_production_run(self.run, user=self.actor, expected_stage_id=downstream.pk, expected_attempt_ids=[])
            downstream.refresh_from_db()
            self.assertEqual((downstream.config_snapshot, downstream.input_snapshot), base)
            self.assertEqual(list(GenerationAttempt.objects.values_list("pk", "status")), successful)
            self.assertEqual(list(ApprovalDecision.objects.values()), approved)
            self.assertEqual(get_run_execution_plan(self.run), self.plan)
            for prep in downstream.execution_preparations.all():
                self.assertEqual(prep.contract.intent["request"]["requested"]["input_amendment"], {"id": applied["id"], "sha256": applied["sha256"]})
        preparations = list(ExecutionPreparation.objects.filter(stage_run=downstream).order_by("request_key"))
        self.assertEqual(len(preparations), 2, (self.run.blocked_code, self.run.blocked_reason))
        self.assertIsNone(self.plan.value["initial_content"])
        frozen = deepcopy(preparations[0].contract.resolved_inputs)
        facts = frozen["inputs"]
        self.assertEqual(facts["logical_values"]["adopted_script"], self.run.script_revision.content)
        proof = next(row for row in facts["input_provenance"] if row["input_key"] == "adopted_script")
        self.assertEqual(proof["kind"], "selected_output")
        self.assertEqual(proof["artifact_id"], str(self.artifact.pk))
        self.assertTrue(proof["approval_id"])
        from studio.services.execution_input_resolution import resolve_execution_input_sources
        from studio.services.json_contracts import json_contract
        from studio.services.production_recipe_preparations import _project_inputs
        style_spec = {"key": "style", "kind": "json", "required": True, "min_items": 1, "max_items": 1,
            "targets": [], "semantic_contract": {"schema_version": 1, "allowed_scope_kinds": ["project"],
                "allowed_subject_kinds": [], "allowed_purposes": [], "paired_with_input_key": None, "allow_repeated_asset": False}}
        definition = json_contract({"configuration": {"inputs": [style_spec]}, "resources": {"conditioning_mode": None},
            "interface": {"input_targets": {}}})
        rules = {"style": {"kind": "structured", "template": {"kind": "unit_map", "value": {
            "kind": "selected_output", "stage_key": "script_generate", "output_key": "generated_script.script.project_style",
            "scope": {"kind": "project"}, "selection_policy": "reviewed_selected"}}}}
        validate_recipe_input_sources(definition, rules, prior_stages=["script_generate", "script_review"])
        with self.assertRaises(ValidationError): validate_recipe_input_sources(definition, rules, prior_stages=[])
        bound, eligible = _project_inputs(rules, self.plan, downstream)
        self.assertEqual(eligible, {str(self.stage.pk)})
        def resolve_mapping():
            return resolve_execution_input_sources(definition, bound, owner=self.actor, subject_facts=[], max_media_bytes=65536,
                project_id=self.project.pk, production_run_id=str(self.run.pk), eligible_stage_ids=eligible,
                production_scope={"kind": "project", "id": str(self.project.pk)}, unit_key=f"project:{self.project.pk}").value
        mapping = resolve_mapping()
        self.assertEqual(mapping["logical_values"]["style"], {f"project:{self.project.pk}": None})
        leaf = mapping["input_provenance"][0]["leaves"][0]
        self.assertEqual(leaf["kind"], "selected_output")
        self.assertEqual(leaf["artifact_id"], str(self.artifact.pk))
        self.assertEqual(leaf["approval_id"], proof["approval_id"])
        self.project.continuity_summary = "Changed after preparation"
        self.project.save(update_fields=("continuity_summary",))
        self.artifact.status = "available"
        self.artifact.save(update_fields=("status",))
        replay = prepare_recipe_project_stage(stage_run_id=downstream.pk, expected_run_plan_sha256=self.plan.sha256,
            candidate_number=1, actor=self.actor)
        self.assertEqual(replay.pk, preparations[0].pk)
        self.assertEqual(replay.contract.resolved_inputs, frozen)
        self.assertFalse(downstream.generation_attempts.exists())
        with self.assertRaises(ValidationError): resolve_mapping()
