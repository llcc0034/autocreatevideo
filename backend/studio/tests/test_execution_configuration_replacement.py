"""Explicit policy successor on the original recipe queue; no live calls."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ExecutionPreparation, GenerationAttempt
from studio.services.execution_configuration_replacement import (
    apply_configuration_replacement, preview_configuration_replacement, replacement_options,
)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import clone_execution_draft, publish_execution_version, update_execution_draft
from studio.services.production_recipe_routing import require_current_recipe_preparation
from studio.services.prompt_profiles import clone_prompt_profile_successor, publish_prompt_profile, refresh_prompt_profile_contract
from studio.tests import test_production_recipe_media_preparations as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ConfigurationReplacementTests(TestCase):
    setUpTestData = classmethod(fixtures.RecipeMediaPreparationTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.RecipeMediaPreparationTests.write_contract)
    create_root = fixtures.RecipeMediaPreparationTests.create_root
    prepare = fixtures.RecipeMediaPreparationTests.prepare

    def setUp(self):
        fixtures.RecipeMediaPreparationTests.setUp(self)
        self.parent = self.prepare()
        self.original = deepcopy(self.parent.contract.resolved_inputs)
        old = self.version.prompt_profile_version
        profile = clone_prompt_profile_successor(old, expected_source_contract_sha256=old.contract_sha256, name="Same facts, corrected strategy")
        rule = profile.field_rules.get()
        rule.language_policy = "preserve_source"
        rule.save()
        profile.review_state = "ready"
        profile.save()
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        draft = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        draft = update_execution_draft(version_id=draft.pk, expected_revision=draft.revision,
            actor=self.actor, changes={"prompt_profile_version": profile})
        self.successor_definition = build_execution_definition(draft.pk)
        self.successor = publish_execution_version(version_id=draft.pk, expected_revision=draft.revision,
            expected_definition_sha256=self.successor_definition.sha256, actor=self.actor)

    def preview(self, **kwargs):
        return preview_configuration_replacement(**{"preparation_id": self.parent.pk, "version_id": self.successor.pk,
            "expected_preparation_revision": self.parent.revision, "actor": self.actor, **kwargs})

    def apply(self, preview, **kwargs):
        return apply_configuration_replacement(**{"preparation_id": self.parent.pk, "version_id": self.successor.pk,
            "expected_preparation_revision": preview["revision"], "preview_sha256": preview["preview_sha256"], "actor": self.actor, **kwargs})

    def test_public_preview_apply_replay_and_original_recipe_producer(self):
        url = f"/api/v1/execution-preparations/{self.parent.pk}/"
        options = self.client.get(url + "configuration-successors/")
        self.assertEqual(options.status_code, 200, options.data)
        self.assertEqual([row["id"] for row in options.data["versions"]], [str(self.successor.pk)])
        body = {"version_id": str(self.successor.pk), "expected_preparation_revision": self.parent.revision}
        preview = self.client.post(url + "configuration-replacement-preview/", body, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertTrue(any(row["path"].endswith("language_policy") for row in preview.data["changes"]))
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        original_plan = self.plan.sha256
        self.project.language = "different later value"
        self.project.save(update_fields=("language",))
        with patch("studio.services.production_recipe_media_preparations.resolve_recipe_media_sources", side_effect=AssertionError("No source reread")), patch(
                "studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("No new seed")):
            result = self.client.post(url + "configuration-replacement/", {**body, "preview_sha256": preview.data["preview_sha256"]}, format="json")
            self.assertEqual(result.status_code, 200, result.data)
            fresh = ExecutionPreparation.objects.get(pk=result.data["replacement_id"])
            self.assertEqual(self.prepare().pk, fresh.pk)
            self.assertEqual(self.apply(preview.data).pk, fresh.pk)
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.status, "cancelled")
        self.assertEqual(self.parent.contract.resolved_inputs, self.original)
        for key in self.original.keys() - {"profile_sources", "field_requests", "media_generation_target"}:
            self.assertEqual(fresh.contract.resolved_inputs[key], self.original[key], key)
        self.assertEqual(fresh.contract.review_contract, self.parent.contract.review_contract)
        self.assertEqual(fresh.execution_instance_version_id, self.successor.pk)
        self.assertEqual(fresh.contract.resolved_inputs["profile_sources"]["source_language"], self.original["profile_sources"]["source_language"])
        require_current_recipe_preparation(fresh, stage=self.stage)
        with self.assertRaisesMessage(ValidationError, "不能重新提交原路线"):
            require_current_recipe_preparation(self.parent, stage=self.stage)
        self.assertEqual(self.plan.sha256, original_plan)
        after = self.client.get(url + "configuration-successors/")
        self.assertEqual(after.data["replacement_id"], str(fresh.pk))
        self.assertFalse(after.data["allowed"])

    def test_stale_confirmation_and_claims_rejected_without_allocating(self):
        preview = self.preview()
        with self.assertRaisesMessage(ValidationError, "重新预览"):
            self.apply(preview, preview_sha256="0" * 64)
        ExecutionPreparation.objects.filter(pk=self.parent.pk).update(claim_token=uuid4(), claim_until=timezone.now())
        with self.assertRaisesMessage(ValidationError, "工作进程占用"):
            self.apply(preview)
        ExecutionPreparation.objects.filter(pk=self.parent.pk).update(claim_token=None, claim_until=None, revision=self.parent.revision + 1)
        with self.assertRaisesMessage(ValidationError, "重新读取"):
            self.apply(preview)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_main_call_any_status_and_frozen_request_are_not_replaced(self):
        attempt = GenerationAttempt.objects.create(stage_run=self.stage, preparation=self.parent,
            execution_instance_version=self.version, role="primary", unit_key=self.parent.unit_key,
            attempt_number=1, request_sha256="a" * 64, request_snapshot_key="isolated-guard-only")
        for status in ("queued", "failed", "succeeded"):
            GenerationAttempt.objects.filter(pk=attempt.pk).update(status=status)
            with self.subTest(status=status):
                with self.assertRaisesMessage(ValidationError, "已有主调用"):
                    self.preview()

    def test_frozen_request_cannot_be_replaced(self):
        from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
        from studio.services.execution_preparation_requests import compile_prepared_execution_transport
        from studio.services.execution_request_storage import store_prepared_request
        prepared = evaluate_preparation_fields(preparation_id=self.parent.pk,
            expected_preparation_revision=self.parent.revision, actor=self.actor)
        compiled = compile_prepared_execution_transport(preparation_id=prepared.pk,
            expected_preparation_revision=prepared.revision, actor=self.actor)
        detail = prepared.contract
        frozen = store_prepared_request(detail, {"schema_version": 1, "preparation_id": str(prepared.pk),
            "basis_preparation_revision": prepared.revision, "intent_sha256": prepared.intent_sha256,
            "field_selections": compiled.field_selections.value, "transport": compiled.transport.value})
        detail.save()
        prepared.status, prepared.request_sha256, prepared.request_frozen_at = "ready", frozen.sha256, timezone.now()
        prepared.save()
        self.parent = prepared
        with self.assertRaisesMessage(ValidationError, "已冻结"):
            self.preview()

    def test_other_users_and_non_successors_rejected(self):
        from django.contrib.auth import get_user_model
        self.client.force_authenticate(get_user_model().objects.create_user(username="not-owner"))
        url = f"/api/v1/execution-preparations/{self.parent.pk}/configuration-replacement-preview/"
        response = self.client.post(url, {"version_id": str(self.successor.pk), "expected_preparation_revision": 1}, format="json")
        self.assertEqual(response.status_code, 404)
        with self.assertRaisesMessage(ValidationError, "明确后继"):
            self.preview(version_id=self.version.pk)

    def test_changed_definition_rejected_and_failed_create_rolls_back_stop(self):
        from studio.services.json_contracts import json_contract
        altered = deepcopy(self.successor_definition.value)
        altered["target_fingerprint"] = "0" * 64
        with patch("studio.services.execution_configuration_replacement.verify_current_execution_definition", return_value=json_contract(altered)):
            with self.assertRaisesMessage(ValidationError, "必须保持不变"):
                self.preview()
        altered = deepcopy(self.successor_definition.value)
        parameter = next(iter(altered["interface"]["parameters"].values()))
        parameter["rule"]["default"] = "changed parameter is not a policy successor"
        with patch("studio.services.execution_configuration_replacement.verify_current_execution_definition", return_value=json_contract(altered)):
            with self.assertRaisesMessage(ValidationError, "必须保持不变"):
                self.preview()
        preview = self.preview()
        with patch("studio.services.execution_preparations.create_execution_instance_preparation", side_effect=ValidationError("isolated failure")):
            with self.assertRaisesMessage(ValidationError, "isolated failure"):
                self.apply(preview)
        self.parent.refresh_from_db()
        self.assertNotEqual(self.parent.status, "cancelled")
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_ended_stage_and_assembly_are_not_replaced(self):
        self.stage.status = "succeeded"
        self.stage.save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "已结束"):
            self.preview()


from studio.tests import test_recipe_material_repreparation as material_fixtures
from studio.tests import test_recipe_material_auxiliary_repreparation as auxiliary_fixtures
from studio.tests.test_data_structures import StructureFixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class MaterialConfigurationReplacementTests(StructureFixture):
    create_job = material_fixtures.RecipeMaterialRepreparationTests.create_job
    primary_version = material_fixtures.RecipeMaterialRepreparationTests.primary_version
    write = material_fixtures.RecipeMaterialRepreparationTests.write
    input = staticmethod(material_fixtures.RecipeMaterialRepreparationTests.input)
    configure_variant_sources = material_fixtures.RecipeMaterialRepreparationTests.configure_variant_sources
    variant_anchors = material_fixtures.RecipeMaterialRepreparationTests.variant_anchors
    select = material_fixtures.RecipeMaterialRepreparationTests.select
    tearDown = material_fixtures.RecipeMaterialRepreparationTests.tearDown
    allow_calls = material_fixtures.RecipeMaterialRepreparationTests.allow_calls
    advance = material_fixtures.RecipeMaterialRepreparationTests.advance
    fill_fields = material_fixtures.RecipeMaterialRepreparationTests.fill_fields
    collect = material_fixtures.RecipeMaterialRepreparationTests.collect
    configure_declaration = material_fixtures.RecipeMaterialRepreparationTests.configure_declaration
    untouched_variants = material_fixtures.RecipeMaterialRepreparationTests.untouched_variants
    child = auxiliary_fixtures.RecipeMaterialAuxiliaryRepreparationTests.child
    dispatch = auxiliary_fixtures.RecipeMaterialAuxiliaryRepreparationTests.dispatch

    def setUp(self):
        material_fixtures.RecipeMaterialRepreparationTests.setUp(self)
        self.untouched_variants()  # Real isolated successful primary images stay intact.
        self.actor = self.user
        self.parent = self.parents[0]
        profile = self.version.prompt_profile_version
        draft_profile = clone_prompt_profile_successor(profile, expected_source_contract_sha256=profile.contract_sha256, name="Corrected language strategy")
        rule = draft_profile.field_rules.order_by("position").first()
        rule.language_policy = "preserve_source"; rule.save()
        draft_profile.review_state = "ready"; draft_profile.save()
        refresh_prompt_profile_contract(draft_profile)
        draft_profile = publish_prompt_profile(draft_profile, actor=self.actor)
        draft = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        draft = update_execution_draft(version_id=draft.pk, expected_revision=draft.revision,
            actor=self.actor, changes={"prompt_profile_version": draft_profile})
        definition = build_execution_definition(draft.pk)
        self.successor = publish_execution_version(version_id=draft.pk, expected_revision=draft.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        from studio.services.execution_configuration_replacement import _changes, _policy_independent_definition
        self.assertEqual(_changes(_policy_independent_definition(self.definition), _policy_independent_definition(definition)), [])

    preview = ConfigurationReplacementTests.preview
    apply = ConfigurationReplacementTests.apply

    def test_reference_facts_and_successful_primary_survive_successor_through_original_worker(self):
        from studio.services.execution_preparations import read_execution_preparation_detail
        calls = list(GenerationAttempt.objects.values())
        inputs = deepcopy(read_execution_preparation_detail(self.parent).resolved_inputs)
        replacement = self.apply(self.preview())
        self.assertEqual(list(GenerationAttempt.objects.values()), calls)
        fresh = read_execution_preparation_detail(replacement).resolved_inputs
        for key in ("inputs", "recipe_material_binding", "recipe_material_requirement"):
            self.assertEqual(fresh[key], inputs[key], key)
        for key in ("values", "generated_values", "resolved_source_contracts"):
            self.assertEqual(fresh["parameters"][key], inputs["parameters"][key], key)
        from studio.services.execution_parameter_preparation import verify_prepared_execution_parameters
        from studio.services.execution_definitions import stored_execution_contract, verify_stored_execution_definition
        verify_prepared_execution_parameters(verify_stored_execution_definition(stored_execution_contract(self.successor.pk)), fresh["parameters"])
        self.assertFalse(replacement.field_selections.exists())
        self.advance(supply_fields=True)
        results = [self.collect(), self.collect()]
        self.advance()
        self.assertIn(replacement.pk, [item.generation_attempt.preparation_id for item in results])
        self.assertFalse(GenerationAttempt.objects.filter(preparation=self.parent).exists())
        self.assertEqual(list(GenerationAttempt.objects.filter(pk__in=[item["id"] for item in calls]).values()), calls)

    def test_queued_auxiliary_is_stopped_but_uncertain_outcome_is_not_replaced(self):
        child = self.child(self.parent)
        attempt = self.dispatch(child)
        self.parent.refresh_from_db()
        self.assertEqual(attempt.status, "queued")
        preview = self.preview()
        GenerationAttempt.objects.filter(pk=attempt.pk).update(status="failed", error_code="uncertain_remote_response")
        with self.assertRaisesMessage(ValidationError, "结果未知"):
            self.apply(preview)
        self.assertFalse(ExecutionPreparation.objects.filter(supersedes=self.parent).exists())
        GenerationAttempt.objects.filter(pk=attempt.pk).update(status="queued", error_code="")
        replacement = self.apply(self.preview())
        attempt.refresh_from_db(); child.refresh_from_db()
        self.assertEqual((attempt.status, child.status), ("cancelled", "cancelled"))
        self.assertFalse(replacement.field_selections.exists())

