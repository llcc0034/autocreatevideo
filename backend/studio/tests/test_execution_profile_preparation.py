"""Resolved literals -> real composite definition -> saved preparation -> Profile.

No fake project/shot and no AI output substitute: the editable field is explicitly
entered, confirmed and selected by its owner through the real field service.
"""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import AssetGenerationJob
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import publish_execution_version
from studio.services.execution_preparation_fields import create_manual_execution_field, review_execution_field, select_execution_field
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_profile_preparation import compile_preparation_profile
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_auxiliary_definitions as composite_fixtures


def request_for(fixture):
    return {"inputs": fixture.sources, "parameters": fixture.parameters, "manual_channels": {}, "subject_facts": [],
        "source_language": "English", "target_language": "English"}


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionProfilePreparationTests(TestCase):
    setUpTestData = classmethod(composite_fixtures.ExecutionAuxiliaryDefinitionTests.setUpTestData.__func__)
    write_contract = staticmethod(composite_fixtures.ExecutionAuxiliaryDefinitionTests.write_contract)
    input = staticmethod(composite_fixtures.ExecutionAuxiliaryDefinitionTests.input)
    bind = composite_fixtures.ExecutionAuxiliaryDefinitionTests.bind

    def setUp(self):
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential", "studio.services.comfyui.ComfyUIClient"):
            self.enterContext(patch(target, side_effect=AssertionError("Preparation preview cannot call a model")))
        composite_fixtures.ExecutionAuxiliaryDefinitionTests.setUp(self)
        self.bind()
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.sources = {"request.action": {"kind": "literal", "value": "A courier opens a blue parcel."},
            "style": {"kind": "literal", "value": {"lighting": "soft daylight"}}}
        self.parameters = {"width": 768, "height": 1344, "fps": 24, "duration_seconds": 3}
        self.job = AssetGenerationJob.objects.create(owner=self.actor, model_deployment=self.deployment, execution_instance_version=self.version,
            job_type="asset", media_type="video", operation="video.text_to_video", prompt="",
            idempotency_key="profile-prepare", request_snapshot_sha256=json_contract({}).sha256)

    def resolve(self, definition, owner):
        return resolve_execution_request_facts(definition, request_for(self), owner=owner.owner, unit_key="asset-unit", max_media_bytes=65536,
            review_contract=json_contract({"schema_version": 1, "mode": "manual"}), parameter_policies=[],
            resolve_parameter_source=lambda _: self.fail("No undeclared parameter sources"))

    def prepare(self):
        self.prep = create_execution_instance_preparation(owner_ref=("asset_job_id", self.job.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256, unit_key="asset-unit", request_key="candidate-1",
            intent=json_contract(request_for(self)), resolve_facts=self.resolve, actor=self.actor)
        return self.prep

    def preview(self):
        self.prep.refresh_from_db()
        return compile_preparation_profile(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.actor)

    def selected_text(self, text):
        self.prep.refresh_from_db()
        value = create_manual_execution_field(preparation_id=self.prep.pk, unit_key="asset-unit", field_key="composite_direction",
            text=text, revision=1, expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.prep.refresh_from_db()
        value = review_execution_field(preparation_id=self.prep.pk, field_value_id=value.pk, approve=True,
            expected_text_sha256=value.text_sha256, expected_source_sha256=value.source_sha256,
            expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.prep.refresh_from_db()
        select_execution_field(preparation_id=self.prep.pk, source_preparation_id=self.prep.pk, field_value_id=value.pk,
            expected_preparation_revision=self.prep.revision, expected_selection_revision=None, replace=False, actor=self.actor)
        return value

    def test_real_sources_become_exact_frozen_field_task_not_an_implicit_model_call(self):
        prep = self.prepare()
        requests = prep.contract.resolved_inputs["field_requests"]
        self.assertEqual(len(requests), 1)
        request = requests[0]["value"]
        self.assertEqual(request["operation"], "prompt.fields.generate")
        item = request["items"][0]
        self.assertEqual(item["source_text"], self.sources["request.action"]["value"])
        self.assertEqual(item["context_facts"], {"project.style": {"lighting": "soft daylight"}})
        self.assertEqual(item["unit_key"], "asset-unit")
        self.assertFalse(prep.field_values.exists())
        self.assertIsNone(prep.request_frozen_at)
        self.assertEqual(AssetGenerationJob.objects.count(), 1)
        with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
            self.preview()

    def test_explicit_manual_selection_uses_original_compiler_and_target_channel_contract(self):
        self.prepare()
        selected = self.selected_text("In soft daylight, a courier carefully opens a blue parcel.")
        result = self.preview()
        self.assertEqual(result.preparation_id, str(self.prep.pk))
        self.assertIn(selected.text, result.resolved_channels["values"]["positive_prompt"])
        self.assertEqual(result.resolved_channels, self.preview().resolved_channels)
        self.assertEqual(result.compilation.reviewed_field_evidence["field_selections"][0]["field_value_id"], str(selected.pk))
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.status, "preparing")
        self.assertIsNone(self.prep.request_frozen_at)
        self.assertEqual(AssetGenerationJob.objects.count(), 1)

    def test_new_source_does_not_modify_an_existing_intent_or_preview(self):
        first = self.prepare()
        self.selected_text("A courier opens a blue parcel in soft daylight.")
        before = self.preview()
        self.sources["request.action"]["value"] = "A different request."
        with self.assertRaises(ValidationError):
            self.prepare()
        self.prep = first
        self.assertEqual(self.preview().resolved_channels, before.resolved_channels)

    def test_undeclared_reference_tokens_are_not_given_invented_labels(self):
        self.sources["request.action"]["value"] = "Use <Picture 1> as an identity reference."
        with self.assertRaises(ValidationError):
            self.prepare()
        self.assertFalse(self.job.execution_preparations.exists())

    def display(self):
        from studio.services.execution_prompt_display import read_preparation_prompt_display
        self.prep.refresh_from_db()
        return read_preparation_prompt_display(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor)

    def candidate(self, text, revision=1):
        self.prep.refresh_from_db()
        return create_manual_execution_field(preparation_id=self.prep.pk, unit_key="asset-unit", field_key="composite_direction",
            text=text, revision=revision, expected_preparation_revision=self.prep.revision, actor=self.actor)

    def test_prompt_display_candidate_is_readonly_and_does_not_bypass_production_gate(self):
        from django.db import connection
        self.prepare()
        value = self.candidate("A courier opens a blue parcel in soft daylight.")
        self.prep.refresh_from_db()
        revision = self.prep.revision
        def no_write(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith(("SELECT", "SAVEPOINT", "RELEASE")), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(no_write):
            result = self.display()
        self.assertEqual(result["mode"], "preview")
        self.assertIn(value.text, result["channels"][0]["text"])
        self.assertEqual(result["field_basis"][0]["origin"], "candidate")
        self.assertIsNone(result["request_sha256"])
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.revision, revision)
        self.assertFalse(self.prep.field_selections.exists())
        value.refresh_from_db()
        self.assertEqual(value.status, "pending")
        with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
            self.preview()

    def test_prompt_display_selection_wins_over_new_unselected_candidate(self):
        self.prepare()
        selected = self.selected_text("A courier opens a blue parcel in soft daylight.")
        candidate = self.candidate("An unselected courier watches the blue parcel.", revision=2)
        result = self.display()
        self.assertEqual(result["field_basis"][0]["field_value_id"], str(selected.pk))
        self.assertEqual(result["field_basis"][0]["origin"], "adopted")
        self.assertNotIn(candidate.text, result["channels"][0]["text"])
        self.assertEqual(result["channels"][0]["text"], self.preview().resolved_channels["values"]["positive_prompt"])

    def test_prompt_display_missing_or_latest_rejected_is_explicit_and_never_uses_older_candidate(self):
        self.prepare()
        self.assertEqual(self.display()["mode"], "unavailable")
        self.candidate("A courier opens a blue parcel in soft daylight.")
        latest = self.candidate("A courier watches the blue parcel in daylight.", revision=2)
        self.prep.refresh_from_db()
        review_execution_field(preparation_id=self.prep.pk, field_value_id=latest.pk, approve=False,
            expected_text_sha256=latest.text_sha256, expected_source_sha256=latest.source_sha256,
            expected_preparation_revision=self.prep.revision, actor=self.actor)
        result = self.display()
        self.assertEqual(result["mode"], "unavailable")
        self.assertEqual(result["channels"], [])
        self.assertTrue(result["issues"])

    def test_prompt_display_api_checks_owner_revision_and_closed_query(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIRequestFactory, force_authenticate
        from studio.execution_preparation_views import ExecutionPreparationViewSet
        self.prepare()
        factory = APIRequestFactory()
        def read(params, user=None):
            request = factory.get("/api/v1/execution-preparations/x/prompt-display/", params)
            if user is not False:
                force_authenticate(request, user or self.actor)
            return ExecutionPreparationViewSet.as_view({"get": "prompt_display"})(request, pk=self.prep.pk)
        params = {"expected_preparation_revision": self.prep.revision}
        self.assertEqual(read(params).status_code, 200)
        self.assertEqual(read({"expected_preparation_revision": self.prep.revision + 1}).status_code, 409)
        self.assertEqual(read({**params, "auto_approve": True}).status_code, 400)
        self.assertEqual(read({"expected_preparation_revision": [1, 2]}).status_code, 400)
        stranger = get_user_model().objects.create_user(username="prompt-stranger")
        self.assertEqual(read(params, stranger).status_code, 404)
        self.assertIn(read(params, False).status_code, (401, 403))
