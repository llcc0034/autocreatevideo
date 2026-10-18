"""Isolated original Run receipt -> text/media source binding; no provider I/O."""
from copy import deepcopy
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import GenerationAttempt, ProductionRun, ProductionRunPlan, StageRun
from studio.services.execution_definitions import read_execution_input_contract
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_input_suggestions import preview_execution_input_suggestion
from studio.services.production_project_snapshots import normalize_project_source_confirmations, validate_recipe_project_confirmations
from studio.services.production_recipe_contracts import validate_recipe_input_sources
from studio.services.production_recipe_creation import create_recipe_run_root
from studio.services.production_recipe_preparations import _project_inputs
from studio.services.production_run_plans import get_run_execution_plan
from studio.tests import test_production_recipe_preparations as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class RecipeProjectSnapshotTests(TestCase):
    setUpTestData = classmethod(fixtures.RecipePreparationTests.setUpTestData.__func__)
    profile_instance = fixtures.RecipePreparationTests.profile_instance
    root = fixtures.RecipePreparationTests.root

    def setUp(self):
        fixtures.RecipePreparationTests.setUp(self)
        self.project.logline = "Frozen one-line premise."
        self.project.save(update_fields=("logline",))
        # Only the successor draft gets project scope; published contracts stay immutable.
        for row in self.definition.value["configuration"]["inputs"]:
            row["semantic_contract"]["allowed_scope_kinds"] = ["project"]
        self.profile_instance(project_selector="project.logline")
        self.values = {key: row["value"] for key, row in self.fixture["inputs"].items()}
        self.sources = {**{key: {"kind": "context_field", "selector_key": f"run.{key}",
            "scope": {"kind": "project"}} for key in self.values}, "project.logline": {"kind": "project_snapshot"}}
        self.fixture_run = self.root(source=self.sources)
        self.contract = read_execution_input_contract(self.version.pk)
        self.preview = preview_execution_input_suggestion(self.contract, owner=self.actor, project_id=self.project.pk,
            input_key="project.logline", suggestion_key="project.logline")
        self.confirmations = [{"execution_instance_version_id": str(self.version.pk), "input_key": "project.logline",
            "reference": self.preview["reference"]}]
        self.stages = self.plan.value["recipe"]["definition"]["stages"]

    def create(self, **changes):
        # Existing fixture is a limited script route, not a public deliverable recipe.
        return create_recipe_run_root(**{"user": self.actor, "project": self.project, "recipe": self.fixture_run.recipe_version,
            "script_revision": None, "idempotency_key": "project-snapshot-root", "allow_external_calls": False,
            "config_overrides": {}, **self.values, "episode_ids": [],
            "unattended_confirmed": False, "project_source_confirmations": self.confirmations, **changes})

    def create_isolated_root(self, **changes):
        # Actual root validation/writing, no mocked source or confirmation checks.
        # The limited fixture does not certify complete public Recipe publication.
        return self.create(**changes)

    def test_rule_is_reader_derived_and_rejects_injected_source_or_nested_alias(self):
        validate_recipe_input_sources(self.definition, self.sources, prior_stages=[])
        for bad in ({"kind": "project_snapshot", "reference": "pretend"},
                {"kind": "project_snapshot", "selector": "project.logline"}, {"kind": "literal", "value": "pretend"}):
            with self.subTest(source=bad), self.assertRaises(ValidationError):
                validate_recipe_input_sources(self.definition, {**self.sources, "project.logline": bad}, prior_stages=[])
        with self.assertRaises(ValidationError):
            validate_recipe_input_sources(self.definition, {**self.sources, "idea": {"kind": "project_snapshot"}}, prior_stages=[])

    def test_freezes_once_reuses_for_candidates_and_media_and_preserves_actual_provenance(self):
        run, created = self.create_isolated_root()
        self.assertTrue(created)
        self.project.logline = "Later edit must not replace the confirmed input."
        self.project.save(update_fields=("logline",))
        plan, stage = get_run_execution_plan(run), run.stages.get()
        bound, _ = _project_inputs(self.sources, plan, stage, definition=self.definition)
        resolved = resolve_execution_input_sources(self.definition, bound, owner=self.actor, subject_facts=[],
            max_media_bytes=1024, project_id=self.project.pk, production_run_id=run.pk).value
        self.assertEqual(resolved["logical_values"]["project.logline"], "Frozen one-line premise.")
        provenance = next(row for row in resolved["input_provenance"] if row["input_key"] == "project.logline")
        self.assertEqual(provenance["source_reference"], self.preview["origin"])
        from studio.services.production_recipe_media_preparations import _project_media_inputs
        media, _ = _project_media_inputs(definition=self.definition, route={"input_sources": self.sources},
            stage=stage, plan=plan, scope={"kind": "project", "id": str(self.project.pk)}, records=[])
        self.assertEqual(media, bound)
        repeated, created = self.create_isolated_root()
        self.assertFalse(created)
        self.assertEqual(repeated.pk, run.pk)
        with self.assertRaises(ValidationError):
            self.create_isolated_root(project_source_confirmations=[])
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_original_preparation_freezes_snapshot_and_replay_does_not_confirm_new_content(self):
        run, _ = self.create_isolated_root()
        run.status = "running"
        run.save(update_fields=("status",))
        stage = run.stages.get()
        stage.status = "running"
        stage.save(update_fields=("status",))
        from studio.services.production_recipe_preparations import prepare_recipe_project_stage
        args = {"stage_run_id": stage.pk, "expected_run_plan_sha256": get_run_execution_plan(run).sha256,
            "candidate_number": 1, "actor": self.actor}
        prepared = prepare_recipe_project_stage(**args)
        self.project.logline = "Changed after saving the preparation"
        self.project.save(update_fields=("logline",))
        self.assertEqual(prepare_recipe_project_stage(**args).pk, prepared.pk)
        second = prepare_recipe_project_stage(**{**args, "candidate_number": 2})
        for row in (prepared, second):
            self.assertEqual(row.contract.resolved_inputs["inputs"]["logical_values"]["project.logline"], self.preview["content"])
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_formal_create_serializer_preserves_receipt_and_rejects_manual_mixing(self):
        from studio.platform_serializers import ProductionRunCreateSerializer
        body = {"project_id": str(self.project.pk), "plan_source": "recipe", "recipe_version_id": str(self.fixture_run.recipe_version_id),
            "idempotency_key": "serializer", "project_source_confirmations": self.confirmations}
        serializer = ProductionRunCreateSerializer(data=body)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["project_source_confirmations"], self.confirmations)
        invalid = ProductionRunCreateSerializer(data={**body, "project_source_confirmations": [{**self.confirmations[0], "value": "pretend"}]})
        self.assertFalse(invalid.is_valid())
        invalid = ProductionRunCreateSerializer(data={**body, "plan_source": "manual"})
        self.assertFalse(invalid.is_valid())

    def test_missing_extra_duplicate_tampered_and_wrong_contract_do_not_create_roots(self):
        counts = lambda: (ProductionRun.objects.count(), ProductionRunPlan.objects.count(), StageRun.objects.count())
        before = counts()
        for rows in ([], self.confirmations * 2,
                [{**self.confirmations[0], "reference": self.preview["reference"] + "bad"}],
                [{**self.confirmations[0], "input_key": "idea"}],
                [{**self.confirmations[0], "execution_instance_version_id": str(uuid4())}]):
            with self.subTest(rows=repr(rows)[:50]), self.assertRaises(ValidationError):
                self.create_isolated_root(project_source_confirmations=rows)
            self.assertEqual(counts(), before)

    def test_all_routes_require_confirmations_but_identical_consumers_share_one_receipt(self):
        stages = deepcopy(self.stages)
        stages[0]["routing"]["routes"] *= 2
        validate_recipe_project_confirmations(stages, self.confirmations, owner=self.actor, project_id=self.project.pk)
        stages[0]["routing"]["routes"][1]["expected_definition_sha256"] = "0" * 64
        with self.assertRaisesMessage(ValidationError, "合同已变化"):
            validate_recipe_project_confirmations(stages, self.confirmations, owner=self.actor, project_id=self.project.pk)
        for rows in ({}, [True], [{**self.confirmations[0], "value": "injection"}]):
            with self.assertRaises(ValidationError): normalize_project_source_confirmations(rows)

    def test_preview_owner_and_project_must_still_match_at_creation_and_new_preparation(self):
        other = get_user_model().objects.create_user(username="recipe-snapshot-other")
        with self.assertRaises(ValidationError):
            validate_recipe_project_confirmations(self.stages, self.confirmations, owner=other, project_id=self.project.pk)
        with self.assertRaises(ValidationError):
            validate_recipe_project_confirmations(self.stages, self.confirmations, owner=self.actor, project_id=uuid4())
        run, _ = self.create_isolated_root()
        plan = get_run_execution_plan(run)
        bound, _ = _project_inputs(self.sources, plan, run.stages.get(), definition=self.definition)
        self.project.owner = other
        self.project.save(update_fields=("owner",))
        with self.assertRaises(ValidationError):
            resolve_execution_input_sources(self.definition, bound, owner=self.actor, subject_facts=[],
                max_media_bytes=1024, project_id=self.project.pk)
