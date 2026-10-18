"""CL-03: stored fixtures across the M1 consumer and M9 additive migrations.

No provider, worker, live database or business-data cleanup is involved.
"""
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from studio.services.json_contracts import json_contract
from studio.tests.test_asset_management_api import image_upload
from studio.tests.test_configuration_migrations import _MigrationTestCase


class ModuleCutoverMigrationTests(_MigrationTestCase):
    migrate_from = "0206_structure_node_defaults"
    migrate_to = "0211_asset_reference_relations"

    def set_up_before_migration(self, apps):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(override_settings(MEDIA_ROOT=self.root, ASSET_LIBRARY_ROOT=self.root / "library"))
        self.external = self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("No external call")))
        self.rows = []

        def create(model_name, **values):
            row = apps.get_model("studio", model_name).objects.create(**values)
            self.rows.append((model_name, row.pk))
            return row

        def snapshot(value, prefix):
            contract = json_contract(value)
            return {prefix: value, f"{prefix}_schema_version": contract.schema_version,
                f"{prefix}_size_bytes": contract.size_bytes, f"{prefix}_sha256": contract.sha256}

        self.owner = apps.get_model(settings.AUTH_USER_MODEL).objects.create(username="isolated-cutover")
        identity = create("FieldDefinition", key="cutover-field")
        field = create("FieldDefinitionVersion", field_definition=identity, version=1,
            status="published", label="可保留的字段", category="custom", value_type="object")
        structure = create("DataStructureVersion", name="不自动指派的结构", status="published")
        create("DataStructureNode", structure=structure, node_key="root", field_definition_version=field)
        self.structure_id = structure.pk
        profile = create("PromptProfileVersion", profile_key="cutover-profile", version=1,
            name="旧策略夹具", model_family="fixture", operation="video.generate", status="published",
            contract_schema_version=8, contract_sha256="a" * 64, contract_size_bytes=123)
        self.profile_id = profile.pk
        rule = create("PromptProfileFieldRule", profile_version=profile, field_definition=identity,
            field_definition_version=field, source_selector="request.subject", channel="positive_prompt", position=1)
        self.rule_id = rule.pk
        provider = create("ProviderProfile", key="cutover-provider", name="无网络夹具", driver_type="comfyui")
        model = create("ModelProfileVersion", profile_key="cutover-model", version=1, provider=provider,
            model_identifier="fixture", modality="image")
        deployment = create("ModelDeployment", key="cutover-deployment", name="隔离实例", profile_version=model)
        instance = create("ModelExecutionInstance", key="cutover-instance", name="隔离执行身份")
        version = create("ModelExecutionInstanceVersion", instance=instance, version=1, deployment=deployment,
            operation="image.generate", prompt_mode="none", status="published",
            published_at=timezone.now(), published_by=self.owner)
        contract = json_contract({"fixture": "unchanged old definition", "inputs": ["subject"]})
        create("ExecutionVersionContract", version=version, definition=contract.value,
            schema_version=contract.schema_version, size_bytes=contract.size_bytes, sha256=contract.sha256,
            target_fingerprint="b" * 64)
        self.input_ids = []
        for index, kind in enumerate(("json", "image")):
            row = create("ExecutionInputSpec", version=version, key=f"input_{index}", kind=kind,
                position=index, required=True, min_items=1, max_items=1, semantic_contract={"fixture": kind})
            self.input_ids.append(row.pk)

        project = create("SeriesProject", owner=self.owner, title="仅迁移测试", language="zh",
            aspect_ratio="16:9", width=640, height=360, fps=24, default_shot_duration_ms=1000, continuity_seed=1)
        recipe = create("ProductionRecipeVersion", recipe_key="cutover-recipe", version=1, name="旧配方夹具",
            status="retired", **snapshot({"fixture": "old recipe"}, "definition"))
        run = create("ProductionRun", owner=self.owner, project=project, recipe_version=recipe,
            plan_source="recipe", status="cancelled", idempotency_key="cutover-run", request_sha256="c" * 64,
            **snapshot({"fixture": [0, False, None]}, "input_snapshot"))
        stage = create("StageRun", production_run=run, stage_key="fixture", ordinal=1, mode="isolated",
            status="cancelled", idempotency_key="cutover-stage",
            **snapshot({"fixture": "original selection"}, "output_snapshot"))
        self.stage_id = stage.pk
        asset = create("Asset", owner=self.owner, name="保留原图", kind="reference", file=image_upload(),
            metadata={"fixture": "do not infer a Bible or series"})
        self.asset_id = asset.pk
        self.media_path = Path(asset.file.path)
        self.media_bytes = self.media_path.read_bytes()
        artifact = create("GeneratedArtifact", production_run=run, stage_run=stage, asset=asset,
            manifest_key="cutover-artifact", kind="image", role="fixture", status="selected", variant_group="fixture")
        self.artifact_id = artifact.pk
        decision = create("ArtifactSelectionDecision", stage_run=stage, artifact=artifact,
            variant_group="fixture", revision=1, selected_by=self.owner)
        self.decision_id = decision.pk
        self.before = {(name, pk): apps.get_model("studio", name).objects.filter(pk=pk).values().get()
            for name, pk in self.rows}
        self.counts = {name: apps.get_model("studio", name).objects.count() for name, _ in self.rows}

    def assert_old_rows_unchanged(self):
        for (name, pk), expected in self.before.items():
            with self.subTest(model=name):
                row = self.apps.get_model("studio", name).objects.filter(pk=pk).values(*expected).get()
                self.assertEqual(row, expected)
        self.assertEqual(self.media_path.read_bytes(), self.media_bytes)
        self.assertEqual(list(self.root.rglob("*.png")), [self.media_path])
        self.external.assert_not_called()

    def test_upgrade_preserves_original_ids_values_hashes_and_does_not_infer_bindings(self):
        self.assert_old_rows_unchanged()
        for name, count in self.counts.items():
            self.assertEqual(self.apps.get_model("studio", name).objects.count(), count, name)
        for pk in self.input_ids:
            row = self.apps.get_model("studio", "ExecutionInputSpec").objects.get(pk=pk)
            self.assertIsNone(row.data_structure_version_id)
            self.assertIsNone(row.media_source_policy)
        self.assertIsNone(self.apps.get_model("studio", "PromptProfileVersion").objects.get(pk=self.profile_id).input_structure_version_id)
        self.assertIsNone(self.apps.get_model("studio", "PromptProfileFieldRule").objects.get(pk=self.rule_id).source_path)
        for name in ("AssetReferenceSeries", "AssetReferenceVersion"):
            self.assertEqual(self.apps.get_model("studio", name).objects.count(), 0)

    def test_old_manual_decision_stays_manual_and_new_actor_modes_are_enforced(self):
        decisions = self.apps.get_model("studio", "ArtifactSelectionDecision").objects
        previous = decisions.get(pk=self.decision_id)
        self.assertEqual((previous.selection_mode, previous.selected_by_id), ("manual", self.owner.pk))
        values = {"stage_run_id": self.stage_id, "artifact_id": self.artifact_id, "variant_group": "fixture", "revision": 2}
        for mode, actor in (("manual", None), ("automatic", self.owner.pk)):
            with self.subTest(mode=mode), self.assertRaises(IntegrityError), transaction.atomic():
                decisions.create(**values, selection_mode=mode, selected_by_id=actor)
        created = decisions.create(**values, selection_mode="automatic", selected_by_id=None)
        self.assertIsNone(created.selected_by_id)
        self.assert_old_rows_unchanged()

    def test_new_library_relations_are_explicit_and_do_not_copy_old_assets(self):
        series = self.apps.get_model("studio", "AssetReferenceSeries")
        versions = self.apps.get_model("studio", "AssetReferenceVersion")
        primary = series.objects.create(owner_id=self.owner.pk, name="主图", role="primary")
        variant = series.objects.create(owner_id=self.owner.pk, name="任意语言 / tenue", role="variant", primary_series=primary)
        for target in (primary, variant):
            row = versions.objects.create(series=target, version=1, asset_id=self.asset_id, source="existing")
            self.assertIsNone(row.generated_from_id)
            self.assertIsNone(row.setting_revision_id)
            self.assertIsNone(row.generation_job_id)
            self.assertIsNone(row.generated_artifact_id)
            self.assertIsNone(target.bible_variant_id)
        self.assertEqual(self.apps.get_model("studio", "Asset").objects.count(), self.counts["Asset"])
        self.assertEqual(versions.objects.count(), 2)
        self.assert_old_rows_unchanged()
