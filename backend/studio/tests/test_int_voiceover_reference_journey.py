"""INT F06/F07: declared speech and exact pictures through the new recipe.

Only in-memory SQLite, temporary media and declared peer/admission fixtures.
The original import, semantic defaults, Bible/Shot writers, review, input
mapping and final request compiler run unchanged; no live target is called.
"""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase, override_settings

from studio.models import (CharacterBible, ExecutionPreparation, FieldDefinition,
    FieldDefinitionVersion, GenerationAttempt, Shot, ShotCharacterRelation, ShotReferenceUse)
from studio.services.data_structures import write_data_structure, transition_data_structure
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.task_queue import claim_next_task
from studio.tests.int_contract_fixtures import IntegrationContracts
from studio.tests.output_structure_fixtures import frozen_response_structure
from studio.tests.script_semantics_fixtures import script_example, path
from studio.tests import test_int_mixed_journey as mixed
from studio.tests import test_int_production_journey as journey


def speech_example(layout):
    raw, structure, semantics = script_example(layout)
    assert layout == "a"
    shots = raw["episodes"][0]["shots"]
    third = deepcopy(shots[0])
    third["shot"] = 3
    shots.append(third)
    for index, (shot, flag) in enumerate(zip(shots, (True, False, None))):
        shot["dialogue"].update(text=("停一下 🙂\r\nJe reste.", "原文 false：别动！", "未提供标记：照原句。") [index],
            start_ms=(0, 125, 300)[index], end_ms=(950, 1125, 1700)[index])
        if flag is None:
            del shot["dialogue"]["is_voiceover"]
        else:
            shot["dialogue"]["is_voiceover"] = flag
        shot["character_keys"] = ["person-1"] if index == 1 else []
        scene = {"use_key": "station", "entity_key": "scene", "variant_key": None, "media_kind": "image",
            "purpose": "环境参考，不是首帧"}
        shot["references"] = [scene]
        if index == 1:
            shot["references"] = [
                {"use_key": "coat", "entity_key": "person-1", "variant_key": "coat", "media_kind": "image", "purpose": "外套原用途"},
                scene,
                {"use_key": "uniform", "entity_key": "person-1", "variant_key": "uniform", "media_kind": "image", "purpose": "校服原用途"},
            ]
    semantics["node"]["fields"]["episodes"]["item"]["fields"]["shots"]["item"]["fields"]["character_keys"] = path("character_keys")
    return raw, structure, semantics


def speech_structure(*, example, **unused):
    """Declare the missing flag's TRUE default before initial publication.

    Field identities are explicit isolated resources, not inferred runtime
    columns or mutation of an already-published structure.
    """
    from uuid import uuid4
    nodes, definitions = [], {}
    shape = deepcopy(example)
    for episode in shape["episodes"]:
        for shot in episode["shots"]:
            for reference in shot["references"]:
                if reference.get("variant_key") is None:
                    reference["variant_key"] = ""
    for original in frozen_response_structure(shape)["nodes"]:
        node = deepcopy(original)
        kind = node.pop("field_definition")["value_type"]
        if kind not in definitions:
            identity = FieldDefinition.objects.create(key="int-speech-" + uuid4().hex)
            definitions[kind] = FieldDefinitionVersion.objects.create(field_definition=identity,
                version=1, status="published", label=kind, category="test", value_type=kind)
        node["field_definition_version"] = str(definitions[kind].pk)
        node["allow_null"] = node["property_name"] in (*unused.get("nullable_fields", ()), "variant_key")
        if node["property_name"] == "is_voiceover":
            node.update(required=False, has_default=True, default_value=True)
        nodes.append(node)
    draft = write_data_structure(content={"name": "INT explicit voiceover default", "nodes": nodes})
    return transition_data_structure(version_id=draft.pk, status="published")


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class IntegrationVoiceoverReferenceTests(IntegrationContracts, TestCase):
    setUp = journey.IntegrationProductionJourneyTests.setUp
    tearDown = journey.IntegrationProductionJourneyTests.tearDown
    advance = journey.IntegrationProductionJourneyTests.advance
    state = journey.IntegrationProductionJourneyTests.state
    collect = journey.IntegrationProductionJourneyTests.collect
    upload = mixed.IntegrationMixedJourneyTests.upload
    fulfill_primaries = mixed.IntegrationMixedJourneyTests.fulfill_primaries

    def approve(self, key):
        if key != "storyboard_review":
            return mixed.IntegrationMixedJourneyTests.approve(self, key)
        detail = self.api("get", self.url + "storyboard-reference-selection/")
        self.assertEqual(detail["shot_count"], 3)
        return self.api("post", self.url + "approve/", {"stage_key": key, "decision": "approve",
            "comment": "INT isolated voiceover fixture review", "expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
            "expected_selection_sha256": detail["selection_sha256"]})

    def test_explicit_and_missing_voiceover_reaches_exact_request_without_inventing_visible_cast(self):
        self.existing = self.upload("INT speech primary")
        # Patch only fixture producers, never the parsing/mapping/compiling code.
        with patch("studio.tests.int_contract_fixtures.script_example", side_effect=speech_example), patch(
                "studio.tests.int_contract_fixtures.response_structure", side_effect=speech_structure):
            self.sample = self.int_sample(layout="a", mode="mixed")
        self.url = f"/api/v1/production-runs/{self.sample.run.pk}/"
        source_fact = deepcopy(self.sample.revision.__dict__)
        self.assertNotIn("is_voiceover", self.sample.raw["episodes"][0]["shots"][2]["dialogue"])
        self.assertEqual(self.advance().current_stage_key, "bible_review")
        self.approve("bible_review")
        self.fulfill_primaries()
        for _ in range(14):
            run = self.advance()
            if run.current_stage_key == "media_generation":
                break
            self.assertNotIn(run.status, ("failed", "cancelled", "blocked"), self.state())
            if run.status == "awaiting_approval":
                self.approve(run.current_stage_key)
                continue
            task = claim_next_task()
            self.assertIsNotNone(task, self.state())
            self.collect(task)
        else:
            self.fail(self.state())
        self.advance()
        shots = list(Shot.objects.filter(episode__project=self.sample.project).order_by("number"))
        self.assertEqual(len(shots), 3)
        speaker = CharacterBible.objects.get(project=self.sample.project)
        preps = list(ExecutionPreparation.objects.filter(stage_run__production_run=run,
            stage_run__stage_key="media_generation").order_by("unit_key"))
        self.assertEqual(len(preps), 3, self.state())
        for index, shot in enumerate(shots):
            declared = self.sample.raw["episodes"][0]["shots"][index]["dialogue"]
            expected_flag = (True, False, True)[index]
            self.assertEqual(shot.dialogue[0]["text"], declared["text"])
            self.assertIs(shot.dialogue[0]["voiceover"], expected_flag)
            self.assertEqual((shot.dialogue[0]["start_ms"], shot.dialogue[0]["end_ms"]),
                (declared["start_ms"], declared["end_ms"]))
            self.assertEqual(list(ShotCharacterRelation.objects.filter(shot=shot, role="speaker")
                .values_list("character_id", flat=True)), [speaker.pk])
            visible = list(ShotCharacterRelation.objects.filter(shot=shot, role="visible").values_list("character_id", flat=True))
            self.assertEqual(visible, [speaker.pk] if index == 1 else [])
            prep = next(row for row in preps if row.unit_key == f"shot:{shot.pk}")
            self.assertIsNotNone(prep.request_frozen_at, self.state())
            original = read_frozen_execution_request(prep)
            transport = original[2].value
            prompt = transport["prompt_channels"]["values"]["positive_prompt"]
            self.assertIn(declared["text"], prompt)
            units = prep.contract.resolved_inputs["inputs"]["logical_values"]["units"]
            self.assertIs(units["ordered_units"][0]["dialogue"][0]["voiceover"], expected_flag)
            logical = prep.contract.resolved_inputs["inputs"]["logical_values"]["references"]
            uses = list(ShotReferenceUse.objects.filter(shot=shot).order_by("position"))
            self.assertEqual([row["shot_reference"]["use_id"] for row in logical], [str(use.pk) for use in uses])
            self.assertEqual([row["purpose"] for row in logical], [None] * len(uses))
            if index == 1:
                self.assertEqual([use.use_key for use in uses], ["coat", "station", "uniform"])
                self.assertEqual(len({use.reference_version_id for use in uses}), 3)
            else:
                self.assertEqual([row["subject"]["kind"] for row in logical], ["scene"])
            before = deepcopy(ExecutionPreparation.objects.values().get(pk=prep.pk))
            shown = self.api("get", f"/api/v1/execution-preparations/{prep.pk}/prompt-display/",
                {"expected_preparation_revision": prep.revision})
            self.assertEqual(next(row["text"] for row in shown["channels"] if row["key"] == "positive_prompt"), prompt)
            self.assertEqual(ExecutionPreparation.objects.values().get(pk=prep.pk), before)
            self.assertEqual(read_frozen_execution_request(prep), original)
        self.sample.revision.refresh_from_db()
        for field, value in source_fact.items():
            if field != "_state":
                self.assertEqual(getattr(self.sample.revision, field), value)
        self.assertEqual(len(self.submissions), 2)
        self.assertTrue(all(row["kind"] == "image" for row in self.submissions))
        # GenerationAttempt.started_at is its creation timestamp, not submission.
        attempts = list(GenerationAttempt.objects.filter(stage_run__production_run=run,
            stage_run__stage_key="media_generation"))
        self.assertEqual(len(attempts), 3)
        for attempt in attempts:
            self.assertIsNone(attempt.claim_token)
            self.assertIsNone(attempt.claimed_at)
            self.assertIsNone(attempt.submission_started_at)
            self.assertIsNone(attempt.submitted_at)
            self.assertIsNone(attempt.finished_at)
            self.assertEqual(attempt.provider_request_id, "")
            self.assertEqual(attempt.generation_task.status, "queued")
            self.assertIsNone(attempt.generation_task.started_at)
