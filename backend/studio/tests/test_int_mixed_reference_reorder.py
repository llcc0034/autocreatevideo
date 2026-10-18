"""INT F07: new authored mixed-media contract and explicit new input order.

Uploads and original preparation/finalization/readers are real and isolated.
Synthetic local signals and admission observations are not model/Smoke proof.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import subprocess
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from studio.models import Asset, AssetGenerationJob, ExecutionPreparation, GenerationAttempt, GenerationTask
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.json_contracts import JsonContract
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.reference_context_facts import read_reference_context_facts
from studio.tests.int_contract_fixtures import IntegrationContracts, input_spec
from studio.tests.reference_rendering_fixtures import rendering_config
from studio.tests.test_execution_asset_requests import freeze
from studio.tests.test_execution_media_source_policy import choices
from studio.tests import test_int_three_entry_journey as entries


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class IntegrationMixedReferenceReorderTests(IntegrationContracts, TestCase):
    setUp = entries.IntegrationThreeEntryJourneyTests.setUp
    tearDown = entries.IntegrationThreeEntryJourneyTests.tearDown
    upload = entries.IntegrationThreeEntryJourneyTests.upload

    def mixed_contract(self):
        template = self.video_workflow.template
        template.is_active = True
        template.save(update_fields=("is_active",))
        config = rendering_config()
        config["primitive_options"]["reference_context_ir_v1"]["rendering"]["reference_rules"].append({
            "category": "image", "purpose": "", "task": "reference generation",
            "relationship": "fully_preserved", "usage": "Preserve this explicitly supplied visual reference."})
        with patch("studio.tests.int_contract_fixtures.rendering_config", return_value=config):
            profile = self.int_profile("int-mixed-order-profile", self.video_model, "video.generate", "Ref2VA", reference=True)
        specs = []
        for key, kind, target, maximum, purpose, parent in (
            ("pictures", "image", "images", 1, None, None),
            ("clips", "video", "videos", 2, "motion_camera_reference", None),
            ("paired", "audio", "video_audios", 1, "audio_reference", "clips"),
            ("ambient", "audio", "audios", 1, "audio_reference", None),
        ):
            spec = input_spec(key, kind, {"target_kind": "workflow_slot", "target_key": target, "is_primary": True}, maximum=maximum)
            spec["position"] = len(specs)
            spec["media_source_policy"] = choices()
            spec["semantic_contract"].update(allowed_purposes=[purpose] if purpose else [], paired_with_input_key=parent)
            specs.append(spec)
        units = input_spec("units", "json", {"target_kind": "profile_input", "target_key": "reference_context.units", "is_primary": False})
        units["position"] = len(specs)
        specs.append(units)
        return self.int_instance("int-mixed-reference-order", self.video_deployment, self.video_workflow,
            profile, "video.reference_to_video", "Ref2VA", specs)

    def signal(self, name, source, codec, mime):
        output = self.int_root / name
        subprocess.run([settings.FFMPEG_BINARY, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", source,
            "-t", "1", "-c", codec, str(output)], check=True, capture_output=True, timeout=30)
        data = output.read_bytes()
        kind = Asset.Kind.REFERENCE_VIDEO if mime == "video/mp4" else Asset.Kind.REFERENCE_AUDIO
        response = self.client.post("/api/v1/assets/", {"name": "原名 " + name, "kind": kind,
            "file": SimpleUploadedFile(name, data, content_type=mime)}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        asset = Asset.objects.get(pk=response.data["id"])
        self.assertEqual(asset.checksum, sha256(data).hexdigest())
        return asset

    def create(self, key, target, inputs):
        version, contract = target
        body = {"execution_instance_version_id": str(version.pk), "expected_definition_sha256": contract["sha256"],
            "parameters": self.video_parameters, "inputs": inputs, "manual_channels": {}, "subject_facts": [],
            "source_language": "English", "target_language": "English", "job_type": "asset", "media_type": "video",
            "name": key, "field_values": []}
        receipt = self.api("post", "/api/v1/asset-generation-jobs/", body, status=202, HTTP_IDEMPOTENCY_KEY=key)
        job = AssetGenerationJob.objects.get(pk=receipt["id"])
        prep = freeze(job, self.actor)
        frozen = read_frozen_execution_request(prep)
        facts = prep.contract.resolved_inputs
        context = read_reference_context_facts(JsonContract(**facts["profile_sources"]["reference_context"]),
            max_bytes=execution_configuration_max_bytes())
        references = [part for row in context.reference_bindings for part in [row, *([row["audio_use"]] if "audio_use" in row else [])]]
        transport = frozen[2].value
        return {"key": key, "body": body, "receipt": receipt, "job": job, "prep": prep, "frozen": frozen,
            "facts": deepcopy(facts), "refs": references, "transport": transport}

    def test_new_mixed_contract_reorder_retains_use_identity_pairing_and_old_frozen_bytes(self):
        target = self.mixed_contract()
        picture = self.upload("原图，不推断首帧")
        clip_a = self.signal("motion.mp4", "testsrc2=size=64x48:rate=24", "libx264", "video/mp4")
        clip_b = self.signal("camera.mp4", "color=c=orange:size=64x48:rate=24", "libx264", "video/mp4")
        sound = self.signal("音轨.wav", "sine=frequency=440:sample_rate=48000", "pcm_s16le", "audio/wav")
        assets = [picture, clip_a, clip_b, sound]
        asset_rows = list(Asset.objects.order_by("pk").values())
        bytes_before = {asset.pk: Path(asset.file.path).read_bytes() for asset in assets}
        def item(key, asset, purpose, order=0, parent=None):
            return {"kind": "asset", "item_key": key, "asset_id": str(asset.pk), "purpose": purpose,
                "scope_ref": {"kind": "request"}, "subject_ref": None, "order": order, "paired_to": parent}
        def selection(items):
            return {"selection": {"source": "asset_library", "items": items}}
        sources = {"pictures": selection([item("picture", picture, None)]),
            "clips": selection([item("walk", clip_a, "motion_camera_reference"), item("camera", clip_b, "motion_camera_reference", 1)]),
            "paired": selection([item("paired-use", sound, "audio_reference", parent="camera")]),
            "ambient": selection([item("independent-use", sound, "audio_reference")]),
            "units": {"kind": "literal", "value": {"schema_version": 1, "ordered_units": [{"unit_key": "composition", "duration_ms": 5000,
                "subject_keys": [], "structured_input": {"direction": "Use the supplied motion and camera references in their declared order."},
                "dialogue": [], "voice_profiles": [], "continuity_notes": "", "requires_first_frame_continuity": False,
                "requires_last_frame_continuity": False, "ambient_sound": ""}],
                "color_script": "", "extra_direction": "", "overall_soundscape": "", "non_diegetic_music": ""}}}
        first = self.create("int-mixed-forward", target, sources)
        changed = deepcopy(sources)
        changed["clips"]["selection"]["items"].reverse()
        for index, use in enumerate(changed["clips"]["selection"]["items"]):
            use["order"] = index
        second = self.create("int-mixed-reverse", target, changed)
        self.assertNotEqual(first["prep"].intent_sha256, second["prep"].intent_sha256)
        self.assertNotEqual(first["frozen"][2].sha256, second["frozen"][2].sha256)
        for result in (first, second):
            refs = {row["item_key"]: row for row in result["refs"]}
            self.assertEqual(set(refs), {"picture", "walk", "camera", "paired-use", "independent-use"})
            self.assertEqual(refs["paired-use"]["asset_id"], refs["independent-use"]["asset_id"])
            self.assertNotEqual(refs["paired-use"]["input_label"], refs["independent-use"]["input_label"])
            logical = result["facts"]["inputs"]["logical_values"]
            self.assertEqual(logical["paired"][0]["paired_to"], "camera")
            self.assertIsNone(logical["ambient"][0]["paired_to"])
            self.assertIsNone(logical["pictures"][0]["purpose"])
            self.assertEqual(len(result["transport"]["payload"]["media_preparation"]["bindings"]), 5)
            prompt = result["transport"]["prompt_channels"]["values"]["positive_prompt"]
            for asset in assets:
                self.assertIn(asset.name, prompt)
            for row in result["refs"]:
                self.assertIn(row["input_label"], prompt)
        for key in ("walk", "camera"):
            old = next(row for row in first["refs"] if row["item_key"] == key)
            new = next(row for row in second["refs"] if row["item_key"] == key)
            self.assertEqual((old["asset_id"], old["entity_label"], old["purpose"]), (new["asset_id"], new["entity_label"], new["purpose"]))
            self.assertNotEqual(old["input_label"], new["input_label"])
            self.assertNotEqual(old["target_port_ref"], new["target_port_ref"])
        before = {model.__name__: list(model.objects.order_by("pk").values()) for model in (AssetGenerationJob, ExecutionPreparation)}
        for result in (first, second):
            prep = result["prep"]
            self.assertEqual(self.api("post", "/api/v1/asset-generation-jobs/", result["body"], HTTP_IDEMPOTENCY_KEY=result["key"])["id"], str(result["job"].pk))
            self.api("get", f"/api/v1/execution-preparations/{prep.pk}/prompt-display/", {"expected_preparation_revision": prep.revision})
            self.assertEqual(read_frozen_execution_request(prep), result["frozen"])
        self.assertEqual(before, {model.__name__: list(model.objects.order_by("pk").values()) for model in (AssetGenerationJob, ExecutionPreparation)})
        self.assertEqual(list(Asset.objects.order_by("pk").values()), asset_rows)
        for asset in assets:
            self.assertEqual(Path(asset.file.path).read_bytes(), bytes_before[asset.pk])
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GenerationTask.objects.exists())
        self.assertFalse(AssetGenerationJob.objects.exclude(submission_started_at=None, started_at=None).exists())
