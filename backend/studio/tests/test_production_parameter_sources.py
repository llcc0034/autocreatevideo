"""Registered scalar authoring and real project reads; no remote execution."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import SeriesProject
from studio.services.production_parameter_sources import (
    bind_recipe_parameter_sources, recipe_parameter_source_options,
    resolved_recipe_parameter_source, validate_recipe_parameter_source,
)
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class ParameterSourceContractTests(SimpleTestCase):
    def test_exact_authored_selectors_never_accept_values_or_arbitrary_paths(self):
        source = {"kind": "context_scalar", "selector_key": "project.width", "scope": {"kind": "project"}}
        for kind in ("integer", "number"):
            validate_recipe_parameter_source(source, parameter_type=kind)
        for kind in ("string", "boolean"):
            with self.assertRaises(ValidationError):
                validate_recipe_parameter_source(source, parameter_type=kind)
        for change in ({"kind": "context_field"}, {"selector_key": "project.password"},
                {"selector_key": "width * height"}, {"selector_key": []}, {"value": 512},
                {"scope": {"kind": "project", "id": str(uuid4())}}, {"scope": {"kind": "shot"}}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_recipe_parameter_source({**source, **change}, parameter_type="integer", require_complete=False)

    def test_bound_value_decoder_never_reloads_sources_and_rejects_malformed_evidence(self):
        source = {"schema_version": 1, "kind": "context_scalar", "selector_key": "project.width",
            "scope": {"kind": "project", "id": str(uuid4())},
            "observed_updated_at": "2026-09-10T00:00:00+00:00", "value": 720}
        self.assertEqual(resolved_recipe_parameter_source(source), 720)
        changes = [{"schema_version": True}, {"selector_key": []}, {"selector_key": "project.unknown"},
            {"scope": {"kind": "project", "id": "not-a-uuid"}}, {"scope": {"kind": "shot", "id": str(uuid4())}},
            {"observed_updated_at": "bad"}, {"observed_updated_at": "2026-09-10T00:00:00"}]
        changes += [{"value": value} for value in (None, True, "720", [], {}, 0, -1, 1.5)]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                resolved_recipe_parameter_source({**source, **change})

    def test_source_requires_original_transaction_but_value_policies_need_no_reads(self):
        values = [{"key": "seed", "policy": "locked_value", "value": 0}]
        self.assertEqual(bind_recipe_parameter_sources(values, run=None), values)
        with self.assertRaisesMessage(ValidationError, "准备事务"):
            bind_recipe_parameter_sources([{"key": "seed", "policy": "locked_source", "source": {}}], run=None)


class ParameterSourceProjectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="scalar-source-owner")
        cls.project = SeriesProject.objects.create(owner=cls.owner, title="Scalar project", **PROJECT_CONFIG)

    def test_all_registered_fields_share_one_narrow_query_and_preserve_declared_units(self):
        catalog = [row for row in recipe_parameter_source_options() if row["source_kind"] == "context_scalar"]
        self.assertEqual(len({row["selector_key"] for row in catalog}), len(catalog))
        policies = [{"key": row["selector_key"], "policy": "locked_source", "source": {
            "kind": "context_scalar", "selector_key": row["selector_key"], "scope": {"kind": "project"}}} for row in catalog]
        original = deepcopy(policies)
        with CaptureQueriesContext(connection) as queries:
            bound = bind_recipe_parameter_sources(policies, run=SimpleNamespace(project_id=self.project.pk, owner_id=self.owner.pk))
        self.assertEqual(len(queries), 1)
        self.assertNotIn('"title"', queries[0]["sql"])
        self.assertNotIn('"synopsis"', queries[0]["sql"])
        self.assertEqual(policies, original)
        values = {row["key"]: resolved_recipe_parameter_source(row["source"]) for row in bound}
        self.assertEqual(values, {"project.width": 720, "project.height": 1280, "project.fps": 24,
            "project.default_shot_duration_ms": 1000})
        self.assertEqual({row["selector_key"]: row["unit"] for row in catalog}["project.default_shot_duration_ms"], "ms")

    def test_foreign_and_missing_positive_fact_are_not_replaced_by_defaults(self):
        policies = [{"key": "width", "policy": "locked_source", "source": {
            "kind": "context_scalar", "selector_key": "project.width", "scope": {"kind": "project"}}}]
        with self.assertRaisesMessage(ValidationError, "所有者"):
            bind_recipe_parameter_sources(policies, run=SimpleNamespace(project_id=self.project.pk, owner_id=-1))
        SeriesProject.objects.filter(pk=self.project.pk).update(width=0)
        with self.assertRaisesMessage(ValidationError, "不采用参数默认值"):
            bind_recipe_parameter_sources(policies, run=SimpleNamespace(project_id=self.project.pk, owner_id=self.owner.pk))

    def frame_fixture(self):
        from studio.services.execution_parameters import compile_execution_parameter_contract
        from studio.services.json_contracts import json_contract
        definition = json_contract({"configuration": {"workflow_version_id": str(uuid4())}, "interface": {
            "parameters": compile_execution_parameter_contract({
                "video.fps": [{"source": "fixture.fps", "rule": {"type": "number", "required": True, "default": 24}}],
                "video.frames": [{"source": "fixture.frames", "rule": {"type": "integer", "required": True, "minimum": 5, "maximum": 3600}}]}),
            "media_semantic_keys": {"fps": "video.fps"},
            "frame_policy": {"fps": 24, "minimum_frames": 5, "maximum_frames": 3600, "alignment_modulus": 17, "alignment_remainder": 5}}})
        source = {"kind": "workflow_frames", "selector_key": "shot.duration_ms", "scope": {"kind": "shot"}}
        return definition, [{"key": "video.frames", "policy": "locked_source", "source": source}]

    def bind_frames(self, definition, policies, duration, **kwargs):
        scope = {"kind": "shot", "id": str(uuid4())}
        episode = str(uuid4())
        reviewed = {"canonical": {"episodes": [{"id": episode, "shots": [{"id": scope["id"], "duration_ms": duration}]}]},
            "review": {"stage_run_id": str(uuid4()), "decision_id": str(uuid4()), "revision": 1, "snapshot_sha256": "b"*64}}
        with patch("studio.services.production_execution_units.resolve_production_unit", return_value={"scope": scope, "episode_id": episode}), patch(
                "studio.services.production_reviewed_content.read_reviewed_storyboard", return_value=reviewed):
            return bind_recipe_parameter_sources(policies, run=SimpleNamespace(project_id=self.project.pk, owner_id=self.owner.pk),
                definition=definition, eligible_stage_ids=[], stage_key="media_generation", **{"scope": scope, **kwargs})

    def test_registered_alignment_uses_effective_fps_and_freezes_without_mutating_definition(self):
        definition, policies = self.frame_fixture()
        before = deepcopy(definition.value)
        for duration, expected in ((2000, 56), (3000, 73), (5000, 124)):
            bound = self.bind_frames(definition, policies, duration)
            with self.assertNumQueries(0), patch("studio.services.workflow.aligned_frames", side_effect=AssertionError("No recalculation")):
                self.assertEqual(resolved_recipe_parameter_source(bound[0]["source"]), expected)
        policies.append({"key": "video.fps", "policy": "locked_value", "value": 12})
        bound = self.bind_frames(definition, policies, 2000)
        self.assertEqual(bound[0]["source"]["value"], 22)
        self.assertEqual(bound[0]["source"]["fps"]["value"], 12)
        policies[-1] = {"key": "video.fps", "policy": "locked_source", "source": {
            "kind": "context_scalar", "selector_key": "project.fps", "scope": {"kind": "project"}}}
        self.assertEqual(self.bind_frames(definition, policies, 2000)[0]["source"]["value"], 56)
        self.assertEqual(definition.value, before)

    def test_derived_source_rejects_scope_missing_clock_out_of_range_and_corrupt_evidence(self):
        from studio.services.json_contracts import json_contract
        definition, policies = self.frame_fixture()
        source = policies[0]["source"]
        validate_recipe_parameter_source(source, parameter_type="integer", parameter_key="video.frames",
            definition=definition, stage_key="media_generation")
        for changes in ({"stage_key": "script_generate"}, {"parameter_key": "seed"}, {"parameter_type": "number"}):
            with self.assertRaises(ValidationError):
                validate_recipe_parameter_source(source, **{"parameter_type": "integer", "parameter_key": "video.frames",
                    "definition": definition, "stage_key": "media_generation", **changes})
        for duration in (0, True, -1, 10, 1000000):
            with self.subTest(duration=duration), self.assertRaises(ValidationError):
                self.bind_frames(definition, policies, duration)
        with self.assertRaisesMessage(ValidationError, "当前明确的镜头"):
            self.bind_frames(definition, policies, 2000, scope={"kind": "project", "id": str(self.project.pk)})
        for key, value in (("frame_policy", None), ("media_semantic_keys", {})):
            invalid = deepcopy(definition.value); invalid["interface"][key] = value
            with self.assertRaises(ValidationError): self.bind_frames(json_contract(invalid), policies, 2000)
        invalid = deepcopy(definition.value)
        invalid["interface"]["parameters"]["video.fps"]["layers"][0]["rule"].pop("default")
        with self.assertRaises(ValidationError): self.bind_frames(json_contract(invalid), policies, 2000)
        bound = self.bind_frames(definition, policies, 2000)[0]["source"]
        for changes in ({"review": None}, {"fps": {}}, {"scope": {"kind": "shot", "id": "bad"}}, {"value": 55},
                {"definition_sha256": "bad"}, {"workflow_version_id": None}, {"duration_ms": False}, {"frame_policy": None}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                resolved_recipe_parameter_source({**bound, **changes})
