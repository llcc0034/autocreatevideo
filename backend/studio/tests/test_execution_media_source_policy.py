"""Isolated instance choices and real managed-media reads; no provider calls."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.services.asset_provenance import ASSET_UPLOAD_SOURCE_INFO
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_instances import execution_version_configuration, clone_execution_draft
from studio.services.execution_media_source_policy import execution_media_source_policy, select_execution_media_source
from studio.services.execution_media_sources import resolve_execution_workflow_media_sources
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_input_resolution as media_fixture
from studio.tests import test_execution_instances as registry_fixture
from studio.tests import test_execution_selected_outputs as selected_fixture


def choices(**changes):
    return {"schema_version": 1, "allowed_sources": ["asset_library", "asset_upload", "selected_output"],
        "default_source": "asset_library", "allow_override": True, "order": "provided", **changes}


class ExecutionMediaPolicyContractTests(SimpleTestCase):
    def test_projection_keeps_one_constraint_owner_and_copies_original_context(self):
        spec = {"key": "images", "kind": "image", "min_items": 1, "max_items": 1,
            "semantic_contract": {"allowed_purposes": []}, "media_source_policy": choices()}
        item = {"kind": "asset", "item_key": "one", "purpose": None,
            "asset_id": "12345678-1234-1234-1234-123456789012", "scope_ref": {"kind": "request"}}
        source = {"selection": {"source": "asset_library", "items": [item]}}
        resolved, evidence = select_execution_media_source(spec, source)
        self.assertEqual(evidence, {"source": "asset_library", "overridden": False})
        self.assertEqual(resolved, {"items": [item]})
        resolved["items"][0]["scope_ref"]["kind"] = "changed"
        self.assertEqual(item["scope_ref"], {"kind": "request"})
        spec["max_items"] = 0
        with self.assertRaises(ValidationError):
            select_execution_media_source(spec, source)


class ExecutionMediaPolicyAuthoringTests(TestCase):
    setUpTestData = classmethod(registry_fixture.ExecutionRegistryTests.setUpTestData.__func__)
    selection = registry_fixture.ExecutionRegistryTests.selection
    managed_draft = registry_fixture.ExecutionRegistryTests.managed_draft
    update_draft = registry_fixture.ExecutionRegistryTests.update_draft
    input_payload = registry_fixture.ExecutionRegistryTests.input_payload

    def test_api_save_read_clone_and_remove_use_existing_input_rows(self):
        version = self.managed_draft()
        client = APIClient()
        client.force_authenticate(self.actor)
        url = f"/api/v1/model-execution-instance-versions/{version.pk}/"
        row = {**self.input_payload(), "media_source_policy": choices()}
        with patch("studio.services.execution_instances.verify_workflow_version_integrity"):
            response = client.patch(url, {"expected_revision": 1, "inputs": [row]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        version.refresh_from_db()
        saved = execution_version_configuration(version)
        self.assertEqual(saved["inputs"][0]["media_source_policy"], choices())
        self.assertNotIn("min_items", saved["inputs"][0]["media_source_policy"])
        with patch("studio.services.execution_instances.verify_workflow_version_integrity"):
            clone = clone_execution_draft(source_id=version.pk, expected_revision=2, actor=self.actor)
        self.assertEqual(execution_version_configuration(clone), saved)
        changed = self.update_draft(version, {"inputs": [{**row, "media_source_policy": None}]}, expected_revision=2)
        self.assertNotIn("media_source_policy", execution_version_configuration(changed)["inputs"][0])
        self.assertEqual(execution_version_configuration(clone), saved)

    def test_invalid_choices_rollback_and_cannot_duplicate_constraints(self):
        version = self.managed_draft()
        row = {**self.input_payload(), "media_source_policy": choices()}
        version = self.update_draft(version, {"inputs": [row]})
        original = version.inputs.get().pk
        for policy in ({}, choices(min_items=1), choices(allow_override=1), choices(order="name"),
                choices(default_source="missing"), choices(allowed_sources=["bible_reference"]),
                choices(allowed_sources=["asset_library", "asset_library"])):
            with self.subTest(policy=policy), self.assertRaises(ValidationError):
                self.update_draft(version, {"inputs": [{**row, "media_source_policy": policy}]}, expected_revision=2)
            self.assertEqual(version.inputs.get().pk, original)
        for kind in ("text", "json"):
            with self.assertRaises(ValidationError):
                execution_media_source_policy({**row, "kind": kind})


class ExecutionMediaPolicyResolutionTests(TestCase):
    setUpTestData = classmethod(media_fixture.ExecutionInputResolutionTests.setUpTestData.__func__)
    definition = media_fixture.ExecutionInputResolutionTests.definition
    item = media_fixture.ExecutionInputResolutionTests.item
    resolve = media_fixture.ExecutionInputResolutionTests.resolve

    def setUp(self):
        media_fixture.ExecutionInputResolutionTests.setUp(self)
        self.spec["media_source_policy"] = choices()

    def source(self, mode="asset_library", items=None):
        return {"source": mode, "items": [self.item()] if items is None else items}

    def test_frozen_choice_uses_original_reader_and_stages_exact_bytes(self):
        definition = json_contract(deepcopy(self.definition().value))
        request = {"references": {"selection": self.source()}}
        original = deepcopy(request)
        self.spec["media_source_policy"] = choices(default_source="asset_upload")
        with CaptureQueriesContext(connection) as queries:
            result = self.resolve(sources=request, definition=definition)
        self.assertFalse(any("studio_executioninputspec" in query["sql"] for query in queries))
        record = result["logical_values"]["references"][0]
        self.assertEqual(record["source_selection"], {"source": "asset_library", "overridden": False})
        self.assertEqual(result["input_provenance"], [])  # Non-media provenance remains non-media.
        self.assertEqual(request, original)
        staged = resolve_execution_workflow_media_sources(definition, json_contract(result), owner=self.owner, max_media_bytes=65536)
        self.assertEqual(staged["use-one"].read_bytes(), self.bytes)

    def test_override_replaces_without_reading_base_and_checks_upload_writer(self):
        self.asset.source_info = ASSET_UPLOAD_SOURCE_INFO
        self.asset.save(update_fields=["source_info"])
        request = {"references": {"selection": {"source": "asset_library", "items": [{"not_read": True}]},
            "override": self.source("asset_upload")}}
        result = self.resolve(sources=request)
        self.assertEqual(result["logical_values"]["references"][0]["source_selection"], {"source": "asset_upload", "overridden": True})
        self.asset.source_info = "not an upload"
        self.asset.save(update_fields=["source_info"])
        with self.assertRaisesMessage(ValidationError, "上传来源事实"):
            self.resolve(sources=request)

    def test_wrong_sources_cannot_bypass_policy_or_merge_or_fallback(self):
        cases = [{"items": [self.item()]}, {"selection": self.source("asset_upload")},
            {"selection": self.source("bible_reference")}, {"selection": self.source(items=[])},
            {"selection": self.source(items=[self.item(local_path="fake")])},
            {"selection": self.source("selected_output")}, {"selection": self.source(), "override": None}]
        with patch("studio.services.execution_media_sources.resolve_execution_asset_media", side_effect=AssertionError("invalid choice must not read")):
            for request in cases:
                with self.subTest(request=request), self.assertRaises(ValidationError):
                    self.resolve(sources={"references": request})
            self.spec["media_source_policy"]["allow_override"] = False
            with self.assertRaises(ValidationError):
                self.resolve(sources={"references": {"selection": self.source(), "override": self.source()}})

    def test_usage_order_and_original_scope_subject_checks_remain(self):
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        items = [self.item(item_key="second"), self.item(item_key="first")]
        result = self.resolve(sources={"references": {"selection": self.source(items=items)}})
        self.assertEqual([row["item_key"] for row in result["logical_values"]["references"]], ["second", "first"])
        for item in (self.item(purpose="unknown"), self.item(scope_ref={"kind": "unknown"}),
                self.item(subject_ref={"kind": "external", "id": "missing"})):
            with self.assertRaises(ValidationError):
                self.resolve(sources={"references": {"selection": self.source(items=[item])}})


class ExecutionMediaPolicySelectedTests(TestCase):
    setUpTestData = classmethod(selected_fixture.ExecutionSelectedOutputTests.setUpTestData.__func__)
    setUp = selected_fixture.ExecutionSelectedOutputTests.setUp
    definition = selected_fixture.ExecutionSelectedOutputTests.definition
    item = selected_fixture.ExecutionSelectedOutputTests.item

    def test_exact_selected_output_retains_upstream_authorization(self):
        self.spec["media_source_policy"] = choices(default_source="selected_output")
        request = {"references": {"selection": {"source": "selected_output", "items": [self.item()]}}}
        params = {"owner": self.owner, "subject_facts": [], "max_media_bytes": 65536,
            "project_id": self.project.pk, "production_run_id": str(self.run.pk), "eligible_stage_ids": [self.stage.pk]}
        result = resolve_execution_input_sources(self.definition(), request, **params).value
        record = result["logical_values"]["references"][0]
        self.assertEqual(record["artifact_id"], str(self.artifact.pk))
        self.assertEqual(record["origin"]["decision_id"], str(self.decision.pk))
        with self.assertRaises(ValidationError):
            resolve_execution_input_sources(self.definition(), request, **{**params, "eligible_stage_ids": []})
