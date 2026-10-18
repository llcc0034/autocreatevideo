"""Real local Asset facts and typed input envelopes; no provider calls."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from unittest.mock import patch

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase

from studio.models import Asset, CharacterBible, SeriesProject
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.json_contracts import json_contract
from studio.services.media_capabilities import CONDITIONING_INPUT_SLOTS
from studio.tests.factories import conditioning_contract


class ExecutionInputResolutionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="input-owner")
        cls.other = get_user_model().objects.create_user(username="input-other")

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        settings_override = self.settings(MEDIA_ROOT=self.root, ASSET_LIBRARY_ROOT=self.root / "library")
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        buffer = BytesIO()
        Image.new("RGB", (12, 18), "red").save(buffer, format="PNG")
        self.bytes = buffer.getvalue()
        self.asset = Asset(owner=self.owner, kind="reference", name="Source", checksum=sha256(self.bytes).hexdigest(),
                           description="A declared reference.", prompt_hint="Use the recorded facts.")
        self.asset.file.save("reference.png", ContentFile(self.bytes))
        self.spec = {"key": "references", "kind": "image", "required": True, "min_items": 1, "max_items": 2,
                     "semantic_contract": {"schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": ["external", "character"],
                        "allowed_scope_kinds": ["request", "project"], "paired_with_input_key": None, "allow_repeated_asset": False},
                     "targets": [{"target_kind": "workflow_slot", "target_key": "reference_group", "is_primary": True}]}
        self.target = {"target_kind": "workflow_slot", "target_key": "reference_group", "kind": "image", "min_items": 1, "max_items": 2,
                       "wiring": {"kind": "semantic_group", "semantic_keys": ["reference.one", "reference.two"]}}

    def definition(self, specs=None, targets=None):
        # A minimal service-verified interface fixture, not registry publication.
        return json_contract({"schema_version": 1, "configuration": {"inputs": specs if specs is not None else [self.spec]}, "resources": {"conditioning_mode": None},
                              "interface": {"input_targets": targets if targets is not None else {"reference_group": self.target}}}, max_bytes=65536)

    def item(self, **changes):
        return {"kind": "asset", "item_key": "use-one", "asset_id": str(self.asset.pk), "purpose": None, **changes}

    @staticmethod
    def mode_with_counts(kind="classified_references", **bounds):
        return {"input_kind": kind, "inputs": {key: {
            "minimum_items": bounds.get(key, (0, 0))[0], "maximum_items": bounds.get(key, (0, 0))[1],
        } for key in CONDITIONING_INPUT_SLOTS}, "dynamic_reference_topology": None}

    def resolve(self, items=None, *, sources=None, definition=None, **kwargs):
        return resolve_execution_input_sources(definition or self.definition(), sources if sources is not None else {"references": {"items": items if items is not None else [self.item()]}},
            owner=kwargs.pop("owner", self.owner), subject_facts=kwargs.pop("subject_facts", []), max_media_bytes=65536, **kwargs).value

    def test_actual_image_has_path_free_checksum_dimensions_and_frozen_port(self):
        result = self.resolve()
        item = result["logical_values"]["references"][0]
        self.assertEqual(item["media_facts"]["sha256"], self.asset.checksum)
        self.assertEqual(item["media_facts"]["observed_media"], {"schema_version": 1, "observation_source": "image_decoder", "width": 12, "height": 18, "frame_count": 1})
        self.assertEqual(item["scope_ref"], {"kind": "request"})
        self.assertNotIn(str(self.root), str(result))
        self.assertNotIn("local_path", str(result))
        self.assertIn("reference.one", str(result["workflow_slots"]))
        self.assertEqual(self.resolve(), result)

    def test_array_order_and_explicit_multiple_use_are_preserved(self):
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        result = self.resolve([self.item(item_key="second"), self.item(item_key="first", order=1)])
        self.assertEqual([row["item_key"] for row in result["logical_values"]["references"]], ["second", "first"])
        with self.assertRaises(ValidationError):
            self.resolve([self.item(order=1)])
        with self.assertRaises(ValidationError):
            self.resolve([self.item(), self.item()])

    def test_duplicate_asset_and_undeclared_purpose_are_not_silently_removed(self):
        for items in ([self.item(), self.item(item_key="different-use")], [self.item(purpose="guess-from-name")], []):
            with self.subTest(items=items), self.assertRaises(ValidationError):
                self.resolve(items)

    def test_cross_owner_inactive_and_missing_sources_fail(self):
        with self.assertRaises(ValidationError):
            self.resolve(owner=self.other)
        self.asset.is_active = False
        self.asset.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            self.resolve()
        with self.assertRaises(ValidationError):
            self.resolve(sources={})

    def test_checksum_and_real_decode_cannot_be_replaced_by_extension_or_metadata(self):
        self.asset.checksum = "0" * 64
        self.asset.save(update_fields=["checksum"])
        with self.assertRaises(ValidationError):
            self.resolve()
        invalid = b"not actually an image"
        self.asset.file.save("pretend.png", ContentFile(invalid), save=False)
        self.asset.checksum = sha256(invalid).hexdigest()
        self.asset.mime_type = "image/png"
        self.asset.save()
        with self.assertRaises(ValidationError):
            self.resolve()

    def test_technical_smoke_fixture_cannot_enter_business_reference_resolution(self):
        self.asset.metadata = {"fixture_scope": "workflow_smoke"}
        self.asset.save(update_fields=("metadata",))
        with self.assertRaisesMessage(ValidationError, "Smoke"):
            self.resolve()

    def test_external_subject_is_explicit_not_a_fake_bible(self):
        identity = str(uuid4())
        item = self.item(subject_ref={"kind": "external", "id": identity})
        with self.assertRaises(ValidationError):
            self.resolve([item])
        facts = {"id": identity, "category": "character", "name": "Paper courier", "description": "Blue folded paper"}
        result = self.resolve([item], subject_facts=[facts])
        self.assertEqual(result["logical_values"]["references"][0]["subject"]["category"], "character")
        self.assertEqual(result["logical_values"]["references"][0]["subject"]["kind"], "external")
        self.assertEqual(CharacterBible.objects.count(), 0)
        for category in (None, "", "external", "invented", []):
            invalid = {**facts, "category": category}
            if category is None:
                invalid.pop("category")
            with self.subTest(category=category), self.assertRaises(ValidationError):
                self.resolve([item], subject_facts=[invalid])
        changed = self.resolve([item], subject_facts=[{**facts, "category": "prop"}])
        self.assertNotEqual(result["logical_values"]["references"][0]["source_sha256"],
            changed["logical_values"]["references"][0]["source_sha256"])

    def test_subject_payload_requires_category_without_changing_identity(self):
        from studio.execution_instance_serializers import ExecutionSubjectFactSerializer

        facts = {"id": str(uuid4()), "category": "scene", "name": "Quiet room", "description": ""}
        serializer = ExecutionSubjectFactSerializer(data=facts)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, facts)
        self.assertIsInstance(serializer.validated_data["id"], str)
        invalid = [{key: value for key, value in facts.items() if key != "category"},
                   {**facts, "category": "external"}, {**facts, "category": None},
                   {**facts, "category": []}, {**facts, "kind": "scene"}]
        for payload in invalid:
            with self.subTest(payload=payload):
                serializer = ExecutionSubjectFactSerializer(data=payload)
                self.assertFalse(serializer.is_valid())
        self.assertEqual(CharacterBible.objects.count(), 0)

    def test_real_subject_and_scope_need_ownership_project_and_asset_relationship(self):
        config = {"language": "English", "aspect_ratio": "9:16", "width": 720, "height": 1280,
                  "fps": 24, "default_shot_duration_ms": 1000, "continuity_seed": 5}
        project = SeriesProject.objects.create(owner=self.owner, title="Actual project", **config)
        other_project = SeriesProject.objects.create(owner=self.other, title="Other project", **config)
        subject = CharacterBible.objects.create(project=project, name="Actual person", slug="person")
        item = self.item(subject_ref={"kind": "character", "id": str(subject.pk)}, scope_ref={"kind": "project", "id": str(project.pk)})
        with self.assertRaises(ValidationError):
            self.resolve([item])
        subject.reference_assets.add(self.asset)
        self.assertEqual(self.resolve([item], project_id=project.pk)["logical_values"]["references"][0]["subject"]["name"], "Actual person")
        with self.assertRaises(ValidationError):
            self.resolve([item], project_id=other_project.pk)
        with self.assertRaises(ValidationError):
            self.resolve([self.item(scope_ref={"kind": "project", "id": str(other_project.pk)})])

    def reviewed_shot_use(self):
        from studio.models import Episode, Shot, ShotReferenceUse
        from studio.services.bible_variants import create_variant
        from studio.services.production_reference_mapping import reviewed_reference_item_key
        from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
        project = SeriesProject.objects.create(owner=self.owner, title="Exact selected use", **PROJECT_CONFIG)
        episode = Episode.objects.create(project=project, number=1, title="One")
        shot = Shot.objects.create(episode=episode, number=1, title="One", media_type="video", duration_ms=5000, include_audio=True)
        buffer = BytesIO(); Image.new("RGB", (12, 18), "blue").save(buffer, format="PNG")
        anchor = Asset(owner=self.owner, kind="reference", name="Unchanged default", mime_type="image/png",
            checksum=sha256(buffer.getvalue()).hexdigest())
        anchor.file.save("default.png", ContentFile(buffer.getvalue()))
        subject = CharacterBible.objects.create(project=project, name="Courier", slug="courier", stable_speaker_id="S1", anchor_asset=anchor)
        setting = create_variant(actor=self.owner, project_id=project.pk, entity_kind="character", entity_id=subject.pk,
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="",
            value={"name": "Courier", "reference_name": "Courier coat", "identity_anchor": "Courier",
                "appearance_anchor": "A coat", "basis_asset_id": None, "basis_asset_sha256": ""}).revisions.get(revision=1)
        self.asset.mime_type = "image/png"; self.asset.save(update_fields=("mime_type",))
        use = ShotReferenceUse.objects.create(shot=shot, use_key="coat", position=0, media_kind="image",
            purpose="A creative note", setting_revision=setting, source_mode="existing",
            asset=self.asset, asset_sha256=self.asset.checksum)
        self.spec["semantic_contract"]["allowed_scope_kinds"].append("shot")
        item = self.item(item_key=reviewed_reference_item_key(input_key="references", shot_id=str(shot.pk),
            reference_key="shot-use:coat"), scope_ref={"kind": "shot", "id": str(shot.pk)},
            subject_ref={"kind": "character", "id": str(subject.pk)})
        return project, shot, subject, use, item

    def test_reviewed_shot_uses_selected_image_without_registering_it_as_bible_default(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from studio.models import ShotReferenceUse
        from studio.services.production_reference_mapping import reviewed_reference_item_key
        project, shot, subject, use, item = self.reviewed_shot_use()
        original = subject.anchor_asset_id
        ShotReferenceUse.objects.create(shot=shot, use_key="detail", position=1, media_kind="image",
            purpose="Another independent use", setting_revision=use.setting_revision,
            source_mode="existing", asset=self.asset, asset_sha256=self.asset.checksum)
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        second = {**deepcopy(item), "item_key": reviewed_reference_item_key(input_key="references",
            shot_id=str(shot.pk), reference_key="shot-use:detail")}
        with CaptureQueriesContext(connection) as queries:
            result = self.resolve([item, second], project_id=project.pk)
        values = result["logical_values"]["references"]
        self.assertEqual([row["asset_id"] for row in values], [str(self.asset.pk)] * 2)
        self.assertEqual([row["subject"]["id"] for row in values], [str(subject.pk)] * 2)
        self.assertNotEqual(values[0]["source_sha256"], values[1]["source_sha256"])
        use_reads = [row["sql"] for row in queries if 'FROM "studio_shotreferenceuse"' in row["sql"]]
        self.assertEqual(len(use_reads), 1)
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + use_reads[0])
                plan = " ".join(str(row) for row in cursor.fetchall())
            self.assertIn("USING INDEX", plan)
            self.assertNotIn("SELECT *", use_reads[0].upper())
        subject.refresh_from_db()
        self.assertEqual(subject.anchor_asset_id, original)
        self.assertFalse(subject.reference_assets.exists())

    def test_reviewed_subject_rejects_forged_use_asset_and_subject_even_if_old_pool_accepts(self):
        project, shot, subject, use, item = self.reviewed_shot_use()
        other = CharacterBible.objects.create(project=project, name="Other", slug="other", stable_speaker_id="S2", anchor_asset=self.asset)
        subject.reference_assets.add(self.asset)
        for changes in ({"item_key": "reviewed:" + "f" * 64},
                {"asset_id": str(subject.anchor_asset_id)},
                {"subject_ref": {"kind": "character", "id": str(other.pk)}}):
            with self.subTest(changes=changes), self.assertRaisesMessage(ValidationError, "本镜确切用途"):
                self.resolve([{**item, **changes}], project_id=project.pk)
        use.asset_sha256 = "f" * 64; use.save(update_fields=("asset_sha256",))
        with self.assertRaisesMessage(ValidationError, "指纹"):
            self.resolve([item], project_id=project.pk)

    def test_reviewed_subject_cache_cannot_authorize_another_shot_or_use(self):
        from studio.models import Shot
        project, shot, subject, use, item = self.reviewed_shot_use()
        sibling = Shot.objects.create(episode=shot.episode, number=2, title="Unselected sibling", media_type="video", duration_ms=5000, include_audio=True)
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        forged = {**deepcopy(item), "item_key": "reviewed:" + "d" * 64,
            "scope_ref": {"kind": "shot", "id": str(sibling.pk)}}
        with self.assertRaisesMessage(ValidationError, "本镜确切用途"):
            self.resolve([item, forged], project_id=project.pk)
        forged["scope_ref"] = item["scope_ref"]
        with self.assertRaisesMessage(ValidationError, "本镜确切用途"):
            self.resolve([item, forged], project_id=project.pk)
        with self.assertRaises(ValidationError):
            self.resolve([item], owner=self.other, project_id=project.pk)

    def test_reviewed_subject_rechecks_changed_choice_instead_of_caching_between_requests(self):
        project, shot, subject, use, item = self.reviewed_shot_use()
        self.resolve([item], project_id=project.pk)
        use.asset = subject.anchor_asset; use.asset_sha256 = subject.anchor_asset.checksum
        use.save(update_fields=("asset", "asset_sha256"))
        with self.assertRaisesMessage(ValidationError, "本镜确切用途"):
            self.resolve([item], project_id=project.pk)
        actual = self.resolve([{**item, "asset_id": str(use.asset_id)}], project_id=project.pk)
        self.assertEqual(actual["logical_values"]["references"][0]["asset_id"], str(use.asset_id))

    def test_literal_json_false_is_a_value_and_no_expression_or_old_source_is_executed(self):
        spec = {**deepcopy(self.spec), "key": "enabled", "kind": "json", "max_items": 1,
                "targets": [{"target_kind": "profile_input", "target_key": "request.enabled", "is_primary": False}]}
        definition_value = self.definition([spec], {}).value
        # A logical mapping also needs its declared target contract, even for
        # literal JSON. Production definitions derive this from the profile.
        definition_value["interface"]["profile_inputs"] = {"request.enabled": {
            "target_kind": "profile_input", "target_key": "request.enabled", "kind": "json",
            "min_items": 1, "max_items": 1, "value_domain": "boolean",
        }}
        definition = json_contract(definition_value, max_bytes=65536)
        result = self.resolve(sources={"enabled": {"kind": "literal", "value": False}}, definition=definition)
        self.assertIs(result["logical_values"]["enabled"], False)
        self.assertEqual(result["input_provenance"][0]["value_sha256"], json_contract(False, max_bytes=65536).sha256)
        for source in ({"kind": "context_field", "selector_key": "project.secret"}, {"kind": "literal", "value": 2, "path": "fake"}):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                self.resolve(sources={"enabled": source}, definition=definition)

    def test_unknown_inputs_temporary_paths_and_unbound_pairing_fail(self):
        cases = ({"references": {"items": [self.item(path="temporary.png")]}},
                 {"references": {"items": [self.item(paired_to="missing")]}},
                 {"references": {"items": [self.item()]}, "another": {"kind": "literal", "value": "ignored?"}})
        for sources in cases:
            with self.subTest(sources=sources), self.assertRaises(ValidationError):
                self.resolve(sources=sources)

    def operation_definition(self, count=1):
        targets = {f"field_{i}": {"target_kind": "operation_input", "kind": "json"} for i in range(count)}
        spec = {**deepcopy(self.spec), "key": "batch", "kind": "json", "max_items": 1,
            "targets": [{"target_kind": "operation_input", "target_key": key, "is_primary": i == 0}
                for i, key in enumerate(targets)]}
        return self.definition([spec], targets)

    def test_operation_envelope_uses_resolved_cap_not_individual_source_cap(self):
        from studio.services.json_contracts import canonical_json_bytes
        from studio.services.execution_workflow_uses import read_execution_workflow_uses
        value = {"text": "x" * 40000}
        sources = {"batch": {"kind": "literal", "value": value}}
        result = self.resolve(sources=sources, definition=self.operation_definition())
        self.assertGreater(len(canonical_json_bytes(result)), 65536)
        self.assertEqual(result["logical_values"]["batch"], value)
        self.assertEqual(result["operation_inputs"]["field_0"], value)
        self.assertEqual(result, self.resolve(sources=sources, definition=self.operation_definition()))
        envelope = json_contract(result, max_bytes=262144)
        self.assertEqual(read_execution_workflow_uses(self.operation_definition(), envelope).items, {})
        with patch("studio.services.execution_request_storage.boundary_int", return_value=65536):
            with self.assertRaisesMessage(ValidationError, "65536"):
                self.resolve(sources=sources, definition=self.operation_definition())

    def test_operation_values_and_envelope_follow_configured_request_limit(self):
        value = "x" * 90000
        result = self.resolve(sources={"batch": {"kind": "literal", "value": value}}, definition=self.operation_definition(5))
        self.assertEqual(result["logical_values"]["batch"], value)
        for actual in result["operation_inputs"].values():
            self.assertEqual(actual, value)
        with patch("studio.services.execution_request_storage.boundary_int", return_value=262144):
            with self.assertRaisesMessage(ValidationError, "262144"):
                self.resolve(sources={"batch": {"kind": "literal", "value": value}}, definition=self.operation_definition(5))

    def test_non_string_input_keys_are_validation_errors_not_coerced_json_keys(self):
        with self.assertRaises(ValidationError):
            self.resolve(sources={1: {"kind": "literal", "value": "hidden"}})

    def paired_definition(self, *, allow_external=True):
        video = {**deepcopy(self.spec), "key": "clips", "kind": "video"}
        video["semantic_contract"]["allowed_purposes"] = ["declared_motion"]
        video["targets"][0]["target_key"] = "videos"
        audio = {**deepcopy(self.spec), "key": "sound", "kind": "audio", "max_items": 1}
        audio["semantic_contract"].update(allowed_purposes=["declared_sync"], paired_with_input_key="clips")
        audio["targets"][0]["target_key"] = "video_audios"
        targets = {
            "videos": {**deepcopy(self.target), "target_key": "videos", "kind": "video", "wiring": {
                "kind": "classified_group", "node_id": "receiver", "input_names": ["video.zero", "video.one"]}},
            "video_audios": {**deepcopy(self.target), "target_key": "video_audios", "kind": "audio", "max_items": 1, "wiring": {
                "kind": "classified_group", "node_id": "receiver", "input_names": ["audio.zero", "audio.one"], "index_source_target_key": "videos"}},
        }
        definition = self.definition([video, audio], targets).value
        definition["resources"]["conditioning_mode"] = {**self.mode_with_counts(reference_videos=(1, 2), paired_video_audios=(0, 1)), "dynamic_reference_topology": {
            "preprocessing": conditioning_contract("test", reference_videos=(0, 2), paired_video_audios=(0, 1))["modes"][0]["dynamic_reference_topology"]["preprocessing"],
            "videos": {"minimum_items": 1, "maximum_items": 2, "purposes": [{"key": "declared_motion", "minimum_items": 1, "maximum_items": 2}]},
            "audios": {"minimum_items": 0, "maximum_items": 0, "purposes": [{"key": "declared_sync", "minimum_items": 0, "maximum_items": 0}]},
            "mutual_exclusion_rules": [], "pairing_rules": [{"key": "declared_pair", "source_purpose_keys": ["declared_motion"],
                "target_purpose_keys": ["declared_sync"], "minimum_targets_per_source": 0, "maximum_targets_per_source": 1,
                "allow_embedded_source": False, "allow_external_target": allow_external, "source_and_external_mutually_exclusive": True}],
        }}
        return json_contract(definition, max_bytes=65536)

    def paired_sources(self):
        asset_ids = [str(self.asset.pk)]
        for index in range(2):
            asset = Asset(owner=self.owner, kind="reference", name=f"Source {index}", checksum=sha256(self.bytes).hexdigest())
            asset.file.save(f"source-{index}.png", ContentFile(self.bytes))
            asset_ids.append(str(asset.pk))
        return {"clips": {"items": [self.item(asset_id=asset_ids[index], item_key=f"clip-{index}", purpose="declared_motion") for index in range(2)]},
                "sound": {"items": [self.item(asset_id=asset_ids[2], item_key="paired-track", purpose="declared_sync", paired_to="clip-1")]}}

    def test_sparse_audio_pair_uses_parent_video_position_not_audio_array_position(self):
        # Isolated wiring test: media probing is stubbed, not real AV acceptance.
        # Asset permissions/hash/path are real; image decoding is tested above.
        facts = {"sha256": sha256(self.bytes).hexdigest(), "size_bytes": len(self.bytes), "mime_type": "test/probed-media"}
        with patch("studio.services.execution_media_sources.verified_local_media_facts", return_value=facts):
            result = self.resolve(sources=self.paired_sources(), definition=self.paired_definition())
        slot = result["workflow_slots"]["video_audios"][0]
        self.assertEqual((slot["position"], slot["target_position"]), (0, 1))
        self.assertEqual(slot["target_port_ref"]["input_name"], "audio.one")

    def test_declared_model_pairing_and_missing_parent_fail_without_rewriting_purposes(self):
        sources = self.paired_sources()
        facts = {"sha256": sha256(self.bytes).hexdigest(), "size_bytes": len(self.bytes), "mime_type": "test/probed-media"}
        with patch("studio.services.execution_media_sources.verified_local_media_facts", return_value=facts):
            with self.assertRaisesMessage(ValidationError, "不允许外配音频"):
                self.resolve(sources=sources, definition=self.paired_definition(allow_external=False))
            sources["sound"]["items"][0]["paired_to"] = "different-request"
            with self.assertRaises(ValidationError):
                self.resolve(sources=sources, definition=self.paired_definition())

    def test_model_image_counts_restrict_logical_slot_range_and_count_uses(self):
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        definition = self.definition().value
        definition["resources"]["conditioning_mode"] = self.mode_with_counts(total_images=(1, 1), entity_images=(1, 2))
        resolved = self.resolve(definition=json_contract(definition))
        self.assertEqual(resolved["model_input_counts"]["entity_images"], 1)
        with self.assertRaisesMessage(ValidationError, "total_images"):
            self.resolve([self.item(), self.item(item_key="another-use")], definition=json_contract(definition))
        definition["resources"]["conditioning_mode"]["inputs"]["total_images"]["maximum_items"] = 2
        resolved = self.resolve([self.item(), self.item(item_key="another-use")], definition=json_contract(definition))
        self.assertEqual(resolved["model_input_counts"]["entity_images"], 2)

    def test_prompt_only_media_does_not_consume_primary_model_slots(self):
        self.spec["targets"] = [{"target_kind": "profile_input", "target_key": "context.visual", "is_primary": False}]
        definition = self.definition(targets={}).value
        definition["resources"]["conditioning_mode"] = self.mode_with_counts("text_only")
        result = self.resolve(definition=json_contract(definition))
        self.assertEqual(result["model_input_counts"], dict.fromkeys(CONDITIONING_INPUT_SLOTS, 0))
        self.assertEqual(len(result["logical_values"]["references"]), 1)
        self.assertEqual(result["workflow_slots"], {})

    def test_frame_roles_are_not_inferred_from_first_selected_image(self):
        definition = self.definition().value
        definition["resources"]["conditioning_mode"] = self.mode_with_counts("frame_slots", total_images=(1, 1), first_frame=(1, 1))
        with self.assertRaisesMessage(ValidationError, "不能按顺序或名称猜测") as caught:
            self.resolve(definition=json_contract(definition))
        self.assertEqual(caught.exception.code, "execution_interface_unavailable")

    def test_frame_count_uses_frozen_category_not_target_name_or_position(self):
        self.spec.update(key="first_picture_in_the_form", max_items=1)
        self.target.update(max_items=1, reference_binding={"category": "last_frame", "input_slot": 2},
            wiring={"kind": "semantic", "semantic_key": "asset.image.custom"})
        definition = self.definition().value
        definition["resources"]["conditioning_mode"] = self.mode_with_counts("frame_slots", total_images=(1, 1), last_frame=(1, 1))
        result = self.resolve(sources={self.spec["key"]: {"items": [self.item()]}}, definition=json_contract(definition))
        self.assertEqual(result["model_input_counts"]["last_frame"], 1)
        self.assertEqual(result["model_input_counts"]["first_frame"], 0)
        definition["resources"]["conditioning_mode"] = self.mode_with_counts("classified_references", total_images=(1, 1), entity_images=(1, 1))
        with self.assertRaises(ValidationError):
            self.resolve(sources={self.spec["key"]: {"items": [self.item()]}}, definition=json_contract(definition))
