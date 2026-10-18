"""Read-only saved-semantic evidence on the exact adopted manual script."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import GenerationAttempt, ScriptRevision, ScriptRevisionStructure
from studio.services.business_outputs import output_reference
from studio.services.script_revision_structures import create_structured_script_source
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
from studio.tests import test_script_input_structures as inputs


class ManualScriptStructureCheckTests(TestCase):
    setUp = inputs.ManualScriptInputStructureTests.setUp
    create = inputs.ManualScriptInputStructureTests.create
    post = inputs.ManualScriptInputStructureTests.post
    make_output = inputs.ManualScriptInputStructureTests.make_output
    imported = inputs.ManualScriptInputStructureTests.imported
    adopt = inputs.ManualScriptInputStructureTests.adopt

    def adopt_text(self, content):
        _, revision = create_pasted_script_source(project=self.project, content=content, user=self.actor)
        revision = confirm_script_revision(revision, user=self.actor)
        response, _ = self.adopt({"id": str(revision.pk), "content_sha256": revision.content_sha256})
        self.assertEqual(response.status_code, 201, response.data)
        return revision

    def check(self, status=200, params=None):
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-script-structure/", params or {})
        self.assertEqual(response.status_code, status, response.data)
        return response

    def test_no_adopted_script_does_not_guess_from_project_manuscript(self):
        self.project.master_script = '{"episodes": [{"episode": 1, "shots": []}]}'
        self.project.save(update_fields=("master_script",))
        response = self.check()
        self.assertEqual(response.data["status"], "no_script")
        self.assertIsNone(response.data["current_script"])
        self.assertIsNone(response.data["episode_count"])
        self.assertIsNone(response.data["shot_count"])
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_two_layouts_use_saved_projection_without_writes_reparse_or_cold_history(self):
        for selection, layout in enumerate(("a", "b")):
            revision, detail, raw, _ = self.imported(layout)
            response, _ = self.adopt(revision, selection=selection)
            self.assertEqual(response.status_code, 201, response.data)
            self.project.master_script = "other content"
            self.project.save(update_fields=("master_script",))
            with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                       side_effect=AssertionError("never reparse")), CaptureQueriesContext(connection) as queries:
                result = self.check().data
            self.assertEqual(result["status"], "structured")
            self.assertEqual((result["episode_count"], result["shot_count"]), (2, 4))
            self.assertEqual(result["current_script"]["id"], revision["id"])
            self.assertEqual(result["current_script"]["sha256"], revision["content_sha256"])
            self.run.refresh_from_db()
            self.assertEqual(result["current_script_decision_id"], str(self.run.script_adoption_decision_id))
            for query in queries:
                self.assertFalse(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")), query)
                self.assertNotIn('"manifest_metadata"', query["sql"])
                self.assertNotIn('"request_snapshot"', query["sql"])
            self.assertEqual(set(result), {"run_id", "current_script", "current_script_decision_id", "status",
                "reason_code", "reason", "episode_count", "shot_count"})
            saved = ScriptRevisionStructure.objects.get(revision_id=revision["id"])
            self.assertEqual(saved.document, detail["document"])
            self.assertEqual(saved.document["projection"]["source_value"], raw)
        self.assertFalse(self.project.episodes.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_raw_json_and_old_markers_are_not_a_saved_structure(self):
        for content in ("第1集：已经有集名\n镜头1：已经有镜头", '{"entities": [], "episodes": []}',
                        "<<<SCRIPT_BEGIN>>>\n第1集\n<<<SCRIPT_END>>>"):
            with transaction.atomic():
                self.adopt_text(content)
                result = self.check().data
                self.assertEqual(result["status"], "unstructured")
                self.assertEqual(result["reason_code"], "script_structure_required")
                self.assertTrue(result["reason"])
                self.assertIsNone(result["episode_count"])
                self.assertIsNone(result["shot_count"])
                self.assertFalse(ScriptRevisionStructure.objects.exists())
                self.assertFalse(GenerationAttempt.objects.exists())
                transaction.set_rollback(True)

    def test_corrupt_raw_body_is_error_not_unstructured(self):
        revision = self.adopt_text("未经结构化的原文")
        ScriptRevision.objects.filter(pk=revision.pk).update(content="tampered body")
        response = self.check(400)
        self.assertIn("摘要", str(response.data))
        self.assertNotEqual(response.data.get("status"), "unstructured")

    def test_corrupt_or_missing_saved_detail_is_error_not_unstructured(self):
        revision, _, _, _ = self.imported()
        self.assertEqual(self.adopt(revision)[0].status_code, 201)
        ScriptRevisionStructure.objects.filter(revision_id=revision["id"]).update(document_sha256="0" * 64)
        self.assertNotEqual(self.check(400).data.get("status"), "unstructured")
        ScriptRevisionStructure.objects.filter(revision_id=revision["id"]).delete()
        self.assertNotEqual(self.check(400).data.get("status"), "unstructured")

    def test_empty_declarations_are_counted_without_a_new_quality_gate(self):
        output, raw = self.make_output()
        for selection, episodes in enumerate(([], [{**raw["episodes"][0], "shots": []}])):
            content = json.dumps({**raw, "episodes": episodes}, ensure_ascii=False)
            _, revision, _ = create_structured_script_source(project=self.project, user=self.actor,
                content=content, output=output_reference(output), source_type="paste", max_bytes=inputs.LIMIT)
            revision = confirm_script_revision(revision, user=self.actor)
            response, _ = self.adopt({"id": str(revision.pk), "content_sha256": revision.content_sha256}, selection=selection)
            self.assertEqual(response.status_code, 201, response.data)
            result = self.check().data
            self.assertEqual(result["status"], "structured")
            self.assertEqual((result["episode_count"], result["shot_count"]), (len(episodes), 0))
        self.assertFalse(self.project.episodes.exists())

    def test_closed_run_can_read_but_foreign_user_and_source_override_cannot(self):
        revision, _, _, _ = self.imported()
        self.assertEqual(self.adopt(revision)[0].status_code, 201)
        self.run.status = "cancelled"
        self.run.save(update_fields=("status",))
        self.assertEqual(self.check().data["status"], "structured")
        self.check(400, {"revision_id": "different"})
        outsider = get_user_model().objects.create_user(username="structure-outsider")
        self.client.force_authenticate(outsider)
        self.check(404)
        self.client.force_authenticate(None)
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-script-structure/")
        self.assertIn(response.status_code, (401, 403))

    def test_polling_options_does_not_fetch_script_body_or_structure(self):
        revision, _, _, _ = self.imported()
        self.assertEqual(self.adopt(revision)[0].status_code, 201)
        with patch("studio.services.production_manual_scripts.read_manual_script_structure",
                   side_effect=AssertionError("on demand only")), CaptureQueriesContext(connection) as queries:
            response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-action-options/")
        self.assertEqual(response.status_code, 200, response.data)
        for query in queries:
            self.assertNotIn('"studio_scriptrevision"."content"', query["sql"])
            self.assertNotIn('studio_scriptrevisionstructure', query["sql"])
