from studio.tests.field_definition_fixtures import create_field_definition
"""One isolated Recipe run from generated script to sealed episode delivery.

Provider HTTP is replaced, but admission, queues, media probing, original
FFmpeg assembly, decisions and the delivery manifest are not bypassed.
Formal publication/creation are exercised; no live model is certified.
"""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import (ApprovalDecision, ArtifactSelectionDecision, AssemblyJob, GeneratedArtifact, GenerationAttempt, ModelDeployment,
    ModelDeploymentSmokeAttempt, ModelProfileVersion, FieldDefinition, PromptProfileFieldRule,
    PromptProfileVersion, ProductionRecipeVersion, ProductionRun, ProviderProfile, ReleaseBundle, Shot)
from studio.services.async_media_queue import claim_next_async_media_attempt, process_async_media_attempt
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, process_deployment_smoke
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import (create_execution_instance, create_execution_draft,
    update_execution_draft, publish_execution_version)
from studio.services.execution_smoke import create_instance_smoke
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.json_contracts import assign_json_contract
from studio.services.production_runs import advance_production_run, decide_stage, retry_production_run
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
from studio.tests.test_async_media_p0 import _async_spec, _provider_config
from studio.tests import test_production_recipe_content_chain as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeDeliveryChainTests(TestCase):
    setUpTestData = classmethod(fixtures.RecipeContentChainTests.setUpTestData.__func__)
    setUp = fixtures.RecipeContentChainTests.setUp
    profile_instance = fixtures.RecipeContentChainTests.profile_instance
    root = fixtures.RecipeContentChainTests.root
    receipt = fixtures.RecipeContentChainTests.receipt
    pass_smoke = fixtures.RecipeContentChainTests.pass_smoke
    advance = fixtures.RecipeContentChainTests.advance
    response = fixtures.RecipeContentChainTests.response
    process = fixtures.RecipeContentChainTests.process
    finish_content_chain = fixtures.RecipeContentChainTests.finish_content_chain

    def media_instance(self):
        provider = ProviderProfile(key="delivery-media", name="Isolated media", driver_type="async_rest", modalities="video")
        config = _provider_config()
        config["request_field_map"].update(fps="fps", duration_seconds="duration")
        assign_json_contract(provider, "driver_config", config); provider.save()
        model = ModelProfileVersion(profile_key="delivery-media", version=1, model_identifier="fixture-video",
            provider=provider, modality="video", status="published")
        specification = _async_spec()
        specification["capabilities"] = ["video.text_to_video"]
        specification["async_media"]["operations"].pop("image.text_to_image")
        assign_json_contract(model, "specification", specification); model.save()
        deployment = ModelDeployment(key="delivery-media", name="Isolated media", profile_version=model,
            credential=self.deployment.credential)
        assign_json_contract(deployment, "runtime_config", {}); deployment.save()
        profile = PromptProfileVersion.objects.create(profile_key="delivery-media", version=1, name="Reviewed shot",
            model_family="async_cloud", operation="video.generate", mode="T2VA", review_state="ready", contract_schema_version=8)
        # This delivery fixture tests scheduling/assembly, not M6 custom-field
        # projection. Keep its explicit strategy text out of legacy Shot columns.
        self.media_prompt = "A traveler opens a box."
        field = create_field_definition(key="delivery-instruction", label="Instruction", value_type="long_text")
        create_profile_field_rule(profile_version=profile, field_definition=field,
            fixed_value=self.media_prompt, source_selector="fixed.value", generation_source_selector="fixed.value", generation_mode="server_locked",
            channel="positive_prompt", position=1, required=True)
        refresh_prompt_profile_contract(profile); profile = publish_prompt_profile(profile, actor=self.actor)
        instance = create_execution_instance(key="delivery-media", name="Isolated media", description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": deployment, "operation": "video.text_to_video", "mode": "T2VA", "prompt_mode": "profile",
                "prompt_profile_version": profile, "workflow_version": None})
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"inputs": []})
        definition = build_execution_definition(version.pk)
        version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        self.media_version, self.media_definition = version, definition
        self.parameters = {"width": 64, "height": 64, "fps": 24, "duration_seconds": 5}
        return {"key": "media_generation", "mode": "existing_pipeline", "approval": "auto", "routing": {
            "mode": "fixed", "variant_count": 1, "evaluator": None, "routes": [{
                "execution_instance_version_id": str(version.pk), "expected_definition_sha256": definition.sha256,
                "input_sources": {},
                "parameter_policies": [{"key": key, "policy": "locked_value", "value": value} for key, value in self.parameters.items()]}]}}

    def media_bytes(self):
        self.output = Path(self.media_root) / "provider-fixture.mp4"
        # A tiny synthetic clip in the isolated MEDIA_ROOT, never a model call.
        subprocess.run([settings.FFMPEG_BINARY, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=24:d=5", "-an", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(self.output)], check=True, capture_output=True, timeout=30)
        self.enterContext(patch("studio.services.remote_artifacts._download_one", side_effect=self.transfer))

    def transfer(self, **kwargs):
        target = kwargs["target"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.output, target)
        return verified_local_media_facts(target, expected_kind=kwargs["expected_kind"], max_bytes=kwargs["max_bytes"])

    def pass_media_smoke(self):
        attempt, preparation, _ = create_instance_smoke(version_id=self.media_version.pk,
            expected_definition_sha256=self.media_definition.sha256, fixture={
                "inputs": {}, "parameters": self.parameters,
                "manual_channels": {}, "subject_facts": [], "source_language": "English", "target_language": "English"},
            field_values=[], client_idempotency_key="isolated-delivery-smoke", allow_external_calls=True, actor=self.actor)
        ready = self.advance(preparation)
        self.assertEqual(ready.status, "dispatched", ready.reason)
        peer = Mock()
        peer.invoke.side_effect = [{"request_id": "isolated-video-smoke", "status": "submitted"},
            {"terminal_success": True, "status": "succeeded"}, self.collected("isolated-video-smoke")]
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            for _ in range(2):
                ModelDeploymentSmokeAttempt.objects.filter(pk=attempt.pk).update(next_poll_at=timezone.now())
                claimed = claim_next_deployment_smoke("isolated-video-smoke")
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed.pk, attempt.pk)
                result = process_deployment_smoke(claimed)
        self.assertEqual(result.status, "succeeded", result.error_code)
        self.assertEqual([call.args[1] for call in peer.invoke.call_args_list], ["submit", "poll", "collect"])

    @staticmethod
    def collected(request_id):
        return {"request_id": request_id,
            "artifacts": [{"kind": "video", "url": "https://cdn.example.com/isolated.mp4"}], "actual_cost": "0.01", "currency": "USD"}

    def finish_media(self, attempt=None):
        attempt = attempt or GenerationAttempt.objects.get(stage_run__production_run=self.run, stage_run__stage_key="media_generation")
        peer = Mock()
        peer.invoke.side_effect = [{"request_id": "isolated-shot-video", "status": "submitted"},
            {"terminal_success": True, "status": "succeeded"}, self.collected("isolated-shot-video")]
        with patch("studio.services.async_media_queue.driver_for", return_value=peer):
            for _ in range(3):
                GenerationAttempt.objects.filter(pk=attempt.pk).update(next_poll_at=timezone.now())
                claim = claim_next_async_media_attempt("isolated-delivery-worker")
                self.assertIsNotNone(claim)
                self.assertEqual(claim.pk, attempt.pk)
                result = process_async_media_attempt(claim)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual([call.args[1] for call in peer.invoke.call_args_list], ["submit", "poll", "collect"])
        artifact = GeneratedArtifact.objects.get(generation_attempt=attempt)
        self.assertEqual(artifact.status, "available")
        return artifact

    def start_complete_run(self, *, unattended=False, prior=None, delivery_metadata=None, episode_count=1, shots_per_episode=1):
        media = self.media_instance()
        self.media_bytes()
        self.pass_media_smoke()
        self.values = {**self.values, "idea": "A traveler opens a box.", "style_hint": "Warm light"}
        values = {**self.values, "episode_count": episode_count, "shots_per_episode": shots_per_episode,
            "episode_duration_seconds": 5 * shots_per_episode}
        self.candidate.update(title="The box", logline="A traveler opens a box.")
        entity = self.candidate["entities"][0]
        entity["name"] = "Traveler"
        entity["identity_anchor"] = "A traveler"
        entity["primary"]["reference_name"] = "Traveler·Primary"
        episode = deepcopy(self.candidate["episodes"][0])
        shot = deepcopy(episode["shots"][0])
        self.candidate["episodes"] = []
        for episode_number in range(1, episode_count + 1):
            current = {**deepcopy(episode), "episode": episode_number, "shots": []}
            for shot_number in range(1, shots_per_episode + 1):
                current["shots"].append({**deepcopy(shot), "shot": shot_number, "duration_ms": 5000,
                    "action": self.media_prompt, "references": []})
            self.candidate["episodes"].append(current)
        self.fixture["inputs"] = {key: {"kind": "literal", "value": value} for key, value in values.items()}
        stages = fixtures.RecipeContentChainTests.content_stages(self) + [media,
            {"key": "artifact_selection", "mode": "review", "approval": "required"},
            {"key": "assembly", "mode": "existing_pipeline", "approval": "auto"},
            {"key": "delivery", "mode": "existing_pipeline", "approval": "auto"}]
        if delivery_metadata is not None:
            stages[-1]["delivery_metadata"] = delivery_metadata
        if unattended:
            from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY
            for stage in stages:
                stage["approval"] = "policy" if stage["mode"] == "review" else "auto"
                if stage["mode"] == "review":
                    stage["decision_policy"] = UNATTENDED_DECISION_POLICY_REGISTRY[stage["key"]]
        self.run = self.root(source=self.sources, variants=1, prior=prior, following=stages,
            interaction_mode="unattended" if unattended else "assisted", stage_approval="auto" if unattended else "required")
        self.pass_smoke()
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        response = self.client.post(reverse("production-recipe-list"), {"recipe_key": "complete-delivery",
            "name": "Isolated complete production", "definition": deepcopy(self.run.recipe_version.definition)}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.recipe = ProductionRecipeVersion.objects.get(pk=response.data["id"])
        self.assertEqual(self.recipe.status, "draft")
        response = self.client.post(reverse("production-recipe-publish", args=[self.recipe.pk]), {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.status, "published")
        self.assertIsNotNone(self.recipe.published_at)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.create_payload = {"project_id": str(self.project.pk), "recipe_version_id": str(self.recipe.pk),
            "plan_source": "recipe", "idempotency_key": "formal-complete-run", "allow_external_calls": True,
            "unattended_confirmed": unattended, **values}
        response = self.client.post("/api/v1/production-runs/", self.create_payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.run = ProductionRun.objects.get(pk=response.data["id"])
        self.assertEqual(self.run.status, "queued")
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_formal_create_replay_keeps_original_retired_recipe_and_rejects_new_intent(self):
        self.start_complete_run()
        plan_sha = self.run.execution_plan.definition_sha256
        stage_ids = list(self.run.stages.values_list("pk", flat=True))
        response = self.client.post(reverse("production-recipe-retire", args=[self.recipe.pk]), {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.post("/api/v1/production-runs/", self.create_payload, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["id"], str(self.run.pk))
        self.assertEqual(self.run.execution_plan.definition_sha256, plan_sha)
        self.assertEqual(list(self.run.stages.values_list("pk", flat=True)), stage_ids)
        response = self.client.post("/api/v1/production-runs/", {**self.create_payload, "idea": "A different idea"}, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        response = self.client.post("/api/v1/production-runs/", {**self.create_payload, "idempotency_key": "new-after-retirement"}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ProductionRun.objects.filter(recipe_version=self.recipe).count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_publish_rejects_incomplete_closure_and_missing_review_authority(self):
        self.start_complete_run()
        run_count = ProductionRun.objects.count()
        for index, (removed, changed) in enumerate([
            ("assembly", None), ("bible_review", None),
            (None, ("bible_review", "mode", "manual")),
            (None, ("script_review", "approval", "auto")),
            (None, ("artifact_selection", "approval", "auto")),
        ]):
            value = deepcopy(self.recipe.definition)
            value["stages"] = [row for row in value["stages"] if row["key"] != removed]
            if changed:
                next(row for row in value["stages"] if row["key"] == changed[0])[changed[1]] = changed[2]
            with self.subTest(removed=removed, changed=changed):
                response = self.client.post(reverse("production-recipe-list"), {"recipe_key": f"incomplete-{index}",
                    "name": "Isolated missing closure", "definition": value}, format="json")
                self.assertEqual(response.status_code, 201, response.data)
                recipe = ProductionRecipeVersion.objects.get(pk=response.data["id"])
                response = self.client.post(reverse("production-recipe-publish", args=[recipe.pk]), {}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                recipe.refresh_from_db()
                self.assertEqual(recipe.status, "draft")
                self.assertIsNone(recipe.published_at)
        self.assertEqual(ProductionRun.objects.count(), run_count)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_same_run_generates_reviews_assembles_and_seals_original_episode(self):
        self.start_complete_run()
        # Creation durably queues the run; it does not dispatch in the HTTP
        # request. Exercise the same advancement service as the worker.
        self.run = advance_production_run(self.run)
        self.finish_content_chain(revision=None)
        storyboard = self.run.stages.get(stage_key="storyboard")
        shot = Shot.objects.get(episode__project=self.project)
        from studio.services.production_storyboard_policy import read_recipe_storyboard_reference_selection
        reference_selection = read_recipe_storyboard_reference_selection(self.run)
        self.run = decide_stage(self.run, stage_key="storyboard_review", decision="approve", user=self.actor, comment="Reviewed shot facts",
            expected_source_snapshot_sha256=storyboard.output_snapshot_sha256,
            expected_selection_sha256=reference_selection["selection_sha256"])
        self.assertEqual(self.run.blocked_code, "waiting_external_task", self.run.blocked_reason)
        media_attempt = GenerationAttempt.objects.get(stage_run__production_run=self.run, stage_run__stage_key="media_generation")
        frozen = deepcopy(media_attempt.request_snapshot)
        self.assertFalse({"narrative", "action", "detailed_description"} & {field.name for field in Shot._meta.get_fields()})
        self.assertIn(self.media_prompt, frozen["transport"]["payload"]["prompt"])
        artifact = self.finish_media()
        self.run = advance_production_run(self.run)
        self.assertEqual((self.run.status, self.run.current_stage_key), ("awaiting_approval", "artifact_selection"), self.run.blocked_reason)
        url = f"/api/v1/production-runs/{self.run.pk}/select-artifact/"
        response = self.client.post(url, {"artifact_id": str(artifact.pk), "selection_mode": "automatic"}, format="json", HTTP_IDEMPOTENCY_KEY="choose-clip")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(artifact.stage_run.artifact_selection_decisions.exists())
        response = self.client.post(url, {"artifact_id": str(artifact.pk)}, format="json", HTTP_IDEMPOTENCY_KEY="choose-clip")
        self.assertEqual(response.status_code, 200, response.data)
        selection = artifact.stage_run.artifact_selection_decisions.get(artifact=artifact)
        self.assertEqual((selection.selection_mode, selection.selected_by_id), ("manual", self.actor.pk))
        self.run = decide_stage(self.run, stage_key="artifact_selection", decision="approve", user=self.actor, comment="Reviewed exact clip")
        self.finish_delivery(artifact)
        media_attempt.refresh_from_db()
        self.assertEqual(media_attempt.request_snapshot, frozen)

    def test_explicit_unattended_policies_reach_delivery_without_human_or_implicit_selection(self):
        artifact = self.unattended_media_candidate()
        self.run = advance_production_run(self.run)
        selection = artifact.stage_run.artifact_selection_decisions.get(artifact=artifact)
        self.assertEqual((selection.selection_mode, selection.selected_by_id), ("automatic", None))
        self.run = advance_production_run(self.run)
        self.assertEqual(artifact.stage_run.artifact_selection_decisions.get(artifact=artifact).pk, selection.pk)
        self.finish_delivery(artifact)
        decisions = ApprovalDecision.objects.filter(stage_run__production_run=self.run)
        self.assertEqual(decisions.count(), 4)
        self.assertFalse(decisions.exclude(actor="recipe_policy", decision="approve").exists())
        self.assertFalse(decisions.filter(decision_policy_sha256="").exists())
        review = self.run.stages.get(stage_key="artifact_selection")
        proof = review.output_snapshot["policy_evidence"]["selected_artifacts"]
        self.assertEqual(proof, [{"artifact_id": str(artifact.pk), "sha256": artifact.sha256,
            "decision_id": str(artifact.stage_run.artifact_selection_decisions.get(artifact=artifact).pk)}])

    def unattended_media_candidate(self):
        self.start_complete_run(unattended=True)
        self.run = advance_production_run(self.run)
        self.assertEqual(self.run.blocked_code, "waiting_external_task", self.run.blocked_reason)
        result, _ = self.process()
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.run = advance_production_run(self.run)
        self.assertEqual((self.run.current_stage_key, self.run.blocked_code), ("media_generation", "waiting_external_task"), self.run.blocked_reason)
        return self.finish_media()

    def test_manual_selection_checks_actual_candidate_before_new_receipt_but_not_old_replay(self):
        from studio.models import ProductionManualCommand
        artifact = self.unattended_media_candidate()
        path = Path(settings.MEDIA_ROOT) / artifact.object_key
        original = path.read_bytes()
        manifest = deepcopy(artifact.manifest_metadata)
        counts = (ArtifactSelectionDecision.objects.count(), ProductionManualCommand.objects.count(), GenerationAttempt.objects.count())
        url = f"/api/v1/production-runs/{self.run.pk}/select-artifact/"
        def submit(key):
            return self.client.post(url, {"artifact_id": str(artifact.pk)}, format="json", HTTP_IDEMPOTENCY_KEY=key)
        for bad in (b"", b"broken bytes", None):
            try:
                if bad is None:
                    path.unlink()
                else:
                    path.write_bytes(bad)
                response = submit("new-clip")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual((ArtifactSelectionDecision.objects.count(), ProductionManualCommand.objects.count(), GenerationAttempt.objects.count()), counts)
            finally:
                path.write_bytes(original)
        changed = deepcopy(manifest); changed["request_sha256"] = "0" * 64
        assign_json_contract(artifact, "manifest_metadata", changed); artifact.save()
        self.assertEqual(submit("new-clip").status_code, 400)
        self.assertEqual((ArtifactSelectionDecision.objects.count(), ProductionManualCommand.objects.count(), GenerationAttempt.objects.count()), counts)
        assign_json_contract(artifact, "manifest_metadata", manifest); artifact.save()
        accepted = submit("new-clip")
        self.assertEqual(accepted.status_code, 200, accepted.data)
        path.unlink()
        try:
            replay = submit("new-clip")
            self.assertEqual(replay.status_code, 200, replay.data)
            self.assertEqual(replay.data, accepted.data, "Historical receipt is not a new media acceptance")
            self.assertEqual(submit("another-intent").status_code, 400)
            self.assertEqual(ArtifactSelectionDecision.objects.count(), counts[0] + 1)
            self.assertEqual(ProductionManualCommand.objects.count(), counts[1] + 1)
            self.assertEqual(GenerationAttempt.objects.count(), counts[2])
        finally:
            path.write_bytes(original)

    def test_policy_approval_failure_rolls_back_selection_then_explicit_retry_does_not_regenerate(self):
        artifact = self.unattended_media_candidate()
        with patch("studio.services.production_runs.ApprovalDecision.objects.create", side_effect=ValidationError("isolated approval write failure")):
            self.run = advance_production_run(self.run)
        self.assertEqual((self.run.current_stage_key, self.run.blocked_code), ("artifact_selection", "stage_validation_failed"), self.run.blocked_reason)
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, "available")
        self.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run=artifact.stage_run).exists())
        review = self.run.stages.get(stage_key="artifact_selection")
        self.assertFalse(review.approval_decisions.exists())
        self.assertFalse(AssemblyJob.objects.exists())
        self.run = retry_production_run(self.run, user=self.actor, expected_stage_id=str(review.pk), expected_attempt_ids=[])
        selection = artifact.stage_run.artifact_selection_decisions.get(artifact=artifact)
        self.assertEqual((selection.selection_mode, selection.selected_by_id), ("automatic", None))
        self.finish_delivery(artifact)
        self.assertEqual(review.approval_decisions.count(), 1)
        self.assertEqual(ArtifactSelectionDecision.objects.filter(stage_run=artifact.stage_run).count(), 1)

    def test_authored_release_fields_survive_formal_publish_worker_seal_and_signed_read(self):
        import json
        from studio.services.business_outputs import write_output
        from studio.tests.output_structure_fixtures import response_structure
        from studio.tests.test_structure_mapping_preview import field, obj
        from studio.services.release_bundles import live_verified_release_bundle, ReleaseBundleError
        value = {"标题": "  Original title 原题🌧  ", "说明": "  Keep CRLF\r\n保留原文  ", "custom": [1, 2]}
        structure = response_structure(example=value)
        output = write_output(actor=self.actor, data={"name": "Release fields", "description": "Isolated author",
            "parser_key": "structured_json.v1", "data_structure_version": str(structure.pk), "quality_advisories": {}})
        output = write_output(actor=self.actor, output_id=output.pk, expected_revision=output.revision, action="publish")
        rule = {"output_version_id": str(output.pk), "expected_contract_sha256": output.contract_sha256,
            "source_text": json.dumps(value, ensure_ascii=False),
            "mapping": {"schema_version": 1, "node": obj(title=field("标题"), notes=field("说明"))}}
        self.start_complete_run(unattended=True, delivery_metadata=rule)
        self.run = advance_production_run(self.run)
        self.process()
        self.run = advance_production_run(self.run)
        artifact = self.finish_media()
        self.run = advance_production_run(self.run)
        self.finish_delivery(artifact)
        bundle = ReleaseBundle.objects.get(production_run=self.run)
        self.assertEqual((bundle.title, bundle.notes), (value["标题"], value["说明"]))
        counts = (GenerationAttempt.objects.count(), AssemblyJob.objects.count(), ArtifactSelectionDecision.objects.count())
        frozen = deepcopy(self.run.stages.get(stage_key="delivery").config_snapshot["delivery_metadata"])
        output.status = "retired"; output.save(update_fields=("status",))
        with patch("studio.services.assembly_author_contracts.output_reference", side_effect=AssertionError("Frozen only")):
            document = live_verified_release_bundle(bundle)
        self.assertEqual(document["delivery_metadata"], frozen)
        self.assertEqual(document["delivery_metadata"]["projection"]["raw_value"], value)
        self.assertEqual(document["notes"], value["说明"])
        from studio.tests.test_release_bundles import assert_metadata_batch_reads
        from studio.services.release_bundles import verified_release_bundle
        from studio.services.configuration import ConfigurationResolutionError
        assert_metadata_batch_reads(self, bundle)
        with patch("studio.services.production_recipe_stage_snapshots.recipe_delivery_metadata",
                side_effect=ConfigurationResolutionError("Isolated tightened limit", code="snapshot_invalid")):
            self.assertEqual(verified_release_bundle(bundle)["verification"]["status"], "verified")
            with self.assertRaisesMessage(ReleaseBundleError, "Isolated tightened limit"):
                live_verified_release_bundle(bundle)
        bundle.manifest["delivery_metadata"]["projection"]["raw_value"]["custom"].append(3)
        with self.assertRaises(ReleaseBundleError):
            live_verified_release_bundle(bundle)
        bundle.refresh_from_db(); bundle.notes = "changed database display"
        with self.assertRaises(ReleaseBundleError):
            live_verified_release_bundle(bundle)
        self.assertEqual(counts, (GenerationAttempt.objects.count(), AssemblyJob.objects.count(), ArtifactSelectionDecision.objects.count()))

    def finish_delivery(self, artifact):
        self.assertEqual(self.run.current_stage_key, "assembly", self.run.blocked_reason)
        assembly = self.run.stages.get(stage_key="assembly")
        frozen = deepcopy(assembly.input_snapshot)
        source = frozen["assembly_sources"]["episodes"][0]["shots"][0]["source"]
        decision = artifact.stage_run.artifact_selection_decisions.get(artifact=artifact)
        self.assertEqual((source["generated_artifact_id"], source["sha256"], source["manifest_sha256"]),
            (str(artifact.pk), artifact.sha256, artifact.manifest_metadata_sha256))
        self.assertEqual((source["decision_id"], source["decision_revision"]), (str(decision.pk), decision.revision))
        self.assertFalse(ReleaseBundle.objects.filter(production_run=self.run).exists())
        job = claim_next_assembly(worker_id="isolated-delivery-encoder")
        self.assertIsNotNone(job)
        self.assertEqual(job.source_items.get().generated_artifact_id, artifact.pk)
        with self.captureOnCommitCallbacks(execute=True):
            result = process_assembly_job(job)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.run = advance_production_run(self.run)
        self.assertEqual(self.run.status, "succeeded", self.run.blocked_reason)
        assembly.refresh_from_db()
        self.assertEqual(assembly.input_snapshot, frozen)
        bundle = ReleaseBundle.objects.get(production_run=self.run)
        self.assertEqual(bundle.status, "sealed")
        self.assertEqual(AssemblyJob.objects.count(), 1)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run__production_run=self.run).count(), 2)
        from studio.services.release_bundles import live_verified_release_bundle
        live_verified_release_bundle(bundle)
