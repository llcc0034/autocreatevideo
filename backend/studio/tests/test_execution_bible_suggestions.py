"""Actual adopted script -> reviewed Bible -> editable input, without calls."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, BibleVariant, CharacterBible, SceneBible, PropBible, ExecutionPreparation, GenerationAttempt
from studio.services.execution_input_catalog import execution_context_field_options, execution_input_form_options
from studio.services.execution_input_suggestions import preview_execution_input_suggestion
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_bible_suggestions import read_bible_input_reference
from studio.services.json_contracts import json_contract
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.production_execution_units import resolve_production_unit
from studio.services.production_reviewed_content import read_reviewed_bible, read_reviewed_entity_primary, reviewed_entity_row
from studio.tests import test_production_manual_bible as bible_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionBibleSuggestionTests(TestCase):
    setUpTestData = classmethod(bible_fixtures.ProductionManualBibleTests.setUpTestData.__func__)
    create = bible_fixtures.ProductionManualBibleTests.create
    post = bible_fixtures.ProductionManualBibleTests.post
    read = bible_fixtures.ProductionManualBibleTests.read
    adopt_source = bible_fixtures.ProductionManualBibleTests.adopt_source
    approval = bible_fixtures.ProductionManualBibleTests.approval

    def setUp(self):
        bible_fixtures.ProductionManualBibleTests.setUp(self)
        self.adopt_source()
        response = self.post(self.approval())
        self.assertEqual(response.status_code, 201, response.data)
        self.stage = self.run.stages.get(stage_key="bible_review")
        self.scene = SceneBible.objects.get(project=self.project)
        self.scope = {"kind": "scene", "id": str(self.scene.pk)}
        self.definition = json_contract({"schema_version": 1, "configuration": {"inputs": [{"key": "renamed_subject",
            "kind": "text", "required": True, "min_items": 1, "max_items": 1,
            "targets": [{"target_kind": "profile_input", "target_key": "request.subject", "is_primary": True}],
            "semantic_contract": {"schema_version": 1, "allowed_scope_kinds": ["request"],
                "allowed_purposes": [], "allowed_subject_kinds": [], "paired_with_input_key": None,
                "allow_repeated_asset": False}}]}, "resources": {"conditioning_mode": None},
            "interface": {"input_targets": {}, "profile_inputs": {"request.subject": {"source_kinds": ["literal"]}}}})
        self.contract = {"sha256": self.definition.sha256, "definition": self.definition.value,
            "input_forms": execution_input_form_options(self.definition.value)}

    def preview(self, **kwargs):
        return preview_execution_input_suggestion(self.contract, **{"owner": self.actor,
            "project_id": self.project.pk, "input_key": "renamed_subject",
            "suggestion_key": "production.entity.description", "production_run_id": self.run.pk,
            "production_scope": self.scope, **kwargs})

    def resolve(self, preview, value=None, **kwargs):
        return resolve_execution_input_sources(self.definition, {"renamed_subject": {"kind": "literal",
            "value": preview["content"] if value is None else value, "reference": preview["reference"]}},
            **{"owner": self.actor, "project_id": self.project.pk, "subject_facts": [], "max_media_bytes": 65536,
                "production_run_id": str(self.run.pk), "production_scope": self.scope,
                "eligible_stage_ids": [self.stage.pk], **kwargs}).value

    def test_description_and_exact_primary_fields_are_bounded_editable_input_copies(self):
        before = ApprovalDecision.objects.count()
        choices = self.contract["input_forms"]["renamed_subject"]["suggestions"]
        self.assertEqual([row["key"] for row in choices if row["prefill"]], ["production.entity.description"])
        self.assertNotIn("production.entity.visual_identity", [row["key"] for row in choices])
        for kind, model in (("scene", SceneBible), ("character", CharacterBible), ("prop", PropBible)):
            entity = model.objects.get(project=self.project)
            scope = {"kind": kind, "id": str(entity.pk)}
            primary = BibleVariant.objects.get(**{kind: entity}, appearance_role="primary").revisions.get(revision=1)
            expected = {"description": entity.description, "primary.identity_anchor": primary.identity_anchor,
                "primary.appearance_anchor": primary.appearance_anchor}
            for field, original in expected.items():
                with self.subTest(kind=kind, field=field):
                    preview = self.preview(production_scope=scope, suggestion_key=f"production.entity.{field}")
                    self.assertEqual(preview["content"], original)
                    if field.startswith("primary."):
                        self.assertEqual(preview["origin"]["setting"]["setting_revision_id"], str(primary.pk))
                        self.assertEqual(preview["origin"]["setting"]["content_sha256"], primary.content_sha256)
                    self.assertEqual(preview["origin"]["script_revision_id"], str(self.revision.pk))
                    result = self.resolve(preview, preview["content"] + " 本次人工补充", production_scope=scope)
                    self.assertEqual(result["logical_values"]["renamed_subject"], preview["content"] + " 本次人工补充")
                    ref = result["input_provenance"][0]["reference"]
                    self.assertTrue(ref["edited"])
                    self.assertEqual(ref["content"], original)
                    self.assertEqual(ref["review"]["revision"], 1)
                    self.assertNotIn("镜头1", preview["content"])
                    primary.refresh_from_db(); entity.refresh_from_db()
                    self.assertEqual(expected[field], entity.description if field == "description"
                        else getattr(primary, field.removeprefix("primary.")))
        self.assertEqual(ApprovalDecision.objects.count(), before)
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=self.run).exists())

    def primary(self):
        return BibleVariant.objects.get(scene=self.scene, appearance_role="primary")

    def reapprove(self):
        response = self.post({**self.approval(), "idempotency_key": str(uuid4())})
        self.assertEqual(response.status_code, 201, response.data)

    def context_inputs(self, fields=("identity_anchor", "appearance_anchor")):
        definition = deepcopy(self.definition.value)
        template = definition["configuration"]["inputs"][0]
        definition["configuration"]["inputs"] = [{**template, "key": field, "targets": []} for field in fields]
        decision_id = self.read()["approval"]["decision_id"]
        inputs = {field: {"kind": "context_field", "selector_key": f"production.entity.primary.{field}",
            "scope": {"kind": "request"}, "content_revision_id": decision_id} for field in fields}
        return json_contract(definition), inputs

    def resolve_context(self, definition, inputs):
        return resolve_execution_input_sources(definition, inputs, owner=self.actor, project_id=self.project.pk,
            subject_facts=[], max_media_bytes=65536, production_run_id=str(self.run.pk),
            production_scope=self.scope, eligible_stage_ids=[self.stage.pk]).value

    def test_context_and_editable_copy_share_primary_revision_not_old_entity_text(self):
        original = self.primary().revisions.get(revision=1)
        revise_variant(actor=self.actor, variant=original.variant, expected_revision=1,
            value={**setting_payload(original), "identity_anchor": "新的共同身份", "appearance_anchor": "新的基础外观"})
        self.reapprove()
        definition, inputs = self.context_inputs()
        with patch("studio.services.production_reviewed_content.read_reviewed_entity_primary",
                wraps=read_reviewed_entity_primary) as reader:
            resolved = self.resolve_context(definition, inputs)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(resolved["logical_values"], {"identity_anchor": "新的共同身份", "appearance_anchor": "新的基础外观"})
        for proof in resolved["input_provenance"]:
            self.assertEqual(proof["setting"]["revision"], 2)
            self.assertEqual(proof["setting"]["appearance_id"], str(original.variant_id))
            preview = self.preview(suggestion_key=proof["selector_key"])
            self.assertEqual(preview["content"], resolved["logical_values"][proof["input_key"]])
            self.assertEqual(preview["origin"]["setting"], proof["setting"])
        self.assertNotIn("OLD ENTITY SENTINEL", str(resolved))
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run__production_run=self.run).exists())

    def test_primary_reader_is_exact_bounded_and_refuses_missing_wrong_or_changed_references(self):
        reviewed = read_reviewed_bible(run=self.run, eligible_stage_ids=[self.stage.pk])
        unit = resolve_production_unit(self.run, self.scope)
        row = reviewed_entity_row(reviewed, unit)
        with CaptureQueriesContext(connection) as queries:
            values, ref = read_reviewed_entity_primary(row, unit)
        self.assertEqual(len(queries), 1)
        self.assertNotIn('"visual_identity"', queries[0]["sql"])
        self.assertNotIn('"studio_scriptrevision"."content"', queries[0]["sql"])
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + queries[0]["sql"])
                self.assertTrue(any("SEARCH studio_biblevariantrevision USING INDEX" in str(item)
                    for item in cursor.fetchall()))
        bad_rows = [dict(row, appearance_settings=[]), dict(row, appearance_settings=[ref, ref]),
            dict(row, appearance_settings=[{**ref, "setting_revision_id": str(uuid4())}]),
            dict(row, appearance_settings=[{**ref, "content_sha256": "f" * 64}])]
        other_scope = {"kind": "prop", "id": str(PropBible.objects.get(project=self.project).pk)}
        other_row = reviewed_entity_row(reviewed, resolve_production_unit(self.run, other_scope))
        bad_rows.append(dict(row, appearance_settings=other_row["appearance_settings"]))
        for bad in bad_rows:
            with self.subTest(row=bad), self.assertRaises(ValidationError):
                read_reviewed_entity_primary(bad, unit)
        with self.assertNumQueries(0):
            self.assertEqual(read_reviewed_entity_primary(dict(row, appearance_settings=[]), unit,
                allow_absent=True), (None, None))
        for bad in bad_rows[1:] + [dict(row, appearance_settings=None), dict(row, appearance_settings=[{}])]:
            with self.subTest(optional_bad=bad), self.assertRaises(ValidationError):
                read_reviewed_entity_primary(bad, unit, allow_absent=True)
        primary = self.primary()
        original = primary.revisions.get(revision=1)
        preview = self.preview(suggestion_key="production.entity.primary.identity_anchor")
        revise_variant(actor=self.actor, variant=primary, expected_revision=1,
            value={**setting_payload(original), "identity_anchor": "后续身份"})
        self.assertEqual(read_reviewed_entity_primary(row, unit), (values, ref))
        with self.assertRaises(ValidationError):
            self.resolve(preview)
        with self.assertRaises(ValidationError):
            self.preview(suggestion_key="production.entity.primary.identity_anchor")

    def test_whole_entity_json_and_individual_fields_share_exact_primary_without_old_text(self):
        definition, inputs = self.context_inputs()
        payload = deepcopy(definition.value)
        payload["configuration"]["inputs"].insert(0, {**payload["configuration"]["inputs"][0],
            "key": "whole", "kind": "json"})
        inputs = {"whole": {**inputs["identity_anchor"], "selector_key": "production.entity.content"}, **inputs}
        definition = json_contract(payload)
        counts = (ApprovalDecision.objects.count(), ExecutionPreparation.objects.count(), GenerationAttempt.objects.count())
        for kind, model in (("scene", SceneBible), ("character", CharacterBible), ("prop", PropBible)):
            with self.subTest(kind=kind):
                entity = model.objects.get(project=self.project)
                self.scope = {"kind": kind, "id": str(entity.pk)}
                setting = BibleVariant.objects.get(**{kind: entity}, appearance_role="primary").revisions.get(revision=1)
                with patch("studio.services.production_reviewed_content.read_reviewed_entity_primary",
                        wraps=read_reviewed_entity_primary) as reader:
                    resolved = self.resolve_context(definition, inputs)
                self.assertEqual(reader.call_count, 1)
                whole = resolved["logical_values"]["whole"]
                self.assertNotIn("visual_identity", whole)
                self.assertNotIn("reference_asset_ids", whole)
                self.assertNotIn("anchor_asset_id", whole)
                self.assertEqual(whole["primary_setting"], {"identity_anchor": setting.identity_anchor,
                    "appearance_anchor": setting.appearance_anchor})
                for proof in resolved["input_provenance"]:
                    self.assertEqual(proof["setting"]["setting_revision_id"], str(setting.pk))
        self.assertEqual(counts, (ApprovalDecision.objects.count(), ExecutionPreparation.objects.count(), GenerationAttempt.objects.count()))

    def test_whole_entity_keeps_blank_primary_and_does_not_refresh_frozen_value(self):
        original = self.primary().revisions.get(revision=1)
        definition, inputs = self.context_inputs(("identity_anchor",))
        payload = deepcopy(definition.value)
        payload["configuration"]["inputs"][0]["kind"] = "json"
        definition = json_contract(payload)
        inputs["identity_anchor"]["selector_key"] = "production.entity.content"
        frozen = self.resolve_context(definition, inputs)
        frozen_copy = deepcopy(frozen)
        revise_variant(actor=self.actor, variant=original.variant, expected_revision=1,
            value={**setting_payload(original), "identity_anchor": "", "appearance_anchor": ""})
        with self.assertRaises(ValidationError):
            self.resolve_context(definition, inputs)
        self.reapprove()
        inputs["identity_anchor"]["content_revision_id"] = self.read()["approval"]["decision_id"]
        current = self.resolve_context(definition, inputs)
        self.assertEqual(current["logical_values"]["identity_anchor"]["primary_setting"],
            {"identity_anchor": "", "appearance_anchor": ""})
        self.assertEqual(current["input_provenance"][0]["setting"]["revision"], 2)
        self.assertEqual(frozen, frozen_copy)
        self.assertEqual(frozen["input_provenance"][0]["setting"]["setting_revision_id"], str(original.pk))

    def test_optional_blank_is_preserved_and_old_selector_is_not_accepted(self):
        primary = self.primary(); original = primary.revisions.get(revision=1)
        revise_variant(actor=self.actor, variant=primary, expected_revision=1,
            value={**setting_payload(original), "identity_anchor": "", "appearance_anchor": ""})
        self.reapprove()
        self.assertTrue(SceneBible.objects.filter(pk=self.scene.pk).exists())
        definition, inputs = self.context_inputs()
        self.assertEqual(self.resolve_context(definition, inputs)["logical_values"],
            {"identity_anchor": "", "appearance_anchor": ""})
        with self.assertRaises(ValidationError):
            self.preview(suggestion_key="production.entity.primary.identity_anchor")
        self.assertNotIn("production.entity.visual_identity", [row["selector_key"] for row in execution_context_field_options()])
        bad = deepcopy(inputs); bad["identity_anchor"]["selector_key"] = "production.entity.visual_identity"
        with self.assertRaises(ValidationError):
            self.resolve_context(definition, bad)
        # A genuinely signed old source receipt must not become an unchecked literal reference.
        preview = self.preview()
        receipt = signing.loads(preview["reference"], salt="studio.execution-input-suggestion.v1")
        receipt["origin"]["selector"] = "production.entity.visual_identity"
        preview["reference"] = signing.dumps(receipt, salt="studio.execution-input-suggestion.v1")
        with self.assertRaises(ValidationError):
            self.resolve(preview)

    def test_preview_rejects_wrong_owner_run_entity_and_missing_or_changed_review(self):
        for kwargs in ({"owner": self.other}, {"production_run_id": uuid4()},
                {"production_scope": {"kind": "scene", "id": str(uuid4())}}, {"production_run_id": None},
                {"production_scope": None}, {"suggestion_key": "script.content"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.preview(**kwargs)
        self.scene.description = "Changed after review"
        self.scene.save(update_fields=("description",))
        with self.assertRaises(ValidationError):
            self.preview()

    def test_receipt_rejects_cross_scope_stale_review_and_tampering_without_replacing_edits(self):
        original_description = self.scene.description
        preview = self.preview()
        self.assertFalse(self.resolve(preview)["input_provenance"][0]["reference"]["edited"])
        for kwargs in ({"production_run_id": str(uuid4())}, {"production_scope": {"kind": "prop", "id": str(PropBible.objects.get().pk)}},
                {"eligible_stage_ids": []}, {"production_run_id": None}, {"owner": self.other}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.resolve(preview, "My edit", **kwargs)
        with self.assertRaises(ValidationError):
            self.resolve({**preview, "reference": preview["reference"] + "tamper"})
        self.scene.description = "Later source change"
        self.scene.save(update_fields=("description",))
        with self.assertRaises(ValidationError):
            self.resolve(preview, "My edit")
        self.assertEqual(preview["content"], original_description)

    def test_catalog_excludes_non_literal_subjects_and_query_requires_complete_context(self):
        from studio.execution_instance_serializers import ExecutionInputSuggestionQuerySerializer
        definition = deepcopy(self.definition.value)
        definition["interface"]["profile_inputs"]["request.subject"]["source_kinds"] = ["context_field"]
        self.assertEqual(execution_input_form_options(definition)["renamed_subject"]["suggestions"], [])
        base = {"input_key": "renamed_subject", "suggestion_key": "production.entity.description"}
        for params in ({"production_run_id": str(self.run.pk)}, {"entity_id": str(self.scene.pk)},
                {"project_id": str(self.project.pk), "production_run_id": str(self.run.pk), "entity_kind": "shot", "entity_id": str(self.scene.pk)}):
            self.assertFalse(ExecutionInputSuggestionQuerySerializer(data={**base, **params}).is_valid())
        self.assertTrue(ExecutionInputSuggestionQuerySerializer(data={**base, "project_id": str(self.project.pk),
            "production_run_id": str(self.run.pk), "entity_kind": "scene", "entity_id": str(self.scene.pk)}).is_valid())
