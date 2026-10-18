"""Per-use token wiring; only the PNG source fixture is physically probed.

Other media are explicit protocol fixtures, not operational Smoke evidence.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.services.execution_workflow_media import project_execution_workflow_media, prepare_execution_workflow_media
from studio.services.workflow_media_tokens import is_workflow_media_use_token
from studio.services.json_contracts import json_contract
from studio.services.workflow_classified_references import materialize_reference_groups
from studio.tests import test_execution_reference_materials as fixtures


class ExecutionWorkflowMediaTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionReferenceMaterialTests.setUpTestData.__func__)
    item = fixtures.ExecutionReferenceMaterialTests.item
    resolve = fixtures.ExecutionReferenceMaterialTests.resolve
    definition = fixtures.ExecutionReferenceMaterialTests.definition
    media_protocol_fixture = fixtures.ExecutionReferenceMaterialTests.media_protocol_fixture

    def setUp(self):
        fixtures.ExecutionReferenceMaterialTests.setUp(self)

    def test_artifact_and_paired_audio_reach_the_declared_ports_without_fake_assets(self):
        definition, resolved = self.media_protocol_fixture()
        tokens = {row["item_key"]: "use:" + row["item_key"] for rows in resolved["logical_values"].values() for row in rows}
        media = project_execution_workflow_media(definition, json_contract(resolved), tokens_by_use=tokens).value
        artifact = next(row for row in media["bindings"] if row["item_key"] == "images-use")
        self.assertIsNone(artifact["asset_id"])
        self.assertEqual(artifact["artifact_id"], resolved["logical_values"]["images"][0]["artifact_id"])
        self.assertEqual(media["classified_groups"]["videos"][0]["paired_audio"], {"kind": "external", "token": tokens["video_audios-use"]})
        contract = {"classified_references": {"target_node_id": "conditioner", "groups": {
            key: {"min_items": target["min_items"], "max_items": target["max_items"], "input_names": target["wiring"]["input_names"]}
            for key, target in definition.value["interface"]["input_targets"].items()}}}
        graph = materialize_reference_groups({"conditioner": {"class_type": "ConfiguredReferenceNode", "inputs": {}}},
            contract, media["classified_groups"])
        self.assertEqual(graph["cr_images_0"]["inputs"]["image"], tokens["images-use"])
        self.assertEqual(graph["cr_paired_audio_0"]["inputs"]["audio"], tokens["video_audios-use"])
        self.assertEqual(graph["conditioner"]["inputs"]["video_audios.0"], ["cr_paired_audio_0", 0])

    def test_repeated_source_keeps_distinct_uses_in_exact_input_order(self):
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        resolved = self.resolve([self.item(item_key="second"), self.item(item_key="first")])
        media = project_execution_workflow_media(self.definition(), json_contract(resolved),
            tokens_by_use={"first": "use:first", "second": "use:second"}).value
        self.assertEqual(media["classified_groups"]["images"], [{"token": "use:second"}, {"token": "use:first"}])
        self.assertEqual([row["item_key"] for row in media["bindings"]], ["second", "first"])
        with self.assertRaisesMessage(ValidationError, "逐使用提供唯一token"):
            project_execution_workflow_media(self.definition(), json_contract(resolved), tokens_by_use={"first": "same", "second": "same"})

    def test_placeholder_preparation_is_deterministic_and_keeps_original_source_identity(self):
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        resolved = json_contract(self.resolve([self.item(item_key="second"), self.item(item_key="first")]))
        media = prepare_execution_workflow_media(self.definition(), resolved)
        self.assertEqual(media, prepare_execution_workflow_media(self.definition(), resolved))
        tokens = [row["token"] for row in media.value["bindings"]]
        self.assertEqual(len(set(tokens)), 2)
        self.assertTrue(all(is_workflow_media_use_token(token) for token in tokens))
        self.assertEqual({row["asset_id"] for row in media.value["bindings"]}, {str(self.asset.pk)})
        self.assertEqual(media.value["resolved_inputs_sha256"], resolved.sha256)

    def test_token_map_must_cover_exactly_the_actual_graph_uses(self):
        resolved = self.resolve()
        key = resolved["logical_values"]["references"][0]["item_key"]
        for tokens in ({}, {key: "use:actual", "unrelated": "use:extra"}, {key: ""}):
            with self.subTest(tokens=tokens), self.assertRaises(ValidationError):
                project_execution_workflow_media(self.definition(), json_contract(resolved), tokens_by_use=tokens)

    def test_fixed_semantic_port_is_not_guessed_to_be_a_prompt_reference_role(self):
        self.target.update(max_items=1, wiring={"kind": "semantic", "semantic_key": "asset.image.source"})
        resolved = self.resolve()
        key = resolved["logical_values"]["references"][0]["item_key"]
        media = project_execution_workflow_media(self.definition(), json_contract(resolved), tokens_by_use={key: "use:fixed"}).value
        self.assertIsNone(media["classified_groups"])
        self.assertEqual(media["semantic_inputs"], {"asset.image.source": {"item_key": key, "token": "use:fixed"}})
        self.assertNotIn("input_label", str(media))

    def test_context_only_material_is_not_uploaded_as_an_execution_input(self):
        spec = deepcopy(self.spec)
        spec.update(key="context_photo", targets=[])
        definition = self.definition(specs=[self.spec, spec])
        for row in definition.value["configuration"]["inputs"]:
            row["semantic_contract"]["allow_repeated_asset"] = True
        definition = json_contract(definition.value)
        resolved = self.resolve(definition=definition, sources={"references": {"items": [self.item(item_key="primary")]},
            "context_photo": {"items": [self.item(item_key="context-only")]}})
        media = project_execution_workflow_media(definition, json_contract(resolved), tokens_by_use={"primary": "use:primary"}).value
        self.assertEqual(media["context_only_item_keys"], ["context-only"])
        self.assertEqual(len(media["bindings"]), 1)
