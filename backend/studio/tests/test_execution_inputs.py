from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.execution_inputs import allocate_workflow_input_slots, workflow_input_targets


class ExecutionInputProjectionTests(SimpleTestCase):
    def setUp(self):
        guard = patch("studio.services.execution_instances.boundary_int", return_value=65536)
        guard.start()
        self.addCleanup(guard.stop)
        self.key = "asset.image.reference"
        self.contract = {"parameters": {self.key: {
            "type": "asset_image", "required": True, "min_items": 1, "max_items": 1,
        }}}
        self.rows = [{"semantic_key": self.key, "node_id": "1", "input_name": "image", "value_type": "asset_image", "is_required": True}]

    def project(self):
        return workflow_input_targets(SimpleNamespace(
            input_contract=self.contract, semantic_bindings=Mock(values=Mock(return_value=self.rows)),
        ))

    def test_fixed_slot_preserves_exact_key_type_and_count(self):
        before = deepcopy(self.contract)
        target = self.project()[self.key]
        self.assertEqual(target["kind"], "image")
        self.assertEqual((target["min_items"], target["max_items"]), (1, 1))
        self.assertEqual(target["wiring"], {"kind": "semantic", "semantic_key": self.key})
        self.assertEqual(self.contract, before)

    def test_missing_or_wrong_bound_and_binding_never_defaults(self):
        for changes in ({"min_items": None}, {"max_items": 2}, {"required": None}, {"min_items": False}, {"min_items": 0}):
            original = self.contract["parameters"][self.key]
            self.contract["parameters"][self.key] = {**original, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.project()
            self.contract["parameters"][self.key] = original
        self.rows[0]["value_type"] = "asset_video"
        with self.assertRaises(ValidationError):
            self.project()

    def test_frozen_reference_role_is_preserved_not_inferred_from_semantic_name(self):
        self.contract["parameters"][self.key].update(asset_category="last_frame", input_slot=2)
        self.assertEqual(self.project()[self.key]["reference_binding"], {"category": "last_frame", "input_slot": 2})
        for changes in ({"asset_category": None}, {"input_slot": True}, {"input_slot": 0}):
            original = deepcopy(self.contract["parameters"][self.key])
            self.contract["parameters"][self.key].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.project()
            self.contract["parameters"][self.key] = original
    def test_unmanaged_asset_binding_is_an_error(self):
        self.contract["parameters"] = {}
        with self.assertRaisesMessage(ValidationError, "未声明完整"):
            self.project()

    def test_dynamic_groups_are_not_flattened_or_inferred_from_names(self):
        self.contract["classified_references"] = {"target_node_id": "target", "groups": {
            "user_configured_group": {"media_kind": "audio", "min_items": 0, "max_items": 3, "input_names": ["a0", "a1", "a2"]},
        }}
        target = self.project()["user_configured_group"]
        self.assertEqual(target["kind"], "audio")
        self.assertEqual(target["max_items"], 3)
        self.assertEqual(target["wiring"]["input_names"], ["a0", "a1", "a2"])
        self.assertFalse(target["required"])

    def test_fixed_and_dynamic_targets_cannot_share_a_key(self):
        self.contract["classified_references"] = {"target_node_id": "target", "groups": {
            self.key: {"media_kind": "image", "min_items": 0, "max_items": 3, "input_names": ["a0", "a1", "a2"]},
        }}
        with self.assertRaisesMessage(ValidationError, "重名"):
            self.project()

    def test_frozen_group_validation_is_not_bypassed(self):
        self.contract["asset_groups"] = [{"key": "broken"}]
        with self.assertRaises(ValidationError):
            self.project()


class ExecutionSlotAllocationTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch("studio.services.execution_instances.boundary_int", return_value=65536))
        self.targets = {
            "pictures": {"target_kind": "workflow_slot", "target_key": "pictures", "kind": "image", "min_items": 1, "max_items": 2,
                         "wiring": {"kind": "classified_group", "node_id": "conditioner", "input_names": ["inputs.ref_0", "inputs.ref_1"]}},
            "voices": {"target_kind": "workflow_slot", "target_key": "voices", "kind": "audio", "min_items": 0, "max_items": 1,
                       "wiring": {"kind": "semantic_group", "semantic_keys": ["asset.audio.reference"]}},
        }
        self.items = {"pictures": [{"item_key": "second-selected", "kind": "image"}, {"item_key": "first-selected", "kind": "image"}]}

    def test_declared_port_order_preserves_actual_item_order(self):
        before = deepcopy((self.targets, self.items))
        assigned = allocate_workflow_input_slots(self.targets, self.items)
        self.assertEqual(assigned["pictures"], [
            {"item_key": "second-selected", "position": 0, "target_position": 0, "target_key": "pictures", "target_port_ref": {"kind": "node_input", "node_id": "conditioner", "input_name": "inputs.ref_0"}},
            {"item_key": "first-selected", "position": 1, "target_position": 1, "target_key": "pictures", "target_port_ref": {"kind": "node_input", "node_id": "conditioner", "input_name": "inputs.ref_1"}},
        ])
        self.assertEqual(assigned["voices"], [])
        self.assertEqual((self.targets, self.items), before)

    def test_fixed_and_semantic_group_slots_keep_exact_identity(self):
        self.targets["voices"]["wiring"] = {"kind": "semantic", "semantic_key": "asset.audio.custom"}
        self.items["voices"] = [{"item_key": "voice-use", "kind": "audio"}]
        assigned = allocate_workflow_input_slots(self.targets, self.items)
        self.assertEqual(assigned["voices"][0]["target_port_ref"], {"kind": "semantic", "semantic_key": "asset.audio.custom"})

    def test_quantity_is_after_expansion_and_never_truncated(self):
        for rows in ([], self.items["pictures"] + [{"item_key": "extra", "kind": "image"}]):
            with self.subTest(rows=rows), self.assertRaisesMessage(ValidationError, "展开后"):
                allocate_workflow_input_slots(self.targets, {"pictures": rows})
        with self.assertRaises(ValidationError):
            allocate_workflow_input_slots(self.targets, {})

    def test_unknown_target_duplicate_item_type_and_file_path_rejected(self):
        invalid = [
            {**self.items, "unknown": []},
            {"pictures": [self.items["pictures"][0]] * 2},
            {"pictures": [{"item_key": "wrong-kind", "kind": "video"}]},
            {"pictures": [{"item_key": "ref", "kind": "image", "path": "C:/temp/image.png"}]},
        ]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValidationError):
                allocate_workflow_input_slots(self.targets, rows)

    def test_unregistered_wiring_or_incomplete_ports_do_not_infer_names(self):
        for changes in ({"kind": "guess_prefix", "prefix": "ref_"},
                        {"kind": "classified_group", "node_id": "node", "input_names": ["only-one"]},
                        {"kind": "semantic_group", "semantic_keys": ["same", "same"]}):
            self.targets["pictures"]["wiring"] = changes
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                allocate_workflow_input_slots(self.targets, self.items)

    def test_empty_optional_groups_still_cannot_claim_duplicate_ports(self):
        self.targets["more_voices"] = deepcopy(self.targets["voices"])
        self.targets["more_voices"]["target_key"] = "more_voices"
        with self.assertRaisesMessage(ValidationError, "争写"):
            allocate_workflow_input_slots(self.targets, self.items)

    def test_same_asset_uses_are_not_deduplicated_by_this_allocator(self):
        # Two authorized uses can refer to one Asset elsewhere; each use has
        # its own identity/purpose and must retain its place in the request.
        self.items["pictures"] = [{"item_key": "asset-use-identity", "kind": "image"}, {"item_key": "asset-use-style", "kind": "image"}]
        result = allocate_workflow_input_slots(self.targets, self.items)
        self.assertEqual(len(result["pictures"]), 2)

    def paired_groups(self):
        self.targets["videos"] = {
            "target_kind": "workflow_slot", "target_key": "videos", "kind": "video", "min_items": 0, "max_items": 3,
            "wiring": {"kind": "classified_group", "node_id": "conditioner", "input_names": ["video_0", "video_1", "video_2"]},
        }
        self.targets["paired_audio"] = {
            "target_kind": "workflow_slot", "target_key": "paired_audio", "kind": "audio", "min_items": 0, "max_items": 1,
            "wiring": {"kind": "classified_group", "node_id": "conditioner", "input_names": ["audio_0", "audio_1", "audio_2"], "index_source_target_key": "videos"},
        }
        self.items["videos"] = [{"item_key": f"video-use-{i}", "kind": "video"} for i in range(3)]
        self.items["paired_audio"] = [{"item_key": "one-audio", "kind": "audio", "paired_to": "video-use-2"}]

    def test_sparse_audio_indexes_parent_video_not_audio_list(self):
        self.paired_groups()
        row = allocate_workflow_input_slots(self.targets, self.items)["paired_audio"][0]
        self.assertEqual((row["position"], row["target_position"]), (0, 2))
        self.assertEqual(row["target_port_ref"]["input_name"], "audio_2")
        reordered = {key: self.targets[key] for key in reversed(self.targets)}
        self.assertEqual(allocate_workflow_input_slots(reordered, self.items)["paired_audio"][0], row)

    def test_pair_must_be_explicit_and_cannot_reuse_another_parent_slot(self):
        self.paired_groups()
        for paired_to in (None, "missing-video", []):
            self.items["paired_audio"][0]["paired_to"] = paired_to
            with self.subTest(paired_to=paired_to), self.assertRaises(ValidationError):
                allocate_workflow_input_slots(self.targets, self.items)
        self.targets["paired_audio"]["max_items"] = 2
        self.items["paired_audio"] = [
            {"item_key": key, "kind": "audio", "paired_to": "video-use-2"} for key in ("first", "second")
        ]
        with self.assertRaisesMessage(ValidationError, "争写"):
            allocate_workflow_input_slots(self.targets, self.items)

    def test_paired_capacity_is_parent_capacity_but_count_is_audio_limit(self):
        self.paired_groups()
        self.targets["paired_audio"]["wiring"]["input_names"] = ["audio_0"]
        with self.assertRaisesMessage(ValidationError, "有序端口"):
            allocate_workflow_input_slots(self.targets, self.items)

    def test_malformed_catalog_values_are_reported_as_contract_errors(self):
        self.paired_groups()
        self.targets["videos"]["kind"] = []
        with self.assertRaises(ValidationError):
            allocate_workflow_input_slots(self.targets, self.items)
        self.targets["videos"]["kind"] = "video"
        self.targets["videos"]["wiring"] = []
        reordered = {key: self.targets[key] for key in reversed(self.targets)}
        with self.assertRaises(ValidationError):
            allocate_workflow_input_slots(reordered, self.items)
