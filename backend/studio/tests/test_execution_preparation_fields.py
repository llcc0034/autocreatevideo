from studio.tests.field_definition_fixtures import create_field_definition
"""Real frozen Profile rules; no HTTP, model dispatch, auto approval or fake Run."""
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import AssetGenerationJob, ExecutionPreparation, ExecutionPreparationContract, ModelDeploymentSmokeAttempt, FieldDefinition, PromptProfileFieldRule, PromptProfileVersion
from studio.services.execution_field_contracts import ExecutionFieldInput, freeze_execution_field_request
from studio.services.execution_preparation_fields import (
    create_manual_execution_field, frozen_preparation_field_requests, review_execution_field, select_execution_field,
    read_execution_field_selection_snapshot,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.services.prompt_profile_compiler import PromptProfileCompilationFacts, compile_prompt_profile_facts
from studio.tests import test_execution_preparation_storage as storage_fixtures


class ExecutionPreparationFieldTests(TestCase):
    setUpTestData = classmethod(storage_fixtures.ExecutionPreparationStorageTests.setUpTestData.__func__)
    smoke = storage_fixtures.ExecutionPreparationStorageTests.smoke
    preparation = storage_fixtures.ExecutionPreparationStorageTests.preparation

    def setUp(self):
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        self.member = get_user_model().objects.create_user(username="field-outsider")
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential", "studio.services.comfyui.ComfyUIClient"):
            self.enterContext(patch(target, side_effect=AssertionError("field edits cannot call a model")))
        field = create_field_definition(key="prepared_direction", label="Direction", value_type="long_text")
        profile = PromptProfileVersion.objects.create(
            profile_key="prep-fields", name="Fixture", version=1, model_family="fixture", operation="video.generate",
            mode="T2VA", review_state="ready", contract_schema_version=8,
        )
        create_profile_field_rule(
            profile_version=profile, field_definition=field, source_selector="request.text", generation_source_selector="request.action",
            generation_mode="model_generated", generation_instruction="Describe only the supplied facts.",
            generation_context_selectors=[], channel="positive_prompt", position=1, required=True, max_chars=1000,
        )
        refresh_prompt_profile_contract(profile)
        self.profile = publish_prompt_profile(profile, actor=self.actor)
        self.prep = self.prepared()

    def prepared(self, *, source_text="A red cube turns.", unit="sample", asset_job=None):
        if asset_job is None:
            smoke = self.smoke()
            smoke.prompt_profile_version = self.profile
            smoke.save(update_fields=("prompt_profile_version",))
            prep = self.preparation(deployment_smoke=smoke)
        else:
            prep = self.preparation(deployment_smoke=None, asset_job=asset_job, purpose="production", execution_instance_version=self.version)
        request = freeze_execution_field_request(operation="prompt.fields.generate", profile=self.profile, inputs=[ExecutionFieldInput(
            request_id="field-one", unit_key=unit, field_key="prepared_direction", source_text=source_text,
            source_language="English", target_language="English", context_facts={},
            reference_facts={"protected_tokens": [], "logical_inputs": [], "subject_facts": []},
        )])
        contract = ExecutionPreparationContract(preparation=prep)
        for name, value in (
            ("intent", {"fixture": True}),
            ("resolved_inputs", {"schema_version": 1, "field_requests": frozen_preparation_field_requests([request])}),
            ("review_contract", {"schema_version": 1, "mode": "manual"}),
        ):
            assign_json_contract(contract, name, value, max_bytes=65536)
        contract.save()
        return prep

    def create(self, **changes):
        self.prep.refresh_from_db()
        return create_manual_execution_field(**{
            "preparation_id": self.prep.pk, "unit_key": "sample", "field_key": "prepared_direction",
            "text": "The red cube turns slowly.", "revision": 1,
            "expected_preparation_revision": self.prep.revision, "actor": self.actor, **changes,
        })

    def review(self, value, **changes):
        self.prep.refresh_from_db()
        return review_execution_field(**{
            "preparation_id": self.prep.pk, "field_value_id": value.pk, "approve": True,
            "expected_text_sha256": value.text_sha256, "expected_source_sha256": value.source_sha256,
            "expected_preparation_revision": self.prep.revision, "actor": self.actor, **changes,
        })

    def select(self, value, **changes):
        self.prep.refresh_from_db()
        return select_execution_field(**{
            "preparation_id": self.prep.pk, "source_preparation_id": value.preparation_id, "field_value_id": value.pk,
            "expected_preparation_revision": self.prep.revision, "expected_selection_revision": None,
            "replace": False, "actor": self.actor, **changes,
        })

    def selection_snapshot(self, **changes):
        self.prep.refresh_from_db()
        return read_execution_field_selection_snapshot(**{
            "preparation_id": self.prep.pk, "expected_preparation_revision": self.prep.revision,
            "actor": self.actor, **changes,
        }).value

    def test_field_slot_label_uses_frozen_version_after_new_definition(self):
        from studio.services.execution_preparation_fields import read_preparation_field_slots
        from studio.tests.field_definition_fixtures import publish_test_field_version
        field = FieldDefinition.objects.get(key="prepared_direction")
        publish_test_field_version(field.pk, {"label": "新名称", "category": "test", "value_type": "integer"})
        slots = read_preparation_field_slots(self.prep)
        self.assertEqual(slots[0]["field_label"], "Direction")
        self.assertEqual(slots[0]["field_contract"]["field_definition"]["value_type"], "long_text")

    def test_selection_snapshot_requires_explicit_choice_not_only_confirmation(self):
        snapshot = self.selection_snapshot()
        self.assertEqual(snapshot["fields"], [])
        self.assertEqual(snapshot["unselected_fields"], [{"unit_key": "sample", "field_key": "prepared_direction", "required": True}])
        value = self.review(self.create())
        self.assertEqual(self.selection_snapshot()["fields"], [])
        selected = self.select(value)
        snapshot = self.selection_snapshot()
        self.assertEqual(snapshot["unselected_fields"], [])
        field = snapshot["fields"][0]
        self.assertEqual(field["selection_id"], str(selected.pk))
        self.assertEqual(field["field_value_id"], str(value.pk))
        self.assertEqual(field["approval"]["kind"], "human")
        self.assertEqual(field["origin"], {"kind": "manual", "attempt_id": None, "asset_job_id": None})
        self.assertIsNone(self.prep.request_frozen_at)
        compiled = compile_prompt_profile_facts(self.profile, facts=PromptProfileCompilationFacts(
            sources={}, source_language="English", target_language="English", reference_context=None,
            reviewed_values={row["field_key"]: row["text"] for row in snapshot["fields"]}, generation_contributions={},
        ))
        self.assertIn(value.text, compiled.assembly["channels"]["positive_prompt"])

    def test_selection_snapshot_ignores_new_confirmed_text_and_rechecks_revision(self):
        first = self.review(self.create())
        self.select(first)
        before = self.selection_snapshot()
        second = self.review(self.create(revision=2, text="New confirmed but not selected text."))
        after = self.selection_snapshot()
        self.assertEqual(before["fields"], after["fields"])
        with self.assertRaises(ValidationError):
            self.selection_snapshot(expected_preparation_revision=before["preparation_revision"])
        self.select(second, replace=True, expected_selection_revision=1)
        self.assertEqual(self.selection_snapshot()["fields"][0]["field_value_id"], str(second.pk))

    def test_selection_snapshot_keeps_original_preparation_and_approval_when_reused(self):
        value = self.review(self.create())
        source = self.prep
        self.prep = self.prepared()
        self.select(value)
        snapshot = self.selection_snapshot()
        self.assertEqual(snapshot["fields"][0]["source_preparation_id"], str(source.pk))
        self.assertEqual(snapshot["fields"][0]["approval"]["reviewed_at"], value.reviewed_at.isoformat())
        self.assertFalse(self.prep.field_values.exists())
        with self.assertRaises(PermissionDenied):
            self.selection_snapshot(actor=self.member)

    def test_manual_text_confirm_select_are_explicit_and_replays_do_not_duplicate(self):
        value = self.create()
        self.assertEqual(value.status, "pending")
        self.assertIsNone(value.origin_attempt_id)
        self.assertIsNone(value.origin_asset_job_id)
        replay = self.create(expected_preparation_revision=1)
        self.assertEqual(replay.pk, value.pk)
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.revision, 2)
        value = self.review(value)
        self.assertEqual(value.approval_kind, "human")
        self.assertFalse(self.prep.field_selections.exists())
        self.assertEqual(self.review(value, expected_preparation_revision=1).pk, value.pk)
        selection = self.select(value)
        self.assertEqual(self.select(value, expected_preparation_revision=1).pk, selection.pk)
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.revision, 4)
        self.assertIsNone(self.prep.request_frozen_at)

    def test_newer_confirmation_does_not_replace_selection(self):
        first = self.review(self.create())
        selected = self.select(first)
        second = self.review(self.create(revision=2, text="The cube rotates once."))
        selected.refresh_from_db()
        self.assertEqual(selected.field_value_id, first.pk)
        with self.assertRaises(ValidationError):
            self.select(second)
        with self.assertRaises(ValidationError):
            self.select(second, replace=True, expected_selection_revision=2)
        replaced = self.select(second, replace=True, expected_selection_revision=1)
        self.assertEqual(replaced.revision, 2)
        self.assertEqual(replaced.field_value_id, second.pk)

    def test_invalid_or_stale_field_write_does_not_leave_revision_or_text(self):
        for changes in (
            {"unit_key": "other-shot"}, {"field_key": "invented"}, {"text": "x" * 1001},
            {"revision": 2}, {"revision": True}, {"expected_preparation_revision": 9},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.create(**changes)
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.revision, 1)
        self.assertFalse(self.prep.field_values.exists())
        self.create()
        with self.assertRaises(ValidationError):
            self.create(text="Different text under same revision.")

    def test_wrong_sha_rejection_and_frozen_preparation_are_not_silent(self):
        value = self.create()
        with self.assertRaises(ValidationError):
            self.review(value, expected_source_sha256="f" * 64)
        rejected = self.review(value, approve=False)
        self.assertEqual(rejected.status, "rejected")
        self.assertIsNone(rejected.approval_kind)
        with self.assertRaises(ValidationError):
            self.review(value)
        self.prep.refresh_from_db()
        self.prep.status = "ready"
        self.prep.request_sha256 = "d" * 64
        self.prep.request_frozen_at = timezone.now()
        self.prep.save()
        with self.assertRaises(ValidationError):
            self.create(revision=2)

    def test_reuse_requires_explicit_authorized_source_and_exact_facts(self):
        value = self.review(self.create())
        first = self.prep
        self.prep = self.prepared()
        selected = self.select(value)
        self.assertEqual(selected.field_value_id, value.pk)
        self.assertEqual(value.preparation_id, first.pk)  # no invented child call
        self.assertFalse(self.prep.field_values.exists())
        self.prep = self.prepared(source_text="Changed source facts.")
        with self.assertRaises(ValidationError):
            self.select(value)
        with self.assertRaises(PermissionDenied):
            self.select(value, actor=self.member)
        with self.assertRaises(ExecutionPreparation.DoesNotExist):
            self.select(value, source_preparation_id=uuid.uuid4())

    def test_permissions_and_transaction_rollback(self):
        with self.assertRaises(PermissionDenied):
            self.create(actor=self.member)
        self.actor.is_active = False
        with self.assertRaises(PermissionDenied):
            self.create()
        self.actor.is_active = True
        with patch("studio.services.execution_preparation_fields._touch", side_effect=RuntimeError("rollback")):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertFalse(self.prep.field_values.exists())
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), 1)  # fixture only; no dispatch

    def test_idempotent_replays_after_freeze_are_reads_not_new_mutations(self):
        value = self.review(self.create())
        selection = self.select(value)
        self.prep.refresh_from_db()
        revision = self.prep.revision
        self.prep.status = "ready"
        self.prep.request_sha256 = "e" * 64
        self.prep.request_frozen_at = timezone.now()
        self.prep.save()
        self.assertEqual(self.create(expected_preparation_revision=1).pk, value.pk)
        self.assertEqual(self.review(value, expected_preparation_revision=1).pk, value.pk)
        self.assertEqual(self.select(value, expected_preparation_revision=1).pk, selection.pk)
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.revision, revision)
        with self.assertRaises(ValidationError):
            self.create(revision=2)

    def test_asset_owner_is_authorized_but_not_other_tenants_or_foreign_reuse(self):
        self.version.prompt_mode, self.version.prompt_profile_version = "profile", self.profile
        self.version.save(update_fields=("prompt_mode", "prompt_profile_version"))
        def job(owner):
            return AssetGenerationJob.objects.create(
                owner=owner, model_deployment=self.deployment, execution_instance_version=self.version, job_type="prompt", media_type="script",
                prompt="Fixture", operation="prompt.draft", idempotency_key=str(uuid.uuid4()), request_snapshot_sha256=json_contract({}).sha256,
            )
        owned = self.prepared(asset_job=job(self.member))
        self.prep = owned
        value = self.create(actor=self.member)
        value = self.review(value, actor=self.member)
        self.assertEqual(self.select(value, actor=self.member).field_value_id, value.pk)
        self.prep = self.prepared(asset_job=job(self.actor))
        with self.assertRaises(PermissionDenied):
            self.create(actor=self.member)
        foreign = self.review(self.create())
        self.prep = owned
        with self.assertRaises(PermissionDenied):
            self.select(foreign, actor=self.member, replace=True, expected_selection_revision=1)
