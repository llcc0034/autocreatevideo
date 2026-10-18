"""Exact selected JSON -> M1 assembly mapping; isolated protocol receipts only.

The upstream worker uses its existing synthetic provider fixture. No real model,
encoding, media adoption or whole multi-episode delivery is claimed here.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ArtifactSelectionDecision, AssemblyJob, GenerationAttempt, GenerationTask
from studio.services.execution_text_output_catalog import plan_text_output_options
from studio.services.production_recipe_assembly_output import recipe_assembly_output_projection
from studio.services.production_recipe_contracts import validate_recipe_definition
from studio.services.security_boundaries import boundary_int
from studio.tests.test_structure_mapping_preview import field, fixed, obj
from studio.tests import test_structured_text_downstream as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",), STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class RecipeAssemblyUpstreamTests(TestCase):
    setUpTestData = fixtures.StructuredTextDownstreamTests.__dict__["setUpTestData"]
    setUp = fixtures.StructuredTextDownstreamTests.setUp
    node = fixtures.StructuredTextDownstreamTests.node
    create = fixtures.StructuredTextDownstreamTests.create
    publish = fixtures.StructuredTextDownstreamTests.publish
    output = fixtures.StructuredTextDownstreamTests.output
    process = fixtures.StructuredTextDownstreamTests.process
    prepare = fixtures.StructuredTextDownstreamTests.prepare
    queue_fixture = fixtures.StructuredTextDownstreamTests.queue_fixture
    advance = fixtures.StructuredTextDownstreamTests.advance
    output_layout = fixtures.StructuredTextDownstreamTests.output_layout
    produce = fixtures.StructuredTextDownstreamTests.produce
    complete_first_step = fixtures.StructuredTextDownstreamTests.complete_first_step
    source = fixtures.StructuredTextDownstreamTests.source
    candidate_count = 1
    evaluator = "first_succeeded"

    def reset_empty_plan(self):
        output = self.definition.value["interface"]["business_output"]
        delivery = getattr(self, "delivery_author", False)
        self.declarations.append({"key": "delivery" if delivery else "assembly", "mode": "existing_pipeline", "approval": "auto",
            "delivery_metadata" if delivery else "assembly_output": {"output_version_id": output["id"], "expected_contract_sha256": output["sha256"],
                "source": {"kind": "selected_output", "stage_key": "step-one",
                    "output_key": "structured_text." + output["id"], "scope": {"kind": "project"},
                    "selection_policy": "reviewed_selected"}, "mapping": {"schema_version": 1,
                        "node": obj(title=field("标题"), notes=field("说明")) if delivery else obj(width=field("宽"), height=field("高"), fps=field("率"))}}})
        fixtures.StructuredTextDownstreamTests.reset_empty_plan(self)
        self.assembly_stage = self.steps[-1]
        self.assembly_stage.mode = "existing_pipeline"
        self.assembly_stage.status = "pending"
        self.assembly_stage.save(update_fields=("mode", "status"))

    def setup_layout(self, extra_nodes=()):
        self.output_layout([self.node("root", "object"),
            self.node("w", "integer", "root", property_name="宽", required=True),
            self.node("h", "integer", "root", property_name="高", required=True, position=1),
            self.node("f", "integer", "root", property_name="率", required=True, position=2), *extra_nodes])
        option = next(row for row in plan_text_output_options(self.plan) if row["stage_key"] == "step-one")
        self.rule = {"output_version_id": option["output_version_id"],
            "expected_contract_sha256": option["output_contract_sha256"], "source": self.source(),
            "mapping": {"schema_version": 1, "node": obj(width=field("宽"), height=field("高"), fps=field("率"))}}
        self.declaration = {**self.declarations[-1], "assembly_output": self.rule}

    def test_delivery_metadata_freezes_selected_output_and_rejects_selection_drift(self):
        from studio.services.configuration import resolve_configuration
        from studio.services.json_contracts import assign_json_contract
        from studio.services.production_runs import _freeze_stage_config
        from studio.services.production_recipe_stage_snapshots import recipe_delivery_metadata
        self.delivery_author = True
        self.output_layout([self.node("root", "object"),
            self.node("title", "text", "root", property_name="标题", required=True),
            self.node("notes", "text", "root", property_name="说明", required=True, position=1)])
        definition = {"schema_version": 3, "interaction_mode": "assisted", "stages": self.declarations}
        validate_recipe_definition(definition, require_complete=True)
        value = {"标题": " 雨の夜 🌧 ", "说明": "原始\r\n说明"}
        artifact = self.produce(value)
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes")); self.run.save()
        with patch("studio.services.assembly_contracts.parse_structure_text", side_effect=AssertionError("No reparsing")):
            _freeze_stage_config(self.run, self.assembly_stage)
            result = recipe_delivery_metadata(self.assembly_stage)
        self.assertEqual(result["projection"]["output"], {"title": value["标题"], "notes": value["说明"]})
        self.assertEqual(result["selected_source"]["artifact_id"], str(artifact.pk))
        self.assertEqual(result["selected_source"]["selection_mode"], "automatic")
        self.assertIsNone(result["selected_source"]["selected_by"])
        counts = (GenerationAttempt.objects.count(), ArtifactSelectionDecision.objects.count())
        saved = deepcopy(self.assembly_stage.config_snapshot)
        # Isolated signed-document fixture: no media/assembly delivery is claimed.
        from studio.models import ReleaseBundle
        from studio.services.release_bundles import (verified_release_bundle, live_verified_release_bundle,
            ReleaseBundleError, _canonical_sha256, RELEASE_BUNDLE_SIGNING_SALT)
        from studio.tests.test_release_bundles import ReleaseBundleServiceTests, assert_metadata_batch_reads
        from django.core import signing
        bundle = ReleaseBundle.objects.create(production_run=self.run, owner=self.actor, project=self.project,
            created_by=self.actor, version=1, title=result["projection"]["output"]["title"],
            notes=result["projection"]["output"]["notes"], selection={"shots": [], "episodes": [], "series_artifact_id": None})
        ReleaseBundleServiceTests._seal_document(self, bundle)
        body = deepcopy(bundle.manifest); body.pop("integrity")
        body.update(delivery_metadata=result, notes=bundle.notes)
        digest = _canonical_sha256(body)
        bundle.manifest = {**body, "integrity": {"payload_sha256": digest,
            "signature": signing.Signer(salt=RELEASE_BUNDLE_SIGNING_SALT).sign(digest)}}
        bundle.manifest_sha256 = digest; bundle.save(update_fields=("manifest", "manifest_sha256"))
        assert_metadata_batch_reads(self, bundle)
        with patch("studio.services.assembly_author_contracts.output_reference", side_effect=AssertionError("No registry")):
            self.assertEqual(recipe_delivery_metadata(self.assembly_stage), result)
        decision = ArtifactSelectionDecision.objects.get(stage_run=self.steps[0])
        ArtifactSelectionDecision.objects.filter(pk=decision.pk).update(revision=decision.revision + 1)
        self.assertEqual(verified_release_bundle(bundle)["verification"]["status"], "verified")
        with self.assertRaises(ReleaseBundleError):
            live_verified_release_bundle(bundle)
        with self.assertRaises(ValidationError):
            recipe_delivery_metadata(self.assembly_stage)
        self.assembly_stage.refresh_from_db(); self.assertEqual(self.assembly_stage.config_snapshot, saved)
        self.assertEqual(counts, (GenerationAttempt.objects.count(), ArtifactSelectionDecision.objects.count()))
        self.assertFalse(AssemblyJob.objects.exists())

    def projection(self, *, frozen=None):
        return recipe_assembly_output_projection(self.declaration, stage_run=self.assembly_stage,
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"), frozen=frozen)

    def test_formal_author_save_roundtrip_and_reject_future_wrong_hash_dual_source(self):
        self.setup_layout()
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        client = APIClient(); client.force_authenticate(self.actor)
        definition = {"schema_version": 3, "interaction_mode": "assisted",
            "stages": [*deepcopy(self.declarations[:-1]), self.declaration]}
        response = client.post("/api/v1/production-recipes/", {"recipe_key": "assembly-" + uuid4().hex,
            "name": "Isolated upstream assembly author", "definition": definition}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        url = f'/api/v1/production-recipes/{response.data["id"]}/'
        self.assertEqual(client.get(url).data["definition"], definition)
        cases = []
        bad = deepcopy(definition); bad["stages"][-1]["assembly_output"]["source"]["stage_key"] = "assembly"; cases.append(bad)
        bad = deepcopy(definition); bad["stages"][-1]["assembly_output"]["expected_contract_sha256"] = "0" * 64; cases.append(bad)
        bad = deepcopy(definition); bad["stages"][-1]["assembly_output"]["source_text"] = "{}"; cases.append(bad)
        bad = deepcopy(definition); bad["stages"][-1]["assembly_output"]["mapping"]["node"]["fields"].pop("fps"); cases.append(bad)
        for bad in cases:
            result = client.patch(url, {"definition": bad}, format="json")
            self.assertEqual(result.status_code, 400, result.data)
            self.assertEqual(client.get(url).data["definition"], definition)
        validate_recipe_definition(definition, require_complete=True)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(AssemblyJob.objects.exists())

    def author_client(self):
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        client = APIClient()
        client.force_authenticate(self.actor)
        return client

    def author_definition(self, stage):
        return {"schema_version": 3, "interaction_mode": "assisted",
                "stages": [*deepcopy(self.declarations[:-1]), stage]}

    def create_author(self, client, definition):
        return client.post("/api/v1/production-recipes/", {
            "recipe_key": "assembly-" + uuid4().hex,
            "name": "Isolated assembly mapping validation", "definition": definition,
        }, format="json")

    def test_formal_save_rejects_empty_timing_and_incompatible_whole_object_paths(self):
        from studio.models import ProductionRecipeVersion
        self.setup_layout(extra_nodes=[
            self.node("geometry", "object", "root", property_name="画幅", required=True, position=3),
            self.node("bad-width", "text", "geometry", property_name="width", required=True),
            self.node("height", "integer", "geometry", property_name="height", required=True, position=1),
            self.node("fps", "integer", "geometry", property_name="fps", required=True, position=2),
        ])
        client = self.author_client()
        definition = self.author_definition(self.declaration)
        response = self.create_author(client, definition)
        self.assertEqual(response.status_code, 201, response.data)
        url = f'/api/v1/production-recipes/{response.data["id"]}/'
        count = ProductionRecipeVersion.objects.count()
        cases = []
        for node in (field("画幅"), field()):
            bad = deepcopy(definition)
            bad["stages"][-1]["assembly_output"]["mapping"]["node"] = node
            cases.append(bad)
        for node in (obj(episodes={"kind": "array", "items": []}),
                     obj(episodes={"kind": "array", "items": [obj(number=fixed(1), shots={"kind": "array", "items": []})]})):
            bad = deepcopy(definition)
            bad["stages"][-1]["assembly_timing"] = {**deepcopy(self.rule), "mapping": {"schema_version": 1, "node": node}}
            cases.append(bad)
        for bad in cases:
            with self.subTest(mapping=bad["stages"][-1]):
                created = self.create_author(client, bad)
                self.assertEqual(created.status_code, 400, created.data)
                updated = client.patch(url, {"definition": bad}, format="json")
                self.assertEqual(updated.status_code, 400, updated.data)
                self.assertEqual(client.get(url).data["definition"], definition)
                self.assertEqual(ProductionRecipeVersion.objects.count(), count)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GenerationTask.objects.exists())
        self.assertFalse(AssemblyJob.objects.exists())

    def check_timing_paths(self, *, nullable):
        self.setup_layout(extra_nodes=[
            self.node("start", "integer", "root", property_name="起", required=True, allow_null=nullable, position=3),
            self.node("end", "integer", "root", property_name="止", required=True, allow_null=nullable, position=4),
        ])
        timing_node = obj(episodes={"kind": "array", "items": [obj(number=fixed(1), shots={"kind": "array", "items": [obj(
            number=fixed(1), duration_ms=fixed(1250), episode_start_ms=field("起"), episode_end_ms=field("止"))]})]})
        rule = {**deepcopy(self.rule), "mapping": {"schema_version": 1, "node": timing_node}}
        stage = {**self.declaration, "assembly_timing": rule}
        client = self.author_client()
        definition = self.author_definition(stage)
        response = self.create_author(client, definition)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(client.get(f'/api/v1/production-recipes/{response.data["id"]}/').data["definition"], definition)
        self.assertFalse(GenerationAttempt.objects.exists())
        start, end = (None, None) if nullable else (0, 1250)
        raw = {"宽": 640, "高": 360, "率": 30, "起": start, "止": end}
        self.produce(raw)
        counts = (GenerationAttempt.objects.count(), GenerationTask.objects.count(), ArtifactSelectionDecision.objects.count())
        args = dict(key="assembly_timing", stage_run=self.assembly_stage,
                    max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        result = recipe_assembly_output_projection(stage, **args)
        self.assertEqual(result["projection"]["raw_value"], raw)
        self.assertEqual(result["projection"]["output"], {"episodes": [{"number": 1, "shots": [{"number": 1,
            "duration_ms": 1250, "episode_start_ms": start, "episode_end_ms": end}]}]})
        self.assertEqual(result["selected_source"]["selection_mode"], "automatic")
        self.assertIsNone(result["selected_source"]["selected_by"])
        with patch("studio.services.assembly_author_contracts.output_reference", side_effect=AssertionError("No current output")):
            self.assertEqual(recipe_assembly_output_projection(stage, frozen=result, **args), result)
        self.assertEqual(counts, (GenerationAttempt.objects.count(), GenerationTask.objects.count(), ArtifactSelectionDecision.objects.count()))
        self.assertFalse(AssemblyJob.objects.exists())

    def test_dynamic_integer_timing_paths_save_and_freeze_exact_zero(self):
        self.check_timing_paths(nullable=False)

    def test_dynamic_media_identity_maps_exact_selected_value_without_adopting_any_clip(self):
        self.setup_layout(extra_nodes=[self.node("clip", "text", "root", property_name="片段", required=True, position=3)])
        media_node = obj(episodes={"kind": "array", "items": [obj(number=fixed(1), shots={"kind": "array", "items": [obj(
            number=fixed(1), artifact_id=field("片段"))]})]})
        rule = {**deepcopy(self.rule), "mapping": {"schema_version": 1, "node": media_node}}
        stage = {**self.declaration, "assembly_media": rule}
        client = self.author_client(); definition = self.author_definition(stage)
        response = self.create_author(client, definition)
        self.assertEqual(response.status_code, 201, response.data)
        clip_id = str(uuid4())  # identity in synthetic text, not an adopted/available media claim
        artifact = self.produce({"宽": 640, "高": 360, "率": 30, "片段": clip_id})
        counts = (GenerationAttempt.objects.count(), ArtifactSelectionDecision.objects.count())
        args = dict(key="assembly_media", stage_run=self.assembly_stage, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        with patch("studio.services.assembly_contracts.parse_structure_text", side_effect=AssertionError("No reparse")):
            result = recipe_assembly_output_projection(stage, **args)
        self.assertEqual(result["projection"]["output"]["episodes"][0]["shots"][0]["artifact_id"], clip_id)
        self.assertEqual(result["selected_source"]["artifact_id"], str(artifact.pk))
        self.assertEqual(result["selected_source"]["selection_mode"], "automatic")
        self.assertIsNone(result["selected_source"]["selected_by"])
        with patch("studio.services.assembly_author_contracts.output_reference", side_effect=AssertionError("Frozen only")):
            self.assertEqual(recipe_assembly_output_projection(stage, frozen=result, **args), result)
        decision = ArtifactSelectionDecision.objects.get(stage_run=self.steps[0])
        ArtifactSelectionDecision.objects.filter(pk=decision.pk).update(revision=decision.revision + 1)
        with self.assertRaises(ValidationError):
            recipe_assembly_output_projection(stage, frozen=result, **args)
        self.assertEqual(counts, (GenerationAttempt.objects.count(), ArtifactSelectionDecision.objects.count()))
        self.assertFalse(AssemblyJob.objects.exists())

    def test_dynamic_nullable_timing_paths_save_and_freeze_exact_null(self):
        self.check_timing_paths(nullable=True)

    def test_original_stage_configuration_freezes_actual_upstream_and_rejects_changed_choice(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from studio.services.configuration import resolve_configuration
        from studio.services.json_contracts import assign_json_contract
        from studio.services.production_runs import _freeze_stage_config
        from studio.services.production_recipe_stage_snapshots import recipe_assembly_configuration
        self.setup_layout()
        self.produce({"宽": 640, "高": 360, "率": 30})
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        with TemporaryDirectory() as directory:
            font = Path(directory) / "isolated-font.ttf"
            font.write_bytes(b"Not a real font: config-only fixture, encoding forbidden")
            with override_settings(ASSEMBLY_AI_LABEL_FONTFILE=str(font)):
                _freeze_stage_config(self.run, self.assembly_stage)
        saved = deepcopy(self.assembly_stage.config_snapshot)
        self.assertEqual(saved["assembly_configuration"]["width"], 640)
        self.assertEqual(saved["assembly_output"]["selected_source"]["stage_key"], "step-one")
        with patch("studio.services.assembly.resolve_assembly_configuration", side_effect=AssertionError("Frozen config only")):
            self.assertEqual(recipe_assembly_configuration(self.assembly_stage), saved["assembly_configuration"])
        decision = ArtifactSelectionDecision.objects.get(stage_run=self.steps[0])
        ArtifactSelectionDecision.objects.filter(pk=decision.pk).update(revision=decision.revision + 1)
        with self.assertRaises(ValidationError):
            recipe_assembly_configuration(self.assembly_stage)
        self.assembly_stage.refresh_from_db()
        self.assertEqual(self.assembly_stage.config_snapshot, saved)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_actual_selected_value_retains_automatic_identity_and_frozen_projection(self):
        self.setup_layout()
        artifact = self.produce({"宽": 640, "高": 360, "率": 30})
        counts = (GenerationAttempt.objects.count(), GenerationTask.objects.count(), ArtifactSelectionDecision.objects.count())
        result = self.projection()
        self.assertEqual(result["projection"]["output"], {"width": 640, "height": 360, "fps": 30})
        self.assertEqual(result["projection"]["raw_value"], {"宽": 640, "高": 360, "率": 30})
        self.assertEqual(result["selected_source"]["artifact_id"], str(artifact.pk))
        self.assertEqual(result["selected_source"]["selection_mode"], "automatic")
        self.assertIsNone(result["selected_source"]["selected_by"])
        self.assertNotIn("source_text", self.rule)
        with patch("studio.services.assembly_author_contracts.output_reference", side_effect=AssertionError("No current output")):
            self.assertEqual(self.projection(frozen=result), result)
        for location in ("artifact_id", "output_contract_sha256", "structure_version_id"):
            bad = deepcopy(result); bad["selected_source"][location] = "changed"
            with self.assertRaises(ValidationError):
                self.projection(frozen=bad)
        self.assertEqual(counts, (GenerationAttempt.objects.count(), GenerationTask.objects.count(), ArtifactSelectionDecision.objects.count()))
        self.assertFalse(AssemblyJob.objects.exists())

    def test_unfinished_unselected_and_changed_selection_do_not_use_candidate_or_project_defaults(self):
        self.setup_layout()
        with self.assertRaises(ValidationError):
            self.projection()
        self.produce({"宽": 640, "高": 360, "率": 30}, select=False)
        with self.assertRaises(ValidationError):
            self.projection()
        self.complete_first_step()
        frozen = self.projection()
        decision = ArtifactSelectionDecision.objects.get(stage_run=self.steps[0])
        ArtifactSelectionDecision.objects.filter(pk=decision.pk).update(revision=decision.revision + 1)
        with self.assertRaises(ValidationError):
            self.projection(frozen=frozen)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_selected_result_uses_same_timing_mapper_without_reparsing_text(self):
        self.setup_layout()
        self.produce({"宽": 1250, "高": 360, "率": 30})
        # A custom JSON layout can map selected values or explicit literals.
        from studio.tests.test_structure_mapping_preview import fixed
        timing = {"episodes": [{"number": 1, "shots": [{"number": 1,
            "duration_ms": 1250, "episode_start_ms": None, "episode_end_ms": None}]}]}
        rule = {**self.rule, "mapping": {"schema_version": 1, "node": obj(episodes={
            "kind": "array", "items": [obj(number=fixed(1), shots={"kind": "array", "items": [obj(
                number=fixed(1), duration_ms=field("宽"), episode_start_ms=fixed(None),
                episode_end_ms=fixed(None))]})]})}}
        stage = {**self.declarations[-1], "assembly_timing": rule}
        with patch("studio.services.assembly_contracts.parse_structure_text", side_effect=AssertionError("Not authored text")):
            result = recipe_assembly_output_projection(stage, key="assembly_timing", stage_run=self.assembly_stage,
                max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.assertEqual(result["projection"]["output"], timing)
        self.assertEqual(result["selected_source"]["output_version_id"], rule["output_version_id"])
