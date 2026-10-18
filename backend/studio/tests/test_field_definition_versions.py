from copy import deepcopy

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from rest_framework.exceptions import ValidationError as ApiValidationError

from studio.field_definition_serializers import FieldDefinitionVersionSerializer
from studio.models import FieldDefinition, FieldDefinitionVersion
from studio.services.field_definitions import (
    create_field_definition_version,
    field_definition_snapshot,
    publish_field_definition_version,
    retire_field_definition_version,
    update_field_definition_draft,
)


class FieldDefinitionVersionTests(TestCase):
    def setUp(self):
        self.definition = FieldDefinition.objects.create(
            key="test_custom_motion",
        )
        self.content = {"label": "动作说明", "category": "custom", "value_type": "long_text"}

    def create(self, **content):
        return create_field_definition_version(
            field_definition_id=self.definition.pk, content={**self.content, **content},
        )

    def test_partial_draft_requires_complete_definition_only_when_published(self):
        draft = create_field_definition_version(field_definition_id=self.definition.pk, content={})
        self.assertEqual(draft.status, "draft")
        with self.assertRaises(ValidationError):
            publish_field_definition_version(version_id=draft.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "draft")
        with self.assertRaises(ValidationError):
            field_definition_snapshot(draft)
        update_field_definition_draft(version_id=draft.pk, content=self.content)
        published = publish_field_definition_version(version_id=draft.pk)
        self.assertEqual(published.status, "published")
        self.assertEqual(publish_field_definition_version(version_id=draft.pk).pk, draft.pk)

    def test_exact_version_snapshot_survives_renaming_and_type_change(self):
        old = publish_field_definition_version(version_id=self.create().pk)
        snapshot = deepcopy(field_definition_snapshot(old))
        new = create_field_definition_version(
            field_definition_id=self.definition.pk, source_version_id=old.pk,
            content={"label": "动作计数", "value_type": "integer", "unit": "次"},
        )
        publish_field_definition_version(version_id=new.pk)
        self.definition.display_order = 5
        self.definition.save()
        old.refresh_from_db()
        self.assertEqual(field_definition_snapshot(old), snapshot)
        self.assertEqual((old.version, new.version), (1, 2))
        self.assertEqual(old.field_definition_id, new.field_definition_id)
        self.assertEqual(snapshot["label"], "动作说明")
        self.assertEqual(snapshot["value_type"], "long_text")

    def test_published_and_retired_content_cannot_be_edited_or_reopened(self):
        version = publish_field_definition_version(version_id=self.create().pk)
        with self.assertRaises(ValidationError):
            update_field_definition_draft(version_id=version.pk, content={"label": "覆盖"})
        version.label = "覆盖"
        with self.assertRaises(ValidationError):
            version.save()
        version = retire_field_definition_version(version_id=version.pk)
        self.assertEqual(version.status, "retired")
        self.assertEqual(field_definition_snapshot(version)["label"], "动作说明")
        with self.assertRaises(ValidationError):
            publish_field_definition_version(version_id=version.pk)
        version.status = "draft"
        with self.assertRaises(ValidationError):
            version.save()

    def test_per_use_rules_cannot_leak_into_definition_versions(self):
        for name, value in (("default_value", False), ("is_required", True), ("actual_value", "test")):
            with self.subTest(name=name):
                with self.assertRaises(FieldDoesNotExist):
                    FieldDefinitionVersion._meta.get_field(name)
                with self.assertRaises(ValidationError):
                    self.create(**{name: value})

    def test_supported_types_are_generic_and_enum_values_are_explicit(self):
        for value_type, _ in FieldDefinitionVersion.ValueType.choices:
            with self.subTest(value_type=value_type):
                options = ["中文", "English", "日本語"] if value_type == "enum" else []
                version = publish_field_definition_version(
                    version_id=self.create(value_type=value_type, value_options=options).pk,
                )
                self.assertEqual(field_definition_snapshot(version)["value_type"], value_type)
        for value_type, options in (("enum", ["a", "a"]), ("enum", [""]), ("enum", [False]),
                                    ("enum", {"a": 1}), ("text", ["a"])):
            with self.subTest(value_type=value_type, options=options):
                with self.assertRaises(ValidationError):
                    self.create(value_type=value_type, value_options=options)
        empty_enum = self.create(value_type="enum")
        with self.assertRaises(ValidationError):
            publish_field_definition_version(version_id=empty_enum.pk)

    def test_clone_never_guesses_identity_or_copies_actual_defaults(self):
        first = self.create(value_type="enum", value_options=["a", "b"])
        second = create_field_definition_version(
            field_definition_id=self.definition.pk, source_version_id=first.pk, content={"label": "另一个标签"},
        )
        second.value_options.append("c")
        second.save()
        first.refresh_from_db()
        self.assertEqual(first.value_options, ["a", "b"])
        other = FieldDefinition.objects.create(key="other_field")
        with self.assertRaises(ValidationError):
            create_field_definition_version(field_definition_id=other.pk, source_version_id=first.pk, content={})
        self.assertFalse(other.definition_versions.exists())

    def test_stable_identity_and_version_constraints(self):
        version = self.create()
        self.definition.key = "renamed_key"
        with self.assertRaises(ValidationError):
            self.definition.save()
        version.version = 9
        with self.assertRaises(ValidationError):
            version.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            FieldDefinitionVersion.objects.bulk_create([
                FieldDefinitionVersion(field_definition_id=self.definition.pk, version=1),
            ])
        with self.assertRaises(IntegrityError), transaction.atomic():
            FieldDefinitionVersion.objects.bulk_create([
                FieldDefinitionVersion(field_definition_id=self.definition.pk, version=0),
            ])

    def test_disabled_identity_does_not_invalidate_frozen_version(self):
        version = publish_field_definition_version(version_id=self.create().pk)
        before = field_definition_snapshot(version)
        self.definition.is_active = False
        self.definition.save()
        version.refresh_from_db()
        self.assertEqual(field_definition_snapshot(version), before)
        with self.assertRaises(ValidationError):
            publish_field_definition_version(version_id=self.create().pk)

    def test_unused_identity_deletion_cleans_owned_versions(self):
        version = self.create()
        self.definition.delete()
        self.assertFalse(FieldDefinitionVersion.objects.filter(pk=version.pk).exists())

    def test_serializer_owns_version_number_and_never_accepts_usage_rules(self):
        serializer = FieldDefinitionVersionSerializer(data={
            "field_definition": str(self.definition.pk), **self.content,
            "version": 99, "status": "published",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        instance = serializer.save()
        self.assertEqual((instance.version, instance.status), (1, "draft"))
        self.assertNotIn("source_version", FieldDefinitionVersionSerializer(instance).data)
        invalid = FieldDefinitionVersionSerializer(data={
            "field_definition": str(self.definition.pk), "default_value": "not a task value",
        })
        self.assertFalse(invalid.is_valid())
        self.assertIn("default_value", invalid.errors)
        published = publish_field_definition_version(version_id=instance.pk)
        editor = FieldDefinitionVersionSerializer(published, data={"label": "修改"}, partial=True)
        self.assertTrue(editor.is_valid(), editor.errors)
        with self.assertRaises(ApiValidationError):
            editor.save()

    def test_serializer_clone_and_draft_update(self):
        first = self.create(unit="秒")
        clone = FieldDefinitionVersionSerializer(data={
            "field_definition": str(self.definition.pk), "source_version": str(first.pk), "label": "修订名称",
        })
        self.assertTrue(clone.is_valid(), clone.errors)
        second = clone.save()
        self.assertEqual((second.version, second.label, second.unit), (2, "修订名称", "秒"))
        editor = FieldDefinitionVersionSerializer(second, data={"description": "显式含义"}, partial=True)
        self.assertTrue(editor.is_valid(), editor.errors)
        self.assertEqual(editor.save().description, "显式含义")


class FieldDefinitionVersionInputTests(SimpleTestCase):
    def test_serializer_rejects_non_object_data_without_server_error(self):
        for data in ([], [{"label": "nested object"}], "text", 42):
            with self.subTest(data=data):
                serializer = FieldDefinitionVersionSerializer(data=data)
                self.assertFalse(serializer.is_valid())
                self.assertIn("non_field_errors", serializer.errors)
