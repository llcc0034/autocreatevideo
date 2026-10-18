"""Real source/fixture/Smoke endpoints; the external response alone is isolated."""
from copy import deepcopy
import json
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import AssetGenerationJob, ExecutionPreparation, ModelDeploymentSmokeAttempt, ModelDeploymentCapabilitySnapshot, PromptProfileVersion, PromptProfileFieldRule
from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, process_deployment_smoke
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import publish_execution_version
from studio.services.json_contracts import assign_json_contract
from studio.tests.test_execution_text_contracts import ExecutionTextContractTests


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionFieldSmokeTests(TestCase):
    setUpTestData = classmethod(ExecutionTextContractTests.setUpTestData.__func__)
    version = ExecutionTextContractTests.version

    def setUp(self):
        ExecutionTextContractTests.setUp(self)
        self.enterContext(patch("studio.services.provider_registry.OpenAICompatibleDriver.validate_execution_credentials"))
        provider = self.deployment.provider
        assign_json_contract(provider, "driver_config", {**provider.driver_config, "pricing": {"fixed_per_request": "0.1", "currency": "USD"}})
        provider.save()
        self.leaf = self.version("prompt.fields.generate")
        self.definition = build_execution_definition(self.leaf.pk)
        publish_execution_version(version_id=self.leaf.pk, expected_revision=self.leaf.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.base = f"/api/v1/model-execution-instance-versions/{self.leaf.pk}/"
        self.item = {"field_key": "protocol_direction", "source_text": "A person waves.", "source_language": "English",
            "target_language": "English", "context_facts": {}, "protected_tokens": []}
        self.body = {"profile_version_id": str(self.profile.pk), "expected_definition_sha256": self.definition.sha256,
            "expected_profile_contract_sha256": self.profile.contract_sha256, "items": [self.item]}

    def build(self):
        response = self.client.post(self.base+"field-smoke-source/", self.body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_large_field_source_roundtrips_through_public_preparation(self):
        from studio.services.execution_preparations import read_execution_preparation_detail
        self.item["source_text"] = "Source text remains complete. " * 3200
        source = self.build()
        self.assertEqual(source["source"]["value"]["schema_version"], 2)
        result = self.smoke(source, "large-runtime-source")
        self.assertEqual(result.status_code, 202, result.data)
        prep = ExecutionPreparation.objects.get(pk=result.data["preparation"]["id"])
        detail = read_execution_preparation_detail(prep)
        self.assertGreater(detail.intent_size_bytes, 65536)
        batch = detail.resolved_inputs["inputs"]["operation_inputs"]["field_request"]
        self.assertEqual(batch, source["source"]["value"])
        self.assertEqual(batch["items"][0]["source_text"], self.item["source_text"])
        self.assertIsNone(prep.request_frozen_at)

    def smoke(self, source, key="formal-leaf"):
        return self.client.post(self.base+"smoke-test/", {
            "expected_definition_sha256": self.definition.sha256,
            "fixture": {"inputs": {source["input_key"]: source["source"]}, "parameters": {},
                "manual_channels": {}, "subject_facts": [], "source_language": "", "target_language": ""},
            "field_values": [], "allow_external_calls": True,
        }, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def test_source_and_preview_have_no_tasks_or_parent_and_copy_published_rules(self):
        response = self.client.get(self.base+"field-smoke-source/", {"profile_version_id": str(self.profile.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["input_key"], "fields")
        self.assertEqual([rule["field_key"] for rule in response.data["rules"]], ["protocol_direction"])
        source = self.build()
        self.assertEqual(source["source"]["value"]["items"][0]["field_contract"], response.data["rules"][0])
        self.assertEqual(self.build(), source)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        self.assertFalse(AssetGenerationJob.objects.exists())

    def retired_source(self, *, final_source):
        from studio.services.prompt_profiles import refresh_prompt_profile_contract
        # Isolated pre-cutover rule snapshot, not an allowed current authoring write.
        PromptProfileFieldRule.objects.filter(profile_version=self.profile).update(
            source_selector="shot.action" if final_source else "request.text",
            generation_source_selector="request.action" if final_source else "shot.action")
        self.profile.status = "draft"
        refresh_prompt_profile_contract(self.profile)
        self.profile.status = "published"
        self.body["expected_profile_contract_sha256"] = self.profile.contract_sha256
        with patch("studio.services.execution_field_smoke._validate_current_field_rules"):
            return self.build()

    def test_retired_final_and_generation_sources_block_new_leaf_but_preserve_frozen_read(self):
        from studio.services.execution_field_smoke import verify_instance_field_fixture
        for final_source in (True, False):
            with self.subTest(final_source=final_source):
                source = self.retired_source(final_source=final_source)
                before = deepcopy(source)
                row = deepcopy(self.profile.field_rules.values().get())
                expected_code = ("prompt_profile_source_selector_unsupported" if final_source
                                 else "prompt_profile_generation_source_unknown")
                for response in (
                    self.client.get(self.base + "field-smoke-source/", {"profile_version_id": str(self.profile.pk)}),
                    self.client.post(self.base + "field-smoke-source/", self.body, format="json"),
                    self.smoke(source, "retired-source-" + str(final_source)),
                ):
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertIn(expected_code, str(response.data))
                frozen = verify_instance_field_fixture(self.definition,
                    {"operation_inputs": {"field_request": source["source"]["value"]}}, allow_retired=True)
                self.assertEqual(frozen.value, source["source"]["value"])
                self.assertEqual(source, before)
                self.assertEqual(self.profile.field_rules.values().get(), row)
                self.assertFalse(ExecutionPreparation.objects.exists())
                self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())

    def test_retired_leaf_reuse_cannot_allocate_new_smoke_but_same_key_replays(self):
        source = self.retired_source(final_source=False)
        # Reproduce a historical allocation only; never advance or invoke a driver.
        with patch("studio.services.execution_field_smoke._validate_current_field_rules"):
            created = self.smoke(source)
        self.assertEqual(created.status_code, 202, created.data)
        prep = ExecutionPreparation.objects.get(pk=created.data["preparation"]["id"])
        before = deepcopy(prep.contract.resolved_inputs)
        digest = prep.contract.resolved_inputs_sha256
        replay = self.smoke(source)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["preparation"]["id"], str(prep.pk))
        ModelDeploymentSmokeAttempt.objects.update(status="failed")  # Synthetic finished owner, no remote request.
        from studio.services.execution_smoke import read_instance_smoke_reuse_fixture
        fixture = read_instance_smoke_reuse_fixture(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor)
        fixture.pop("execution_instance_version_id")
        rejected = self.client.post(self.base + "smoke-test/", {
            **fixture, "field_values": [], "allow_external_calls": True,
        }, format="json", HTTP_IDEMPOTENCY_KEY="new-from-retired")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("prompt_profile_generation_source_unknown", str(rejected.data))
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), 1)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        prep.contract.refresh_from_db()
        self.assertEqual((prep.contract.resolved_inputs, prep.contract.resolved_inputs_sha256), (before, digest))
        self.assertFalse(AssetGenerationJob.objects.exists())

    def test_resource_leaf_reuses_exact_field_protocol_without_an_instance_preparation(self):
        from studio.services.execution_instances import ExecutionResourceSelection
        from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
        from studio.services.execution_smoke import create_resource_smoke
        from studio.services.execution_preparation_requests import compile_prepared_execution_transport
        from studio.services.execution_field_smoke import resource_field_smoke_source, build_resource_field_smoke_source
        selection = ExecutionResourceSelection.from_version(self.leaf)
        definition = build_resource_smoke_definition(selection)
        choices = resource_field_smoke_source(selection=selection, profile_version_id=self.profile.pk, actor=self.actor)
        source = build_resource_field_smoke_source(selection=selection, actor=self.actor,
            **{**self.body, "expected_definition_sha256": definition.sha256})
        self.assertIsNone(choices["execution_instance_version_id"])
        self.assertIsNone(source["execution_instance_version_id"])
        self.assertEqual(source["source"]["value"]["items"][0]["field_contract"], choices["rules"][0])
        self.assertFalse(ExecutionPreparation.objects.exists())
        input_key = source["input_key"]
        self.assertEqual(input_key, definition.value["configuration"]["inputs"][0]["key"])
        attempt, prep, _ = create_resource_smoke(selection=selection, expected_definition_sha256=definition.sha256,
            fixture={"inputs": {input_key: source["source"]}, "parameters": {},
                "manual_channels": {}, "subject_facts": [], "source_language": "", "target_language": ""},
            field_values=[], allow_external_calls=True, budget_limit=None, actor=self.actor, client_idempotency_key="resource-leaf")
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor).transport.value
        self.assertIsNone(prep.execution_instance_version_id)
        self.assertIsNone(transport["execution_instance_version_id"])
        self.assertEqual(attempt.scope, "resource")
        self.assertTrue(transport["field_request_sha256"])
        self.assertIsNone(attempt.prompt_profile_version_id)  # Fixture rule source is not a main strategy binding.
        self.assertFalse(AssetGenerationJob.objects.exists())

    def test_resource_field_source_rejects_identity_drift_custom_rules_and_non_admin(self):
        from django.core.exceptions import ValidationError, PermissionDenied
        from studio.services.execution_instances import ExecutionResourceSelection
        from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
        from studio.services.execution_field_smoke import resource_field_smoke_source, build_resource_field_smoke_source
        selection = ExecutionResourceSelection.from_version(self.leaf)
        definition = build_resource_smoke_definition(selection)
        args = {**self.body, "expected_definition_sha256": definition.sha256, "selection": selection, "actor": self.actor}
        for change in ({"expected_definition_sha256": self.definition.sha256},
                {"expected_profile_contract_sha256": "0" * 64},
                {"items": [{**self.item, "field_contract": {"generation_instruction": "Invented"}}]}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                build_resource_field_smoke_source(**{**args, **change})
        self.actor.is_staff = False
        self.actor.save(update_fields=["is_staff"])
        with self.assertRaises(PermissionDenied):
            resource_field_smoke_source(selection=selection, profile_version_id=self.profile.pk, actor=self.actor)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())

    def test_resource_field_source_api_reads_and_builds_exact_fixture_without_allocating(self):
        base = f"/api/v1/model-deployments/{self.deployment.pk}/"
        selection = {"operation": "prompt.fields.generate", "mode": "", "prompt_mode": "none", "prompt_profile_version_id": None}
        response = self.client.post(base + "smoke-contract/", selection, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        query = {"selection": selection, "profile_version_id": str(self.profile.pk)}
        choices = self.client.post(base + "smoke-field-source/", query, format="json")
        self.assertEqual(choices.status_code, 200, choices.data)
        self.assertIsNone(choices.data["execution_instance_version_id"])
        self.assertNotIn("source", choices.data)
        body = {**query, "expected_definition_sha256": response.data["sha256"],
            "expected_profile_contract_sha256": self.profile.contract_sha256, "items": [self.item]}
        built = self.client.post(base + "smoke-field-source/", body, format="json")
        self.assertEqual(built.status_code, 200, built.data)
        self.assertIsNone(built.data["execution_instance_version_id"])
        self.assertEqual(built.data["source"]["value"]["items"][0]["field_contract"], choices.data["rules"][0])
        self.assertEqual(self.client.post(base + "smoke-field-source/", {**query, "items": [self.item]}, format="json").status_code, 400)
        stale = self.client.post(base + "smoke-field-source/", {**body, "expected_definition_sha256": "0" * 64}, format="json")
        self.assertEqual(stale.status_code, 409, stale.data)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())

    def test_changed_identity_fabricated_rules_and_wrong_operation_are_rejected(self):
        for body in ({**self.body, "expected_definition_sha256": "0"*64},
                     {**self.body, "expected_profile_contract_sha256": "0"*64}):
            response = self.client.post(self.base+"field-smoke-source/", body, format="json")
            self.assertEqual(response.status_code, 409, response.data)
        bad = {**self.body, "items": [{**self.item, "field_contract": {"generation_instruction": "caller rule"}}]}
        self.assertEqual(self.client.post(self.base+"field-smoke-source/", bad, format="json").status_code, 400)
        for change in ("rule", "operation"):
            source = deepcopy(self.build())
            batch = source["source"]["value"]
            if change == "rule":
                batch["items"][0]["field_contract"]["generation_instruction"] = "Invented instruction"
            else:
                batch["operation"] = "prompt.fields.translate"
            rejected = self.smoke(source, change)
            self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        self.actor.is_staff = False
        self.actor.save(update_fields=["is_staff"])
        self.assertEqual(self.client.post(self.base+"field-smoke-source/", self.body, format="json").status_code, 403)

    def test_leaf_smoke_runs_original_queue_without_parent_smoke_and_keeps_exact_field_ids(self):
        source = self.build()
        response = self.smoke(source)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(self.smoke(source).status_code, 200)
        prep = ExecutionPreparation.objects.get(pk=response.data["preparation"]["id"])
        advanced = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        claim = claim_next_deployment_smoke("field-fixture-test")
        self.assertIsNotNone(claim)
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps({"items": [{"request_id": "smoke:protocol_direction", "text": "A person waves."}]}), "request_id": "isolated-leaf-call"}
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            result = process_deployment_smoke(claim)
        self.assertEqual(result.status, "succeeded", result.error_code)
        peer.invoke.assert_called_once()
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), 1)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())
        self.assertFalse(AssetGenerationJob.objects.exists())

    def test_translation_source_uses_only_translation_rules_and_requires_source_text(self):
        from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
        translator = self.version("prompt.fields.translate")
        definition = build_execution_definition(translator.pk)
        publish_execution_version(version_id=translator.pk, expected_revision=translator.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        profile = PromptProfileVersion.objects.create(profile_key="translation-fixture", version=1, name="Translation test",
            model_family="text-protocol", operation="video.generate", mode="", review_state="ready", contract_schema_version=8)
        create_profile_field_rule(profile_version=profile, field_definition=self.profile.field_rules.get().field_definition,
            source_selector="request.text", generation_mode="translate_only", generation_source_selector="request.action",
            generation_instruction="Translate only the supplied text.", channel="positive_prompt", position=1,
            required=True, generation_required=True, max_chars=1000, language_policy="en")
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        url = f"/api/v1/model-execution-instance-versions/{translator.pk}/field-smoke-source/"
        self.assertEqual(self.client.get(url, {"profile_version_id": str(self.profile.pk)}).status_code, 400)
        body = {**self.body, "profile_version_id": str(profile.pk), "expected_definition_sha256": definition.sha256,
            "expected_profile_contract_sha256": profile.contract_sha256,
            "items": [{**self.item, "source_text": "一个人挥手。", "source_language": "Chinese"}]}
        result = self.client.post(url, body, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["source"]["value"]["operation"], "prompt.fields.translate")
        body["items"][0]["source_text"] = ""
        self.assertEqual(self.client.post(url, body, format="json").status_code, 400)
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())

    def uncertain_field_smoke(self):
        from studio.services.provider_registry import ProviderDriverError
        response = self.smoke(self.build())
        self.assertEqual(response.status_code, 202, response.data)
        prep = ExecutionPreparation.objects.get(pk=response.data["preparation"]["id"])
        advanced = advance_execution_preparation(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        claim = claim_next_deployment_smoke("field-resolution-test")
        self.assertIsNotNone(claim)
        peer = Mock()
        peer.invoke.side_effect = ProviderDriverError("Isolated lost response", submission_uncertain=True)
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            result = process_deployment_smoke(claim)
        self.assertEqual(result.status, "uncertain")
        peer.invoke.assert_called_once()
        prep.refresh_from_db()
        return result, prep, peer

    def resolve(self, attempt):
        return self.client.post(f"/api/v1/model-deployments/{self.deployment.pk}/resolve-smoke-uncertain/", {
            "smoke_attempt_id": str(attempt.pk), "outcome": "confirmed_not_submitted",
            "note": "已从测试服务记录确认该请求未被接收。",
        }, format="json")

    def test_field_smoke_resolution_uses_frozen_operation_and_never_promotes_or_replays(self):
        attempt, prep, peer = self.uncertain_field_smoke()
        old_result, old_sha = deepcopy(attempt.result), attempt.result_sha256
        response = self.resolve(attempt)
        self.assertEqual(response.status_code, 201, response.data)
        evidence = attempt.resolutions.get().evidence
        self.assertEqual(evidence["operation"], "prompt.fields.generate")
        self.assertEqual(evidence["scope"], "instance")
        self.assertEqual(evidence["preparation_id"], str(prep.pk))
        self.assertEqual(evidence["execution_instance_version_id"], str(self.leaf.pk))
        self.assertEqual(evidence["prepared_request_sha256"], prep.request_sha256)
        self.assertEqual(evidence["definition_sha256"], self.definition.sha256)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "uncertain")
        self.assertIsNone(attempt.evidence_expires_at)
        self.assertEqual((attempt.result, attempt.result_sha256), (old_result, old_sha))
        self.assertEqual(self.resolve(attempt).status_code, 400)
        self.assertIsNone(claim_next_deployment_smoke("after-resolution"))
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), 1)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())
        peer.invoke.assert_called_once()

    def test_field_smoke_resolution_rejects_corrupted_prepared_identity(self):
        attempt, prep, peer = self.uncertain_field_smoke()
        ModelDeploymentSmokeAttempt.objects.filter(pk=attempt.pk).update(request_sha256="0" * 64)
        response = self.resolve(attempt)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(attempt.resolutions.exists())
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())
        peer.invoke.assert_called_once()
