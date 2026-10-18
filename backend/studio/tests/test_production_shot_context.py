"""Original review, precise Shot content and shared input resolution; no calls."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import ApprovalDecision, ExecutionPreparation, GenerationAttempt, Shot
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.json_contracts import json_contract
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.production_recipe_contracts import validate_recipe_input_sources
from studio.services.production_recipe_media_preparations import resolve_recipe_media_sources
from studio.services.production_reviewed_content import read_reviewed_storyboard
from studio.services.production_runs import _formal_snapshot, _save_stage_snapshot
from studio.services.production_run_plans import get_run_execution_plan
from studio.tests.test_production_reviewed_references import ReviewedReferenceTests


class ShotContextTests(TestCase):
    setUp = ReviewedReferenceTests.setUp
    approve = ReviewedReferenceTests.approve

    def definition(self, kind="json", keys=("brief",)):
        return json_contract({"schema_version": 1, "configuration": {"inputs": [
            {"key": key, "kind": kind, "required": True, "min_items": 1, "max_items": 1,
                "targets": [], "semantic_contract": {"schema_version": 1, "allowed_scope_kinds": ["shot"],
                    "allowed_purposes": [], "allowed_subject_kinds": [], "paired_with_input_key": None, "allow_repeated_asset": False}}
            for key in keys]}, "resources": {"conditioning_mode": None}, "interface": {"input_targets": {}}})

    def source(self, selector="shot.duration_ms", shot=None):
        return {"kind": "context_field", "selector_key": selector,
            "scope": {"kind": "shot", "id": str((shot or self.shot).pk)}, "content_revision_id": str(self.decision.pk)}

    def resolve(self, sources=None, *, definition=None, **overrides):
        return resolve_execution_input_sources(definition or self.definition(), sources or {"brief": self.source()},
            **{"owner": self.actor, "subject_facts": [], "max_media_bytes": 1024*1024, "project_id": self.project.pk,
                "production_run_id": str(self.run.pk), "eligible_stage_ids": execution_source_stage_ids(self.stage), **overrides})

    def reapprove(self):
        _save_stage_snapshot(self.review, "output_snapshot", _formal_snapshot(self.run, self.review, "storyboard:" + str(self.episode.pk)))
        self.decision = ApprovalDecision.objects.create(stage_run=self.review, revision=self.decision.revision+1,
            decision="approve", actor="human", decided_by=self.actor, output_snapshot_sha256=self.review.output_snapshot_sha256)

    def test_different_shots_bind_their_own_reviewed_timing_without_literal_copies(self):
        self.shot.duration_ms = 2345; self.shot.save(update_fields=("duration_ms",))
        second = Shot.objects.create(episode=self.episode, number=2, duration_ms=1001,
            media_type="image", include_audio=False)
        self.reapprove()
        rule = {"kind": "context_field", "selector_key": "shot.duration_ms", "scope": {"kind": "shot"}}
        definition = self.definition()
        validate_recipe_input_sources(definition, {"brief": rule}, prior_stages=["storyboard_review"])
        for shot in (self.shot, second):
            bound = resolve_recipe_media_sources(definition=definition, route={"input_sources": {"brief": rule}},
                stage=self.stage, plan=get_run_execution_plan(self.run), scope={"kind": "shot", "id": str(shot.pk)})
            self.assertEqual(bound["inputs"]["brief"], self.source(shot=shot))
            result = self.resolve(bound["inputs"]).value
            self.assertEqual(result["logical_values"]["brief"], shot.duration_ms)
            origin = result["input_provenance"][0]
            self.assertEqual(origin["review"]["decision_id"], str(self.decision.pk))
            self.assertEqual(origin["scope"]["id"], str(shot.pk))
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())

    def test_old_creative_selectors_and_whole_shot_shortcut_are_rejected_everywhere(self):
        from studio.services.execution_input_catalog import execution_context_field_options
        from studio.services.execution_structure_mapping import freeze_structure_source_schemas
        retired = ["shot." + field for field in ("content", "narrative", "detailed_description",
            "action", "camera", "sound_effects", "music", "continuity_notes")]
        catalog = {row["selector_key"] for row in execution_context_field_options()}
        self.assertFalse(set(retired) & catalog)
        self.assertFalse(set(retired) & set(freeze_structure_source_schemas()["context_field"]))
        for selector in retired:
            definition = self.definition("json" if selector == "shot.content" else "text")
            rule = {"kind": "context_field", "selector_key": selector, "scope": {"kind": "shot"}}
            with self.subTest(selector=selector):
                with self.assertRaisesMessage(ValidationError, "目录"):
                    validate_recipe_input_sources(definition, {"brief": rule}, prior_stages=["storyboard_review"])
                # Even an already-bound old source cannot bypass the catalog.
                with self.assertRaisesMessage(ValidationError, "未登记"):
                    self.resolve({"brief": self.source(selector)}, definition=definition)
                with self.assertRaises(ValidationError):
                    validate_recipe_input_sources(self.definition(), {"brief": {
                        "kind": "structured", "template": rule}}, prior_stages=["storyboard_review"])
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())

    def test_missing_changed_review_or_unapproved_identity_rejects(self):
        for changes in ({"content_revision_id": str(uuid4())}, {"selector_key": "shot.__dict__"},
                {"value": "client replacement"}, {"scope": {"kind": "shot", "id": str(uuid4())}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.resolve({"brief": {**self.source(), **changes}})
        for changes in ({"production_run_id": None}, {"eligible_stage_ids": ()}, {"owner": self.other}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError): self.resolve(**changes)
        self.shot.duration_ms += 1; self.shot.save(update_fields=("duration_ms",))
        with self.assertRaisesMessage(ValidationError, "已变更"): self.resolve()

    def test_old_approval_does_not_follow_a_new_approval(self):
        original = self.source()
        self.reapprove()
        with self.assertRaisesMessage(ValidationError, "不一致"): self.resolve({"brief": original})
        self.resolve()

    def test_missing_preceding_review_and_disallowed_scope_are_rejected_at_authoring(self):
        rule = {"kind": "context_field", "selector_key": "shot.duration_ms", "scope": {"kind": "shot"}}
        with self.assertRaisesMessage(ValidationError, "更早"): validate_recipe_input_sources(self.definition(), {"brief": rule}, prior_stages=[])
        payload = deepcopy(self.definition().value)
        payload["configuration"]["inputs"][0]["semantic_contract"]["allowed_scope_kinds"] = ["project"]
        with self.assertRaisesMessage(ValidationError, "没有开放"):
            validate_recipe_input_sources(json_contract(payload), {"brief": rule}, prior_stages=["storyboard_review"])

    def test_multiple_fields_share_one_review_integrity_read(self):
        with patch("studio.services.production_reviewed_content.read_reviewed_storyboard", wraps=read_reviewed_storyboard) as reader:
            self.resolve({"brief": self.source(), "dialogue": self.source("shot.dialogue")}, definition=self.definition(keys=("brief", "dialogue")))
        self.assertEqual(reader.call_count, 1)
