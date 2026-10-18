"""M6-05: real shot preparations, public review APIs and exact selection reuse.

Model/recipe rows and candidate text are explicit isolated fixtures, not model
outputs or root-production acceptance. No worker, finalization or dispatch runs.
The 269 generation and 270 final-assembly suites are deliberately not imported.
"""
from contextlib import ExitStack
from copy import deepcopy
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    Episode, ExecutionFieldValue, ExecutionTextResponseReview, GenerationAttempt,
    ModelDeploymentSmokeAttempt, PromptProfileVersion, SeriesProject, Shot,
    WorkflowSmokeAttempt,
)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_field_review import field_review_policy_options, production_field_review_contract
from studio.services.execution_instances import publish_execution_version, update_execution_draft
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_fields import read_execution_field_selection_snapshot, select_execution_field
from studio.services.execution_preparations import create_execution_instance_preparation, read_execution_preparation_detail
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.json_contracts import json_contract
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.tests import test_execution_auxiliary_definitions as composite_fixtures
from studio.tests import test_execution_field_review as review_fixtures
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule, publish_test_field_version
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class PromptFieldReviewContractTests(TestCase):
    setUpTestData = classmethod(composite_fixtures.ExecutionAuxiliaryDefinitionTests.setUpTestData.__func__)
    write_contract = staticmethod(composite_fixtures.ExecutionAuxiliaryDefinitionTests.write_contract)
    input = staticmethod(composite_fixtures.ExecutionAuxiliaryDefinitionTests.input)
    bind = composite_fixtures.ExecutionAuxiliaryDefinitionTests.bind
    stage = review_fixtures.ExecutionFieldReviewTests.stage

    @classmethod
    def setUpClass(cls):
        isolation = ExitStack()
        cls.addClassCleanup(isolation.close)
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            blocked = isolation.enter_context(patch(target, side_effect=AssertionError("No network in field review contract")))
            cls.addClassCleanup(blocked.assert_not_called)
        super().setUpClass()

    def setUp(self):
        self.assertEqual(settings.DATABASES["default"]["ENGINE"], "django.db.backends.sqlite3")
        self.assertIn("memory", str(settings.DATABASES["default"]["NAME"]))
        root = self.enterContext(TemporaryDirectory(prefix="prompt-field-review-"))
        self.enterContext(override_settings(MEDIA_ROOT=root))
        composite_fixtures.ExecutionAuxiliaryDefinitionTests.setUp(self)
        self.keys = ("m6_custom_direction", "m6_custom_sound", "m6_custom_rhythm")
        self.texts = {
            self.keys[0]: "  The courier opens the parcel.\nThen pauses beside it.  ",
            self.keys[1]: "原声：轻轻叩门。\n保留这句声音说明。",
            self.keys[2]: "Beat one：停顿；beat two：转身。",
        }
        self.languages = dict(zip(self.keys, ("English", "Chinese", "any"), strict=True))
        self.profile = PromptProfileVersion.objects.create(profile_key="m6-review-three-fields", version=1,
            name="Isolated three-field review", model_family="async_cloud", operation="video.generate",
            mode="T2VA", review_state="ready", contract_schema_version=8)
        self.definitions = {}
        for position, (key, policy) in enumerate(zip(self.keys, ("inherit", "preserve_source", "any"), strict=True), 1):
            field = create_field_definition(key=key, label=f"Exact {key}", value_type="long_text")
            self.definitions[key] = field
            create_profile_field_rule(profile_version=self.profile, field_definition=field,
                source_selector="request.text", generation_source_selector="request.action",
                generation_context_selectors=["project.style"], generation_mode="model_generated",
                generation_output_semantics="final", generation_instruction="Use only the declared source facts.",
                generation_required=True, language_policy=policy, channel="positive_prompt",
                position=position, required=True, max_chars=1000)
        refresh_prompt_profile_contract(self.profile)
        self.profile = publish_prompt_profile(self.profile, actor=self.actor)
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision,
            actor=self.actor, changes={"prompt_profile_version": self.profile})
        self.bind()
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.actor.is_staff = False
        self.actor.save(update_fields=("is_staff",))
        self.project = SeriesProject.objects.create(owner=self.actor, title="M6 review fixture", **PROJECT_CONFIG)
        self.episode = Episode.objects.create(project=self.project, number=1, title="Isolated shots")
        option = field_review_policy_options()[0]
        self.choice = {"mode": "policy", "policy": {key: option[key] for key in ("key", "version", "implementation_sha256")}}
        self.manual_stage = self.stage(source="manual")
        self.preps = []
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.preparation_url = "/api/v1/execution-preparations/"
        self.field_url = "/api/v1/execution-field-values/"
        self.addCleanup(self.assert_no_execution)

    def assert_no_execution(self):
        # Also pins the shared retained-response reader to its unchanged empty branch.
        for model in (GenerationAttempt, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt, ExecutionTextResponseReview):
            self.assertFalse(model.objects.exists(), model.__name__)
        for prep in self.preps:
            prep.refresh_from_db()
            self.assertIsNone(prep.request_frozen_at)
            self.assertIsNone(prep.request_sha256)
            stage = prep.stage_run
            self.assertEqual(stage.status, "running")
            self.assertFalse(stage.approval_decisions.exists())

    def revision(self, prep):
        prep.refresh_from_db()
        return prep.revision

    def prepare(self, number=1, *, stage=None, shot=None, request_key="candidate-1", source=None):
        stage = stage or self.manual_stage
        shot = shot or Shot.objects.create(episode=self.episode, number=number, title="Review only",
            duration_ms=3000, media_type="video", include_audio=True)
        unit = f"shot:{shot.pk}"
        request = {"inputs": {
            "request.action": {"kind": "literal", "value": source or "快递员打开包裹，停顿后转身。"},
            "style": {"kind": "literal", "value": {"lighting": "soft daylight"}},
        }, "parameters": {"width": 768, "height": 1344, "fps": 24, "duration_seconds": 3},
            "manual_channels": {}, "subject_facts": [], "source_language": "Chinese", "target_language": "English"}

        def resolve(definition, owner):
            return resolve_execution_request_facts(definition, request, owner=self.actor, unit_key=unit,
                max_media_bytes=65536, review_contract=production_field_review_contract(owner),
                parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No undeclared parameter source"))

        prep = create_execution_instance_preparation(owner_ref=("stage_run_id", stage.pk), version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, unit_key=unit, request_key=request_key,
            intent=json_contract(request), resolve_facts=resolve, actor=self.actor)
        self.preps.append(prep)
        return prep

    def add(self, prep, key, *, revision=1, text=None):
        response = self.client.post(self.field_url, {"preparation_id": str(prep.pk), "unit_key": prep.unit_key,
            "field_key": key, "text": self.texts[key] if text is None else text, "revision": revision,
            "expected_preparation_revision": self.revision(prep)}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        value = response.json()["field"]
        self.assertEqual((value["origin_kind"], value["status"]), ("manual", "pending"))
        return value

    def fill(self, prep):
        return {key: self.add(prep, key) for key in self.keys}

    def detail(self, prep):
        response = self.client.get(f"{self.preparation_url}{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()

    def preview(self, preps):
        response = self.client.post(self.preparation_url + "bulk-field-preview/",
            {"preparation_ids": [str(prep.pk) for prep in preps]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        return response.json()["items"]

    def receipts(self, preps):
        return [{key: row[key] for key in ("preparation_id", "expected_preparation_revision", "preview_sha256")}
            for row in self.preview(preps)]

    def bulk_confirm(self, receipts):
        return self.client.post(self.preparation_url + "bulk-field-confirm/", {"items": receipts}, format="json")

    def confirm(self, prep, value, **overrides):
        return self.client.post(f"{self.field_url}{value['id']}/confirm/", {
            "expected_preparation_revision": self.revision(prep), "expected_text_sha256": value["text_sha256"],
            "expected_source_sha256": value["source_sha256"], "select_for_execution": True,
            "expected_selection_revision": None, "replace": False, **overrides,
        }, format="json")

    def snapshot(self, prep):
        return read_execution_field_selection_snapshot(preparation_id=prep.pk,
            expected_preparation_revision=self.revision(prep), actor=self.actor).value

    def audit(self, prep):
        return {"revision": self.revision(prep), "values": list(prep.field_values.order_by("id").values()),
            "selections": list(prep.field_selections.order_by("id").values())}

    def assert_conflict(self, response, message):
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.json()["error"]["code"], "execution_preparation_conflict")
        self.assertIn(message, str(response.json()["error"]["details"]))

    def evaluate(self, prep):
        return evaluate_preparation_fields(preparation_id=prep.pk,
            expected_preparation_revision=self.revision(prep), actor=self.actor)

    def test_single_shot_one_click_uses_exact_custom_field_versions_text_and_languages(self):
        prep = self.prepare()
        values = self.fill(prep)
        for field in self.definitions.values():
            publish_test_field_version(field.pk, {"label": "Later catalog label must not replace frozen label",
                "category": "test", "value_type": "long_text"})
        before = self.audit(prep)
        rows = self.preview([prep])
        slots = {row["field_key"]: row for row in self.detail(prep)["field_slots"]}
        self.assertEqual(self.audit(prep), before)
        self.assertTrue(rows[0]["ready"])
        for row in rows[0]["fields"]:
            key = row["field_key"]
            self.assertEqual((row["value_id"], row["revision"], row["text"]), (values[key]["id"], 1, self.texts[key]))
            self.assertEqual(row["label"], f"Exact {key}")
            self.assertEqual(slots[key]["effective_language"], self.languages[key])
            self.assertEqual(slots[key]["field_contract"]["field_definition"]["version"], 1)
        result = self.bulk_confirm(self.receipts([prep]))
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.json()["confirmed_field_count"], 3)
        snapshot = self.snapshot(prep)
        self.assertEqual(snapshot["unselected_fields"], [])
        for row in snapshot["fields"]:
            self.assertEqual(row["field_value_id"], values[row["field_key"]]["id"])
            self.assertEqual(row["text"], self.texts[row["field_key"]])
            self.assertEqual((row["approval"]["kind"], row["approval"]["reviewed_by"]), ("human", str(self.actor.pk)))

    def test_multi_shot_batch_confirms_only_listed_exact_versions_without_touching_other_shot(self):
        preps = [self.prepare(number) for number in (1, 2, 3)]
        values = {prep.pk: self.fill(prep) for prep in preps}
        untouched = self.audit(preps[1])
        listed = [preps[2], preps[0]]
        receipts = self.receipts(listed)
        self.assertEqual([row["preparation_id"] for row in receipts], [str(prep.pk) for prep in listed])
        response = self.bulk_confirm(receipts)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["confirmed_field_count"], 6)
        for prep in listed:
            self.assertEqual({row["field_value_id"] for row in self.snapshot(prep)["fields"]},
                {row["id"] for row in values[prep.pk].values()})
        self.assertEqual(self.audit(preps[1]), untouched)
        approved = [self.audit(prep) for prep in listed]
        self.assert_conflict(self.bulk_confirm(receipts), "已更新")
        self.assertEqual([self.audit(prep) for prep in listed], approved)

    def test_mixed_batch_preserves_adoption_and_only_explicit_current_selection_revision_replaces_it(self):
        prep = self.prepare()
        values = self.fill(prep)
        key = self.keys[0]
        first = self.confirm(prep, values[key])
        self.assertEqual(first.status_code, 200, first.data)
        chosen = deepcopy(first.json()["selection"])
        old_audit = ExecutionFieldValue.objects.values().get(pk=values[key]["id"])
        later = self.add(prep, key, revision=2, text="A later candidate watches the parcel.")
        preview = self.preview([prep])[0]
        self.assertTrue(preview["ready"])
        selected = next(row for row in preview["fields"] if row["field_key"] == key)
        self.assertEqual((selected["state"], selected["value_id"]), ("selected", values[key]["id"]))
        response = self.bulk_confirm(self.receipts([prep]))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["confirmed_field_count"], 2)
        self.assertEqual(ExecutionFieldValue.objects.values().get(pk=values[key]["id"]), old_audit)
        self.assertEqual(next(row for row in self.detail(prep)["field_selections"] if row["field_key"] == key), chosen)
        history = self.client.get(self.field_url, {"preparation_id": str(prep.pk)})
        self.assertEqual(history.status_code, 200, history.data)
        versions = [row for row in history.json()["results"] if row["field_key"] == key]
        self.assertEqual([(row["id"], row["revision"], row["status"]) for row in versions],
            [(later["id"], 2, "pending"), (values[key]["id"], 1, "confirmed")])
        unchanged = self.audit(prep)
        self.assert_conflict(self.confirm(prep, later), "必须明确替换")
        self.assertEqual(self.audit(prep), unchanged)  # Failed selection also rolls back its attempted approval.
        self.assert_conflict(self.confirm(prep, later, replace=True, expected_selection_revision=2), "已更新")
        self.assertEqual(self.audit(prep), unchanged)
        replaced = self.confirm(prep, later, replace=True, expected_selection_revision=chosen["revision"])
        self.assertEqual(replaced.status_code, 200, replaced.data)
        self.assertEqual((replaced.json()["selection"]["id"], replaced.json()["selection"]["revision"]), (chosen["id"], 2))
        self.assertEqual(replaced.json()["selection"]["field_value"], later["id"])
        self.assertEqual(ExecutionFieldValue.objects.values().get(pk=values[key]["id"]), old_audit)

    def test_stale_batch_and_ambiguous_revision_fail_atomically_then_explicit_older_choice_recovers(self):
        preps = [self.prepare(number) for number in (1, 2)]
        first, second = [self.fill(prep) for prep in preps]
        receipts = self.receipts(preps)
        key = self.keys[0]
        later = self.add(preps[1], key, revision=2, text="A different pending candidate.")
        unchanged = [self.audit(prep) for prep in preps]
        self.assert_conflict(self.bulk_confirm(receipts), "已更新")
        self.assertEqual([self.audit(prep) for prep in preps], unchanged)
        blocked = self.preview(preps)[1]
        self.assertFalse(blocked["ready"])
        ambiguous = next(row for row in blocked["fields"] if row["field_key"] == key)
        self.assertIn("多个可选修订", ambiguous["reason"])
        self.assertIsNone(ambiguous["value_id"])
        self.assert_conflict(self.bulk_confirm(self.receipts(preps)), "本批未采用任何字段")
        self.assertEqual([self.audit(prep) for prep in preps], unchanged)
        choice = self.confirm(preps[1], second[key])
        self.assertEqual(choice.status_code, 200, choice.data)
        response = self.bulk_confirm(self.receipts(preps))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["confirmed_field_count"], 5)
        self.assertEqual({row["field_value_id"] for row in self.snapshot(preps[0])["fields"]}, {row["id"] for row in first.values()})
        self.assertEqual({row["field_value_id"] for row in self.snapshot(preps[1])["fields"]}, {row["id"] for row in second.values()})
        self.assertEqual(ExecutionFieldValue.objects.get(pk=later["id"]).status, "pending")

    def test_exact_reuse_links_original_versions_without_copy_or_reapproval_and_rejects_source_drift(self):
        original = self.prepare()
        values = self.fill(original)
        response = self.bulk_confirm(self.receipts([original]))
        self.assertEqual(response.status_code, 200, response.data)
        source_audit = self.audit(original)
        shot = Shot.objects.get(pk=original.unit_key[5:])
        reused = self.prepare(shot=shot, request_key="candidate-2")
        for value in values.values():
            select_execution_field(preparation_id=reused.pk, source_preparation_id=original.pk, field_value_id=value["id"],
                expected_preparation_revision=self.revision(reused), expected_selection_revision=None, replace=False, actor=self.actor)
        self.assertFalse(reused.field_values.exists())
        self.assertEqual(self.audit(original), source_audit)
        snapshot = self.snapshot(reused)
        self.assertEqual(snapshot["unselected_fields"], [])
        for row in snapshot["fields"]:
            self.assertEqual(row["source_preparation_id"], str(original.pk))
            self.assertEqual(row["field_value_id"], values[row["field_key"]]["id"])
        later = self.add(original, self.keys[0], revision=2, text="Another unadopted source revision.")
        self.assertEqual(self.snapshot(reused), snapshot)
        self.assertNotIn(later["id"], {row["field_value_id"] for row in self.snapshot(reused)["fields"]})
        history = self.client.get(self.field_url, {"preparation_id": str(reused.pk)})
        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual(history.json()["count"], 0)
        self.assertEqual({row["field_value"] for row in self.detail(reused)["field_selections"]}, {row["id"] for row in values.values()})
        changed = self.prepare(shot=shot, request_key="candidate-3", source="快递员不再打开包裹，而是走出房间。")
        unchanged = self.audit(changed)
        with self.assertRaisesMessage(ValidationError, "字段来源或规则") as caught:
            select_execution_field(preparation_id=changed.pk, source_preparation_id=original.pk,
                field_value_id=values[self.keys[0]]["id"], expected_preparation_revision=self.revision(changed),
                expected_selection_revision=None, replace=False, actor=self.actor)
        self.assertEqual(caught.exception.code, "execution_preparation_conflict")
        self.assertEqual(self.audit(changed), unchanged)

    def test_authorized_policy_waits_for_complete_fields_then_selects_all_without_human_approval(self):
        prep = self.prepare(stage=self.stage())
        for key in self.keys[:2]:
            self.add(prep, key)
        evaluation = self.evaluate(prep)
        self.assertEqual(evaluation.status, "awaiting_input")
        self.assertFalse(prep.field_selections.exists())
        self.assertEqual(set(prep.field_values.values_list("status", flat=True)), {"pending"})
        self.add(prep, self.keys[2])
        evaluation = self.evaluate(prep)
        self.assertEqual(evaluation.status, "preparing")
        self.assertEqual(read_execution_preparation_detail(evaluation).evaluation["outstanding_fields"], [])
        self.assertEqual(prep.field_selections.count(), 3)
        for row in self.snapshot(prep)["fields"]:
            self.assertEqual(row["approval"]["kind"], "policy")
            self.assertIsNone(row["approval"]["reviewed_by"])
            self.assertEqual(row["approval"]["policy_sha256"], json_contract(self.choice["policy"]).sha256)
        detail = self.detail(prep)
        self.assertEqual(detail["review_summary"]["mode"], "policy")
        self.assertEqual(detail["review_contract"]["policy"], self.choice["policy"])
        old_values = list(prep.field_values.order_by("id").values())
        old_selections = list(prep.field_selections.order_by("id").values())
        later = self.add(prep, self.keys[0], revision=2, text="A new candidate still awaits an explicit selection.")
        self.assertEqual(self.evaluate(prep).status, "preparing")
        self.assertEqual(list(prep.field_selections.order_by("id").values()), old_selections)
        self.assertEqual(list(prep.field_values.filter(revision=1).order_by("id").values()), old_values)
        self.assertEqual(ExecutionFieldValue.objects.get(pk=later["id"]).status, "pending")
        stable_revision = self.revision(prep)
        self.assertEqual(self.evaluate(prep).revision, stable_revision)

    def test_policy_ambiguity_requires_explicit_version_not_per_field_human_review(self):
        prep = self.prepare(stage=self.stage())
        values = self.fill(prep)
        self.add(prep, self.keys[0], revision=2, text="A second candidate cannot win by recency.")
        unchanged = self.audit(prep)
        with self.assertRaisesMessage(ValidationError, "不会自动选择最新版本") as caught:
            self.evaluate(prep)
        self.assertEqual(caught.exception.code, "field_selection_conflict")
        self.assertEqual(self.audit(prep), unchanged)
        response = self.confirm(prep, values[self.keys[0]])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.evaluate(prep).status, "preparing")
        approved = {row["field_key"]: row["approval"]["kind"] for row in self.snapshot(prep)["fields"]}
        self.assertEqual(approved, {self.keys[0]: "human", self.keys[1]: "policy", self.keys[2]: "policy"})
        manual = self.prepare(number=2)
        self.fill(manual)
        self.assertEqual(self.evaluate(manual).status, "awaiting_review")
        self.assertFalse(manual.field_selections.exists())
        self.assertEqual(set(manual.field_values.values_list("status", flat=True)), {"pending"})
