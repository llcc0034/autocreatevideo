"""Real manual root + local Asset bytes -> original selection and approval."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
import wave
import subprocess

from PIL import Image
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import Asset, ArtifactSelectionDecision, ApprovalDecision, ExecutionPreparation, GeneratedArtifact, GenerationAttempt, GenerationTask, ProductionManualCommand, ProductionRun
from studio.tests import test_production_manual_creation as creation
from studio.services.execution_selected_outputs import resolve_selected_execution_media
from studio.services.production_execution_sources import execution_source_stage_ids


class AdoptedMediaTests(TestCase):
    create = creation.ProductionManualCreationTests.create

    def setUp(self):
        creation.ProductionManualCreationTests.setUp(self)
        self.workspace = self.enterContext(TemporaryDirectory())
        self.enterContext(override_settings(MEDIA_ROOT=self.workspace, ASSET_LIBRARY_ROOT=str(Path(self.workspace) / "library")))
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        self.run = ProductionRun.objects.get(pk=response.data["id"])
        self.stage = self.run.stages.get(stage_key="media_generation")
        self.plan_sha = response.data["execution_plan"]["sha256"]
        self.url = f"/api/v1/production-runs/{self.run.pk}/manual-actions/"
        self.inventory_url = f"/api/v1/production-runs/{self.run.pk}/manual-media-review/"

    def asset(self, kind="image", *, owner=None):
        stream = BytesIO()
        if kind == "image":
            Image.new("RGB", (16, 24), (30, 60, 90)).save(stream, format="PNG")
            name, mime, asset_kind = "actual.png", "image/png", "reference"
        elif kind == "audio":
            with wave.open(stream, "wb") as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000); output.writeframes(b"\0\0" * 8000)
            name, mime, asset_kind = "actual.wav", "audio/wav", "reference_audio"
        elif kind == "video":
            path=Path(self.workspace,"synthetic.mp4")
            subprocess.run([settings.FFMPEG_BINARY,"-y","-hide_banner","-loglevel","error","-f","lavfi",
                "-i","color=c=blue:s=16x24:r=2","-t","1","-c:v","libx264","-pix_fmt","yuv420p","-an",str(path)],
                check=True,capture_output=True,timeout=20)
            stream.write(path.read_bytes())
            name,mime,asset_kind="actual.mp4","video/mp4","reference_video"
        else:
            raise AssertionError("Unsupported fixture media")
        content = stream.getvalue()
        asset = Asset(owner=owner or self.actor, name=name, kind=asset_kind, mime_type=mime, checksum=sha256(content).hexdigest())
        asset.file.save(name, ContentFile(content), save=True)
        return asset

    def adoption_request(self, asset, *, revision=0, key="adopt-asset", kind="image"):
        return {"action":"adopt","stage_key":"media_generation","scope":{"kind":"project","id":str(self.project.pk)},
            "unit_key":f"project:{self.project.pk}","expected_run_plan_sha256":self.plan_sha,
            "expected_selection_revision":revision,"idempotency_key":key,
            "payload":{"source":{"kind":"asset","asset_id":str(asset.pk),"expected_sha256":asset.checksum,"media_kind":kind}}}

    def post(self, body):
        return self.client.post(self.url, body, format="json")

    def test_adopt_real_image_without_any_model_objects_and_reuse_original_decision(self):
        asset = self.asset()
        body = self.adoption_request(asset)
        response = self.post(body)
        self.assertEqual(response.status_code, 201, response.data)
        artifact = GeneratedArtifact.objects.get()
        self.assertEqual((artifact.asset_id, artifact.role, artifact.status), (asset.pk, "adopted_media", "selected"))
        self.assertIsNone(artifact.generation_attempt_id); self.assertIsNone(artifact.generation_task_id)
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists()); self.assertFalse(GenerationTask.objects.exists())
        with self.assertRaisesMessage(ValidationError,"尚未审核"):
            resolve_selected_execution_media(decision_id=response.data["target_id"],artifact_id=str(artifact.pk),owner=self.actor,
                production_run_id=str(self.run.pk),eligible_stage_ids=[self.stage.pk],expected_kind="image",max_media_bytes=65536)
        self.stage.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual((self.stage.status,self.run.status), ("pending","awaiting_input"))
        self.assertEqual(self.post(body).data["target_id"], response.data["target_id"])
        repeated = self.post(self.adoption_request(asset, revision=1, key="same-asset"))
        self.assertEqual(repeated.status_code, 201, repeated.data)
        self.assertEqual(repeated.data["target_id"], response.data["target_id"])
        self.assertEqual(GeneratedArtifact.objects.count(),1); self.assertEqual(ArtifactSelectionDecision.objects.count(),1)
        detail = self.client.get(f"/api/v1/generated-artifacts/{artifact.pk}/")
        self.assertEqual(detail.status_code,200,detail.data)
        self.assertEqual(detail.data["manual_media_candidate"]["source_kind"],"asset")
        self.assertIsNone(detail.data["manual_media_candidate"]["preparation_id"])
        self.assertIsNone(detail.data["manual_media_candidate"]["request_sha256"])
        preview=self.client.get(detail.data["preview_url"])
        try:
            self.assertEqual(preview.status_code,200)
            self.assertEqual(sha256(b"".join(preview.streaming_content)).hexdigest(),asset.checksum)
        finally:
            preview.close()
        # The file copy remains the frozen result even if the original upload is removed.
        Path(asset.file.path).unlink()
        self.assertEqual(self.client.get(f"/api/v1/generated-artifacts/{artifact.pk}/").status_code,200)

    def test_adopted_audio_can_be_reviewed_without_completing_producer_or_assembly(self):
        response = self.post(self.adoption_request(self.asset("audio"),kind="audio"))
        self.assertEqual(response.status_code,201,response.data)
        inventory = self.client.get(self.inventory_url)
        self.assertEqual(inventory.status_code,200,inventory.data)
        data = inventory.data
        request = {"action":"approve","stage_key":"artifact_selection","scope":{"kind":"project","id":str(self.project.pk)},
            "unit_key":f"project:{self.project.pk}","expected_run_plan_sha256":self.plan_sha,"expected_selection_revision":0,
            "idempotency_key":"review-asset-audio","payload":{"selection":data["selection"],"expected_selection_sha256":data["selection_sha256"],"comment":"已核对声音"}}
        response = self.post(request)
        self.assertEqual(response.status_code,201,response.data)
        self.assertEqual(self.client.get(self.inventory_url).data["approval"]["status"],"current")
        self.assertEqual(ApprovalDecision.objects.count(),1)
        decision = ArtifactSelectionDecision.objects.get()
        selected = resolve_selected_execution_media(decision_id=str(decision.pk),artifact_id=str(decision.artifact_id),owner=self.actor,
            production_run_id=str(self.run.pk),eligible_stage_ids=execution_source_stage_ids(self.stage),expected_kind="audio",max_media_bytes=65536)
        self.assertTrue(selected.path.is_file())
        self.assertEqual(selected.media_facts["observed_media"]["duration_ms"],1000)
        self.assertFalse(GenerationAttempt.objects.exists())
        changed=self.post(self.adoption_request(self.asset("audio"),kind="audio",revision=1,key="change-after-review"))
        self.assertEqual(changed.status_code,201,changed.data)
        self.assertEqual(self.client.get(self.inventory_url).data["approval"]["status"],"changed")
        with self.assertRaisesMessage(ValidationError,"选片、上游内容及审核依据已变化"):
            resolve_selected_execution_media(decision_id=str(decision.pk),artifact_id=str(decision.artifact_id),owner=self.actor,
                production_run_id=str(self.run.pk),eligible_stage_ids=execution_source_stage_ids(self.stage),expected_kind="audio",max_media_bytes=65536)
        self.assertFalse(self.run.assembly_links.exists())
        self.stage.refresh_from_db(); self.assertEqual(self.stage.status,"pending")

    def test_wrong_source_type_digest_scope_or_selection_revision_is_rejected(self):
        asset = self.asset()
        body = self.adoption_request(asset)
        invalid = [dict(body,unit_key="guess-the-shot"), dict(body,expected_selection_revision=1), dict(body,scope={"kind":"project","id":str(uuid4())})]
        for key,value in (("expected_sha256","0"*64),("media_kind","video"),("media_kind",[]),("asset_id",str(uuid4())),("workflow_version_id",str(uuid4()))):
            changed=deepcopy(body);changed["payload"]["source"][key]=value;invalid.append(changed)
        for changed in invalid:
            with self.subTest(changed=changed): self.assertIn(self.post(changed).status_code,(400,409))
        self.assertFalse(GeneratedArtifact.objects.exists()); self.assertFalse(ProductionManualCommand.objects.exists())

    def test_video_and_authorized_global_asset_use_actual_probe_and_original_signed_media(self):
        asset=self.asset("video")
        response=self.post(self.adoption_request(asset,kind="video"))
        self.assertEqual(response.status_code,201,response.data)
        artifact=GeneratedArtifact.objects.get()
        facts=artifact.manifest_metadata["observed_media"]
        self.assertEqual((facts["width"],facts["height"],facts["duration_ms"]),(16,24,1000))
        detail=self.client.get(f"/api/v1/generated-artifacts/{artifact.pk}/")
        self.assertEqual(detail.status_code,200,detail.data)
        preview=self.client.get(detail.data["preview_url"])
        try:
            self.assertEqual(preview.status_code,200)
            self.assertEqual(sha256(b"".join(preview.streaming_content)).hexdigest(),asset.checksum)
        finally:
            preview.close()
        image=self.asset()
        shared=Path(settings.ASSET_LIBRARY_ROOT,"shared.png");shared.parent.mkdir(parents=True,exist_ok=True)
        shared.write_bytes(Path(image.file.path).read_bytes())
        Asset.objects.filter(pk=image.pk).update(owner=None,file="",local_path=str(shared))
        response=self.post(self.adoption_request(image,revision=1,key="global-source"))
        self.assertEqual(response.status_code,201,response.data)
        shared_artifact=GeneratedArtifact.objects.get(status="selected")
        self.assertIsNone(shared_artifact.manifest_metadata["asset_owner_id"])
        self.assertEqual(self.client.get(f"/api/v1/generated-artifacts/{shared_artifact.pk}/").status_code,200)

    def test_other_owner_inactive_cover_and_corrupt_source_are_not_adopted(self):
        other = self.asset(owner=self.other)
        self.assertEqual(self.post(self.adoption_request(other)).status_code,400)
        for changes in ({"is_active":False},{"metadata":{"usage":"project_cover"}},{"checksum":"0"*64}):
            asset=self.asset(); Asset.objects.filter(pk=asset.pk).update(**changes)
            self.assertEqual(self.post(self.adoption_request(asset)).status_code,400)
        self.assertFalse(GeneratedArtifact.objects.exists()); self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_frozen_run_size_limit_cannot_be_changed_without_its_contract(self):
        asset=self.asset()
        changed=deepcopy(self.run.config_snapshot)
        changed["values"]["artifact.max_bytes"]=999999999
        ProductionRun.objects.filter(pk=self.run.pk).update(config_snapshot=changed)
        response=self.post(self.adoption_request(asset))
        self.assertEqual(response.status_code,400,response.data)
        self.assertFalse(GeneratedArtifact.objects.exists());self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_receipt_failure_rolls_back_db_and_retry_reuses_exact_archived_bytes(self):
        asset=self.asset(); body=self.adoption_request(asset)
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save",side_effect=RuntimeError("receipt failure")):
            with self.assertRaises(RuntimeError): self.post(body)
        self.assertFalse(GeneratedArtifact.objects.exists()); self.assertFalse(ArtifactSelectionDecision.objects.exists())
        archived=list((Path(self.workspace)/"generated-artifacts").rglob(asset.checksum))
        self.assertEqual(len(archived),1)
        before=archived[0].stat().st_mtime_ns
        response=self.post(body); self.assertEqual(response.status_code,201,response.data)
        self.assertEqual(archived[0].stat().st_mtime_ns,before)

    def test_new_asset_revision_and_corrupt_archive_are_not_repaired(self):
        first=self.asset(); self.assertEqual(self.post(self.adoption_request(first)).status_code,201)
        second=self.asset()
        response=self.post(self.adoption_request(second,revision=1,key="replace"))
        self.assertEqual(response.status_code,201,response.data)
        self.assertEqual(GeneratedArtifact.objects.count(),2)
        self.assertEqual(ArtifactSelectionDecision.objects.count(),2)
        adopted=GeneratedArtifact.objects.get(status="selected")
        Path(self.workspace,adopted.object_key).write_bytes(b"corrupt archive")
        response=self.post(self.adoption_request(second,revision=2,key="cannot-repair"))
        self.assertEqual(response.status_code,400,response.data)
        self.assertEqual(ArtifactSelectionDecision.objects.count(),2)
