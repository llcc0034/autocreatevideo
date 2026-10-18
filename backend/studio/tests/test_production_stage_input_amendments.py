"""Real frozen run -> explicit amendment -> same preview and preparation."""
from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (ApprovalDecision, CharacterBible, ExecutionPreparation, GenerationAttempt,
    ProductionRecipeVersion, ProductionRun, ProductionStageInputAmendment, Shot, ShotCharacterRelation, StageRun)
from studio.services.configuration import resolve_configuration
from studio.services.json_contracts import assign_json_contract
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_runs import (_formal_snapshot, _save_stage_snapshot,
    _freeze_stage_config, _freeze_stage_input, _poll_media_stage, advance_production_run, retry_production_run)
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.production_stage_input_amendments import amend_stage_inputs, POLICY, SOURCE_POLICY
from studio.services.production_recipe_preparations import recipe_stage_context
from studio.services.production_recipe_media_preparations import preview_recipe_media_mapping
from studio.services.security_boundaries import boundary_int
from studio.tests.test_recipe_structure_preparation import RecipeStructurePreparationTests, field


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class StageInputAmendmentTests(TestCase):
    reference_audio_enabled = True
    write = RecipeStructurePreparationTests.write
    input = staticmethod(RecipeStructurePreparationTests.input)
    approve = RecipeStructurePreparationTests.approve
    prepare = RecipeStructurePreparationTests.prepare

    def create_route(self, route):
        recipe = ProductionRecipeVersion(recipe_key=str(uuid4()), version=1, name="Amendment fixture", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "bible_review", "mode": "review", "approval": "required"},
            {"key": "storyboard_review", "mode": "review", "approval": "required"},
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "required", "routing": {
                "mode": "fixed", "variant_count": 2, "routes": [route], "evaluator": None}}]}, schema_version=3)
        recipe.save()
        self.run = ProductionRun.objects.create(owner=self.actor, project=self.project, recipe_version=recipe, plan_source="recipe",
            status="running", current_stage_key="media_generation", idempotency_key=str(uuid4()), request_sha256="b"*64)
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(), max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 1}); self.run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "episodes", "episode_ids": [str(self.episode.pk)]},
            delivery={"kind": "episode_masters"}, initial_content=None, stage_catalog_key=catalog.value["key"],
            expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        self.plan = get_run_execution_plan(self.run)
        bible = StageRun.objects.create(production_run=self.run, stage_key="bible_review", mode="review", status="succeeded",
            ordinal=1, idempotency_key=str(uuid4()))
        _save_stage_snapshot(bible, "output_snapshot", _formal_snapshot(self.run, bible, "bible"))
        ApprovalDecision.objects.create(stage_run=bible, revision=1, decision="approve", actor="human",
            decided_by=self.actor, output_snapshot_sha256=bible.output_snapshot_sha256)
        self.review = StageRun.objects.create(production_run=self.run, stage_key="storyboard_review", mode="review", status="succeeded",
            ordinal=2, idempotency_key=str(uuid4()))
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", mode="existing_pipeline", status="running",
            ordinal=3, idempotency_key=str(uuid4()))
        self.approve()

    def setUp(self):
        RecipeStructurePreparationTests.setUp(self)
        actor = CharacterBible.objects.create(project=self.project, slug="speaker", stable_speaker_id="S1", name="Courier", voice_profile="Warm")
        ShotCharacterRelation.objects.bulk_create([
            ShotCharacterRelation(shot=self.shot, character=actor, role=role, position=position)
            for role, position in (("visible", 0), ("speaker", 0), ("speaker", 1))
        ])
        # The first line intentionally lacks voiceover to test the explicit amendment, not an inferred default.
        self.shot.dialogue = [dict(speaker_id="S1", text="Hello", delivery="calm", start_ms=0, end_ms=100),
            dict(speaker_id="S1", text="Aside", delivery="soft", voiceover=True, start_ms=100, end_ms=500)]
        self.shot.include_audio = True
        self.shot.save(update_fields=("dialogue", "include_audio"))
        route = deepcopy(self.plan.value["recipe"]["definition"]["stages"][-1]["routing"]["routes"][0])
        fields = route["input_sources"]["units"]["template"]["fields"]["ordered_units"]["items"][0]["fields"]
        fields["dialogue"] = field("shot.dialogue_with_speakers")
        self.create_route(route)

    def amend(self, action="preview", sha=None, **changes):
        return amend_stage_inputs(**{ "stage_run_id": self.stage.pk, "expected_run_plan_sha256": self.plan.sha256, "policy": POLICY,
            "action": action, "expected_amendment_sha256": sha, "actor": self.actor, **changes})

    def preview(self):
        return preview_recipe_media_mapping(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
            shot_id=str(self.shot.pk), candidate_number=1, actor=self.actor)

    def test_original_missing_field_then_amend_preview_and_preparation_share_explicit_rule(self):
        with self.assertRaisesMessage(ValidationError, "voiceover"):
            self.preview()
        approvals = list(ApprovalDecision.objects.values())
        original = deepcopy(self.shot.dialogue)
        proposed = self.amend()
        self.assertEqual(proposed["status"], "proposed")
        self.assertFalse(ProductionStageInputAmendment.objects.exists())
        applied = self.amend("apply", proposed["sha256"])
        self.assertEqual(applied["status"], "applied")
        result = self.preview()
        units = next(row for row in result["value_inputs"] if row["key"] == "units")
        self.assertEqual([row["voiceover"] for row in units["value"]["ordered_units"][0]["dialogue"]], [False, True])
        self.assertTrue(any(row.get("resolution") == "explicit_if_missing" for row in units["provenance"]["leaves"]))
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run=self.stage).exists())
        preparation = self.prepare(self.shot)
        identity = {"id": applied["id"], "sha256": applied["sha256"]}
        self.assertEqual(preparation.contract.intent["request"]["requested"]["input_amendment"], identity)
        self.assertEqual(self.prepare(self.shot).pk, preparation.pk)
        self.assertEqual(self.amend("apply", proposed["sha256"])["id"], applied["id"])
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.dialogue, original)
        self.assertEqual(approvals, list(ApprovalDecision.objects.values()))
        self.assertEqual(get_run_execution_plan(self.run), self.plan)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_permissions_stale_confirmation_and_lifecycle_reject_without_writes(self):
        proposal = self.amend()
        with self.assertRaises(PermissionDenied): self.amend(actor=self.other)
        with self.assertRaises(ValidationError): self.amend(expected_run_plan_sha256="a"*64)
        with self.assertRaises(ValidationError): self.amend("apply", "a"*64)
        for status in ("succeeded", "cancelled", "awaiting_approval"):
            self.stage.status = status; self.stage.save(update_fields=("status",))
            with self.assertRaises(ValidationError): self.amend("apply", proposal["sha256"])
        self.assertFalse(ProductionStageInputAmendment.objects.exists())

    def test_existing_preparation_cannot_be_amended(self):
        # A preparation on any unit, including one with no dialogue, freezes the stage policy.
        self.prepare(self.second)
        with self.assertRaisesMessage(ValidationError, "已有执行准备"):
            self.amend()
        self.assertFalse(ProductionStageInputAmendment.objects.exists())

    def test_api_exact_identity_unknown_fields_and_audit_immutability(self):
        client = APIClient(); client.force_authenticate(self.actor)
        url = f"/api/v1/production-runs/{self.run.pk}/stage-input-amendment/"
        payload = {"stage_run_id": str(self.stage.pk), "expected_run_plan_sha256": self.plan.sha256, "action": "preview", "policy": POLICY}
        proposed = client.post(url, payload, format="json")
        self.assertEqual(proposed.status_code, 200, proposed.data)
        self.assertEqual(proposed["Cache-Control"], "private, no-store")
        for change in ({"stage_run_id": str(uuid4())}, {"sources": {}}, {"action": "apply"}):
            self.assertEqual(client.post(url, {**payload, **change}, format="json").status_code, 400)
        applied = client.post(url, {**payload, "action": "apply", "expected_amendment_sha256": proposed.data["sha256"]}, format="json")
        self.assertEqual(applied.status_code, 200, applied.data)
        record = ProductionStageInputAmendment.objects.get()
        record.evidence["policy"] = "silent-default"
        with self.assertRaises(ValidationError): record.save()
        with self.assertRaises(ValidationError): record.delete()
        with self.assertRaises(ValidationError): ProductionStageInputAmendment.objects.all().delete()
        with self.assertRaises(ValidationError): ProductionStageInputAmendment.objects.update(evidence={})
        self.assertEqual(recipe_stage_context(self.stage)[0], self.plan)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_raw_context_mapping_and_non_dialogue_settings_are_untouched(self):
        proposal = self.amend()
        self.amend("apply", proposal["sha256"])
        _, effective = recipe_stage_context(self.stage)
        original = self.plan.value["recipe"]["definition"]["stages"][-1]
        self.assertEqual(effective["routing"]["routes"][0]["parameter_policies"], original["routing"]["routes"][0]["parameter_policies"])
        self.assertEqual(effective["routing"]["routes"][0]["input_sources"]["styles"], original["routing"]["routes"][0]["input_sources"]["styles"])
        self.assertEqual(len(proposal["evidence"]["changes"]), 1)

    def test_blocked_scheduler_amendment_retry_preserves_snapshots_and_uses_effective_inputs(self):
        from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
        freeze_recipe_request_fixture(self.run, authorized=False)
        # Exercise the actual scheduler failure, not just the mapping preview.
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "blocked", result.blocked_reason)
        self.assertIn("voiceover", result.blocked_reason)
        self.stage.refresh_from_db()
        original_config = deepcopy(self.stage.config_snapshot)
        original_input = deepcopy(self.stage.input_snapshot)
        approvals = list(ApprovalDecision.objects.values())
        self.assertFalse(self.stage.execution_preparations.exists())
        applied = self.amend("apply", self.amend()["sha256"])
        # Only the spending boundary is stubbed; retry, both snapshot checks,
        # scope validation and all four persisted preparations remain real.
        def no_dispatch(**kwargs):
            return ExecutionPreparation.objects.get(pk=kwargs["preparation_id"])
        with patch("studio.services.production_recipe_media_stages.advance_execution_preparation",
                side_effect=no_dispatch) as dispatch:
            result = retry_production_run(result, user=self.actor, expected_stage_id=self.stage.pk, expected_attempt_ids=[])
            self.assertEqual(result.status, "running", result.blocked_reason)
            self.assertEqual(dispatch.call_count, 4)
            ids = set(self.stage.execution_preparations.values_list("id", flat=True))
            self.assertEqual(len(ids), 4)
            advance_production_run(result)
            self.assertEqual(set(self.stage.execution_preparations.values_list("id", flat=True)), ids)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.config_snapshot, original_config)
        self.assertEqual(self.stage.input_snapshot, original_input)
        for prep in self.stage.execution_preparations.filter(unit_key=f"shot:{self.shot.pk}"):
            self.assertEqual(prep.contract.intent["request"]["requested"]["input_amendment"],
                {"id": applied["id"], "sha256": applied["sha256"]})
            dialogue = prep.contract.resolved_inputs["inputs"]["logical_values"]["units"]["ordered_units"][0]["dialogue"]
            self.assertEqual([row["voiceover"] for row in dialogue], [False, True])
        self.assertEqual(list(ApprovalDecision.objects.values()), approvals)
        self.assertEqual(get_run_execution_plan(self.run), self.plan)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_amendment_before_first_freeze_keeps_the_same_base_identity(self):
        from studio.services.production_recipe_media_stages import recipe_media_stage_contract, recipe_media_input_contract
        base_config = recipe_media_stage_contract(self.stage)
        base_input = recipe_media_input_contract(self.stage)
        self.amend("apply", self.amend()["sha256"])
        _freeze_stage_config(self.run, self.stage)
        _freeze_stage_input(self.run, self.stage)
        self.assertEqual(self.stage.config_snapshot, base_config.value)
        self.assertEqual(self.stage.input_snapshot, base_input.value)

    def test_amendment_does_not_authorize_snapshot_or_target_drift(self):
        _freeze_stage_config(self.run, self.stage)
        _freeze_stage_input(self.run, self.stage)
        self.amend("apply", self.amend()["sha256"])
        config = deepcopy(self.stage.config_snapshot)
        for key in ("stage_declaration_sha256", "run_plan_sha256"):
            _save_stage_snapshot(self.stage, "config_snapshot", {**config, key: "0" * 64})
            with self.subTest(key=key), self.assertRaisesMessage(ValidationError, "冻结实例路线"):
                _freeze_stage_config(self.run, self.stage)
        _save_stage_snapshot(self.stage, "config_snapshot", config)
        Shot.objects.create(episode=self.episode, number=3, media_type="video", duration_ms=1000, include_audio=False)
        with self.assertRaisesMessage(ValidationError, "制作对象"):
            _freeze_stage_input(self.run, self.stage)
        with self.assertRaisesMessage(ValidationError, "制作对象范围"):
            _poll_media_stage(self.run, self.stage)
        self.assertFalse(self.stage.execution_preparations.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_freeze_still_verifies_amendment_evidence(self):
        from studio.services.production_recipe_media_stages import recipe_media_stage_contract
        self.amend("apply", self.amend()["sha256"])
        row = ProductionStageInputAmendment.objects.get(stage_run=self.stage)
        row.evidence_sha256 = "0" * 64
        with patch("studio.services.production_stage_input_amendments.ProductionStageInputAmendment.objects.filter") as lookup:
            lookup.return_value.first.return_value = row
            with self.assertRaises(ValidationError):
                recipe_media_stage_contract(self.stage)

    def reference_source_changes(self):
        route = self.plan.value["recipe"]["definition"]["stages"][-1]["routing"]["routes"][0]
        source = deepcopy(route["input_sources"]["units"])
        dialogue = source["template"]["fields"]["ordered_units"]["items"][0]["fields"]
        dialogue["dialogue"] = {"kind": "each", "source": field("shot.dialogue_with_speakers"),
            "item": {"kind": "object", "fields": {name: {"kind": "item_field", "path": [name],
                **({"if_missing": False} if name == "voiceover" else {"if_missing": ""} if name == "delivery" else {})}
                for name in ("speaker_id", "text", "delivery", "subject", "voice_profile", "voiceover", "start_ms", "end_ms")}}}
        return [{"route_index": 0, "input_key": "units", "source": source},
            {"route_index": 0, "input_key": "styles", "source": deepcopy(route["input_sources"]["styles"])}]

    def broken_reference_route(self):
        # The same missing grouping and optional dialogue facts as the real blocked Run.
        self.shot.dialogue[0].pop("delivery")
        self.shot.save(update_fields=("dialogue",))
        route = deepcopy(self.plan.value["recipe"]["definition"]["stages"][-1]["routing"]["routes"][0])
        route["input_sources"]["styles"]["template"] = route["input_sources"]["styles"]["template"]["value"]
        route["input_sources"]["units"]["template"]["fields"]["ordered_units"]["items"][0]["fields"]["dialogue"] = field("shot.dialogue")
        self.create_route(route)
        changes = self.reference_source_changes()
        changes[1]["source"]["template"] = {"kind": "unit_map", "value": changes[1]["source"]["template"]}
        return changes

    def test_explicit_sources_same_run_retry_keeps_frozen_plan_and_all_reviews(self):
        from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
        changes = self.broken_reference_route()
        freeze_recipe_request_fixture(self.run, authorized=False)
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "blocked")
        self.assertFalse(self.stage.execution_preparations.exists())
        self.stage.refresh_from_db()
        original_config, original_input = deepcopy(self.stage.config_snapshot), deepcopy(self.stage.input_snapshot)
        approvals, dialogue = list(ApprovalDecision.objects.values()), deepcopy(self.shot.dialogue)
        proposal = self.amend(policy=SOURCE_POLICY, source_changes=changes)
        self.assertFalse(ProductionStageInputAmendment.objects.exists())
        applied = self.amend("apply", proposal["sha256"], policy=SOURCE_POLICY, source_changes=changes)
        self.assertEqual(len(applied["evidence"]["changes"]), 2)
        for shot in (self.shot, self.second):
            preview_recipe_media_mapping(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
                shot_id=str(shot.pk), candidate_number=1, actor=self.actor)
        with patch("studio.services.production_recipe_media_stages.advance_execution_preparation",
                side_effect=lambda **kw: ExecutionPreparation.objects.get(pk=kw["preparation_id"])):
            retried = retry_production_run(result, user=self.actor, expected_stage_id=self.stage.pk, expected_attempt_ids=[])
        self.assertEqual(retried.status, "running", retried.blocked_reason)
        self.assertEqual(self.stage.execution_preparations.count(), 4)
        for prep in self.stage.execution_preparations.all():
            facts = prep.contract.resolved_inputs
            self.assertEqual(set(facts["inputs"]["logical_values"]["styles"]), {prep.unit_key})
            self.assertEqual(prep.contract.intent["request"]["requested"]["input_amendment"]["id"], applied["id"])
            if prep.unit_key == f"shot:{self.shot.pk}":
                lines = facts["inputs"]["logical_values"]["units"]["ordered_units"][0]["dialogue"]
                self.assertEqual(lines[0]["delivery"], "")
                self.assertEqual(lines[1]["delivery"], "soft")
                self.assertEqual([row["voiceover"] for row in lines], [False, True])
                self.assertEqual(lines[0]["subject"], "Courier")
        self.stage.refresh_from_db(); self.shot.refresh_from_db()
        self.assertEqual(self.stage.config_snapshot, original_config)
        self.assertEqual(self.stage.input_snapshot, original_input)
        self.assertEqual(self.shot.dialogue, dialogue)
        self.assertEqual(list(ApprovalDecision.objects.values()), approvals)
        self.assertEqual(get_run_execution_plan(self.run), self.plan)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertEqual(self.amend("apply", applied["sha256"], policy=SOURCE_POLICY, source_changes=changes)["id"], applied["id"])
        with self.assertRaisesMessage(ValidationError, "已有不同"):
            self.amend(policy=SOURCE_POLICY, source_changes=[changes[0]])

    def test_explicit_sources_validate_scope_type_duplicates_and_existing_execution(self):
        changes = self.broken_reference_route()
        with self.assertRaises(PermissionDenied): self.amend(policy=SOURCE_POLICY, source_changes=changes, actor=self.other)
        for invalid in ([], changes + [changes[0]], [{**changes[0], "route_index": 99}],
                [{**changes[0], "input_key": "unknown"}], [{**changes[0], "input_key": "images"}],
                [{**changes[0], "parameter_policies": []}], [{**changes[0], "source": {"kind": "unknown"}}]):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.amend(policy=SOURCE_POLICY, source_changes=invalid)
        with self.assertRaises(ValidationError): self.amend(policy=POLICY, source_changes=changes)
        with self.assertRaises(ValidationError): self.amend(policy="unknown", source_changes=changes)
        proposed = self.amend(policy=SOURCE_POLICY, source_changes=changes)
        with self.assertRaises(ValidationError): self.amend("apply", "0"*64, policy=SOURCE_POLICY, source_changes=changes)
        with patch("studio.services.production_stage_input_amendments.ExecutionPreparation.objects.filter") as existing:
            existing.return_value.exists.return_value = True
            with self.assertRaisesMessage(ValidationError, "已有执行准备"):
                self.amend("apply", proposed["sha256"], policy=SOURCE_POLICY, source_changes=changes)
        self.assertFalse(ProductionStageInputAmendment.objects.exists())

    def test_explicit_sources_api_requires_policy_and_rejects_unknown_nested_fields(self):
        changes = self.broken_reference_route()
        client = APIClient(); client.force_authenticate(self.actor)
        url = f"/api/v1/production-runs/{self.run.pk}/stage-input-amendment/"
        payload = {"stage_run_id": str(self.stage.pk), "expected_run_plan_sha256": self.plan.sha256,
            "action": "preview", "policy": SOURCE_POLICY, "source_changes": changes}
        response = client.post(url, payload, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        for change in ({"policy": POLICY}, {"source_changes": []}, {"source_changes": [{**changes[0], "model": "other"}]}):
            self.assertEqual(client.post(url, {**payload, **change}, format="json").status_code, 400)
        self.assertFalse(ProductionStageInputAmendment.objects.exists())
