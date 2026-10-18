"""M2 structured detail on real revisions, isolated SQLite and no model calls."""
from copy import deepcopy
from hashlib import sha256
import json
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection

from studio.models import BusinessOutputVersion, ScriptRevision, ScriptRevisionStructure, ScriptSource, SeriesProject
from studio.services.business_outputs import output_reference, verified_output_contract, write_output
from studio.services.json_contracts import assign_json_contract
from studio.services.script_revision_structures import (
    create_structured_script_source, read_script_revision_structure, save_script_revision_structure,
)
from studio.services.script_semantics import SEMANTICS_KEY
from studio.services.script_semantic_relations import VALIDATION_SCOPE
from studio.tests.output_structure_fixtures import response_structure
from studio.tests.script_semantics_fixtures import script_example
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


LIMIT = 1_000_000


class ScriptRevisionStructureTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="structure-owner", is_staff=True)
        self.project = SeriesProject.objects.create(owner=self.actor, title="Isolated script structure", **PROJECT_CONFIG)
        self.output, self.raw = self.make_output()
        self.reference = output_reference(self.output)
        self.revision = self.make_revision(self.raw)

    def make_output(self, layout="a", *, publish=True, parser=SEMANTICS_KEY):
        raw, _, mapping = script_example(layout)
        structure = response_structure(example=raw)
        row = write_output(actor=self.actor, data={"name": "Isolated script output", "parser_key": parser,
            "data_structure_version": str(structure.pk),
            **({"semantic_mapping": mapping} if parser == SEMANTICS_KEY else {})})
        if publish:
            row = write_output(actor=self.actor, output_id=row.pk, expected_revision=row.revision, action="publish")
        return row, raw

    def make_revision(self, raw, *, text=None, source=None):
        # Explicit persisted-source fixture: exact whitespace/CRLF are facts,
        # not the old paste normalizer or a claimed HTTP import/worker run.
        text = text if text is not None else " \r\n" + json.dumps(raw, ensure_ascii=False, indent=2) + "\r\n "
        encoded = text.encode("utf-8")
        source = source or ScriptSource.objects.create(project=self.project, owner=self.actor,
            source_type="paste", source_format="plain", status="preview_ready")
        return ScriptRevision.objects.create(source=source, revision=source.revisions.count() + 1,
            content=text, content_sha256=sha256(encoded).hexdigest(), content_size_bytes=len(encoded), created_by=self.actor)

    def save_detail(self, **changes):
        return save_script_revision_structure(**{"revision_id": self.revision.pk, "project_id": self.project.pk,
            "output": self.reference, "max_bytes": LIMIT, **changes})

    def read_detail(self, **changes):
        return read_script_revision_structure(**{"revision_id": self.revision.pk,
            "project_id": self.project.pk, "max_bytes": LIMIT, **changes})

    def create_source(self, **changes):
        return create_structured_script_source(**{"project": self.project, "user": self.actor,
            "content": self.revision.content, "output": self.reference, "source_type": "paste",
            "max_bytes": LIMIT, **changes})

    def test_original_source_writer_and_detail_commit_exact_raw_in_one_transaction(self):
        for layout in ("a", "b"):
            output, raw = self.make_output(layout)
            content = " \r\n" + json.dumps(raw, ensure_ascii=False, indent=2).replace("\n", "\r\n") + "\r\n\t "
            source, revision, detail = self.create_source(content=content, output=output_reference(output))
            encoded = content.encode("utf-8")
            self.assertEqual(revision.content, content)
            self.assertEqual(source.file_sha256, sha256(encoded).hexdigest())
            self.assertEqual(revision.content_sha256, source.file_sha256)
            self.assertEqual(revision.content_size_bytes, len(encoded))
            self.assertEqual(detail["original_text"], content)
            self.assertEqual(detail["document"]["projection"]["source_value"], raw)
            self.assertEqual(detail["document"]["revision"]["id"], str(revision.pk))
            self.assertEqual(revision.status, "preview")
            self.assertIsNone(revision.origin_generation_attempt_id)
            self.assertIsNone(revision.confirmed_by_id)

    def test_source_transaction_rolls_back_raw_detail_and_enclosing_candidate_failure(self):
        counts = (ScriptSource.objects.count(), ScriptRevision.objects.count(), ScriptRevisionStructure.objects.count())
        def unchanged():
            self.assertEqual((ScriptSource.objects.count(), ScriptRevision.objects.count(),
                ScriptRevisionStructure.objects.count()), counts)
        bad = deepcopy(self.raw)
        bad["episodes"][0]["shots"][0]["references"][0]["entity_key"] = "unknown"
        for content in ("invalid json", json.dumps(bad)):
            with self.assertRaises(ValidationError):
                self.create_source(content=content)
            unchanged()
        with patch.object(ScriptRevisionStructure, "save", side_effect=IntegrityError("isolated write failure")):
            with self.assertRaises(IntegrityError):
                self.create_source()
        unchanged()
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.create_source()
            raise RuntimeError("original candidate writer failed after revision")
        unchanged()

    def test_source_writer_rejects_wrong_output_and_size_without_leftover_source(self):
        draft, _ = self.make_output(publish=False)
        contract = verified_output_contract(draft)
        draft_reference = {**contract.value, "sha256": contract.sha256}
        counts = (ScriptSource.objects.count(), ScriptRevision.objects.count())
        for changes in ({"output": draft_reference}, {"max_bytes": 100}, {"source_type": "upload"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.create_source(**changes)
            self.assertEqual((ScriptSource.objects.count(), ScriptRevision.objects.count()), counts)

    def test_actual_response_source_kind_does_not_invent_a_call_or_adoption(self):
        source, revision, _ = self.create_source(source_type="ai_generated")
        self.assertEqual(source.source_type, "ai_generated")
        self.assertIsNone(revision.origin_generation_attempt_id)
        self.assertIsNone(revision.confirmed_at)
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, "")

    def test_two_layouts_persist_original_and_all_custom_fields_without_adoption(self):
        results = []
        for layout in ("a", "b"):
            output, raw = self.make_output(layout)
            revision = self.make_revision(raw)
            detail = self.save_detail(revision_id=revision.pk, output=output_reference(output))
            self.assertEqual(detail["original_text"], revision.content)
            projection = detail["document"]["projection"]
            self.assertEqual(projection["source_value"], raw)
            self.assertEqual(projection["validation_scope"], VALIDATION_SCOPE)
            revision.refresh_from_db()
            self.assertEqual(revision.status, "preview")
            self.assertIsNone(revision.confirmed_by_id)
            self.assertIsNone(revision.confirmed_at)
            self.assertIsNone(revision.origin_generation_attempt_id)
            self.project.refresh_from_db()
            self.assertEqual(self.project.master_script, "")
            results.append(projection["value"])
        self.assertEqual(*results)

    def test_read_and_replay_use_saved_values_without_parser_or_current_registry(self):
        from studio.services.script_revision_structures import script_structure_max_bytes
        from studio.services.security_boundaries import security_boundary_resolution_cache

        before = self.save_detail()
        with security_boundary_resolution_cache(), \
             patch("studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError("reparse")), \
             patch("studio.services.script_revision_structures.output_reference", side_effect=AssertionError("current registry")):
            # Resolve the active content boundary once; the detail itself still
            # uses one query and never reloads today's output registry.
            script_structure_max_bytes()
            with self.assertNumQueries(1):
                self.assertEqual(self.read_detail(), before)
            self.assertEqual(self.save_detail(), before)
        self.assertEqual(ScriptRevisionStructure.objects.count(), 1)

    def test_invalid_relation_never_creates_structured_detail_or_adoption(self):
        raw = deepcopy(self.raw)
        raw["episodes"][0]["shots"][0]["references"][0]["variant_key"] = "not-declared"
        revision = self.make_revision(raw)
        with self.assertRaises(ValidationError) as caught:
            self.save_detail(revision_id=revision.pk)
        self.assertIn("value.episodes[0].shots[0].references[0].variant_key", caught.exception.message_dict)
        self.assertFalse(ScriptRevisionStructure.objects.filter(revision=revision).exists())
        revision.refresh_from_db()
        self.assertEqual(json.loads(revision.content), raw)
        self.assertEqual(revision.status, "preview")
        self.assertIsNone(revision.confirmed_by_id)

    def test_retirement_and_successor_do_not_replace_original_output(self):
        before = self.save_detail()
        successor = write_output(actor=self.actor, output_id=self.output.pk,
            expected_revision=self.output.revision, action="successor")
        successor = write_output(actor=self.actor, output_id=successor.pk,
            expected_revision=successor.revision, action="publish")
        write_output(actor=self.actor, output_id=self.output.pk, expected_revision=self.output.revision, action="retire")
        self.assertEqual(self.read_detail(), before)
        later = self.make_revision(self.raw)
        self.assertEqual(self.save_detail(revision_id=later.pk)["document"]["output"], self.reference)
        with self.assertRaises(ValidationError):
            self.save_detail(output=output_reference(successor))
        self.assertEqual(self.read_detail(), before)

    def test_only_one_detail_per_revision_and_output_is_a_real_foreign_key(self):
        self.save_detail()
        row = ScriptRevisionStructure.objects.get(pk=self.revision.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScriptRevisionStructure.objects.create(revision=self.revision, output_version=self.output,
                document=row.document, document_schema_version=row.document_schema_version,
                document_size_bytes=row.document_size_bytes, document_sha256=row.document_sha256)
        with self.assertRaises(ProtectedError):
            self.output.delete()

    def test_delete_revision_cascades_only_its_detail_not_output_or_sibling(self):
        self.save_detail()
        other = self.make_revision(self.raw, source=self.revision.source)
        self.save_detail(revision_id=other.pk)
        self.revision.delete()
        self.assertEqual(list(ScriptRevisionStructure.objects.values_list("revision_id", flat=True)), [other.pk])
        self.assertTrue(BusinessOutputVersion.objects.filter(pk=self.output.pk).exists())

    def test_invalid_json_and_type_keep_original_but_create_no_detail(self):
        invalid = deepcopy(self.raw)
        invalid["episodes"][0]["shots"][0]["duration_ms"] = "wrong"
        for text in ('{"episodes":}', json.dumps(invalid), '```json\n{}\n```'):
            revision = self.make_revision(None, text=text)
            with self.assertRaises(ValidationError):
                self.save_detail(revision_id=revision.pk)
            revision.refresh_from_db()
            self.assertEqual(revision.content, text)
            self.assertFalse(ScriptRevisionStructure.objects.filter(pk=revision.pk).exists())

    def test_wrong_project_and_missing_detail_never_reconstruct(self):
        with self.assertRaises(ValidationError):
            self.read_detail()
        self.save_detail()
        for action in (self.save_detail, self.read_detail):
            with self.assertRaises(ValidationError):
                action(project_id=uuid4())
        self.assertEqual(ScriptRevisionStructure.objects.count(), 1)

    def test_content_or_revision_evidence_drift_is_not_repaired(self):
        before = self.save_detail()
        ScriptRevision.objects.filter(pk=self.revision.pk).update(content="{}")
        for action in (self.read_detail, self.save_detail):
            with self.assertRaises(ValidationError):
                action()
        self.assertEqual(ScriptRevisionStructure.objects.get(pk=self.revision.pk).document,
            before["document"])
        # Even a new internally consistent text digest cannot impersonate the
        # original revision content that produced this saved projection.
        ScriptRevision.objects.filter(pk=self.revision.pk).update(content_sha256=sha256(b"{}").hexdigest(), content_size_bytes=2)
        with self.assertRaises(ValidationError):
            self.read_detail()

    def test_tampered_detail_or_wrong_output_link_is_rejected(self):
        self.save_detail()
        row = ScriptRevisionStructure.objects.get(pk=self.revision.pk)
        original = deepcopy(row.document)
        row.document["projection"]["value"]["episodes"] = []
        row.save(update_fields=("document",))
        with self.assertRaises(ValidationError):
            self.read_detail()
        assign_json_contract(row, "document", original, max_bytes=LIMIT)
        different, _ = self.make_output()
        row.output_version = different
        row.save()
        with self.assertRaises(ValidationError):
            self.read_detail()

    def test_wrong_protocol_draft_and_unknown_output_never_write(self):
        generic, _ = self.make_output(parser="structured_json.v1")
        draft, _ = self.make_output(publish=False)
        draft_contract = verified_output_contract(draft)
        draft_reference = {**draft_contract.value, "sha256": draft_contract.sha256}
        from studio.tests.test_script_semantic_requests import reference
        _, source, mapping = script_example()
        for output in (output_reference(generic), draft_reference, reference(source, mapping)):
            with self.subTest(output=output["id"]), self.assertRaises(ValidationError):
                self.save_detail(output=output)
        self.assertFalse(ScriptRevisionStructure.objects.exists())

    def test_failed_store_or_enclosing_transaction_leaves_no_success_detail(self):
        with patch.object(ScriptRevisionStructure, "save", side_effect=IntegrityError("isolated storage failure")):
            with self.assertRaises(IntegrityError):
                self.save_detail()
        self.assertFalse(ScriptRevisionStructure.objects.exists())
        with patch("studio.services.script_revision_structures._read_detail", side_effect=ValidationError("isolated readback failure")):
            with self.assertRaises(ValidationError):
                self.save_detail()
        self.assertFalse(ScriptRevisionStructure.objects.exists())
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.save_detail()
            raise RuntimeError("caller failed after structured detail")
        self.assertFalse(ScriptRevisionStructure.objects.exists())

    def test_confirmed_revision_is_not_backfilled_but_existing_detail_is_readable(self):
        ScriptRevision.objects.filter(pk=self.revision.pk).update(status="confirmed")
        with self.assertRaises(ValidationError):
            self.save_detail()
        ScriptRevision.objects.filter(pk=self.revision.pk).update(status="preview")
        before = self.save_detail()
        ScriptRevision.objects.filter(pk=self.revision.pk).update(status="confirmed")
        self.assertEqual(self.save_detail(), before)

    def test_list_queries_do_not_load_detail_and_detail_lookup_uses_unique_index(self):
        self.save_detail()
        with CaptureQueriesContext(connection) as queries:
            list(ScriptRevision.objects.filter(source=self.revision.source).values("id", "revision", "status"))
        self.assertNotIn("studio_scriptrevisionstructure", queries.captured_queries[0]["sql"])
        self.assertIn("INDEX", ScriptRevisionStructure.objects.filter(revision_id=self.revision.pk).explain().upper())

    def test_storage_limit_fails_explicitly_without_truncating_original(self):
        with self.assertRaises(ValidationError):
            self.save_detail(max_bytes=100)
        self.assertFalse(ScriptRevisionStructure.objects.exists())
        self.revision.refresh_from_db()
        self.assertIn("mood_note", self.revision.content)

    def test_twenty_four_shot_content_uses_text_boundary_not_configuration_budget(self):
        from studio.services.script_revision_structures import script_structure_max_bytes
        from studio.services.script_storyboard_fields import storyboard_source_fields
        raw = deepcopy(self.raw)
        raw["episodes"] = [raw["episodes"][0]]
        shot = raw["episodes"][0]["shots"][0]
        raw["episodes"][0]["shots"] = [
            {**deepcopy(shot), "shot": index, "action": "雨夜车厢中人物缓慢转身。" * 20}
            for index in range(1, 25)]
        revision = self.make_revision(raw, text=json.dumps(raw, ensure_ascii=False))
        detail = self.save_detail(revision_id=revision.pk, max_bytes=65536)
        row = ScriptRevisionStructure.objects.get(pk=revision.pk)
        self.assertGreater(row.document_size_bytes, 65536)
        self.assertLess(row.document_size_bytes, script_structure_max_bytes())
        with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                side_effect=AssertionError("saved detail must not reparse")):
            self.assertEqual(self.read_detail(revision_id=revision.pk, max_bytes=65536), detail)
        self.assertEqual(detail["document"]["projection"]["source_value"], raw)
        fields = storyboard_source_fields({"structure": detail}, max_bytes=script_structure_max_bytes())
        self.assertEqual(set(fields), {(1, index) for index in range(1, 25)})
        self.assertEqual(detail["original_text"], revision.content)

    def test_text_boundary_still_rejects_write_and_read_without_truncation(self):
        with patch("studio.services.script_revision_structures.script_structure_max_bytes", return_value=100):
            with self.assertRaises(ValidationError):
                self.create_source(max_bytes=65536)
        self.assertFalse(ScriptRevisionStructure.objects.exists())
        self.save_detail()
        before = deepcopy(ScriptRevisionStructure.objects.get(pk=self.revision.pk).document)
        with patch("studio.services.script_revision_structures.script_structure_max_bytes", return_value=100):
            with self.assertRaises(ValidationError):
                self.read_detail(max_bytes=65536)
        self.assertEqual(ScriptRevisionStructure.objects.get(pk=self.revision.pk).document, before)

    def test_saved_projection_shape_and_semantic_identity_are_verified_not_recomputed(self):
        self.save_detail()
        row = ScriptRevisionStructure.objects.get(pk=self.revision.pk)
        original = deepcopy(row.document)
        for mutation in (lambda p: p.update(configuration_sha256="0" * 64),
                         lambda p: p.update(validation_scope="structure_and_mapping"),
                         lambda p: p["value"].update(episodes="wrong"),
                         lambda p: p["source_value"].update(episodes="wrong")):
            changed = deepcopy(original)
            mutation(changed["projection"])
            assign_json_contract(row, "document", changed, max_bytes=LIMIT)
            row.save()
            with self.assertRaises(ValidationError):
                self.read_detail()
