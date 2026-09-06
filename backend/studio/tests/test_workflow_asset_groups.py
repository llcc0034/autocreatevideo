from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from django.test import TestCase

from studio.models import Asset, WorkflowProjector, WorkflowProjectorVersion
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.workflow_asset_groups import (
    AssetGroupError, build_group_plans, expand_group_graph, materialize_group_graph,
    validate_group_selection, recommend_group_definition,
)
from studio.services.workflow_projectors import (
    current_projector_catalog, validate_workflow_projector_definition, WorkflowProjectorError, validate_frozen_workflow_projector_contract,
    WORKFLOW_PROJECTOR_ENGINE_ADAPTER_CONTRACTS,
)
from studio.services.comfyui_workflow_imports import (
    _asset_dependency_candidates, _suggested_import_contract, _sanitized_import_graph,
    _validate_saved_import_structural_contract,
)
from studio.services.comfyui_contract import validate_comfyui_api_contract
from studio.services.workflow_compiler import compile_frozen_workflow, workflow_execution_contract
from studio.services.workflow_smoke_fixtures import workflow_execution_input_schema, workflow_smoke_fixture_schema
from studio.platform_serializers import WorkflowExecutionInputSchemaSerializer


class GenericAssetGroupTests(TestCase):
    def setUp(self):
        approved = patch("studio.services.workflow._extra_allowed_node_types", return_value={"PrepareReference", "MultiReferenceEncoder", "MergeImages"})
        approved.start()
        self.addCleanup(approved.stop)
        self.graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "fixture.png"}},
            "2": {"class_type": "PrepareReference", "inputs": {"image": ["1", 0], "strength": 1.0}},
            "3": {"class_type": "MultiReferenceEncoder", "inputs": {"image1": ["2", 0]}},
            "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "fixture"}},
        }
        self.info = {
            "LoadImage": {"input": {"required": {"image": [["fixture.png"], {"image_upload": True}]}}, "output": ["IMAGE", "MASK"]},
            "PrepareReference": {"input": {"required": {"image": ["IMAGE"], "strength": ["FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0}]}}, "output": ["IMAGE"]},
            "MultiReferenceEncoder": {"input": {"required": {"image1": ["IMAGE"]}, "optional": {"image2": ["IMAGE"], "image3": ["IMAGE"]}}, "output": ["IMAGE"]},
            "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING", {"default": "fixture"}]}}, "output": [], "output_node": True},
        }
        self.base = next(row for row in current_projector_catalog() if row.key == "generic.comfy.fixed")
        self.definition = deepcopy(self.base.definition)
        self.definition.update(
            detector={"fallback": False, "allow_other_nodes": False, "node_counts": {row["class_type"]: 1 for row in self.graph.values()}, "optional_node_counts": {}, "input_equals": []},
            node_roles={"source": {"node_type": "LoadImage", "cardinality": "one"}, "prepare": {"node_type": "PrepareReference", "cardinality": "one"}, "target": {"node_type": "MultiReferenceEncoder", "cardinality": "one"}},
            required_edges=[], parameter_policies=[], model_selectors=[],
            asset_slots=[{"key": "references", "media_kind": "image", "dynamic": True, "contract_source": "workflow"}],
            adapters=deepcopy(WORKFLOW_PROJECTOR_ENGINE_ADAPTER_CONTRACTS["generic.references"]),
            projection={"kind": "generic_references", "config": {"groups": [{
                "key": "references", "source_role": "source", "branch_roles": ["source", "prepare"],
                "output_role": "prepare", "output_index": 0, "target_roles": ["target"],
                "input_names": ["image1", "image2", "image3"], "wiring": "ordered_ports",
                "reducer_role": "", "reducer_inputs": [], "reducer_output_index": 0,
                "minimum_items": 1, "maximum_items": 3, "conditioning_mode": "", "model_input": "",
            }]}},
        )

    @property
    def group(self):
        return self.definition["projection"]["config"]["groups"][0]

    def project(self):
        definition = validate_workflow_projector_definition(self.definition, engine_key="generic.references", engine_version=1)
        family = WorkflowProjector.objects.create(key=f"test.references.{uuid4().hex}", name="Test references")
        version = WorkflowProjectorVersion(projector=family, version=1, provider_driver_type="comfyui", operation="workflow.import", engine_key="generic.references", engine_version=1, status="published", is_current=True)
        assign_json_contract(version, "definition", definition)
        version.save()
        projector = replace(self.base, key=family.key, version_id=str(version.pk), definition_sha256=version.definition_sha256, engine_key="generic.references", definition=definition)
        contract, bindings, requirements, blockers, _parameters = _suggested_import_contract(
            self.graph, self.info, primary_api_node_ids=["4"], model_dependencies=[],
            asset_dependencies=_asset_dependency_candidates(self.graph, self.info),
            model_profile_version=None, projector=projector,
        )
        item = {"input_contract": contract, "semantic_bindings": bindings, "model_requirements": requirements}
        frozen = _sanitized_import_graph(self.graph, self.info, item=item)
        _validate_saved_import_structural_contract(frozen, self.info, item=item)
        validate_frozen_workflow_projector_contract(contract)
        graph_sha = json_contract(frozen, max_bytes=10**7).sha256
        input_sha = json_contract(contract, max_bytes=10**7).sha256
        payload, execution_sha = workflow_execution_contract(compiler_id="generic.comfy", compiler_version=2, graph_sha256=graph_sha, input_contract_sha256=input_sha, bindings=bindings, requirements=requirements)
        return {**item, "graph": frozen, "graph_sha256": graph_sha, "input_contract_sha256": input_sha, "execution_contract_sha256": execution_sha, "model_identifier": "fixture", "compiler_id": "generic.comfy", "compiler_version": 2, "semantic_bindings_sha256": payload["semantic_bindings_sha256"], "model_requirements_sha256": payload["model_requirements_sha256"]}, blockers

    def test_import_freezes_complete_max_graph_and_optional_slots(self):
        source = deepcopy(self.graph)
        snapshot, _ = self.project()
        self.assertEqual(self.graph, source)
        self.assertEqual(snapshot["input_contract"]["compiler"], {"id": "generic.comfy", "version": 2})
        group = snapshot["input_contract"]["asset_groups"][0]
        self.assertEqual(len(group["semantic_keys"]), 3)
        self.assertEqual([snapshot["input_contract"]["parameters"][key]["required"] for key in group["semantic_keys"]], [True, False, False])
        self.assertEqual(len(snapshot["graph"]), 8)
        self.assertNotIn("fixture.png", str(snapshot["graph"]))

    def test_smoke_and_production_serialize_identical_groups_without_frozen_scalars(self):
        snapshot, _ = self.project()
        version = SimpleNamespace(input_contract=snapshot["input_contract"], compiler_id="generic.comfy", compiler_version=2,
            semantic_bindings=SimpleNamespace(values=lambda *args: snapshot["semantic_bindings"]),
            template=SimpleNamespace(output_kind="image"), model_profile_version_id=None, model_profile_version=None)
        smoke = workflow_smoke_fixture_schema(version)
        production = workflow_execution_input_schema(version)
        self.assertEqual(smoke["asset_groups"], production["asset_groups"])
        serialized = WorkflowExecutionInputSchemaSerializer(production).data
        self.assertEqual(serialized["asset_groups"][0]["max_items"], 3)
        constants = {key for key, value in snapshot["input_contract"]["parameters"].items() if value.get("asset_group_constant")}
        self.assertTrue(constants)
        self.assertFalse(constants & {row["semantic_key"] for row in smoke["parameters"]})
        self.assertFalse(constants & {row["semantic_key"] for row in production["parameters"]})

    def test_frozen_contract_rejects_drift_from_reviewed_projector(self):
        snapshot, _ = self.project()
        contract = deepcopy(snapshot["input_contract"])
        contract["asset_groups"][0]["input_names"][1] = "unreviewed_port"
        with self.assertRaises(WorkflowProjectorError):
            validate_frozen_workflow_projector_contract(contract)

    def test_quantity_can_come_from_exact_model_contract(self):
        from studio.tests.factories import conditioning_contract
        self.definition["asset_slots"][0]["contract_source"] = "model_profile"
        self.group.update(minimum_items=None, maximum_items=None, conditioning_mode="Ref2VA", model_input="entity_images")
        specification = {"conditioning_contract": conditioning_contract("Ref2VA", input_kind="classified_references", entity_images=(1, 3))}
        with patch("studio.services.model_profile_contracts.verified_model_profile_specification", return_value=specification):
            plans = build_group_plans(self.definition, self.graph, self.info, object())
        self.assertEqual((plans[0]["min_items"], plans[0]["max_items"]), (1, 3))
        with self.assertRaisesRegex(AssetGroupError, "精确模型"):
            build_group_plans(self.definition, self.graph, self.info, None)

    def test_runtime_counts_one_two_three_compile_real_semantics(self):
        self.graph["3"]["inputs"].update(width=512, height=512, seed=1)
        self.info["MultiReferenceEncoder"]["input"]["required"].update(
            width=["INT", {"default": 512, "min": 64, "max": 2048, "step": 64}],
            height=["INT", {"default": 512, "min": 64, "max": 2048, "step": 64}],
            seed=["INT", {"default": 1, "min": 0, "max": 100000}],
        )
        snapshot, _ = self.project()
        contract = snapshot["input_contract"]
        keys = contract["asset_groups"][0]["semantic_keys"]
        assets = [Asset.objects.create(name=f"ref-{index}", kind=Asset.Kind.REFERENCE, mime_type="image/png", checksum=str(index+1)*64) for index in range(3)]
        project = SimpleNamespace(id="test", owner_id=None, continuity_seed=1, aspect_ratio="1:1", width=512, height=512, fps=24)
        for count in (1, 2, 3):
            with self.subTest(count=count):
                references = [{"semantic_key": key, "input_slot": contract["parameters"][key]["input_slot"], "asset_id": str(asset.id), "checksum": asset.checksum, "category": "image"} for key, asset in zip(keys[:count], assets)]
                prompt = SimpleNamespace(id="test", prompt_type="image", project=project, prompt_text="test", negative_prompt="", include_audio=False, total_duration_ms=1000, structured_payload={"reference_bindings": references})
                compiled = compile_frozen_workflow(workflow_snapshot=snapshot, prompt=prompt, project=project)
                self.assertEqual(sum(node["class_type"] == "LoadImage" for node in compiled.graph.values()), count)
                self.assertEqual(len(compiled.graph["3"]["inputs"]), count + 3)
                self.assertEqual(len(compiled.asset_bindings), count)

    def test_rejects_skipped_position_and_below_minimum(self):
        snapshot, _ = self.project()
        contract = snapshot["input_contract"]
        keys = contract["asset_groups"][0]["semantic_keys"]
        for selected in (set(), {keys[1]}, {keys[0], keys[2]}):
            with self.subTest(selected=selected), self.assertRaises(AssetGroupError):
                validate_group_selection(contract, selected)

    def test_zero_count_prunes_all_template_nodes(self):
        self.info["MultiReferenceEncoder"]["input"]["optional"]["image1"] = self.info["MultiReferenceEncoder"]["input"]["required"].pop("image1")
        self.group["minimum_items"] = 0
        snapshot, _ = self.project()
        result = materialize_group_graph(snapshot["graph"], snapshot["input_contract"], set())
        self.assertEqual(set(result), {"3", "4"})
        self.assertEqual(result["3"]["inputs"], {})

    def test_live_schema_preflight_accepts_all_declared_cardinalities(self):
        snapshot, _ = self.project()
        contract = snapshot["input_contract"]
        for count in (1, 2, 3):
            with self.subTest(count=count):
                graph = materialize_group_graph(snapshot["graph"], contract, set(contract["asset_groups"][0]["semantic_keys"][:count]))
                report = validate_comfyui_api_contract(graph, self.info, allow_managed_asset_tokens=True)
                self.assertIsNotNone(report.schema_sha256)

    def test_multiple_same_type_consumers_share_ordered_group(self):
        self.graph["5"] = deepcopy(self.graph["3"])
        self.definition["node_roles"]["target"]["cardinality"] = "many"
        self.definition["detector"]["node_counts"]["MultiReferenceEncoder"] = 2
        snapshot, _ = self.project()
        self.assertEqual(snapshot["graph"]["3"], snapshot["graph"]["5"])

    def test_reduce_fold_bypasses_single_and_prunes_unused_reducers(self):
        self.graph["6"] = {"class_type": "MergeImages", "inputs": {"left": ["2", 0], "right": ["2", 0]}}
        self.graph["3"]["inputs"] = {"image1": ["6", 0]}
        self.info["MergeImages"] = {"input": {"required": {"left": ["IMAGE"], "right": ["IMAGE"]}}, "output": ["IMAGE"]}
        self.definition["node_roles"]["merge"] = {"node_type": "MergeImages", "cardinality": "one"}
        self.definition["detector"]["node_counts"]["MergeImages"] = 1
        self.group.update(wiring="reduce", input_names=["image1"], reducer_role="merge", reducer_inputs=["left", "right"])
        snapshot, _ = self.project()
        contract = snapshot["input_contract"]
        for count in (1, 2, 3):
            result = materialize_group_graph(snapshot["graph"], contract, set(contract["asset_groups"][0]["semantic_keys"][:count]))
            self.assertEqual(sum(row["class_type"] == "MergeImages" for row in result.values()), count-1)
            if count == 1:
                self.assertEqual(result["3"]["inputs"]["image1"], ["2", 0])

    def test_schema_required_port_and_occupied_port_fail_closed(self):
        self.info["MultiReferenceEncoder"]["input"]["required"]["image3"] = self.info["MultiReferenceEncoder"]["input"]["optional"].pop("image3")
        with self.assertRaisesRegex(AssetGroupError, "可选"):
            build_group_plans(self.definition, self.graph, self.info, None)
        self.info["MultiReferenceEncoder"]["input"]["optional"]["image3"] = self.info["MultiReferenceEncoder"]["input"]["required"].pop("image3")
        self.graph["3"]["inputs"]["image3"] = ["1", 0]
        with self.assertRaisesRegex(AssetGroupError, "已有其他来源"):
            build_group_plans(self.definition, self.graph, self.info, None)

    def test_shared_external_branch_consumer_fails_closed(self):
        self.graph["4"]["inputs"]["images"] = ["2", 0]
        with self.assertRaisesRegex(AssetGroupError, "组外依赖"):
            build_group_plans(self.definition, self.graph, self.info, None)

    def test_node_budget_and_id_collision_fail_closed(self):
        with patch("studio.services.workflow_asset_groups.boundary_int", side_effect=lambda key: 5 if key.endswith("max_nodes") else 128):
            with self.assertRaisesRegex(AssetGroupError, "预算"):
                build_group_plans(self.definition, self.graph, self.info, None)
        self.graph["ag_references_1_1"] = deepcopy(self.graph["1"])
        with self.assertRaises(AssetGroupError):
            build_group_plans(self.definition, self.graph, self.info, None)

    def test_recommendation_is_structural_and_unsaved(self):
        source = deepcopy(self.definition)
        recommendation = recommend_group_definition(source, self.graph, self.info, _asset_dependency_candidates(self.graph, self.info))
        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation["projection"]["config"]["groups"][0]["maximum_items"], 3)
        self.assertEqual(source, self.definition)
        self.assertIsNone(recommend_group_definition(source, self.graph, self.info, []))

    def test_unknown_wiring_and_unbounded_contract_are_rejected(self):
        for key, value in (("wiring", "eval"), ("maximum_items", True), ("maximum_items", 10**9)):
            invalid = deepcopy(self.definition)
            invalid["projection"]["config"]["groups"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(WorkflowProjectorError):
                validate_workflow_projector_definition(invalid, engine_key="generic.references", engine_version=1)
