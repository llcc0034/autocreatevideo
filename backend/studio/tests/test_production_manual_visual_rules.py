"""Manual film-rule review uses real candidates and original atomic adoption."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import ApprovalDecision, ArtifactSelectionDecision, CharacterBible, ModelDeployment, ModelProfileVersion
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_instance, create_execution_draft, update_execution_draft, publish_execution_version
from studio.services.json_contracts import assign_json_contract
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
from studio.services.production_reviewed_visual_rules import visual_rules_review_state
from studio.tests import test_production_manual_scripts as scripts


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ManualVisualRulesTests(TestCase):
    setUpTestData = classmethod(scripts.ManualScriptReviewTests.setUpTestData.__func__)
    post = scripts.ManualScriptReviewTests.post
    preparation = scripts.ManualScriptReviewTests.preparation
    make_candidate = scripts.ManualScriptReviewTests.make_candidate
    options = scripts.ManualScriptReviewTests.options

    def setUp(self):
        scripts.ManualScriptReviewTests.setUp(self)
        from studio.tests.visual_rules_fixtures import configure_visual_instance
        configure_visual_instance(self)
        self.character = CharacterBible.objects.create(project=self.project, slug="traveler", name="旅人", description="旅人",
            stable_speaker_id="S1")
        self.body.update(stage_key="visual_rules_generate", idempotency_key="visual-candidate",
            payload={"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256, **deepcopy(self.fixture)})
        self.stage = self.run.stages.get(stage_key="visual_rules_generate")

    def approval(self, artifact):
        state = self.options()
        return {**self.body, "stage_key": "visual_rules_review", "action": "approve", "idempotency_key": "review-visual",
            "expected_selection_revision": state["visual_rules_review"]["revision"], "payload": {
                "text_candidate": {"artifact_id": str(artifact.pk), "expected_sha256": artifact.sha256,
                    "expected_manifest_sha256": artifact.manifest_metadata_sha256},
                "comment": "逐项审核",
                "candidate_overrides": deepcopy(artifact.manifest_metadata["result"]["result"]["candidate"])}}

    def test_reviewed_three_fields_apply_once_without_completing_producer_or_other_stages(self):
        artifact = self.make_candidate()
        body = self.approval(artifact)
        body["payload"]["candidate_overrides"]["continuity_summary"] = "Reviewed traveler continuity."
        result = self.post(body)
        self.assertEqual(result.status_code, 201, result.data)
        self.project.refresh_from_db(); self.stage.refresh_from_db(); artifact.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, "Reviewed traveler continuity.")
        self.assertEqual((self.stage.status, artifact.status), ("running", "selected"))
        self.assertFalse(self.run.stages.filter(ordinal__gt=8).exclude(status="pending").exists())
        self.assertEqual(self.options()["visual_rules_review"]["revision"], 1)
        replay = self.post(body)
        self.assertEqual((replay.status_code, replay.data), (200, result.data))
        self.assertEqual(ArtifactSelectionDecision.objects.filter(artifact=artifact).count(), 1)
        decision = ApprovalDecision.objects.get(pk=result.data["target_id"])
        self.assertEqual(decision.stage_run.output_snapshot["modified_fields"], ["continuity_summary"])
        original_sha = decision.stage_run.output_snapshot_sha256
        self.assertEqual(decision.stage_run.output_snapshot_schema_version, 3)
        self.assertEqual(self.options()["visual_rules_review"]["status"], "current")
        self.project.continuity_summary = "User changed after review"
        self.project.save(update_fields=("continuity_summary",))
        self.character.description = "Changed character after approval"
        self.character.save(update_fields=("description",))
        current = self.options()
        self.assertEqual(current["visual_rules_review"]["status"], "changed")
        self.assertEqual(len(current["visual_rules_review"]["reasons"]), 1)
        self.assertIn("规则已修改", current["visual_rules_review"]["reasons"][0])
        # Read-only state evaluation also compares decision identity, even if
        # a subsequent explicit choice were to use the identical script text.
        self.run.refresh_from_db()
        self.run.script_adoption_decision_id = uuid4()
        self.assertEqual(len(visual_rules_review_state(self.run)["reasons"]), 1)
        self.project.continuity_summary = "Reviewed traveler continuity."
        self.project.save(update_fields=("continuity_summary",))
        self.character.description = "旅人"
        self.character.save(update_fields=("description",))
        state = self.options()["visual_rules_review"]
        self.assertEqual((state["status"], state["reasons"]), ("current", []))
        decision.stage_run.refresh_from_db()
        self.assertEqual(decision.stage_run.output_snapshot_sha256, original_sha)
        self.assertEqual(ArtifactSelectionDecision.objects.filter(artifact=artifact).count(), 1)

    def test_missing_or_unowned_review_basis_cannot_be_rebuilt_from_current_data(self):
        artifact = self.make_candidate()
        result = self.post(self.approval(artifact))
        self.assertEqual(result.status_code, 201, result.data)
        stage = self.run.stages.get(stage_key="visual_rules_review")
        original = deepcopy(stage.output_snapshot)
        for basis in (None, {"project_id": str(uuid4())}):
            value = {**original, "input_baseline": basis}
            assign_json_contract(stage, "output_snapshot", value, schema_version=3)
            stage.save(update_fields=("output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes", "output_snapshot_sha256"))
            ApprovalDecision.objects.filter(pk=result.data["target_id"]).update(output_snapshot_sha256=stage.output_snapshot_sha256)
            response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-action-options/")
            self.assertEqual(response.status_code, 400, response.data)
            stage.refresh_from_db()
            self.assertEqual(stage.output_snapshot["input_baseline"], basis)

    def test_changed_bible_rules_missing_fields_and_started_downstream_do_not_adopt(self):
        artifact = self.make_candidate()
        body = self.approval(artifact)
        bad = deepcopy(body); del bad["payload"]["candidate_overrides"]["color_script"]
        self.assertEqual(self.post(bad).status_code, 400)
        self.project.continuity_summary = "Concurrent edit"; self.project.save(update_fields=("continuity_summary",))
        self.assertEqual(self.post(body).status_code, 409)
        self.project.continuity_summary = ""; self.project.save(update_fields=("continuity_summary",))
        self.character.description = "Changed Bible"; self.character.save(update_fields=("description",))
        # Unselected Bible changes do not block this candidate.
        self.character.description = "旅人"; self.character.save(update_fields=("description",))
        self.run.stages.filter(stage_key="storyboard").update(status="running")
        self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(ArtifactSelectionDecision.objects.filter(artifact=artifact).exists())

    def test_unselected_script_after_visual_approval_preserves_current_evidence(self):
        from studio.models import ExecutionPreparation, GenerationAttempt
        artifact = self.make_candidate()
        approved = self.post(self.approval(artifact))
        self.assertEqual(approved.status_code, 201, approved.data)
        stage = self.run.stages.get(stage_key="visual_rules_review")
        original = deepcopy(stage.output_snapshot)
        _, revision = create_pasted_script_source(project=self.project, user=self.actor, content="旅人离开房间。")
        revision = confirm_script_revision(revision, user=self.actor)
        body = {**self.body, "stage_key": "script_input", "action": "adopt", "idempotency_key": "changed-after-visual",
            "expected_selection_revision": 0, "payload": {"comment": "明确改变来源", "content": {
                "kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256}}}
        response = self.post(body)
        self.assertEqual(response.status_code, 201, response.data)
        state = self.options()["visual_rules_review"]
        self.assertEqual(state["status"], "current")
        self.assertEqual(state["decision_id"], approved.data["target_id"])
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot, original)
        create = deepcopy(self.body); create["idempotency_key"] = "new-visual-for-new-script"
        response = self.post(create)
        self.assertEqual(response.status_code, 202, response.data)
        preparation = ExecutionPreparation.objects.get(pk=response.data["target_id"])
        self.assertIsNone(preparation.next_check_at)
        self.assertFalse(GenerationAttempt.objects.filter(preparation=preparation).exists())
        self.assertEqual(self.options()["visual_rules_review"]["status"], "current")
        self.assertEqual(self.post(self.body).data["target_id"], str(artifact.generation_attempt.preparation_id))

    def test_receipt_failure_rolls_back_project_and_selection(self):
        artifact = self.make_candidate()
        body = self.approval(artifact)
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=ValidationError("receipt failure")):
            self.assertEqual(self.post(body).status_code, 400)
        self.project.refresh_from_db(); artifact.refresh_from_db()
        self.assertEqual((self.project.continuity_summary, artifact.status), ("", "available"))
        self.assertFalse(ArtifactSelectionDecision.objects.filter(artifact=artifact).exists())
        self.assertEqual(self.options()["visual_rules_review"]["revision"], 0)
        self.assertEqual(self.options()["visual_rules_review"]["status"], "not_reviewed")
