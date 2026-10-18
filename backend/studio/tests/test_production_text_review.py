"""Original approval API adopts only a verified, explicitly selected candidate."""
from uuid import uuid4
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ApprovalDecision, ArtifactSelectionDecision, ExecutionPreparation, GeneratedArtifact, ScriptRevision, StageRun
from studio.services.production_runs import advance_production_run, decide_stage, select_run_artifact
from studio.tests import test_execution_production_dispatch as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class TextCandidateReviewTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionDispatchTests.setUpTestData.__func__)
    profile_instance = fixtures.ProductionDispatchTests.profile_instance
    root = fixtures.ProductionDispatchTests.root
    prepare = fixtures.ProductionDispatchTests.prepare
    pass_smoke = fixtures.ProductionDispatchTests.pass_smoke
    advance = fixtures.ProductionDispatchTests.advance
    response = fixtures.ProductionDispatchTests.response
    process = fixtures.ProductionDispatchTests.process
    setUp = fixtures.ProductionDispatchTests.setUp
    visual_root = fixtures.ProductionDispatchTests.visual_root

    def candidates(self, *, following=None):
        from studio.services.configuration import resolve_configuration
        from studio.services.json_contracts import assign_json_contract
        from studio.services.security_boundaries import boundary_int

        self.run = self.root(following=[{"key": "script_review", "mode": "review", "approval": "required"}, *(following or [])])
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(project=self.project).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.allow_external_calls = True
        self.run.save()
        self.review = StageRun.objects.create(production_run=self.run, stage_key="script_review", ordinal=2,
            mode="review", status="pending", idempotency_key=str(uuid4()))
        for ordinal, declaration in enumerate(following or [], start=3):
            StageRun.objects.create(production_run=self.run, stage_key=declaration["key"], ordinal=ordinal,
                mode=declaration["mode"], status="pending", idempotency_key=str(uuid4()))
        self.pass_smoke()
        started = advance_production_run(self.run)
        self.assertEqual(started.blocked_code, "waiting_external_task", (started.blocked_reason,
            list(ExecutionPreparation.objects.filter(stage_run=self.stage).values("status", "reason_code", "reason"))))
        self.process()
        self.process()
        self.run = advance_production_run(self.run)
        self.assertEqual(self.run.status, "awaiting_approval", self.run.blocked_reason)
        self.stage.refresh_from_db()
        self.review.refresh_from_db()
        self.artifact = GeneratedArtifact.objects.filter(stage_run=self.stage).order_by("variant_index").last()
        self.selection = {"artifact_id": str(self.artifact.pk), "expected_sha256": self.artifact.sha256}

    def approve(self, **changes):
        return decide_stage(self.run, **{"stage_key": "script_review", "decision": "approve", "comment": "已核对候选",
            "user": self.actor, "text_candidate": self.selection,
            "expected_source_snapshot_sha256": self.stage.output_snapshot_sha256, **changes})

    def test_select_second_candidate_confirms_only_it_and_records_original_decisions(self):
        self.candidates()
        self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())
        metadata = (self.project.title, self.project.logline)
        result = self.approve()
        self.project.refresh_from_db()
        self.assertEqual((self.project.title, self.project.logline), metadata)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "succeeded", result.blocked_reason)
        self.assertEqual(str(result.script_revision_id), self.artifact.manifest_metadata["script_revision_id"])
        self.assertEqual(ScriptRevision.objects.filter(origin_generation_attempt__stage_run=self.stage, status="confirmed").count(), 1)
        decision = ArtifactSelectionDecision.objects.get(stage_run=self.stage)
        self.assertEqual(decision.artifact_id, self.artifact.pk)
        self.assertEqual(ApprovalDecision.objects.filter(stage_run=self.review).count(), 1)
        self.assertEqual(self.review.output_snapshot["review_payload"]["artifact_id"], str(self.artifact.pk))
        with self.assertRaises(ValidationError):
            self.approve()
        self.assertEqual(ArtifactSelectionDecision.objects.filter(stage_run=self.stage).count(), 1)

    def test_recipe_field_edit_uses_same_writer_and_original_candidate(self):
        import json
        from studio.services.execution_production_candidates import read_production_text_candidate

        self.candidates()
        raw = read_production_text_candidate(self.artifact)["candidate"]
        raw["人工补充"] = [0, False, None]
        content = " \r\n" + json.dumps(raw, ensure_ascii=False) + "\r\n"
        original_id = self.artifact.manifest_metadata["script_revision_id"]
        client = APIClient()
        client.force_authenticate(self.actor)
        request = {"stage_key": "script_review", "decision": "approve", "text_candidate": self.selection,
            "expected_source_snapshot_sha256": self.stage.output_snapshot_sha256, "script_content": content}
        for changes in ({"script_content": None}, {"script_content": False}, {"script_content": {}},
                {"script_content": 7}, {"script_content": ""}, {"decision": "reject"},
                {"stage_key": "visual_rules_review"}):
            with self.subTest(changes=changes):
                response = client.post(f"/api/v1/production-runs/{self.run.pk}/approve/", {
                    **request, **changes}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        response = client.post(f"/api/v1/production-runs/{self.run.pk}/approve/", request, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()
        result = self.run
        self.review.refresh_from_db()
        self.assertEqual(result.script_revision.content, content)
        self.assertIsNone(result.script_revision.origin_generation_attempt_id)
        self.assertNotEqual(str(result.script_revision_id), original_id)
        self.assertEqual(self.review.output_snapshot["review_payload"]["edited_from_revision_id"], original_id)
        self.assertEqual(read_production_text_candidate(self.artifact)["revision"]["id"], original_id)
        from studio.services.script_revision_structures import read_confirmed_script_structure
        from studio.services.production_runs import _assert_stage_output_integrity

        source = self.review.output_snapshot["review_payload"]["structure_source"]
        downstream = read_confirmed_script_structure(project_id=self.project.pk, reference=source["script"],
            expected_source=source, max_bytes=1_000_000)
        self.assertEqual(source["script"]["id"], str(result.script_revision_id))
        self.assertEqual(downstream["structure"]["original_text"], content)
        self.assertEqual(downstream["structure"]["document"]["projection"]["source_value"], raw)
        _assert_stage_output_integrity(result, self.review)

    def test_missing_wrong_or_stale_selection_never_mutates_project(self):
        self.candidates()
        baseline = self.project.master_script
        for changes in ({"text_candidate": None}, {"text_candidate": {**self.selection, "expected_sha256": "0" * 64}},
                {"text_candidate": {**self.selection, "artifact_id": str(uuid4())}},
                {"expected_source_snapshot_sha256": "0" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.approve(**changes)
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, baseline)
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def bible_handoff(self, *, edited=False):
        """Original recipe scheduler/approval; isolated recipe and fake provider."""
        import json
        from studio.models import BibleVariant, GenerationAttempt, ScriptBibleEntityBinding
        from studio.services.script_revision_structures import parse_script_semantic_output, read_recipe_script_structure

        self.candidates(following=[
            {"key": "bible_materialize", "mode": "deterministic", "approval": "auto"},
            {"key": "bible_review", "mode": "review", "approval": "required"},
        ])
        content = None
        if edited:
            from studio.services.execution_production_candidates import read_production_text_candidate
            raw = read_production_text_candidate(self.artifact)["candidate"]
            raw["明确人工补充"] = [0, False, None]
            content = " \r\n" + json.dumps(raw, ensure_ascii=False) + "\r\n"
        with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                   wraps=parse_script_semantic_output) as parser:
            result = self.approve(**({"script_content": content} if edited else {}))
        self.assertEqual(parser.call_count, 1 if edited else 0)
        self.assertEqual((result.status, result.current_stage_key), ("awaiting_approval", "bible_review"), result.blocked_reason)
        self.review.refresh_from_db()
        source = self.review.output_snapshot["review_payload"]["structure_source"]
        materialize = result.stages.get(stage_key="bible_materialize")
        attempts = list(GenerationAttempt.objects.values_list("pk", "request_sha256"))
        artifacts = list(GeneratedArtifact.objects.values_list("pk", "sha256"))
        client = APIClient()
        client.force_authenticate(self.actor)
        with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                   side_effect=AssertionError("handoff cannot reparse")):
            handoff = read_recipe_script_structure(stage=materialize, max_bytes=1_000_000)
            self.assertEqual(handoff["source"], source)
            if edited:
                self.assertEqual(handoff["structure"]["original_text"], content)
                self.assertEqual(handoff["structure"]["document"]["projection"]["source_value"]["明确人工补充"], [0, False, None])
                self.assertNotEqual(source["script"]["id"], self.artifact.manifest_metadata["script_revision_id"])
            detail = client.get(f"/api/v1/production-runs/{result.pk}/bible-review/")
            self.assertEqual(detail.status_code, 200, detail.data)
            detail = detail.data
            self.assertTrue(detail["settings"])
            self.assertEqual(detail["settings"][0]["source_facts"]["structure_source"], source)
            confirmation = {"expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
                "expected_project_bible_sha256": detail["project_bible_sha256"],
                "reviewed_candidate_ids": [row["id"] for row in detail["candidates"]],
                "reviewed_setting_keys": [row["review_key"] for row in detail["settings"]], "reviewed_project_bible": True}
            response = client.post(f"/api/v1/production-runs/{result.pk}/approve/", {
                "stage_key": "bible_review", "decision": "approve", "bible_review": confirmation}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            review = result.stages.get(stage_key="bible_review")
            self.assertEqual(review.status, "succeeded")
            self.assertEqual(review.approval_decisions.get().actor, "human")
            bindings = list(ScriptBibleEntityBinding.objects.values_list("pk", "declaration_sha256"))
            settings = list(BibleVariant.objects.values_list("pk", "revision"))
            for _ in range(2):
                result = advance_production_run(result)
                self.assertEqual(read_recipe_script_structure(stage=materialize, max_bytes=1_000_000), handoff)
            self.assertEqual(list(ScriptBibleEntityBinding.objects.values_list("pk", "declaration_sha256")), bindings)
            self.assertEqual(list(BibleVariant.objects.values_list("pk", "revision")), settings)
        self.assertTrue(bindings)
        self.assertEqual(list(GenerationAttempt.objects.values_list("pk", "request_sha256")), attempts)
        self.assertEqual(list(GeneratedArtifact.objects.values_list("pk", "sha256")), artifacts)
        self.assertNotEqual(result.status, "succeeded")  # No media/delivery in this isolated recipe.
        return materialize, source

    def test_original_recipe_scheduler_hands_adopted_candidate_to_bible_without_reparse(self):
        self.bible_handoff()

    def test_recipe_edited_revision_not_model_original_is_handed_to_bible(self):
        self.bible_handoff(edited=True)

    def test_completed_script_review_has_one_formal_shape_and_requires_its_approval(self):
        from studio.services.production_runs import _assert_stage_output_integrity, _save_stage_snapshot
        self.candidates()
        result = self.approve()
        self.review.refresh_from_db()
        _assert_stage_output_integrity(result, self.review)
        original = self.review.output_snapshot
        missing_source = {key: value for key, value in original["review_payload"].items() if key != "structure_source"}
        with self.assertRaisesMessage(ValidationError, "确切结构化依据"):
            _assert_stage_output_integrity(result, self.review, script_review_decision=missing_source)
        changed_source = {**original["review_payload"], "structure_source": {
            **original["review_payload"]["structure_source"], "document_sha256": "0" * 64}}
        with self.assertRaisesMessage(ValidationError, "已冻结来源不一致"):
            _assert_stage_output_integrity(result, self.review, script_review_decision=changed_source)
        _save_stage_snapshot(self.review, "output_snapshot", original["review_payload"])
        with self.assertRaisesMessage(ValidationError, "正式审核快照"):
            _assert_stage_output_integrity(result, self.review)
        _save_stage_snapshot(self.review, "output_snapshot", original)
        _assert_stage_output_integrity(result, self.review)
        with patch("studio.services.script_revision_structures.read_confirmed_script_structure",
                side_effect=ValidationError("isolated structured source drift")) as reader:
            with self.assertRaisesMessage(ValidationError, "structured source drift"):
                _assert_stage_output_integrity(result, self.review)
            source = original["review_payload"]["structure_source"]
            self.assertEqual(reader.call_args.kwargs["reference"], source["script"])
            self.assertEqual(reader.call_args.kwargs["expected_source"], source)
        self.review.approval_decisions.all().delete()
        with self.assertRaisesMessage(ValidationError, "通过决定"):
            _assert_stage_output_integrity(result, self.review)
        self.assertEqual(ArtifactSelectionDecision.objects.get(stage_run=self.stage).artifact_id, self.artifact.pk)

    def test_project_edits_are_not_overwritten(self):
        self.candidates()
        self.project.title = "人工标题"
        self.project.logline = "人工更新的梗概"
        self.project.save(update_fields=("title", "logline"))
        self.approve()
        self.project.refresh_from_db()
        self.assertEqual(self.project.title, "人工标题")
        self.assertEqual(self.project.logline, "人工更新的梗概")
        self.review.refresh_from_db()
        evidence = self.review.output_snapshot["review_payload"]
        self.assertNotIn("metadata_adoption", evidence)

    def test_shared_adoption_writer_does_not_require_candidate_metadata(self):
        # Business metadata is not required for adopting a verified candidate;
        # the saved output contract still owns its configurable JSON fields.
        from studio.services.script_review import reviewed_text_candidate
        self.candidates()
        original = (self.project.title, self.project.logline)

        def without_metadata(**kwargs):
            source, artifact, detail = reviewed_text_candidate(**kwargs)
            detail.pop("candidate")
            return source, artifact, detail

        with patch("studio.services.script_review.reviewed_text_candidate", side_effect=without_metadata):
            self.approve()
        self.project.refresh_from_db()
        self.assertEqual((self.project.title, self.project.logline), original)
        self.assertEqual(ArtifactSelectionDecision.objects.get(stage_run=self.stage).artifact_id, self.artifact.pk)

    def test_removed_metadata_parameter_is_rejected_not_silently_ignored(self):
        self.candidates()
        client = APIClient()
        client.force_authenticate(self.actor)
        for value in (True, False):
            response = client.post(f"/api/v1/production-runs/{self.run.pk}/approve/", {
                "stage_key": "script_review", "decision": "approve", "text_candidate": self.selection,
                "expected_source_snapshot_sha256": self.stage.output_snapshot_sha256,
                "adopt_generated_metadata": value}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_changed_master_or_corrupt_candidate_is_not_repaired(self):
        self.candidates()
        self.project.master_script = "人工新母稿"
        self.project.save(update_fields=("master_script",))
        with self.assertRaisesMessage(ValidationError, "母稿"):
            self.approve()
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(ApprovalDecision.objects.exists())

    def test_approval_failure_rolls_back_adoption_and_generic_selection_is_rejected(self):
        self.candidates()
        with self.assertRaisesMessage(ValidationError, "审核"):
            select_run_artifact(self.run, self.artifact, user=self.actor)
        with patch("studio.services.production_runs.ApprovalDecision.objects.create", side_effect=ValidationError("audit failed")):
            with self.assertRaisesMessage(ValidationError, "audit failed"):
                self.approve()
        self.run.refresh_from_db()
        self.assertIsNone(self.run.script_revision_id)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())

    def test_formal_approval_api_requires_explicit_candidate(self):
        self.candidates()
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.post(f"/api/v1/production-runs/{self.run.pk}/approve/", {
            "stage_key": "script_review", "decision": "approve", "text_candidate": self.selection,
            "expected_source_snapshot_sha256": self.stage.output_snapshot_sha256}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def visual_candidates(self):
        self.visual_root(following=[{"key": "visual_rules_review", "mode": "review", "approval": "required"}])
        self.assertIsNone(self.plan.value["initial_content"])
        self.assertIsNone(self.run.script_revision_id)
        self.review = StageRun.objects.create(production_run=self.run, stage_key="visual_rules_review", ordinal=2,
            mode="review", status="pending", idempotency_key=str(uuid4()))
        self.pass_smoke()
        advance_production_run(self.run)
        self.process(); self.process()
        self.run = advance_production_run(self.run)
        self.assertEqual(self.run.status, "awaiting_approval", self.run.blocked_reason)
        self.stage.refresh_from_db()
        self.artifact = GeneratedArtifact.objects.filter(stage_run=self.stage).order_by("variant_index").last()
        self.selection = {"artifact_id": str(self.artifact.pk), "expected_sha256": self.artifact.sha256}

    def test_visual_review_adopts_explicit_candidate_and_three_reviewed_fields(self):
        from copy import deepcopy
        from studio.services.json_contracts import json_contract
        from studio.services.production_runs import _save_stage_snapshot
        from studio.services.production_reviewed_visual_rules import read_reviewed_visual_rules
        self.visual_candidates()
        candidate = self.artifact.manifest_metadata["result"]["result"]["candidate"]
        result = self.approve(stage_key="visual_rules_review", candidate_overrides=candidate)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "succeeded", result.blocked_reason)
        self.project.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, candidate["continuity_summary"])
        self.assertEqual(ArtifactSelectionDecision.objects.get(stage_run=self.stage).artifact_id, self.artifact.pk)
        decision = self.review.approval_decisions.get()
        original = deepcopy(self.review.output_snapshot)
        self.assertEqual(original["review_payload"]["schema_version"], 3)
        reviewed = read_reviewed_visual_rules(run=result, eligible_stage_ids=[str(self.review.pk)], decision_id=str(decision.pk))
        self.assertEqual(reviewed["canonical"], candidate)
        self.assertEqual(reviewed["review"]["snapshot_sha256"], self.review.output_snapshot_sha256)
        self.assertEqual(reviewed["output"]["artifact_id"], str(self.artifact.pk))
        for changed in ("project_id", "rules"):
            corrupt = deepcopy(original)
            corrupt["canonical"][changed] = str(uuid4()) if changed == "project_id" else {**candidate, "continuity_summary": "Not reviewed"}
            corrupt["canonical_sha256"] = json_contract(corrupt["canonical"]).sha256
            _save_stage_snapshot(self.review, "output_snapshot", corrupt)
            with self.subTest(changed=changed), self.assertRaisesMessage(ValidationError, "审核内容不一致"):
                read_reviewed_visual_rules(run=result, eligible_stage_ids=[str(self.review.pk)])
        _save_stage_snapshot(self.review, "output_snapshot", original)

    def test_visual_review_rejects_source_drift_without_partial_adoption(self):
        self.visual_candidates()
        self.project.continuity_summary = "Human edit after generation."
        self.project.save(update_fields=("continuity_summary",))
        candidate = self.artifact.manifest_metadata["result"]["result"]["candidate"]
        with self.assertRaises(ValidationError):
            self.approve(stage_key="visual_rules_review", candidate_overrides=candidate)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(ApprovalDecision.objects.exists())

    def unattended_visual(self):
        from studio.services.json_contracts import json_contract
        from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY
        from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
        from studio.tests.visual_rules_fixtures import configure_visual_instance
        configure_visual_instance(self)
        policy = UNATTENDED_DECISION_POLICY_REGISTRY["visual_rules_review"]
        self.run = self.root(stage_key="visual_rules_generate", variants=1, interaction_mode="unattended", following=[{
            "key": "visual_rules_review", "mode": "review", "approval": "policy", "decision_policy": policy}])
        self.run.allow_external_calls = True
        freeze_recipe_request_fixture(self.run, authorized=True)
        self.run.save()
        policy_sha = json_contract({"schema_version": 1, "interaction_mode": "unattended", "stage_key": "visual_rules_review",
            "decision_policy": policy, "recipe_definition_sha256": self.plan.value["recipe"]["sha256"]}).sha256
        self.review = StageRun.objects.create(production_run=self.run, stage_key="visual_rules_review", ordinal=2,
            mode="review", status="pending", decision_policy=policy, decision_policy_sha256=policy_sha, idempotency_key=str(uuid4()))
        self.pass_smoke()
        advance_production_run(self.run)
        self.process()
        self.run = advance_production_run(self.run)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "succeeded", self.run.blocked_reason)

    def test_unattended_visual_can_be_read_without_fabricated_human_review(self):
        from studio.services.production_reviewed_visual_rules import read_reviewed_visual_rules
        self.unattended_visual()
        decision = ApprovalDecision.objects.get(stage_run=self.review)
        self.assertEqual(decision.actor, "recipe_policy")
        self.assertIsNone(decision.decided_by_id)
        result = read_reviewed_visual_rules(run=self.run, eligible_stage_ids=[self.review.pk], decision_id=str(decision.pk))
        self.assertEqual(result["canonical"], self.review.output_snapshot["review_payload"]["candidate"])
        self.assertEqual(result["review"]["decision_id"], str(decision.pk))
        for invalid in ({"eligible_stage_ids": []}, {"decision_id": str(uuid4())}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                read_reviewed_visual_rules(**{"run": self.run, "eligible_stage_ids": [self.review.pk], **invalid})
        self.assertEqual(ApprovalDecision.objects.count(), 1)

    def test_unattended_visual_rejects_policy_hash_and_rehashed_evidence_drift(self):
        from copy import deepcopy
        from studio.services.json_contracts import assign_json_contract
        from studio.services.production_reviewed_visual_rules import read_reviewed_visual_rules
        self.unattended_visual()
        decision = ApprovalDecision.objects.get(stage_run=self.review)
        original_hash = decision.decision_policy_sha256
        ApprovalDecision.objects.filter(pk=decision.pk).update(decision_policy_sha256="0" * 64)
        with self.assertRaises(ValidationError):
            read_reviewed_visual_rules(run=self.run, eligible_stage_ids=[self.review.pk])
        ApprovalDecision.objects.filter(pk=decision.pk).update(decision_policy_sha256=original_hash)
        original = deepcopy(self.review.output_snapshot)
        for section, key, value in (("policy_evidence", "source_snapshot_sha256", "0" * 64),
                ("policy_evidence", "artifact_id", str(uuid4())),
                ("review_payload", "public_generation_snapshot_sha256", "0" * 64),
                ("review_payload", "modified_fields", ["color_script"]),
                ("review_payload", "generation_stage_id", str(uuid4()))):
            with self.subTest(section=section, key=key):
                forged = deepcopy(original)
                forged[section][key] = value
                assign_json_contract(self.review, "output_snapshot", forged)
                self.review.save()
                ApprovalDecision.objects.filter(pk=decision.pk).update(output_snapshot_sha256=self.review.output_snapshot_sha256)
                with self.assertRaises(ValidationError):
                    read_reviewed_visual_rules(run=self.run, eligible_stage_ids=[self.review.pk])
        self.assertEqual(ApprovalDecision.objects.count(), 1)

    def unattended(self, variants):
        from studio.services.json_contracts import assign_json_contract, json_contract
        from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY
        policy = UNATTENDED_DECISION_POLICY_REGISTRY["script_review"]
        self.run = self.root(variants=variants, interaction_mode="unattended", following=[{
            "key": "script_review", "mode": "review", "approval": "policy", "decision_policy": policy}])
        self.run.allow_external_calls = True
        from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
        freeze_recipe_request_fixture(self.run, authorized=True)
        self.run.save()
        policy_sha = json_contract({"schema_version": 1, "interaction_mode": "unattended", "stage_key": "script_review",
            "decision_policy": policy, "recipe_definition_sha256": self.plan.value["recipe"]["sha256"]}).sha256
        self.review = StageRun.objects.create(production_run=self.run, stage_key="script_review", ordinal=2,
            mode="review", status="pending", decision_policy=policy, decision_policy_sha256=policy_sha, idempotency_key=str(uuid4()))
        self.pass_smoke()
        advance_production_run(self.run)
        for _ in range(variants): self.process()
        return advance_production_run(self.run)

    def test_original_unattended_policy_approves_only_unique_verified_candidate(self):
        metadata = (self.project.title, self.project.logline)
        result = self.unattended(1)
        self.project.refresh_from_db()
        self.assertEqual((self.project.title, self.project.logline), metadata)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "succeeded", result.blocked_reason)
        self.assertEqual(ApprovalDecision.objects.get(stage_run=self.review).actor, "recipe_policy")
        self.assertEqual(ArtifactSelectionDecision.objects.filter(stage_run=self.stage).count(), 1)
        from studio.services.production_runs import _assert_stage_output_integrity

        self.assertEqual(self.review.output_snapshot["review_payload"]["structure_source"]["script"]["id"],
            str(result.script_revision_id))
        _assert_stage_output_integrity(result, self.review)

    def test_original_unattended_policy_never_picks_first_of_multiple_candidates(self):
        result = self.unattended(2)
        self.assertEqual(result.blocked_code, "unattended_text_candidate_count_invalid", result.blocked_reason)
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
