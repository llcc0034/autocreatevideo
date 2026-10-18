"""Isolated real-image associations; no live model, adoption or configuration writes."""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import Asset, CharacterBible, PropBible, SceneBible, SeriesProject
from studio.services.bible_variants import create_variant, revise_variant, setting_payload
from studio.services.execution_workflow_media import prepare_execution_workflow_media
from studio.services.execution_workflow_uses import read_execution_workflow_uses
from studio.services.json_contracts import json_contract
from studio.services.reference_context import compile_reference_context
from studio.services.reference_context_facts import ReferenceSubjectFacts
from studio.tests import test_execution_reference_materials as fixtures


class ExecutionSubjectAssociationTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionReferenceMaterialTests.setUpTestData.__func__)
    item = fixtures.ExecutionReferenceMaterialTests.item
    resolve = fixtures.ExecutionReferenceMaterialTests.resolve
    definition = fixtures.ExecutionReferenceMaterialTests.definition
    project = fixtures.ExecutionReferenceMaterialTests.project

    def setUp(self):
        fixtures.ExecutionReferenceMaterialTests.setUp(self)
        self.spec["semantic_contract"]["allowed_subject_kinds"] = ["character", "scene", "prop", "external"]
        self.series = SeriesProject.objects.create(owner=self.owner, title="Subject association fixture",
            language="English", aspect_ratio="9:16", width=720, height=1280, fps=24,
            default_shot_duration_ms=1000, continuity_seed=5)
        self.entities = {}
        for kind, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible)):
            self.entities[kind] = model.objects.create(project=self.series, slug=kind, name=f"Named {kind}",
                description=f"Declared {kind} description",
                **({"stable_speaker_id": "S1"} if kind == "character" else {}))
            self.entities[kind].reference_assets.add(self.asset)

    def subject_item(self, kind="character", **changes):
        return self.item(subject_ref={"kind": kind, "id": str(self.entities[kind].pk)}, **changes)

    def primary(self, kind):
        return create_variant(actor=self.owner, project_id=self.series.pk, entity_kind=kind,
            entity_id=self.entities[kind].pk, appearance_role="primary",
            primary_setting_revision_id=None, primary_setting_sha256="", value={
                "name": "Primary", "reference_name": f"{kind} primary",
                "identity_anchor": "CURRENT PRIMARY IDENTITY", "appearance_anchor": "CURRENT APPEARANCE",
                "basis_asset_id": None, "basis_asset_sha256": ""})

    def test_three_kinds_keep_real_association_and_render_only_selected_facts(self):
        for kind, entity in self.entities.items():
            with self.subTest(kind=kind), CaptureQueriesContext(connection) as queries:
                resolved = self.resolve([self.subject_item(kind)], project_id=self.series.pk)
            row = resolved["logical_values"]["references"][0]
            self.assertEqual(row["subject"], {"kind": kind, "id": str(entity.pk),
                "project_id": str(self.series.pk), "name": entity.name, "description": entity.description})
            sql = "\n".join(q["sql"] for q in queries)
            self.assertNotIn('"visual_identity"', sql)
            self.assertNotIn('"studio_biblevariant', sql)
            facts = self.project(resolved)
            subject = facts["subjects"][0]
            self.assertEqual(subject["description_parts"], [entity.name, entity.description])
            reference = facts["references"][0]
            self.assertEqual(reference["entity_id"], str(entity.pk))
            self.assertEqual(reference["category"], kind)
            self.assertEqual(reference["asset_id"], str(self.asset.pk))
            self.assertEqual(reference["input_label"], "<Picture 1>")
            self.assertEqual(reference["description"], self.asset.description)
            self.assertEqual(reference["prompt_hint"], self.asset.prompt_hint)
            context = replace(self.context, ordered_units=(replace(self.unit, unit_key="first",
                subject_keys=((kind, str(entity.pk)),)),), reference_bindings=({**reference, "unit_keys": ["first"]},),
                subject_facts=(ReferenceSubjectFacts(**{**subject, "description_parts": tuple(subject["description_parts"])}),))
            rendered = str(compile_reference_context(context, self.policy)["fields"])
            self.assertIn(entity.name, rendered)
            self.assertIn(entity.description, rendered)
            self.assertNotIn("OBSOLETE IDENTITY", rendered)

    def test_primary_creation_revision_and_old_identity_changes_do_not_change_selected_media_facts(self):
        for kind, entity in self.entities.items():
            with self.subTest(kind=kind):
                before = self.resolve([self.subject_item(kind)], project_id=self.series.pk)
                primary = self.primary(kind)
                self.assertEqual(self.resolve([self.subject_item(kind)], project_id=self.series.pk), before)
                revise_variant(actor=self.owner, variant=primary, expected_revision=1,
                    value={**setting_payload(primary.revisions.get(revision=1)),
                        "identity_anchor": "", "appearance_anchor": "CHANGED PRIMARY"})
                # The selected image is not an implicit selection of the current primary text.
                self.assertEqual(self.resolve([self.subject_item(kind)], project_id=self.series.pk), before)

    def test_repeated_use_reuses_request_local_association_not_body_queries_or_dicts(self):
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        with CaptureQueriesContext(connection) as queries:
            resolved = self.resolve([self.subject_item(item_key="first"), self.subject_item(item_key="second")],
                project_id=self.series.pk)
        subject_sql = [q["sql"] for q in queries if '"studio_characterbible"' in q["sql"]]
        relation_sql = [q["sql"] for q in queries if '"studio_characterbible_reference_assets"' in q["sql"]]
        self.assertEqual(len(subject_sql), 1, subject_sql)
        self.assertEqual(len(relation_sql), 1, relation_sql)
        first, second = resolved["logical_values"]["references"]
        self.assertEqual(first["subject"], second["subject"])
        self.assertIsNot(first["subject"], second["subject"])
        first["subject"]["description"] = "Mutated caller value"
        self.assertEqual(second["subject"]["description"], self.entities["character"].description)

    def test_cache_does_not_authorize_another_asset_or_a_disallowed_input(self):
        second = Asset(owner=self.owner, kind="reference", name="Unrelated image", checksum=self.asset.checksum)
        second.file.save("unrelated.png", ContentFile(self.bytes))
        items = [self.subject_item(item_key="first"), self.subject_item(item_key="second", asset_id=str(second.pk))]
        with self.assertRaisesMessage(ValidationError, "不是该主体已登记"):
            self.resolve(items, project_id=self.series.pk)
        self.entities["character"].reference_assets.add(second)
        self.assertEqual(len(self.resolve(items, project_id=self.series.pk)["logical_values"]["references"]), 2)
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        other_spec = deepcopy(self.spec)
        other_spec.update(key="context", targets=[])
        other_spec["semantic_contract"]["allowed_subject_kinds"] = ["scene"]
        with self.assertRaisesMessage(ValidationError, "主体类型不在"):
            self.resolve(definition=self.definition(specs=[self.spec, other_spec]), sources={
                "references": {"items": [self.subject_item()]},
                "context": {"items": [self.subject_item(item_key="different-input")]}}, project_id=self.series.pk)

    def test_new_resolution_observes_description_and_revoked_association_not_previous_request_cache(self):
        entity = self.entities["character"]
        original = self.resolve([self.subject_item()], project_id=self.series.pk)
        CharacterBible.objects.filter(pk=entity.pk).update(name="Changed name", description="")
        updated = self.resolve([self.subject_item()], project_id=self.series.pk)
        self.assertNotEqual(updated, original)
        self.assertEqual(updated["logical_values"]["references"][0]["subject"]["description"], "")
        entity.reference_assets.clear()
        with self.assertRaisesMessage(ValidationError, "不是该主体已登记"):
            self.resolve([self.subject_item()], project_id=self.series.pk)
        entity.anchor_asset = self.asset
        entity.save(update_fields=["anchor_asset"])
        self.assertEqual(self.resolve([self.subject_item()], project_id=self.series.pk), updated)

    def test_owner_and_project_boundaries_remain_required_for_each_kind(self):
        other_project = SeriesProject.objects.create(owner=self.owner, title="Unrelated project",
            language="English", aspect_ratio="9:16", width=720, height=1280, fps=24,
            default_shot_duration_ms=1000, continuity_seed=5)
        for kind, entity in self.entities.items():
            with self.subTest(kind=kind), self.assertRaisesMessage(ValidationError, "不属于当前授权项目"):
                self.resolve([self.subject_item(kind)], project_id=other_project.pk)
        self.series.owner = self.other
        self.series.save(update_fields=["owner"])
        for kind in self.entities:
            with self.subTest(kind=kind), self.assertRaisesMessage(ValidationError, "当前项目不属于业务所有者"):
                self.resolve([self.subject_item(kind)], project_id=self.series.pk)
            with self.subTest(kind=kind), self.assertRaisesMessage(ValidationError, "不属于当前授权项目"):
                # Request-scoped input reaches the subject ownership check;
                # an explicit foreign project is rejected earlier by the root.
                self.resolve([self.subject_item(kind)])

    def test_frozen_projection_does_not_reread_current_names_associations_or_bodies(self):
        resolved = self.resolve([self.subject_item()], project_id=self.series.pk)
        before = self.project(resolved)
        CharacterBible.objects.filter(pk=self.entities["character"].pk).update(name="Changed current name")
        self.entities["character"].reference_assets.clear()
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.project(resolved), before)
        # Existing configurable size limits may be queried; subject/media facts may not.
        sql = "\n".join(q["sql"] for q in queries)
        for table in ("characterbible", "scenebible", "propbible", "biblevariant", "asset", "seriesproject"):
            self.assertNotIn(f'"studio_{table}', sql)

    def test_rehashed_old_or_malformed_subjects_fail_in_both_frozen_consumers(self):
        resolved = self.resolve([self.subject_item()], project_id=self.series.pk)
        current = resolved["logical_values"]["references"][0]["subject"]
        invalid = [[], "guess", {}, {**current, "visual_identity": "old duplicated body"},
            {**current, "primary_setting": {"identity_anchor": "implicit current"}},
            {k: v for k, v in current.items() if k != "description"},
            {**current, "description": None}, {**current, "name": " "},
            {**current, "id": "not-a-uuid"}, {**current, "project_id": 123},
            {**current, "kind": []}, {**current, "kind": "unknown"}]
        for subject in invalid:
            value = deepcopy(resolved)
            row = value["logical_values"]["references"][0]
            row["subject"] = subject
            row["source_sha256"] = json_contract({k: v for k, v in row.items() if k != "source_sha256"}).sha256
            for consumer in (lambda: self.project(value),
                    lambda: prepare_execution_workflow_media(self.definition(), json_contract(value))):
                with self.subTest(subject=subject, consumer=consumer), self.assertRaises(ValidationError):
                    consumer()
        row.pop("subject")
        row["source_sha256"] = json_contract({k: v for k, v in row.items() if k != "source_sha256"}).sha256
        with self.assertRaisesMessage(ValidationError, "无主体须明确为null"):
            read_execution_workflow_uses(self.definition(), json_contract(value))

    def test_external_subject_exact_shape_and_input_allow_list_are_validated_after_freezing(self):
        subject_id = str(uuid4())
        resolved = self.resolve([self.item(subject_ref={"kind": "external", "id": subject_id})],
            subject_facts=[{"id": subject_id, "category": "scene", "name": "External scene", "description": ""}])
        self.assertEqual(self.project(resolved)["subjects"][0]["category"], "external")
        for extra in ({"category": []}, {"category": "external"}, {"project_id": str(self.series.pk)},
                {"visual_identity": "old"}):
            value = deepcopy(resolved)
            row = value["logical_values"]["references"][0]
            row["subject"].update(extra)
            row["source_sha256"] = json_contract({k: v for k, v in row.items() if k != "source_sha256"}).sha256
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                self.project(value)
        self.spec["semantic_contract"]["allowed_subject_kinds"] = ["character"]
        with self.assertRaisesMessage(ValidationError, "主体类型不在"):
            self.project(resolved)
