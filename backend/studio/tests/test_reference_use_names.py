"""M6-02: exact use identities, multilingual names and allocated ports.

Real local PNG resolution and frozen auxiliary protocol fixtures, never a model
call, live database change, current-version lookup or real acceptance result.
"""
from copy import deepcopy
from dataclasses import replace, asdict
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.services.bible_reference_contract import bible_reference_name_key
from studio.services.execution_reference_fields import prepare_reference_profile_fields
from studio.services.execution_reference_display import frozen_reference_display
from studio.services.execution_workflow_uses import read_execution_workflow_uses
from studio.services.execution_text_contracts import build_execution_text_payload
from studio.services.execution_field_contracts import expand_field_request
from studio.services.json_contracts import JsonContract, json_contract
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.reference_context import compile_reference_context
from studio.services.reference_context_facts import ReferenceSubjectFacts, freeze_reference_context_facts, read_reference_context_facts
from studio.tests import test_video_reference_projection as fixtures


class ReferenceUseNameTests(TestCase):
    setUpTestData = classmethod(fixtures.VideoReferenceProjectionTests.setUpTestData.__func__)
    setUp = fixtures.VideoReferenceProjectionTests.setUp
    definition = fixtures.VideoReferenceProjectionTests.definition
    resolve = fixtures.VideoReferenceProjectionTests.resolve
    item = fixtures.VideoReferenceProjectionTests.item
    project = fixtures.VideoReferenceProjectionTests.project
    media_protocol_fixture = fixtures.VideoReferenceProjectionTests.media_protocol_fixture
    profile = fixtures.VideoReferenceProjectionTests.profile
    _profile = fixtures.VideoReferenceProjectionTests._profile
    _field = fixtures.VideoReferenceProjectionTests._field
    _publish = fixtures.VideoReferenceProjectionTests._publish

    def _rule(self, *args, **kwargs):
        rule = fixtures.VideoReferenceProjectionTests._rule(self, *args, **kwargs)
        rule.generation_validator_keys = ["protected_tokens_preserve_count"]
        rule.save(update_fields=("generation_validator_keys",))
        return rule

    def named_sources(self, names):
        self.spec["max_items"] = self.target["max_items"] = len(names)
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        self.target["wiring"]["input_names"] = [f"ref_images.{index}" for index in range(len(names))]
        subjects = [{"id": str(uuid4()), "category": "character", "name": name,
                     "description": f"Explicit body {index}"} for index, name in enumerate(names)]
        items = [self.item(item_key=f"use-{index}", subject_ref={"kind": "external", "id": subject["id"]})
                 for index, subject in enumerate(subjects)]
        return items, subjects

    def subject_policy(self):
        policy = deepcopy(self.policy)
        policy["reference_layout"] = {"subject_label_template": "<Subject {index}>", "source_separator": ", ",
            "summary_reference_template": "{label}: {usage}", "shot_reference_template": ""}
        policy["templates"]["entity_reference"] = "{label} is {subject} from {source_labels}."
        return policy

    def context_for(self, projection, source, *, scoped=False):
        subjects = tuple(ReferenceSubjectFacts(**{**row, "description_parts": tuple(row["description_parts"])})
                         for row in projection["subjects"])
        identities = tuple((subject.category, subject.identity) for subject in subjects)
        return replace(self.context, language="Chinese", subject_facts=subjects,
            reference_bindings=tuple(projection["references"]),
            ordered_units=tuple(replace(self.unit, unit_key=key,
                subject_keys=identities[index:index + 1] if scoped else identities,
                render_values={**self.unit.render_values, "description": source})
                for index, key in enumerate(("first", "second"))))

    def prepare(self, context, profile, source):
        return prepare_reference_profile_fields(profile=profile, reference_context=context,
            generation_contexts={key: {"target_shot.core": {"direction": source}} for key in ("first", "second")},
            structured_inputs={key: {"direction": source} for key in ("first", "second")},
            source_language="Chinese", target_language="Chinese").value

    def display(self, definition, resolved, projection):
        context = freeze_reference_context_facts(self.context_for(projection, "原正文不变"), max_bytes=65536)
        return frozen_reference_display({"profile_sources": {"reference_context": asdict(context)}},
            read_execution_workflow_uses(definition, json_contract(resolved)))

    def test_homonyms_and_unicode_equivalents_follow_use_identity_when_ports_reorder(self):
        for names, duplicate in ((["林岚", "林岚"], True), (["Ｃafé", "Cafe\u0301"], True),
                                 (["Straße", "STRASSE"], True), (["東京", "とうきょう"], False)):
            with self.subTest(names=names):
                items, subjects = self.named_sources(names)
                resolved = self.resolve(items, subject_facts=subjects)
                original = deepcopy(resolved)
                first_projection = self.project(resolved, prompt_type="video")
                backward = self.resolve(items[::-1], subject_facts=subjects)
                second_projection = self.project(backward, prompt_type="video")
                first, second = first_projection["references"], second_projection["references"]
                display = self.display(self.definition(), resolved, first_projection)
                reversed_display = self.display(self.definition(), backward, second_projection)
                by_key = {row["item_key"]: row for row in second}
                self.assertEqual(len({bible_reference_name_key(row["entity_label"]) for row in first}), 2)
                for index, row in enumerate(first):
                    reordered = by_key[row["item_key"]]
                    self.assertEqual(row["original_name"], names[index])
                    self.assertEqual(row["name_disambiguated"], duplicate)
                    self.assertEqual(row["entity_id"], subjects[index]["id"])
                    self.assertEqual(row["entity_label"], reordered["entity_label"])
                    self.assertEqual(row["source_sha256"], original["logical_values"]["references"][index]["source_sha256"])
                    self.assertEqual(row["input_label"], f"<Picture {index + 1}>")
                    self.assertEqual(reordered["input_label"], f"<Picture {2 - index}>")
                    self.assertEqual(reordered["target_port_ref"]["input_name"], f"ref_images.{1 - index}")
                    self.assertEqual(row["asset_id"], reordered["asset_id"])
                    self.assertEqual(display[row["item_key"]]["reference_name"], row["entity_label"])
                    self.assertEqual(display[row["item_key"]]["input_slot"], index + 1)
                    self.assertEqual(reversed_display[row["item_key"]]["input_slot"], 2 - index)
                    self.assertEqual(reversed_display[row["item_key"]]["reference_name"], row["entity_label"])
                self.assertEqual(resolved, original)

    def test_authored_alias_like_names_and_normalized_use_keys_cannot_collide(self):
        items, subjects = self.named_sources(["Alex", "Alex", "Alex [use-0]"])
        projected = self.project(self.resolve(items, subject_facts=subjects))["references"]
        self.assertEqual(projected[0]["entity_label"], "Alex [use-0] [use-0]")
        self.assertEqual(projected[2]["entity_label"], "Alex [use-0]")
        self.assertFalse(projected[2]["name_disambiguated"])
        self.assertEqual([row["original_name"] for row in projected], [row["name"] for row in subjects])
        items, subjects = self.named_sources(["林岚", "林岚"])
        items[0]["item_key"], items[1]["item_key"] = "Ａ", "A"
        forward = self.project(self.resolve(items, subject_facts=subjects))["references"]
        backward = self.project(self.resolve(items[::-1], subject_facts=subjects))["references"]
        self.assertEqual(len({bible_reference_name_key(row["entity_label"]) for row in forward}), 2)
        self.assertEqual({row["item_key"]: row["entity_label"] for row in forward},
                         {row["item_key"]: row["entity_label"] for row in backward})

    def test_repeated_entity_and_unassociated_asset_remain_separate_uses_not_fake_entities(self):
        items, subjects = self.named_sources(["林岚", "林岚"])
        items[1]["subject_ref"] = deepcopy(items[0]["subject_ref"])
        projection = self.project(self.resolve(items, subject_facts=subjects[:1]))
        self.assertEqual(len(projection["subjects"]), 1)
        self.assertEqual([row["entity_label"] for row in projection["references"]], ["林岚"] * 2)
        self.assertTrue(all(not row["name_disambiguated"] for row in projection["references"]))
        self.assertEqual({row["entity_id"] for row in projection["references"]}, {subjects[0]["id"]})
        projection = self.project(self.resolve([self.item(item_key="left"), self.item(item_key="right")]))
        self.assertEqual(projection["subjects"], [])
        self.assertEqual([row["original_name"] for row in projection["references"]], [self.asset.name] * 2)
        self.assertEqual(len({row["entity_label"] for row in projection["references"]}), 2)
        self.assertTrue(all(row["entity_id"] is None for row in projection["references"]))

    def test_same_subject_multiple_pictures_and_other_homonym_keep_separate_identities(self):
        for category in ("character", "scene", "prop"):
            with self.subTest(category=category):
                items, subjects = self.named_sources(["同名主体"] * 3)
                items[1]["subject_ref"] = deepcopy(items[0]["subject_ref"])
                subjects = [subjects[0], subjects[2]]
                for subject in subjects:
                    subject["category"] = category
                resolved = self.resolve(items, subject_facts=subjects)
                projection = self.project(resolved, prompt_type="video")
                refs = projection["references"]
                self.assertEqual(refs[0]["entity_label"], refs[1]["entity_label"])
                self.assertNotEqual(refs[0]["entity_label"], refs[2]["entity_label"])
                self.assertTrue(all(row["name_disambiguated"] for row in refs))
                reversed_refs = self.project(self.resolve(items[::-1], subject_facts=subjects), prompt_type="video")["references"]
                self.assertEqual({r["item_key"]: r["entity_label"] for r in refs},
                    {r["item_key"]: r["entity_label"] for r in reversed_refs})
                fields = compile_reference_context(self.context_for(projection, "Explicit action."), self.subject_policy())["fields"]
                self.assertEqual(len(fields["subject_definitions"]), 2)
                self.assertIn("<Picture 1>, <Picture 2>", fields["subject_definitions"][0])
                self.assertIn("<Picture 3>", fields["subject_definitions"][1])

    def test_reviewed_primary_variant_resolve_render_freeze_and_reach_auxiliary_wire(self):
        from studio.models import ShotReferenceUse
        from studio.services.bible_variants import create_variant
        from studio.services.production_reference_mapping import reference_use_records, expand_reviewed_reference_sources
        from studio.services.shot_reference_sources import resolve_shot_reference_sources
        from studio.tests.test_execution_input_resolution import ExecutionInputResolutionTests
        from studio.tests.test_production_reference_mapping import mapping

        project, shot, subject, primary_use, _ = ExecutionInputResolutionTests.reviewed_shot_use(self)
        primary = primary_use.setting_revision
        variant = create_variant(actor=self.owner, project_id=project.pk, entity_kind="character", entity_id=subject.pk,
            appearance_role="variant", primary_setting_revision_id=primary.pk, primary_setting_sha256=primary.content_sha256,
            value={"name": "Courier without coat", "reference_name": "Courier shirt", "identity_anchor": "",
                "appearance_anchor": "Shirt without coat", "basis_asset_id": None, "basis_asset_sha256": ""}).revisions.get(revision=1)
        ShotReferenceUse.objects.create(shot=shot, use_key="shirt", position=1, media_kind="image", purpose="Changed clothing",
            setting_revision=variant, source_mode="existing", asset=subject.anchor_asset, asset_sha256=subject.anchor_asset.checksum)
        uses = resolve_shot_reference_sources(actor=self.owner, project_id=project.pk, shot_ids=[str(shot.pk)]).value["shots"][0]["uses"]
        sources, excluded = expand_reviewed_reference_sources(specs=[self.spec], sources={"references": mapping("images")},
            records=reference_use_records(uses), shot_id=str(shot.pk))
        self.assertEqual(excluded, [])
        resolved = self.resolve(sources=sources, project_id=project.pk)
        original = deepcopy(resolved)
        projection = self.project(resolved, scope_units={("shot", str(shot.pk)): ["first", "second"]}, prompt_type="video")
        refs = projection["references"]
        self.assertEqual(len(projection["subjects"]), 1)
        self.assertEqual([r["asset_id"] for r in refs], [str(self.asset.pk), str(subject.anchor_asset_id)])
        self.assertEqual([r["entity_label"] for r in refs], [subject.name] * 2)
        self.assertNotEqual(refs[0]["shot_reference"], refs[1]["shot_reference"])
        self.assertNotEqual(refs[0]["source_sha256"], refs[1]["source_sha256"])
        self.assertNotEqual(refs[0]["target_port_ref"], refs[1]["target_port_ref"])
        context = replace(self.context_for(projection, "人物脱下外套。"), prompt_type="video",
            frame_policy={"fps": 24, "minimum_frames": 1, "maximum_frames": 240, "alignment_modulus": 1, "alignment_remainder": 0})
        frozen = freeze_reference_context_facts(context, max_bytes=65536)
        restored = read_reference_context_facts(frozen, max_bytes=65536)
        policy = self.subject_policy()
        rendered = compile_reference_context(restored, policy)["fields"]
        self.assertEqual(len(rendered["subject_definitions"]), 1)
        self.assertIn("<Picture 1>, <Picture 2>", rendered["subject_definitions"][0])
        config = fixtures.rendering_config()
        config["primitive_options"]["reference_context_ir_v1"]["rendering"] = policy
        with patch.object(fixtures, "rendering_config", return_value=config):
            profile = self.profile("video", "model_generated")
        prepared = self.prepare(restored, profile, "人物脱下外套。")
        batch = JsonContract(**prepared["field_requests"][0])
        wire = build_execution_text_payload({"operation": "prompt.fields.generate", "output": {"adapter": "text.prompt_fields/v1"},
            "parameters": {}, "prompt": {"mode": "none", "channels": {}}, "chat_request_options": {}},
            parameters={}, channels={}, field_request=batch)
        self.assertEqual(json.loads(wire["messages"][1]["content"]), batch.value)
        expanded = expand_field_request(batch.value)
        for row in expanded["items"]:
            self.assertEqual(row["reference_facts"]["logical_inputs"], refs)
        display = self.display(self.definition(), resolved, projection)
        self.assertEqual([display[r["item_key"]]["reference_name"] for r in refs], [subject.name] * 2)
        self.assertEqual(resolved, original)

    def test_same_subject_actual_role_conflict_is_still_rejected(self):
        items, subjects = self.named_sources(["林岚", "林岚"])
        items[1]["subject_ref"] = deepcopy(items[0]["subject_ref"])
        projection = self.project(self.resolve(items, subject_facts=subjects[:1]), prompt_type="video")
        context = self.context_for(projection, "Explicit action.")
        policy = self.subject_policy()
        rule = next(r for r in policy["reference_rules"] if r["category"] == "character" and r["purpose"] == "")
        policy["reference_rules"].append({**rule, "purpose": "different-use"})
        conflicting = deepcopy(context.reference_bindings)
        conflicting[1]["purpose"] = "different-use"
        with self.assertRaisesMessage(PromptBuildError, "参考用途不一致"):
            compile_reference_context(replace(context, reference_bindings=conflicting), policy)
        inconsistent_names = deepcopy(context.reference_bindings)
        inconsistent_names[1]["entity_label"] = "Different display name"
        with self.assertRaisesMessage(PromptBuildError, "参考名称不一致"):
            compile_reference_context(replace(context, reference_bindings=inconsistent_names), policy)

    def test_paired_audio_uses_same_name_namespace_without_inventing_artifact_names(self):
        definition, resolved = self.media_protocol_fixture()
        for key, rows in resolved["logical_values"].items():
            row = rows[0]
            if row["asset_facts"] is not None:
                row["asset_facts"]["name"] = "相同素材"
            row["source_sha256"] = json_contract({name: value for name, value in row.items() if name != "source_sha256"}).sha256
        before = deepcopy(resolved)
        projection = self.project(resolved, definition=definition, prompt_type="video")
        image, video, audio = projection["references"]
        named = [video, video["audio_use"], audio]
        display = self.display(definition, resolved, projection)
        for row in [image, *named]:
            self.assertEqual(display[row["item_key"]]["reference_name"], row["entity_label"])
            self.assertEqual(display[row["item_key"]]["input_slot"], row["input_slot"])
        self.assertEqual(len({row["entity_label"] for row in named}), 3)
        self.assertEqual([row["original_name"] for row in named], ["相同素材"] * 3)
        self.assertEqual(image["entity_label"], "")
        self.assertEqual(image["original_name"], "")
        self.assertFalse(image["name_disambiguated"])
        self.assertIsNotNone(image["artifact_id"])
        context = self.context_for(projection, "保留原始正文")
        policy = deepcopy(self.policy)
        policy["reference_rules"] = [{"category": kind, "purpose": "configured-purpose", "task": "fixture",
            "relationship": "reference", "usage": "Explicit fixture"} for kind in ("image", "video", "audio")]
        rendered = compile_reference_context(context, policy)["fields"]["subject_definitions"]
        for row in named:
            self.assertTrue(any(row["entity_label"] in text and row["input_label"] in text for text in rendered))
        self.assertEqual(resolved, before)

    def test_auxiliary_wire_and_renderer_share_exact_scoped_names_without_replacing_free_text(self):
        items, subjects = self.named_sources(["林岚", "林岚"])
        projection = self.project(self.resolve(items, subject_facts=subjects))
        source = "林岚转身。名字相同也不猜测或替换正文。"
        context = self.context_for(projection, source, scoped=True)
        original = deepcopy(context)
        for mode, operation in (("model_generated", "prompt.fields.generate"), ("translate_only", "prompt.fields.translate")):
            with self.subTest(operation=operation):
                prepared = self.prepare(context, self.profile("image", mode), source)
                batch = JsonContract(**prepared["field_requests"][0])
                wire = build_execution_text_payload({"operation": operation, "output": {"adapter": "text.prompt_fields/v1"},
                    "parameters": {}, "prompt": {"mode": "none", "channels": {}}, "chat_request_options": {}},
                    parameters={}, channels={}, field_request=batch)
                payload = json.loads(wire["messages"][1]["content"])
                self.assertEqual(payload, batch.value)
                for index, row in enumerate(expand_field_request(payload)["items"]):
                    self.assertEqual(row["source_text"], source)
                    self.assertEqual(row["reference_facts"]["logical_inputs"], [projection["references"][index]])
                    self.assertEqual(row["reference_facts"]["subject_facts"][0]["identity"], subjects[index]["id"])
                    self.assertEqual(row["reference_facts"]["protected_tokens"], [])
        frozen = freeze_reference_context_facts(context, max_bytes=65536)
        restored = read_reference_context_facts(frozen, max_bytes=65536)
        self.assertEqual(restored, context)
        self.asset.name = "Later rename must not rewrite a frozen name"
        self.asset.save(update_fields=("name",))
        rendered = compile_reference_context(restored, self.policy)["fields"]
        for row in projection["references"]:
            self.assertTrue(any(row["entity_label"] in text and row["input_label"] in text
                                for text in rendered["subject_definitions"]))
        self.assertIn(source, str(rendered))
        self.assertEqual(context, original)

    def test_marker_like_name_does_not_declare_an_unallocated_slot(self):
        items, subjects = self.named_sources(["<Picture 99>"])
        projection = self.project(self.resolve(items, subject_facts=subjects))
        source = "Use <Picture 99>"
        context = self.context_for(projection, source)
        profile = self.profile("image", "model_generated")
        with self.assertRaises(ValidationError):
            self.prepare(context, profile, source)
        source = "Use <Picture 1>"
        # The actual port remains legal; the creative name cannot grant 99.
        rule = profile.field_rules.get()
        self.assertIn("protected_tokens_preserve_count", rule.generation_validator_keys)
        prepared = self.prepare(self.context_for(projection, source), profile, source)
        self.assertEqual(prepared["field_requests"][0]["value"]["items"][0]["reference_facts"]["protected_tokens"], ["<Picture 1>"])

    def test_malformed_frozen_asset_name_is_explicitly_rejected_not_filled_from_current_asset(self):
        resolved = self.resolve()
        for name in (None, 42, [], {}):
            for prompt_type in ("image", "video"):
                with self.subTest(name=name, prompt_type=prompt_type):
                    invalid = deepcopy(resolved)
                    row = invalid["logical_values"]["references"][0]
                    row["asset_facts"]["name"] = name
                    row["source_sha256"] = json_contract({key: value for key, value in row.items() if key != "source_sha256"}).sha256
                    with self.assertRaisesMessage(ValidationError, "冻结素材原名"):
                        self.project(invalid, prompt_type=prompt_type)
        row = resolved["logical_values"]["references"][0]
        row["asset_facts"].pop("name")
        row["source_sha256"] = json_contract({key: value for key, value in row.items() if key != "source_sha256"}).sha256
        with self.assertRaisesMessage(ValidationError, "冻结素材原名"):
            self.project(resolved)
