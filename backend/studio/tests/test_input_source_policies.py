from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import Asset
from studio.services.input_source_policies import (
    input_source_options, validate_media_source_policy, select_media_source, resolve_media_source_selection,
)
from studio.tests.test_asset_management_api import image_upload
from studio.tests import test_execution_input_resolution as asset_fixtures
from studio.tests import test_execution_selected_outputs as selected_fixtures


def policy(**changes):
    return {"schema_version": 1, "media_kind": "image", "allowed_sources": ["asset_library", "asset_upload"],
            "default_source": "asset_library", "allow_override": True, "min_items": 1, "max_items": 2,
            "allowed_purposes": ["appearance", "composition"], "order": "provided", **changes}


def selection(asset_id=None, **changes):
    return {"source": "asset_library", "items": [{"item_key": "use-1", "purpose": "appearance",
            "asset_id": str(asset_id or uuid4())}], **changes}


class MediaSourcePolicyTests(SimpleTestCase):
    def test_catalog_is_detached_and_unimplemented_reference_reader_is_explicit(self):
        catalog = input_source_options()
        self.assertEqual(catalog["configuration_owner"], "recipe_instance_or_profile")
        reference = next(row for row in catalog["sources"] if row["value"] == "bible_reference")
        self.assertFalse(reference["available"])
        self.assertIsNone(reference["reader"])
        catalog["sources"].clear()
        self.assertEqual(len(input_source_options()["sources"]), 6)

    def test_invalid_policy_matrix_without_coercion_or_hidden_defaults(self):
        changes = [{"schema_version": True}, {"media_kind": []}, {"allow_override": 1},
            {"allowed_sources": []}, {"allowed_sources": ["asset_library", "asset_library"]},
            {"allowed_sources": ["literal"]}, {"allowed_sources": ["bible_reference"]},
            {"default_source": "unknown"}, {"allowed_purposes": [None]}, {"min_items": False},
            {"min_items": -1}, {"min_items": 3}, {"max_items": 1.5}, {"order": "name"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_media_source_policy(policy(**change))
        for key in policy():
            draft = policy()
            draft.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValidationError):
                validate_media_source_policy(draft)

    def test_explicit_override_replaces_selection_and_preserves_input(self):
        base, other = selection(), selection(source="asset_upload")
        original = deepcopy([base, other])
        chosen, changed = select_media_source(policy(), base, override=other)
        self.assertTrue(changed)
        self.assertEqual(chosen, other)
        chosen["items"].clear()
        self.assertEqual([base, other], original)
        with self.assertRaises(ValidationError):
            select_media_source(policy(allow_override=False), base, override=base)
        with self.assertRaises(ValidationError):
            select_media_source(policy(), other)
        with self.assertRaises(ValidationError):
            select_media_source(policy(), base, override=None)

    def test_empty_missing_quantity_usage_and_identity_are_distinct(self):
        self.assertEqual(select_media_source(policy(min_items=0), selection(items=[]))[0]["items"], [])
        for value in (None, {}, selection(items=[]), selection(items=[{}, {}, {}]),
                      selection(items=[{"item_key": "x", "purpose": "wrong", "asset_id": str(uuid4())}])):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                select_media_source(policy(), value)
        base = selection()
        for change in ({"item_key": ""}, {"asset_id": 12}, {"asset_id": "temp.png"},
                       {"path": "local.png"}, {"purpose": None}, {"decision_id": str(uuid4())}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                select_media_source(policy(), selection(items=[{**base["items"][0], **change}]))
        with self.assertRaises(ValidationError):
            select_media_source(policy(), selection(items=[*base["items"], *base["items"]]))

    def test_same_asset_different_uses_keep_provided_order(self):
        base = selection()
        items = [{**base["items"][0], "item_key": "z", "purpose": "composition"},
                 {**base["items"][0], "item_key": "a"}]
        self.assertEqual(select_media_source(policy(), selection(items=items))[0]["items"], items)
        items[0]["purpose"] = None
        self.assertEqual(select_media_source(policy(allowed_purposes=[]), selection(items=items[:1]))[0]["items"], items[:1])


class MediaSourceResolutionTests(TestCase):
    setUpTestData = classmethod(asset_fixtures.ExecutionInputResolutionTests.setUpTestData.__func__)
    setUp = asset_fixtures.ExecutionInputResolutionTests.setUp

    def resolve(self, value=None, **changes):
        return resolve_media_source_selection(changes.pop("policy", policy()), value or selection(self.asset.pk),
            owner=changes.pop("owner", self.owner), max_media_bytes=65536,
            max_media_items=changes.pop("max_media_items", 100), **changes)

    def test_existing_asset_reader_preserves_exact_bytes_and_no_paths_or_writes(self):
        base = selection(self.asset.pk)
        base["items"].append({**base["items"][0], "item_key": "other-use", "purpose": "composition"})
        with CaptureQueriesContext(connection) as queries:
            result = self.resolve(base)
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        self.assertEqual([row["position"] for row in result["items"]], [0, 1])
        self.assertEqual(result["items"][0]["media_facts"]["sha256"], self.asset.checksum)
        self.assertEqual(result["items"][0]["asset_id"], str(self.asset.pk))
        self.assertNotIn(str(self.root), str(result))

    def test_invalid_override_does_not_read_valid_configured_fallback(self):
        other = selection(uuid4(), source="asset_upload")
        with patch("studio.services.execution_media_sources.resolve_execution_asset_media", side_effect=ValidationError("missing")) as reader:
            with self.assertRaisesMessage(ValidationError, "missing"):
                self.resolve(override=other)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(reader.call_args.kwargs["asset_id"], other["items"][0]["asset_id"])

    def test_service_quantity_limit_precedes_io_and_repeated_uses_share_verification(self):
        from studio.services.execution_media_sources import resolve_execution_asset_media
        base = selection(self.asset.pk)
        base["items"].append({**base["items"][0], "item_key": "another", "purpose": "composition"})
        with patch("studio.services.execution_media_sources.resolve_execution_asset_media", wraps=resolve_execution_asset_media) as reader:
            with self.assertRaisesMessage(ValidationError, "最多核对1项"):
                self.resolve(base, max_media_items=1)
            reader.assert_not_called()
            self.assertEqual(len(self.resolve(base)["items"]), 2)
            self.assertEqual(reader.call_count, 1)

    def test_upload_mode_cannot_assert_an_asset_without_original_upload_fact(self):
        with self.assertRaisesMessage(ValidationError, "没有上传来源事实"):
            self.resolve(selection(self.asset.pk, source="asset_upload"), policy=policy(default_source="asset_upload"))

    def test_retired_wrong_owner_kind_and_file_drift_fail_without_substitution(self):
        with self.assertRaises((ValidationError, PermissionDenied)):
            self.resolve(owner=self.other)
        with self.assertRaises(ValidationError):
            self.resolve(policy=policy(media_kind="video"))
        self.asset.is_active = False
        self.asset.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            self.resolve()
        self.asset.is_active = True
        self.asset.save(update_fields=["is_active"])
        self.asset.file.open("wb").write(b"changed")
        self.asset.file.close()
        with self.assertRaises(ValidationError):
            self.resolve()

    def test_catalog_and_preview_api_permissions_and_zero_write(self):
        client = APIClient()
        url = "/api/v1/data-structure-versions/"
        self.assertIn(client.get(url + "input-source-options/").status_code, (401, 403))
        client.force_authenticate(self.owner)
        self.assertEqual(client.get(url + "input-source-options/").status_code, 200)
        self.assertEqual(client.post(url + "media-source-preview/", {}, format="json").status_code, 403)
        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        self.assertEqual(client.get(url + "input-source-options/").data["schema_version"], 1)
        with CaptureQueriesContext(connection) as queries:
            response = client.post(url + "media-source-preview/", {"policy": policy(), "selection": selection(self.asset.pk)}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["valid"])
        self.assertTrue(response.data["preview_only"])
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        rejected = client.post(url + "media-source-preview/", {"policy": policy(), "selection": None, "production_run_id": str(uuid4())}, format="json")
        self.assertEqual(rejected.status_code, 400)
        invalid = client.post(url + "media-source-preview/", {"policy": policy(), "selection": selection(uuid4())}, format="json")
        self.assertFalse(invalid.data["valid"])
        self.assertIsNone(invalid.data["resolved"])
        self.assertIn("selection.items[0]", invalid.data["errors"])

    def test_formal_upload_and_library_selection_resolve_the_same_asset(self):
        client = APIClient()
        client.force_authenticate(self.owner)
        created = client.post("/api/v1/assets/", {"kind": "reference", "name": "Uploaded fixture",
            "description": "isolated", "prompt_hint": "", "file": image_upload()}, format="multipart")
        self.assertEqual(created.status_code, 201, created.data)
        uploaded = Asset.objects.get(pk=created.data["id"])
        chosen = selection(uploaded.pk, source="asset_upload")
        result = self.resolve(chosen, policy=policy(default_source="asset_upload"))
        library = self.resolve(selection(uploaded.pk))
        self.assertEqual(result["items"], library["items"])
        self.assertEqual(result["source"], "asset_upload")
        self.assertNotIn("generated_from", result)


class MediaSelectedOutputPolicyTests(TestCase):
    setUpTestData = classmethod(selected_fixtures.ExecutionSelectedOutputTests.setUpTestData.__func__)
    setUp = selected_fixtures.ExecutionSelectedOutputTests.setUp

    def test_upstream_uses_exact_existing_reader_and_requires_authorized_context(self):
        value = {"source": "selected_output", "items": [{"item_key": "upstream-1", "purpose": "appearance",
            "decision_id": str(self.decision.pk), "artifact_id": str(self.artifact.pk)}]}
        config = policy(allowed_sources=["selected_output"], default_source="selected_output")
        with self.assertRaisesMessage(ValidationError, "未提供"):
            resolve_media_source_selection(config, value, owner=self.owner, max_media_bytes=65536, max_media_items=100)
        result = resolve_media_source_selection(config, value, owner=self.owner, max_media_bytes=65536, max_media_items=100,
            production_run_id=str(self.run.pk), eligible_stage_ids=[self.stage.pk])
        self.assertEqual(result["items"][0]["artifact_id"], str(self.artifact.pk))
        self.assertEqual(result["items"][0]["asset_id"], str(self.artifact.asset_id) if self.artifact.asset_id else None)
        with self.assertRaises((PermissionDenied, ValidationError)):
            resolve_media_source_selection(config, value, owner=self.owner, max_media_bytes=65536, max_media_items=100,
                production_run_id=str(uuid4()), eligible_stage_ids=[self.stage.pk])
