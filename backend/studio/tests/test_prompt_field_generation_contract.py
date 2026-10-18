"""M6-03: original production preparation/worker/API, synthetic model boundary only.

Admission observations are the existing isolated fixture, not live readiness or
Smoke evidence. All files are temporary; outbound sockets are forbidden. Import
fixture methods, not their test cases, so this module runs only its own methods.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    ExecutionFieldValue, ExecutionTextResponseReview, GeneratedArtifact,
    GenerationAttempt, ModelDeploymentSmokeAttempt, PromptProfileVersion,
)
from studio.services.execution_field_review import (
    apply_preparation_field_policy, production_field_review_contract,
)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import (
    clone_execution_draft, publish_execution_version, update_execution_draft,
)
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_profile_preparation import compile_preparation_profile
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.field_definitions import field_definition_snapshot
from studio.services.json_contracts import json_contract
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.services.provider_registry import ProviderDriverError
from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
from studio.tests import test_execution_production_auxiliaries as fixtures
from studio.tests.field_definition_fixtures import (
    create_field_definition, create_profile_field_rule, publish_test_field_version,
)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class PromptFieldGenerationContractTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionAuxiliaryTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ProductionAuxiliaryTests.write_contract)
    input = staticmethod(fixtures.ProductionAuxiliaryTests.input)
    bind = fixtures.ProductionAuxiliaryTests.bind
    stage = fixtures.ProductionAuxiliaryTests.stage
    prepare = fixtures.ProductionAuxiliaryTests.prepare
    child = fixtures.ProductionAuxiliaryTests.child
    advance = fixtures.ProductionAuxiliaryTests.advance

    def setUp(self):
        self.assertEqual(settings.DATABASES["default"]["ENGINE"], "django.db.backends.sqlite3")
        self.assertIn("memory", str(settings.DATABASES["default"]["NAME"]))
        root = self.enterContext(TemporaryDirectory(prefix="prompt-field-contract-"))
        self.media_root = Path(root).resolve()
        self.enterContext(override_settings(MEDIA_ROOT=root, STORAGES={
            **settings.STORAGES, "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        }))
        for target in ("socket.socket.connect", "socket.socket.connect_ex"):
            blocked = self.enterContext(patch(target, side_effect=AssertionError("No business HTTP/model sockets")))
            self.addCleanup(blocked.assert_not_called)
        fixtures.ProductionAuxiliaryTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.fields = {}

    def configure(self, *, keys=("custom_camera_orbit",), mode="model_generated", language="en",
                  source_language="English", target_language="English", source="A courier opens a blue parcel.",
                  policy=False):
        """Publish exact test versions, then resolve the real original preparation."""
        if mode == "translate_only" and self.leaf.operation != "prompt.fields.translate":
            leaf = clone_execution_draft(source_id=self.leaf.pk, expected_revision=self.leaf.revision,
                expected_definition_sha256=self.leaf.contract.sha256, actor=self.actor)
            leaf = update_execution_draft(version_id=leaf.pk, expected_revision=leaf.revision,
                actor=self.actor, changes={"operation": "prompt.fields.translate"})
            self.leaf = publish_execution_version(version_id=leaf.pk, expected_revision=leaf.revision,
                expected_definition_sha256=build_execution_definition(leaf.pk).sha256, actor=self.actor)
        profile = PromptProfileVersion.objects.create(profile_key=f"m6-contract-{uuid4().hex}", version=1,
            name="Isolated dynamic field contract", model_family="async_cloud", operation="video.generate",
            mode="T2VA", review_state="ready", contract_schema_version=8)
        for position, key in enumerate(keys, 1):
            if key not in self.fields:
                field = create_field_definition(key=key, label=f"Old {key}", value_type="long_text")
                version = publish_test_field_version(field.pk, {
                    "label": f"Exact {key}", "category": "test", "value_type": "long_text",
                })
                self.fields[key] = (field, version)
            field, version = self.fields[key]
            create_profile_field_rule(profile_version=profile, field_definition=field,
                field_definition_version=version, source_selector="request.text",
                generation_source_selector="request.action", generation_context_selectors=["project.style"],
                generation_mode=mode, generation_output_semantics="final",
                generation_instruction="Use only the supplied source and lighting context.",
                generation_required=True, language_policy=language, channel="positive_prompt",
                position=position, required=True, max_chars=1000)
        refresh_prompt_profile_contract(profile)
        self.profile = publish_prompt_profile(profile, actor=self.actor)
        version = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        purpose = "translate_fields" if mode == "translate_only" else "generate_fields"
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"prompt_profile_version": self.profile,
                     "auxiliaries": [{"purpose": purpose, "target_version_id": str(self.leaf.pk)}]})
        self.definition = build_execution_definition(version.pk)
        self.version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.root_stage = self.stage() if policy else self.stage(interaction="assisted", choice={"mode": "manual"})
        self.run = self.root_stage.production_run
        self.run.allow_external_calls = True  # Test DB only; all external sockets remain blocked.
        self.run.save(update_fields=("allow_external_calls",))
        request = {"inputs": {"request.action": {"kind": "literal", "value": source},
                   "style": {"kind": "literal", "value": {"lighting": "soft daylight"}}},
                   "parameters": self.parameters, "manual_channels": {}, "subject_facts": [],
                   "source_language": source_language, "target_language": target_language}

        def resolve(definition, owner):
            return resolve_execution_request_facts(definition, request, owner=self.actor, unit_key="asset-unit",
                max_media_bytes=65536, review_contract=production_field_review_contract(owner), parameter_policies=[],
                resolve_parameter_source=lambda _: self.fail("No undeclared parameter source"))

        self.parent = create_execution_instance_preparation(owner_ref=("stage_run_id", self.root_stage.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            unit_key="asset-unit", request_key="candidate-1", intent=json_contract(request), resolve_facts=resolve,
            actor=self.actor)
        self.batch = deepcopy(self.parent.contract.resolved_inputs["field_requests"][0])
        self.key = keys[0]
        self.base = f"/api/v1/execution-preparations/{self.parent.pk}/"
        self.frozen = deepcopy(self.detail()["resolved_inputs"])

    def detail(self, preparation=None):
        preparation = preparation or self.parent
        response = self.client.get(f"/api/v1/execution-preparations/{preparation.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()

    def revision(self):
        self.parent.refresh_from_db()
        return self.parent.revision

    def manual(self, text, revision):
        response = self.client.post("/api/v1/execution-field-values/", {
            "preparation_id": str(self.parent.pk), "unit_key": "asset-unit", "field_key": self.key,
            "text": text, "revision": revision, "expected_preparation_revision": self.revision(),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.json()["field"]

    def confirm(self, field, **changes):
        response = self.client.post(f"/api/v1/execution-field-values/{field['id']}/confirm/", {
            "expected_text_sha256": field["text_sha256"], "expected_source_sha256": field["source_sha256"],
            "expected_preparation_revision": self.revision(), **changes,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()

    def value_detail(self, value):
        response = self.client.get(f"/api/v1/execution-field-values/{value.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()

    def execute(self, *, text="A courier opens a blue parcel in soft daylight.", raw=None, effect=None):
        child = self.child()
        self.assertEqual(self.advance(child).status, "dispatched")
        claimed = claim_next_text_attempt("isolated-m6-field-contract")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.preparation_id, child.pk)
        if raw is None:
            raw = json.dumps({"items": [{"request_id": item["request_id"], "text": text}
                for item in self.batch["value"]["items"]]}, ensure_ascii=False)
        peer = Mock()
        peer.invoke.side_effect = effect
        peer.invoke.return_value = {"text": raw, "request_id": "isolated-m6-response",
                                    "actual_cost": "0.03", "currency": "USD"}
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            result = process_text_attempt(claimed)
            peer.invoke.assert_called_once()
            # Reprocessing an original settled claim must not generate/save again.
            self.assertEqual(process_text_attempt(result).pk, result.pk)
            peer.invoke.assert_called_once()
        self.assertEqual(json.loads(peer.invoke.call_args.args[2]["messages"][1]["content"]), self.batch["value"])
        self.assertEqual((result.role, result.parent_preparation_id), ("auxiliary", self.parent.pk))
        self.assertFalse(child.field_values.exists())
        self.assertFalse(GenerationAttempt.objects.filter(preparation=self.parent).exists())
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        self.assertEqual(self.detail()["resolved_inputs"], self.frozen)
        return child, result, raw

    def assert_original(self, child, attempt, raw):
        review = ExecutionTextResponseReview.objects.get(attempt=attempt)
        self.assertEqual(review.original_text, raw)
        self.assertEqual(review.original_sha256, sha256(raw.encode("utf-8")).hexdigest())
        self.assertTrue((self.media_root / review.original_file).resolve().is_relative_to(self.media_root))
        self.assertTrue((self.media_root / review.original_file).is_file())
        data = self.detail(child)["text_response_review"]
        self.assertEqual(data["original_text"], raw)
        self.assertFalse(data["editable"])  # Manual field revision is a different source.
        return data

    def test_custom_fields_freeze_exact_version_source_and_worker_result_without_adoption(self):
        keys = ("custom_camera_orbit", "custom.sound_texture")
        self.configure(keys=keys)
        for key in keys:
            field, _ = self.fields[key]
            publish_test_field_version(field.pk, {"label": "Later catalog label", "category": "test", "value_type": "text"})
        child, attempt, raw = self.execute()
        self.assertEqual(attempt.status, "succeeded", attempt.error_message)
        self.assert_original(child, attempt, raw)
        values = {row.field_key: row for row in self.parent.field_values.all()}
        self.assertEqual(set(values), set(keys))
        for item in self.batch["value"]["items"]:
            rule = item["field_contract"]
            key = rule["field_key"]
            self.assertEqual(rule["field_definition"], field_definition_snapshot(self.fields[key][1]))
            self.assertEqual(rule["field_definition"]["version"], 2)
            self.assertEqual((rule["generation_mode"], rule["generation_source_selector"], rule["language_policy"]),
                             ("model_generated", "request.action", "en"))
            self.assertEqual(item["source_text"], "A courier opens a blue parcel.")
            self.assertEqual(item["context_facts"], {"project.style": {"lighting": "soft daylight"}})
            value = values[key]
            source = {name: content for name, content in item.items() if name not in {"field_contract", "request_id"}}
            self.assertEqual(value.source_sha256, json_contract({"profile": self.batch["value"]["profile"], **source}).sha256)
            self.assertEqual(value.field_contract_sha256, json_contract(rule).sha256)
            data = self.value_detail(value)
            self.assertEqual((data["origin_kind"], data["origin_attempt"], data["revision"]), ("model", str(attempt.pk), 1))
            self.assertEqual((data["status"], data["approval_kind"], data["reviewed_by"]), ("pending", None, None))
            self.assertEqual(data["text"], value.text)
        detail = self.detail()
        self.assertEqual(detail["review_summary"]["mode"], "manual")
        self.assertEqual(detail["field_selections"], [])
        self.assertEqual({slot["field_label"] for slot in detail["field_slots"]}, {f"Exact {key}" for key in keys})
        self.assertEqual(self.advance(self.parent).status, "awaiting_review")

    def test_language_modes_survive_generation_and_api_roundtrip(self):
        cases = (
            ("model_generated", "inherit", "English", "Chinese", "Chinese", "快递员打开蓝色包裹。"),
            ("model_generated", "preserve_source", "Chinese", "English", "Chinese", "快递员打开蓝色包裹。"),
            ("model_generated", "zh-Hant", "English", "English", "zh-Hant", "快遞員打開藍色包裹。"),
            ("model_generated", "bilingual:en+zh-CN", "English", "English", "bilingual:en+zh-CN", "A courier opens a parcel. 快递员打开包裹。"),
            ("translate_only", "en", "Chinese", "English", "en", "A courier opens a blue parcel."),
        )
        for mode, language, source_language, target_language, effective, text in cases:
            with self.subTest(mode=mode, language=language):
                source = "快递员打开蓝色包裹。" if source_language == "Chinese" else "A courier opens a blue parcel."
                self.configure(mode=mode, language=language, source_language=source_language,
                               target_language=target_language, source=source)
                child, attempt, raw = self.execute(text=text)
                self.assertEqual(attempt.status, "succeeded", attempt.error_message)
                operation = "prompt.fields.translate" if mode == "translate_only" else "prompt.fields.generate"
                self.assertEqual(attempt.operation, operation)
                self.assert_original(child, attempt, raw)
                slot = self.detail()["field_slots"][0]
                self.assertEqual((slot["source_language"], slot["target_language"], slot["effective_language"]),
                                 (source_language, target_language, effective))
                self.assertEqual(slot["field_contract"]["language_policy"], language)
                field = self.value_detail(self.parent.field_values.get())
                self.assertEqual((field["text"], field["status"]), (text, "pending"))
                self.confirm(field)  # Confirmation alone must not imply selection.
                self.assertEqual(self.detail()["field_selections"], [])
                self.assertEqual(self.detail()["field_slots"][0], slot)

    def test_generated_and_manual_revisions_preserve_original_selected_value_until_explicit_replace(self):
        self.configure()
        first = self.manual("The original manually adopted wording.", 1)
        selected = self.confirm(first, select_for_execution=True)["selection"]
        original = deepcopy(self.value_detail(ExecutionFieldValue.objects.get(pk=first["id"])))
        child, attempt, raw = self.execute()
        self.assertEqual(attempt.status, "succeeded", attempt.error_message)
        generated = self.value_detail(self.parent.field_values.get(origin_attempt=attempt))
        self.assertEqual((generated["revision"], generated["status"]), (2, "pending"))
        self.assertEqual(self.detail()["field_selections"], [selected])
        third = self.manual("An explicitly edited independent revision.", 3)
        self.assertEqual((third["origin_kind"], third["origin_attempt"], third["status"]), ("manual", None, "pending"))
        self.confirm(third)
        self.assertEqual(self.detail()["field_selections"], [selected])
        self.assertEqual(self.value_detail(ExecutionFieldValue.objects.get(pk=first["id"])), original)
        self.assertEqual(self.value_detail(ExecutionFieldValue.objects.get(pk=generated["id"])), generated)
        preview = compile_preparation_profile(preparation_id=self.parent.pk,
            expected_preparation_revision=self.revision(), actor=self.actor)
        self.assertIn(first["text"], preview.resolved_channels["values"]["positive_prompt"])
        self.assertNotIn(third["text"], preview.resolved_channels["values"]["positive_prompt"])
        replacement = self.confirm(third, select_for_execution=True, replace=True, expected_selection_revision=1)
        self.assertEqual(replacement["selection"]["field_value"], third["id"])
        self.assertEqual(replacement["selection"]["revision"], 2)
        self.assertEqual(replacement["field"]["approval_kind"], "human")
        self.assertEqual(self.parent.field_values.count(), 3)
        self.assert_original(child, attempt, raw)

    def test_frozen_policy_is_explicit_and_never_replaces_an_existing_human_selection(self):
        for existing in (False, True):
            with self.subTest(existing_human_selection=existing):
                self.configure(policy=True)
                before = None
                if existing:
                    first = self.manual("Previously selected by the owner.", 1)
                    before = self.confirm(first, select_for_execution=True)["selection"]
                frozen_review = deepcopy(self.detail()["review_contract"])
                _, attempt, _ = self.execute()
                self.assertEqual(attempt.status, "succeeded", attempt.error_message)
                value = self.parent.field_values.get(origin_attempt=attempt)
                self.assertEqual(value.status, "pending")
                self.assertEqual(self.detail()["field_selections"], [before] if existing else [])
                apply_preparation_field_policy(preparation_id=self.parent.pk,
                    expected_preparation_revision=self.revision(), actor=self.actor)
                value.refresh_from_db()
                if existing:
                    self.assertEqual((value.status, value.approval_kind), ("pending", None))
                    self.assertEqual(self.detail()["field_selections"], [before])
                else:
                    self.assertEqual((value.status, value.approval_kind, value.reviewed_by_id), ("confirmed", "policy", None))
                    self.assertEqual(value.approval_policy_sha256, json_contract(self.choice["policy"]).sha256)
                    self.assertEqual(self.parent.field_selections.get().field_value_id, value.pk)
                self.assertEqual(self.detail()["review_contract"], frozen_review)
                self.assertEqual(self.detail()["review_summary"]["mode"], "policy")
                self.root_stage.refresh_from_db()
                self.assertEqual(self.root_stage.status, "running")
                self.assertFalse(self.root_stage.approval_decisions.exists())

    def test_invalid_batch_keeps_raw_error_and_old_adoption_without_partial_field_writes(self):
        self.configure(keys=("custom_camera_orbit", "custom.sound_texture"))
        first = self.manual("Keep this human-selected text.", 1)
        selection = self.confirm(first, select_for_execution=True)["selection"]
        items = self.batch["value"]["items"]
        raw = json.dumps({"items": [{"request_id": items[0]["request_id"], "text": "A valid first field."},
                                  {"request_id": items[1]["request_id"], "text": ""}]})
        child, attempt, _ = self.execute(raw=raw)
        self.assertEqual((attempt.status, attempt.error_code), ("awaiting_review", "execution_output_review_required"))
        data = self.assert_original(child, attempt, raw)
        self.assertEqual(data["diagnostics"]["validation_code"], "empty")
        self.assertEqual([row["text"] for row in data["fields"]], ["A valid first field.", ""])
        self.assertTrue(attempt.error_message)
        self.assertEqual(self.parent.field_values.count(), 1)
        self.assertEqual(self.detail()["field_selections"], [selection])
        self.assertEqual(self.detail(child)["presentation"]["task"]["error_code"], attempt.error_code)
        self.client.force_authenticate(get_user_model().objects.create_user(username="m6-outsider"))
        self.assertEqual(self.client.get(f"/api/v1/execution-preparations/{child.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/execution-field-values/{first['id']}/").status_code, 404)

    def test_truncated_response_can_only_be_repaired_as_manual_field_not_model_success(self):
        self.configure()
        error = ProviderDriverError("isolated length failure", code="provider_response_invalid")
        error.metadata = {"contract_error_code": "response_output_budget_exhausted", "finish_reason": "length"}
        error.partial_text = '{"items": [\n{"request_id": "incomplete original"'
        child, attempt, _ = self.execute(effect=error)
        self.assertEqual(attempt.status, "awaiting_review")
        review = self.assert_original(child, attempt, error.partial_text)
        self.assertFalse(self.parent.field_values.exists())
        child.refresh_from_db()
        denied = self.client.post(f"/api/v1/execution-preparations/{child.pk}/complete-text-response/", {
            "expected_preparation_revision": child.revision, "review_id": review["id"],
            "original_sha256": review["original_sha256"], "text": '{"items": []}',
        }, format="json")
        self.assertEqual(denied.status_code, 400, denied.data)
        manual = self.manual("A human supplies the missing wording.", 1)
        self.assertEqual((manual["origin_kind"], manual["origin_attempt"]), ("manual", None))
        self.assertEqual(self.detail()["field_selections"], [])
        self.confirm(manual, select_for_execution=True)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "awaiting_review")
        self.assert_original(child, attempt, error.partial_text)
