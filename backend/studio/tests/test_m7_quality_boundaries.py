"""M7-06 isolated quality / hard-error composition; no remote acceptance.

Run from this worktree's backend with the main checkout's venv Python -B:
  PYTHON_DOTENV_DISABLED=1 DATABASE_URL=sqlite:///:memory:
  python -B manage.py test studio.tests.test_m7_quality_boundaries --verbosity 2

Existing builders supply isolated rows. Response text below is an explicit
transport fixture passed to the real parser, NOT a successful model task.
Saving that text is an explicit manual action and keeps its manual origin.
Registry/Workflow history comes from the existing isolated builders. No
admission verdict, business validator, selection, parser or compiler is replaced.
The positive endpoint is the real transport compiler, not a submission permit;
M7-01 owns frozen-request/driver integration. No existing suite is inherited.
"""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import (
    Asset, AssetGenerationJob, ExecutionFieldSelection, ExecutionFieldValue,
    ExecutionPreparationContract, GeneratedArtifact, GenerationAttempt,
    GenerationTask, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt,
)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_instances import (
    clone_execution_draft, publish_execution_version, update_execution_draft,
)
from studio.services.execution_media_sources import resolve_execution_workflow_media_sources
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_fields import confirm_execution_field
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_profile_preparation import compile_preparation_profile
from studio.services.execution_prompt_display import read_preparation_prompt_display
from studio.services.execution_text_contracts import parse_execution_text_response
from studio.services.json_contracts import JsonContract, assign_json_contract, json_contract
from studio.services.prompt_profiles import (
    PromptProfileChannelLengthError, clone_prompt_profile_successor,
    publish_prompt_profile, refresh_prompt_profile_contract,
)
from studio.tests import test_execution_field_review as review_fixtures
from studio.tests import test_prompt_final_assembly_contract as assembly_fixtures


class IsolationAssertions:
    @classmethod
    def setUpClass(cls):
        if not sys.dont_write_bytecode or os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
            raise AssertionError("Use Python -B and PYTHON_DOTENV_DISABLED=1")
        if os.environ.get("DATABASE_URL") != "sqlite:///:memory:":
            raise AssertionError("M7 requires an explicit in-memory database")
        if connection.vendor != "sqlite" or "memory" not in str(connection.settings_dict["NAME"]):
            raise AssertionError("M7 must not connect to a persistent database")
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
            "socket.getaddrinfo", "requests.sessions.Session.request",
            "studio.services.provider_registry.resolve_credential",
        ):
            guard = cls.enterClassContext(patch(target, side_effect=AssertionError("M7 forbids network/credentials")))
            cls.addClassCleanup(guard.assert_not_called)
        super().setUpClass()

    def isolate_quality(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="m7-quality-")))
        self.enterContext(override_settings(MEDIA_ROOT=root, ASSET_LIBRARY_ROOT=root / "library"))

    def effects(self):
        return {model.__name__: model.objects.count() for model in (
            Asset, AssetGenerationJob, GenerationAttempt, GenerationTask, GeneratedArtifact,
            ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt,
        )}

    def evidence(self):
        self.prep.refresh_from_db()
        root = Path(settings.MEDIA_ROOT)
        return {
            "effects": self.effects(),
            "fields": list(ExecutionFieldValue.objects.filter(preparation=self.prep).order_by("id").values()),
            "selections": list(ExecutionFieldSelection.objects.filter(preparation=self.prep).order_by("id").values()),
            "preparation": (self.prep.revision, self.prep.status, self.prep.reason_code,
                self.prep.request_sha256, self.prep.request_frozen_at),
            "files": {str(p.relative_to(root)): sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*") if p.is_file()},
        }

    def no_admission(self, *_args):
        self.fail("Hard errors and unselected fields must fail before admission")


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class M7FieldQualityBoundariesTests(IsolationAssertions, TestCase):
    setUpTestData = classmethod(review_fixtures.ExecutionFieldReviewTests.setUpTestData.__func__)
    write_contract = staticmethod(review_fixtures.ExecutionFieldReviewTests.write_contract)
    input = staticmethod(review_fixtures.ExecutionFieldReviewTests.input)
    bind = review_fixtures.ExecutionFieldReviewTests.bind
    stage = review_fixtures.ExecutionFieldReviewTests.stage
    prepare = review_fixtures.ExecutionFieldReviewTests.prepare
    field = review_fixtures.ExecutionFieldReviewTests.field
    body = "composite_direction:\r\nA courier  opens the blue parcel, pauses, and slowly turns toward the door."

    def setUp(self):
        self.isolate_quality()
        review_fixtures.ExecutionFieldReviewTests.setUp(self)

    def configure(self, *, field_max=1000, channel_max=1600):
        """Explicit isolated successor; never mutate a published contract."""
        profile = clone_prompt_profile_successor(self.profile,
            expected_source_contract_sha256=self.profile.contract_sha256, name="M7 advisory fixture")
        rule = profile.field_rules.get()
        rule.max_chars, rule.length_unit = field_max, "english_words"
        rule.generation_min, rule.generation_max = 4, 8
        rule.generation_validator_keys = ["non_empty", "no_control_characters", "length_advisory"]
        rule.generation_instruction = "Prefer concise prose without a field label; preserve complete factual wording."
        rule.label_style = "none"
        rule.save()
        profile.channel_limits = {"positive_prompt": {"max_chars": channel_max, "warn_at_chars": 30}}
        profile.review_state = "ready"
        profile.save(update_fields=("channel_limits", "review_state"))
        refresh_prompt_profile_contract(profile)
        self.profile = publish_prompt_profile(profile, actor=self.actor)
        draft = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        draft = update_execution_draft(version_id=draft.pk, expected_revision=draft.revision,
            changes={"prompt_profile_version": self.profile}, actor=self.actor)
        self.definition = build_execution_definition(draft.pk)
        self.version = publish_execution_version(version_id=draft.pk, expected_revision=draft.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)

    def parse(self, text, *, response_id=None):
        self.prep.refresh_from_db()
        batch = JsonContract(**self.prep.contract.resolved_inputs["field_requests"][0])
        item = batch.value["items"][0]
        response = {"text": json.dumps({"items": [{
            "request_id": item["request_id"] if response_id is None else response_id, "text": text,
        }]}, ensure_ascii=False)}
        parsed = parse_execution_text_response(batch.value["operation"], response, field_request=batch)
        self.assertEqual(parsed["result"]["items"], [{"request_id": item["request_id"], "text": text}])
        return parsed

    def evaluate(self):
        self.prep.refresh_from_db()
        self.prep = evaluate_preparation_fields(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor)
        return self.prep

    def confirm(self, value):
        self.prep.refresh_from_db()
        confirm_execution_field(preparation_id=self.prep.pk, field_value_id=value.pk,
            expected_preparation_revision=self.prep.revision, expected_text_sha256=value.text_sha256,
            expected_source_sha256=value.source_sha256, select_for_execution=True,
            expected_selection_revision=None, replace=False, actor=self.actor)

    def display(self):
        self.prep.refresh_from_db()
        return read_preparation_prompt_display(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor)

    def freeze(self, collector):
        self.prep.refresh_from_db()
        self.prep = finalize_execution(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor,
            collect_admission_facts=collector)
        return self.prep

    def assert_assembled_body(self, selected, pending):
        evidence = self.evidence()
        before = self.display()
        self.assertEqual(before["channels"][0]["text"], selected.text)
        self.assertEqual(before["field_basis"][0]["field_value_id"], str(selected.pk))
        self.prep.refresh_from_db()
        compilation = compile_preparation_profile(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor).compilation
        self.assertEqual(compilation.assembly["channel_audit"]["positive_prompt"]["status"], "warning")
        self.assertTrue(compilation.assembly["warnings"])
        request = compile_prepared_execution_transport(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor)
        transport = request.transport
        self.assertEqual(transport.value["payload"]["prompt"], selected.text)
        self.assertEqual(transport.value["prompt_channels"]["values"]["positive_prompt"], selected.text)
        self.assertEqual(transport.value["profile_compilation"]["assembly"]["warnings"], compilation.assembly["warnings"])
        fields = request.field_selections.value["fields"]
        self.assertEqual([row["field_value_id"] for row in fields], [str(selected.pk)])
        self.assertEqual(fields[0]["text_sha256"], sha256(selected.text.encode()).hexdigest())
        pending.refresh_from_db()
        self.assertEqual((pending.status, pending.approval_kind), ("pending", None))
        self.assertNotIn(pending.text, transport.value["payload"]["prompt"])
        with CaptureQueriesContext(connection) as queries:
            after = self.display()
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual(after["mode"], "preview")
        self.assertEqual(after["channels"], before["channels"])
        self.assertEqual(self.evidence(), evidence)
        self.assertIsNone(self.prep.request_frozen_at)
        self.assertEqual(self.prep.stage_run.status, "running")
        self.assertFalse(self.prep.stage_run.approval_decisions.exists())

    def test_manual_advisories_preserve_parsed_and_saved_body_require_action_then_assemble(self):
        self.configure()
        self.prepare(stage=self.stage(source="manual"))
        before = self.evidence()
        parsed = self.parse(self.body)
        self.assertEqual({row["code"] for row in parsed["warnings"]},
            {"recommended_length_out_of_range", "generated_field_label"})
        self.assertTrue(all(row["severity"] == "warning" for row in parsed["warnings"]))
        self.assertEqual(self.evidence(), before)  # Parsing is not task completion or adoption.
        selected = self.field(text=parsed["result"]["items"][0]["text"])
        self.assertEqual((selected.text, selected.origin_kind), (self.body, "manual"))
        self.assertEqual(self.evaluate().status, "awaiting_review")
        self.assertFalse(self.prep.field_selections.exists())
        selected.refresh_from_db()
        self.assertEqual((selected.status, selected.approval_kind), ("pending", None))
        before = self.evidence()
        with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
            self.freeze(self.no_admission)
        self.assertEqual(self.evidence(), before)
        self.confirm(selected)
        selected.refresh_from_db()
        self.assertEqual((selected.approval_kind, selected.reviewed_by_id), ("human", self.actor.pk))
        pending = self.field(revision=2, text="Pending alternative must never replace the adopted wording.")
        self.assertEqual(self.evaluate().status, "preparing")
        self.assert_assembled_body(selected, pending)

    def test_explicit_policy_advisories_approve_as_policy_and_never_replace_selected_value(self):
        self.configure()
        self.prepare()
        self.assertEqual(self.parse(self.body)["result"]["items"][0]["text"], self.body)
        selected = self.field(text=self.body)
        self.assertEqual(self.evaluate().status, "preparing")
        selected.refresh_from_db()
        self.assertEqual((selected.status, selected.approval_kind, selected.reviewed_by_id), ("confirmed", "policy", None))
        self.assertEqual(selected.approval_policy_sha256, json_contract(self.choice["policy"]).sha256)
        receipt = (selected.reviewed_at, selected.approval_policy_sha256)
        pending = self.field(revision=2, text="This newer pending wording must remain unselected.")
        self.evaluate()
        selected.refresh_from_db()
        self.assertEqual((selected.reviewed_at, selected.approval_policy_sha256), receipt)
        self.assert_assembled_body(selected, pending)

    def test_policy_ambiguous_advisory_candidates_are_not_silently_chosen(self):
        self.configure()
        self.prepare()
        self.field(text=self.body)
        self.field(revision=2, text="Go")  # Below the configured recommended minimum also remains valid.
        self.assertEqual(self.parse("Go")["warnings"][0]["code"], "recommended_length_out_of_range")
        before = self.evidence()
        with self.assertRaises(ValidationError) as rejected:
            self.evaluate()
        self.assertEqual(rejected.exception.code, "field_selection_conflict")
        self.assertEqual(self.evidence(), before)

    def test_adopted_boundary_whitespace_is_preserved_by_final_assembly(self):
        """Minimal regression: _typed_value strips the already adopted final text."""
        self.configure()
        self.prepare()
        text = "\r\nA courier opens the blue parcel.\r\n"
        self.assertEqual(self.parse(text)["result"]["items"][0]["text"], text)
        selected = self.field(text=text)
        self.evaluate()
        before = self.evidence()
        self.prep.refresh_from_db()
        compiled = compile_preparation_profile(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor)
        selected.refresh_from_db()
        self.assertEqual(selected.text, text)
        self.assertEqual(self.evidence(), before)
        self.assertEqual(compiled.resolved_channels["values"]["positive_prompt"], text)

    def test_field_hard_limit_rejects_manual_save_and_response_without_replacing_adoption(self):
        self.configure()
        self.prepare()
        selected = self.field(text=self.body)
        self.evaluate()
        before = self.evidence()
        oversized = "A" * 1001
        for action in (lambda: self.field(revision=2, text=oversized), lambda: self.parse(oversized)):
            with self.subTest(action=action.__name__), self.assertRaisesMessage(ValidationError, "字段硬字符上限"):
                action()
            self.assertEqual(self.evidence(), before)
        self.assertEqual(self.prep.field_selections.get().field_value_id, selected.pk)

    def assert_length_rejection(self, *, channel_max, field_max, text, effective_max):
        self.configure(field_max=field_max, channel_max=channel_max)
        self.prepare()
        selected = self.field(text=text)
        self.evaluate()
        self.field(revision=2, text="New candidate must not conceal the adopted overflow.")
        before = self.evidence()
        shown = self.display()
        self.assertEqual(shown["mode"], "diagnostic")
        self.assertEqual(next(row["text"] for row in shown["channels"] if row["key"] == "positive_prompt"), text)
        self.assertEqual(shown["field_basis"][0]["field_value_id"], str(selected.pk))
        diagnostic = next(row for row in shown["length_diagnostics"] if row["channel"] == "positive_prompt")
        self.assertEqual(diagnostic["max_chars"], effective_max)
        self.assertEqual(diagnostic["over_by"], len(text) - effective_max)
        self.assertTrue(shown["issues"])
        with self.assertRaises(PromptProfileChannelLengthError) as rejected:
            self.freeze(self.no_admission)
        self.assertEqual(rejected.exception.diagnostic["channels"]["positive_prompt"], text)
        self.assertEqual(self.evidence(), before)
        self.assertIsNone(self.prep.request_frozen_at)

    def test_profile_channel_hard_limit_keeps_complete_diagnostic_and_refuses_freeze(self):
        self.assert_length_rejection(channel_max=100, field_max=1000, text="courier " * 20 + ".", effective_max=100)

    def test_actual_instance_interface_limit_wins_over_larger_profile_limit(self):
        self.assert_length_rejection(channel_max=4000, field_max=3000, text="courier " * 260 + ".", effective_max=2000)

    def test_response_identity_mapping_and_structure_errors_leave_adopted_value_untouched(self):
        self.configure()
        self.prepare()
        self.field(text=self.body)
        self.evaluate()
        before = self.evidence()
        with self.assertRaisesMessage(ValidationError, "不属于本次请求"):
            self.parse(self.body, response_id="other-request")
        batch = JsonContract(**self.prep.contract.resolved_inputs["field_requests"][0])
        for response in ({"text": "not JSON"}, {"text": '{"items": [], "items": []}'}, {}):
            with self.subTest(response=response), self.assertRaises(ValidationError):
                parse_execution_text_response(batch.value["operation"], response, field_request=batch)
        self.assertEqual(self.evidence(), before)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class M7ReferenceHardBoundariesTests(IsolationAssertions, assembly_fixtures.AssemblyAssertions, TestCase):
    _create_version = assembly_fixtures.ReferenceFinalAssemblyTests._create_version
    selection = assembly_fixtures.ReferenceFinalAssemblyTests.selection
    resolve = assembly_fixtures.ReferenceFinalAssemblyTests.resolve
    prepare = assembly_fixtures.ReferenceFinalAssemblyTests.prepare
    configure = assembly_fixtures.ReferenceFinalAssemblyTests.configure
    candidate = assembly_fixtures.ReferenceFinalAssemblyTests.candidate

    def setUp(self):
        assembly_fixtures.ReferenceFinalAssemblyTests.setUp(self)

    def adopted_pair(self):
        self.prepare()
        self.selected = self.candidate()
        self.candidate("Pending alternative is not a replacement for damaged media.", revision=2, adopt=False)

    def assert_rejected_unchanged(self):
        before = self.evidence()
        with self.assertRaises(ValidationError) as rejected:
            self.finalize(collector=self.no_admission)
        self.assertTrue(rejected.exception.messages)
        self.assertEqual(self.evidence(), before)
        self.assertIsNone(self.prep.request_frozen_at)
        self.assertEqual(self.prep.field_selections.get().field_value_id, self.selected.pk)
        self.assert_no_dispatch()
        return str(rejected.exception)

    def test_missing_actual_media_file_refuses_staging_source_without_replacement_or_artifact(self):
        self.adopted_pair()
        # Compilation uses saved metadata; the original staging adapter calls
        # this shared reader to re-authorize current bytes before any upload.
        request = self.transport()
        self.assertTrue(request.transport.value["payload"]["compiled_workflow"]["graph"])
        self.prep.refresh_from_db()
        inputs = json_contract(self.prep.contract.resolved_inputs["inputs"], max_bytes=262144)
        path = Path(self.asset.file.path).resolve()
        self.assertTrue(path.is_relative_to(Path(settings.MEDIA_ROOT).resolve()))
        self.assertTrue(path.is_file())
        path.unlink()  # Only this test's temporary file, never persistent media.
        before = self.evidence()
        with self.assertRaises(ValidationError) as rejected:
            resolve_execution_workflow_media_sources(self.definition, inputs,
                owner=self.user, max_media_bytes=65536)
        self.assertIn("media_source", rejected.exception.message_dict)
        self.assertEqual(self.evidence(), before)
        self.assertIsNone(self.prep.request_frozen_at)
        self.assertEqual(self.prep.field_selections.get().field_value_id, self.selected.pk)
        self.assert_no_dispatch()
        self.assertFalse(path.exists())

    def test_rehashed_wrong_media_slot_is_rejected_against_original_intent_without_fallback(self):
        self.adopted_pair()
        self.prep.refresh_from_db()
        detail = self.prep.contract
        facts = deepcopy(detail.resolved_inputs)
        envelope = facts["profile_sources"]["reference_context"]
        envelope["value"]["context"]["reference_bindings"][0]["input_slot"] = 99
        facts["profile_sources"]["reference_context"] = asdict(json_contract(envelope["value"]))
        assign_json_contract(detail, "resolved_inputs", facts, max_bytes=262144)
        # Explicit isolated corruption. Rehashing the detail cannot authorize a
        # different mapping: the original intent must still bind these facts.
        ExecutionPreparationContract._base_manager.filter(pk=detail.pk).update(**{
            key: getattr(detail, key) for key in ("resolved_inputs", "resolved_inputs_schema_version",
                "resolved_inputs_size_bytes", "resolved_inputs_sha256")})
        message = self.assert_rejected_unchanged()
        self.assertIn("准备意图没有覆盖完整来源、实际种子及审核合同", message)
