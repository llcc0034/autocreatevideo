from copy import deepcopy
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from jsonschema import Draft202012Validator, FormatChecker
from rest_framework.test import APIClient

from studio.models import DataStructureNode, DataStructureVersion, FieldDefinition, FieldDefinitionVersion
from studio.services.data_structures import (
    compile_data_structure, compile_structure_nodes, data_structure_snapshot,
    transition_data_structure, write_data_structure, validate_data_structure_snapshot, validate_structure_value,
)


class StructureFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.definitions = {}
        for kind in FieldDefinitionVersion.ValueType.values:
            identity = FieldDefinition.objects.create(key=f"structure_{kind}")
            cls.definitions[kind] = FieldDefinitionVersion.objects.create(
                field_definition=identity, version=1, status="published", label=f"Custom {kind}",
                category="test", value_type=kind, value_options=["calm", "tense"] if kind == "enum" else [],
            )

    def node(self, key, kind="text", parent=None, **values):
        return {"node_key": key, "field_definition_version": str(self.definitions[kind].pk),
            "parent_key": parent, **values}

    def create(self, nodes=None, **values):
        return write_data_structure(content={"name": "Generic structure", "nodes": nodes or [self.node("root", "object")], **values})

    def publish(self, version):
        return transition_data_structure(version_id=version.pk, status="published")


class DataStructureCompilerTests(StructureFixture):
    def test_frozen_structure_validation_rejects_invalid_envelopes_without_queries(self):
        frozen = data_structure_snapshot(self.publish(self.create([self.node("root", "text")])))
        with self.assertNumQueries(0):
            self.assertEqual(validate_data_structure_snapshot(frozen)["type"], "string")
            for change in ({"schema_version": True}, {"version": 1.0}, {"name": ""},
                    {"id": 1}, {"structure_key": "not-a-uuid"}, {"extra": "unsupported"}, {"nodes": []}):
                with self.subTest(change=change), self.assertRaises(ValidationError):
                    validate_data_structure_snapshot({**frozen, **change})

    def test_value_validation_preserves_unknown_fields_and_reports_nested_path(self):
        structure = self.publish(self.create([
            self.node("root", "object"), self.node("groups", "list", "root", property_name="groups", required=True),
            self.node("item", "integer", "groups", constraints={"minimum": 0}),
        ]))
        frozen = data_structure_snapshot(structure)
        value = {"groups": [0, 1], "extra": {"untouched": False}}
        original = deepcopy(value)
        with self.assertNumQueries(0):
            validate_structure_value(frozen, value, path="input")
            self.assertEqual(value, original)
            with self.assertRaises(ValidationError) as error:
                validate_structure_value(frozen, {"groups": [0, False]}, path="input")
            self.assertIn("input.groups[1]", error.exception.message_dict)

    def nested_structure(self):
        return self.create([
            self.node("root", "object"),
            self.node("groups", "list", "root", property_name="chapters", required=True, allow_empty=False),
            self.node("group", "object", "groups"),
            self.node("items", "list", "group", property_name="frames"),
            self.node("item", "object", "items"),
            self.node("motion", "long_text", "item", property_name="move", required=True, allow_blank=False),
            self.node("count", "integer", "item", property_name="count", constraints={"minimum": 0, "maximum": 10}),
            self.node("duration", "decimal", "item", property_name="time", constraints={"minimum": 0.1}),
            self.node("off_screen", "boolean", "item", property_name="off_screen"),
            self.node("tone", "enum", "item", property_name="tone", constraints={"enum": ["calm"]}, allow_null=True),
            self.node("reference", "media", "item", property_name="ref", constraints={"media_types": ["image", "video"], "usage": "identity"}),
        ])

    def test_nested_types_produce_one_schema_with_exact_field_meaning(self):
        version = self.nested_structure()
        schema = compile_data_structure(version)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        item = {"move": "Turn", "count": 0, "time": 1.5, "off_screen": False,
            "tone": None, "ref": {"asset_id": str(uuid4())}, "not_mapped": "keep original"}
        validator.validate({"chapters": [{"frames": [item]}]})
        for field, value in (("count", True), ("count", 2.4), ("time", "1.5"), ("off_screen", "false"),
                ("tone", "tense"), ("ref", {"asset_id": "bad"}), ("move", "")):
            with self.subTest(field=field):
                payload = {"chapters": [{"frames": [{**item, field: value}]}]}
                errors = list(validator.iter_errors(payload))
                self.assertTrue(errors)
                self.assertEqual(list(errors[0].absolute_path)[:4], ["chapters", 0, "frames", 0])
        nested = schema["properties"]["chapters"]["items"]["properties"]["frames"]["items"]["properties"]
        self.assertEqual(nested["move"]["x-field-definition"]["key"], "structure_long_text")
        self.assertEqual(nested["ref"]["x-media-types"], ["image", "video"])
        self.assertEqual(nested["count"]["x-field-definition"]["field_definition_version_id"], str(self.definitions["integer"].pk))

    def test_same_identity_can_be_used_under_different_properties(self):
        version = self.create([self.node("root", "object"),
            self.node("a", parent="root", property_name="action"), self.node("b", parent="root", property_name="motion")])
        schema = compile_data_structure(version)
        self.assertEqual(schema["properties"]["action"]["x-field-definition"], schema["properties"]["motion"]["x-field-definition"])

    def test_nested_lists_and_media_lists_have_explicit_element_definitions(self):
        version = self.create([self.node("root", "list", constraints={"max_items": 2}),
            self.node("row", "list", "root"), self.node("reference", "media", "row", constraints={"media_types": ["audio"]})])
        schema = compile_data_structure(version)
        self.assertEqual(schema["items"]["items"]["x-media-types"], ["audio"])
        Draft202012Validator(schema, format_checker=FormatChecker()).validate([[{"asset_id": str(uuid4())}]])

    def test_empty_draft_preview_and_publication_are_explicitly_invalid(self):
        unnamed = write_data_structure(content={})
        with self.assertRaises(ValidationError) as error:
            self.publish(unnamed)
        self.assertIn("name", error.exception.message_dict)
        draft = write_data_structure(content={"name": "Empty draft"})
        for action in (compile_data_structure, self.publish):
            with self.assertRaisesMessage(ValidationError, "根节点"):
                action(draft)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "draft")

    def test_draft_cycle_even_disconnected_from_root_is_located(self):
        for nodes in (
            [self.node("a", "object", "b"), self.node("b", "object", "a")],
            [self.node("root", "object"), self.node("a", "object", "b"), self.node("b", "object", "a")],
        ):
            draft = self.create(nodes)
            with self.assertRaises(ValidationError) as error:
                compile_data_structure(draft)
            self.assertIn("nodes.a.parent_key", error.exception.message_dict)
            self.assertIn("循环", str(error.exception))
            write_data_structure(version_id=draft.pk, content={"nodes": [self.node("root", "object")]})
            self.assertEqual(compile_data_structure(draft)["type"], "object")

    def test_missing_parent_rejects_write_and_preserves_previous_draft(self):
        version = self.create()
        with self.assertRaisesMessage(ValidationError, "父节点不存在"):
            write_data_structure(version_id=version.pk, content={"name": "Changed", "nodes": [self.node("child", parent="missing")]})
        version.refresh_from_db()
        self.assertEqual(version.name, "Generic structure")
        self.assertEqual(list(version.nodes.values_list("node_key", flat=True)), ["root"])

    def test_structure_shape_errors_have_node_paths(self):
        cases = [
            ([self.node("root", "list")], "nodes.root"),
            ([self.node("root"), self.node("child", parent="root", property_name="extra")], "nodes.root"),
            ([self.node("root", "object"), self.node("a", parent="root")], "nodes.a.property_name"),
            ([self.node("root", "object"), self.node("a", parent="root", property_name="dup"), self.node("b", parent="root", property_name="dup")], "nodes.root"),
            ([self.node("root", "object"), self.node("second", "object")], "nodes"),
            ([self.node("root", "list"), self.node("child", parent="root", property_name="not_an_object")], "nodes.child"),
        ]
        for nodes, path in cases:
            with self.subTest(path=path):
                draft = self.create(nodes)
                with self.assertRaises(ValidationError) as error:
                    self.publish(draft)
                self.assertIn(path, error.exception.message_dict)

    def test_wrong_constraint_types_unknown_rules_and_contradictions_are_located(self):
        cases = [
            ("text", {"max_length": "12"}), ("text", {"min_length": 5, "max_length": 2}),
            ("boolean", {"minimum": 0}), ("integer", {"minimum": 0.5}),
            ("integer", {"maximum": True}), ("decimal", {"maximum": float("inf")}),
            ("text", {"pattern": "guess"}), ("enum", {"enum": ["absent"]}),
            ("enum", {"enum": []}), ("media", {}), ("media", {"media_types": ["image", "image"]}),
        ]
        # Pure compiler accepts no type coercion; even non-finite in-memory values fail explicitly.
        snapshot = data_structure_snapshot(self.publish(self.create([self.node("root")])))["nodes"]
        for kind, constraints in cases:
            with self.subTest(kind=kind, constraints=constraints):
                from studio.services.field_definitions import field_definition_snapshot
                node = {**snapshot[0], "field_definition": field_definition_snapshot(self.definitions[kind]), "constraints": constraints}
                with self.assertRaises(ValidationError):
                    compile_structure_nodes([node])

    def test_explicit_empty_constraints_and_required_are_in_schema(self):
        version = self.create([self.node("root", "object", allow_empty=False),
            self.node("note", parent="root", property_name="note", required=True, allow_blank=False, constraints={"max_length": 4})])
        validator = Draft202012Validator(compile_data_structure(version))
        for value in ({}, {"note": ""}, {"note": "longer"}, {"note": None}):
            self.assertFalse(validator.is_valid(value))
        self.assertTrue(validator.is_valid({"note": "valid"[:4]}))
        draft = self.create([self.node("root", allow_blank=False, constraints={"max_length": 0})])
        with self.assertRaisesMessage(ValidationError, "冲突"):
            compile_data_structure(draft)

    def test_compile_is_single_join_not_per_node_queries(self):
        version = self.nested_structure()
        with CaptureQueriesContext(connection) as captured:
            compile_data_structure(version)
        self.assertEqual(len(captured), 1, [q["sql"] for q in captured])


class DataStructureLifecycleTests(StructureFixture):
    def test_exact_versions_and_snapshot_do_not_change_with_new_definitions(self):
        structure = self.publish(self.create())
        before = data_structure_snapshot(structure)
        definition = self.definitions["object"]
        FieldDefinitionVersion.objects.create(field_definition=definition.field_definition, version=2,
            label="New meaning", category="test", value_type="text", status="published")
        definition.status = "retired"
        definition.save()
        definition.field_definition.is_active = False
        definition.field_definition.save()
        self.assertEqual(data_structure_snapshot(structure), before)
        with self.assertNumQueries(0):
            self.assertEqual(compile_structure_nodes(before["nodes"]), compile_structure_nodes(deepcopy(before["nodes"])))
        clone = write_data_structure(source_version_id=structure.pk, content={})
        self.assertEqual(clone.version, 2)
        self.assertEqual(clone.nodes.get().field_definition_version_id, definition.pk)
        with self.assertRaisesMessage(ValidationError, "可用字段版本"):
            self.publish(clone)

    def test_published_shape_is_immutable_and_retired_cannot_reopen(self):
        structure = self.publish(self.create())
        with self.assertRaises(ValidationError):
            write_data_structure(version_id=structure.pk, content={"nodes": []})
        node = structure.nodes.get()
        node.property_name = "bad"
        with self.assertRaises(ValidationError):
            node.save()
        with self.assertRaises(ValidationError):
            node.delete()
        structure.name = "Changed"
        with self.assertRaises(ValidationError):
            structure.save()
        retired = transition_data_structure(version_id=structure.pk, status="retired")
        with self.assertRaises(ValidationError):
            transition_data_structure(version_id=retired.pk, status="published")

    def test_draft_can_be_incomplete_but_wrong_identity_and_value_types_cannot_be_saved(self):
        draft = write_data_structure(content={"name": "Incomplete field", "nodes": [{"node_key": "root"}]})
        self.assertIsNone(draft.nodes.get().field_definition_version_id)
        with self.assertRaisesMessage(ValidationError, "确切字段"):
            self.publish(draft)
        for values in ({"required": "false"}, {"position": "1"}, {"property_name": 42},
                {"field_definition_version": 42}, {"field_definition_version": str(uuid4())}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.create([self.node("root", **values)])

    def test_direct_publish_and_cross_structure_parent_cannot_bypass_validation(self):
        draft = write_data_structure(content={"name": "Empty"})
        draft.status = "published"
        with self.assertRaises(ValidationError):
            draft.save()
        left, right = self.create(), self.create()
        node = left.nodes.get()
        node.parent = right.nodes.get()
        with self.assertRaisesMessage(ValidationError, "同一结构"):
            node.save()


class DataStructureApiTests(StructureFixture):
    root = "/api/v1/data-structure-versions/"

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user(username="structure-editor", is_staff=True))

    def test_authoring_catalog_is_derived_from_compiler_and_has_no_writes(self):
        from studio.services.data_structures import CONSTRAINT_SCHEMAS, NODE_DEFAULTS
        before = (DataStructureVersion.objects.count(), DataStructureNode.objects.count())
        response = self.client.get(self.root + "authoring-options/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["node_defaults"], NODE_DEFAULTS)
        self.assertEqual(set(response.data["value_schemas"]), set(self.definitions))
        self.assertEqual({item["value"] for item in response.data["statuses"]}, {value for value, _ in DataStructureVersion._meta.get_field("status").choices})
        for kind, schema in response.data["constraint_schemas"].items():
            self.assertEqual(schema["properties"], CONSTRAINT_SCHEMAS[kind])
            self.assertFalse(schema["additionalProperties"])
        self.assertEqual(before, (DataStructureVersion.objects.count(), DataStructureNode.objects.count()))
        self.client.force_authenticate(get_user_model().objects.create_user(username="catalog-reader"))
        self.assertEqual(self.client.get(self.root + "authoring-options/").status_code, 403)

    def test_crud_preview_clone_and_lifecycle(self):
        created = self.client.post(self.root, {"name": "Input or output", "nodes": [self.node("root", "object")]}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        url = f"{self.root}{created.data['id']}/"
        preview = self.client.get(url + "schema-preview/")
        self.assertTrue(preview.data["valid"], preview.data)
        self.assertEqual(preview.data["json_schema"]["type"], "object")
        published = self.client.post(url + "publish/", {}, format="json")
        self.assertEqual(published.status_code, 200, published.data)
        self.assertEqual(self.client.patch(url, {"name": "No"}, format="json").status_code, 400)
        clone = self.client.post(self.root, {"source_version": created.data["id"]}, format="json")
        self.assertEqual(clone.status_code, 201, clone.data)
        self.assertEqual(clone.data["nodes"], created.data["nodes"])
        self.assertEqual((clone.data["version"], clone.data["status"]), (2, "draft"))
        self.assertEqual(self.client.post(url + "retire/", {}, format="json").status_code, 200)

    def test_invalid_preview_is_read_only_and_publish_does_not_fake_success(self):
        draft = self.create([self.node("root", "list")])
        url = f"{self.root}{draft.pk}/"
        before = (draft.updated_at, draft.nodes.count())
        preview = self.client.get(url + "schema-preview/")
        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.data["valid"])
        self.assertIsNone(preview.data["json_schema"])
        self.assertIn("nodes.root", preview.data["errors"])
        self.assertEqual(self.client.post(url + "publish/", {}, format="json").status_code, 400)
        draft.refresh_from_db()
        self.assertEqual((draft.updated_at, draft.nodes.count()), before)
        self.assertEqual(draft.status, "draft")

    def test_summary_pagination_does_not_query_nodes_or_description(self):
        for _ in range(3):
            self.create(description="Detail only")
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(self.root, {"page_size": 2})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertIsNotNone(response.data["next"])
        self.assertNotIn("nodes", response.data["results"][0])
        self.assertNotIn("description", response.data["results"][0])
        relevant = [q["sql"] for q in captured if 'studio_datastructure' in q["sql"]]
        self.assertEqual(len(relevant), 2, relevant)
        self.assertNotIn('"description"', relevant[-1])
        self.assertNotIn('studio_datastructurenode', str(relevant))
        plan = DataStructureVersion.objects.only(
            "id", "structure_key", "version", "name", "status", "created_at", "updated_at",
        ).order_by("structure_key", "-version", "id")[:2].explain()
        self.assertTrue(plan)
        if connection.vendor == "sqlite":
            self.assertIn("USING INDEX", plan)
        print("Structure summary EXPLAIN:", plan)

    def test_unsupported_writes_and_non_admin_access_are_explicit(self):
        for key, value in (("version", 8), ("status", "published"), ("schema", {}), ("output_direction", "input")):
            self.assertEqual(self.client.post(self.root, {key: value}, format="json").status_code, 400)
        draft = self.create()
        self.assertEqual(self.client.post(f"{self.root}{draft.pk}/publish/", {"name": "hidden edit"}, format="json").status_code, 400)
        self.client.force_authenticate(get_user_model().objects.create_user(username="structure-reader"))
        self.assertEqual(self.client.get(self.root).status_code, 403)
        self.assertEqual(self.client.post(self.root, {}, format="json").status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.root).status_code, (401, 403))
