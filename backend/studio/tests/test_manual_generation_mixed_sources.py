"""Real frontend builder -> formal manual API -> frozen mixed-use facts.

All uploads/reviews are isolated test records. Local FFmpeg fixtures only;
the inherited fixture forbids providers and ComfyUI submission.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework.renderers import JSONRenderer

from studio.models import Asset, Episode, ExecutionPreparation, GenerationAttempt, GenerationTask, ModelExecutionInstanceVersion, ProductionRun, SeriesProject, Shot, ShotReferenceUse
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import update_execution_draft, publish_execution_version
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.production_stage_catalog import production_stage_catalog
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests import test_execution_media_processing as media_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ManualGenerationMixedSourcesTests(TestCase):
    _create_version = media_fixtures.ExecutionMediaProcessingTests._create_version
    compile = media_fixtures.ExecutionMediaProcessingTests.compile
    stage = media_fixtures.ExecutionMediaProcessingTests.stage
    persist = media_fixtures.ExecutionMediaProcessingTests.persist
    embedded_sources = media_fixtures.ExecutionMediaProcessingTests.embedded_sources

    def setUp(self):
        media_fixtures.ExecutionMediaProcessingTests.setUp(self)
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=("is_active",))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_frontend_mixed_uses_upload_guard_reorder_and_embedded_audio_freeze_without_dispatch(self):
        # The upload endpoint, not an authored provenance label, owns this fact.
        response = self.client.post("/api/v1/assets/", {"name": "Isolated uploaded picture", "kind": "reference",
            "file": SimpleUploadedFile("picture.png", Path(self.asset.file.path).read_bytes(), content_type="image/png")}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        uploaded = Asset.objects.get(pk=response.data["id"])
        embedded = self.embedded_sources()
        second_video = Asset.objects.get(pk=embedded["clips"]["items"][1]["asset_id"])
        for asset, mime in ((self.originals[0], "video/mp4"), (second_video, "video/mp4"), (self.originals[2], "audio/wav")):
            asset.mime_type = mime
            asset.save(update_fields=("mime_type",))
        project = SeriesProject.objects.create(owner=self.user, title="Isolated mixed shot", **PROJECT_CONFIG)
        episode = Episode.objects.create(project=project, number=1, title="First")
        shot = Shot.objects.create(episode=episode, number=1, title="Mixed uses", duration_ms=1000, media_type="video", include_audio=True)
        for index, (key, kind, asset, purpose, mode) in enumerate((
            ("picture", "image", uploaded, "外观，不是首帧", "upload"),
            ("video-a", "video", self.originals[0], "走位参考", "existing"),
            ("video-b", "video", second_video, "运镜参考", "existing"),
            ("audio-sync", "audio", self.originals[2], "配套音轨", "existing"),
            ("audio-ambient", "audio", self.originals[2], "同文件环境参考", "existing"),
        )):
            ShotReferenceUse.objects.create(shot=shot, use_key=key, position=index, media_kind=kind,
                purpose=purpose, source_mode=mode, asset=asset, asset_sha256=asset.checksum)
        version = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        specs = deepcopy(self.definition.value["configuration"]["inputs"])
        for spec in specs:
            spec["semantic_contract"]["allowed_scope_kinds"] = ["shot"]
        specs[0]["media_source_policy"] = {"schema_version": 1, "allowed_sources": ["asset_upload"],
            "default_source": "asset_upload", "allow_override": False, "order": "provided"}
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision,
            actor=self.user, changes={"inputs": specs})
        definition = build_execution_definition(version.pk)
        publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.user)
        catalog = production_stage_catalog()
        response = self.client.post("/api/v1/production-runs/", {"project_id": str(project.pk), "plan_source": "manual",
            "idempotency_key": "mixed-run", "allow_external_calls": False, "config_overrides": {}, "manual_plan": {
                "stage_catalog_key": catalog.value["key"], "expected_stage_catalog_sha256": catalog.sha256,
                "scope": {"kind": "project"}, "delivery": {"kind": "episode_masters"}, "initial_content": None}}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        run = ProductionRun.objects.get(pk=response.data["id"])
        base = f"/api/v1/production-runs/{run.pk}/"
        targets = self.client.get(base + "targets/", {"kind": "shot"}).data
        target = targets["results"][0]
        def action(stage, payload, revision, key):
            return self.client.post(base + "manual-actions/", {"action": "adopt" if stage == "storyboard" else "approve",
                "stage_key": stage, "unit_key": f"project:{project.pk}", "scope": {"kind": "project", "id": str(project.pk)},
                "expected_run_plan_sha256": targets["run_plan_sha256"], "expected_selection_revision": revision,
                "idempotency_key": key, "payload": payload}, format="json")
        detail = self.client.get(base + "manual-storyboard/").data
        response = action("storyboard", {"source": {"kind": "existing_storyboard"}, "expected_content_sha256": detail["content_sha256"],
            "reviewed_shot_ids": [str(shot.pk)], "comment": "Isolated agent operation"}, detail["adoption"]["revision"], "adopt-mixed")
        self.assertEqual(response.status_code, 201, response.data)
        detail = self.client.get(base + "manual-storyboard-review/").data
        self.assertTrue(detail["ready"], detail)
        response = action("storyboard_review", {"expected_contract_sha256": detail["contract_sha256"],
            "reviewed_shot_ids": [str(shot.pk)], "comment": "Isolated agent operation"}, detail["approval"]["revision"], "approve-mixed")
        self.assertEqual(response.status_code, 201, response.data)
        inputs = {
            "references": [{"key": "picture", "reference": "shot-use:picture"}],
            "clips": [{"key": key, "reference": "shot-use:" + key, "purpose": "motion_camera_reference"} for key in ("video-a", "video-b")],
            "paired": [{"key": "sync", "reference": "shot-use:audio-sync", "purpose": "audio_reference", "pairedTo": "video-b"}],
            "ambient": [{"key": "ambient", "reference": "shot-use:audio-ambient", "purpose": "audio_reference"}],
        }
        reordered = deepcopy(inputs)
        reordered["clips"].reverse()
        original = deepcopy(reordered)
        original["paired"] = [{"key": "original", "videoAudio": True, "purpose": "audio_reference", "pairedTo": "video-b"}]
        root = Path(__file__).resolve().parents[3]
        node = root / ".runtime/toolchains/node-v22.23.2-win-x64/node.exe"
        bridge = root / "frontend/tests/fixtures/manualGenerationRequestBridge.mjs"
        def read(url):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, (url, response.data))
            return response.data
        request = {"run": read(base), "review": read(base + "manual-storyboard-review/"),
            "contract": read(f"/api/v1/model-execution-instance-versions/{version.pk}/input-contract/"),
            "options": read(base + "manual-action-options/"), "target": target,
            "parameters": {key: value for key, value in self.parameters["values"].items() if self.parameters["sources"][key] == "request"},
            "channels": {"positive_prompt": "A parcel opens."}, "variants": [
                {"key": "mixed-first", "inputs": inputs}, {"key": "mixed-reordered", "inputs": reordered}, {"key": "mixed-original", "inputs": original}]}
        completed = subprocess.run([str(node), str(bridge)], input=JSONRenderer().render(request).decode("utf-8"),
            text=True, encoding="utf-8", capture_output=True, timeout=45)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        bodies = json.loads(completed.stdout)
        before = (GenerationAttempt.objects.count(), GenerationTask.objects.count())
        frozen, prepared_ids, media_plans = [], [], []
        for body in bodies:
            response = self.client.post(base + "manual-actions/", body, format="json")
            self.assertEqual(response.status_code, 202, response.data)
            prep = ExecutionPreparation.objects.get(pk=response.data["target_id"])
            self.assertIsNone(prep.next_check_at)
            frozen.append(prep.contract.resolved_inputs["inputs"]["logical_values"])
            prepared_ids.append(str(prep.pk))
            evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
            compiled = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=evaluated.revision, actor=self.user)
            media_plans.append(compiled.transport.value["payload"]["media_preparation"])
        first, reordered, original = frozen
        self.assertEqual(first["paired"][0]["asset_id"], first["ambient"][0]["asset_id"])
        self.assertNotEqual(first["paired"][0]["item_key"], first["ambient"][0]["item_key"])
        for position, (actual, previous) in enumerate(zip(reordered["clips"], reversed(first["clips"]))):
            self.assertEqual(actual["position"], position)
            # Array position is frozen into each source hash; identity and all
            # other facts survive reordering, but the position/hash must change.
            self.assertNotEqual(actual["source_sha256"], previous["source_sha256"])
            self.assertEqual(
                {key: value for key, value in actual.items() if key not in {"position", "source_sha256"}},
                {key: value for key, value in previous.items() if key not in {"position", "source_sha256"}},
            )
        self.assertEqual(reordered["paired"], first["paired"])
        audio = original["paired"][0]
        self.assertEqual(audio["scope_ref"], target["scope"])
        self.assertIsNone(audio["asset_id"])
        self.assertEqual(audio["origin"]["parent_item_key"], original["clips"][0]["item_key"])
        self.assertNotIn("shot_reference", audio)
        for values, plan in zip(frozen, media_plans):
            self.assertEqual([row["item_key"] for row in plan["bindings"] if row["kind"] == "video"],
                [row["item_key"] for row in values["clips"]])
        self.assertEqual(len(media_plans[0]["bindings"]), 5)
        self.assertEqual(len(media_plans[2]["bindings"]), 4)
        self.assertEqual(media_plans[2]["classified_groups"]["videos"][0]["paired_audio"], {"kind": "embedded"})
        bad = deepcopy(bodies[0]); bad["idempotency_key"] = "not-uploaded"
        bad["payload"]["inputs"]["references"]["selection"]["items"][0]["asset_id"] = str(self.asset.pk)
        response = self.client.post(base + "manual-actions/", bad, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("上传", str(response.data))
        use = ShotReferenceUse.objects.get(shot=shot, use_key="audio-sync")
        use.purpose = "Changed after original preparation"
        use.save(update_fields=("purpose",))
        replay = self.client.post(base + "manual-actions/", bodies[0], format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["target_id"], prepared_ids[0])
        fresh = deepcopy(bodies[0]); fresh["idempotency_key"] = "stale-new-request"
        self.assertEqual(self.client.post(base + "manual-actions/", fresh, format="json").status_code, 400)
        self.assertEqual(ExecutionPreparation.objects.get(pk=prepared_ids[0]).contract.resolved_inputs["inputs"]["logical_values"], first)
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), before)
