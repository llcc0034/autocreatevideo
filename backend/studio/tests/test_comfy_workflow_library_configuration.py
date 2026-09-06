from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from types import SimpleNamespace
from unittest.mock import patch

from studio.models import ConfigDefinition
from studio.services.configuration import validate_config_value
from studio.services.comfyui_workflow_library import (
    SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY,
    SavedWorkflowDirectoryError,
    normalize_saved_workflow_directory,
    saved_workflow_directory,
    saved_workflow_directory_resolution_cache,
)


class ComfyWorkflowLibraryConfigurationTests(SimpleTestCase):
    def test_accepts_only_workflows_root_or_descendant(self):
        self.assertEqual(normalize_saved_workflow_directory("workflows"), "workflows")
        self.assertEqual(
            normalize_saved_workflow_directory("workflows/team-a/图像"),
            "workflows/team-a/图像",
        )
        self.assertEqual(
            saved_workflow_directory(values={
                SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY: "workflows/team-a",
            }),
            "workflows/team-a",
        )

    def test_rejects_absolute_traversal_and_noncanonical_directories(self):
        for invalid in (
            "",
            "other",
            "/workflows",
            "C:/workflows",
            "workflows\\team-a",
            "workflows/../outside",
            "workflows/./team-a",
            "workflows//team-a",
            " workflows",
            "workflows/team-a ",
            "workflows/%2e%2e/outside",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                SavedWorkflowDirectoryError,
            ):
                normalize_saved_workflow_directory(invalid)

    def test_config_definition_format_rejects_invalid_directory_before_publish(self):
        definition = ConfigDefinition(
            key=SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY,
            value_type=ConfigDefinition.ValueType.STRING,
            value_schema={"format": "comfyui-saved-workflow-directory"},
        )
        validate_config_value(definition, "workflows/team-a")
        with self.assertRaises(ValidationError):
            validate_config_value(definition, "../outside")

    def test_operation_cache_freezes_one_resolved_directory_snapshot(self):
        resolved = SimpleNamespace(values={
            SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY: "workflows/team-a",
        })
        with (
            patch(
                "studio.services.configuration.resolve_configuration",
                return_value=resolved,
            ) as resolve,
            saved_workflow_directory_resolution_cache(),
        ):
            self.assertEqual(saved_workflow_directory(), "workflows/team-a")
            self.assertEqual(saved_workflow_directory(), "workflows/team-a")
        resolve.assert_called_once_with(
            required_keys=(SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY,),
        )

    def test_resolution_is_not_process_cached_outside_an_operation(self):
        with patch(
            "studio.services.configuration.resolve_configuration",
            side_effect=[
                SimpleNamespace(values={SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY: "workflows/a"}),
                SimpleNamespace(values={SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY: "workflows/b"}),
            ],
        ) as resolve:
            self.assertEqual(saved_workflow_directory(), "workflows/a")
            self.assertEqual(saved_workflow_directory(), "workflows/b")
        self.assertEqual(resolve.call_count, 2)
