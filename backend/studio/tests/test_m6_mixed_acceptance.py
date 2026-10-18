"""M6-A03: mixed local media through auxiliary, approval, full prompt and APIs.

Isolated records and FFmpeg test signals only. No model, dispatch or live data.
This extends the input-only manual combination with a published Profile chain.
"""
from base64 import b64encode
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from studio.models import Asset, AssetGenerationJob, GenerationAttempt, GenerationTask, ShotReferenceUse
from studio.services.execution_asset_auxiliaries import create_asset_field_child
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_instances import clone_execution_draft, publish_execution_version, update_execution_draft
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_preparations import ResolvedExecutionPreparation
from studio.services.execution_profile_preparation import prepare_execution_profile
from studio.services.execution_reference_display import frozen_reference_display
from studio.services.execution_workflow_uses import read_execution_workflow_uses
from studio.services.json_contracts import json_contract
from studio.services.production_reference_mapping import reviewed_reference_item_key
from studio.tests import test_m6_reference_acceptance as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class M6MixedReferenceAcceptanceTests(TestCase):
    _create_version = fixtures.M6ReferenceAcceptanceTests._create_version
    selection = fixtures.M6ReferenceAcceptanceTests.selection
    configure = fixtures.M6ReferenceAcceptanceTests.configure
    prepare = fixtures.M6ReferenceAcceptanceTests.prepare

    def setUp(self):
        fixtures.M6ReferenceAcceptanceTests.setUp(self)
        draft = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        specs = deepcopy(self.definition.value["configuration"]["inputs"])
        for key, kind, target, maximum, parent in (
            ("clips", "video", "videos", 2, None),
            ("paired", "audio", "video_audios", 1, "clips"),
            ("ambient", "audio", "audios", 1, None),
        ):
            spec = deepcopy(specs[0])
            spec.update(key=key, position=len(specs), kind=kind, min_items=1, max_items=maximum)
            spec["targets"] = [{"target_kind": "workflow_slot", "target_key": target, "is_primary": True}]
            spec["semantic_contract"].update(allowed_purposes=["motion_camera_reference" if kind == "video" else "audio_reference"],
                paired_with_input_key=parent, allowed_subject_kinds=[])
            specs.append(spec)
        draft = update_execution_draft(version_id=draft.pk, expected_revision=draft.revision, actor=self.user,
            changes={"inputs": specs})
        self.definition = build_execution_definition(draft.pk)
        self.version = publish_execution_version(version_id=draft.pk, expected_revision=draft.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        self.job.execution_instance_version = self.version
        self.job.save(update_fields=("execution_instance_version",))
        self.media_by_id = {str(self.asset.pk): self.asset}
        with TemporaryDirectory() as directory:
            for name, source, codec, mime in (
                ("walk.mp4", "testsrc2=size=64x48:rate=24", "libx264", "video/mp4"),
                ("camera.mp4", "color=c=orange:size=64x48:rate=24", "libx264", "video/mp4"),
                ("sound.wav", "sine=frequency=440:sample_rate=48000", "pcm_s16le", "audio/wav"),
            ):
                output = Path(directory) / name
                result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", source, "-t", "1", "-c", codec, str(output)], capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                content = output.read_bytes()
                asset = Asset(owner=self.user, name=name, kind="reference", mime_type=mime,
                    checksum=sha256(content).hexdigest(), description="NOT_IMPLICIT_APPEARANCE", prompt_hint="NOT_IMPLICIT_IMAGE_PROMPT")
                asset.file.save(name, ContentFile(content))
                self.media_by_id[str(asset.pk)] = asset
        media = list(self.media_by_id.values())
        self.declared_uses = {"references": [self.use]}
        for position, (slot, key, kind, asset, note) in enumerate((
            ("clips", "walk", "video", media[1], "走位原文"),
            ("clips", "camera", "video", media[2], "运镜原文"),
            ("paired", "sync", "audio", media[3], "与第二视频配套"),
            ("ambient", "ambience", "audio", media[3], "同文件独立环境音用途"),
        ), start=1):
            use = ShotReferenceUse.objects.create(shot=self.shot, use_key=key, position=position,
                media_kind=kind, purpose=note, source_mode="existing", asset=asset, asset_sha256=asset.checksum)
            self.declared_uses.setdefault(slot, []).append(use)
        for slot in ("clips", "paired", "ambient"):
            self.sources[slot] = {"selection": {"source": "asset_library", "items": [
                {"kind": "asset", "item_key": reviewed_reference_item_key(input_key=slot, shot_id=str(self.shot.pk),
                    reference_key="shot-use:" + use.use_key), "asset_id": str(use.asset_id),
                    "purpose": "motion_camera_reference" if slot == "clips" else "audio_reference",
                    "subject_ref": None, "scope_ref": {"kind": "shot", "id": str(self.shot.pk)},
                    "order": index, "paired_to": None} for index, use in enumerate(self.declared_uses[slot])]}}
        self.sources["paired"]["selection"]["items"][0]["paired_to"] = self.sources["clips"]["selection"]["items"][1]["item_key"]

    def resolve(self, definition, owner):
        inputs = resolve_execution_input_sources(definition, self.sources, owner=owner.owner,
            subject_facts=[], max_media_bytes=2000000, project_id=self.project_row.pk)
        parameters = initialize_execution_definition_parameters(definition, self.parameter_values, policies=[],
            resolve_source=lambda *_: self.fail("No implicit parameter source"))
        profile = prepare_execution_profile(definition, inputs, profile=self.prompt_profile, unit_key="composition",
            source_language="English", target_language="English", prepared_parameters=parameters,
            scope_units={("shot", str(self.shot.pk)): ["composition"]})
        return ResolvedExecutionPreparation(json_contract({**profile.value, "inputs": inputs.value, "parameters": parameters}),
            json_contract({"schema_version": 1, "mode": "manual"}))

    def complete(self, key):
        prep = self.prepare(key)
        facts = deepcopy(prep.contract.resolved_inputs)
        batch = facts["field_requests"][0]
        refs = batch["value"]["items"][0]["reference_facts"]["logical_inputs"]
        flattened = [part for row in refs for part in [row, *([row["audio_use"]] if "audio_use" in row else [])]]
        self.assertEqual(len(flattened), 5)
        self.assertEqual({row["input_label"] for row in flattened}, {"<Picture 1>", "<Video 1>", "<Video 2>", "<Audio 1>", "<Audio 2>"})
        child, created = create_asset_field_child(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            field_batch_key=batch["sha256"], attempt_number=1, actor=self.user)
        self.assertTrue(created)
        child_prep = child.execution_preparations.get()
        wire = compile_prepared_execution_transport(preparation_id=child_prep.pk,
            expected_preparation_revision=child_prep.revision, actor=self.user).transport.value
        self.assertEqual(json.loads(wire["payload"]["messages"][1]["content"]), batch["value"])
        self.assertNotIn("NOT_IMPLICIT_", str(wire))
        prep.refresh_from_db()
        text = "The parcel turns with <Video 1>; keep <Video 2> timing, <Audio 1> synchronized, and <Audio 2> independent."
        response = self.client.post("/api/v1/execution-field-values/", {"preparation_id": str(prep.pk),
            "unit_key": "composition", "field_key": "configured_action", "text": text, "revision": 1,
            "expected_preparation_revision": prep.revision}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        field = response.data["field"]
        prep.refresh_from_db()
        response = self.client.post(f"/api/v1/execution-field-values/{field['id']}/confirm/", {
            "expected_text_sha256": field["text_sha256"], "expected_source_sha256": field["source_sha256"],
            "expected_preparation_revision": prep.revision, "select_for_execution": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        prep.refresh_from_db()
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=evaluated.revision, actor=self.user).transport.value
        fulltext = transport["prompt_channels"]["values"]["positive_prompt"]
        self.assertIn(text, fulltext)
        self.assertNotIn("NOT_IMPLICIT_", fulltext)
        uses = read_execution_workflow_uses(self.definition, json_contract(facts["inputs"]))
        correspondence = frozen_reference_display(facts, uses)
        bindings = transport["payload"]["media_preparation"]["bindings"]
        self.assertEqual({row["item_key"] for row in bindings}, set(correspondence))
        graph = transport["payload"]["compiled_workflow"]["graph"]
        for row in bindings:
            self.assertIn(row["token"], json.dumps(graph))
        audios = [row for row in flattened if row["category"] == "audio"]
        self.assertEqual(audios[0]["asset_id"], audios[1]["asset_id"])
        self.assertNotEqual(audios[0]["shot_reference"], audios[1]["shot_reference"])
        self.assertNotEqual(audios[0]["entity_label"], audios[1]["entity_label"])
        base = f"/api/v1/execution-preparations/{prep.pk}/"
        response = self.client.get(base)
        self.assertEqual(response.status_code, 200, response.data)
        detail = response.json()
        self.assertEqual(detail["resolved_inputs"], facts)
        response = self.client.get(base + "prompt-display/", {"expected_preparation_revision": evaluated.revision})
        self.assertEqual(response.status_code, 200, response.data)
        prompt = response.json()
        self.assertEqual(prompt["mode"], "preview")
        self.assertEqual(next(row["text"] for row in prompt["channels"] if row["key"] == "positive_prompt"), fulltext)
        self.assertEqual(prompt["sources"]["references"], [{"label": row["input_label"], "name": row["entity_label"],
            "category": row["category"], "guidance": ""} for row in flattened])
        response = self.client.get(base + "storage-metrics/", {"expected_preparation_revision": evaluated.revision})
        self.assertEqual(response.status_code, 200, response.data)
        metrics = response.json()
        resolved = next(row for row in metrics["items"] if row["key"] == "resolved_inputs")
        self.assertEqual((resolved["size_bytes"], resolved["sha256"]), (json_contract(facts).size_bytes, json_contract(facts).sha256))
        previews = []
        for row in flattened:
            asset = self.media_by_id[row["asset_id"]]
            content = Path(asset.file.path).read_bytes()
            self.assertEqual(sha256(content).hexdigest(), asset.checksum)
            previews.append({"item_key": row["item_key"], "label": row["input_label"], "asset_id": str(asset.pk),
                "kind": row["category"], "sha256": asset.checksum,
                "data_url": "data:" + asset.mime_type + ";base64," + b64encode(content).decode("ascii")})
        self.receipts.append({"order": key, "preparation": detail, "prompt": prompt, "metrics": metrics,
            "correspondence": correspondence, "media": previews})
        child.refresh_from_db()
        self.assertIsNone(child.submission_started_at)
        self.assertEqual(child.result_text, "")
        self.assertIsNone(prep.request_frozen_at)
        return prep, flattened, facts, transport

    def test_mixed_media_full_field_chain_keeps_paired_and_independent_audio_in_prompt_sources_after_reorder(self):
        before = (GenerationAttempt.objects.count(), GenerationTask.objects.count())
        first, refs, facts, transport = self.complete("mixed-forward")
        self.sources["clips"]["selection"]["items"].reverse()
        for index, item in enumerate(self.sources["clips"]["selection"]["items"]):
            item["order"] = index
        self.job = AssetGenerationJob.objects.create(owner=self.user, model_deployment=self.deployment,
            execution_instance_version=self.version, workflow_template_version=self.workflow,
            execution_kind="comfy_workflow", job_type="asset", media_type="video", operation=self.version.operation,
            prompt="", idempotency_key="m6-mixed-reordered", request_snapshot_sha256=json_contract({}).sha256,
            allow_external_calls=True)
        _, reordered, _, _ = self.complete("mixed-reversed")
        for row in reordered:
            original = next(value for value in refs if value["item_key"] == row["item_key"])
            self.assertEqual(row["shot_reference"], original["shot_reference"])
            self.assertEqual(row["entity_label"], original["entity_label"])
        self.assertEqual(next(row for row in reordered if "audio_use" in row)["input_label"], "<Video 1>")
        for asset in self.media_by_id.values():
            asset.name = "Changed after original preparation"
            asset.save(update_fields=("name",))
        with patch("studio.services.shot_reference_sources.resolve_shot_reference_sources",
                side_effect=AssertionError("Frozen replay cannot select current uses")):
            response = self.client.get(f"/api/v1/execution-preparations/{first.pk}/")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.json()["resolved_inputs"], facts)
            replay = compile_prepared_execution_transport(preparation_id=first.pk,
                expected_preparation_revision=response.data["revision"], actor=self.user).transport.value
            self.assertEqual(replay, transport)
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), before)
        if directory := os.environ.get("M6_ACCEPTANCE_RECEIPTS"):
            output = Path(directory)
            output.mkdir(parents=True, exist_ok=True)
            (output / "mixed.json").write_text(json.dumps(self.receipts, ensure_ascii=False, indent=2), encoding="utf-8")
