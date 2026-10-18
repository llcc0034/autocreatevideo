"""Private request manifest boundaries; no provider or live data changes."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
from importlib import import_module
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import SimpleTestCase, override_settings

from studio.services.execution_request_storage import (
    ExecutionRequestStorageError, ExecutionRequestSizeError, read_request, reference_prepared_request,
    request_document, request_part_contract, store_prepared_request,
)
from studio.services.json_contracts import canonical_json_bytes, json_contract


@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class ExecutionRequestFileTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch("studio.services.execution_request_storage.boundary_int", return_value=8 * 1024 * 1024))
        self.detail = SimpleNamespace(preparation_id=uuid4(), prepared_request_key=None,
            prepared_request_schema_version=None, prepared_request_size_bytes=None, prepared_request_sha256=None)
        self.value = {"schema_version": 1, "preparation_id": str(self.detail.preparation_id),
            "field_selections": {"description": "汉" * 2000}, "transport": {"prompt": "x" * 61216}}

    def test_more_than_64k_round_trips_with_original_identity_and_full_fields(self):
        self.assertLess(json_contract(self.value["transport"]).size_bytes, 65536)
        self.assertLess(json_contract(self.value["field_selections"]).size_bytes, 65536)
        contract = store_prepared_request(self.detail, self.value)
        self.assertGreater(contract.size_bytes, 65536)
        self.assertEqual(read_request(self.detail, "prepared_request"), self.value)
        self.assertLess(len(self.detail.prepared_request_key), 191)
        with default_storage.open(self.detail.prepared_request_key, "rb") as stream:
            self.assertEqual(stream.read(), canonical_json_bytes(self.value))
        with self.assertRaises(ExecutionRequestStorageError):
            store_prepared_request(self.detail, self.value)

    def test_production_references_same_file_not_a_second_request(self):
        store_prepared_request(self.detail, self.value)
        attempt = SimpleNamespace(preparation_id=self.detail.preparation_id)
        with patch.object(default_storage, "save", side_effect=AssertionError("No duplicate write")):
            reference_prepared_request(attempt, self.detail)
        self.assertEqual(attempt.request_snapshot_key, self.detail.prepared_request_key)
        self.assertEqual(read_request(attempt, "request_snapshot"), self.value)
        attempt.preparation_id = uuid4()
        with self.assertRaises(ExecutionRequestStorageError):
            read_request(attempt, "request_snapshot")

    def test_missing_wrong_hash_size_encoding_and_scope_fail_closed(self):
        store_prepared_request(self.detail, self.value)
        for field, bad in (("prepared_request_sha256", "0" * 64), ("prepared_request_size_bytes", 4),
                ("prepared_request_size_bytes", True), ("prepared_request_schema_version", True),
                ("prepared_request_key", "../secret"), ("preparation_id", uuid4())):
            altered = deepcopy(self.detail)
            setattr(altered, field, bad)
            with self.subTest(field=field), self.assertRaises(ExecutionRequestStorageError):
                read_request(altered, "prepared_request")
        default_storage.delete(self.detail.prepared_request_key)
        with self.assertRaises(ExecutionRequestStorageError):
            read_request(self.detail, "prepared_request")
        default_storage.save(self.detail.prepared_request_key, ContentFile(b"\xff"))
        with self.assertRaises(ExecutionRequestStorageError):
            read_request(self.detail, "prepared_request")

    def test_readback_and_write_failures_are_storage_errors_not_raw_paths(self):
        for method in ("save", "open"):
            with self.subTest(method=method):
                detail = deepcopy(self.detail)
                with patch.object(default_storage, method, side_effect=OSError("private filesystem details")):
                    with self.assertRaises(ExecutionRequestStorageError) as failure:
                        store_prepared_request(detail, self.value)
                self.assertNotIn("private filesystem details", str(failure.exception))

    def test_document_policy_not_json_column_ceiling_and_partial_manifest(self):
        with patch("studio.services.execution_request_storage.boundary_int", return_value=100):
            with self.assertRaises(ExecutionRequestSizeError):
                request_document(self.value)
        with self.assertRaises(ValidationError):
            json_contract(self.value)  # Database JSON contracts remain bounded.
        self.assertIsNone(read_request(self.detail, "prepared_request"))
        self.detail.prepared_request_sha256 = "0" * 64
        with self.assertRaises(ExecutionRequestStorageError):
            read_request(self.detail, "prepared_request")

    def test_request_parts_report_exact_utf8_size_and_config_without_source_text(self):
        value = {"text": "秘密原稿🌧" * 12000}
        size = len(canonical_json_bytes(value))
        for part in ("native_plan", "provider_payload", "transport", "document"):
            with self.subTest(part=part):
                self.assertEqual(request_part_contract(value, max_bytes=size, part=part).value, value)
                with self.assertRaises(ExecutionRequestSizeError) as rejected:
                    request_part_contract(value, max_bytes=size - 1, part=part)
                diagnostic = rejected.exception.diagnostic
                self.assertEqual(diagnostic["actual_bytes"], size)
                self.assertEqual(diagnostic["max_bytes"], size - 1)
                self.assertEqual(diagnostic["configuration_key"], "security.snapshot.stage_max_bytes")
                self.assertNotIn("秘密原稿", str(rejected.exception))
        with patch("studio.services.execution_request_storage.boundary_int", return_value=size - 1), \
                patch.object(default_storage, "save") as save, self.assertRaises(ExecutionRequestSizeError):
            store_prepared_request(self.detail, {**self.value, "large": value})
        save.assert_not_called()
        self.assertIsNone(self.detail.prepared_request_key)

    def test_migration_rejects_corrupt_source_before_any_file_or_manifest_write(self):
        migration = import_module("studio.migrations.0194_managed_execution_requests")
        value = {"schema_version": 1, "preparation_id": str(self.detail.preparation_id)}
        contract = request_document(value)
        row = SimpleNamespace(pk=uuid4(), preparation_id=self.detail.preparation_id, prepared_request=value,
            prepared_request_schema_version=1, prepared_request_size_bytes=contract.size_bytes,
            prepared_request_sha256="0" * 64)
        manager = Mock()
        manager.using.return_value = manager
        manager.exclude.return_value.select_related.return_value.iterator.return_value = iter([row])
        apps = Mock()
        apps.get_model.return_value = SimpleNamespace(objects=manager)
        with patch.object(default_storage, "save") as save, self.assertRaises(RuntimeError):
            migration.move_requests(apps, SimpleNamespace(connection=SimpleNamespace(alias="fixture")))
        save.assert_not_called()
        manager.filter.assert_not_called()

    def test_private_file_has_no_public_route(self):
        from django.urls import Resolver404, resolve
        store_prepared_request(self.detail, self.value)
        for prefix in ("/", "/media/", "/assets/"):
            with self.subTest(prefix=prefix), self.assertRaises(Resolver404):
                resolve(prefix + self.detail.prepared_request_key)

    def test_manifest_only_does_not_read_file_or_current_policy_but_full_reader_still_enforces_it(self):
        from studio.services.execution_request_storage import read_request_manifest
        with patch("studio.services.execution_request_storage.boundary_int", side_effect=AssertionError("No limit needed for absent file")):
            self.assertIsNone(read_request(self.detail, "prepared_request"))
        contract = store_prepared_request(self.detail, self.value)
        with patch.object(default_storage, "open", side_effect=AssertionError("No file read")), \
                patch("studio.services.execution_request_storage.boundary_int", return_value=contract.size_bytes - 1):
            self.assertEqual(read_request_manifest(self.detail, "prepared_request")["size_bytes"], contract.size_bytes)
            with self.assertRaises(ExecutionRequestStorageError):
                read_request(self.detail, "prepared_request")
