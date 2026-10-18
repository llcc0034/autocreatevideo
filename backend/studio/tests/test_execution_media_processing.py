"""Real FFmpeg derivatives and frozen per-use upload protocol; no provider submit."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from studio.models import Asset, ModelExecutionInstanceVersion
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_instances import update_execution_draft, publish_execution_version
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
from studio.services.execution_workflow_media import prepare_execution_workflow_media
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_workflow_staging as staging_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionMediaProcessingTests(TestCase):
    _create_version = staging_fixtures.ExecutionWorkflowStagingTests._create_version
    compile = staging_fixtures.ExecutionWorkflowStagingTests.compile
    stage = staging_fixtures.ExecutionWorkflowStagingTests.stage
    persist = staging_fixtures.ExecutionWorkflowStagingTests.persist

    def setUp(self):
        staging_fixtures.ExecutionWorkflowStagingTests.setUp(self)
        specs = deepcopy(self.definition.value["configuration"]["inputs"])
        for key, kind, target, maximum, parent, purpose in (
            ("clips", "video", "videos", 2, None, "motion_camera_reference"),
            ("paired", "audio", "video_audios", 1, "clips", "audio_reference"),
            ("ambient", "audio", "audios", 1, None, "audio_reference"),
        ):
            spec = deepcopy(specs[0])
            spec.update(key=key, position=len(specs), kind=kind, min_items=0, required=False, max_items=maximum)
            spec["targets"] = [{"target_kind": "workflow_slot", "target_key": target, "is_primary": True}]
            spec["semantic_contract"].update(allowed_purposes=[purpose], paired_with_input_key=parent)
            specs.append(spec)
        version = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision,
            actor=self.user, changes={"inputs": specs})
        self.definition = build_execution_definition(version.pk)
        self.parameters = initialize_execution_definition_parameters(self.definition,
            {key: value for key, value in self.parameters["values"].items() if self.parameters["sources"][key] == "request"},
            policies=[], resolve_source=lambda *_: self.fail("No inferred parameter source"))
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.originals = []
        for name, source, duration, codec in (("short.mp4", "testsrc2=size=64x48:rate=30", 2.2, "libx264"),
                ("long.mp4", "testsrc2=size=64x48:rate=30", 3.4, "libx264"),
                ("track.wav", "sine=frequency=440:sample_rate=48000", 4, "pcm_s16le")):
            output = root / name
            result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", source, "-t", str(duration), "-c", codec, str(output)], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            content = output.read_bytes()
            asset = Asset(owner=self.user, kind="reference", name=name, checksum=sha256(content).hexdigest())
            asset.file.save(name, ContentFile(content))
            self.originals.append(asset)
        def item(key, asset, purpose, **extra):
            return {"kind": "asset", "item_key": key, "asset_id": str(asset.pk), "purpose": purpose, **extra}
        self.sources = {"references": {"items": [item("picture", self.asset, None)]},
            "clips": {"items": [item("short", self.originals[0], "motion_camera_reference"), item("long", self.originals[1], "motion_camera_reference")]},
            "paired": {"items": [item("sync", self.originals[2], "audio_reference", paired_to="long")]},
            "ambient": {"items": [item("standalone", self.originals[2], "audio_reference")]}}
        self.inputs = resolve_execution_input_sources(self.definition, self.sources, owner=self.user, subject_facts=[], max_media_bytes=2000000)
        self.transport = self.compile()

    def test_real_video_and_sparse_pair_are_normalized_and_each_audio_use_keeps_its_own_bytes(self):
        result = self.stage(max_media_bytes=2000000)
        records = {row["item_key"]: row for row in result.receipts.value}
        self.assertEqual(len(records), 5)
        self.assertEqual(records["short"]["adapter"], "cfr_video_v1")
        self.assertEqual(records["short"]["metadata"]["effective_fps"], 24)
        self.assertNotEqual(records["short"]["metadata"]["effective_duration_ms"], records["long"]["metadata"]["effective_duration_ms"])
        self.assertEqual(records["sync"]["paired_video_item_key"], "long")
        self.assertEqual(records["sync"]["metadata"]["effective_duration_ms"], records["long"]["metadata"]["effective_duration_ms"])
        self.assertNotIn("paired_video_asset_id", records["sync"]["metadata"])
        self.assertEqual(records["sync"]["source_sha256"], records["standalone"]["source_sha256"])
        self.assertNotEqual(records["sync"]["submitted_sha256"], records["standalone"]["submitted_sha256"])
        self.assertEqual(records["standalone"]["submitted_sha256"], self.originals[2].checksum)
        from studio.task_center_media_trace import media_trace
        traced = media_trace(self.definition, self.transport, {"inputs": self.inputs.value},
            owner_id=self.user.pk, root_id=self.job.pk, receipts=result.receipts.value)
        rows = {row["item_key"]: row for row in traced["items"]}
        self.assertEqual(rows["short"]["adapter"], "cfr_video_v1")
        self.assertEqual(rows["short"]["processing_metadata"]["effective_fps"], 24)
        self.assertEqual(rows["sync"]["paired_to"], "long")
        self.assertNotEqual(rows["sync"]["receipt"]["submitted_sha256"], rows["standalone"]["receipt"]["submitted_sha256"])
        for asset in self.originals:
            self.assertEqual(sha256(Path(asset.file.path).read_bytes()).hexdigest(), asset.checksum)
        with patch("studio.services.reference_media.preprocess_reference_input", side_effect=AssertionError("Cannot renormalize a durable receipt")), \
                patch("studio.services.execution_workflow_staging.preprocess_reference_input", side_effect=AssertionError("Cannot renormalize a durable receipt")):
            self.assertEqual(self.stage(max_media_bytes=2000000), result)
        self.assertEqual(len(self.peer.uploads), 5)

    def embedded_sources(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "with-audio.mp4"
            completed = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", self.originals[1].file.path, "-i", self.originals[2].file.path,
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-shortest", str(output)],
                capture_output=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
            content = output.read_bytes()
        asset = Asset(owner=self.user, kind="reference", name="With original audio", checksum=sha256(content).hexdigest())
        asset.file.save("with-audio.mp4", ContentFile(content))
        sources = deepcopy(self.sources)
        sources["clips"]["items"][1]["asset_id"] = str(asset.pk)
        sources["paired"]["items"] = [{"kind": "video_audio", "item_key": "original-sound", "purpose": "audio_reference", "paired_to": "long"}]
        return sources

    def test_original_audio_uses_native_video_port_and_one_verified_upload(self):
        from studio.services.execution_reference_materials import project_execution_reference_materials
        sources = self.embedded_sources()
        asset_count = Asset.objects.count()
        self.inputs = resolve_execution_input_sources(self.definition, sources, owner=self.user, subject_facts=[], max_media_bytes=2000000)
        audio = self.inputs.value["logical_values"]["paired"][0]
        self.assertIsNone(audio["asset_id"])
        self.assertEqual(audio["origin"]["parent_item_key"], "long")
        self.assertEqual(self.inputs.value["model_input_counts"]["paired_video_audios"], 1)
        media = prepare_execution_workflow_media(self.definition, self.inputs).value
        self.assertEqual(media["classified_groups"]["videos"][1]["paired_audio"], {"kind": "embedded"})
        self.assertNotIn("original-sound", [row["item_key"] for row in media["bindings"]])
        self.assertNotIn("original-sound", media["context_only_item_keys"])
        references = project_execution_reference_materials(self.definition, self.inputs, prompt_type="video", unit_keys=["one"], scope_units={}).value
        original = next(row for row in references["references"] if row["item_key"] == "long")
        self.assertTrue(original["use_embedded_audio"])
        self.assertEqual(original["audio_input_label"], "<Audio 1>")
        self.transport = self.compile()
        result = self.stage(max_media_bytes=2000000)
        records = {row["item_key"]: row for row in result.receipts.value}
        self.assertEqual(len(records), 4)
        self.assertNotIn("original-sound", records)
        from studio.task_center_media_trace import media_trace
        traced = media_trace(self.definition, self.transport, {"inputs": self.inputs.value},
            owner_id=self.user.pk, root_id=self.job.pk, receipts=result.receipts.value)
        rows = {row["item_key"]: row for row in traced["items"]}
        self.assertEqual(rows["original-sound"]["mode"], "embedded_audio")
        self.assertEqual(rows["original-sound"]["graph_inputs"], rows["long"]["graph_inputs"])
        self.assertEqual(rows["original-sound"]["receipt"], rows["long"]["receipt"])
        self.assertNotEqual(rows["original-sound"]["ports"], rows["long"]["ports"])
        from studio.services.generated_artifacts import verified_local_media_facts
        with TemporaryDirectory() as directory:
            submitted = Path(directory) / "submitted.mp4"
            submitted.write_bytes(self.peer.files[records["long"]["comfy_path"]])
            observed = verified_local_media_facts(submitted, expected_kind="video", max_bytes=2000000,
                include_observed_media=True)["observed_media"]
            self.assertTrue(observed["has_audio"])
        self.assertEqual(Asset.objects.count(), asset_count)
        self.assertEqual(self.stage(max_media_bytes=2000000, staged_inputs=result.receipts.value).receipts, result.receipts)

    def test_original_audio_is_independent_of_input_order_and_rechecks_parent_identity(self):
        sources = self.embedded_sources()
        resolved = resolve_execution_input_sources(self.definition, sources, owner=self.user, subject_facts=[], max_media_bytes=2000000)
        value = deepcopy(self.definition.value)
        specs = value["configuration"]["inputs"]
        specs.sort(key=lambda spec: spec["key"] != "paired")
        for position, spec in enumerate(specs):
            spec["position"] = position
        reordered = json_contract(value)
        actual = resolve_execution_input_sources(reordered, sources, owner=self.user, subject_facts=[], max_media_bytes=2000000)
        self.assertEqual(actual.value["logical_values"]["paired"], resolved.value["logical_values"]["paired"])
        for field, changed in (("parent_item_key", "short"), ("parent_source_sha256", "0" * 64)):
            invalid = deepcopy(resolved.value)
            invalid["logical_values"]["paired"][0]["origin"][field] = changed
            with self.subTest(field=field), self.assertRaises(ValidationError):
                prepare_execution_workflow_media(self.definition, json_contract(invalid))

    def test_original_audio_rejects_silent_video_mixed_sources_and_wrong_parent(self):
        sources = deepcopy(self.sources)
        sources["paired"]["items"] = [{"kind": "video_audio", "item_key": "original-sound", "purpose": "audio_reference", "paired_to": "long"}]
        with self.assertRaisesMessage(ValidationError, "未检测到内嵌音轨"):
            resolve_execution_input_sources(self.definition, sources, owner=self.user, subject_facts=[], max_media_bytes=2000000)
        sources = self.embedded_sources()
        for change in ({"asset_id": str(self.originals[2].pk)}, {"paired_to": "missing"}, {"subject_ref": {"kind": "external", "id": "fake"}}):
            invalid = deepcopy(sources)
            invalid["paired"]["items"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                resolve_execution_input_sources(self.definition, invalid, owner=self.user, subject_facts=[], max_media_bytes=2000000)

    def test_audio_file_port_uses_original_paired_adapter_on_verified_video_bytes(self):
        # Protocol-level extra port fixture, not a published workflow/admission
        # certificate. Exercise real source authorization and FFmpeg extraction.
        from studio.services.execution_media_processing import execution_media_processing_uses
        from studio.services.execution_media_sources import resolve_execution_workflow_media_sources
        from studio.services.generated_artifacts import verified_local_media_facts
        from studio.services.reference_media import inspect_reference_video_contract, preprocess_reference_input
        sources = self.embedded_sources()
        value = deepcopy(self.definition.value)
        target = {"target_kind": "workflow_slot", "target_key": "input.audio.file", "kind": "audio",
            "min_items": 0, "max_items": 1, "required": False, "source": "fixture.audio_file_port",
            "wiring": {"kind": "semantic", "semantic_key": "input.audio.file"},
            "reference_binding": {"category": "audio", "input_slot": 1}}
        value["interface"]["input_targets"][target["target_key"]] = target
        paired = next(row for row in value["configuration"]["inputs"] if row["key"] == "paired")
        paired["targets"].append({"target_kind": "workflow_slot", "target_key": target["target_key"], "is_primary": False})
        definition = json_contract(value)
        resolved = resolve_execution_input_sources(definition, sources, owner=self.user, subject_facts=[], max_media_bytes=2000000)
        media = prepare_execution_workflow_media(definition, resolved).value
        self.assertEqual(media["classified_groups"]["videos"][1]["paired_audio"]["kind"], "external")
        binding = next(row for row in media["bindings"] if row["item_key"] == "original-sound")
        self.assertEqual(len(binding["ports"]), 2)
        paths = resolve_execution_workflow_media_sources(definition, resolved, owner=self.user, max_media_bytes=2000000)
        self.assertEqual(paths["original-sound"], paths["long"])
        processing = execution_media_processing_uses(definition, resolved)
        spec, profile = processing["original-sound"], media["reference_media_profile"]["value"]
        self.assertNotIn("embedded_audio_sources", processing["long"])
        self.assertEqual(spec["slot"], "paired_video_audios")
        plan = inspect_reference_video_contract(paths["long"], processing["long"]["contract"], profile)
        before = Asset.objects.count()
        with TemporaryDirectory() as directory:
            path, metadata = preprocess_reference_input(paths["original-sound"], directory, slot=spec["slot"],
                contract=spec["contract"], media_profile=profile, video_plan=plan, include_embedded_audio=False)
            facts = verified_local_media_facts(path, expected_kind="audio", max_bytes=2000000, include_observed_media=True)
            self.assertNotEqual(facts["sha256"], binding["media_facts"]["sha256"])
            self.assertTrue(facts["observed_media"]["has_audio"])
            self.assertGreater(metadata["effective_duration_ms"], 0)
        self.assertEqual(Asset.objects.count(), before)

    def test_frozen_profile_is_not_replaced_by_current_configuration_and_missing_profile_fails(self):
        with patch("studio.services.execution_media_processing.resolve_reference_media_profile", side_effect=AssertionError("Cannot reread current defaults")):
            self.assertEqual(self.compile(), self.transport)
            self.stage(max_media_bytes=2000000)
        for mutation in ("missing", "hash", "value"):
            value = deepcopy(self.inputs.value)
            if mutation == "missing":
                del value["media_processing_profile"]
            elif mutation == "hash":
                value["media_processing_profile"]["sha256"] = "0" * 64
            else:
                value["media_processing_profile"]["value"]["video_crf"] += 1
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                prepare_execution_workflow_media(self.definition, json_contract(value))

    def test_changed_parent_or_observed_metadata_receipt_cannot_be_reused(self):
        original = self.stage(max_media_bytes=2000000).receipts.value
        for field, value in (("paired_video_item_key", "short"), ("media_profile_sha256", "0" * 64),
                ("metadata", {"effective_duration_ms": 1})):
            records = deepcopy(original)
            record = next(row for row in records if row["item_key"] == "sync")
            record[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.stage(max_media_bytes=2000000, staged_inputs=records)

    def test_common_resolver_persists_media_and_compiles_from_the_same_preparation_without_a_shot(self):
        version = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        request = {"inputs": self.sources, "parameters": {key: value for key, value in self.parameters["values"].items()
            if self.parameters["sources"][key] == "request"}, "manual_channels": {"positive_prompt": "A parcel opens."},
            "subject_facts": [], "source_language": "", "target_language": ""}
        def resolve(definition, root):
            return resolve_execution_request_facts(definition, request, owner=root.owner, unit_key="asset-request", max_media_bytes=2000000,
                parameter_policies=[], resolve_parameter_source=lambda *_: self.fail("No hidden step source"),
                review_contract=json_contract({"schema_version": 1, "mode": "manual"}))
        prep = create_execution_instance_preparation(owner_ref=("asset_job_id", self.job.pk), version_id=version.pk,
            expected_definition_sha256=self.definition.sha256, unit_key="asset-request", request_key="primary",
            intent=json_contract(request), resolve_facts=resolve, actor=self.user)
        self.assertEqual(prep.status, "preparing")
        self.assertIsNone(prep.request_frozen_at)
        self.assertEqual(prep.contract.resolved_inputs["field_requests"], [])
        self.assertEqual(prep.contract.resolved_inputs["inputs"]["media_processing_profile"], self.inputs.value["media_processing_profile"])
        with patch("studio.services.execution_media_processing.resolve_reference_media_profile", side_effect=AssertionError("No current config")):
            result = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
        self.assertEqual(result.transport.value["payload"]["media_preparation"], self.transport.value["payload"]["media_preparation"])
        self.stage(max_media_bytes=2000000, transport=result.transport)
        self.assertEqual(len(self.peer.uploads), 5)
