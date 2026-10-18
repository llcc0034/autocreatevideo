"""Isolated real RunPlan/Profile/field rows; no provider calls or stage wins."""
from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ExecutionFieldSelection, ProductionRecipeVersion, ProductionRun, SeriesProject, StageRun
from studio.services.execution_field_review import (
    apply_preparation_field_policy, field_review_policy_options, production_field_review_contract,
    validate_field_review_choice, verify_field_review_contract,
)
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_fields import create_manual_execution_field, confirm_execution_field
from studio.services.execution_preparations import create_execution_instance_preparation, read_execution_preparation_detail
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.tests import test_execution_profile_preparation as fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionFieldReviewTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionProfilePreparationTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ExecutionProfilePreparationTests.write_contract)
    input = staticmethod(fixtures.ExecutionProfilePreparationTests.input)
    bind = fixtures.ExecutionProfilePreparationTests.bind

    def setUp(self):
        fixtures.ExecutionProfilePreparationTests.setUp(self)
        option = field_review_policy_options()[0]
        self.choice = {"mode": "policy", "policy": {key: option[key] for key in ("key", "version", "implementation_sha256")}}
        self.project = SeriesProject.objects.create(owner=self.actor, title="Field review", **PROJECT_CONFIG)

    def stage(self, *, source="recipe", interaction="unattended", choice=None, authorized=True):
        recipe = None
        if source == "recipe":
            recipe = ProductionRecipeVersion(recipe_key="policy-fixture", version=ProductionRecipeVersion.objects.count() + 1,
                name="Field policy isolated fixture", status="published")
            # The fixture tests field authorization, not full recipe publishing.
            assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": interaction, "stages": [{
                "key": "media_generation", "mode": "existing_pipeline", "approval": "required",
                "field_review": deepcopy(self.choice if choice is None else choice),
            }]}, schema_version=3)
            recipe.save()
        run = ProductionRun(owner=self.actor, project=self.project, plan_source=source, recipe_version=recipe,
            status="running", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        assign_json_contract(run, "input_snapshot", {"schema_version": 1})
        assign_json_contract(run, "config_snapshot", {"schema_version": 1})
        run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        if source == "recipe":
            from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
            freeze_recipe_request_fixture(run, authorized=authorized)
        return StageRun.objects.create(production_run=run, stage_key="media_generation", ordinal=1 if source == "recipe" else 13, mode="existing_pipeline",
            status="running", approval_required=True, idempotency_key=str(uuid4()))

    def prepare(self, *, stage=None, supplied_review=None, owner_ref=None):
        stage = self.stage() if stage is None and owner_ref is None else stage
        request = fixtures.request_for(self)
        def resolve(definition, owner):
            review = supplied_review if supplied_review is not None else production_field_review_contract(owner)
            return resolve_execution_request_facts(definition, request, owner=self.actor, unit_key="asset-unit", max_media_bytes=65536,
                review_contract=review, parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No source lock"))
        self.prep = create_execution_instance_preparation(owner_ref=owner_ref or ("stage_run_id", stage.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256, unit_key="asset-unit", request_key="candidate-1",
            intent=json_contract(request, max_bytes=execution_request_max_bytes()), resolve_facts=resolve, actor=self.actor)
        return self.prep

    def field(self, *, revision=1, text="A courier opens a blue parcel."):
        self.prep.refresh_from_db()
        value = create_manual_execution_field(preparation_id=self.prep.pk, unit_key="asset-unit", field_key="composite_direction",
            text=text, revision=revision, expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.prep.refresh_from_db()
        return value

    def apply(self, **changes):
        self.prep.refresh_from_db()
        return apply_preparation_field_policy(**dict(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision,
            actor=self.actor, **changes))

    def test_policy_identity_and_choice_are_closed_and_not_a_stage_policy(self):
        self.assertEqual(validate_field_review_choice(self.choice, interaction_mode="unattended"), self.choice)
        self.assertEqual(validate_field_review_choice({"mode": "manual"}, interaction_mode="assisted"), {"mode": "manual"})
        for choice in ({"mode": "policy"}, {"mode": "auto"}, {**self.choice, "approve": True},
                {"mode": "policy", "policy": {**self.choice["policy"], "version": True}},
                {"mode": "policy", "policy": {**self.choice["policy"], "implementation_sha256": "not-a-sha"}},
                {"mode": "policy", "policy": {**self.choice["policy"], "key": "stage.auto"}}):
            with self.subTest(choice=choice), self.assertRaises(ValidationError):
                validate_field_review_choice(choice, interaction_mode="unattended")
        with self.assertRaises(ValidationError):
            validate_field_review_choice(self.choice, interaction_mode="assisted")
        for review in ({"schema_version": True, "mode": "manual"}, {"schema_version": 1, **self.choice}):
            with self.assertRaises(ValidationError):
                verify_field_review_contract(review)

    def test_pending_policy_review_uses_original_field_selection_and_does_not_complete_stage(self):
        self.prepare()
        field = self.field()
        old_revision = self.prep.revision
        revision = self.apply()
        self.assertEqual(revision, old_revision + 1)
        field.refresh_from_db()
        self.assertEqual((field.status, field.approval_kind, field.reviewed_by_id), ("confirmed", "policy", None))
        self.assertEqual(field.approval_policy_sha256, json_contract(self.choice["policy"]).sha256)
        self.assertEqual(self.prep.field_selections.get().field_value_id, field.pk)
        approved = field.reviewed_at
        self.assertEqual(self.apply(), revision)
        field.refresh_from_db()
        self.assertEqual(field.reviewed_at, approved)
        self.prep.refresh_from_db()
        result = evaluate_preparation_fields(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.assertEqual(read_execution_preparation_detail(result).evaluation["outstanding_fields"], [])
        self.assertIsNone(result.request_frozen_at)
        self.assertFalse(result.generation_attempts.exists())
        self.assertEqual(result.stage_run.status, "running")
        self.assertFalse(result.stage_run.approval_decisions.exists())

    def test_evaluation_invokes_explicit_policy_but_missing_fields_still_wait(self):
        self.prepare()
        evaluated = evaluate_preparation_fields(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.assertEqual(evaluated.status, "awaiting_input")
        self.assertFalse(self.prep.field_selections.exists())
        field = self.field()
        self.prep.refresh_from_db()
        evaluated = evaluate_preparation_fields(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.assertEqual(evaluated.status, "preparing")
        field.refresh_from_db()
        self.assertEqual(field.approval_kind, "policy")

    def test_manual_roots_and_assisted_recipe_do_not_auto_approve(self):
        for stage in (self.stage(source="manual"), self.stage(interaction="assisted", choice={"mode": "manual"})):
            self.prepare(stage=stage)
            field = self.field()
            revision = self.prep.revision
            self.assertEqual(self.apply(), revision)
            field.refresh_from_db()
            self.assertEqual(field.status, "pending")
            self.assertFalse(self.prep.field_selections.exists())
        review = production_field_review_contract(self.stage())
        with self.assertRaises(ValidationError):
            self.prepare(owner_ref=("asset_job_id", self.job.pk), supplied_review=review)
        self.assertFalse(self.job.execution_preparations.exists())

    def test_unauthorized_or_wrong_plan_policy_cannot_allocate_preparation(self):
        stage = self.stage(authorized=False)
        with self.assertRaises(ValidationError):
            self.prepare(stage=stage)
        other = self.stage()
        review = production_field_review_contract(other)
        with self.assertRaises(ValidationError):
            self.prepare(stage=self.stage(), supplied_review=review)
        with self.assertRaises(ValidationError):
            self.prepare(stage=other, supplied_review=json_contract({"schema_version": 1, "mode": "manual"}))

    def test_policy_never_picks_latest_or_replaces_human_selection(self):
        self.prepare()
        first = self.field()
        second = self.field(revision=2, text="Another candidate.")
        with self.assertRaises(ValidationError) as caught:
            self.apply()
        self.assertEqual(caught.exception.code, "field_selection_conflict")
        self.assertFalse(self.prep.field_selections.exists())
        self.prep.refresh_from_db()
        confirm_execution_field(preparation_id=self.prep.pk, field_value_id=first.pk,
            expected_preparation_revision=self.prep.revision, expected_text_sha256=first.text_sha256,
            expected_source_sha256=first.source_sha256, select_for_execution=True, replace=False,
            expected_selection_revision=None, actor=self.actor)
        self.apply()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.approval_kind, second.status), ("human", "pending"))
        self.assertEqual(self.prep.field_selections.get().field_value_id, first.pk)

    def test_code_change_allows_new_approval_without_rewriting_frozen_authorization(self):
        self.prepare()
        field = self.field()
        before = read_execution_preparation_detail(self.prep).review_contract
        with patch("studio.services.execution_field_review._POLICY_IDENTITY", {**self.choice["policy"], "implementation_sha256": "0" * 64}):
            self.assertEqual(validate_field_review_choice(self.choice, interaction_mode="unattended"), self.choice)
            self.assertEqual(production_field_review_contract(self.prep.stage_run).value, before)
            self.apply()
            # New approval still refers to the original authorization. It does
            # not claim the frozen implementation hash is the running code.
            self.assertEqual(read_execution_preparation_detail(self.prep).review_contract, before)
        field.refresh_from_db()
        self.assertEqual((field.status, field.approval_kind, field.reviewed_by_id), ("confirmed", "policy", None))
        self.assertEqual(field.approval_policy_sha256, json_contract(self.choice["policy"]).sha256)
        self.assertEqual(self.prep.field_selections.get().field_value_id, field.pk)

    def test_frozen_recipe_accepts_new_preparation_after_code_change(self):
        from studio.services.production_recipe_contracts import validate_recipe_stage_policies
        from studio.services.production_run_plans import get_run_execution_plan
        stage = self.stage()
        plan = get_run_execution_plan(stage.production_run)
        before = deepcopy(plan.value)
        # The same validator is used by authoring/publication as production.
        declaration = {"key": "media_generation", "mode": "existing_pipeline", "approval": "auto", "field_review": self.choice}
        with patch("studio.services.execution_field_review._POLICY_IDENTITY", {**self.choice["policy"], "implementation_sha256": "0" * 64}):
            validate_recipe_stage_policies(declaration, interaction_mode="unattended")
            self.prepare(stage=stage)
            review = read_execution_preparation_detail(self.prep).review_contract
            self.assertEqual(review["policy"], self.choice["policy"])
            self.field()
            self.apply()
        after = get_run_execution_plan(stage.production_run)
        self.assertEqual((after.value, after.sha256), (before, plan.sha256))

    def test_unknown_business_policy_does_not_authorize_new_approval(self):
        self.prepare()
        field = self.field()
        before = read_execution_preparation_detail(self.prep).review_contract
        for change in ({"key": "execution_fields.other"}, {"version": 2}):
            with self.subTest(change=change), patch("studio.services.execution_field_review._POLICY_IDENTITY", {**self.choice["policy"], **change}):
                with self.assertRaises(ValidationError) as caught:
                    self.apply()
                self.assertEqual(caught.exception.code, "field_review_stale")
                self.assertEqual(read_execution_preparation_detail(self.prep).review_contract, before)
        field.refresh_from_db()
        self.assertEqual(field.status, "pending")
        self.assertFalse(self.prep.field_selections.exists())

    def test_code_change_does_not_bypass_current_generic_value_validation(self):
        self.prepare()
        field = self.field()
        with patch("studio.services.execution_field_review._POLICY_IDENTITY", {**self.choice["policy"], "implementation_sha256": "0" * 64}), \
                patch("studio.services.execution_field_review._verify_value", side_effect=ValidationError("invalid source")):
            with self.assertRaises(ValidationError):
                self.apply()
        field.refresh_from_db()
        self.assertEqual(field.status, "pending")
        self.assertFalse(self.prep.field_selections.exists())

    def test_approved_selection_keeps_original_evidence_after_policy_deployment(self):
        self.prepare()
        field = self.field()
        self.apply()
        field.refresh_from_db()
        before = (field.approval_policy_sha256, field.reviewed_at)
        with patch("studio.services.execution_field_review._POLICY_IDENTITY", {**self.choice["policy"], "implementation_sha256": "0" * 64}):
            self.apply()
        field.refresh_from_db()
        self.assertEqual((field.approval_policy_sha256, field.reviewed_at), before)
        self.assertEqual(self.prep.field_selections.count(), 1)

    def test_invalid_text_or_failed_selection_rolls_back_approval(self):
        self.prepare()
        field = self.field()
        with patch("studio.services.execution_field_review._verify_value", side_effect=ValidationError("invalid result")):
            with self.assertRaises(ValidationError):
                self.apply()
        with patch.object(ExecutionFieldSelection.objects, "create", side_effect=RuntimeError("selection write failed")):
            with self.assertRaises(RuntimeError):
                self.apply()
        field.refresh_from_db()
        self.assertEqual(field.status, "pending")
        self.assertIsNone(field.approval_kind)
        self.assertFalse(self.prep.field_selections.exists())

    def test_permissions_cancel_and_stale_revision_do_not_grant_approval(self):
        from django.contrib.auth import get_user_model
        self.prepare()
        self.field()
        outsider = get_user_model().objects.create_user(username="policy-outsider")
        with self.assertRaises(PermissionDenied):
            apply_preparation_field_policy(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=outsider)
        with self.assertRaises(ValidationError):
            apply_preparation_field_policy(preparation_id=self.prep.pk, expected_preparation_revision=1, actor=self.actor)
        run = self.prep.stage_run.production_run
        run.status = "cancelled"
        run.save(update_fields=("status",))
        with self.assertRaises(ValidationError):
            self.apply()
        self.assertFalse(self.prep.field_selections.exists())

    def test_candidate_query_aggregates_identity_without_loading_all_revision_texts(self):
        self.prepare()
        self.field()
        with CaptureQueriesContext(connection) as queries:
            self.apply()
        aggregates = [row["sql"] for row in queries if 'COUNT(' in row["sql"] and 'studio_executionfieldvalue' in row["sql"]]
        self.assertEqual(len(aggregates), 1)
        self.assertNotIn('"text"', aggregates[0])
        self.assertIn('CAST(', aggregates[0])
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                plan = cursor.execute("EXPLAIN QUERY PLAN " + aggregates[0]).fetchall()
            self.assertIn("SEARCH", str(plan))

    def test_field_source_and_text_are_validated_even_if_revision_was_inserted_directly(self):
        from hashlib import sha256
        from studio.models import ExecutionFieldValue
        from studio.services.execution_preparation_fields import _field_contexts
        self.prepare()
        context = _field_contexts(self.prep)[("asset-unit", "composite_direction")]
        text = "A source-mismatched candidate."
        value = ExecutionFieldValue.objects.create(preparation=self.prep, unit_key="asset-unit", field_key="composite_direction",
            revision=1, source_sha256="0" * 64, field_contract_sha256=context["field_contract_sha256"],
            text=text, text_sha256=sha256(text.encode()).hexdigest(), origin_kind="manual", created_by=self.actor, status="pending")
        with self.assertRaises(ValidationError):
            self.apply()
        value.refresh_from_db()
        self.assertEqual(value.status, "pending")
        self.assertFalse(self.prep.field_selections.exists())
