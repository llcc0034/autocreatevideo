"""Output protocol checks with actual PNG bytes, no GPU or remote requests."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.execution_workflow_outputs import verify_comfy_primary_output, frozen_comfy_output_prefix
from studio.services.provider_registry import ProviderDriverError


class ExecutionWorkflowOutputTests(SimpleTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.enterContext(self.settings(COMFYUI_OUTPUT_DIR=str(self.root)))
        self.folder = self.root / "autoprompt" / "execution"
        self.folder.mkdir(parents=True)
        self.source = self.folder / "request_0001.png"
        Image.new("RGB", (16, 24), "blue").save(self.source)
        self.receipt = {"node_id": "output", "filename": self.source.name, "subfolder": "autoprompt/execution", "type": "output"}

    def verify(self, **changes):
        return verify_comfy_primary_output(**{"collected": {"outputs": [self.receipt]},
            "output_contract": {"schema_version": 1, "primary": {"node_id": "output", "kind": "image"}},
            "expected_kind": "image", "expected_prefix": "autoprompt/execution/request", "resolved_semantics": {}, "max_bytes": 65536, **changes})

    def test_actual_bytes_checked_without_trusting_receipt_hash(self):
        self.receipt["sha256"] = "a" * 64
        source, facts = self.verify()
        self.assertEqual(source, self.source)
        self.assertEqual(facts["sha256"], sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(facts["kind"], "image")
        self.assertEqual(facts["node_id"], "output")

    def test_fixed_image_dimensions_are_checked_and_observed_not_copied(self):
        _, facts = self.verify(resolved_semantics={"image.width": 16, "image.height": 24})
        self.assertEqual(facts["observed_media"]["width"], 16)
        self.assertEqual(facts["observed_media"]["height"], 24)
        for semantics in ({"image.width": 32}, {"image.height": 32}):
            with self.subTest(semantics=semantics), self.assertRaises(ValidationError):
                self.verify(resolved_semantics=semantics)

    def test_wrong_namespace_node_duplicate_and_non_output_storage_rejected(self):
        for changes in ({"filename": "another_0001.png"}, {"subfolder": "../execution"}, {"node_id": "unselected"}, {"type": "temp"}):
            with self.subTest(changes=changes), self.assertRaises((ValidationError, ProviderDriverError)):
                self.verify(collected={"outputs": [{**self.receipt, **changes}]})
        with self.assertRaises(ProviderDriverError):
            self.verify(collected={"outputs": [self.receipt, self.receipt]})

    def test_source_derived_size_is_verified_against_actual_bytes(self):
        contract = {"schema_version": 2, "primary": {"node_id": "output", "kind": "image"},
            "resolution": {"adapter": "comfy.flux_kontext_image_scale/v1",
                "source_semantic_key": "asset.image.source", "source_asset_id": "isolated-source",
                "source_checksum": "a" * 64, "source_width": 600, "source_height": 600,
                "width": 1024, "height": 1024}}
        with self.assertRaisesMessage(ValidationError, "实际图片尺寸"):
            self.verify(output_contract=contract)
        Image.new("RGB", (1024, 1024), "blue").save(self.source)
        _, facts = self.verify(output_contract=contract)
        self.assertEqual(facts["observed_media"]["width"], 1024)
        self.assertEqual(facts["observed_media"]["height"], 1024)
        self.assertEqual(facts["sha256"], sha256(self.source.read_bytes()).hexdigest())
        contract["resolution"]["width"] = 512
        with self.assertRaisesMessage(ValidationError, "冻结输出宽高"):
            self.verify(output_contract=contract)

    def test_video_contract_checks_each_frozen_parameter_against_probe(self):
        video = self.folder / "request_0001.mp4"
        video.write_bytes(b"isolated-probe-fixture")
        options = {"collected": {"outputs": [{**self.receipt, "filename": video.name}]}, "expected_kind": "video",
            "output_contract": {"schema_version": 1, "primary": {"node_id": "output", "kind": "video"}},
            "resolved_semantics": {"image.width": 320, "image.height": 512, "video.frames": 124, "video.fps": 24, "audio.enabled": True}}
        observed = {"width": 320, "height": 512, "frames": 124, "fps": 24, "has_audio": True}
        with patch("studio.services.execution_workflow_outputs.verified_local_media_facts", return_value={"observed_media": observed}):
            _, facts = self.verify(**options)
        self.assertEqual(facts["kind"], "video")
        self.assertEqual(facts["node_id"], "output")
        for key in observed:
            with self.subTest(key=key), patch("studio.services.execution_workflow_outputs.verified_local_media_facts",
                    return_value={"observed_media": {**observed, key: 0}}), self.assertRaises(ValidationError):
                self.verify(**options)

    def test_text_requires_actual_hash_size_nonempty_and_exact_node(self):
        text = "A parcel opens."
        item = {"node_id": "output", "kind": "text", "text": text, "size_bytes": len(text), "sha256": sha256(text.encode()).hexdigest()}
        options = {"expected_kind": "text", "output_contract": {"schema_version": 1, "primary": {"node_id": "output", "kind": "text"}}}
        _, facts = self.verify(collected={"outputs": [item]}, **options)
        self.assertEqual(facts["text"], text)
        for changes in ({"sha256": "0" * 64}, {"text": ""}, {"size_bytes": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.verify(collected={"outputs": [{**item, **changes}]}, **options)

    def test_inline_text_has_no_file_namespace_but_media_and_explicit_prefix_remain_strict(self):
        compiled = {"output_contract": {"primary": {"kind": "text"}}, "resolved_semantics": {}}
        self.assertIsNone(frozen_comfy_output_prefix(compiled, preparation_id="request"))
        for semantics in ({"output.prefix": None}, {"output.prefix": "another"}, {"output.prefix": ""}):
            with self.subTest(semantics=semantics), self.assertRaises(ValidationError):
                frozen_comfy_output_prefix({**compiled, "resolved_semantics": semantics}, preparation_id="request")
        for kind in ("image", "video", "audio"):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                frozen_comfy_output_prefix({**compiled, "output_contract": {"primary": {"kind": kind}}}, preparation_id="request")
