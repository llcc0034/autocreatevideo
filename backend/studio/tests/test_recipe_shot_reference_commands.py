"""Recipe human choices via the original API/receipt; isolated files, no calls.

The M4 fixture has real original fulfillment/review receipts. Storyboard and
media stages are declared fixtures, not full recipe admission or live media
acceptance. The media validator remains the application's recording boundary.
"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ApprovalDecision, Episode, ExecutionPreparation, GenerationAttempt, ProductionManualCommand, Shot
from studio.services.production_runs import _save_stage_snapshot
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_shot_reference_application as application
from studio.tests import test_shot_reference_sources as media_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeShotReferenceCommandTests(StructureFixture):
    Application = application.RecipeShotReferenceApplicationTests
    Fixture = Application.Fixture
    extra_before_media_declarations = Application.extra_before_media_declarations
    tearDown = Application.tearDown
    facts = Application.facts
    attach = Application.attach
    choose = Application.choose
    create_job = Application.create_job
    primary_version = Application.primary_version
    write = Application.write
    input = staticmethod(Application.input)
    configure_variant_sources = Application.configure_variant_sources
    configure_declaration = Application.configure_declaration
    create_prior_media_stages = Application.create_prior_media_stages
    target = Application.target
    prepare = Application.prepare
    complete = Application.complete
    use = Application.use
    media = media_fixtures.ShotReferenceSourceTests.media

    def freeze_source(self):
        # Declare batch uses before the original storyboard evidence is frozen.
        # Adding them after admission must remain a creative-drift conflict.
        if self._testMethodName in {
                "test_batch_existing_preserves_multiple_uses_and_unselected_shot",
                "test_batch_existing_bad_use_rolls_back_its_shot_not_sibling"}:
            self.batch_extra = self.use(shot=self.shots[0], setting=self.settings[0], key="another-use", position=1)
        if self._testMethodName == "test_batch_existing_preserves_multiple_uses_and_unselected_shot":
            self.batch_untouched = self.use(shot=self.shots[0], setting=self.settings[0], key="unchecked-use", position=2)
        self.Application.freeze_source(self)

    def setUp(self):
        self.review_declaration = deepcopy(self.Application.review_declaration)
        upload_tests = {"test_upload_requires_real_upload_and_matching_frozen_source",
            "test_no_override_allows_first_selection_and_unchanged_choice_but_not_replacement",
            "test_allow_override_replaces_only_its_use_without_approval",
            "test_replay_confirms_original_receipt_without_rewriting_later_choice_or_closed_run"}
        if self._testMethodName in upload_tests:
            self.review_declaration["shot_reference_selection"].update(source="upload", source_stage_key=None, selection="manual")
        if "existing" in self._testMethodName:
            self.review_declaration["shot_reference_selection"].update(source="existing", source_stage_key=None, selection="manual")
        if any(word in self._testMethodName for word in ("allow_override", "replay")):
            self.review_declaration["shot_reference_selection"]["allow_override"] = True
        self.Application.setUp(self)
        self.owner = self.user
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.detail_url = f"/api/v1/production-runs/{self.run.pk}/recipe-shot-references/"
        self.action_url = f"/api/v1/production-runs/{self.run.pk}/manual-actions/"
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("No external call")))

    def detail(self, index=0):
        result = self.client.get(self.detail_url, {"shot_id": str(self.shots[index].pk)})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result["Cache-Control"], "private, no-store")
        return result.data

    def body(self, *, index=0, key="choose", choice=None):
        detail = self.detail(index)
        if choice is None:
            candidate = next(row for row in detail["upstream_candidates"] if row["setting_revision_id"] == str(self.settings[index].pk))
            choice = {"use_id": str(self.uses[index].pk), "source_mode": candidate["source_mode"],
                "reference_version_id": candidate["reference_version_id"], "asset_id": None,
                "asset_sha256": candidate["asset_sha256"]}
        return {"action": "select", "stage_key": "storyboard_review", "unit_key": f"shot:{self.shots[index].pk}",
            "scope": {"kind": "shot", "id": str(self.shots[index].pk)}, "expected_run_plan_sha256": detail["run_plan_sha256"],
            "expected_selection_revision": None, "idempotency_key": key,
            "payload": {"expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
                "expected_uses_sha256": detail["uses_sha256"], "choices": [choice]}}

    def asset_choice(self, asset, *, index=0, mode="upload"):
        return {"use_id": str(self.uses[index].pk), "source_mode": mode, "reference_version_id": None,
            "asset_id": str(asset.pk), "asset_sha256": asset.checksum}

    def post(self, body):
        return self.client.post(self.action_url, body, format="json")

    def test_read_is_metadata_only_and_not_manual_adoption_or_file_evidence(self):
        with CaptureQueriesContext(connection) as queries, patch(
                "studio.services.shot_reference_sources.resolve_execution_asset_media", side_effect=AssertionError("No file probe")), patch(
                "studio.services.production_material_references.read_material_result_reference", side_effect=AssertionError("No result probe")):
            detail = self.detail()
        self.assertTrue(detail["editability"]["allowed"])
        self.assertEqual(detail["selection_rule"]["rule"], self.review_declaration["shot_reference_selection"])
        self.assertEqual(len(detail["upstream_candidates"]), 3)
        self.assertNotIn("storyboard_adoption_revision", detail)
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])

    def test_upstream_choice_only_writes_original_use_and_human_command(self):
        stages = list(self.run.stages.values())
        approvals = list(ApprovalDecision.objects.values())
        source_sha = self.source.output_snapshot_sha256
        primary = self.character.anchor_asset_id
        result = self.post(self.body())
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual((result.data["result_kind"], str(result.data["target_id"])), ("shot_reference_choices", str(self.shots[0].pk)))
        receipt = ProductionManualCommand.objects.get(pk=result.data["id"])
        self.assertEqual(receipt.created_by_id, self.user.pk)
        self.uses[0].refresh_from_db(); self.uses[1].refresh_from_db()
        self.assertEqual(self.uses[0].reference_version_id, self.fulfillments[1].reference_version_id)
        self.assertEqual(self.uses[0].source_mode, "existing")
        self.assertIsNone(self.uses[1].reference_version_id)
        self.source.refresh_from_db(); self.character.refresh_from_db()
        self.assertEqual(self.source.output_snapshot_sha256, source_sha)
        self.assertEqual(self.character.anchor_asset_id, primary)
        self.assertEqual(stages, list(self.run.stages.values()))
        self.assertEqual(approvals, list(ApprovalDecision.objects.values()))
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())

    def test_upstream_rejects_catalog_external_version_direct_asset_and_forged_origin(self):
        original = self.body()
        bad = deepcopy(original); bad["payload"]["choices"][0]["reference_version_id"] = str(uuid4())
        direct = self.body(choice=self.asset_choice(self.media(), mode="existing"))
        origin = deepcopy(original); origin["payload"]["choices"][0]["source_mode"] = "generated"
        for body in (bad, direct, origin):
            self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(ProductionManualCommand.objects.filter(run=self.run).exists())

    def test_upload_requires_real_upload_and_matching_frozen_source(self):
        asset = self.media()
        self.assertEqual(self.detail()["upstream_candidates"], [])
        wrong = self.post(self.body(choice=self.asset_choice(asset, mode="existing")))
        self.assertEqual(wrong.status_code, 400, wrong.data)
        asset.source_info = "not an upload"; asset.save(update_fields=("source_info",))
        self.assertEqual(self.post(self.body(choice=self.asset_choice(asset))).status_code, 400)
        valid = self.media()
        self.assertEqual(self.post(self.body(choice=self.asset_choice(valid))).status_code, 201)

    def test_existing_reuses_exact_version_without_relabeling_acquisition(self):
        # The command's reuse mode never rewrites the version's acquisition
        # provenance. These M4 fixtures fulfilled existing library versions.
        version = self.fulfillments[1].reference_version
        choice = {"use_id": str(self.uses[0].pk), "source_mode": "existing", "reference_version_id": str(version.pk),
            "asset_id": None, "asset_sha256": version.asset.checksum}
        before = version.source
        result = self.post(self.body(choice=choice))
        self.assertEqual(result.status_code, 201, result.data)
        version.refresh_from_db()
        self.assertEqual(version.source, before)
        wrong = deepcopy(choice); wrong["source_mode"] = "generated"
        self.assertEqual(self.post(self.body(key="wrong-mode", choice=wrong)).status_code, 400)

    def test_no_override_allows_first_selection_and_unchanged_choice_but_not_replacement(self):
        first = self.asset_choice(self.media())
        self.assertEqual(self.post(self.body(choice=first)).status_code, 201)
        self.assertEqual(self.post(self.body(key="same", choice=first)).status_code, 201)
        result = self.post(self.body(key="replace", choice=self.asset_choice(self.media())))
        self.assertEqual(result.status_code, 400, result.data)
        self.assertIn("不允许替换", str(result.data))
        # A sibling use remains independently selectable, even though override
        # is prohibited for the already chosen first shot.
        self.assertEqual(self.post(self.body(index=1, key="sibling", choice=self.asset_choice(self.media(), index=1))).status_code, 201)

    def test_allow_override_replaces_only_its_use_without_approval(self):
        original = self.asset_choice(self.media())
        next_choice = self.asset_choice(self.media())
        self.assertEqual(self.post(self.body(choice=original)).status_code, 201)
        result = self.post(self.body(key="replace", choice=next_choice))
        self.assertEqual(result.status_code, 201, result.data)
        self.uses[0].refresh_from_db(); self.uses[1].refresh_from_db()
        self.assertEqual(str(self.uses[0].asset_id), next_choice["asset_id"])
        self.assertIsNone(self.uses[1].asset_id)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())

    def test_replay_confirms_original_receipt_without_rewriting_later_choice_or_closed_run(self):
        first = self.body(choice=self.asset_choice(self.media()))
        result = self.post(first)
        self.assertEqual(result.status_code, 201, result.data)
        replacement = self.body(key="replace", choice=self.asset_choice(self.media()))
        self.assertEqual(self.post(replacement).status_code, 201)
        type(self.run).objects.filter(pk=self.run.pk).update(status="cancelled")
        with patch("studio.services.production_recipe_shot_reference_commands.select_recipe_shot_references", side_effect=AssertionError("No replay write")):
            replay = self.post(first)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data, result.data)
        self.uses[0].refresh_from_db()
        self.assertEqual(str(self.uses[0].asset_id), replacement["payload"]["choices"][0]["asset_id"])
        changed = deepcopy(first); changed["payload"]["choices"][0]["asset_sha256"] = "0" * 64
        self.assertEqual(self.post(changed).status_code, 409)
        self.assertEqual(self.post({**first, "idempotency_key": "new"}).status_code, 400)

    def test_receipt_failure_rolls_back_choice(self):
        body = self.body()
        baseline = self.detail()["uses_sha256"]
        with patch.object(ProductionManualCommand, "save", side_effect=RuntimeError("isolated receipt failure")):
            with self.assertRaisesRegex(RuntimeError, "receipt failure"):
                self.post(body)
        self.assertEqual(self.detail()["uses_sha256"], baseline)
        self.assertFalse(ProductionManualCommand.objects.filter(run=self.run).exists())

    def test_batch_existing_preserves_multiple_uses_and_unselected_shot(self):
        extra, untouched = self.batch_extra, self.batch_untouched
        before = self.detail(1)
        asset = self.media()
        body = self.body(choice=self.asset_choice(asset, mode="existing"))
        body["payload"]["choices"].append({**body["payload"]["choices"][0], "use_id": str(extra.pk)})
        response = self.post(body)
        self.assertEqual(response.status_code, 201, response.data)
        self.uses[0].refresh_from_db(); extra.refresh_from_db(); untouched.refresh_from_db()
        self.assertEqual(self.uses[0].asset_id, asset.pk)
        self.assertEqual(extra.asset_id, asset.pk)
        self.assertNotEqual(self.uses[0].use_key, extra.use_key)
        self.assertIsNone(untouched.asset_id)
        self.assertEqual(self.detail(1), before)
        replay = self.post(body)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(response.data, replay.data)
        self.assertEqual(ProductionManualCommand.objects.filter(run=self.run).count(), 1)
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())

    def test_batch_existing_conflict_does_not_rollback_independent_shot(self):
        first = self.body(key="batch-1", choice=self.asset_choice(self.media(), mode="existing"))
        second = self.body(index=1, key="batch-2", choice=self.asset_choice(self.media(), index=1, mode="existing"))
        primary = self.character.anchor_asset_id
        concurrent = self.body(key="other-edit", choice=self.asset_choice(self.media(), mode="existing"))
        self.assertEqual(self.post(concurrent).status_code, 201)
        conflict = self.post(first)
        self.assertEqual(conflict.status_code, 409, conflict.data)
        succeeded = self.post(second)
        self.assertEqual(succeeded.status_code, 201, succeeded.data)
        self.uses[0].refresh_from_db(); self.uses[1].refresh_from_db(); self.character.refresh_from_db()
        self.assertEqual(str(self.uses[0].asset_id), concurrent["payload"]["choices"][0]["asset_id"])
        self.assertEqual(str(self.uses[1].asset_id), second["payload"]["choices"][0]["asset_id"])
        self.assertEqual(self.character.anchor_asset_id, primary)
        self.assertEqual(ProductionManualCommand.objects.filter(run=self.run).count(), 2)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists() or GenerationAttempt.objects.exists())

    def test_batch_existing_bad_use_rolls_back_its_shot_not_sibling(self):
        extra = self.batch_extra
        first = self.body(key="batch-invalid", choice=self.asset_choice(self.media(), mode="existing"))
        first["payload"]["choices"].append({**first["payload"]["choices"][0], "use_id": str(extra.pk), "asset_sha256": "0" * 64})
        second = self.body(index=1, key="batch-valid", choice=self.asset_choice(self.media(), index=1, mode="existing"))
        before = self.detail()["uses_sha256"]
        failed = self.post(first)
        self.assertEqual(failed.status_code, 400, failed.data)
        self.assertEqual(self.detail()["uses_sha256"], before)
        succeeded = self.post(second)
        self.assertEqual(succeeded.status_code, 201, succeeded.data)
        self.assertEqual(ProductionManualCommand.objects.filter(run=self.run).count(), 1)

    def test_stale_sha_scope_permission_and_fake_adoption_are_rejected(self):
        original = self.body()
        cases = []
        for field, value in (("expected_source_snapshot_sha256", "0" * 64), ("expected_uses_sha256", "0" * 64), ("allow_override", True)):
            bad = deepcopy(original); bad["payload"][field] = value; cases.append(bad)
        for field, value in (("expected_selection_revision", 1), ("unit_key", "untrusted"), ("action", "approve")):
            cases.append({**original, field: value})
        bad = deepcopy(original); bad["payload"]["choices"][0]["use_id"] = str(self.uses[1].pk); cases.append(bad)
        for body in cases:
            self.assertIn(self.post(body).status_code, (400, 409))
        self.assertFalse(ProductionManualCommand.objects.filter(run=self.run).exists())

    def test_upstream_and_creative_drift_are_not_refrozen(self):
        body = self.body()
        source_sha = self.source.output_snapshot_sha256
        _save_stage_snapshot(self.stage, "output_snapshot", {**self.stage.output_snapshot, "changed": True})
        self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(ProductionManualCommand.objects.filter(run=self.run).exists())
        self.source.refresh_from_db()
        self.assertEqual(self.source.output_snapshot_sha256, source_sha)

    def test_creative_drift_and_completed_review_block_new_choices(self):
        body = self.body()
        Shot.objects.filter(pk=self.shots[0].pk).update(title="changed original creative field")
        self.assertEqual(self.post(body).status_code, 409)
        Shot.objects.filter(pk=self.shots[0].pk).update(title=self.shots[0].title)
        self.review.status = "succeeded"; self.review.save(update_fields=("status",))
        self.assertEqual(self.detail()["editability"]["code"], "review_not_current")
        self.assertEqual(self.post(body).status_code, 400)

    def test_forged_stage_input_cannot_grant_new_source_or_override_permission(self):
        body = self.body()
        changed = deepcopy(self.review.input_snapshot)
        changed["shot_reference_selection"]["rule"]["allow_override"] = True
        _save_stage_snapshot(self.review, "input_snapshot", changed)
        self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(ProductionManualCommand.objects.filter(run=self.run).exists())

    def test_existing_preparation_only_locks_its_shot(self):
        version = self.fulfillments[1].reference_version
        choice = {"use_id": str(self.uses[0].pk), "source_mode": "existing", "reference_version_id": str(version.pk),
            "asset_id": None, "asset_sha256": version.asset.checksum}
        body = self.body(choice=choice)
        ExecutionPreparation.objects.create(stage_run=self.media_stage, execution_instance_version=self.version,
            purpose="production", unit_key=f"shot:{self.shots[0].pk}", request_key="isolated-existing",
            created_by=self.user, status="awaiting_input", intent_sha256="a" * 64)
        self.assertEqual(self.detail()["editability"]["code"], "shot_prepared")
        self.assertEqual(self.post(body).status_code, 400)
        self.assertTrue(self.detail(1)["editability"]["allowed"])

    def test_missing_upstream_file_rolls_back_without_receipt(self):
        body = self.body()
        Path(self.fulfillments[1].reference_version.asset.file.path).unlink()  # Disposable isolated fixture only.
        result = self.post(body)
        self.assertEqual(result.status_code, 400, result.data)
        self.assertFalse(ProductionManualCommand.objects.filter(run=self.run).exists())
        self.uses[0].refresh_from_db()
        self.assertIsNone(self.uses[0].reference_version_id)

    def test_query_owner_and_frozen_storyboard_membership_are_enforced(self):
        for query in ("", "?shot_id=bad", f"?shot_id={self.shots[0].pk}&shot_id={self.shots[1].pk}",
                f"?shot_id={self.shots[0].pk}&allow_override=true"):
            self.assertEqual(self.client.get(self.detail_url + query).status_code, 400)
        body = self.body()
        episode = Episode.objects.create(project=self.project, number=92, title="Outside frozen storyboard")
        outside = Shot.objects.create(episode=episode, number=1, duration_ms=1000, media_type="image", include_audio=False)
        self.assertEqual(self.client.get(self.detail_url, {"shot_id": str(outside.pk)}).status_code, 400)
        other = get_user_model().objects.create_user(username=f"other-{uuid4()}")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.detail_url, {"shot_id": str(self.shots[0].pk)}).status_code, 404)
        self.assertEqual(self.post(body).status_code, 404)


class RecipeShotReferenceApiShapeTests(SimpleTestCase):
    def test_source_conflict_identifies_normalized_command_and_saved_row_shots(self):
        from studio.services.production_recipe_shot_references import (
            RecipeShotReferenceSourceConflict, validate_recipe_shot_reference_sources,
        )
        shot_id = str(uuid4())
        choice = {"source_mode": "upload", "reference_version_id": None,
            "asset_id": uuid4(), "asset_sha256": "a" * 64}
        for source in ("upstream", "existing"):
            for saved in (False, True):
                with self.subTest(source=source, saved=saved):
                    with self.assertRaises(RecipeShotReferenceSourceConflict) as caught:
                        validate_recipe_shot_reference_sources(stage=None, configured={"rule": {"source": source}},
                            catalog=[], choices=[{**choice, **({"shot_id": shot_id} if saved else {})}],
                            shot_id=None if saved else shot_id)
                    self.assertEqual(caught.exception.shot_id, shot_id)
                    self.assertEqual(caught.exception.code, "shot_reference_source_conflict")

    def test_openapi_declares_recipe_context_and_no_fabricated_adoption_revision(self):
        from drf_spectacular.generators import SchemaGenerator
        schema = SchemaGenerator().get_schema(request=None, public=True)
        operation = schema["paths"]["/api/v1/production-runs/{id}/recipe-shot-references/"]["get"]
        self.assertTrue(next(row for row in operation["parameters"] if row["name"] == "shot_id")["required"])
        required = schema["components"]["schemas"]["RecipeShotReferenceDetail"]["required"]
        self.assertIn("selection_rule", required)
        self.assertIn("source_snapshot_sha256", required)
        self.assertNotIn("storyboard_adoption_revision", required)
        command = schema["paths"]["/api/v1/production-runs/{id}/manual-actions/"]["post"]
        ref = command["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        self.assertIn("payload", schema["components"]["schemas"][ref.rsplit("/", 1)[1]]["required"])
