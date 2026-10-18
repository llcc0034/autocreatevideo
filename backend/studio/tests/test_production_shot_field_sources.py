"""Bound source -> exact manual revision -> original review, no external calls.

The compiler proof uses the shared structured-input seam in isolation; recipe
authoring/binding and other runtime consumers are not claimed by these tests.
"""
from copy import deepcopy
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, ExecutionPreparation, GenerationAttempt, ShotFieldRevision, ShotReferenceUse
from studio.services.production_shot_field_sources import read_reviewed_shot_field_sources, select_shot_field
from studio.services.shot_field_revisions import read_frozen_shot_field_revision, _detail
from studio.tests import test_shot_field_revisions as revisions
from studio.tests.test_storyboard_declared_writer import no_dml


def selection(field):
    return {"structure_version_id": field["structure_version_id"], "node_key": field["node_key"],
        "field_definition_version_id": field["field_definition"]["field_definition_version_id"]}


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class FrozenShotFieldSourcesTests(TestCase):
    create = revisions.ShotFieldRevisionTests.create
    post = revisions.ShotFieldRevisionTests.post
    read = revisions.ShotFieldRevisionTests.read
    adopt_source = revisions.ShotFieldRevisionTests.adopt_source
    approval = revisions.ShotFieldRevisionTests.approval
    revision_url = revisions.ShotFieldRevisionTests.revision_url
    detail = revisions.ShotFieldRevisionTests.detail
    field_payload = revisions.ShotFieldRevisionTests.field_payload
    write = revisions.ShotFieldRevisionTests.write

    def setUp(self):
        revisions.ShotFieldRevisionTests.setUp(self)
        media = TemporaryDirectory(); self.addCleanup(media.cleanup)
        setting = override_settings(MEDIA_ROOT=media.name, ASSET_LIBRARY_ROOT=media.name)
        setting.enable(); self.addCleanup(setting.disable)

    def frozen(self, detail, **changes):
        return read_frozen_shot_field_revision(**{
            "shot_id": self.shot_id, "project_id": self.project.pk, "max_bytes": 65536,
            "source_reference": {key: value for key, value in detail["source"].items() if key != "shot_id"},
            "field_revision": detail["revision"], **changes})

    def tone(self, detail):
        return next(row["original"] for row in detail["fields"] if row["original"].get("value") == "紧张")

    def test_original_null_and_old_revision_never_follow_latest(self):
        original = self.detail()
        first = self.write(self.field_payload("  手工一\n"))
        self.assertEqual(first.status_code, 201, first.data)
        second = self.write(self.field_payload("手工二"))
        self.assertEqual(second.status_code, 201, second.data)
        with CaptureQueriesContext(connection) as queries, patch(
                "studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError()):
            raw = self.frozen(original)
            old = self.frozen(first.data["detail"])
        self.assertTrue(no_dml(queries))
        self.assertFalse(any('"studio_fielddefinition' in row["sql"] or '"studio_datastructure' in row["sql"] for row in queries))
        self.assertEqual(select_shot_field(raw, selection(self.tone(raw)), max_bytes=65536)[0], "紧张")
        value, origin = select_shot_field(old, selection(self.tone(old)), max_bytes=65536)
        self.assertEqual(value, "  手工一\n")
        self.assertEqual(origin["field_revision"]["id"], first.data["detail"]["revision"]["id"])
        self.assertTrue(origin["overridden"])
        self.assertEqual(origin["field"]["field_definition"], self.tone(original)["field_definition"])
        self.assertNotIn("value", origin["field"])
        self.assertEqual(self.detail()["revision"]["revision"], 2)

    def test_foreign_damaged_and_incomplete_references_are_explicit_conflicts(self):
        detail = self.write(self.field_payload()).data["detail"]
        for reference in (None, {}, {**detail["source"], "document_sha256": "0" * 64}):
            with self.assertRaises(ValidationError): self.frozen(detail, source_reference=reference)
        for reference in ({}, {**detail["revision"], "id": str(uuid4())},
                {**detail["revision"], "revision": True}, {**detail["revision"], "content_sha256": "0" * 64}):
            with self.assertRaises(ValidationError): self.frozen(detail, field_revision=reference)
        with self.assertRaises(ValidationError): self.frozen(detail, project_id=uuid4())
        with connection.cursor() as cursor:
            cursor.execute('UPDATE studio_shotfieldrevision SET overrides = %s', ['[]'])
        with self.assertRaises(ValidationError): self.frozen(detail)

    def approve_storyboard(self):
        # The source fixture declares real reference requirements. Satisfy them
        # with disposable local PNGs through the shared selection writer; do
        # not remove requirements or bypass the formal review's readiness.
        from studio.tests import test_production_manual_storyboard_review as media
        from studio.services.shot_reference_choices import apply_shot_reference_choices, read_shot_reference_choices
        pending = ShotReferenceUse.objects.filter(shot__episode__project=self.project, source_mode="")
        for shot_id in list(pending.values_list("shot_id", flat=True).distinct()):
            asset = media.ManualStoryboardReferenceTests.asset(self)
            state = read_shot_reference_choices(actor=self.actor, project_id=self.project.pk, shot_id=shot_id)
            apply_shot_reference_choices(actor=self.actor, project_id=self.project.pk, shot_id=shot_id,
                expected_uses_sha256=state.sha256, choices=[{"use_id": str(use.pk), "source_mode": "existing",
                    "reference_version_id": None, "asset_id": str(asset.pk), "asset_sha256": asset.checksum}
                    for use in pending.filter(shot_id=shot_id)])
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-storyboard/")
        self.assertEqual(response.status_code, 200, response.data)
        current = response.data
        shot_ids = [row["id"] for episode in current["content"]["episodes"] for row in episode["shots"]]
        result = self.post({**self.body, "stage_key": "storyboard", "idempotency_key": str(uuid4()),
            "expected_selection_revision": current["adoption"]["revision"], "payload": {
                "expected_content_sha256": current["content_sha256"], "source": {"kind": "existing_storyboard"},
                "reviewed_shot_ids": shot_ids, "comment": "Isolated agent test adoption"}})
        self.assertEqual(result.status_code, 201, result.data)
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-storyboard-review/")
        self.assertEqual(response.status_code, 200, response.data)
        current = response.data
        self.assertTrue(current["ready"], current["reason"])
        result = self.post({**self.body, "action": "approve", "stage_key": "storyboard_review", "idempotency_key": str(uuid4()),
            "expected_selection_revision": current["approval"]["revision"], "payload": {
                "expected_contract_sha256": current["contract_sha256"], "reviewed_shot_ids": shot_ids,
                "comment": "Isolated agent test review"}})
        self.assertEqual(result.status_code, 201, result.data)
        self.review_stage = self.run.stages.get(stage_key="storyboard_review")
        return str(self.review_stage.approval_decisions.order_by("-revision").first().pk)

    def resolve_fields(self, decision, fields, **changes):
        self.run.refresh_from_db()
        return read_reviewed_shot_field_sources(**{"run": self.run,
            "scope": {"kind": "shot", "id": str(self.shot_id)}, "eligible_stage_ids": [self.review_stage.pk],
            "decision_id": decision, "selections": fields, "max_bytes": 65536, **changes})

    def test_real_review_reads_once_and_new_revision_requires_rereview(self):
        detail = self.write(self.field_payload("  已审核原文\n")).data["detail"]
        requested = selection(self.tone(detail))
        decision = self.approve_storyboard()
        before = list(ApprovalDecision.objects.values()), list(ShotFieldRevision.objects.values())
        with patch("studio.services.production_shot_field_sources.read_frozen_shot_field_revision",
                wraps=read_frozen_shot_field_revision) as reader, CaptureQueriesContext(connection) as queries:
            result = self.resolve_fields(decision, [requested, requested])
        self.assertEqual(reader.call_count, 1)
        self.assertTrue(no_dml(queries))
        self.assertEqual([row["value"] for row in result.value], ["  已审核原文\n"] * 2)
        self.assertTrue(all(row["origin"]["review"]["decision_id"] == decision for row in result.value))
        self.assertEqual((list(ApprovalDecision.objects.values()), list(ShotFieldRevision.objects.values())), before)
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())
        self.assertEqual(self.write(self.field_payload("后来未审核")).status_code, 201)
        with self.assertRaises(ValidationError): self.resolve_fields(decision, [requested])
        newer = self.approve_storyboard()
        with self.assertRaises(ValidationError): self.resolve_fields(decision, [requested])
        self.assertEqual(self.resolve_fields(newer, [requested]).value[0]["value"], "后来未审核")

    def test_review_scope_identity_and_budget_cannot_be_inferred(self):
        requested = selection(self.tone(self.detail()))
        decision = self.approve_storyboard()
        for changes in ({"eligible_stage_ids": []}, {"decision_id": None}, {"decision_id": str(uuid4())},
                {"scope": {"kind": "project", "id": str(self.project.pk)}},
                {"scope": {"kind": "shot", "id": str(uuid4())}}, {"selections": []}, {"max_bytes": 64}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.resolve_fields(decision, [requested], **changes)
        with self.assertRaisesMessage(ValidationError, "累计"):
            self.resolve_fields(decision, [requested] * 100, max_bytes=65536)

    def test_shared_strategy_composes_independent_raw_fields_only_by_its_rules(self):
        from studio.models import PromptProfileVersion
        from studio.services.prompt_profile_compiler import compile_prompt_profile_facts, PromptProfileCompilationFacts
        from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
        from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR
        from studio.tests.output_structure_fixtures import response_structure
        from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule

        detail = self.write(self.field_payload("  原始情绪\n")).data["detail"]
        tone = selection(self.tone(detail))
        motion = selection(next(row["original"] for row in detail["fields"] if row["original"].get("value") == "转身"))
        decision = self.approve_storyboard()
        resolved = self.resolve_fields(decision, [tone, motion])
        source = {"自定义.输入": {"emotion": resolved.value[0]["value"], "movement": resolved.value[1]["value"]}}
        original = deepcopy(source)
        structure = response_structure(example=source)
        fields = {key: create_field_definition(key="isolated_" + key, label=key, value_type="long_text")
            for key in ("emotion", "movement")}
        profiles = []
        for version, order in enumerate((("emotion", "movement"), ("movement", "emotion")), 1):
            profile = PromptProfileVersion.objects.create(profile_key="isolated-shot-fields", version=version,
                name="Isolated source composition", model_family="async_cloud", operation="video.generate", mode="T2VA",
                review_state="ready", input_structure_version=structure)
            for position, key in enumerate(order):
                create_profile_field_rule(profile_version=profile, field_definition=fields[key],
                    source_selector=STRUCTURED_INPUT_SELECTOR, generation_source_selector=STRUCTURED_INPUT_SELECTOR,
                    source_path=["自定义.输入", key], channel="positive_prompt", position=position,
                    label_style="none", separator="newline", required=True, max_chars=4000)
            refresh_prompt_profile_contract(profile)
            profiles.append(publish_prompt_profile(profile, actor=self.actor))
        facts = PromptProfileCompilationFacts(sources={STRUCTURED_INPUT_SELECTOR: source}, source_language="zh",
            target_language="zh", reference_context=None, reviewed_values={}, generation_contributions={},
            reviewed_fingerprint={"field_sources": [row["origin"] for row in resolved.value]})
        with CaptureQueriesContext(connection) as queries:
            first, second = [compile_prompt_profile_facts(profile, facts=facts) for profile in profiles]
        self.assertTrue(no_dml(queries))
        for result in (first, second):
            self.assertIn("原始情绪", result.assembly["channels"]["positive_prompt"])
            self.assertIn("转身", result.assembly["channels"]["positive_prompt"])
            self.assertEqual(set(result.structured_fields), {"isolated_emotion", "isolated_movement"})
        self.assertNotEqual(first.assembly["channels"], second.assembly["channels"])
        self.assertEqual(source, original)
        self.assertEqual(self.detail(), detail)
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())


class ShotFieldSelectionTypesTests(SimpleTestCase):
    def detail(self):
        from studio.tests.output_structure_fixtures import frozen_response_structure
        from studio.tests.test_storyboard_source_fields import document
        from studio.services.script_storyboard_fields import storyboard_source_fields
        from studio.tests.script_semantics_fixtures import each, literal, obj, path
        raw = {"frames": [{"number": 3, "zero": 0, "flag": False, "text": "", "nullable": "sample",
            "items": [], "object": {}, "optional": "sample", "defaulted": "sample", "lines": [{"text": "a"}, {"text": "b"}]}]}
        structure = frozen_response_structure(raw)
        for node in structure["nodes"]:
            if node["property_name"] == "nullable": node["allow_null"] = True
            if node["property_name"] == "optional": node["required"] = False
            if node["property_name"] == "defaulted": node.update(has_default=True, default_value=" configured ")
        raw["frames"][0]["nullable"] = None
        del raw["frames"][0]["optional"], raw["frames"][0]["defaulted"]
        mapping = {"schema_version": 1, "node": obj(entities=literal([]), episodes={"kind": "array", "items": [
            obj(episode=literal(8), shots=each(path("frames", scope="root"), obj(shot=path("number"))))]})}
        fields = storyboard_source_fields(document(raw, structure, mapping), max_bytes=65536)[8, 3]
        return _detail({"source": {"fixture": True}, "fields": fields}, structure, None, max_bytes=65536)

    def test_types_default_provenance_and_missing_are_not_coerced(self):
        detail = self.detail()
        before = deepcopy(detail)
        fields = {row["original"]["value_path"][-1]: row["original"] for row in detail["fields"]
            if len(row["original"]["value_path"]) == 3}
        for key, expected in (("zero", 0), ("flag", False), ("text", ""), ("nullable", None), ("items", []),
                ("object", {}), ("defaulted", " configured ")):
            value, origin = select_shot_field(detail, selection(fields[key]), max_bytes=65536)
            self.assertEqual(value, expected); self.assertIs(type(value), type(expected))
            self.assertFalse(origin["overridden"])
            self.assertEqual(bool(origin["field"]["default_paths"]), key == "defaulted")
        with self.assertRaisesMessage(ValidationError, "缺失"):
            select_shot_field(detail, selection(fields["optional"]), max_bytes=65536)
        self.assertEqual(detail, before)

    def test_exact_node_and_version_only_and_repeated_child_is_ambiguous(self):
        detail = self.detail()
        child = next(row["original"] for row in detail["fields"] if row["original"].get("value") == "a")
        with self.assertRaisesMessage(ValidationError, "出现多次"):
            select_shot_field(detail, selection(child), max_bytes=65536)
        parent = next(row["original"] for row in detail["fields"] if row["original"]["value_path"][-1] == "lines")
        self.assertEqual(select_shot_field(detail, selection(parent), max_bytes=65536)[0], [{"text": "a"}, {"text": "b"}])
        chosen = selection(parent)
        for changed in ({**chosen, "structure_version_id": str(uuid4())}, {**chosen, "node_key": "lines"},
                {**chosen, "field_definition_version_id": str(uuid4())}, {**chosen, "node_key": []},
                {**chosen, "path": [0]}, {**chosen, "structure_version_id": "latest"}):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                select_shot_field(detail, changed, max_bytes=65536)
