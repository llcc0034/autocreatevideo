"""Declared generic ports -> frozen roles -> real isolated Asset projection.

No provider, live Smoke or production configuration is used.
"""
from copy import deepcopy
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.services.execution_inputs import workflow_input_targets
from studio.services.execution_reference_materials import project_execution_reference_materials
from studio.services.execution_workflow_uses import read_execution_workflow_uses, execution_workflow_reference_slots
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_input_resolution as source_fixtures
from studio.tests import test_workflow_asset_groups as group_fixtures


class GenericReferenceRoleTests(TestCase):
    setUpTestData = classmethod(source_fixtures.ExecutionInputResolutionTests.setUpTestData.__func__)
    definition = source_fixtures.ExecutionInputResolutionTests.definition
    item = source_fixtures.ExecutionInputResolutionTests.item
    resolve = source_fixtures.ExecutionInputResolutionTests.resolve

    def setUp(self):
        source_fixtures.ExecutionInputResolutionTests.setUp(self)
        group_fixtures.GenericAssetGroupTests.setUp(self)
        self.snapshot, _ = group_fixtures.GenericAssetGroupTests.project(self)
        # group fixture's author definition is not an execution definition.
        del self.definition
        self.contract = self.snapshot["input_contract"]
        self.keys = self.contract["asset_groups"][0]["semantic_keys"]
        self.spec["targets"][0]["target_key"] = "reference_group"
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        self.refresh_target()

    def project_targets(self):
        return workflow_input_targets(SimpleNamespace(input_contract=self.contract,
            semantic_bindings=SimpleNamespace(values=lambda *args: self.snapshot["semantic_bindings"])))

    def refresh_target(self):
        self.target = self.project_targets()["references"]
        # Only the logical group name changes; exact semantic ports remain.
        self.target["target_key"] = "reference_group"

    def reference_projection(self, resolved=None, definition=None):
        return project_execution_reference_materials(definition or self.definition(),
            json_contract(resolved if resolved is not None else self.resolve()),
            prompt_type="image", unit_keys=["unit"], scope_units={}).value

    def test_projector_declared_roles_are_frozen_without_changing_source(self):
        before = deepcopy(self.contract)
        roles = self.target["reference_bindings"]
        self.assertEqual(roles, {key: {"category": self.contract["parameters"][key]["asset_category"],
            "input_slot": self.contract["parameters"][key]["input_slot"]} for key in self.keys})
        self.assertEqual(self.contract, before)
        self.contract["parameters"][self.keys[0]]["input_slot"] = 7
        changed = self.project_targets()["references"]
        self.assertNotEqual(roles, changed["reference_bindings"])
        self.assertEqual(roles[self.keys[0]]["input_slot"], 1)

    def test_real_asset_and_reordering_follow_allocated_semantic_not_name_or_position_label(self):
        self.contract["parameters"][self.keys[0]].update(asset_category="last_frame", input_slot=7)
        self.contract["parameters"][self.keys[1]].update(asset_category="image", input_slot=3)
        self.refresh_target()
        for order in (("a", "b"), ("b", "a")):
            resolved = self.resolve([self.item(item_key=order[0]), self.item(item_key=order[1], order=1)])
            by_use = {row["item_key"]: row for row in self.reference_projection(resolved)["references"]}
            for use, semantic, slot, category in ((order[0], self.keys[0], 7, "last_frame"),
                    (order[1], self.keys[1], 3, "image")):
                self.assertEqual(by_use[use]["target_port_ref"], {"kind": "semantic", "semantic_key": semantic})
                self.assertEqual(by_use[use]["input_label"], f"<Picture {slot}>")
                self.assertEqual(by_use[use]["category"], category)
                self.assertEqual(by_use[use]["asset_id"], str(self.asset.pk))
            self.assertEqual(len(by_use), 2)

    def test_frozen_role_does_not_read_current_workflow_or_mutate_previous_definition(self):
        frozen = self.definition()
        resolved = self.resolve(definition=frozen)
        original = deepcopy(frozen)
        self.contract["parameters"][self.keys[0]].update(asset_category="first_frame", input_slot=9)
        row, = self.reference_projection(resolved, frozen)["references"]
        self.assertEqual((row["category"], row["input_slot"]), ("image", 1))
        self.assertEqual(frozen, original)

    def test_missing_role_remains_raw_media_only_never_infers_label(self):
        for member in ("asset_category", "input_slot"):
            self.contract["parameters"][self.keys[0]].pop(member)
        self.refresh_target()
        resolved = self.resolve()
        uses = read_execution_workflow_uses(self.definition(), json_contract(resolved))
        self.assertEqual(execution_workflow_reference_slots(uses, require_roles=False), {})
        with self.assertRaisesMessage(ValidationError, "尚未声明明确参考角色"):
            self.reference_projection(resolved)

    def test_partial_or_invalid_roles_are_rejected_by_authoritative_projection(self):
        original = deepcopy(self.contract["parameters"][self.keys[0]])
        for changes in ({"input_slot": None}, {"input_slot": True}, {"input_slot": 0}, {"asset_category": ""}):
            self.contract["parameters"][self.keys[0]] = {**original, **changes}
            with self.subTest(changes=changes), self.assertRaisesMessage(ValidationError, "同时明确声明"):
                self.project_targets()

    def test_duplicate_roles_and_corrupt_frozen_mapping_fail_closed(self):
        resolved = self.resolve([self.item(), self.item(item_key="use-two", order=1)])
        self.target["reference_bindings"][self.keys[1]]["input_slot"] = 1
        with self.assertRaisesMessage(ValidationError, "序号无效或重复"):
            self.reference_projection(resolved)
        self.refresh_target()
        self.target["reference_bindings"]["not-a-port"] = {"category": "image", "input_slot": 4}
        with self.assertRaisesMessage(ValidationError, "冻结端口目录不一致"):
            self.reference_projection(resolved)

    def test_old_definition_is_not_enriched_from_current_contract(self):
        self.target.pop("reference_bindings")
        with self.assertRaisesMessage(ValidationError, "尚未声明明确参考角色"):
            self.reference_projection()

    def test_port_directory_reordering_uses_exact_semantic_key_not_mapping_order(self):
        self.target["wiring"]["semantic_keys"] = list(reversed(self.keys))
        row, = self.reference_projection()["references"]
        self.assertEqual(row["target_port_ref"]["semantic_key"], self.keys[-1])
        self.assertEqual((row["category"], row["input_label"]), ("image", "<Picture 3>"))

    def test_unused_missing_role_does_not_block_used_role_but_used_missing_role_does(self):
        self.target["reference_bindings"].pop(self.keys[-1])
        row, = self.reference_projection()["references"]
        self.assertEqual(row["input_label"], "<Picture 1>")
        self.target["wiring"]["semantic_keys"] = list(reversed(self.keys))
        with self.assertRaisesMessage(ValidationError, "尚未声明明确参考角色"):
            self.reference_projection()
