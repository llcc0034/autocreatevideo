"""Original script HTTP import plus explicit deterministic JSON attachment."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import GenerationAttempt, ScriptRevision, ScriptRevisionStructure, SeriesProject
from studio.services.business_outputs import write_output
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests import test_script_revision_structures as storage_tests


class ScriptRevisionStructureApiTests(TestCase):
    make_output = storage_tests.ScriptRevisionStructureTests.make_output

    def revise(self, revision, content, **changes):
        detail = self.client.get(self.url(revision)).data["structure"]
        data = {"source_id": revision["source"], "content": content,
            "structure_basis": {"revision_id": revision["id"], "document_sha256": detail["document_sha256"]}}
        data.update(changes)
        return self.client.post("/api/v1/script-revisions/", data, format="json")

    def test_field_revision_reuses_frozen_schema_and_atomic_writer_without_adoption(self):
        from studio.services.data_structures import validate_data_structure_snapshot
        for layout in ("a", "b"):
            output, raw = self.make_output(layout)
            revision = self.source(raw)
            saved = self.save(revision, output).data["structure"]
            self.assertTrue(saved["can_revise"])
            self.assertEqual(saved["json_schema"], validate_data_structure_snapshot(saved["document"]["output"]["data_structure"]))
            raw["自定义.字段"] = {"note": "人工修改", "zero": 0, "flag": False, "empty": None}
            write_output(actor=self.actor, output_id=output.pk, expected_revision=output.revision, action="retire")
            revised = self.revise(revision, json.dumps(raw, ensure_ascii=False, indent=2))
            self.assertEqual(revised.status_code, 201, revised.data)
            self.assertEqual(revised.data["source"], revision["source"])
            self.assertEqual(revised.data["status"], "preview")
            self.assertNotEqual(revised.data["id"], revision["id"])
            structure = self.client.get(self.url(revised.data)).data["structure"]
            self.assertEqual(structure["document"]["output"], saved["document"]["output"])
            self.assertEqual(structure["document"]["projection"]["source_value"], raw)
            self.assertEqual(self.client.get(self.url(revision)).data["structure"], saved)
            self.assertEqual(ScriptRevision.objects.get(pk=revision["id"]).content, revision["content"])
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, "")
        self.assertEqual(GenerationAttempt.objects.count(), 0)

    def test_invalid_field_draft_rolls_back_revision_and_source_state(self):
        revision = self.source()
        self.assertEqual(self.save(revision).status_code, 200)
        confirmed = self.client.post(f'/api/v1/script-revisions/{revision["id"]}/confirm/', {}, format="json")
        self.assertEqual(confirmed.status_code, 200)
        bad = deepcopy(self.raw)
        bad["episodes"][0]["shots"][0]["references"][0]["entity_key"] = "missing"
        for content in ('{"invalid":', json.dumps(bad)):
            response = self.revise(revision, content)
            self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(ScriptRevision.objects.count(), 1)
            self.assertEqual(ScriptRevisionStructure.objects.count(), 1)
            original = ScriptRevision.objects.select_related("source").get(pk=revision["id"])
            self.assertEqual((original.status, original.source.status), ("confirmed", "confirmed"))
        self.assertIn("source.episodes[0].shots[0].references[0].entity_key", str(response.data))

    def test_wrong_basis_and_unknown_basis_fields_do_not_create_revision(self):
        first, second = self.source(), self.source()
        self.save(first); self.save(second)
        digest = self.client.get(self.url(first)).data["structure"]["document_sha256"]
        for basis in ({"revision_id": first["id"], "document_sha256": "0" * 64},
                {"revision_id": second["id"], "document_sha256": digest},
                {"revision_id": first["id"], "document_sha256": digest, "output_version_id": str(self.output.pk)}):
            response = self.revise(first, first["content"], structure_basis=basis)
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ScriptRevision.objects.count(), 2)
        self.assertEqual(ScriptRevisionStructure.objects.count(), 2)

    def test_generated_source_and_foreign_owner_cannot_bypass_original_revision_boundary(self):
        from studio.models import ScriptSource
        revision = self.source(); self.save(revision)
        digest = self.client.get(self.url(revision)).data["structure"]["document_sha256"]
        ScriptSource.objects.filter(pk=revision["source"]).update(source_type="ai_generated")
        self.assertFalse(self.client.get(self.url(revision)).data["structure"]["can_revise"])
        self.assertEqual(self.revise(revision, revision["content"]).status_code, 400)
        self.client.force_authenticate(self.other)
        response = self.client.post("/api/v1/script-revisions/", {"source_id": revision["source"],
            "content": revision["content"], "structure_basis": {"revision_id": revision["id"], "document_sha256": digest}}, format="json")
        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(ScriptRevision.objects.count(), 1)

    def test_structure_write_failure_rolls_back_new_preview(self):
        from django.core.exceptions import ValidationError
        revision = self.source(); self.save(revision)
        with patch("studio.services.script_revision_structures.save_script_revision_structure", side_effect=ValidationError("isolated write failed")):
            response = self.revise(revision, revision["content"])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ScriptRevision.objects.count(), 1)
        self.assertEqual(ScriptRevisionStructure.objects.count(), 1)

    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="script-json-owner")
        self.other = get_user_model().objects.create_user(username="script-json-other")
        self.project = SeriesProject.objects.create(owner=self.actor, title="JSON import", **PROJECT_CONFIG)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.output, self.raw = self.make_output()
        self.network = patch("studio.services.provider_registry._request_json", side_effect=AssertionError("No provider in JSON import"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def source(self, raw=None, *, text=None):
        text = text if text is not None else " \r\n" + json.dumps(self.raw if raw is None else raw, ensure_ascii=False, indent=2) + "\r\n "
        response = self.client.post("/api/v1/script-sources/", {"project_id": str(self.project.pk), "content": text}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["revision"]

    def url(self, revision):
        return f'/api/v1/script-revisions/{revision["id"]}/structure/'

    def save(self, revision, output=None, **extra):
        return self.client.post(self.url(revision), {"output_version_id": str((output or self.output).pk), **extra}, format="json")

    def test_actual_http_import_two_layouts_saves_projection_without_model_or_adoption(self):
        values = []
        for layout in ("a", "b"):
            output, raw = self.make_output(layout)
            revision = self.source(raw)
            before = self.client.get(self.url(revision))
            self.assertEqual(before.data, {"structure": None})
            saved = self.save(revision, output)
            self.assertEqual(saved.status_code, 200, saved.data)
            document = saved.data["structure"]["document"]
            self.assertEqual(document["output"]["id"], str(output.pk))
            self.assertEqual(document["projection"]["source_value"], raw)
            values.append(document["projection"]["value"])
            original = ScriptRevision.objects.get(pk=revision["id"])
            self.assertEqual(original.content, revision["content"])
            self.assertEqual(original.status, "preview")
            self.assertIsNone(original.confirmed_by_id)
            self.assertIsNone(original.origin_generation_attempt_id)
            self.assertEqual(self.client.get(self.url(revision)).data, saved.data)
        self.assertEqual(values[0], values[1])
        self.assertEqual(GenerationAttempt.objects.count(), 0)
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, "")

    def test_options_are_read_only_published_script_versions_paged_for_regular_users(self):
        draft, _ = self.make_output(publish=False)
        other, _ = self.make_output(parser="structured_json.v1")
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/v1/script-revisions/structure-options/", {"page_size": 1, "search": "Isolated"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.output.pk))
        self.assertEqual(set(response.data["results"][0]), {"id", "output_key", "name", "description", "version"})
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        sql = " ".join(q["sql"] for q in queries)
        self.assertNotIn('"parser_contract"', sql)
        self.assertNotIn('"document"', sql)
        for output in (draft, other):
            self.assertEqual(self.save(self.source(), output).status_code, 400)

    def test_invalid_json_and_relationship_keep_original_but_never_create_partial_detail(self):
        bad = deepcopy(self.raw)
        bad["episodes"][0]["shots"][0]["references"][0]["entity_key"] = "not-declared"
        for text in ("free text, no implicit AI", "{bad JSON", json.dumps(bad)):
            revision = self.source(text=text)
            response = self.save(revision)
            self.assertEqual(response.status_code, 400, response.data)
            if text == json.dumps(bad):
                self.assertIn("source.episodes[0].shots[0].references[0].entity_key", str(response.data))
            self.assertEqual(ScriptRevision.objects.get(pk=revision["id"]).content, text)
            self.assertEqual(self.client.get(self.url(revision)).data, {"structure": None})
        self.assertEqual(ScriptRevisionStructure.objects.count(), 0)

    def test_same_output_replay_and_read_never_reparse_even_after_retirement(self):
        revision = self.source()
        saved = self.save(revision)
        self.assertEqual(saved.status_code, 200, saved.data)
        with patch("studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError("must not reparse")):
            self.assertEqual(self.save(revision).data, saved.data)
            write_output(actor=self.actor, output_id=self.output.pk, expected_revision=self.output.revision, action="retire")
            self.assertEqual(self.client.get(self.url(revision)).data, saved.data)
        self.assertEqual(self.save(revision).status_code, 400)
        self.assertEqual(ScriptRevisionStructure.objects.count(), 1)

    def test_source_visibility_and_auth_apply_to_read_and_write(self):
        revision = self.source()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.url(revision)).status_code, 404)
        self.assertEqual(self.save(revision).status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url(revision)).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/script-revisions/structure-options/").status_code, 401)
        self.assertEqual(ScriptRevisionStructure.objects.count(), 0)

    def test_unknown_fields_and_missing_selection_are_not_inferred(self):
        revision = self.source()
        self.assertEqual(self.client.post(self.url(revision), {}, format="json").status_code, 400)
        self.assertEqual(self.save(revision, content="replacement").status_code, 400)
        self.assertEqual(self.save(revision, expected_output={}).status_code, 400)
        self.assertEqual(ScriptRevisionStructure.objects.count(), 0)

    def test_confirmed_source_not_rewritten_and_new_revision_has_no_inherited_projection(self):
        revision = self.source()
        confirmed = self.client.post(f'/api/v1/script-revisions/{revision["id"]}/confirm/', {}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(self.save(revision).status_code, 400)
        new = self.client.post("/api/v1/script-revisions/", {"source_id": revision["source"], "content": revision["content"]}, format="json")
        self.assertEqual(new.status_code, 201, new.data)
        self.assertEqual(self.save(new.data).status_code, 200)
        self.assertEqual(self.client.get(self.url(revision)).data, {"structure": None})

    def test_changed_output_and_corrupt_detail_never_fall_back_or_replace(self):
        revision = self.source()
        saved = self.save(revision)
        self.assertEqual(saved.status_code, 200, saved.data)
        other, _ = self.make_output()
        self.assertEqual(self.save(revision, other).status_code, 400)
        self.assertEqual(self.client.get(self.url(revision)).data, saved.data)
        ScriptRevisionStructure.objects.filter(revision_id=revision["id"]).update(document_sha256="0" * 64)
        self.assertEqual(self.client.get(self.url(revision)).status_code, 400)
        self.assertEqual(self.save(revision).status_code, 400)

    def test_missing_configuration_is_explicit_conflict_without_detail_or_source_changes(self):
        from studio.services.configuration import ConfigurationResolutionError
        revision = self.source()
        with patch("studio.script_revision_structure_views.execution_configuration_max_bytes",
                side_effect=ConfigurationResolutionError("isolated missing boundary", code="config_missing", missing_keys=("test.capacity",))):
            response = self.save(revision)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "config_missing")
        self.assertEqual(ScriptRevisionStructure.objects.count(), 0)
        self.assertEqual(ScriptRevision.objects.get(pk=revision["id"]).content, revision["content"])
