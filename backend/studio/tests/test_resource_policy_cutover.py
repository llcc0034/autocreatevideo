"""Maintenance guards only; no provider calls or live database mutation."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from studio.management.resource_policy_cutover import CONFIRMATION, clean_specification, execute, incompatible


class ResourcePolicyCutoverTests(SimpleTestCase):
    def test_one_off_conversion_removes_only_retired_output_controls(self):
        original = {"series_concept": {"max_output_tokens": 4096, "max_input_bytes": 10000},
            "visual_rules": {"output_token_warning_threshold": 2000, "chat_request_options": {}},
            "text_execution": {"operations": {"prompt.draft": {
                "parameters": {"max_tokens": {"type": "integer"}, "seed": {"type": "integer"}},
                "chat_request_options": {"temperature": 0}}}}}
        before = deepcopy(original)
        self.assertTrue(incompatible(original))
        result = clean_specification(original)
        self.assertFalse(incompatible(result))
        self.assertEqual(original, before)
        self.assertEqual(result["series_concept"], {"max_input_bytes": 10000})
        operation = result["text_execution"]["operations"]["prompt.draft"]
        self.assertEqual(operation["parameters"], {"seed": {"type": "integer"}})
        self.assertEqual(operation["chat_request_options"], {"temperature": 0})

    def test_confirmation_and_backup_are_mandatory_before_any_database_access(self):
        for confirmation, backup in (("", "manifest.json"), (CONFIRMATION, "")):
            with self.assertRaises(CommandError):
                execute(expected_sha256="a" * 64, confirmation=confirmation,
                    backup_manifest=backup, operator="operator")

    def test_schema_cutover_is_required_before_cleanup(self):
        with patch("django.db.migrations.recorder.MigrationRecorder.Migration") as migration:
            migration.objects.filter.return_value.exists.return_value = False
            with self.assertRaisesMessage(CommandError, "Schema"):
                execute(expected_sha256="a" * 64, confirmation=CONFIRMATION,
                    backup_manifest="manifest.json", operator="operator")

    def test_changed_scope_or_active_call_never_reaches_delete(self):
        from contextlib import nullcontext
        boundary = "studio.management.resource_policy_cutover."
        for digest, active in (("b" * 64, []), ("a" * 64, [{"status": "submitting"}])):
            with self.subTest(digest=digest, active=active), \
                    patch("django.db.migrations.recorder.MigrationRecorder.Migration") as migration, \
                    patch(boundary + "_validate_backup_manifest", return_value={}), \
                    patch("studio.management.execution_cutover.cutover_operator", return_value=Mock()), \
                    patch(boundary + "registry_reference_write_guard", side_effect=nullcontext), \
                    patch(boundary + "_lock_plan_rows"), \
                    patch(boundary + "build_plan", return_value=SimpleNamespace(report={"plan_sha256": digest, "active_calls": active})), \
                    patch(boundary + "_hard_delete_plan") as delete:
                migration.objects.filter.return_value.exists.return_value = True
                with self.assertRaises(CommandError):
                    execute(expected_sha256="a" * 64, confirmation=CONFIRMATION,
                        backup_manifest="manifest.json", operator="operator")
                delete.assert_not_called()
