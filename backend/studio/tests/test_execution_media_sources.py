"""Preparation-to-staging source checks against actual managed PNG files."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from studio.models import Asset
from studio.services.execution_media_sources import resolve_execution_workflow_media_sources
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_input_resolution as asset_fixtures
from studio.tests import test_execution_selected_outputs as selected_fixtures


class ExecutionAssetMediaSourceTests(TestCase):
    setUpTestData = classmethod(asset_fixtures.ExecutionInputResolutionTests.setUpTestData.__func__)
    setUp = asset_fixtures.ExecutionInputResolutionTests.setUp
    definition = asset_fixtures.ExecutionInputResolutionTests.definition
    item = asset_fixtures.ExecutionInputResolutionTests.item
    resolve = asset_fixtures.ExecutionInputResolutionTests.resolve

    def read(self, inputs, **changes):
        return resolve_execution_workflow_media_sources(self.definition(), json_contract(inputs),
            **{"owner": self.owner, "max_media_bytes": 65536, **changes})

    def test_exact_bytes_reauthorize_each_use_without_persisting_paths(self):
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        inputs = self.resolve([self.item(item_key="left"), self.item(item_key="right")])
        original = deepcopy(inputs)
        paths = self.read(inputs)
        self.assertEqual(list(paths), ["left", "right"])
        self.assertEqual(paths["left"], paths["right"])
        self.assertEqual(paths["left"].read_bytes(), self.bytes)
        self.assertEqual(inputs, original)
        self.assertNotIn(str(self.root), str(inputs))

    def test_deactivation_owner_or_changed_content_block_reuse(self):
        inputs = self.resolve()
        with self.assertRaises((PermissionDenied, ValidationError)):
            self.read(inputs, owner=self.other)
        self.asset.is_active = False
        self.asset.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            self.read(inputs)
        self.asset.is_active = True
        self.asset.save(update_fields=["is_active"])
        self.read(inputs)["use-one"].write_bytes(b"changed bytes, not the authorized PNG")
        with self.assertRaises(ValidationError):
            self.read(inputs)

    def test_context_only_media_are_not_resolved_for_upload(self):
        inputs = self.resolve()
        self.spec["targets"] = [{"target_kind": "profile_input", "target_key": "reference-context", "is_primary": False}]
        inputs["workflow_slots"] = {}
        definition = self.definition(targets={})
        with patch("studio.services.execution_media_sources.resolve_execution_asset_media", side_effect=AssertionError("No context upload")):
            self.assertEqual(resolve_execution_workflow_media_sources(definition, json_contract(inputs),
                owner=self.owner, max_media_bytes=65536), {})


class ExecutionSelectedMediaSourceTests(TestCase):
    setUpTestData = classmethod(selected_fixtures.ExecutionSelectedOutputTests.setUpTestData.__func__)
    setUp = selected_fixtures.ExecutionSelectedOutputTests.setUp
    definition = selected_fixtures.ExecutionSelectedOutputTests.definition
    item = selected_fixtures.ExecutionSelectedOutputTests.item
    resolve = selected_fixtures.ExecutionSelectedOutputTests.resolve

    def read(self, inputs, **changes):
        return resolve_execution_workflow_media_sources(self.definition(), json_contract(inputs), **{
            "owner": self.owner, "max_media_bytes": 65536, "production_run_id": str(self.run.pk),
            "eligible_stage_ids": [self.stage.pk], **changes})

    def test_selected_artifact_path_never_requires_an_asset_proxy(self):
        inputs, before = self.resolve(), Asset.objects.count()
        paths = self.read(inputs)
        self.assertEqual(paths[self.item()["item_key"]], self.path)
        self.assertEqual(paths[self.item()["item_key"]].read_bytes(), self.bytes)
        self.assertIsNone(inputs["logical_values"]["references"][0]["asset_id"])
        self.assertEqual(Asset.objects.count(), before)

    def test_request_cannot_authorize_its_own_upstream_and_lost_selection_blocks_reuse(self):
        inputs = self.resolve()
        with self.assertRaises(ValidationError):
            self.read(inputs, eligible_stage_ids=[])
        with self.assertRaises((PermissionDenied, ValidationError)):
            self.read(inputs, production_run_id=None)
        self.artifact.status = "available"
        self.artifact.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            self.read(inputs)

    def test_changed_manifest_or_bytes_cannot_keep_the_frozen_media_mapping(self):
        inputs = self.resolve()
        self.path.write_bytes(b"different artifact bytes")
        with self.assertRaises(ValidationError):
            self.read(inputs)
