"""Pure identity projection, not a model request or a media fulfillment."""
from copy import deepcopy
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.production_recipe_material_units import project_recipe_material_units


class RecipeMaterialUnitProjectionTests(SimpleTestCase):
    def setUp(self):
        self.scope = {"kind": "character", "id": str(uuid4())}
        self.primary = self.target("primary")
        self.children = [self.target("variant", self.primary["setting"]) for _ in range(2)]
        self.requirements = {"targets": [self.primary, *self.children]}

    def target(self, role, dependency=None):
        setting = {"appearance_id": str(uuid4()), "setting_revision_id": str(uuid4()),
            "revision": 1, "content_sha256": "a" * 64}
        return {"scope": dict(self.scope), "appearance_role": role, "setting": setting,
            "requirement_key": f'setting:{setting["setting_revision_id"]}',
            "primary_dependency": deepcopy(dependency), "reference_name": "Same name",
            "declared_reference_asset": {"asset_id": str(uuid4()), "sha256": "b" * 64}}

    def test_siblings_have_separate_units_and_exact_primary_requirement(self):
        units = project_recipe_material_units(self.requirements)
        self.assertEqual(len({unit["unit_key"] for unit in units}), 3)
        self.assertIsNone(units[0]["primary_dependency"])
        for child in units[1:]:
            self.assertEqual(child["primary_dependency"], {key: units[0][key] for key in ("unit_key", "requirement_key")})
        self.assertTrue(all("reference_name" not in unit and "declared_reference_asset" not in unit for unit in units))

    def test_names_and_reference_assets_do_not_define_identity_or_fulfillment(self):
        original = project_recipe_material_units(self.requirements)
        changed = deepcopy(self.requirements)
        for target in changed["targets"]:
            target.update(reference_name="Renamed", declared_reference_asset=None)
        self.assertEqual(project_recipe_material_units(changed), original)

    def test_setting_revision_changes_requirement_not_appearance_identity(self):
        original = project_recipe_material_units(self.requirements)
        changed = deepcopy(self.requirements)
        target = changed["targets"][0]
        target["setting"].update(setting_revision_id=str(uuid4()), revision=2, content_sha256="c" * 64)
        target["requirement_key"] = f'setting:{target["setting"]["setting_revision_id"]}'
        units = project_recipe_material_units(changed)
        self.assertEqual(units[0]["unit_key"], original[0]["unit_key"])
        self.assertNotEqual(units[0]["requirement_key"], original[0]["requirement_key"])
        # The historical child's dependency does not silently move to v2.
        self.assertEqual(units[1:], original[1:])

    def test_missing_primary_target_is_not_invented_or_replaced(self):
        units = project_recipe_material_units({"targets": self.children})
        self.assertEqual(len(units), 2)
        self.assertNotIn(units[0]["primary_dependency"]["unit_key"], {unit["unit_key"] for unit in units})

    def test_duplicate_appearance_even_with_another_revision_is_rejected(self):
        duplicate = deepcopy(self.primary)
        duplicate["setting"]["setting_revision_id"] = str(uuid4())
        duplicate["requirement_key"] = f'setting:{duplicate["setting"]["setting_revision_id"]}'
        with self.assertRaisesMessage(ValidationError, "无效或重复"):
            project_recipe_material_units({"targets": [self.primary, duplicate]})

    def test_invalid_identity_role_and_dependency_fail_explicitly(self):
        cases = [None, {"targets": None}, {"targets": [None]}]
        for replacement in (None, {"appearance_id": "not-a-uuid"},
                {**self.primary["setting"], "setting_revision_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"}):
            cases.append({"targets": [{**self.primary, "setting": replacement}]})
        cases.extend({"targets": [{**self.primary, **update}]} for update in (
            {"appearance_role": "variant"}, {"requirement_key": "setting:invented"},
            {"primary_dependency": self.primary["setting"]},
            {"appearance_role": "variant", "primary_dependency": self.primary["setting"]}))
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                project_recipe_material_units(invalid)

    def test_projection_cannot_mutate_original_receipt(self):
        original = deepcopy(self.requirements)
        units = project_recipe_material_units(self.requirements)
        units[0]["scope"]["id"] = str(uuid4())
        units[1]["primary_dependency"]["requirement_key"] = "changed"
        self.assertEqual(self.requirements, original)

    def test_no_demand_means_no_units(self):
        self.assertEqual(project_recipe_material_units({"targets": []}), [])
