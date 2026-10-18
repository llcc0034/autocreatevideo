"""Exact M2 downstream source receipts, using isolated real revisions only."""
from copy import deepcopy
from hashlib import sha256
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import GenerationAttempt, ScriptRevision, ScriptRevisionStructure
from studio.services.business_outputs import output_reference, write_output
from studio.services.json_contracts import assign_json_contract
from studio.services.production_run_plans import confirmed_script_reference
from studio.services.script_revision_structures import read_confirmed_script_structure
from studio.services.script_sources import confirm_script_revision
from studio.tests import test_script_revision_structures as fixtures


LIMIT = fixtures.LIMIT


class ConfirmedScriptStructureTests(TestCase):
    setUp = fixtures.ScriptRevisionStructureTests.setUp
    make_output = fixtures.ScriptRevisionStructureTests.make_output
    make_revision = fixtures.ScriptRevisionStructureTests.make_revision
    create_source = fixtures.ScriptRevisionStructureTests.create_source
    save_detail = fixtures.ScriptRevisionStructureTests.save_detail

    def confirm(self, revision):
        revision = confirm_script_revision(revision, user=self.actor)
        return confirmed_script_reference(self.project.pk, {"kind": "script_revision",
            "id": str(revision.pk), "expected_sha256": revision.content_sha256})

    def read(self, reference, **changes):
        return read_confirmed_script_structure(**{"project_id": self.project.pk, "reference": reference,
            "max_bytes": LIMIT, **changes})

    def test_two_import_layouts_export_exact_saved_values_without_a_model_or_reparse(self):
        projections = []
        for layout in ("a", "b"):
            output, raw = self.make_output(layout)
            raw["custom"] = {"zero": 0, "flag": False, "empty": None, "list": ["原样", 0, False]}
            text = " \r\n" + json.dumps(raw, ensure_ascii=False, indent=2).replace("\n", "\r\n") + "\r\n\t "
            _, revision, detail = self.create_source(content=text, output=output_reference(output))
            reference = self.confirm(revision)
            with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                    side_effect=AssertionError("must not reparse")), \
                 patch("studio.services.script_revision_structures.output_reference",
                    side_effect=AssertionError("must not consult current registry")), self.assertNumQueries(2):
                result = self.read(reference)
            self.assertEqual(result["structure"], detail)
            self.assertEqual(result["structure"]["original_text"], text)
            self.assertEqual(result["structure"]["document"]["projection"]["source_value"], raw)
            source = result["source"]
            self.assertEqual(set(source), {"schema_version", "kind", "script", "document_sha256",
                "output", "configuration_sha256"})
            self.assertEqual(source["script"], reference)
            self.assertEqual(source["document_sha256"], detail["document_sha256"])
            self.assertEqual(source["output"], {key: detail["document"]["output"][key]
                for key in ("id", "version", "sha256")})
            projections.append(result["structure"]["document"]["projection"]["value"])
        self.assertEqual(*projections)
        self.assertEqual(GenerationAttempt.objects.count(), 0)
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, "")

    def test_preview_wrong_project_or_inexact_reference_is_never_a_confirmed_source(self):
        self.save_detail()
        with self.assertRaises(ValidationError):
            self.read({"id": str(self.revision.pk), "sha256": self.revision.content_sha256})
        reference = self.confirm(self.revision)
        for changes in ({"id": str(uuid4())}, {"source_id": str(uuid4())}, {"sha256": "0" * 64},
                {"revision": 2}, {"confirmed_at": "later"}, {"kind": "latest"}, {"extra": True}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.read({**reference, **changes})
        with self.assertRaises(ValidationError):
            self.read(reference, project_id=uuid4())
        for invalid in (None, [], "latest"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.read(invalid)

    def test_new_revision_current_master_and_output_successor_never_replace_frozen_source(self):
        self.save_detail()
        reference = self.confirm(self.revision)
        original = self.read(reference)
        successor = write_output(actor=self.actor, output_id=self.output.pk,
            expected_revision=self.output.revision, action="successor")
        write_output(actor=self.actor, output_id=successor.pk, expected_revision=successor.revision, action="publish")
        write_output(actor=self.actor, output_id=self.output.pk, expected_revision=self.output.revision, action="retire")
        raw = deepcopy(self.raw)
        raw["later"] = "new independent draft"
        later = self.make_revision(raw, source=self.revision.source)
        self.save_detail(revision_id=later.pk)
        self.project.master_script = "unrelated current manuscript"
        self.project.save(update_fields=("master_script",))
        self.assertEqual(self.read(reference, expected_source=original["source"]), original)
        later_reference = self.confirm(later)
        # The existing confirmation lifecycle supersedes the previous revision.
        # Surface that state explicitly; never replace an old downstream basis.
        with self.assertRaises(ValidationError):
            self.read(reference, expected_source=original["source"])
        with self.assertRaisesMessage(ValidationError, "已冻结来源不一致"):
            self.read(later_reference, expected_source=original["source"])
        self.assertNotEqual(self.read(later_reference)["source"], original["source"])

    def test_missing_detail_is_not_backfilled_for_confirmed_import(self):
        reference = self.confirm(self.revision)
        with patch("studio.services.script_revision_structures.save_script_revision_structure",
                side_effect=AssertionError("read must not write")), self.assertRaisesMessage(ValidationError, "尚无结构化内容"):
            self.read(reference)
        self.assertFalse(ScriptRevisionStructure.objects.exists())

    def test_changed_receipt_is_rejected_and_returned_data_is_not_mutable_storage(self):
        self.save_detail()
        reference = self.confirm(self.revision)
        original = self.read(reference)
        for changes in ({"document_sha256": "0" * 64}, {"configuration_sha256": "0" * 64},
                {"output": {**original["source"]["output"], "version": 99}}, {"schema_version": 2},
                {"kind": "latest"}, {"script": {**reference, "revision": 99}}):
            with self.subTest(changes=changes), self.assertRaisesMessage(ValidationError, "已冻结来源不一致"):
                self.read(reference, expected_source={**original["source"], **changes})
        returned = self.read(reference)
        returned["structure"]["document"]["projection"]["value"]["episodes"].clear()
        returned["source"]["script"]["revision"] = 99
        self.assertEqual(self.read(reference), original)

    def test_body_or_saved_detail_corruption_is_reported_without_repair(self):
        self.save_detail()
        reference = self.confirm(self.revision)
        original = self.read(reference)
        ScriptRevision.objects.filter(pk=self.revision.pk).update(content="{}")
        with self.assertRaises(ValidationError):
            self.read(reference, expected_source=original["source"])
        ScriptRevision.objects.filter(pk=self.revision.pk).update(content_sha256=sha256(b"{}").hexdigest(), content_size_bytes=2)
        replaced = {**reference, "sha256": sha256(b"{}").hexdigest(), "size_bytes": 2}
        with self.assertRaises(ValidationError):
            self.read(replaced)
        self.assertEqual(ScriptRevisionStructure.objects.get(revision=self.revision).document,
            original["structure"]["document"])

    def test_rehashed_projection_drift_cannot_match_original_frozen_receipt(self):
        self.save_detail()
        reference = self.confirm(self.revision)
        original = self.read(reference)
        row = ScriptRevisionStructure.objects.get(revision=self.revision)
        row.document["projection"]["value"]["episodes"][0]["shots"][0]["action"] = "unexpected replacement"
        row.save(update_fields=("document",))
        with self.assertRaises(ValidationError):
            self.read(reference)
        assign_json_contract(row, "document", row.document, max_bytes=LIMIT)
        row.save()
        with self.assertRaisesMessage(ValidationError, "已冻结来源不一致"):
            self.read(reference, expected_source=original["source"])
