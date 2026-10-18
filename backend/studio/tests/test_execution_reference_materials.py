"""Real image resolution and deterministic multi-media protocol fixtures.

Video/audio rows below are explicitly pre-resolved unit-test facts, not a live
Smoke result or proof that a caller may bypass the governed source resolver.
"""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.services.execution_inputs import allocate_workflow_input_slots
from studio.services.execution_reference_materials import project_execution_reference_materials
from studio.services.json_contracts import json_contract
from studio.services.reference_context import compile_reference_context
from studio.services.reference_context_facts import ReferenceSubjectFacts
from studio.tests import test_execution_input_resolution as source_fixtures
from studio.tests import test_reference_context_facts as context_fixtures


class ExecutionReferenceMaterialTests(TestCase):
    setUpTestData = classmethod(source_fixtures.ExecutionInputResolutionTests.setUpTestData.__func__)
    item = source_fixtures.ExecutionInputResolutionTests.item
    resolve = source_fixtures.ExecutionInputResolutionTests.resolve

    def setUp(self):
        source_fixtures.ExecutionInputResolutionTests.setUp(self)
        self.spec["targets"][0]["target_key"] = "images"
        self.target.update(target_key="images", wiring={"kind": "classified_group", "node_id": "conditioner",
            "input_names": ["ref_images.0", "ref_images.1"]})
        context_fixtures.ReferenceContextFactTests.setUp(self)

    def project(self, resolved=None, *, definition=None, scope_units=None, prompt_type="image"):
        return project_execution_reference_materials(definition or self.definition(),
            json_contract(resolved if resolved is not None else self.resolve()),
            prompt_type=prompt_type, unit_keys=["first", "second"], scope_units=scope_units if scope_units is not None else {}).value

    def definition(self, specs=None, targets=None):
        return source_fixtures.ExecutionInputResolutionTests.definition(self, specs=specs,
            targets=targets if targets is not None else {"images": self.target})

    def test_real_asset_keeps_explicit_port_label_checksum_and_no_file_path(self):
        resolved = self.resolve()
        actual = self.project(resolved)
        record = actual["references"][0]
        self.assertEqual(record["asset_id"], str(self.asset.pk))
        self.assertIsNone(record["artifact_id"])
        self.assertEqual(record["input_label"], "<Picture 1>")
        self.assertEqual(record["target_port_ref"], {"kind": "node_input", "node_id": "conditioner", "input_name": "ref_images.0"})
        self.assertEqual(record["unit_keys"], ["first", "second"])
        self.assertEqual(record["source_sha256"], resolved["logical_values"]["references"][0]["source_sha256"])
        self.assertNotIn(str(self.root), str(actual))
        self.assertEqual(actual, self.project(resolved))

    def test_exact_shot_uses_flow_through_mapping_and_real_resolver_to_call_ports(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from studio.models import ShotReferenceUse
        from studio.services.production_reference_mapping import reference_use_records, expand_reviewed_reference_sources
        from studio.services.shot_reference_sources import resolve_shot_reference_sources
        from studio.tests.test_production_reference_mapping import mapping

        project, shot, subject, use, _ = source_fixtures.ExecutionInputResolutionTests.reviewed_shot_use(self)
        ShotReferenceUse.objects.create(shot=shot, use_key="second-angle", position=1, media_kind="image",
            purpose="first_frame", setting_revision=use.setting_revision, source_mode="existing",
            asset=self.asset, asset_sha256=self.asset.checksum)
        # A different Bible anchor must not replace the exact per-use selection.
        original_anchor = subject.anchor_asset_id
        self.spec["semantic_contract"].update(allow_repeated_asset=True, allowed_purposes=["appearance", "composition"])
        with CaptureQueriesContext(connection) as queries:
            uses = resolve_shot_reference_sources(actor=self.owner, project_id=project.pk, shot_ids=[str(shot.pk)]).value["shots"][0]["uses"]
            records = reference_use_records(uses)
            before = deepcopy(records)
            outcomes = []
            for purpose in ("appearance", "composition"):
                mapped, excluded = expand_reviewed_reference_sources(specs=[self.spec],
                    sources={"references": mapping("images", purpose=purpose)}, records=records, shot_id=str(shot.pk))
                resolved = self.resolve(sources=mapped, project_id=project.pk)
                projected = self.project(resolved, scope_units={("shot", str(shot.pk)): ["second"]})
                rows = projected["references"]
                self.assertEqual(excluded, [])
                self.assertEqual([row["asset_id"] for row in rows], [str(self.asset.pk)] * 2)
                self.assertEqual([row["input_label"] for row in rows], ["<Picture 1>", "<Picture 2>"])
                self.assertEqual([row["purpose"] for row in rows], [purpose] * 2)
                self.assertEqual([row["category"] for row in rows], ["character"] * 2)
                self.assertEqual([row["unit_keys"] for row in rows], [["second"], ["second"]])
                self.assertNotEqual(rows[0]["item_key"], rows[1]["item_key"])
                self.assertEqual([row["target_port_ref"]["input_name"] for row in rows], ["ref_images.0", "ref_images.1"])
                outcomes.append(rows)
        self.assertEqual(records, before)
        self.assertEqual([row["purpose"] for row in records], ["A creative note", "first_frame"])
        self.assertNotEqual(outcomes[0][0]["source_sha256"], outcomes[1][0]["source_sha256"])
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
        self.assertFalse(any('studio_shot_reference_assets' in row["sql"] for row in queries))
        subject.refresh_from_db()
        self.assertEqual(subject.anchor_asset_id, original_anchor)

    def test_repeated_asset_is_multiple_uses_with_distinct_slots_and_labels(self):
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        resolved = self.resolve([self.item(item_key="second"), self.item(item_key="first")])
        records = self.project(resolved)["references"]
        self.assertEqual([row["item_key"] for row in records], ["second", "first"])
        self.assertEqual([row["input_label"] for row in records], ["<Picture 1>", "<Picture 2>"])
        self.assertEqual(len({row["asset_id"] for row in records}), 1)

    def test_port_or_source_tampering_is_rejected_even_with_a_new_envelope_hash(self):
        resolved = self.resolve()
        bad_port, bad_source = deepcopy(resolved), deepcopy(resolved)
        bad_port["workflow_slots"]["images"][0]["target_port_ref"]["input_name"] = "different"
        bad_source["logical_values"]["references"][0]["purpose"] = "unrecorded"
        for value in (bad_port, bad_source):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.project(value)

    def test_scope_requires_root_mapping_and_does_not_become_global(self):
        resolved = self.resolve()
        item = resolved["logical_values"]["references"][0]
        item["scope_ref"] = {"kind": "shot", "id": str(uuid4())}
        item["source_sha256"] = json_contract({key: value for key, value in item.items() if key != "source_sha256"}).sha256
        with self.assertRaisesMessage(ValidationError, "不能默认为全局"):
            self.project(resolved)
        scope = ("shot", item["scope_ref"]["id"])
        self.assertEqual(self.project(resolved, scope_units={scope: ["second"]})["references"][0]["unit_keys"], ["second"])
        for members in (["absent"], ["first", "first"], []):
            with self.subTest(members=members), self.assertRaises(ValidationError):
                self.project(resolved, scope_units={scope: members})

    def test_fixed_frame_role_does_not_change_the_external_subject_identity(self):
        subject_id = str(uuid4())
        self.spec["min_items"] = self.spec["max_items"] = 1
        self.target.update(min_items=1, max_items=1, reference_binding={"category": "first_frame", "input_slot": 1},
            wiring={"kind": "semantic", "semantic_key": "input.first_frame"})
        resolved = self.resolve([self.item(subject_ref={"kind": "external", "id": subject_id})],
            subject_facts=[{"id": subject_id, "category": "character", "name": "Courier", "description": "Blue jacket"}])
        facts = self.project(resolved)
        record = facts["references"][0]
        self.assertEqual(record["category"], "first_frame")
        self.assertEqual(record["entity_category"], "external")
        self.assertEqual(facts["subjects"][0]["category"], "external")
        subject = ReferenceSubjectFacts(**{**facts["subjects"][0], "description_parts": tuple(facts["subjects"][0]["description_parts"])})
        record["unit_keys"] = ["second"]
        context = replace(self.context, ordered_units=(replace(self.unit, unit_key="first", subject_keys=(("external", subject_id),)),
            replace(self.unit, unit_key="second", subject_keys=(("external", subject_id),))),
            reference_bindings=(record,), subject_facts=(subject,))
        result = compile_reference_context(context, self.policy)
        text = str(result["fields"])
        self.assertIn("Blue jacket", text)
        self.assertIn("(shots 2)", text)
        self.assertNotIn("(shots 1", text)

    def test_missing_fixed_role_does_not_guess_from_input_name(self):
        self.target["wiring"] = {"kind": "semantic_group", "semantic_keys": ["first_frame", "last_frame"]}
        with self.assertRaisesMessage(ValidationError, "不能按名称或数组位置猜测"):
            self.project()

    def test_external_identity_and_explicit_render_category_are_separate(self):
        subject_id = str(uuid4())
        for category in ("character", "scene", "prop"):
            with self.subTest(category=category):
                resolved = self.resolve([self.item(subject_ref={"kind": "external", "id": subject_id})],
                    subject_facts=[{"id": subject_id, "category": category, "name": "Test subject", "description": "Explicit source description"}])
                facts = self.project(resolved)
                record = facts["references"][0]
                self.assertEqual(record["category"], category)
                self.assertEqual(record["entity_category"], "external")
                self.assertEqual(record["entity_id"], subject_id)
                subject = ReferenceSubjectFacts(**{**facts["subjects"][0], "description_parts": tuple(facts["subjects"][0]["description_parts"])})
                context = replace(self.context, ordered_units=(replace(self.unit, unit_key="first", subject_keys=(("external", subject_id),)),),
                    reference_bindings=({**record, "unit_keys": ["first"]},), subject_facts=(subject,))
                self.assertIn("Explicit source description", str(compile_reference_context(context, self.policy)["fields"]))
                # Re-hashing an old fact cannot silently classify it as a prop.
                frozen = resolved["logical_values"]["references"][0]
                frozen["subject"].pop("category")
                frozen["source_sha256"] = json_contract({key: value for key, value in frozen.items() if key != "source_sha256"}).sha256
                with self.assertRaisesMessage(ValidationError, "不自动推测"):
                    self.project(resolved)

    def test_empty_optional_group_has_no_invented_reference(self):
        self.spec.update(required=False, min_items=0)
        self.target["min_items"] = 0
        self.assertEqual(self.project(self.resolve([]))["references"], [])

    def test_context_only_source_does_not_acquire_a_model_reference_label(self):
        spec = deepcopy(self.spec)
        spec.update(key="context_photo", targets=[])
        definition = self.definition(specs=[self.spec, spec])
        # The same Asset can only be reused when both logical contracts allow it.
        for row in definition.value["configuration"]["inputs"]:
            row["semantic_contract"]["allow_repeated_asset"] = True
        definition = json_contract(definition.value)
        resolved = self.resolve(definition=definition, sources={"references": {"items": [self.item()]},
            "context_photo": {"items": [self.item(item_key="context-only")]}})
        projected = self.project(resolved, definition=definition)
        self.assertEqual(len(projected["references"]), 1)
        self.assertEqual(projected["context_only_item_keys"], ["context-only"])

    def media_protocol_fixture(self):
        specs, targets, values, target_items = [], {}, {}, {}
        for key, kind in (("images", "image"), ("videos", "video"), ("video_audios", "audio"), ("audios", "audio")):
            wiring = {"kind": "classified_group", "node_id": "conditioner", "input_names": [key + ".0", key + ".1"]}
            if key == "video_audios":
                wiring["index_source_target_key"] = "videos"
            targets[key] = {"target_kind": "workflow_slot", "target_key": key, "kind": kind, "min_items": 0, "max_items": 2, "wiring": wiring}
            specs.append({"key": key, "kind": kind, "targets": [{"target_kind": "workflow_slot", "target_key": key, "is_primary": True}]})
            item = {"input_key": key, "item_key": key + "-use", "position": 0, "kind": kind,
                "asset_id": str(uuid4()), "artifact_id": None, "origin": {"kind": "asset"}, "purpose": "configured-purpose",
                "scope_ref": {"kind": "request"}, "subject": None, "paired_to": "videos-use" if key == "video_audios" else None,
                "media_facts": {"sha256": "a" * 64}, "asset_facts": {"name": key, "description": key + " description", "prompt_hint": key + " guidance"}}
            if key == "images":
                item.update(asset_id=None, artifact_id=str(uuid4()), asset_facts=None)
            item["source_sha256"] = json_contract(item).sha256
            values[key] = [item]
            target_items[key] = [{"item_key": item["item_key"], "kind": kind,
                **({"paired_to": item["paired_to"]} if key == "video_audios" else {})}]
        definition = json_contract({"configuration": {"inputs": specs}, "interface": {"input_targets": targets},
            "resources": {"conditioning_mode": None}})
        resolved = {"logical_values": values, "workflow_slots": allocate_workflow_input_slots(targets, target_items)}
        return definition, resolved

    def test_paired_audio_has_one_label_namespace_and_preserves_artifact_identity(self):
        definition, resolved = self.media_protocol_fixture()
        projected = self.project(resolved, definition=definition)
        image, video, audio = projected["references"]
        self.assertIsNone(image["asset_id"])
        self.assertEqual(image["artifact_id"], resolved["logical_values"]["images"][0]["artifact_id"])
        self.assertEqual(video["input_label"], "<Video 1>")
        self.assertEqual(video["audio_input_label"], "<Audio 1>")
        self.assertEqual(audio["input_label"], "<Audio 2>")
        self.assertEqual(video["audio_use"]["target_port_ref"]["input_name"], "video_audios.0")
        self.assertNotIn("local_path", str(projected))

    def test_reference_only_image_pairs_audio_without_inventing_units(self):
        definition, resolved = self.media_protocol_fixture()
        projected = project_execution_reference_materials(definition, json_contract(resolved),
            prompt_type="image", unit_keys=[], scope_units={}).value
        image, video, audio = projected["references"]
        for item in (image, video, audio, video["audio_use"]):
            self.assertNotIn("unit_keys", item)
        self.assertEqual(video["audio_input_label"], "<Audio 1>")

    def test_reference_only_image_preserves_authorized_production_scope(self):
        definition, resolved = self.media_protocol_fixture()
        scope = {"kind": "shot", "id": str(uuid4())}
        for rows in resolved["logical_values"].values():
            for item in rows:
                item["scope_ref"] = scope
                item["source_sha256"] = json_contract({key: value for key, value in item.items() if key != "source_sha256"}).sha256
        with self.assertRaisesMessage(ValidationError, "素材范围尚未映射"):
            project_execution_reference_materials(definition, json_contract(resolved),
                prompt_type="image", unit_keys=[], scope_units={})
        projected = project_execution_reference_materials(definition, json_contract(resolved),
            prompt_type="image", unit_keys=[], scope_units={("shot", scope["id"]): []}).value
        for record in projected["references"]:
            self.assertEqual(record["scope_ref"], scope)
            self.assertNotIn("unit_keys", record)

    def test_mismatched_paired_audio_scope_is_not_discarded(self):
        definition, resolved = self.media_protocol_fixture()
        audio = resolved["logical_values"]["video_audios"][0]
        audio["scope_ref"] = {"kind": "shot", "id": str(uuid4())}
        audio["source_sha256"] = json_contract({key: value for key, value in audio.items() if key != "source_sha256"}).sha256
        with self.assertRaisesMessage(ValidationError, "相同的单元范围"):
            self.project(resolved, definition=definition, scope_units={("shot", audio["scope_ref"]["id"]): ["first"]})
