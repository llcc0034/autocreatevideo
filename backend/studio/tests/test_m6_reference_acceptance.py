"""M6-A01/A02: real local PNG, formal writers and persisted preparation APIs.

All records are isolated fixtures. No model request, worker, Smoke or live DB.
Two variants means two variant identities, not two revisions of one variant.
"""
from copy import deepcopy
from base64 import b64encode
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import Asset, AssetGenerationJob, AssetReferenceSeries, CharacterBible, GenerationAttempt, GenerationTask, ModelExecutionInstanceVersion, ShotReferenceUse
from studio.services.asset_references import attach_asset
from studio.services.bible_variants import create_variant
from studio.services.execution_asset_auxiliaries import create_asset_field_child
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import update_execution_draft
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_reference_display import frozen_reference_display
from studio.services.execution_workflow_uses import read_execution_workflow_uses
from studio.services.json_contracts import json_contract
from studio.services.production_reference_mapping import expand_reviewed_reference_sources, reference_use_records
from studio.services.shot_reference_sources import resolve_shot_reference_sources
from studio.tests import test_execution_shot_reference_facts as fixtures
from studio.tests.test_production_reference_mapping import mapping


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class M6ReferenceAcceptanceTests(TestCase):
    _create_version = fixtures.ShotReferencePreparationPersistenceTests._create_version
    selection = fixtures.ShotReferencePreparationPersistenceTests.selection
    resolve = fixtures.ShotReferencePreparationPersistenceTests.resolve

    def configure(self, profile):
        auxiliaries = fixtures.ShotReferencePreparationPersistenceTests.configure(self, profile)
        spec = deepcopy(self.definition.value["configuration"]["inputs"][0])
        spec["semantic_contract"]["allowed_subject_kinds"] = ["character"]
        version = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        version = update_execution_draft(version_id=version.pk,
            expected_revision=version.revision, actor=self.user, changes={"inputs": [spec]})
        self.definition = build_execution_definition(version.pk)
        return auxiliaries

    def setUp(self):
        fixtures.ShotReferencePreparationPersistenceTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.receipts = []
        buffer = BytesIO()
        Image.new("RGB", (18, 12), "orange").save(buffer, format="PNG")
        other = Asset(owner=self.user, kind="reference", name="Second isolated reference",
            mime_type="image/png", checksum=sha256(buffer.getvalue()).hexdigest())
        other.file.save("second-reference.png", ContentFile(buffer.getvalue()))
        self.assets = [self.asset, other]

    def setting(self, entity, key, primary=None):
        variant = create_variant(actor=self.user, project_id=self.project_row.pk,
            entity_kind="character", entity_id=entity.pk,
            appearance_role="variant" if primary else "primary",
            primary_setting_revision_id=primary.pk if primary else None,
            primary_setting_sha256=primary.content_sha256 if primary else "",
            value={"name": key, "reference_name": key, "identity_anchor": "" if primary else entity.name,
                "appearance_anchor": "NOT_IMPLICIT_PROMPT_" + key,
                "basis_asset_id": None, "basis_asset_sha256": ""})
        return variant.revisions.get(revision=1)

    def reference_use(self, key, setting, position, parent_series=None):
        asset = self.assets[position]
        series = AssetReferenceSeries.objects.create(owner=self.user, name=key,
            role="variant" if parent_series else "primary", primary_series=parent_series,
            bible_variant=setting.variant)
        version, _ = attach_asset(actor=self.user, series_id=series.pk, asset_id=asset.pk,
            source="existing", setting_revision_id=setting.pk)
        if position == 0:
            use = self.use
            use.use_key, use.setting_revision, use.reference_version = key, setting, version
            use.save(update_fields=("use_key", "setting_revision", "reference_version"))
        else:
            use = ShotReferenceUse.objects.create(shot=self.shot, use_key=key, position=position,
                media_kind="image", purpose="自由创作描述，不是模型枚举", source_mode="existing",
                setting_revision=setting, reference_version=version, asset_sha256=asset.checksum)
        return use, version

    def prepare(self, key):
        return create_execution_instance_preparation(owner_ref=("asset_job_id", self.job.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            unit_key="composition", request_key=key,
            intent=json_contract({"inputs": self.sources, "parameters": self.parameter_values}),
            resolve_facts=self.resolve, actor=self.user)

    def complete_preparation(self, key):
        prep = self.prepare(key)
        facts = deepcopy(prep.contract.resolved_inputs)
        batch = facts["field_requests"][0]
        refs = batch["value"]["items"][0]["reference_facts"]["logical_inputs"]
        child, created = create_asset_field_child(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, field_batch_key=batch["sha256"],
            attempt_number=1, actor=self.user)
        self.assertTrue(created)
        child_prep = child.execution_preparations.get()
        wire = compile_prepared_execution_transport(preparation_id=child_prep.pk,
            expected_preparation_revision=child_prep.revision, actor=self.user).transport.value
        self.assertEqual(json.loads(wire["payload"]["messages"][1]["content"]), batch["value"])
        prep.refresh_from_db()
        # Explicit manual contribution; not a fabricated model result.
        text = "Café turns. Preserve the author's exact words — without rewriting."
        response = self.client.post("/api/v1/execution-field-values/", {
            "preparation_id": str(prep.pk), "unit_key": "composition", "field_key": "configured_action",
            "text": text, "revision": 1, "expected_preparation_revision": prep.revision}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        field = response.data["field"]
        prep.refresh_from_db()
        response = self.client.post(f"/api/v1/execution-field-values/{field['id']}/confirm/", {
            "expected_text_sha256": field["text_sha256"], "expected_source_sha256": field["source_sha256"],
            "expected_preparation_revision": prep.revision, "select_for_execution": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        prep.refresh_from_db()
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.user)
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=evaluated.revision, actor=self.user).transport.value
        fulltext = transport["prompt_channels"]["values"]["positive_prompt"]
        self.assertIn(text, fulltext)
        self.assertNotIn("NOT_IMPLICIT_PROMPT_", fulltext)
        uses = read_execution_workflow_uses(self.definition, json_contract(facts["inputs"]))
        display = frozen_reference_display(facts, uses)
        bindings = transport["payload"]["media_preparation"]["bindings"]
        for index, ref in enumerate(refs):
            self.assertIn(ref["entity_label"], fulltext)
            self.assertIn(ref["input_label"], fulltext)
            self.assertEqual(display[ref["item_key"]]["reference_name"], ref["entity_label"])
            self.assertEqual(display[ref["item_key"]]["shot_reference"], ref["shot_reference"])
            self.assertEqual(bindings[index]["item_key"], ref["item_key"])
            graph = transport["payload"]["compiled_workflow"]["graph"]
            self.assertEqual(graph[f"cr_images_{index}"]["inputs"]["image"], bindings[index]["token"])
        response = self.client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["resolved_inputs"], facts)
        detail = response.json()
        response = self.client.get(f"/api/v1/execution-preparations/{prep.pk}/prompt-display/",
            {"expected_preparation_revision": evaluated.revision})
        self.assertEqual(response.status_code, 200, response.data)
        prompt = response.json()
        self.assertEqual(prompt["mode"], "preview")
        self.assertEqual(next(row["text"] for row in prompt["channels"] if row["key"] == "positive_prompt"), fulltext)
        self.assertEqual([row["name"] for row in prompt["sources"]["references"]], [row["entity_label"] for row in refs])
        response = self.client.get(f"/api/v1/execution-preparations/{prep.pk}/storage-metrics/",
            {"expected_preparation_revision": evaluated.revision})
        self.assertEqual(response.status_code, 200, response.data)
        resolved = next(row for row in response.json()["items"] if row["key"] == "resolved_inputs")
        self.assertEqual(resolved["size_bytes"], json_contract(facts).size_bytes)
        self.assertEqual(resolved["sha256"], json_contract(facts).sha256)
        self.receipts.append({"order": key, "preparation": detail, "prompt": prompt,
            "metrics": response.json(), "correspondence": display, "media": self.media_previews(refs)})
        child.refresh_from_db()
        self.assertIsNone(child.submission_started_at)
        self.assertEqual(child.result_text, "")
        return prep, refs, facts, fulltext

    def media_previews(self, refs):
        result = []
        for row in refs:
            asset = next(asset for asset in self.assets if str(asset.pk) == row["asset_id"])
            with asset.file.open("rb") as handle:
                content = handle.read()
            self.assertEqual(sha256(content).hexdigest(), asset.checksum)
            result.append({"item_key": row["item_key"], "label": row["input_label"],
                "asset_id": str(asset.pk), "sha256": asset.checksum,
                "data_url": "data:image/png;base64," + b64encode(content).decode("ascii")})
        return result

    def assert_combination(self, names, same_entity):
        entities = [CharacterBible.objects.create(project=self.project_row, name=name,
            slug=f"entity-{index}", stable_speaker_id=f"S{index + 1}") for index, name in enumerate(names)]
        self.sources["units"]["value"]["ordered_units"][0]["subject_keys"] = [
            ["character", str(entity.pk)] for entity in entities]
        if same_entity:
            primary = self.setting(entities[0], "主形象")
            parent = AssetReferenceSeries.objects.create(owner=self.user, name="Primary", role="primary",
                bible_variant=primary.variant)
            settings = [self.setting(entities[0], key, primary) for key in ("校服", "外套")]
            selected = [self.reference_use(key, setting, i, parent)
                for i, (key, setting) in enumerate(zip(("uniform", "coat"), settings))]
            self.assertNotEqual(settings[0].variant_id, settings[1].variant_id)
            self.assertEqual({setting.primary_setting_revision_id for setting in settings}, {primary.pk})
        else:
            selected = [self.reference_use(f"named-{i}", self.setting(entity, f"主形象{i}"), i)
                for i, entity in enumerate(entities)]
        expected = resolve_shot_reference_sources(actor=self.user, project_id=self.project_row.pk,
            shot_ids=[str(self.shot.pk)]).value["shots"][0]["uses"]
        sources, excluded = expand_reviewed_reference_sources(specs=[self.spec],
            sources={"references": mapping("images")}, records=reference_use_records(expected), shot_id=str(self.shot.pk))
        self.assertEqual(excluded, [])
        self.sources["references"]["selection"]["items"] = sources["references"]["items"]
        before = (GenerationAttempt.objects.count(), GenerationTask.objects.count())
        first, refs, facts, text = self.complete_preparation("forward")
        self.assertEqual(len(refs), 2)
        self.assertEqual([row["shot_reference"] for row in refs], expected)
        self.assertEqual([row["original_name"] for row in refs], names * 2 if same_entity else names)
        self.assertTrue(all(row["name_disambiguated"] for row in refs))
        self.assertEqual(len({row["entity_label"] for row in refs}), 2)
        self.assertEqual(len({row["entity_id"] for row in refs}), 1 if same_entity else 2)
        self.assertEqual([row["shot_reference"]["source"]["reference_version_id"] for row in refs],
            [str(version.pk) for _, version in selected])
        self.sources["references"]["selection"]["items"].reverse()
        for index, item in enumerate(self.sources["references"]["selection"]["items"]):
            item["order"] = index
        # A new authored order is a new isolated root, not an overwrite or a
        # retry of the original preparation's business request.
        self.job = AssetGenerationJob.objects.create(owner=self.user, model_deployment=self.deployment,
            execution_instance_version=self.version, workflow_template_version=self.workflow,
            execution_kind="comfy_workflow", job_type="asset", media_type="video", operation=self.version.operation,
            prompt="", idempotency_key="m6-reordered-root", request_snapshot_sha256=json_contract({}).sha256,
            allow_external_calls=True)
        _, reverse, _, _ = self.complete_preparation("reversed")
        for index, row in enumerate(reverse):
            original = refs[1 - index]
            self.assertEqual(row["item_key"], original["item_key"])
            self.assertEqual(row["entity_label"], original["entity_label"])
            self.assertEqual(row["shot_reference"], original["shot_reference"])
            self.assertEqual(row["input_label"], f"<Picture {index + 1}>")
        self.asset.name = "Later name must not overwrite the preparation"
        self.asset.save(update_fields=("name",))
        for entity in entities:
            entity.name = "Later entity name"
            entity.save(update_fields=("name",))
        with patch("studio.services.shot_reference_sources.resolve_shot_reference_sources",
                side_effect=AssertionError("No current selection on frozen reload")):
            response = self.client.get(f"/api/v1/execution-preparations/{first.pk}/")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.json()["resolved_inputs"], facts)
            replay = compile_prepared_execution_transport(preparation_id=first.pk,
                expected_preparation_revision=response.data["revision"], actor=self.user).transport.value
            self.assertEqual(replay["prompt_channels"]["values"]["positive_prompt"], text)
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), before)
        # Optional generated API receipts for the browser fixture. Never reads
        # a live DB; export only after every combination assertion succeeded.
        if directory := os.environ.get("M6_ACCEPTANCE_RECEIPTS"):
            output = Path(directory)
            output.mkdir(parents=True, exist_ok=True)
            (output / ("variants.json" if same_entity else "names.json")).write_text(
                json.dumps(self.receipts, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_multilingual_normalized_names_keep_two_entities_through_full_prompt_and_reorder(self):
        self.assert_combination(["Ｃafé", "Cafe\u0301"], same_entity=False)

    def test_one_entity_two_distinct_variants_keep_versions_settings_full_prompt_and_reorder(self):
        self.assert_combination(["林岚"], same_entity=True)
