"""Formal author API -> media preparation -> explicit unapproved field stop.

Published model/workflow and media fixtures are isolated, not live admission.
The strategy and both execution targets under test are authored through HTTP.
No field confirmation, auxiliary dispatch, real Smoke or model call is made.
"""
from copy import deepcopy
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    AssetGenerationJob, ExecutionPreparation, GenerationAttempt, GenerationTask,
    ModelDeploymentSmokeAttempt, PromptProfileFieldRule, WorkflowSmokeAttempt,
)
from studio.services.execution_preparation_queue import advance_next_execution_preparation
from studio.tests import test_video_reference_projection as media_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class M6AuthoringWaitingTests(TestCase):
    _create_version = media_fixtures.VideoAuxiliaryMediaPreparationTests._create_version
    selection = media_fixtures.VideoAuxiliaryMediaPreparationTests.selection
    configure_generation = media_fixtures.VideoAuxiliaryMediaPreparationTests.configure_generation

    def setUp(self):
        self.assertEqual(connection.vendor, "sqlite")
        self.assertIn("memory", str(connection.settings_dict["NAME"]))
        for target in ("socket.socket.connect", "socket.socket.connect_ex"):
            self.enterContext(patch(target, side_effect=AssertionError("No external calls in authoring acceptance")))
        media_fixtures.VideoAuxiliaryMediaPreparationTests.setUp(self)
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def api(self, method, path, body=None, *, status=200, **headers):
        response = getattr(self.client, method)(path, body, format="json", **headers)
        self.assertEqual(response.status_code, status, response.data)
        return response.json()

    def author_target(self, key, source, **changes):
        base = "/api/v1/model-execution-instance-versions/"
        configuration = self.api("get", f"{base}{source.pk}/")["configuration"]
        instance = self.api("post", "/api/v1/model-execution-instances/",
            {"key": key, "name": key}, status=201)
        draft = self.api("post", base, {
            **configuration, **changes,
            "instance_id": instance["id"], "expected_revision": instance["revision"],
        }, status=201)
        url = f"{base}{draft['id']}/"
        reviewed = self.api("get", url + "input-contract/")
        self.assertFalse(reviewed["frozen"])
        published = self.api("post", url + "publish/", {
            "expected_revision": reviewed["revision"], "expected_definition_sha256": reviewed["sha256"],
        })
        self.assertEqual(published["status"], "published")
        frozen = self.api("get", url + "input-contract/")
        self.assertTrue(frozen["frozen"])
        self.assertEqual(frozen["definition"], reviewed["definition"])
        return published, frozen

    def test_draft_projection_conversion_keeps_rule_identity_and_requires_compatible_publication(self):
        execution_models = (ExecutionPreparation, AssetGenerationJob, GenerationAttempt, GenerationTask,
            ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt)
        before_counts = tuple(model.objects.count() for model in execution_models)
        profiles = "/api/v1/prompt-profile-versions/"
        source = self.api("get", f"{profiles}{self.prompt_profile.pk}/")
        authored = self.api("post", profiles, {
            "profile_key": "m6-convert-existing-rule", "name": "M6 existing-rule conversion fixture",
            "model_family": source["model_family"], "operation": source["operation"], "mode": source["mode"],
            "source_projection_key": "direct_v1", "source_projection_config": {},
        }, status=201)
        url = f"{profiles}{authored['id']}/"
        source_rule = PromptProfileFieldRule.objects.get(profile_version=self.prompt_profile)
        rule = self.api("post", "/api/v1/prompt-profile-field-rules/", {
            "profile_version": authored["id"], "field_definition": str(source_rule.field_definition_id),
            "field_definition_version": str(source_rule.field_definition_version_id),
            "source_selector": "request.subject", "generation_source_selector": "request.subject",
            "channel": "positive_prompt", "position": 1, "required": True, "max_chars": 2000,
            "generation_mode": "passthrough", "language_policy": "preserve_source",
            "generation_instruction": "", "generation_output_semantics": "final", "generation_required": False,
            "length_unit": "none", "generation_validator_keys": [], "generation_context_selectors": [],
        }, status=201)
        converted = self.api("patch", url, {
            "source_projection_key": "declarative_v1", "source_projection_config": source["source_projection_config"],
            "input_structure_version": source["input_structure_version"], "review_state": "ready",
        })
        self.assertEqual(converted["source_projection_key"], "declarative_v1")
        rule_url = f"/api/v1/prompt-profile-field-rules/{rule['id']}/"
        self.assertEqual(self.api("get", rule_url), rule)
        self.assertEqual(PromptProfileFieldRule.objects.filter(profile_version_id=authored["id"]).count(), 1)
        rejected = self.api("post", url + "publish/", {}, status=400)
        self.assertIn("projection.output", str(rejected))
        self.assertEqual(self.api("get", url)["status"], "draft")
        self.assertEqual(self.api("get", rule_url), rule)
        # Explicitly edit the same rule; conversion itself never guesses or rewrites it.
        source_rule_data = self.api("get", f"/api/v1/prompt-profile-field-rules/{source_rule.pk}/")
        changes = {key: source_rule_data[key] for key in (
            "source_selector", "source_path", "projection_primitive", "generation_mode",
            "generation_source_selector", "generation_instruction", "generation_output_semantics",
            "generation_required", "language_policy", "length_unit", "generation_min", "generation_max",
            "generation_validator_keys", "generation_context_selectors",
        )}
        adapted = self.api("patch", rule_url, changes)
        self.assertEqual(adapted["id"], rule["id"])
        self.assertEqual(adapted["field_definition_version"], rule["field_definition_version"])
        before_rejected_change = self.api("get", url)
        rejected = self.api("patch", url, {
            "source_projection_key": "direct_v1", "source_projection_config": {},
        }, status=400)
        self.assertIn("source_selector", str(rejected))
        self.assertEqual(self.api("get", url), before_rejected_change)
        self.assertEqual(self.api("get", rule_url), adapted)
        self.assertEqual(self.api("post", url + "publish/", {})["status"], "published")
        self.api("patch", url, {"source_projection_key": "direct_v1", "source_projection_config": {}}, status=400)
        self.assertEqual(PromptProfileFieldRule.objects.filter(profile_version_id=authored["id"]).count(), 1)
        self.assertEqual(tuple(model.objects.count() for model in execution_models), before_counts)

    def test_declarative_author_chain_and_leaf_reach_missing_then_pending_media_field_without_dispatch(self):
        profiles = "/api/v1/prompt-profile-versions/"
        source = self.api("get", f"{profiles}{self.prompt_profile.pk}/")
        authored = self.api("post", profiles, {
            "profile_key": "m6-api-authoring", "name": "M6 explicit authoring fixture",
            "model_family": source["model_family"], "operation": source["operation"], "mode": source["mode"],
            "source_projection_key": "direct_v1", "source_projection_config": {},
            "channel_limits": source["channel_limits"],
        }, status=201)
        url = f"{profiles}{authored['id']}/"
        converted = self.api("patch", url, {
            "source_projection_key": "declarative_v1", "source_projection_config": source["source_projection_config"],
            "input_structure_version": source["input_structure_version"],
        })
        self.assertEqual(converted["source_projection_key"], "declarative_v1")
        rule = PromptProfileFieldRule.objects.get(profile_version=self.prompt_profile)
        rule_source = self.api("get", f"/api/v1/prompt-profile-field-rules/{rule.pk}/")
        writable = (
            "field_definition", "field_definition_version", "source_selector", "channel", "source_version",
            "source_path", "projection_primitive", "position", "label_style", "separator", "required",
            "fixed_value", "has_default", "default_value", "omit_if_empty", "max_chars", "generation_mode",
            "generation_source_selector", "generation_instruction", "generation_output_semantics",
            "generation_required", "language_policy", "length_unit", "generation_min", "generation_max",
            "generation_validator_keys", "generation_context_selectors",
        )
        created_rule = self.api("post", "/api/v1/prompt-profile-field-rules/", {
            **{key: rule_source[key] for key in writable}, "profile_version": authored["id"],
        }, status=201)
        self.assertEqual(created_rule["field_definition_version"], rule_source["field_definition_version"])
        self.api("patch", url, {"review_state": "ready"})
        published_profile = self.api("post", url + "publish/", {})
        self.assertEqual(published_profile["status"], "published")
        leaf, leaf_frozen = self.author_target("m6-api-leaf", self.leaf)
        self.assertEqual(leaf["operation"], "prompt.fields.generate")
        self.assertEqual(leaf["configuration"]["auxiliaries"], [])
        self.assertEqual(leaf["configuration"]["inputs"][0]["key"], "field_request")
        self.assertEqual(leaf_frozen["definition"]["interface"]["output"]["adapter"], "text.prompt_fields/v1")
        target, frozen = self.author_target("m6-api-media", self.version,
            prompt_profile_version_id=authored["id"],
            auxiliaries=[{"purpose": "generate_fields", "target_version_id": leaf["id"]}])
        self.assertEqual(target["configuration"]["prompt_profile_version_id"], authored["id"])

        source_text = "The courier in a blue coat turns toward the parcel."
        inputs = deepcopy(self.sources)
        inputs["units"]["value"]["ordered_units"][0]["structured_input"]["direction"] = source_text
        body = {"execution_instance_version_id": target["id"], "expected_definition_sha256": frozen["sha256"],
            "job_type": "asset", "media_type": "video", "name": "M6 authored waiting fixture", "field_values": [],
            "allow_external_calls": False, "inputs": inputs, "parameters": self.parameter_values,
            "manual_channels": {}, "subject_facts": [], "source_language": "English", "target_language": "English"}
        call_models = (GenerationAttempt, GenerationTask, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt)
        counts = lambda: tuple(model.objects.count() for model in call_models)
        before = counts()
        guards = [self.enterContext(patch(target, side_effect=AssertionError("Unapproved fields cannot execute")))
            for target in ("studio.services.execution_advancement.finalize_execution",
                "studio.services.execution_advancement.dispatch_asset_execution")]
        created = self.api("post", "/api/v1/asset-generation-jobs/", body, status=202,
            HTTP_IDEMPOTENCY_KEY="m6-authored-wait")
        job = AssetGenerationJob.objects.get(pk=created["id"])
        prep = job.execution_preparations.get()
        self.assertEqual(prep.status, "awaiting_input")
        self.assertIsNone(prep.next_check_at)
        self.assertIsNone(advance_next_execution_preparation())
        facts = deepcopy(prep.contract.resolved_inputs)
        request = facts["field_requests"][0]["value"]["items"][0]
        self.assertEqual(request["source_text"], source_text)
        logical = request["reference_facts"]["logical_inputs"]
        self.assertEqual([row["item_key"] for row in logical], ["second", "first"])
        candidate_text = "The courier turns toward the parcel without changing the supplied reference identities."
        saved = self.api("post", "/api/v1/execution-field-values/", {
            "preparation_id": str(prep.pk), "unit_key": request["unit_key"], "field_key": created_rule["field_key"],
            "text": candidate_text, "revision": 1, "expected_preparation_revision": prep.revision,
        }, status=201)
        self.assertEqual(saved["field"]["status"], "pending")
        self.assertIsNone(saved["selection"])
        scanned = advance_next_execution_preparation()
        self.assertEqual(scanned.pk, prep.pk)
        self.assertEqual(scanned.status, "awaiting_review")
        self.assertIsNone(scanned.next_check_at)
        self.assertIsNone(advance_next_execution_preparation())
        base = f"/api/v1/execution-preparations/{prep.pk}/"
        for _ in range(2):
            shown = self.api("get", base + "prompt-display/", {"expected_preparation_revision": scanned.revision})
            self.assertEqual(shown["mode"], "preview")
            self.assertEqual({row["origin"] for row in shown["field_basis"]}, {"candidate"})
            self.assertIn(candidate_text, next(row["text"] for row in shown["channels"] if row["key"] == "positive_prompt"))
            sizes = self.api("get", base + "storage-metrics/", {"expected_preparation_revision": scanned.revision})
            self.assertEqual(next(row for row in sizes["items"] if row["key"] == "prepared_request")["state"], "not_generated")
        replay = self.api("post", "/api/v1/asset-generation-jobs/", body, HTTP_IDEMPOTENCY_KEY="m6-authored-wait")
        self.assertEqual(replay["id"], created["id"])
        prep.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(prep.revision, scanned.revision)
        self.assertEqual(prep.contract.resolved_inputs, facts)
        self.assertEqual(prep.field_values.get().status, "pending")
        self.assertFalse(prep.field_selections.exists())
        self.assertFalse(prep.auxiliary_asset_jobs.exists())
        self.assertIsNone(prep.request_frozen_at)
        self.assertIsNone(job.submission_started_at)
        self.assertEqual(job.status, "preparing")
        self.assertEqual(ExecutionPreparation.objects.filter(asset_job=job).count(), 1)
        self.assertEqual(counts(), before)
        for guard in guards:
            guard.assert_not_called()
