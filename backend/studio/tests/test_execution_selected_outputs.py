"""Explicit persisted selections and real PNG bytes; no model/remote mocks."""
from copy import deepcopy
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from studio.models import (ArtifactSelectionDecision, Asset, ExecutionPreparation, GeneratedArtifact, GenerationAttempt,
    ModelDeployment, ModelExecutionInstance, ModelExecutionInstanceVersion, ModelProfileVersion, ProviderProfile,
    ProductionRecipeVersion, ProductionRun, SeriesProject, StageRun)
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_selected_outputs import resolve_selected_execution_media, selected_output_item_key
from studio.services.generated_artifacts import select_artifact
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_input_resolution as input_fixtures


class ExecutionSelectedOutputTests(TestCase):
    setUpTestData = classmethod(input_fixtures.ExecutionInputResolutionTests.setUpTestData.__func__)
    definition = input_fixtures.ExecutionInputResolutionTests.definition

    def setUp(self):
        input_fixtures.ExecutionInputResolutionTests.setUp(self)
        self.project = SeriesProject.objects.create(owner=self.owner, title="Explicit upstream",
            language="English", aspect_ratio="9:16", width=768, height=1344, fps=24,
            default_shot_duration_ms=3000, continuity_seed=1)
        # These are isolated source ownership records, not a claim that the
        # new recipe plan/queue has already been published or run.
        recipe = ProductionRecipeVersion.objects.create(recipe_key="selected-source-test", version=1,
            name="Test source", definition_sha256=json_contract({}).sha256)
        self.run = ProductionRun.objects.create(owner=self.owner, project=self.project, recipe_version=recipe, plan_source="recipe",
            idempotency_key="selection-run", request_sha256=json_contract({}).sha256,
            config_snapshot_sha256=json_contract({}).sha256, input_snapshot_sha256=json_contract({}).sha256)
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="pictures", ordinal=1,
            mode="manual", idempotency_key="source-stage", status="succeeded")
        provider = ProviderProfile.objects.create(key="selected-source", name="Fixture", driver_type="async_rest")
        model = ModelProfileVersion.objects.create(profile_key="selected-source", version=1,
            model_identifier="fixture", modality="image", provider=provider)
        deployment = ModelDeployment.objects.create(key="selected-source", name="Fixture", profile_version=model)
        instance = ModelExecutionInstance.objects.create(key="selected-source", name="Fixture")
        version = ModelExecutionInstanceVersion.objects.create(instance=instance, version=1, deployment=deployment,
            operation="image.text_to_image", prompt_mode="manual")
        preparation = ExecutionPreparation.objects.create(stage_run=self.stage, execution_instance_version=version,
            purpose="production", unit_key="source-unit", request_key="source-image", status="preparing",
            created_by=self.owner, intent_sha256=json_contract({}).sha256)
        self.attempt = GenerationAttempt.objects.create(stage_run=self.stage, unit_key="source-unit",
            model_deployment=deployment, operation=version.operation, execution_instance_version=version, preparation=preparation, role="primary",
            attempt_number=1, status="succeeded", request_sha256=json_contract({}).sha256)
        self.path = self.root / "generated-artifacts" / str(self.owner.pk) / str(self.project.pk) / "source.png"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(self.bytes)
        metadata = json_contract({"tenant_owner_id": str(self.owner.pk)})
        self.artifact = GeneratedArtifact.objects.create(manifest_key="selected-output-test", production_run=self.run,
            stage_run=self.stage, generation_attempt=self.attempt, kind="image", role="reference",
            variant_group="unit-picture", variant_index=1, status="available", storage_backend="managed_local",
            object_key=self.path.relative_to(self.root).as_posix(), mime_type="image/png", size_bytes=len(self.bytes),
            sha256=self.asset.checksum, manifest_metadata=metadata.value, manifest_schema_version=metadata.schema_version,
            manifest_metadata_size_bytes=metadata.size_bytes, manifest_metadata_sha256=metadata.sha256)
        select_artifact(self.artifact, user=self.owner)
        self.decision = ArtifactSelectionDecision.objects.get(artifact=self.artifact)

    def read(self, **kwargs):
        return resolve_selected_execution_media(**{
            "decision_id": str(self.decision.pk), "artifact_id": str(self.artifact.pk), "owner": self.owner,
            "production_run_id": str(self.run.pk), "eligible_stage_ids": [self.stage.pk],
            "expected_kind": "image", "max_media_bytes": 65536, **kwargs})

    def item(self, *, input_key="references", **changes):
        row = {"kind": "selected_output", "decision_id": str(self.decision.pk), "artifact_id": str(self.artifact.pk),
            "purpose": None, **changes}
        row["item_key"] = selected_output_item_key(input_key=input_key, decision_id=row["decision_id"],
            artifact_id=row["artifact_id"], purpose=row["purpose"])
        return row

    def resolve(self, items=None, **kwargs):
        return resolve_execution_input_sources(self.definition(), {"references": {"items": items or [self.item()]}},
            owner=self.owner, subject_facts=[], max_media_bytes=65536, project_id=self.project.pk,
            production_run_id=str(self.run.pk), eligible_stage_ids=[self.stage.pk], **kwargs).value

    def test_selected_artifact_uses_real_bytes_without_creating_or_faking_asset(self):
        before = Asset.objects.count()
        selected = self.read()
        result = self.resolve()
        record = result["logical_values"]["references"][0]
        self.assertIsNone(record["asset_id"])
        self.assertIsNone(record["asset_facts"])
        self.assertEqual(record["artifact_id"], str(self.artifact.pk))
        self.assertEqual(record["origin"]["decision_id"], str(self.decision.pk))
        self.assertEqual(record["origin"]["decision_revision"], 1)
        self.assertEqual(record["origin"]["selected_by"], str(self.owner.pk))
        self.assertEqual(record["media_facts"], selected.media_facts)
        self.assertEqual(record["media_facts"]["observed_media"]["width"], 12)
        self.assertNotIn(str(self.root), str(result))
        self.assertNotIn("object_key", str(result))
        self.assertEqual(Asset.objects.count(), before)
        self.assertEqual(result, self.resolve())
        self.assertIn(self.item()["item_key"], str(result["workflow_slots"]))

    def test_older_decision_is_not_replaced_with_latest_even_for_same_artifact(self):
        original = deepcopy(self.resolve())
        select_artifact(self.artifact, user=self.owner)
        with self.assertRaisesMessage(ValidationError, "选片决定已更新"):
            self.resolve()
        self.decision = ArtifactSelectionDecision.objects.filter(artifact=self.artifact).order_by("-revision").first()
        current = self.resolve()
        self.assertEqual(original["logical_values"]["references"][0]["origin"]["decision_revision"], 1)
        self.assertEqual(current["logical_values"]["references"][0]["origin"]["decision_revision"], 2)
        self.assertNotEqual(original, current)

    def test_policy_selection_keeps_automatic_identity_in_api_and_downstream(self):
        from studio.platform_serializers import ArtifactSelectionDecisionSerializer

        select_artifact(self.artifact, user=self.owner, selection_mode="automatic")
        self.decision = ArtifactSelectionDecision.objects.filter(artifact=self.artifact).latest("revision")
        self.assertEqual((self.decision.selection_mode, self.decision.selected_by_id), ("automatic", None))
        record = ArtifactSelectionDecisionSerializer(self.decision).data
        self.assertEqual((record["selection_mode"], record["selected_by"]), ("automatic", None))
        origin = self.resolve()["logical_values"]["references"][0]["origin"]
        self.assertEqual((origin["selection_mode"], origin["selected_by"]), ("automatic", None))
        # Creating a policy decision never relabels the earlier human record.
        previous = ArtifactSelectionDecision.objects.get(artifact=self.artifact, revision=1)
        self.assertEqual((previous.selection_mode, previous.selected_by_id), ("manual", self.owner.pk))

    def test_policy_selection_preserves_permission_and_candidate_validation(self):
        outsider = get_user_model().objects.create_user(username="unrelated-selector")
        before = list(ArtifactSelectionDecision.objects.values_list("pk", flat=True))
        for user, mode, status, attempt_status, role in [
            (outsider, "automatic", "selected", "succeeded", "reference"),
            (self.owner, "unknown", "selected", "succeeded", "reference"),
            (self.owner, "automatic", "invalid", "succeeded", "reference"),
            (self.owner, "automatic", "selected", "failed", "reference"),
            (self.owner, "automatic", "selected", "succeeded", "structured_text_candidate"),
        ]:
            with self.subTest(user=user.pk, mode=mode, status=status, attempt=attempt_status, role=role):
                GeneratedArtifact.objects.filter(pk=self.artifact.pk).update(status=status, role=role)
                GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status=attempt_status)
                self.artifact.refresh_from_db()
                with self.assertRaises(ValidationError):
                    select_artifact(self.artifact, user=user, selection_mode=mode)
                self.assertEqual(list(ArtifactSelectionDecision.objects.values_list("pk", flat=True)), before)
                self.artifact.refresh_from_db()
                self.assertEqual(self.artifact.status, status)

    def test_owner_run_stage_and_exact_identity_cannot_be_spoofed(self):
        outsider = get_user_model().objects.create_user(username="unrelated-output-owner", is_staff=True)
        for change in ({"owner": outsider}, {"production_run_id": str(uuid4())}):
            with self.subTest(change=change), self.assertRaises(PermissionDenied):
                self.read(**change)
        for change in ({"eligible_stage_ids": []}, {"artifact_id": str(uuid4())}, {"decision_id": str(uuid4())},
                       {"production_run_id": None}, {"expected_kind": "video"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.read(**change)
        bad = self.item()
        bad["item_key"] = "a-caller-invented-selection"
        with self.assertRaises(ValidationError):
            self.resolve([bad])

    def test_pending_stage_failed_task_or_unselected_artifact_is_not_usable(self):
        for model, row, field, value in ((StageRun, self.stage, "status", "awaiting_approval"),
            (GenerationAttempt, self.attempt, "status", "failed"), (GeneratedArtifact, self.artifact, "status", "available")):
            with self.subTest(model=model):
                before = model.objects.values_list(field, flat=True).get(pk=row.pk)
                model.objects.filter(pk=row.pk).update(**{field: value})
                with self.assertRaises(ValidationError):
                    self.read()
                model.objects.filter(pk=row.pk).update(**{field: before})

    def test_file_checksum_manifest_and_tenant_must_agree(self):
        for field, value in (("sha256", "a" * 64), ("size_bytes", len(self.bytes) + 1), ("mime_type", "image/jpeg"),
                             ("manifest_metadata_sha256", "b" * 64), ("object_key", "../outside.png")):
            original = getattr(self.artifact, field)
            GeneratedArtifact.objects.filter(pk=self.artifact.pk).update(**{field: value})
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.read()
            GeneratedArtifact.objects.filter(pk=self.artifact.pk).update(**{field: original})
        self.path.unlink()
        with self.assertRaises(ValidationError):
            self.read()

    def test_real_linked_asset_and_adopted_output_share_duplicate_use_policy(self):
        GeneratedArtifact.objects.filter(pk=self.artifact.pk).update(asset=self.asset)
        items = [self.item(), {"kind": "asset", "item_key": "direct-use", "asset_id": str(self.asset.pk), "purpose": None}]
        with self.assertRaisesMessage(ValidationError, "不允许重复"):
            self.resolve(items)
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        result = self.resolve(items)
        self.assertEqual(len(result["logical_values"]["references"]), 2)
        self.assertEqual([row["asset_id"] for row in result["logical_values"]["references"]], [str(self.asset.pk)] * 2)

    def test_selection_use_key_includes_each_identity_dimension(self):
        arguments = {"input_key": "references", "decision_id": str(self.decision.pk), "artifact_id": str(self.artifact.pk), "purpose": None}
        original = selected_output_item_key(**arguments)
        for change in ({"input_key": "second-use"}, {"decision_id": str(uuid4())}, {"artifact_id": str(uuid4())}, {"purpose": "explicit-purpose"}):
            self.assertNotEqual(original, selected_output_item_key(**{**arguments, **change}))
