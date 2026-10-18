"""Real original review -> declared structure -> shared normalizer. No calls."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import Shot, CharacterBible, ShotCharacterRelation
from studio.services.execution_structure_mapping import validate_structure_value
from studio.services.execution_reference_inputs import reference_context_input_schema
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.json_contracts import json_contract
from studio.services.production_recipe_contracts import validate_recipe_input_sources
from studio.services.production_recipe_media_preparations import resolve_recipe_media_sources
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_reviewed_content import read_reviewed_storyboard
from studio.tests import test_production_shot_context as fixtures


def literal(value):
    return {"kind": "literal", "value": value}


def field(selector):
    return {"kind": "context_field", "selector_key": selector, "scope": {"kind": "shot"}}


class StructureMappingTests(TestCase):
    setUp = fixtures.ShotContextTests.setUp
    approve = fixtures.ShotContextTests.approve
    reapprove = fixtures.ShotContextTests.reapprove

    def definition(self, schema=None):
        payload = fixtures.ShotContextTests.definition(self, "json").value
        if schema is not None:
            payload["configuration"]["inputs"][0]["targets"] = [{"target_kind": "profile_input", "target_key": "units"}]
            payload["interface"]["profile_inputs"] = {"units": {"value_schema": schema}}
        return json_contract(payload)

    def resolve(self, template, *, schema=None, shot=None, unit_key=None):
        scope = {"kind": "shot", "id": str((shot or self.shot).pk)}
        definition = self.definition(schema)
        rules = {"brief": {"kind": "structured", "template": template}}
        validate_recipe_input_sources(definition, rules, prior_stages=["storyboard_review"])
        bound = resolve_recipe_media_sources(definition=definition, route={"input_sources": rules}, stage=self.stage,
            plan=get_run_execution_plan(self.run), scope=scope)
        result = resolve_execution_input_sources(definition, bound["inputs"], owner=self.actor, subject_facts=[],
            max_media_bytes=1024*1024, project_id=self.project.pk, production_run_id=str(self.run.pk),
            eligible_stage_ids=bound["eligible_stage_ids"], production_scope=scope,
            unit_key=unit_key if unit_key is not None else f"shot:{scope['id']}")
        return bound, result.value

    def test_structure_uses_actual_opaque_unit_not_entity_scope(self):
        # Root adapters supply identity; the source scope still authorizes only
        # this real shot. Different appearances must not collapse a unit_map.
        for key in ("appearance:first", "appearance:second"):
            _, result = self.resolve({"kind": "unit_map", "value": {"kind": "unit_key"}}, unit_key=key)
            self.assertEqual(result["logical_values"]["brief"], {key: key})
            self.assertEqual(result["input_provenance"][0]["leaves"][0]["unit_key"], key)

    def test_structure_without_root_unit_identity_cannot_guess_from_scope(self):
        for key in (None, "", " ", 1, "x" * 192):
            with self.subTest(key=key), self.assertRaisesMessage(ValidationError, "实际单元标识"):
                resolve_execution_input_sources(self.definition(),
                    {"brief": {"kind": "structured", "template": {"kind": "unit_key"}}},
                    owner=self.actor, subject_facts=[], max_media_bytes=1024, project_id=self.project.pk,
                    production_run_id=str(self.run.pk), production_scope={"kind": "shot", "id": str(self.shot.pk)},
                    unit_key=key)

    def test_two_real_shots_have_distinct_unit_keys_timing_and_leaf_review_evidence(self):
        self.shot.duration_ms = 2345; self.shot.save(update_fields=("duration_ms",))
        second = Shot.objects.create(episode=self.episode, number=2, duration_ms=2000,
            media_type="image", include_audio=False)
        self.reapprove()
        template = {"kind": "unit_map", "value": {"kind": "object", "fields": {
            "dialogue": field("shot.dialogue"), "duration": field("shot.duration_ms")}}}
        for shot in (self.shot, second):
            bound, result = self.resolve(template, shot=shot)
            self.assertEqual(result["logical_values"]["brief"], {f"shot:{shot.pk}": {"dialogue": shot.dialogue, "duration": shot.duration_ms}})
            self.assertEqual(bound["inputs"]["brief"]["template"]["value"]["fields"]["duration"]["kind"], "context_field")
            leaves = result["input_provenance"][0]["leaves"]
            self.assertTrue(all(row["review"]["decision_id"] == str(self.decision.pk) for row in leaves))
            self.assertTrue(all(row["scope"]["id"] == str(shot.pk) for row in leaves))

    def test_reference_unit_schema_uses_declared_fields_not_shared_literal_sample(self):
        schema = reference_context_input_schema()
        unit_schema = schema["properties"]["ordered_units"]["items"]
        # Blank text/list values here are explicit fixture author choices, not
        # renderer defaults. Actual duration/continuity are reviewed. Creative
        # field wiring is tested by test_execution_shot_field_mapping.
        fields = {"unit_key": {"kind": "unit_key"}, "duration_ms": field("shot.duration_ms"),
            "subject_keys": {"kind": "reference_subjects"}, "dialogue": literal([]), "voice_profiles": literal([]),
            "continuity_notes": literal(""), "ambient_sound": literal(""),
            "requires_first_frame_continuity": field("shot.requires_first_frame_continuity"),
            "requires_last_frame_continuity": field("shot.requires_last_frame_continuity")}
        root = {"kind": "object", "fields": {key: literal("") for key in
            ("color_script", "extra_direction", "overall_soundscape", "non_diegetic_music")}}
        root["fields"].update(schema_version=literal(1), ordered_units={"kind": "array", "items": [{"kind": "object", "fields": fields}]})
        _, result = self.resolve(root, schema=schema)
        unit = result["logical_values"]["brief"]["ordered_units"][0]
        self.assertEqual(unit["unit_key"], f"shot:{self.shot.pk}")
        self.assertEqual(unit["duration_ms"], self.shot.duration_ms)
        missing = deepcopy(root); del missing["fields"]["ordered_units"]["items"][0]["fields"]["duration_ms"]
        with self.assertRaisesMessage(ValidationError, "必需字段"): self.resolve(missing, schema=schema)

    def test_each_preserves_dialogue_order_and_only_reads_registered_fields(self):
        first = CharacterBible.objects.create(project=self.project, name="First", slug="first", stable_speaker_id="S1")
        second = CharacterBible.objects.create(project=self.project, name="Second", slug="second", stable_speaker_id="S2")
        ShotCharacterRelation.objects.create(shot=self.shot, character=second, role="speaker", position=0)
        ShotCharacterRelation.objects.create(shot=self.shot, character=first, role="speaker", position=1)
        self.shot.dialogue = [{"speaker_id": "S2", "text": "第二个人先说"}, {"speaker_id": "S1", "text": "第一个人后说"}]
        self.shot.save(update_fields=("dialogue",)); self.reapprove()
        node = {"kind": "each", "source": field("shot.dialogue"), "item": {"kind": "object", "fields": {
            "speaker": {"kind": "item_field", "path": ["speaker_id"]}, "content": {"kind": "item_field", "path": ["text"]}}}}
        _, result = self.resolve(node)
        self.assertEqual(result["logical_values"]["brief"], [{"speaker": "S2", "content": "第二个人先说"}, {"speaker": "S1", "content": "第一个人后说"}])
        bad = deepcopy(node); bad["item"]["fields"]["content"]["path"] = ["text.__class__"]
        with self.assertRaisesMessage(ValidationError, "目录"): self.resolve(bad)
        bad["item"]["fields"]["content"]["path"] = ["start_ms"]
        with self.assertRaisesMessage(ValidationError, "缺少所选字段"): self.resolve(bad)

    def test_invalid_shapes_sources_or_implicit_scope_are_not_accepted(self):
        for node in ({"kind": "eval", "value": "shot.duration*2"}, {"kind": "unit_key", "value": "fake"},
                {"kind": "item_field", "path": ["text"]}, {"kind": "each", "source": literal([]), "item": literal("")},
                {"kind": "object", "fields": []}, {"kind": []}):
            with self.subTest(node=node), self.assertRaises(ValidationError): self.resolve(node)
        rules = {"brief": {"kind": "structured", "template": field("shot.duration_ms")}}
        with self.assertRaisesMessage(ValidationError, "更早"):
            validate_recipe_input_sources(self.definition(), rules, prior_stages=[])
        _, result = self.resolve({"kind": "unit_key"})
        self.assertEqual(result["logical_values"]["brief"], f"shot:{self.shot.pk}")
        with self.assertRaisesMessage(ValidationError, "明确的运行"):
            resolve_execution_input_sources(self.definition(), rules, owner=self.actor, subject_facts=[], max_media_bytes=1024,
                project_id=self.project.pk)

    def test_declared_deep_path_binds_original_review_then_reads_actual_value(self):
        speaker = CharacterBible.objects.create(project=self.project, name="Speaker", slug="speaker", stable_speaker_id="S2")
        ShotCharacterRelation.objects.create(shot=self.shot, character=speaker, role="speaker", position=0)
        self.shot.dialogue = [{"speaker_id": "S2", "text": "配置路径读取", "voiceover": False}]
        self.shot.save(update_fields=("dialogue",)); self.reapprove()
        template = {**field("shot.dialogue"), "path": [0, "text"]}
        before = deepcopy(template)
        bound, result = self.resolve(template, schema={"type": "string"})
        self.assertEqual(result["logical_values"]["brief"], "配置路径读取")
        self.assertEqual(template, before)
        frozen = bound["inputs"]["brief"]["template"]
        self.assertEqual(frozen["path"], [0, "text"])
        self.assertEqual(len(frozen["source_schema_sha256"]), 64)
        origin = result["input_provenance"][0]["leaves"][0]
        self.assertEqual(origin["review"]["decision_id"], str(self.decision.pk))
        self.assertEqual(origin["source_path"], "source[0].text")
        self.assertNotEqual(origin["source_value_sha256"], origin["value_sha256"])
        self.assertEqual(origin["source_schema_sha256"], frozen["source_schema_sha256"])
        for tokens in ([False], [0, "unknown"], ["text"], None):
            with self.subTest(tokens=tokens), self.assertRaises(ValidationError):
                self.resolve({**field("shot.dialogue"), "path": tokens})
        with self.assertRaisesMessage(ValidationError, "source[2]"):
            self.resolve({**field("shot.dialogue"), "path": [2, "text"]})
        with self.assertRaisesMessage(ValidationError, "类型不匹配"):
            self.resolve(template, schema={"type": "boolean"})
        frozen["source_schema_sha256"] = "0" * 64
        with self.assertRaisesMessage(ValidationError, "来源结构目录已变化"):
            resolve_execution_input_sources(self.definition(), bound["inputs"], owner=self.actor, subject_facts=[],
                max_media_bytes=1024, project_id=self.project.pk, production_run_id=str(self.run.pk),
                eligible_stage_ids=bound["eligible_stage_ids"], production_scope={"kind": "shot", "id": str(self.shot.pk)},
                unit_key=f"shot:{self.shot.pk}")

    def test_schema_types_constants_unique_and_actual_dynamic_values_are_checked(self):
        for value, schema in ((True, {"type": "integer"}), (2, {"type": "integer", "const": 1}),
                (["x", "x"], {"type": "array", "uniqueItems": True, "items": {"type": "string"}}),
                ({"extra": 1}, {"type": "object", "properties": {}, "additionalProperties": False}),
                ({}, {"type": "object", "properties": {"x": {}}, "required": ["x"]})):
            with self.subTest(value=value), self.assertRaises(ValidationError): validate_structure_value(value, schema, "test")
        self.resolve(field("shot.duration_ms"), schema={"type": "integer", "minimum": 1})
        with self.assertRaisesMessage(ValidationError, "不能大于"):
            self.resolve(field("shot.duration_ms"), schema={"type": "integer", "maximum": 1})

    def test_review_drift_and_caller_substitution_cannot_be_hidden_in_a_structure(self):
        template = {"kind": "object", "fields": {"duration": field("shot.duration_ms")}}
        bound, _ = self.resolve(template)
        self.shot.duration_ms += 1; self.shot.save(update_fields=("duration_ms",))
        with self.assertRaisesMessage(ValidationError, "已变更"): self.resolve(template)
        self.reapprove()
        with self.assertRaises(ValidationError):
            resolve_execution_input_sources(self.definition(), bound["inputs"], owner=self.actor, subject_facts=[], max_media_bytes=1024,
                project_id=self.project.pk, production_run_id=str(self.run.pk), eligible_stage_ids=bound["eligible_stage_ids"],
                production_scope={"kind": "shot", "id": str(self.shot.pk)}, unit_key=f"shot:{self.shot.pk}")

    def test_repeated_leaves_share_review_reads_at_binding_and_normalization(self):
        template = {"kind": "object", "fields": {str(i): field("shot.duration_ms") for i in range(8)}}
        with patch("studio.services.production_reviewed_content.read_reviewed_storyboard", wraps=read_reviewed_storyboard) as reader:
            self.resolve(template)
        self.assertEqual(reader.call_count, 2)  # one at binding, one normalization

    def test_subjects_come_from_the_same_real_normalized_primary_images(self):
        from studio.tests.test_production_reference_mapping import spec as media_spec
        character = CharacterBible.objects.create(project=self.project, name="Used subject", slug="used-subject", stable_speaker_id="S1", anchor_asset=self.asset)
        CharacterBible.objects.create(project=self.project, name="Unrelated subject", slug="unused-subject", stable_speaker_id="S2")
        payload = self.definition().value
        image_spec = media_spec()
        image_spec["semantic_contract"].update(schema_version=1, allow_repeated_asset=False)
        image_spec["targets"] = [{"target_kind": "operation_input", "target_key": "images", "is_primary": True}]
        payload["configuration"]["inputs"].append(image_spec)
        payload["interface"]["input_targets"]["images"] = {"target_kind": "operation_input", "kind": "image"}
        scope = {"kind": "shot", "id": str(self.shot.pk)}
        result = resolve_execution_input_sources(json_contract(payload), {
            "brief": {"kind": "structured", "template": {"kind": "reference_subjects"}},
            "images": {"items": [{"kind": "asset", "item_key": "current-image", "asset_id": str(self.asset.pk), "purpose": None,
                "subject_ref": {"kind": "character", "id": str(character.pk)}, "scope_ref": scope, "paired_to": None}]}},
            owner=self.actor, subject_facts=[], max_media_bytes=1024*1024, project_id=self.project.pk,
            production_run_id=str(self.run.pk), production_scope=scope, unit_key=f"shot:{self.shot.pk}").value
        self.assertEqual(result["logical_values"]["brief"], [["character", str(character.pk)]])
        self.assertEqual(result["logical_values"]["images"][0]["subject"]["id"], str(character.pk))

    def test_structure_cannot_bypass_a_protocol_requiring_a_real_context_source(self):
        payload = self.definition().value
        payload["configuration"]["inputs"][0]["targets"] = [{"target_kind": "operation_input", "target_key": "reviewed"}]
        payload["interface"]["input_targets"]["reviewed"] = {"source_kinds": ["context_field"]}
        rules = {"brief": {"kind": "structured", "template": literal({"fake": "approval"})}}
        with self.assertRaisesMessage(ValidationError, "不能用字面值伪造"):
            validate_recipe_input_sources(json_contract(payload), rules, prior_stages=["storyboard_review"])
        with self.assertRaisesMessage(ValidationError, "真实上下文"):
            resolve_execution_input_sources(json_contract(payload), rules, owner=self.actor, subject_facts=[], max_media_bytes=1024,
                project_id=self.project.pk, production_run_id=str(self.run.pk), production_scope={"kind": "shot", "id": str(self.shot.pk)},
                unit_key=f"shot:{self.shot.pk}")
