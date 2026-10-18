"""Real M2 fields -> recipe RunPlan -> reviewed inputs -> shared profile.

Only isolated database/files. The author picker and a live module cutover are
not covered here. No manual copying of resolved field values into the profile.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import (ApprovalDecision, DataStructureVersion, ExecutionPreparation, GenerationAttempt,
    ProductionRecipeVersion, PromptProfileVersion, Shot, StageRun)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import update_execution_draft, publish_execution_version
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_profile_inputs import project_execution_profile_sources
from studio.services.execution_profile_preparation import prepare_execution_profile
from studio.services.execution_shot_field_mapping import shot_field_source_option, shot_field_schema_key
from studio.services.execution_structure_mapping import freeze_structure_source_schemas
from studio.services.production_recipe_contracts import validate_recipe_input_sources
from studio.services.production_recipe_media_preparations import resolve_recipe_media_sources
from studio.services.production_runs import _formal_snapshot, _save_stage_snapshot
from studio.services.prompt_profiles import assemble_prompt_profile, refresh_prompt_profile_contract, publish_prompt_profile
from studio.services.shot_field_revisions import read_frozen_shot_field_revision
from studio.tests import test_production_shot_field_sources as source_fixtures
from studio.tests import test_execution_definitions as execution_fixtures
from studio.tests import test_production_recipe_media_preparations as recipe_fixtures
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule
from studio.tests.output_structure_fixtures import response_structure
from studio.tests.test_storyboard_declared_writer import no_dml


SOURCE = "request.structured_input"


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ShotFieldMappingTests(TestCase):
    # Reuse real source/adoption fixtures, not their tests or fake identities.
    setUpTestData = classmethod(execution_fixtures.ExecutionDefinitionTests.setUpTestData.__func__)
    write_contract = staticmethod(execution_fixtures.ExecutionDefinitionTests.write_contract)
    create_root = recipe_fixtures.RecipeMediaPreparationTests.create_root
    prepare = recipe_fixtures.RecipeMediaPreparationTests.prepare
    create = source_fixtures.FrozenShotFieldSourcesTests.create
    post = source_fixtures.FrozenShotFieldSourcesTests.post
    read = source_fixtures.FrozenShotFieldSourcesTests.read
    adopt_source = source_fixtures.FrozenShotFieldSourcesTests.adopt_source
    approval = source_fixtures.FrozenShotFieldSourcesTests.approval
    revision_url = source_fixtures.FrozenShotFieldSourcesTests.revision_url
    detail = source_fixtures.FrozenShotFieldSourcesTests.detail
    field_payload = source_fixtures.FrozenShotFieldSourcesTests.field_payload
    write = source_fixtures.FrozenShotFieldSourcesTests.write
    tone = source_fixtures.FrozenShotFieldSourcesTests.tone
    approve_storyboard = source_fixtures.FrozenShotFieldSourcesTests.approve_storyboard

    def setUp(self):
        source_fixtures.FrozenShotFieldSourcesTests.setUp(self)
        execution_fixtures.ExecutionDefinitionTests.setUp(self)
        self.shot = Shot.objects.get(pk=self.shot_id)
        detail = self.detail()
        tone = source_fixtures.selection(self.tone(detail))
        motion = source_fixtures.selection(next(row["original"] for row in detail["fields"]
            if row["original"].get("value") == "转身"))
        self.nodes = {key: {"kind": "shot_field", "field": choice, "scope": {"kind": "shot"}}
            for key, choice in (("emotion", tone), ("motion", motion))}

    def root(self, *, variant_count=1):
        # Satisfy actual reference requirements and adopt original M2 fields.
        self.approve_storyboard()
        structure = response_structure(example={"emotion": "tone", "motion": "move"})
        profile = PromptProfileVersion.objects.create(profile_key="mapped-shot-fields", version=1,
            name="Isolated mapped fields", model_family="async_cloud", operation="video.generate", mode="T2VA",
            review_state="ready", input_structure_version=structure)
        for position, key in enumerate(self.nodes):
            field = create_field_definition(key="mapped_" + key, label=key, value_type="long_text")
            create_profile_field_rule(profile_version=profile, field_definition=field, source_selector=SOURCE,
                generation_source_selector=SOURCE, source_path=[key], channel="positive_prompt", position=position,
                label_style="none", separator="newline", required=True, max_chars=4000)
        refresh_prompt_profile_contract(profile)
        self.profile = publish_prompt_profile(profile, actor=self.actor)
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision,
            actor=self.actor, changes={"prompt_mode": "profile", "prompt_profile_version": self.profile, "inputs": []})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.rules = {SOURCE: {"kind": "structured", "template": {"kind": "object", "fields": deepcopy(self.nodes)}}}
        validate_recipe_input_sources(self.definition, self.rules, prior_stages=["storyboard_review"])
        route = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "input_sources": self.rules, "parameter_policies": [
                {"key": key, "policy": "locked_value", "value": value}
                for key, value in {"width": 768, "height": 1344, "fps": 24, "duration_seconds": 3}.items()]}
        self.create_root({"mode": "fixed", "variant_count": variant_count, "routes": [route], "evaluator": None},
            prior=[{"key": "storyboard_review", "mode": "review", "approval": "required"}])
        self.review = StageRun.objects.create(production_run=self.run, stage_key="storyboard_review", ordinal=1,
            mode="review", status="succeeded", idempotency_key=str(uuid4()))
        self.reapprove()

    def reapprove(self):
        _save_stage_snapshot(self.review, "output_snapshot", _formal_snapshot(self.run, self.review,
            "storyboard:" + str(self.shot.episode_id)))
        self.decision = ApprovalDecision.objects.create(stage_run=self.review,
            revision=self.review.approval_decisions.count() + 1, decision="approve", actor="human",
            decided_by=self.actor, output_snapshot_sha256=self.review.output_snapshot_sha256)

    def bind(self, rules=None, plan=None):
        return resolve_recipe_media_sources(definition=self.definition, route={"input_sources": rules or self.rules},
            stage=self.stage, plan=plan or self.plan, scope={"kind": "shot", "id": str(self.shot.pk)})

    def resolve(self, bound, **changes):
        return resolve_execution_input_sources(self.definition, bound["inputs"], **{
            "owner": self.actor, "subject_facts": [], "max_media_bytes": 65536, "project_id": self.project.pk,
            "production_run_id": str(self.run.pk), "eligible_stage_ids": bound["eligible_stage_ids"],
            "production_scope": {"kind": "shot", "id": str(self.shot.pk)}, "unit_key": f"shot:{self.shot.pk}", **changes})

    def test_formal_binding_and_normalizer_reach_profile_without_value_copy_or_write(self):
        revised = self.write(self.field_payload("  配方取原值\n")).data["detail"]
        # The selected independent field is not inferred from another Shot fact.
        Shot.objects.filter(pk=self.shot.pk).update(title="UNSELECTED-TITLE-MUST-NOT-LEAK")
        self.root()
        self.assertEqual(len(self.plan.value["structure_source_schemas"]["shot_field"]), 2)
        before = list(ApprovalDecision.objects.values()), list(Shot.objects.values())
        with patch("studio.services.production_shot_field_sources.read_frozen_shot_field_revision",
                wraps=read_frozen_shot_field_revision) as reader, CaptureQueriesContext(connection) as queries:
            bound = self.bind()
            resolved = self.resolve(bound)
        self.assertTrue(no_dml(queries))
        self.assertEqual(reader.call_count, 1)
        registry_reads = [row for row in queries if 'FROM "studio_datastructureversion"' in row["sql"]
            or 'FROM "studio_datastructurenode"' in row["sql"]]
        self.assertEqual(len(registry_reads), 4)  # One structure + nodes per bind/resolve call, not per leaf.
        if connection.vendor == "sqlite":
            self.assertIn("USING INDEX", DataStructureVersion.objects.filter(
                pk=self.nodes["emotion"]["field"]["structure_version_id"]).explain())
        self.assertEqual(resolved.value["logical_values"][SOURCE], {"emotion": "  配方取原值\n", "motion": "转身"})
        leaves = resolved.value["input_provenance"][0]["leaves"]
        self.assertTrue(all(row["field_revision"] == revised["revision"] for row in leaves))
        self.assertTrue(all(row["review"]["decision_id"] == str(self.decision.pk) for row in leaves))
        prepared = prepare_execution_profile(self.definition, resolved, profile=self.profile,
            unit_key=f"shot:{self.shot.pk}", source_language="zh", target_language="zh")
        projection = project_execution_profile_sources(self.definition, resolved.value["logical_values"])
        self.assertEqual(prepared.value["profile_sources"]["projection"], projection)
        result = assemble_prompt_profile(self.profile, sources=projection["assembly_sources"])
        self.assertEqual(result["structured_fields"]["mapped_motion"], "转身")
        self.assertIn("配方取原值", result["channels"]["positive_prompt"])
        self.assertEqual(before, (list(ApprovalDecision.objects.values()), list(Shot.objects.values())))
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())

    def test_recipe_api_preserves_exact_fields_and_rejects_bad_identity_without_overwrite(self):
        from django.urls import reverse
        self.root()
        self.actor.is_staff = True; self.actor.save(update_fields=("is_staff",))
        options = self.client.get(reverse("production-recipe-editor-options"))
        self.assertEqual(options.status_code, 200, options.data)
        self.assertIn({"value": "shot_field", "label": "独立分镜字段"}, options.data["structure_node_options"])
        retired = {"shot." + key for key in ("content", "narrative", "detailed_description", "action",
            "camera", "sound_effects", "music", "continuity_notes")}
        contract = self.client.get(f"/api/v1/model-execution-instance-versions/{self.version.pk}/input-contract/")
        self.assertEqual(contract.status_code, 200, contract.data)
        self.assertFalse(retired & {row["selector_key"] for row in contract.data["context_fields"]})
        definition = deepcopy(self.plan.value["recipe"]["definition"])
        response = self.client.post(reverse("production-recipe-list"), {"recipe_key": "fields-" + uuid4().hex,
            "name": "Isolated field mapping", "definition": definition}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        url = reverse("production-recipe-detail", args=[response.data["id"]])
        self.assertEqual(self.client.get(url).data["definition"], definition)
        original = ProductionRecipeVersion.objects.get(pk=response.data["id"]).definition_sha256
        invalid = deepcopy(definition)
        invalid["stages"][-1]["routing"]["routes"][0]["input_sources"][SOURCE]["template"]["fields"][
            "emotion"]["field"]["field_definition_version_id"] = str(uuid4())
        failed = self.client.patch(url, {"definition": invalid}, format="json")
        self.assertEqual(failed.status_code, 400, failed.data)
        self.assertEqual(ProductionRecipeVersion.objects.get(pk=response.data["id"]).definition_sha256, original)
        for selector in retired:
            invalid = deepcopy(definition)
            invalid["stages"][-1]["routing"]["routes"][0]["input_sources"][SOURCE]["template"]["fields"][
                "emotion"] = {"kind": "context_field", "selector_key": selector, "scope": {"kind": "shot"}}
            with self.subTest(selector=selector):
                failed = self.client.patch(url, {"definition": invalid}, format="json")
                self.assertEqual(failed.status_code, 400, failed.data)
                self.assertEqual(ProductionRecipeVersion.objects.get(pk=response.data["id"]).definition_sha256, original)
                self.assertEqual(self.client.get(url).data["definition"], definition)

    def test_original_preparation_persists_inputs_and_replays_without_reading_later_fields(self):
        self.root(variant_count=2)
        # Explicit isolated planning change to match this T2VA instance, then
        # re-review. Do not coerce a real shot or silently switch its route.
        self.shot.media_type = "video"; self.shot.save(update_fields=("media_type",))
        self.reapprove()
        first = self.prepare()
        frozen = deepcopy(first.contract.resolved_inputs)
        self.assertEqual(frozen["inputs"]["logical_values"][SOURCE], {"emotion": "紧张", "motion": "转身"})
        self.write(self.field_payload("后来修改，未审核"))
        replay = self.prepare()
        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(replay.contract.resolved_inputs, frozen)
        with self.assertRaisesMessage(ValidationError, "已变更"):
            self.prepare(candidate_number=2)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_source_identity_type_prior_review_and_plan_must_match(self):
        option = shot_field_source_option(self.nodes["emotion"])
        self.assertEqual(option["kind"], "text")
        for key in ("structure_version_id", "field_definition_version_id", "node_key"):
            node = deepcopy(self.nodes["emotion"]); node["field"][key] = str(uuid4())
            with self.assertRaises(ValidationError): shot_field_source_option(node)
        self.root()
        with self.assertRaises(ValidationError):
            validate_recipe_input_sources(self.definition, self.rules, prior_stages=[])
        wrong = deepcopy(self.rules); wrong[SOURCE]["template"]["fields"]["motion"]["path"] = ["undeclared"]
        with self.assertRaises(ValidationError):
            validate_recipe_input_sources(self.definition, wrong, prior_stages=["storyboard_review"])
        from studio.services.json_contracts import json_contract
        bad_plan = deepcopy(self.plan.value); bad_plan["structure_source_schemas"].pop("shot_field")
        with self.assertRaisesMessage(ValidationError, "冻结运行计划"):
            self.bind(plan=json_contract(bad_plan))
        bound = self.bind()
        leaf = bound["inputs"][SOURCE]["template"]["fields"]["emotion"]
        for changes in ({"source_schema_sha256": "0" * 64}, {"content_revision_id": []},
                {"scope": {"kind": "shot", "id": str(uuid4())}}, {"extra": True}):
            bad = deepcopy(bound); bad["inputs"][SOURCE]["template"]["fields"]["emotion"] = {**leaf, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValidationError): self.resolve(bad)
        with self.assertRaises(ValidationError): self.resolve(bound, eligible_stage_ids=[])
        with self.assertRaises(ValidationError): self.resolve(bound, project_id=uuid4())
        restricted = deepcopy(self.definition.value)
        restricted["configuration"]["inputs"][0]["semantic_contract"]["allowed_scope_kinds"] = ["project"]
        with patch.object(self, "definition", json_contract(restricted)), self.assertRaisesMessage(ValidationError, "没有开放镜头范围"):
            self.resolve(bound)
        expanded = deepcopy(bound)
        expanded["inputs"][SOURCE]["template"] = {"kind": "array", "items": [deepcopy(leaf) for _ in range(100)]}
        with patch("studio.services.execution_input_resolution.execution_resolved_inputs_max_bytes", return_value=65536), self.assertRaisesMessage(ValidationError, "累计"):
            self.resolve(expanded)

    def test_revision_change_invalidates_old_review_and_original_frozen_inputs_stay_unchanged(self):
        self.root()
        bound = self.bind(); original = self.resolve(bound)
        before = deepcopy(original.value)
        self.write(self.field_payload("重新审核后取此原文"))
        with self.assertRaises(ValidationError): self.resolve(bound)
        self.reapprove()
        with self.assertRaises(ValidationError): self.resolve(bound)
        changed = self.resolve(self.bind())
        self.assertEqual(changed.value["logical_values"][SOURCE]["emotion"], "重新审核后取此原文")
        self.assertEqual(original.value, before)
        self.assertEqual(before["logical_values"][SOURCE]["emotion"], "紧张")

    def test_freeze_only_declared_fields_not_literal_lookalikes(self):
        root = {"key": "media_generation", "routing": {"routes": [{"input_sources": {
            "one": {"kind": "structured", "template": {"kind": "array", "items": [
                self.nodes["emotion"], self.nodes["emotion"], {"kind": "literal", "value": self.nodes["motion"]}]}}}}]}}
        frozen = freeze_structure_source_schemas(stages=[root])
        self.assertEqual(set(frozen["shot_field"]), {shot_field_schema_key(self.nodes["emotion"])})
        self.assertNotIn("shot_field", freeze_structure_source_schemas())
