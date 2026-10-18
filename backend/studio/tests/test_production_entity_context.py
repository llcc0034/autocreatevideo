"""Current entity content is an explicit production target, never a fake Shot."""
from copy import deepcopy
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import (ApprovalDecision, Asset, CharacterBible, PropBible, SceneBible,
    ProductionRecipeVersion, ProductionRun, SeriesProject, StageRun, ExecutionPreparation, GenerationAttempt)
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.production_recipe_contracts import validate_recipe_input_sources
from studio.services.production_recipe_media_preparations import resolve_recipe_media_sources
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_runs import _formal_snapshot, _save_stage_snapshot
from studio.services.production_stage_catalog import production_stage_catalog
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class EntityContextTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="entity-context")
        self.project = SeriesProject.objects.create(owner=self.actor, title="Entity context", **PROJECT_CONFIG)
        self.entities = [(kind, model.objects.create(project=self.project, slug=kind, name=kind,
            description=f"Description of {kind}"))
            for kind, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible))]
        recipe = ProductionRecipeVersion(recipe_key="entity-context", version=1, name="Source fixture", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "bible_review", "mode": "review", "approval": "required"},
            {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "required"}]}, schema_version=3)
        recipe.save()
        self.run = ProductionRun.objects.create(owner=self.actor, project=self.project, recipe_version=recipe,
            plan_source="recipe", status="running", idempotency_key=str(uuid4()), request_sha256="a"*64)
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        self.review = StageRun.objects.create(production_run=self.run, stage_key="bible_review", mode="review", ordinal=1,
            status="succeeded", idempotency_key=str(uuid4()))
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="bible_image_generation", mode="existing_pipeline", ordinal=2,
            status="running", idempotency_key=str(uuid4()))
        _save_stage_snapshot(self.review, "output_snapshot", _formal_snapshot(self.run, self.review, "bible"))
        self.decision = ApprovalDecision.objects.create(stage_run=self.review, revision=1, decision="approve", actor="human",
            decided_by=self.actor, output_snapshot_sha256=self.review.output_snapshot_sha256)

    def definition(self, kind="text"):
        return json_contract({"schema_version": 1, "configuration": {"inputs": [{"key": "brief", "kind": kind,
            "required": True, "min_items": 1, "max_items": 1, "targets": [], "semantic_contract": {"schema_version": 1,
                "allowed_scope_kinds": ["request"], "allowed_subject_kinds": [], "allowed_purposes": [],
                "paired_with_input_key": None, "allow_repeated_asset": False}}]},
            "resources": {"conditioning_mode": None}, "interface": {"input_targets": {}}})

    def scope(self, index=0):
        kind, row = self.entities[index]
        return {"kind": kind, "id": str(row.pk)}

    def rule(self, field="description"):
        return {"kind": "context_field", "selector_key": f"production.entity.{field}", "scope": {"kind": "request"}}

    def resolve(self, *, field="description", scope=None, source_changes=None):
        return resolve_execution_input_sources(self.definition("json" if field == "content" else "text"),
            {"brief": {**self.rule(field), "content_revision_id": str(self.decision.pk), **(source_changes or {})}},
            owner=self.actor, subject_facts=[], max_media_bytes=1024*1024, project_id=self.project.pk,
            production_run_id=str(self.run.pk), eligible_stage_ids=execution_source_stage_ids(self.stage), production_scope=scope)

    def test_recipe_projects_each_explicit_entity_and_preserves_approval_identity(self):
        definition = self.definition()
        validate_recipe_input_sources(definition, {"brief": self.rule()}, prior_stages=["bible_review"])
        for index, (_, entity) in enumerate(self.entities):
            scope = self.scope(index)
            bound = resolve_recipe_media_sources(definition=definition, route={"input_sources": {"brief": self.rule()}},
                stage=self.stage, plan=get_run_execution_plan(self.run), scope=scope)
            self.assertEqual(bound["inputs"]["brief"]["content_revision_id"], str(self.decision.pk))
            result = self.resolve(scope=scope).value
            self.assertEqual(result["logical_values"]["brief"], entity.description)
            self.assertEqual(result["input_provenance"][0]["unit"]["scope"], scope)
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())

    def test_entity_content_excludes_separately_managed_media_bindings(self):
        before = self.resolve(field="content", scope=self.scope()).value["logical_values"]["brief"]
        asset = Asset.objects.create(owner=self.actor, name="Binding-only fixture", kind="reference")
        entity = self.entities[0][1]; entity.anchor_asset = asset; entity.save(update_fields=("anchor_asset",))
        entity.reference_assets.set([asset])
        after = self.resolve(field="content", scope=self.scope()).value["logical_values"]["brief"]
        self.assertEqual(before, after)
        self.assertNotIn("reference_asset_ids", after); self.assertNotIn("anchor_asset_id", after)
        self.assertNotIn("visual_identity", after)
        self.assertIsNone(after["primary_setting"])
        self.assertEqual(after["appearance_settings"], [])
        self.assertIsNone(self.resolve(field="content", scope=self.scope()).value["input_provenance"][0]["setting"])
        # Null in the aggregate is not permission to invent a selected text field.
        with self.assertRaises(ValidationError):
            self.resolve(field="primary.identity_anchor", scope=self.scope())

    def test_recipe_primary_fields_bind_the_original_review_and_exact_setting(self):
        from studio.services.bible_variants import create_variant
        entity = self.entities[0][1]
        primary = create_variant(actor=self.actor, project_id=self.project.pk, entity_kind="character",
            entity_id=entity.pk, appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="",
            value={"name": "主图", "reference_name": "人物·主图", "identity_anchor": "新的共同身份",
                "appearance_anchor": "主图基础衣着", "basis_asset_id": None, "basis_asset_sha256": ""})
        setting = primary.revisions.get(revision=1)
        # Isolated recipe review snapshot; this does not submit an operational approval.
        _save_stage_snapshot(self.review, "output_snapshot", _formal_snapshot(self.run, self.review, "bible"))
        decision = ApprovalDecision.objects.create(stage_run=self.review, revision=2, decision="approve", actor="human",
            decided_by=self.actor, output_snapshot_sha256=self.review.output_snapshot_sha256)
        for field, content in (("identity_anchor", "新的共同身份"), ("appearance_anchor", "主图基础衣着")):
            rule = self.rule(f"primary.{field}")
            validate_recipe_input_sources(self.definition(), {"brief": rule}, prior_stages=["bible_review"])
            bound = resolve_recipe_media_sources(definition=self.definition(), route={"input_sources": {"brief": rule}},
                stage=self.stage, plan=get_run_execution_plan(self.run), scope=self.scope())
            self.assertEqual(bound["inputs"]["brief"]["content_revision_id"], str(decision.pk))
            result = resolve_execution_input_sources(self.definition(), bound["inputs"], owner=self.actor,
                project_id=self.project.pk, subject_facts=[], max_media_bytes=65536, production_run_id=str(self.run.pk),
                production_scope=self.scope(), eligible_stage_ids=[self.review.pk]).value
            self.assertEqual(result["logical_values"]["brief"], content)
            proof = result["input_provenance"][0]
            self.assertEqual(proof["setting"]["setting_revision_id"], str(setting.pk))
            self.assertEqual(proof["review"]["decision_id"], str(decision.pk))
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())

    def test_missing_foreign_or_wrong_target_and_changed_content_fail(self):
        for scope in (None, {"kind": "project", "id": str(self.project.pk)}, {"kind": "character", "id": str(uuid4())}):
            with self.subTest(scope=scope), self.assertRaises(ValidationError): self.resolve(scope=scope)
        for changes in ({"content_revision_id": str(uuid4())}, {"selector_key": "production.entity.anchor_asset_id"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError): self.resolve(scope=self.scope(), source_changes=changes)
        entity = self.entities[0][1]; entity.description = "Unreviewed edit"; entity.save(update_fields=("description",))
        with self.assertRaisesMessage(ValidationError, "已变更"): self.resolve(scope=self.scope())

    def test_recipe_requires_earlier_bible_review_and_explicit_request_scope(self):
        with self.assertRaisesMessage(ValidationError, "更早"):
            validate_recipe_input_sources(self.definition(), {"brief": self.rule()}, prior_stages=[])
        rule = deepcopy(self.rule()); rule["scope"] = {"kind": "project"}
        with self.assertRaisesMessage(ValidationError, "不在受控目录"):
            validate_recipe_input_sources(self.definition(), {"brief": rule}, prior_stages=["bible_review"])
