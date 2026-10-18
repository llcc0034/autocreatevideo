"""Request-bound candidates over real frozen records/files, no native admission.

Only the not-yet-admitted script/output registry pairing is isolated. Definition
identity, saved request storage, original task links and script payload checks
all run unpatched. Instance definitions and lifecycle rows are explicit storage
fixtures, not authoring/publication results or an executed model fixture.
"""
from copy import deepcopy
from hashlib import sha256
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from studio.models import (ExecutionPreparation, ExecutionPreparationContract, ExecutionVersionContract,
    GeneratedArtifact, GenerationAttempt, ModelExecutionInstanceVersion)
from studio.services.business_outputs import output_reference, write_output
from studio.services.execution_instances import execution_version_configuration
from studio.services.execution_request_storage import reference_prepared_request, store_prepared_request
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.script_semantic_candidates import read_script_semantic_candidate, save_script_semantic_candidate
from studio.services.script_semantic_requests import build_script_semantic_request
from studio.tests import test_script_semantic_candidates as storage


LIMIT = storage.LIMIT


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ScriptSemanticCandidateRequestTests(TestCase):
    make_output = storage.ScriptSemanticCandidateTests.make_output
    counts = storage.ScriptSemanticCandidateTests.counts

    def setUp(self):
        storage.ScriptSemanticCandidateTests.setUp(self)
        self.enterContext(patch.dict("studio.services.execution_output_contracts._OUTPUT_PROTOCOLS", {
            "script.generate": ("script_semantics.v1",), "script.structure": ("script_semantics.v1",)}))

    def call(self, operation="script.generate"):
        self.counter += 1
        version = self.version
        if version.operation != operation:
            version = ModelExecutionInstanceVersion.objects.create(instance=version.instance, version=self.counter,
                operation=operation, deployment=version.deployment, prompt_mode="manual")
        # Construct, but do not save a fake frozen request that would need to be
        # overwritten. freeze() persists the real intent before its first file.
        prep = ExecutionPreparation(stage_run=self.stage, execution_instance_version=version, purpose="production",
            unit_key="script", request_key=str(uuid4()), status="preparing", created_by=self.actor)
        return GenerationAttempt(stage_run=self.stage, execution_instance_version=version, preparation=prep,
            role="primary", unit_key="script", attempt_number=self.counter, operation=operation, status="running")

    def freeze(self, *, operation="script.generate", mutate_transport=None, mutate_definition=None,
            source_reference=None, adoption_baseline=None):
        if operation != self.attempt.operation:
            self.attempt = self.call(operation)
        version = self.attempt.execution_instance_version
        version.output_version = self.output
        version.save(update_fields=("output_version",))
        inputs = {"idea": "只使用本次创意", "custom_requirement": False}
        if operation == "script.structure":
            inputs = {"source_text": " 原始对白\r\n不添加剧情 ", "planning": {"episode_count": 2}}
        parameters = {"max_tokens": 2048, "seed": 0}
        channels = {"positive_prompt": "使用已选结构", "negative_prompt": "不改对白", "system_instruction": "保持顺序"}
        body, plan = build_script_semantic_request(operation=operation, output_reference=self.output_ref,
            inputs=inputs, parameters=parameters, transport_policy={"timeout_seconds": 31, "chat_request_options": {}},
            prompt_profile_assembly={"channels": channels, "structured_fields": {}},
            plan_max_bytes=LIMIT, request_max_bytes=LIMIT, source_reference=source_reference)
        configuration = execution_version_configuration(version)
        if operation == "script.structure":
            # The source resolver's literal provenance is an explicit fixture;
            # no source is read from current project state.
            from studio.models import ExecutionInputSpec, ExecutionInputTarget
            spec = ExecutionInputSpec.objects.create(version=version, key="source", kind="text", position=0,
                required=True, min_items=1, max_items=1,
                semantic_contract={"schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": [],
                    "allowed_scope_kinds": ["project", "request"], "paired_with_input_key": None, "allow_repeated_asset": False})
            ExecutionInputTarget.objects.create(input_spec=spec, target_kind="operation_input", target_key="source_text", is_primary=True)
            configuration = execution_version_configuration(version)
        target = {"deployment_id": str(version.deployment_id), "driver_type": "openai_compatible", "workflow": None,
            "model": {"id": str(version.deployment.profile_version_id), "specification_sha256": "a" * 64}}
        resources = {"deployment_id": str(version.deployment_id), "model_profile_version_id": target["model"]["id"],
            "model_specification_sha256": "a" * 64, "conditioning_mode": None,
            "execution": {"operation": operation, "mode": version.mode}}
        interface = {"parameters": {}, "input_targets": {}, "input_purposes": {}, "auxiliaries": {},
            "prompt": {"mode": "manual", "channels": {}, "auxiliary_purposes": []},
            "business_output": self.output_ref,
            "native_policy": {"timeout_seconds": 31, "chat_request_options": {}},
            "output": {"adapter": "isolated.script_semantics", "kind": "script", "schema_version": 1}}
        definition = {"schema_version": 1, "version_id": str(version.pk), "configuration": configuration,
            "interface": interface, "resources": resources, "target": target,
            "target_fingerprint": json_contract(target).sha256}
        if mutate_definition:
            mutate_definition(definition)
        contract = json_contract(definition)
        ExecutionVersionContract.objects.create(version=version, definition=contract.value, schema_version=1,
            size_bytes=contract.size_bytes, sha256=contract.sha256, target_fingerprint=definition["target_fingerprint"])
        preparation = self.attempt.preparation
        detail = ExecutionPreparationContract(preparation=preparation)
        resolved = {"inputs": {"operation_inputs": inputs, "input_provenance": [{"input_key": "source", "kind": "literal"}]},
            "parameters": {"values": parameters, "sources": {"max_tokens": "system", "seed": "request"}}}
        if adoption_baseline is not None:
            resolved["script_adoption_baseline"] = deepcopy(adoption_baseline)
        assign_json_contract(detail, "resolved_inputs", resolved)
        assign_json_contract(detail, "review_contract", {"schema_version": 1, "mode": "manual"})
        assign_json_contract(detail, "intent", {"schema_version": 1, "request": {"definition_sha256": contract.sha256},
            "resolved_inputs_sha256": detail.resolved_inputs_sha256, "review_contract_sha256": detail.review_contract_sha256})
        transport = {"schema_version": 1, "execution_instance_version_id": str(version.pk),
            "definition_sha256": contract.sha256, "target_fingerprint": definition["target_fingerprint"],
            "operation": operation, "driver_type": "openai_compatible", "idempotency_key": str(preparation.pk),
            "parameters": resolved["parameters"], "prompt_channels": {"values": channels}, "profile_compilation": None,
            "field_request_sha256": None, "payload": body, "output": interface["output"], "operation_plan": plan}
        if mutate_transport:
            mutate_transport(transport)
        preparation.intent_sha256 = detail.intent_sha256
        preparation.save()
        frozen = store_prepared_request(detail, {"schema_version": 1, "preparation_id": str(preparation.pk),
            "basis_preparation_revision": 1, "intent_sha256": detail.intent_sha256, "field_selections": {}, "transport": transport})
        detail.save()
        preparation.request_sha256, preparation.revision, preparation.request_frozen_at = frozen.sha256, 2, timezone.now()
        preparation.status = "dispatched"
        preparation.save()
        self.attempt.request_sha256 = frozen.sha256
        self.attempt.model_deployment = version.deployment
        reference_prepared_request(self.attempt, detail)
        self.attempt.save()
        self.attempt.preparation = preparation
        return detail, transport

    def save(self):
        return save_script_semantic_candidate(self.attempt, self.stage, self.run, response_text=self.text, max_bytes=LIMIT)

    def finish(self):
        digest = self.save()
        self.attempt.status, self.attempt.result_sha256 = "succeeded", digest
        self.attempt.save(update_fields=("status", "result_sha256"))
        return GeneratedArtifact.objects.get(generation_attempt=self.attempt)

    def test_saved_request_drives_candidate_and_read_without_callers_output_override(self):
        self.freeze()
        artifact = self.finish()
        with patch("studio.services.script_semantic_requests.build_script_semantic_request", side_effect=AssertionError("no rebuild")), \
                patch("studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError("no parse")):
            detail = read_script_semantic_candidate(artifact, max_bytes=LIMIT)
        self.assertEqual(detail["output_contract"], self.output_ref)
        self.assertEqual(detail["text"], self.text)
        with self.assertRaises(TypeError):
            read_script_semantic_candidate(artifact, expected_output=self.output_ref, max_bytes=LIMIT)
        with self.assertRaises(TypeError):
            save_script_semantic_candidate(self.attempt, self.stage, self.run, response_text=self.text,
                expected_output=self.output_ref, max_bytes=LIMIT)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_literal_structure_uses_its_actual_input_provenance_and_keeps_original_text(self):
        self.output, self.raw = self.make_output("b")
        self.output_ref = output_reference(self.output)
        self.text = json.dumps(self.raw, ensure_ascii=False)
        _, transport = self.freeze(operation="script.structure")
        artifact = self.finish()
        self.assertEqual(read_script_semantic_candidate(artifact, max_bytes=LIMIT)["text"], self.text)
        self.assertEqual(transport["operation_plan"]["plan"]["inputs"]["source_text"], " 原始对白\r\n不添加剧情 ")

    def test_list_root_candidate_has_shared_review_shape_and_keeps_custom_values(self):
        from studio.tests.output_structure_fixtures import response_structure
        from studio.tests.script_semantics_fixtures import script_example

        raw, _, mapping = script_example()
        def wrap_root(node):
            if isinstance(node, dict):
                if node.get("kind") == "path" and node.get("scope") == "root":
                    node["path"] = [0, *node["path"]]
                else:
                    for value in node.values():
                        wrap_root(value)
            elif isinstance(node, list):
                for value in node:
                    wrap_root(value)
        wrap_root(mapping)
        structure = response_structure(example=[raw])
        self.output = write_output(actor=self.actor, data={"name": "Isolated list root",
            "parser_key": "script_semantics.v1", "data_structure_version": str(structure.pk),
            "semantic_mapping": mapping})
        self.output = write_output(actor=self.actor, output_id=self.output.pk,
            expected_revision=self.output.revision, action="publish")
        self.output_ref = output_reference(self.output)
        raw["自定义.字段"] = {"score": 0, "flag": False, "empty": None}
        self.raw = [raw]
        self.text = " \r\n" + json.dumps(self.raw, ensure_ascii=False) + "\r\n "
        self.freeze()
        artifact = self.finish()
        detail = read_script_semantic_candidate(artifact, max_bytes=LIMIT)
        self.assertEqual(detail["candidate"], self.raw)
        self.assertEqual(detail["text"], self.text)
        self.assertNotIn("source_value", detail["projection"])
        self.assertTrue(detail["warnings"])
        self.assertEqual(detail["warnings"], [f'{row["path"]}：{row["message"]}' for row in detail["projection"]["warnings"]])
        self.assertEqual(detail["projection"]["value"]["entities"][0]["key"], "person-1")
        self.assertEqual(set(artifact.manifest_metadata["result"]),
            {"schema_version", "script_revision_id", "script_structure_sha256"})
        detail["candidate"][0]["自定义.字段"]["flag"] = True
        detail["projection"]["warnings"].clear()
        fresh = read_script_semantic_candidate(artifact, max_bytes=LIMIT)
        self.assertEqual(fresh["candidate"], self.raw)
        self.assertTrue(fresh["warnings"])
        self.assertEqual((artifact.status, self.run.script_revision_id), ("available", None))

    def test_missing_request_never_creates_candidate(self):
        with self.assertRaises(ValidationError):
            self.save()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_missing_frozen_file_prevents_write_and_read_without_rebuilding(self):
        detail, _ = self.freeze()
        artifact = self.finish()
        default_storage.delete(detail.prepared_request_key)
        with self.assertRaises(ValidationError):
            self.save()
        with self.assertRaises(ValidationError):
            read_script_semantic_candidate(artifact, max_bytes=LIMIT)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_body_changed_without_manifest_hash_is_rejected_before_source_creation(self):
        detail, _ = self.freeze()
        with default_storage.open(detail.prepared_request_key, "wb") as stream:
            stream.write(b'{}')
        with self.assertRaises(ValidationError):
            self.save()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_self_consistent_request_with_wrong_plan_output_is_rejected(self):
        other, _ = self.make_output("b")
        self.freeze(mutate_transport=lambda value: value["operation_plan"]["plan"].update(business_output=output_reference(other)))
        with self.assertRaises(ValidationError):
            self.save()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_self_consistent_request_with_wrong_user_message_is_rejected(self):
        self.freeze(mutate_transport=lambda value: value["payload"]["messages"][1].update(content="changed"))
        with self.assertRaises(ValidationError):
            self.save()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_plan_parameters_must_match_preparation_even_when_payload_agrees(self):
        def mismatch(value):
            value["operation_plan"]["plan"]["parameters"]["seed"] = 22
            value["payload"]["seed"] = 22
        self.freeze(mutate_transport=mismatch)
        with self.assertRaises(ValidationError):
            self.save()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_actual_channels_must_match_plan_even_when_messages_agree_with_plan(self):
        self.freeze(mutate_transport=lambda value: value.update(prompt_channels={"values": {"positive_prompt": "other"}}))
        with self.assertRaises(ValidationError):
            self.save()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_plan_inputs_must_match_prepared_inputs_even_with_consistent_message(self):
        def mismatch(value):
            plan = value["operation_plan"]["plan"]
            plan["inputs"]["idea"] = "未授权的另一份创意"
            user = json.loads(value["payload"]["messages"][1]["content"])
            user["inputs"] = deepcopy(plan["inputs"])
            from studio.services.json_contracts import canonical_json_bytes
            value["payload"]["messages"][1]["content"] = canonical_json_bytes(user).decode()
        self.freeze(mutate_transport=mismatch)
        with self.assertRaises(ValidationError):
            self.save()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_literal_preparation_cannot_acquire_invented_revision_provenance(self):
        text = " 原始对白\r\n不添加剧情 "
        self.freeze(operation="script.structure", source_reference={"id": str(uuid4()), "source_id": str(uuid4()),
            "revision": 1, "content_sha256": sha256(text.encode()).hexdigest(), "content_size_bytes": len(text.encode())})
        with self.assertRaises(ValidationError):
            self.save()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_manual_request_cannot_impersonate_a_profile_compilation(self):
        self.freeze(mutate_transport=lambda value: value.update(profile_compilation={"assembly": {"structured_fields": {}}}))
        with self.assertRaises(ValidationError):
            self.save()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_wrong_original_attempt_manifest_or_task_identity_is_rejected(self):
        self.freeze()
        for key, value in (("request_snapshot_key", "execution-requests/missing.json"), ("request_sha256", "b" * 64),
                ("model_deployment_id", None), ("unit_key", "other"), ("role", "auxiliary")):
            previous = getattr(self.attempt, key)
            setattr(self.attempt, key, value)
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.save()
            setattr(self.attempt, key, previous)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_retired_output_does_not_replace_frozen_contract_with_another_output(self):
        self.freeze()
        artifact = self.finish()
        write_output(actor=self.actor, output_id=self.output.pk, expected_revision=self.output.revision, action="retire")
        other, _ = self.make_output("b")
        with CaptureQueriesContext(connection) as queries:
            detail = read_script_semantic_candidate(artifact, max_bytes=LIMIT)
        self.assertEqual(detail["output_contract"], self.output_ref)
        self.assertNotEqual(detail["output_contract"]["id"], str(other.pk))
        # The shared frozen definition reader verifies stored identity, not live
        # profile rules. Query budget excludes publication/admission work.
        self.assertLessEqual(len(queries), 24)

    def test_unregistered_output_pairing_cannot_create_a_candidate(self):
        self.freeze()
        with patch.dict("studio.services.execution_output_contracts._OUTPUT_PROTOCOLS", {}, clear=True):
            with self.assertRaises(ValidationError):
                self.save()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
