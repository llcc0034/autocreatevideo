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
from studio.services.workflow_compiler import compile_frozen_workflow_values, workflow_execution_contract
from studio.services.workflow_smoke_fixtures import workflow_execution_input_schema
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

    def project(self, model_profile=None):
        definition = validate_workflow_projector_definition(self.definition, engine_key="generic.references", engine_version=1)
        family = WorkflowProjector.objects.create(key=f"test.references.{uuid4().hex}", name="Test references")
        version = WorkflowProjectorVersion(projector=family, version=1, provider_driver_type="comfyui", operation="workflow.import", engine_key="generic.references", engine_version=1, status="published", is_current=True)
        assign_json_contract(version, "definition", definition)
        version.save()
        projector = replace(self.base, key=family.key, version_id=str(version.pk), definition_sha256=version.definition_sha256, engine_key="generic.references", definition=definition)
        contract, bindings, requirements, blockers, _parameters = _suggested_import_contract(
            self.graph, self.info, primary_api_node_ids=["4"], model_dependencies=[],
            asset_dependencies=_asset_dependency_candidates(self.graph, self.info),
            model_profile_version=model_profile, projector=projector,
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

    def test_workflow_input_schema_preserves_projected_groups_without_frozen_scalars(self):
        snapshot, _ = self.project()
        version = SimpleNamespace(input_contract=snapshot["input_contract"], compiler_id="generic.comfy", compiler_version=2,
            semantic_bindings=SimpleNamespace(values=lambda *args: snapshot["semantic_bindings"]),
            template=SimpleNamespace(output_kind="image"), model_profile_version_id=None, model_profile_version=None)
        from studio.services.workflow_asset_groups import group_form_schema
        production = workflow_execution_input_schema(version)
        self.assertEqual(production["asset_groups"], group_form_schema(snapshot["input_contract"]))
        serialized = WorkflowExecutionInputSchemaSerializer(production).data
        self.assertEqual(serialized["asset_groups"][0]["max_items"], 3)
        constants = {key for key, value in snapshot["input_contract"]["parameters"].items() if value.get("asset_group_constant")}
        self.assertTrue(constants)
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

    def test_model_quantity_input_must_match_media_kind_before_graph_expansion(self):
        self.definition["asset_slots"][0]["contract_source"] = "model_profile"
        self.group.update(minimum_items=None, maximum_items=None, conditioning_mode="Ref2VA")
        before = deepcopy(self.graph)
        for key in ("reference_videos", "paired_video_audios", "reference_audios", "unregistered"):
            with self.subTest(key=key):
                self.group["model_input"] = key
                with self.assertRaisesRegex(WorkflowProjectorError, "数量输入.*媒体类型"):
                    validate_workflow_projector_definition(self.definition, engine_key="generic.references", engine_version=1)
                with self.assertRaisesRegex(AssetGroupError, "数量输入.*媒体类型"):
                    build_group_plans(self.definition, self.graph, self.info, None)
        self.assertEqual(self.graph, before)

    def model_quantity_fixture(self, **inputs):
        from studio.tests.factories import conditioning_contract
        self.definition["asset_slots"][0]["contract_source"] = "model_profile"
        self.group.update(minimum_items=None, maximum_items=None, conditioning_mode="Ref2VA", model_input="entity_images")
        profile = SimpleNamespace()
        assign_json_contract(profile, "specification", {"conditioning_contract": conditioning_contract(
            "Ref2VA", input_kind="classified_references", entity_images=(1, 3), **inputs)})
        return profile

    def add_second_image_group(self, *, model_input="entity_images", maximum=3):
        for original, new, suffix in (("1", "6", "source2"), ("2", "7", "prepare2")):
            node = deepcopy(self.graph[original])
            node["class_type"] += "Second"
            if original == "2":
                node["inputs"]["image"] = ["6", 0]
            self.graph[new] = node
            self.info[node["class_type"]] = deepcopy(self.info[self.graph[original]["class_type"]])
            self.definition["node_roles"][suffix] = {"node_type": node["class_type"], "cardinality": "one"}
            self.definition["detector"]["node_counts"][node["class_type"]] = 1
        ports = [f"extra{i + 1}" for i in range(maximum)]
        self.info["MultiReferenceEncoder"]["input"]["optional"].update({port: ["IMAGE"] for port in ports})
        self.graph["3"]["inputs"][ports[0]] = ["7", 0]
        self.definition["asset_slots"].append({"key": "extra", "media_kind": "image", "dynamic": True, "contract_source": "model_profile"})
        self.definition["projection"]["config"]["groups"].append({**deepcopy(self.group),
            "key": "extra", "source_role": "source2", "branch_roles": ["source2", "prepare2"],
            "output_role": "prepare2", "input_names": ports, "model_input": model_input})

    def test_same_target_cannot_multiply_one_model_input_capacity(self):
        profile = self.model_quantity_fixture()
        self.add_second_image_group()
        before = deepcopy(self.graph)
        with self.assertRaisesRegex(AssetGroupError, "3.*references.*extra.*entity_images"):
            build_group_plans(self.definition, self.graph, self.info, profile)
        self.assertEqual(self.graph, before)

    def test_same_target_image_sum_cannot_erase_model_total_bounds(self):
        profile = self.model_quantity_fixture(first_frame=(1, 1))
        self.add_second_image_group(model_input="first_frame", maximum=1)
        for total in ((2, 3), (3, 4)):
            with self.subTest(total=total):
                specification = deepcopy(profile.specification)
                specification["conditioning_contract"]["modes"][0]["inputs"]["total_images"] = {
                    "minimum_items": total[0], "maximum_items": total[1]}
                assign_json_contract(profile, "specification", specification)
                with self.assertRaisesRegex(AssetGroupError, "3.*2–4.*total_images"):
                    build_group_plans(self.definition, self.graph, self.info, profile)

    def test_frozen_model_quantity_cannot_be_removed_or_forged(self):
        profile = self.model_quantity_fixture()
        snapshot, _ = self.project(profile)
        contract = snapshot["input_contract"]
        self.assertIn("model_quantity", contract["asset_groups"][0])
        for change in (lambda row: row.pop("model_quantity"),
                       lambda row: row["model_quantity"].update(model_input="reference_audios"),
                       lambda row: row["model_quantity"].update(conditioning_mode="other")):
            changed = deepcopy(contract)
            change(changed["asset_groups"][0])
            with self.assertRaises(WorkflowProjectorError):
                validate_frozen_workflow_projector_contract(changed)

    def test_shared_quantity_rejects_mixed_sources_modes_and_aggregate_alias(self):
        profile = self.model_quantity_fixture(first_frame=(1, 1))
        self.add_second_image_group(model_input="first_frame", maximum=1)
        original = deepcopy(self.definition)
        for case, message in (("workflow", "混用模型"), ("mode", "不同模型模式"), ("total", "总量和分项")):
            with self.subTest(case=case):
                self.definition = deepcopy(original)
                extra = self.definition["projection"]["config"]["groups"][1]
                specification = deepcopy(profile.specification)
                if case == "workflow":
                    self.definition["asset_slots"][1]["contract_source"] = "workflow"
                    extra.update(minimum_items=1, maximum_items=1, conditioning_mode="", model_input="")
                elif case == "mode":
                    mode = deepcopy(specification["conditioning_contract"]["modes"][0])
                    mode["key"] = "Other"
                    specification["conditioning_contract"]["modes"].append(mode)
                    extra["conditioning_mode"] = "Other"
                else:
                    self.group["model_input"] = "total_images"
                    # The individual 1..3 range remains valid, but it must not
                    # be counted alongside its first_frame component.
                    specification["conditioning_contract"]["modes"][0]["inputs"]["total_images"] = {"minimum_items": 2, "maximum_items": 3}
                changed_profile = SimpleNamespace()
                assign_json_contract(changed_profile, "specification", specification)
                with self.assertRaisesRegex(AssetGroupError, message):
                    build_group_plans(self.definition, self.graph, self.info, changed_profile)

    def test_model_quantity_fanout_counts_once_per_target(self):
        profile = self.model_quantity_fixture()
        self.graph["5"] = deepcopy(self.graph["3"])
        self.definition["node_roles"]["target"]["cardinality"] = "many"
        self.definition["detector"]["node_counts"]["MultiReferenceEncoder"] = 2
        snapshot, _ = self.project(profile)
        plan = snapshot["input_contract"]["asset_groups"][0]
        self.assertEqual(plan["target_nodes"], ["3", "5"])
        for count in (1, 2, 3):
            graph = materialize_group_graph(snapshot["graph"], snapshot["input_contract"], set(plan["semantic_keys"][:count]))
            self.assertEqual(graph["3"], graph["5"])
            self.assertEqual(len(graph["3"]["inputs"]), count)

    def test_distinct_inputs_within_total_and_separate_receivers_remain_valid(self):
        profile = self.model_quantity_fixture(first_frame=(1, 1))
        self.add_second_image_group(model_input="first_frame", maximum=1)
        before = deepcopy(profile.specification)
        plans = build_group_plans(self.definition, self.graph, self.info, profile)
        self.assertEqual([(row["min_items"], row["max_items"]) for row in plans], [(1, 3), (1, 1)])
        self.assertEqual(profile.specification, before)
        # Same capacity at another receiver is not a single shared input. The
        # generic engine must not infer downstream model sharing from a name.
        self.definition["projection"]["config"]["groups"].pop()
        self.definition["asset_slots"].pop()
        self.graph.pop("6")
        self.graph.pop("7")
        self.graph["3"]["inputs"].pop("extra1")
        self.add_second_image_group()
        extra = self.definition["projection"]["config"]["groups"][1]
        self.graph["8"] = {"class_type": "SeparateReceiver", "inputs": {"extra1": self.graph["3"]["inputs"].pop("extra1")}}
        self.info["SeparateReceiver"] = {"input": {"optional": {key: ["IMAGE"] for key in extra["input_names"]}}, "output": ["IMAGE"]}
        self.definition["node_roles"]["separate"] = {"node_type": "SeparateReceiver", "cardinality": "one"}
        extra["target_roles"] = ["separate"]
        # Use a mode that has no omitted mandatory first frame.
        profile = self.model_quantity_fixture()
        plans = build_group_plans(self.definition, self.graph, self.info, profile)
        self.assertEqual([row["target_nodes"] for row in plans], [["3"], ["8"]])

    def test_model_quantity_is_rechecked_for_import_publish_and_final_input_range(self):
        from django.core.exceptions import ValidationError
        from studio.services.workflow_versions import _validate_projector_model_definition
        from studio.services.execution_inputs import workflow_input_targets, allocate_workflow_input_slots
        from studio.services.workflow_asset_groups import group_form_schema
        from studio.services.workflow_compiler import WorkflowCompilerError, validate_frozen_workflow_snapshot

        profile = self.model_quantity_fixture()
        snapshot, _ = self.project(profile)
        contract = snapshot["input_contract"]
        _validate_projector_model_definition(snapshot["graph"], contract, profile.specification, [], [])
        changed = deepcopy(contract)
        changed["asset_groups"][0]["model_quantity"]["total_images"]["maximum_items"] = 4
        with self.assertRaisesRegex(ValidationError, "精确模型版本"):
            _validate_projector_model_definition(snapshot["graph"], changed, profile.specification, [], [])
        with self.assertRaises(ValidationError):
            _validate_projector_model_definition(snapshot["graph"], contract, None, [], [])
        version = SimpleNamespace(input_contract=contract, semantic_bindings=SimpleNamespace(values=lambda *args: snapshot["semantic_bindings"]))
        targets = workflow_input_targets(version)
        self.assertEqual((targets["references"]["min_items"], targets["references"]["max_items"]), (1, 3))
        for count in (0, 4):
            with self.assertRaisesRegex(ValidationError, "1–3"):
                allocate_workflow_input_slots(targets, {"references": [{}] * count})
        changed = deepcopy(contract)
        changed["asset_groups"][0]["model_quantity"]["total_images"]["maximum_items"] = 2
        with self.assertRaisesRegex(ValidationError, "total_images"):
            group_form_schema(changed)
        with self.assertRaisesRegex(WorkflowCompilerError, "total_images"):
            validate_frozen_workflow_snapshot({**snapshot, "input_contract": changed})

    def test_import_rejects_model_quantity_drift_without_writing_version(self):
        from django.core.exceptions import ValidationError
        from studio.models import ProviderProfile, ModelProfileVersion, WorkflowTemplate, WorkflowTemplateVersion, PlatformLifecycleAuditEvent
        from studio.services.workflow_versions import import_workflow_version, verify_workflow_version_integrity

        self.graph["3"]["inputs"].update(width=512, height=512)
        self.info["MultiReferenceEncoder"]["input"]["required"].update(
            width=["INT", {"default": 512, "min": 64, "max": 2048, "step": 64}],
            height=["INT", {"default": 512, "min": 64, "max": 2048, "step": 64}],
        )
        fixture = self.model_quantity_fixture()
        provider = ProviderProfile.objects.create(key="group-model", name="Group model", driver_type="comfyui")
        profile = ModelProfileVersion(profile_key="group-model", provider=provider, version=1,
            model_identifier="group-model", modality="image", status="published")
        assign_json_contract(profile, "specification", fixture.specification)
        profile.save()
        snapshot, _ = self.project(profile)
        template = WorkflowTemplate.objects.create(slug="group-import", name="Group import", output_kind="image")
        request = {"template": template, "provider": provider, "model_profile_version": profile,
            **{key: snapshot[key] for key in ("graph", "input_contract", "semantic_bindings", "model_requirements")}}
        original = deepcopy(snapshot["input_contract"])
        before = PlatformLifecycleAuditEvent.objects.count()
        changed = deepcopy(original)
        changed["asset_groups"][0]["model_quantity"]["total_images"]["maximum_items"] = 4
        with self.assertRaisesRegex(ValidationError, "精确模型版本"):
            import_workflow_version(**{**request, "input_contract": changed})
        self.assertFalse(WorkflowTemplateVersion.objects.filter(template=template).exists())
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)
        version = import_workflow_version(**request)
        verify_workflow_version_integrity(version)
        self.assertEqual(version.input_contract, original)
        # Even a coherently rehashed out-of-band range must not bypass the
        # selected model's capability source on subsequent integrity checks.
        assign_json_contract(version, "input_contract", changed)
        _, version.execution_contract_sha256 = workflow_execution_contract(
            compiler_id=version.compiler_id, compiler_version=version.compiler_version,
            graph_sha256=version.graph_sha256, input_contract_sha256=version.input_contract_sha256,
            bindings=snapshot["semantic_bindings"], requirements=snapshot["model_requirements"])
        with self.assertRaisesRegex(ValidationError, "精确模型版本"):
            verify_workflow_version_integrity(version)
        version.refresh_from_db()
        self.assertEqual(version.input_contract, original)

    def test_media_mismatch_api_rejects_create_and_patch_without_partial_write(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from studio.models import PlatformLifecycleAuditEvent
        from studio.services.workflow_projectors import workflow_projector_creation_template

        client = APIClient()
        client.force_authenticate(get_user_model().objects.create_superuser(username="quantity-admin", password="test-password"))
        family = WorkflowProjector.objects.create(key="test.quantity.api", name="Quantity API")
        payload = {**workflow_projector_creation_template("generic.references"),
            "engine_key": "generic.references", "engine_version": 1, "projector": str(family.pk),
            "definition": self.definition}
        self.definition["asset_slots"][0]["contract_source"] = "model_profile"
        self.group.update(minimum_items=None, maximum_items=None, conditioning_mode="Ref2VA", model_input="reference_audios")
        before = PlatformLifecycleAuditEvent.objects.count()
        rejected = client.post("/api/v1/workflow-projector-versions/", payload, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("媒体类型", str(rejected.data))
        self.assertFalse(family.versions.exists())
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)
        self.group["model_input"] = "entity_images"
        created = client.post("/api/v1/workflow-projector-versions/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        version = family.versions.get()
        saved_sha = version.definition_sha256
        before = PlatformLifecycleAuditEvent.objects.count()
        self.group["model_input"] = "reference_audios"
        rejected = client.patch(f"/api/v1/workflow-projector-versions/{version.pk}/", {"definition": self.definition}, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        version.refresh_from_db()
        self.assertEqual(version.definition_sha256, saved_sha)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)

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
        from studio.services.workflow_media_tokens import workflow_media_use_token
        values = {key: definition["default"] for key, definition in contract["parameters"].items()
            if definition.get("asset_group_constant")}
        values.update({"image.width": 512, "image.height": 512, "sampling.seed": 1, "output.prefix": "test/group"})
        for count in (1, 2, 3):
            with self.subTest(count=count):
                media = {key: {"semantic_key": key, "item_key": key, "kind": "image",
                    "asset_id": str(asset.pk), "artifact_id": None, "source_sha256": asset.checksum,
                    "token": workflow_media_use_token(item_key=key, source_sha256=asset.checksum)}
                    for key, asset in zip(keys[:count], assets)}
                def resolve(key, _rule):
                    item = media.get(key)
                    return (None, None) if item is None else (item["token"], item)
                compiled = compile_frozen_workflow_values(workflow_snapshot=snapshot, scalar_values=values,
                    resolve_asset=resolve, classified_groups=None, classified_bindings=[],
                    output_kind="image", total_duration_ms=None)
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
