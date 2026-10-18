"""Actual candidate -> explicit approval -> exact downstream inputs; no external calls."""
from copy import deepcopy
from uuid import uuid4
import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from studio.models import ApprovalDecision, ExecutionPreparation, GenerationAttempt, SeriesProject
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_definitions import read_execution_input_contract
from studio.services.execution_profile_inputs import project_execution_profile_sources
from studio.services.execution_source_readers import VISUAL_RULE_SOURCE_FIELDS
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.production_recipe_contracts import validate_recipe_input_sources
from studio.services.production_recipe_preparations import _project_inputs
from studio.services.production_run_plans import get_run_execution_plan
from studio.tests import test_production_manual_visual_rules as fixtures
from studio.tests.visual_rules_fixtures import configure_visual_instance


class ReviewedSmokeSourceDetectionTests(SimpleTestCase):
    def test_only_explicit_source_nodes_require_context_not_literal_payloads(self):
        from studio.services.execution_smoke_sources import reviewed_sources_requested
        source = {"kind": "context_field", "selector_key": next(iter(VISUAL_RULE_SOURCE_FIELDS))}
        self.assertTrue(reviewed_sources_requested({"input": source}))
        self.assertTrue(reviewed_sources_requested({"input": {"kind": "structured", "template": {
            "kind": "object", "fields": {"values": {"kind": "array", "items": [source]}}}}}))
        for literal in ({"kind": "literal", "value": source}, {"kind": "structured", "template": {
                "kind": "literal", "value": {"nested": [source]}}}):
            self.assertFalse(reviewed_sources_requested({"input": literal}))
        self.assertFalse(reviewed_sources_requested({"input": {"kind": "context_field", "selector_key": []}}))
        for invalid in ([], {"input": {"kind": "structured", "template": {"kind": "array", "items": {}}}}):
            with self.assertRaises(ValidationError):
                reviewed_sources_requested(invalid)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ReviewedVisualSourcesTests(TestCase):
    setUpTestData = classmethod(fixtures.ManualVisualRulesTests.setUpTestData.__func__)
    setUp = fixtures.ManualVisualRulesTests.setUp
    post = fixtures.ManualVisualRulesTests.post
    preparation = fixtures.ManualVisualRulesTests.preparation
    make_candidate = fixtures.ManualVisualRulesTests.make_candidate
    options = fixtures.ManualVisualRulesTests.options
    approval = fixtures.ManualVisualRulesTests.approval

    def adopt(self):
        self.artifact = self.make_candidate()
        body = self.approval(self.artifact)
        body["payload"]["candidate_overrides"]["continuity_summary"] = "Human reviewed continuity, not raw candidate."
        response = self.post(body)
        self.assertEqual(response.status_code, 201, response.data)
        self.decision_id = response.data["target_id"]
        self.downstream = self.run.stages.get(stage_key="media_generation")
        configure_visual_instance(self, reviewed_sources=True)

    def inputs(self):
        return {key: {"kind": "context_field", "selector_key": key,
            "scope": {"kind": "project", "id": str(self.project.pk)}, "content_revision_id": self.decision_id}
            for key in (row["key"] for row in self.definition.value["configuration"]["inputs"])}

    def smoke_body(self):
        return {"expected_definition_sha256": self.definition.sha256,
            "fixture": {**deepcopy(self.fixture), "inputs": self.inputs()},
            "field_values": [], "allow_external_calls": False,
            "reviewed_source_context": {"run_id": str(self.run.pk), "stage_key": "media_generation",
                "run_plan_sha256": get_run_execution_plan(self.run).sha256}}

    def test_independent_smoke_freezes_reviewed_sources_without_production_side_effects(self):
        self.adopt()
        self.actor.is_staff = True
        self.actor.save(update_fields=["is_staff"])
        from studio.models import ModelDeploymentSmokeAttempt
        from studio.services.execution_preparations import read_execution_preparation_detail
        url = f"/api/v1/model-execution-instance-versions/{self.version.pk}/smoke-test/"
        body = self.smoke_body()
        before = (list(self.run.stages.values_list("id", "status", "output_snapshot_sha256")),
            ApprovalDecision.objects.count(), GenerationAttempt.objects.count())
        response = self.client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="reviewed-smoke")
        self.assertEqual(response.status_code, 202, response.data)
        attempt = ModelDeploymentSmokeAttempt.objects.get(pk=response.data["attempt_id"])
        prep = attempt.execution_preparations.get()
        self.assertIsNone(prep.stage_run_id)
        self.assertEqual(prep.purpose, "instance_smoke")
        frozen = read_execution_preparation_detail(prep).resolved_inputs
        self.assertNotIn("production_unit", frozen)
        self.assertNotIn("execution_deadlines", frozen)
        provenance = frozen["inputs"]["input_provenance"]
        self.assertEqual(provenance[0]["smoke_source_context"], body["reviewed_source_context"])
        self.assertEqual(provenance[0]["review"]["decision_id"], self.decision_id)
        self.assertEqual(before, (list(self.run.stages.values_list("id", "status", "output_snapshot_sha256")),
            ApprovalDecision.objects.count(), GenerationAttempt.objects.count()))
        SeriesProject.objects.filter(pk=self.project.pk).update(continuity_summary="Changed after freeze")
        replay = self.client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="reviewed-smoke")
        self.assertEqual((replay.status_code, replay.data["attempt_id"]), (200, response.data["attempt_id"]))
        # Let a new request reach source validation instead of active-Smoke admission.
        ModelDeploymentSmokeAttempt.objects.filter(pk=attempt.pk).update(status="cancelled")
        rejected = self.client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="stale-smoke")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.filter(scope="instance",execution_instance_version=self.version).count(), 1)

    def test_smoke_context_directory_permissions_and_closed_request(self):
        self.adopt()
        self.actor.is_staff = True
        self.actor.save(update_fields=["is_staff"])
        from studio.models import ModelDeploymentSmokeAttempt
        from django.contrib.auth import get_user_model
        base = f"/api/v1/model-execution-instance-versions/{self.version.pk}/"
        page = self.client.get(base+"smoke-source-contexts/", {"page_size": 2})
        self.assertEqual(page.status_code, 200, page.data)
        self.assertIn("no-store", page["Cache-Control"])
        self.assertEqual(len(page.data["results"]), 2)
        self.assertIsNotNone(page.data["next"])
        self.assertEqual(str(page.data["results"][0]["run_id"]), str(self.run.pk))
        self.assertNotIn("output_snapshot", page.data["results"][0])
        for query in ("?unknown=1", "?page=1&page=2", "?page_size=201"):
            self.assertEqual(self.client.get(base+"smoke-source-contexts/"+query).status_code, 400)
        body = self.smoke_body()
        attempts = ModelDeploymentSmokeAttempt.objects.count()
        variants = [None, {**body["reviewed_source_context"], "run_id": str(uuid4())},
            {**body["reviewed_source_context"], "stage_key": "visual_rules_generate"},
            {**body["reviewed_source_context"], "run_plan_sha256": "0"*64},
            {**body["reviewed_source_context"], "eligible_stage_ids": [str(self.stage.pk)]}]
        for context in variants:
            request = deepcopy(body)
            if context is None:
                request.pop("reviewed_source_context")
            else:
                request["reviewed_source_context"] = context
            response = self.client.post(base+"smoke-test/", request, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4()))
            self.assertIn(response.status_code, (400, 409), response.data)
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), attempts)
        other = get_user_model().objects.create_user(username="smoke-foreign-admin", is_staff=True)
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(base+"smoke-source-contexts/").data["count"], 0)
        self.assertEqual(self.client.post(base+"smoke-test/", body, format="json", HTTP_IDEMPOTENCY_KEY="foreign").status_code, 400)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(base+"smoke-source-contexts/").status_code, (401,403))

    def resolve(self, sources=None, **kwargs):
        return resolve_execution_input_sources(self.definition, self.inputs() if sources is None else sources,
            owner=kwargs.get("owner", self.actor), project_id=self.project.pk,
            production_run_id=kwargs.get("run_id", str(self.run.pk)),
            eligible_stage_ids=kwargs.get("eligible", execution_source_stage_ids(self.downstream)),
            subject_facts=[], max_media_bytes=65536).value

    def test_derivation_approval_preview_projection_and_no_whole_script(self):
        self.adopt()
        sources = self.inputs()
        contract = read_execution_input_contract(self.version.pk)
        self.assertEqual({row["key"] for row in contract["definition"]["configuration"]["inputs"]}, set(sources))
        for target in contract["definition"]["interface"]["profile_inputs"].values():
            self.assertEqual(target["source_kinds"], ["context_field"])
            self.assertEqual(target["source_reader"]["output_schema_version"], 3)
        before = (ExecutionPreparation.objects.count(), GenerationAttempt.objects.count(), ApprovalDecision.objects.count())
        url = f"/api/v1/production-runs/{self.run.pk}/reviewed-visual-sources/?stage_key=media_generation"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(len(response.data["sources"]), 3)
        selected = next(row for row in response.data["sources"] if row["selector_key"].endswith("continuity_summary"))
        self.assertEqual(selected["content"], "Human reviewed continuity, not raw candidate.")
        result = self.resolve()
        projected = project_execution_profile_sources(self.definition, result["logical_values"])
        self.assertEqual(projected["assembly_sources"][selected["selector_key"]], selected["content"])
        provenance = next(row for row in result["input_provenance"] if row["selector_key"] == selected["selector_key"])
        self.assertEqual(provenance["review"]["decision_id"], self.decision_id)
        self.assertEqual(provenance["output"]["artifact_id"], str(self.artifact.pk))
        self.assertEqual(provenance["output"]["attempt_id"], str(self.artifact.generation_attempt_id))
        self.assertNotIn("master_script", str(projected))
        self.assertEqual(before, (ExecutionPreparation.objects.count(), GenerationAttempt.objects.count(), ApprovalDecision.objects.count()))

    def test_direct_color_worker_candidate_review_and_downstream_use_same_frozen_output(self):
        from studio.services.visual_rules_output_contract import normalize_visual_rule_field_sources
        sources = {**normalize_visual_rule_field_sources(), 'color_script': 'color_script'}
        self.assert_mapped_output_reaches_review_and_downstream(sources)

    def test_alternate_continuity_and_negative_formats_reach_worker_review_and_downstream(self):
        self.assert_mapped_output_reaches_review_and_downstream({
            'continuity_summary': 'continuity_points', 'color_script': 'color_script', 'negative_prompt': 'negative_prompt'})

    def test_authored_response_names_reach_original_worker_review_and_downstream(self):
        self.assert_mapped_output_reaches_review_and_downstream({
            'continuity_summary': 'continuity_points', 'color_script': 'color_script', 'negative_prompt': 'negative_prompt'},
            {'continuity_summary': 'rules', 'color_script': 'palette', 'negative_prompt': 'avoid'})

    def test_camera_guidance_reaches_review_and_exact_downstream_without_project_write(self):
        self.assert_mapped_output_reaches_review_and_downstream({
            'continuity_summary': 'continuity_points', 'color_script': 'color_script',
            'negative_prompt': 'negative_prompt', 'camera_guidance': 'camera_guidance'},
            {'continuity_summary': 'rules.0', 'color_script': 'rules.1', 'negative_prompt': 'rules.2', 'camera_guidance': 'rules.3'})
        self.project.refresh_from_db()
        self.assertNotIn('camera_guidance', self.project.production_settings)
        self.assertEqual(len(self.client.get(f'/api/v1/production-runs/{self.run.pk}/reviewed-visual-sources/?stage_key=media_generation').data['sources']), 4)

    def test_camera_source_cannot_be_fabricated_from_three_field_review(self):
        self.adopt()
        configure_visual_instance(self, reviewed_sources=True, reviewed_fields=['camera_guidance'])
        with self.assertRaises(ValidationError):
            self.resolve()

    def assert_mapped_output_reaches_review_and_downstream(self, sources, response_keys=None):
        from studio.services.visual_rules_output_contract import unpack_visual_rule_response, pack_visual_rule_response
        configure_visual_instance(self, field_sources=sources, response_keys=response_keys)
        keys = response_keys or sources
        self.body['payload'] = {'execution_instance_version_id': str(self.version.pk),
            'expected_definition_sha256': self.definition.sha256, **deepcopy(self.fixture)}
        frozen = self.definition.value['interface']['business_output']
        sample = deepcopy(frozen['parser']['response_example'])
        values = unpack_visual_rule_response(sample, keys)
        values['color_script'] = 'Direct color description preserved by the original worker and review.'
        sample = pack_visual_rule_response(values, keys)
        # Only the external provider is replaced, in an isolated test database.
        with patch('studio.tests.test_execution_production_dispatch.ProductionDispatchTests.response',
                return_value={'text': json.dumps(sample), 'request_id': 'isolated-direct-color'}):
            self.artifact = self.make_candidate()
        actual = self.artifact.manifest_metadata['result']['result']
        self.assertEqual(actual['candidate']['color_script'], values['color_script'])
        if sources['continuity_summary'] == 'continuity_points':
            self.assertEqual(actual['candidate']['continuity_summary'], ' '.join(values['continuity_summary']))
            self.assertEqual(actual['candidate']['negative_prompt'], values['negative_prompt'])
        self.assertNotIn('color_design', actual)
        self.assertEqual(actual['business_output'], frozen)
        detail = self.client.get(f'/api/v1/generated-artifacts/{self.artifact.pk}/')
        self.assertEqual(detail.status_code, 200, detail.data)
        contract = detail.data['text_candidate']['review_contract']
        self.assertEqual(contract['output']['sha256'], frozen['sha256'])
        self.assertEqual({row['key']: row['response_path'] for row in contract['fields']}, keys)
        url = f'/api/v1/production-runs/{self.run.pk}/reviewed-visual-sources/?stage_key=media_generation'
        self.assertEqual(self.client.get(url).status_code, 400, 'candidate is not an approved source')
        approved = self.post(self.approval(self.artifact))
        self.assertEqual(approved.status_code, 201, approved.data)
        self.decision_id = approved.data['target_id']
        self.downstream = self.run.stages.get(stage_key='media_generation')
        configure_visual_instance(self, reviewed_sources=True, reviewed_fields=list(actual['candidate']))
        resolved = self.resolve()
        for field, content in actual['candidate'].items():
            key = 'reviewed_visual_rules.' + field
            self.assertEqual(resolved['logical_values'][key], content)
            provenance = next(row for row in resolved['input_provenance'] if row['selector_key']==key)
            self.assertEqual(provenance['review']['decision_id'], self.decision_id)
            self.assertEqual(provenance['output']['attempt_id'], str(self.artifact.generation_attempt_id))
            self.assertEqual(provenance['output']['business_output'], contract['output'])
        preview = self.client.get(url)
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data['review_contract'], contract)
        self.assertNotEqual(contract['output']['id'], str(self.version.output_version_id), 'must not substitute the downstream instance output')

    def test_rehashed_result_cannot_replace_original_output_contract_or_quality(self):
        from studio.services.json_contracts import assign_json_contract, json_contract
        self.artifact = self.make_candidate()
        original = deepcopy(self.artifact.manifest_metadata)
        replacement = deepcopy(original['result']['result']['business_output'])
        replacement['id'] = str(uuid4())
        replacement['sha256'] = json_contract({key: value for key, value in replacement.items() if key != 'sha256'}).sha256
        for changes in ({'business_output': replacement}, {'business_output': None},
                {'quality_advisories': {'schema_version': 1, 'continuity_summary_max_chars': 1}}):
            with self.subTest(changes=changes):
                metadata = deepcopy(original)
                metadata['result']['result'].update(changes)
                result = json_contract(metadata['result'])
                metadata['result_sha256'] = result.sha256
                assign_json_contract(self.artifact, 'manifest_metadata', metadata)
                self.artifact.sha256, self.artifact.size_bytes = result.sha256, result.size_bytes
                self.artifact.save()
                GenerationAttempt.objects.filter(pk=self.artifact.generation_attempt_id).update(result_sha256=result.sha256)
                self.assertEqual(self.client.get(f'/api/v1/generated-artifacts/{self.artifact.pk}/').status_code, 400)
                response = self.post(self.approval(self.artifact))
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(ApprovalDecision.objects.filter(stage_run__production_run=self.run).count(), 0)

    def test_rehashed_review_cannot_replace_original_candidate_or_input_evidence(self):
        from studio.services.json_contracts import assign_json_contract
        self.adopt()
        stage = self.run.stages.get(stage_key='visual_rules_review')
        original = deepcopy(stage.output_snapshot)
        for key in ('candidate_sha256', 'business_input_sha256'):
            value = {**original, key: '0' * 64}
            assign_json_contract(stage, 'output_snapshot', value, schema_version=3)
            stage.save()
            ApprovalDecision.objects.filter(pk=self.decision_id).update(output_snapshot_sha256=stage.output_snapshot_sha256)
            with self.assertRaisesMessage(ValidationError, '原产物不一致'):
                self.resolve()

    def test_array_output_reaches_original_worker_review_and_downstream(self):
        self.assert_mapped_output_reaches_review_and_downstream({
            'continuity_summary': 'continuity_points', 'color_script': 'color_script', 'negative_prompt': 'negative_prompt'},
            {'continuity_summary': 'rules.0.summary', 'color_script': 'rules.1.palette', 'negative_prompt': 'rules.2.avoid'})

    def test_nested_output_reaches_original_worker_review_and_downstream(self):
        self.assert_mapped_output_reaches_review_and_downstream({
            'continuity_summary': 'continuity_points', 'color_script': 'color_script', 'negative_prompt': 'negative_prompt'},
            {'continuity_summary': 'visual.rules.summary', 'color_script': 'visual.palette', 'negative_prompt': 'constraints.avoid'})

    def test_stale_review_literal_impostor_wrong_field_and_missing_run_are_rejected(self):
        self.adopt()
        original = self.resolve()
        key = next(iter(VISUAL_RULE_SOURCE_FIELDS))
        for replacement in ({"kind": "literal", "value": "fake approved output"},
                {**self.inputs()[key], "content_revision_id": str(uuid4())},
                {**self.inputs()[key], "selector_key": "script.content"}):
            with self.subTest(replacement=replacement), self.assertRaises(ValidationError):
                self.resolve({**self.inputs(), key: replacement})
        for kwargs in ({"run_id": None}, {"run_id": str(uuid4())}, {"eligible": []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.resolve(**kwargs)
        SeriesProject.objects.filter(pk=self.project.pk).update(continuity_summary="Unreviewed change")
        with self.assertRaisesMessage(ValidationError, "已变化"):
            self.resolve()
        self.assertEqual(original["logical_values"][key], "Human reviewed continuity, not raw candidate.")
        self.assertEqual(self.client.get(f"/api/v1/production-runs/{self.run.pk}/reviewed-visual-sources/?stage_key=media_generation").status_code, 400)

    def test_recipe_binding_uses_current_review_not_initial_script(self):
        self.adopt()
        rules = {key: {"kind": "context_field", "selector_key": key, "scope": {"kind": "project"}} for key in self.inputs()}
        with self.assertRaisesMessage(ValidationError, "更早"):
            validate_recipe_input_sources(self.definition, rules, prior_stages=[])
        validate_recipe_input_sources(self.definition, rules, prior_stages=["visual_rules_review"])
        plan = get_run_execution_plan(self.run)
        self.assertIsNone(plan.value["initial_content"])
        bound, eligible = _project_inputs(rules, plan, self.downstream)
        self.assertEqual(bound, self.inputs())
        self.assertEqual(self.resolve(bound, eligible=eligible)["logical_values"], self.resolve()["logical_values"])
        key = next(iter(rules))
        with self.assertRaises(ValidationError):
            validate_recipe_input_sources(self.definition, {**rules, key: {"kind": "literal", "value": "fake"}}, prior_stages=["visual_rules_review"])

    def test_preview_refuses_unreviewed_wrong_stage_foreign_owner_and_tampered_task(self):
        url = f"/api/v1/production-runs/{self.run.pk}/reviewed-visual-sources/"
        self.assertEqual(self.client.get(url, {"stage_key": "media_generation"}).status_code, 400)
        self.adopt()
        self.assertEqual(self.client.get(url, {"stage_key": "visual_rules_generate"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"stage_key": "not_a_stage"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"stage_key": "media_generation", "fake": 1}).status_code, 400)
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user(username="visual-foreign")
        with self.assertRaises(ValidationError):
            self.resolve(owner=other)
        SeriesProject.objects.filter(pk=self.project.pk).update(owner=other)
        with self.assertRaises(ValidationError):
            self.resolve()
        self.assertEqual(self.client.get(url, {"stage_key": "media_generation"}).status_code, 400)
        SeriesProject.objects.filter(pk=self.project.pk).update(owner=self.actor)
        GenerationAttempt.objects.filter(pk=self.artifact.generation_attempt_id).update(result_sha256="0" * 64)
        with self.assertRaises(ValidationError):
            self.resolve()
