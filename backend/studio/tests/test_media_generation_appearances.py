"""M4 declarations on real isolated roots; no live peer or generation.

These tests cover appearance preparation/API only. Projector validation and
mixed orchestration have separate suites; no live generation is claimed.
"""
from copy import deepcopy
from hashlib import sha256
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings

from studio.models import (Asset, AssetGenerationJob, AssetReferenceVersion, CharacterBible,
    ExecutionPreparation, GenerationAttempt, ProductionRun, SeriesProject)
from studio.services.asset_generation import create_asset_generation_job
from studio.services.asset_references import attach_asset, create_series
from studio.services.bible_variants import create_variant, revise_variant, setting_payload
from studio.services.execution_preparations import ResolvedExecutionPreparation
from studio.services.json_contracts import json_contract
from studio.services.media_generation_appearances import (
    normalize_media_appearance_request, verify_media_appearance_target,
)
from studio.services.media_generation_targets import (
    read_media_generation_target, resolve_media_generation_target, verify_media_generation_target,
)
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.tests import test_execution_comfy_image_assets as image_fixtures
from studio.tests import test_production_recipe_bible_regeneration as recipe_fixtures
from studio.tests.test_execution_asset_requests import freeze, isolated_observations
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


def declaration(role="primary", *, setting=None, primary=None):
    return {"role": role, "setting_revision_id": str(setting.pk) if setting else None,
        "primary_version_id": str(primary.pk) if primary else None,
        "primary_input": {"input_key": "reference_1", "item_key": "image_1"} if role == "variant" else None,
        **({"difference_input": {"kind": "manual_channel", "key": "positive_prompt"}} if role == "variant" else {})}


def create_setting(actor, project, character, *, primary_setting=None, appearance_anchor="蓝色外套"):
    variant = create_variant(actor=actor, project_id=project.pk, entity_kind="character", entity_id=character.pk,
        appearance_role="variant" if primary_setting else "primary",
        primary_setting_revision_id=primary_setting.pk if primary_setting else None,
        primary_setting_sha256=primary_setting.content_sha256 if primary_setting else "",
        value={"name": "外套" if primary_setting else "主图", "reference_name": str(uuid4()),
            "identity_anchor": "" if primary_setting else "保持身份", "appearance_anchor": appearance_anchor,
            "basis_asset_id": None, "basis_asset_sha256": ""})
    return variant.revisions.get(revision=1)


class AppearanceDeclarationShapeTests(SimpleTestCase):
    @patch("studio.execution_instance_serializers.execution_configuration_max_bytes", return_value=1048576)
    def test_only_instance_smoke_fixture_retains_optional_appearance(self, _limit):
        from studio.execution_smoke_serializers import (
            ExecutionInstanceSmokeFixtureSerializer, ExecutionSmokeFixtureSerializer,
        )
        fixture = {"inputs": {}, "parameters": {}, "manual_channels": {}, "subject_facts": [],
            "source_language": "", "target_language": ""}
        self.assertEqual(ExecutionInstanceSmokeFixtureSerializer(fixture).data, fixture)
        for value in (None, declaration()):
            submitted = {**fixture, "appearance_target": value}
            smoke = ExecutionInstanceSmokeFixtureSerializer(data=submitted)
            self.assertTrue(smoke.is_valid(), smoke.errors)
            self.assertEqual(smoke.validated_data, submitted)
            self.assertEqual(ExecutionInstanceSmokeFixtureSerializer(submitted).data, submitted)
            generic = ExecutionSmokeFixtureSerializer(data=submitted)
            self.assertFalse(generic.is_valid())
            self.assertIn("appearance_target", str(generic.errors))

    @patch("studio.execution_instance_serializers.execution_configuration_max_bytes", return_value=1048576)
    def test_public_declaration_preserves_exact_input_and_difference_identities(self, _limit):
        from studio.asset_generation_serializers import AssetAppearanceSerializer
        target = {**declaration("variant"), "primary_version_id": str(uuid4()),
            "primary_input": {"input_key": " original input ", "item_key": " original item "},
            "difference_input": {"kind": "reference_field", "key": " original source ",
                "field_key": " original field ", "unit_key": " original unit "}}
        serializer = AssetAppearanceSerializer(data=target)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, target)

    @patch("studio.execution_instance_serializers.execution_configuration_max_bytes", return_value=1048576)
    def test_public_declaration_still_rejects_whitespace_only_identities(self, _limit):
        from studio.asset_generation_serializers import AssetAppearanceSerializer
        target = {**declaration("variant"), "primary_version_id": str(uuid4()),
            "difference_input": {"kind": "reference_field", "key": "units",
                "field_key": "detail", "unit_key": "second"}}
        for group, keys in (("primary_input", ("input_key", "item_key")),
                ("difference_input", ("key", "field_key", "unit_key"))):
            for key in keys:
                with self.subTest(group=group, key=key):
                    serializer = AssetAppearanceSerializer(data={**target, group: {**target[group], key: " \t "}})
                    self.assertFalse(serializer.is_valid())

    def test_recipe_primary_does_not_replace_missing_or_wrong_captured_baseline(self):
        from types import SimpleNamespace
        # Only the upstream capture is substituted to inject corruption; this
        # guard must not fetch a current setting or create a replacement fact.
        scope = {"kind": "character", "id": str(uuid4())}
        project_id = str(uuid4())
        definition = SimpleNamespace(value={"target": {"driver_type": "async_rest"},
            "interface": {"output": {"kind": "image"}}})
        valid = {"primary_setting": None, "scope": scope, "project_id": project_id}
        for bible in (None, {}, {k: v for k, v in valid.items() if k != "primary_setting"},
                {**valid, "scope": {"kind": "character", "id": str(uuid4())}},
                {**valid, "project_id": str(uuid4())}):
            resolved = SimpleNamespace(resolved_inputs=SimpleNamespace(value={"bible_target": bible}))
            with self.subTest(bible=bible), patch(
                    "studio.services.media_generation_targets.resolve_execution_request_facts", return_value=resolved):
                with self.assertRaisesMessage(ValidationError, "原准备捕获的设定基线"):
                    resolve_media_generation_target(definition, {}, candidate_count=1, candidate_evaluator=None,
                        appearance_target_source="bible_primary", owner=None, max_media_bytes=None,
                        production_run_id=str(uuid4()), project_id=project_id, production_scope=scope)

    def test_recipe_primary_rejects_non_image_output_before_resolving(self):
        from types import SimpleNamespace
        definition = SimpleNamespace(value={"target": {"driver_type": "async_rest"},
            "interface": {"output": {"kind": "video"}}})
        with patch("studio.services.media_generation_targets.resolve_execution_request_facts") as resolve:
            with self.assertRaisesMessage(ValidationError, "实际图片输出合同"):
                resolve_media_generation_target(definition, {}, candidate_count=1, candidate_evaluator=None,
                    appearance_target_source="bible_primary", production_run_id=str(uuid4()),
                    production_scope={"kind": "character", "id": str(uuid4())})
            resolve.assert_not_called()

    def test_recipe_primary_source_requires_explicit_production_entity_context(self):
        cases = [("unknown", {}), ("bible_primary", {}), ("bible_primary", {
            "production_run_id": str(uuid4()), "production_scope": {"kind": "shot", "id": str(uuid4())}})]
        for source, context in cases:
            with self.subTest(source=source, context=context), patch(
                    "studio.services.media_generation_targets.resolve_execution_request_facts") as resolve:
                with self.assertRaises(ValidationError):
                    resolve_media_generation_target(None, {}, candidate_count=1, candidate_evaluator=None,
                        appearance_target_source=source, **context)
                resolve.assert_not_called()

    def test_option_serializers_return_field_errors_for_unknown_query_keys(self):
        from studio.asset_generation_serializers import AssetAppearanceOptionsQuerySerializer, AssetGenerationOptionsQuerySerializer
        for serializer, values in ((AssetAppearanceOptionsQuerySerializer, {"role": "variant", "undeclared": "value"}),
                (AssetGenerationOptionsQuerySerializer, {"job_type": "asset", "media_type": "image", "role": "primary"})):
            query = serializer(data=values)
            self.assertFalse(query.is_valid())
            self.assertIn("query", query.errors)

    def test_absence_primary_and_variant_are_explicit_not_inferred(self):
        self.assertIsNone(normalize_media_appearance_request(None))
        primary = declaration()
        self.assertEqual(normalize_media_appearance_request(primary), primary)
        self.assertIsNot(normalize_media_appearance_request(primary), primary)
        variant = {**declaration(), "role": "variant", "primary_version_id": str(uuid4()),
            "primary_input": {"input_key": "images", "item_key": "chosen-primary"},
            "difference_input": {"kind": "manual_channel", "key": "positive_prompt"}}
        self.assertEqual(normalize_media_appearance_request(variant), variant)

    def test_invalid_roles_aliases_basis_and_unknown_keys_are_rejected(self):
        for value in ({}, [], {**declaration(), "role": "latest"}, {**declaration(), "role": []},
                {**declaration(), "setting_revision_id": "latest"}, {**declaration(), "approved": True},
                {**declaration(), "primary_version_id": str(uuid4())}, declaration("variant"),
                {**declaration("variant"), "primary_version_id": str(uuid4()), "primary_input": {"input_key": "images"}}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                normalize_media_appearance_request(value)

    def test_difference_declaration_requires_only_an_original_source_reference(self):
        valid = {**declaration("variant"), "primary_version_id": str(uuid4())}
        for source in (None, {}, {"kind": "manual_channel", "key": ""},
                {"kind": [], "key": "positive_prompt"}, {"kind": "manual_channel", "key": []},
                {"kind": "literal", "key": "new body"}, {"kind": "logical_input", "key": "x", "text": "copy"}):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                normalize_media_appearance_request({**valid, "difference_input": source})
        with self.assertRaises(ValidationError):
            normalize_media_appearance_request({k: v for k, v in valid.items() if k != "difference_input"})
        with self.assertRaises(ValidationError):
            normalize_media_appearance_request({**declaration(), "difference_input": valid["difference_input"]})
        logical = {**valid, "difference_input": {"kind": "logical_input", "key": "request.theme"}}
        self.assertEqual(normalize_media_appearance_request(logical), logical)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class AppearanceAssetPreparationTests(TestCase):
    def setUp(self):
        image_fixtures.ExecutionComfyImageAssetTests.setUp(self)
        self.external = self.enterContext(patch("requests.sessions.Session.request",
            side_effect=AssertionError("Isolated preparation must not contact a model")))
        self.project = SeriesProject.objects.create(owner=self.user, title="Appearance fixture", **PROJECT_CONFIG)
        self.character = CharacterBible.objects.create(project=self.project, name="Courier")

    def create_job(self, target, *, key="appearance-fixture", **changes):
        return create_asset_generation_job(owner=self.user, client_idempotency_key=key,
            execution_instance_version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            job_type="asset", media_type="image", image_category="reference", name="Declared appearance",
            field_values=[], **{"inputs": self.inputs,
                "parameters": {"image.width": 16, "image.height": 24, "sampling.seed": 42,
                    "sampling.cfg_scale": 6.5, "sampling.scheduler": "karras"},
                "manual_channels": {"positive_prompt": "Supplied references.", "negative_prompt": ""},
                "subject_facts": [], "source_language": "", "target_language": "",
                "appearance_target": target, **changes})

    def primary_version(self, *, setting=None, asset=None, owner=None):
        owner, asset = owner or self.user, asset or self.assets[0]
        asset.mime_type = "image/png"
        asset.save(update_fields=["mime_type"])
        series = create_series(actor=owner, values={"role": "primary", "name": "指定主图",
            "bible_variant_id": str(setting.variant_id) if setting else None})
        return attach_asset(actor=owner, series_id=series.pk, asset_id=asset.pk, source="existing",
            setting_revision_id=setting.pk if setting else None)[0]

    def assert_no_call(self):
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ProductionRun.objects.exists())
        self.assertFalse(AssetGenerationJob.objects.exclude(output_asset=None).exists())
        self.external.assert_not_called()

    def test_detail_api_returns_original_target_without_current_resolution(self):
        setting = create_setting(self.user, self.project, self.character)
        job, _ = self.create_job(declaration(setting=setting))
        prep = job.execution_preparations.get()
        original = read_media_generation_target(prep).value
        original_revision = prep.revision
        revise_variant(actor=self.user, variant=setting.variant, expected_revision=1,
            value={**setting_payload(setting), "appearance_anchor": "后来变更"})
        with patch("studio.services.media_generation_appearances.resolve_media_appearance_target",
                side_effect=AssertionError("Detail must not resolve current sources")), \
                patch("studio.services.media_generation_targets.read_media_generation_target",
                    wraps=read_media_generation_target) as reader:
            response = self.client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["resolved_inputs"]["media_generation_target"], original)
        reader.assert_called_once()
        prep.refresh_from_db()
        self.assertEqual(prep.revision, original_revision)
        self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), (1, 1))
        self.assert_no_call()

    def test_detail_api_preserves_exact_variant_primary_without_file_probe(self):
        primary = self.primary_version()
        job, _ = self.create_job(declaration("variant", primary=primary))
        prep = job.execution_preparations.get()
        with patch("studio.services.media_generation_appearances.resolve_execution_asset_media",
                side_effect=AssertionError("Reading a target must not probe media")):
            response = self.client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        target = response.data["resolved_inputs"]["media_generation_target"]
        self.assertEqual(target["appearance"]["primary"]["version_id"], str(primary.pk))
        self.assertIsNone(target["setting"])
        self.assertEqual(target["appearance"]["primary_input"], declaration("variant", primary=primary)["primary_input"])
        self.assert_no_call()

    def test_detail_api_rejects_invalid_target_even_with_valid_snapshot_digest(self):
        from studio.services.media_generation_targets import _target_value
        def invalid_target(*args, **kwargs):
            kwargs["candidate_count"] = 1
            value = _target_value(*args, **kwargs)
            value["candidates"]["count"] = 0
            return value
        # Deliberately corrupt both the isolated writer's value and its expected
        # value before normal snapshot/hash storage. Restore real verification
        # before GET; no live or historical record is edited or repaired.
        with patch("studio.services.media_generation_targets._target_value", side_effect=invalid_target):
            job, _ = self.create_job(declaration())
        prep = job.execution_preparations.get()
        response = self.client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("正整数候选数", str(response.data))
        self.assert_no_call()

    def test_detail_api_no_appearance_does_not_infer_primary(self):
        job, _ = self.create_job(None)
        response = self.client.get(f"/api/v1/execution-preparations/{job.execution_preparations.get().pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("appearance", response.data["resolved_inputs"]["media_generation_target"])
        self.assert_no_call()

    def test_detail_api_authorizes_root_before_target_read(self):
        job, _ = self.create_job(declaration())
        other = get_user_model().objects.create_user(username="target-reader-foreign")
        self.client.force_authenticate(other)
        with patch("studio.services.media_generation_targets.read_media_generation_target") as reader:
            response = self.client.get(f"/api/v1/execution-preparations/{job.execution_preparations.get().pk}/")
        self.assertEqual(response.status_code, 404, response.data)
        reader.assert_not_called()
        self.assert_no_call()

    def test_primary_keeps_exact_old_setting_and_does_not_fake_generation(self):
        setting = create_setting(self.user, self.project, self.character)
        revise_variant(actor=self.user, variant=setting.variant, expected_revision=1,
            value={**setting_payload(setting), "appearance_anchor": "新修订"})
        request = declaration(setting=setting)
        job, created = self.create_job(request)
        prep = job.execution_preparations.get()
        target = read_media_generation_target(prep).value
        self.assertTrue(created)
        self.assertEqual(target["setting"], target["appearance"]["setting"])
        self.assertEqual(target["setting"]["setting_revision_id"], str(setting.pk))
        self.assertEqual(target["setting"]["revision"], 1)
        self.assertIsNone(target["appearance"]["primary"])
        self.assertEqual(prep.contract.intent["request"]["requested"]["appearance_target"], request)
        self.assertNotIn("appearance_anchor", str(target))
        self.assert_no_call()

    def test_primary_without_setting_does_not_infer_one(self):
        create_setting(self.user, self.project, self.character)
        job, _ = self.create_job(declaration())
        self.assertIsNone(read_media_generation_target(job.execution_preparations.get()).value["setting"])
        self.assert_no_call()

    def test_variant_preparation_freezes_original_primary_input_without_dispatch(self):
        primary = self.primary_version()
        request = declaration("variant", primary=primary)
        job, _ = self.create_job(request)
        prep = freeze(job, self.user)  # Admission is the existing isolated observation fixture.
        target = read_media_generation_target(prep).value
        self.assertEqual(target["appearance"]["primary"], {"version_id": str(primary.pk),
            "series_id": str(primary.series_id), "asset_id": str(self.assets[0].pk), "sha256": self.assets[0].checksum})
        self.assertIn(str(self.assets[0].pk), str(prep.contract.prepared_request))
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)  # No generated result/version invented.
        self.assert_no_call()

    def test_smoke_reuse_keeps_variant_declaration_and_frozen_inputs_through_api(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        target = declaration("variant", primary=self.primary_version())
        job, _ = self.create_job(target)
        prep = job.execution_preparations.get()
        original = deepcopy(prep.contract.resolved_inputs)
        response = self.client.get(f"/api/v1/execution-preparations/{prep.pk}/smoke-fixture/",
            {"expected_preparation_revision": prep.revision})
        self.assertEqual(response.status_code, 200, response.data)
        body = response.json()
        self.assertEqual(body.pop("execution_instance_version_id"), str(self.version.pk))
        self.assertEqual(body["fixture"]["appearance_target"], target)
        body.update(field_values=[], allow_external_calls=False)
        url = f"/api/v1/model-execution-instance-versions/{self.version.pk}/smoke-test/"
        with patch("studio.services.execution_smoke.resolve_execution_request_facts",
                side_effect=AssertionError("Reuse must keep original resolved inputs")):
            response = self.client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="variant-reuse")
        self.assertEqual(response.status_code, 202, response.data)
        reused = ExecutionPreparation.objects.get(pk=response.data["preparation"]["id"])
        self.assertEqual(reused.contract.resolved_inputs, original)
        self.assertEqual(reused.contract.intent["request"]["requested"]["appearance_target"], target)
        self.assertEqual(read_media_generation_target(reused).value, read_media_generation_target(prep).value)
        replay = self.client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="variant-reuse")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["attempt_id"], response.data["attempt_id"])
        self.assertEqual(AssetGenerationJob.objects.count(), 1)
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)
        self.assert_no_call()

    def test_smoke_reuse_rejects_missing_changed_or_new_variant_declaration(self):
        from studio.models import WorkflowSmokeAttempt
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        target = declaration("variant", primary=self.primary_version())
        job, _ = self.create_job(target)
        prep = job.execution_preparations.get()
        response = self.client.get(f"/api/v1/execution-preparations/{prep.pk}/smoke-fixture/",
            {"expected_preparation_revision": prep.revision})
        self.assertEqual(response.status_code, 200, response.data)
        body = response.json()
        body.pop("execution_instance_version_id")
        body.update(field_values=[], allow_external_calls=False)
        missing, changed, fresh = deepcopy(body), deepcopy(body), deepcopy(body)
        missing["fixture"].pop("appearance_target")
        changed["fixture"]["appearance_target"]["primary_version_id"] = str(uuid4())
        for key in ("reuse_preparation_id", "expected_reuse_revision", "field_selections"):
            fresh.pop(key)
        counts = (WorkflowSmokeAttempt.objects.count(), ExecutionPreparation.objects.count())
        for invalid in (missing, changed, fresh):
            with self.subTest(body=invalid):
                response = self.client.post(f"/api/v1/model-execution-instance-versions/{self.version.pk}/smoke-test/",
                    invalid, format="json", HTTP_IDEMPOTENCY_KEY="invalid-variant-reuse")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual((WorkflowSmokeAttempt.objects.count(), ExecutionPreparation.objects.count()), counts)
        self.assert_no_call()

    def test_variant_primary_bytes_reach_the_declared_image_port_through_original_worker(self):
        from studio.services.asset_generation import claim_next_comfy_asset_job, process_comfy_asset_job
        from studio.services.execution_asset_dispatch import dispatch_asset_execution
        # Select the second input explicitly: no first-slot or first-Asset guess.
        primary = self.primary_version(asset=self.assets[1])
        request = declaration("variant", primary=primary)
        request["primary_input"] = {"input_key": "reference_2", "item_key": "image_2"}
        difference = "Change only the jacket to red. Keep the supplied identity."
        job, _ = self.create_job(request, manual_channels={"positive_prompt": difference, "negative_prompt": ""})
        prep = freeze(job, self.user)
        original_graph = deepcopy(self.version.workflow_version.graph)
        peer, uploads = Mock(), RecordingUploadClient()
        with patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission", side_effect=isolated_observations), \
                patch("studio.services.asset_generation.driver_for", return_value=peer), \
                patch("studio.services.execution_workflow_staging.ExecutionWorkflowUploadClient", return_value=uploads):
            dispatch_asset_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
            peer.invoke.return_value = {"request_id": str(prep.pk)}
            submitted = process_comfy_asset_job(claim_next_comfy_asset_job("appearance-isolated-worker"))
        self.assertEqual(submitted.status, "submitted", submitted.error_message)
        graph = peer.invoke.call_args.args[2]["workflow"]
        from studio.services.workflow_versions import workflow_version_execution_snapshot
        prompt_binding = next(row for row in workflow_version_execution_snapshot(self.version.workflow_version)["semantic_bindings"]
            if row["semantic_key"] == "prompt.positive")
        self.assertEqual(graph[str(prompt_binding["node_id"])]["inputs"][prompt_binding["input_name"]], difference)
        prep.refresh_from_db()
        self.assertEqual(read_media_generation_target(prep).value["appearance"]["difference"],
            {"kind": "manual_channel", "key": "positive_prompt", "value_sha256": json_contract(difference).sha256})
        self.assertEqual(len(uploads.uploads), 3)
        primary_token = graph["11"]["inputs"]["image"]
        self.assertEqual(sha256(uploads.files[primary_token]).hexdigest(), primary.asset.checksum)
        self.assertNotEqual(primary_token, graph["10"]["inputs"]["image"])
        self.version.workflow_version.refresh_from_db()
        self.assertEqual(self.version.workflow_version.graph, original_graph)
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)
        self.assertIsNone(submitted.output_asset_id)  # No result or generated-from fact invented.
        self.external.assert_not_called()

    def test_difference_freezes_only_source_digest_and_preserves_each_sibling_request(self):
        primary = self.primary_version()
        request = declaration("variant", primary=primary)
        targets = []
        for index, body in enumerate(("A red jacket.", "A blue raincoat.")):
            channels = {"positive_prompt": body, "negative_prompt": ""}
            job, _ = self.create_job(request, key=f"difference-{index}", manual_channels=channels)
            prep = job.execution_preparations.get()
            target = read_media_generation_target(prep).value["appearance"]
            targets.append(target)
            self.assertEqual(prep.contract.intent["request"]["requested"]["manual_channels"], channels)
            self.assertNotIn(body, str(prep.contract.resolved_inputs))
            self.assertEqual(target["difference"]["value_sha256"], json_contract(body).sha256)
            with patch("studio.services.media_generation_appearances.resolve_media_appearance_target",
                    side_effect=AssertionError("Replay must not resolve current sources")):
                replay, created = self.create_job(request, key=f"difference-{index}", manual_channels=channels)
                self.assertFalse(created)
                self.assertEqual(replay.pk, job.pk)
            with self.assertRaisesMessage(ValidationError, "Idempotency-Key"):
                self.create_job(request, key=f"difference-{index}", manual_channels={**channels, "positive_prompt": "Changed"})
        self.assertEqual(targets[0]["primary"], targets[1]["primary"])
        self.assertNotEqual(targets[0]["difference"], targets[1]["difference"])
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)
        self.assert_no_call()

    def test_empty_unconsumed_and_wrong_mode_difference_roll_back_the_original_root(self):
        request = declaration("variant", primary=self.primary_version())
        for index, (source, channels) in enumerate((
                ({"kind": "manual_channel", "key": "negative_prompt"}, {"positive_prompt": "Valid", "negative_prompt": ""}),
                ({"kind": "manual_channel", "key": "negative_prompt"}, {"positive_prompt": "Valid", "negative_prompt": "   "}),
                ({"kind": "manual_channel", "key": "not_a_channel"}, {"positive_prompt": "Valid", "negative_prompt": ""}),
                ({"kind": "logical_input", "key": "reference_1"}, {"positive_prompt": "Valid", "negative_prompt": ""}))):
            with self.subTest(source=source, channels=channels), self.assertRaises(ValidationError):
                self.create_job({**request, "difference_input": source}, key=f"invalid-difference-{index}", manual_channels=channels)
            self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), (0, 0))
        self.assert_no_call()

    @security_boundary_resolution_cache()
    def test_difference_verification_rejects_changed_intent_or_digest_without_current_reads(self):
        job, _ = self.create_job(declaration("variant", primary=self.primary_version()))
        detail = job.execution_preparations.get().contract
        facts = deepcopy(detail.resolved_inputs)
        request = deepcopy(detail.intent["request"]["requested"])
        with self.assertNumQueries(0):
            verify_media_generation_target(self.definition, facts, request=request)
            for supplied in (None, {}, {**request, "manual_channels": {}},
                    {**request, "manual_channels": {"positive_prompt": "Another difference.", "negative_prompt": ""}}):
                with self.subTest(request=supplied), self.assertRaises(ValidationError):
                    verify_media_generation_target(self.definition, facts, request=supplied)
            for value in ({}, {"kind": "manual_channel", "key": "positive_prompt", "value_sha256": "0" * 64},
                    {"kind": "manual_channel", "key": "positive_prompt", "value_sha256": "bad"}):
                changed = deepcopy(facts)
                changed["media_generation_target"]["appearance"]["difference"] = value
                with self.subTest(difference=value), self.assertRaises(ValidationError):
                    verify_media_generation_target(self.definition, changed, request=request)
            changed = deepcopy(facts)
            # Even two valid nonempty sources are not interchangeable: the
            # target must retain the original declaration, not merely any input.
            changed["media_generation_target"]["appearance"]["difference"] = {
                "kind": "manual_channel", "key": "negative_prompt", "value_sha256": json_contract("No hat.").sha256}
            with self.assertRaisesMessage(ValidationError, "与原请求声明不一致"):
                verify_media_generation_target(self.definition, changed, request={**request,
                    "manual_channels": {**request["manual_channels"], "negative_prompt": "No hat."}})
        self.assert_no_call()

    def test_context_only_use_cannot_claim_primary_even_when_same_asset_has_another_real_port(self):
        primary = self.primary_version()
        job, _ = self.create_job(declaration("variant", primary=primary))
        prep = job.execution_preparations.get()
        facts = deepcopy(prep.contract.resolved_inputs)
        row = deepcopy(facts["inputs"]["logical_values"]["reference_1"][0])
        row.update(input_key="context_photo", item_key="context-only")
        row["source_sha256"] = json_contract({k: v for k, v in row.items() if k != "source_sha256"}).sha256
        facts["inputs"]["logical_values"]["context_photo"] = [row]
        request = declaration("variant", primary=primary)
        request["primary_input"] = {"input_key": "context_photo", "item_key": "context-only"}
        # Deliberately inject unbound upstream facts at the common target
        # boundary. The formal author rejects target-less image inputs; this
        # fixture does not claim a published context-only image configuration.
        resolved = ResolvedExecutionPreparation(json_contract(facts), json_contract(prep.contract.review_contract))
        with patch("studio.services.media_generation_targets.resolve_execution_request_facts", return_value=resolved) as resolve:
            with self.assertRaisesMessage(ValidationError, "上下文素材不能充当生成依据"):
                self.create_job(request, key="context-only-invalid")
            resolve.assert_not_called()
        self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), (1, 1))
        self.assertEqual(job.execution_preparations.get().contract.resolved_inputs, prep.contract.resolved_inputs)
        self.assert_no_call()

    def test_unbound_input_cannot_borrow_the_use_key_of_a_different_image_port(self):
        primary = self.primary_version()
        job, _ = self.create_job(declaration("variant", primary=primary))
        facts = deepcopy(job.execution_preparations.get().contract.resolved_inputs)
        row = deepcopy(facts["inputs"]["logical_values"]["reference_1"][0])
        row["input_key"] = "unbound"
        row["source_sha256"] = json_contract({k: v for k, v in row.items() if k != "source_sha256"}).sha256
        facts["inputs"]["logical_values"]["unbound"] = [row]
        appearance = deepcopy(facts["media_generation_target"]["appearance"])
        appearance["primary_input"]["input_key"] = "unbound"
        request = deepcopy(job.execution_preparations.get().contract.intent["request"]["requested"])
        request["appearance_target"]["primary_input"]["input_key"] = "unbound"
        with self.assertRaisesMessage(ValidationError, "上下文素材不能充当生成依据"):
            verify_media_appearance_target(appearance, facts["inputs"], definition=self.definition, request=request)
        self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), (1, 1))
        self.assert_no_call()

    def test_two_sibling_settings_keep_one_exact_primary_not_newest_version(self):
        setting = create_setting(self.user, self.project, self.character)
        primary = self.primary_version(setting=setting)
        newer = self.assets[1]
        newer.mime_type = "image/png"
        newer.save(update_fields=["mime_type"])
        attach_asset(actor=self.user, series_id=primary.series_id, asset_id=newer.pk, source="existing", setting_revision_id=setting.pk)
        targets = []
        for number in (1, 2):
            sibling = create_setting(self.user, self.project, self.character, primary_setting=setting)
            job, _ = self.create_job(declaration("variant", primary=primary, setting=sibling), key=f"sibling-{number}")
            targets.append(read_media_generation_target(job.execution_preparations.get()).value["appearance"])
        self.assertNotEqual(targets[0]["setting"], targets[1]["setting"])
        self.assertEqual(targets[0]["primary"], targets[1]["primary"])
        self.assertEqual(targets[0]["primary"]["version_id"], str(primary.pk))
        self.assertEqual(AssetReferenceVersion.objects.count(), 2)
        self.assert_no_call()

    def test_replay_never_reresolves_target_and_changed_target_conflicts(self):
        request = declaration("variant", primary=self.primary_version())
        job, _ = self.create_job(request)
        prep = job.execution_preparations.get()
        before = read_media_generation_target(prep).value
        with patch("studio.services.media_generation_appearances.resolve_media_appearance_target",
                side_effect=AssertionError("Replay must retain original target")):
            replay, created = self.create_job(request)
            self.assertFalse(created)
            self.assertEqual(replay.pk, job.pk)
            self.assertEqual(read_media_generation_target(prep).value, before)
        with self.assertRaisesMessage(ValidationError, "Idempotency-Key"):
            self.create_job(declaration())
        self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), (1, 1))
        self.assert_no_call()

    def test_missing_or_wrong_primary_use_fails_without_root(self):
        primary = self.primary_version()
        valid = declaration("variant", primary=primary)
        for use in ({"input_key": "missing", "item_key": "image_1"},
                {"input_key": "reference_1", "item_key": "missing"},
                {"input_key": "reference_2", "item_key": "image_2"}):
            with self.subTest(use=use), self.assertRaises(ValidationError):
                self.create_job({**valid, "primary_input": use})
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assert_no_call()

    def test_variant_cannot_claim_other_setting_revision_or_primary_role_setting(self):
        setting = create_setting(self.user, self.project, self.character)
        sibling = create_setting(self.user, self.project, self.character, primary_setting=setting)
        primary = self.primary_version()  # No setting revision cannot stand in for the fixed revision.
        for selected in (setting, sibling):
            with self.subTest(setting=selected.pk), self.assertRaises(ValidationError):
                self.create_job(declaration("variant", primary=primary, setting=selected))
        with self.assertRaises(ValidationError):
            self.create_job(declaration(setting=sibling))
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assert_no_call()

    def test_foreign_primary_setting_or_sibling_asset_version_cannot_be_basis(self):
        primary = self.primary_version()
        sibling = create_series(actor=self.user, values={"role": "variant", "name": "组织关联不是生成依据",
            "primary_series_id": str(primary.series_id)})
        version = attach_asset(actor=self.user, series_id=sibling.pk, asset_id=primary.asset_id, source="existing")[0]
        with self.assertRaises(ValidationError):
            self.create_job(declaration("variant", primary=version))
        foreign = get_user_model().objects.create_user(username="foreign-appearance")
        asset = Asset(owner=foreign, kind="reference", name="Foreign temporary fixture", checksum=self.assets[1].checksum)
        # A real tenant-owned temporary file, not changing ownership on another
        # tenant's existing path (which the production reader correctly rejects).
        with self.assets[1].file.open("rb") as source:
            asset.file.save("foreign.png", ContentFile(source.read()))
        foreign_version = self.primary_version(asset=asset, owner=foreign)
        with self.assertRaises(ValidationError):
            self.create_job(declaration("variant", primary=foreign_version))
        project = SeriesProject.objects.create(owner=foreign, title="Foreign", **PROJECT_CONFIG)
        character = CharacterBible.objects.create(project=project, name="Foreign")
        setting = create_setting(foreign, project, character)
        with self.assertRaises(ValidationError):
            self.create_job(declaration(setting=setting))
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assert_no_call()

    def test_corrupt_primary_file_identity_rolls_back_original_root(self):
        primary = self.primary_version()
        Asset.objects.filter(pk=primary.asset_id).update(checksum="0" * 64)
        with self.assertRaises(ValidationError):
            self.create_job(declaration("variant", primary=primary))
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assert_no_call()

    def http_payload(self, target):
        return {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "job_type": "asset", "media_type": "image", "image_category": "reference", "name": "Primary HTTP fixture",
            "parameters": {"image.width": 16, "image.height": 24, "sampling.seed": 42, "sampling.cfg_scale": 6.5, "sampling.scheduler": "karras"},
            "inputs": self.inputs, "manual_channels": {"positive_prompt": "Explicit original inputs", "negative_prompt": ""},
            "subject_facts": [], "source_language": "", "target_language": "", "field_values": [], "appearance_target": target}

    def post_target(self, target, *, key="http-primary", **changes):
        return self.client.post("/api/v1/asset-generation-jobs/", {**self.http_payload(target), **changes},
            format="json", HTTP_IDEMPOTENCY_KEY=key)

    def test_http_primary_exact_old_setting_replay_and_changed_intent_conflict(self):
        setting = create_setting(self.user, self.project, self.character)
        target = declaration(setting=setting)
        revise_variant(actor=self.user, variant=setting.variant, expected_revision=1,
            value={**setting_payload(setting), "appearance_anchor": "New setting is not selected"})
        response = self.post_target(target)
        self.assertEqual(response.status_code, 202, response.data)
        job = AssetGenerationJob.objects.get(pk=response.data["id"])
        prep = job.execution_preparations.get()
        value = read_media_generation_target(prep).value
        self.assertEqual(value["appearance"]["role"], "primary")
        self.assertEqual(value["setting"]["setting_revision_id"], str(setting.pk))
        self.assertEqual(value["setting"]["revision"], 1)
        self.assertEqual(prep.contract.intent["request"]["requested"]["appearance_target"], target)
        repeated = self.post_target(target)
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(str(repeated.data["id"]), str(job.pk))
        conflict = self.post_target(declaration())
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), (1, 1))
        self.assert_no_call()

    def test_http_optional_setting_and_absent_role_are_not_inferred(self):
        for key, target in (("no-setting", declaration()), ("no-target", None)):
            response = self.post_target(target, key=key)
            self.assertEqual(response.status_code, 202, response.data)
            prep = AssetGenerationJob.objects.get(pk=response.data["id"]).execution_preparations.get()
            value = read_media_generation_target(prep).value
            self.assertIsNone(value["setting"])
            if target is None:
                self.assertNotIn("appearance", value)
            else:
                self.assertEqual(value["appearance"]["role"], "primary")
        self.assert_no_call()

    def test_http_primary_rejects_unknown_keys_basis_and_wrong_job_media(self):
        for target in ({**declaration(), "approved": True}, {**declaration(), "primary_input": {}},
                {**declaration(), "primary_version_id": str(uuid4())}, {**declaration(), "setting_revision_id": "latest"},
                {"role": "primary"}):
            with self.subTest(target=target):
                response = self.post_target(target)
                self.assertEqual(response.status_code, 400, response.data)
        for changes in ({"job_type": "prompt"}, {"media_type": "video"}):
            response = self.post_target(declaration(), **changes)
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn("只有图片素材", str(response.data))
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assert_no_call()

    def test_http_primary_rejects_foreign_setting_and_variant_setting(self):
        other = get_user_model().objects.create_user(username="primary-http-other")
        project = SeriesProject.objects.create(owner=other, title="Foreign", **PROJECT_CONFIG)
        character = CharacterBible.objects.create(project=project, name="Foreign")
        foreign = create_setting(other, project, character)
        own = create_setting(self.user, self.project, self.character)
        variant = create_setting(self.user, self.project, self.character, primary_setting=own)
        for setting in (foreign, variant):
            response = self.post_target(declaration(setting=setting))
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assert_no_call()

    def test_primary_directory_is_owner_scoped_paginated_and_body_free(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        first = create_setting(self.user, self.project, self.character)
        create_setting(self.user, self.project, self.character, primary_setting=first)
        second_character = CharacterBible.objects.create(project=self.project, name="Second courier", slug="second", stable_speaker_id="second")
        second = create_setting(self.user, self.project, second_character)
        revise_variant(actor=self.user, variant=first.variant, expected_revision=1,
            value={**setting_payload(first), "appearance_anchor": "Newest revision"})
        other = get_user_model().objects.create_user(username="directory-other")
        project = SeriesProject.objects.create(owner=other, title="Invisible", **PROJECT_CONFIG)
        character = CharacterBible.objects.create(project=project, name="Invisible")
        create_setting(other, project, character)
        url = "/api/v1/asset-generation-jobs/appearance-options/?role=primary"
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url, {"role": "primary", "page_size": 1, "page": 1, "search": "Appearance fixture"})
        self.assertEqual(response.status_code, 200, response.data)
        result = response.json()
        self.assertEqual(result["count"], 2)
        self.assertIsNotNone(result["next"])
        self.assertEqual(len(result["results"]), 1)
        next_page = self.client.get(url, {"role": "primary", "page_size": 1, "page": 2}).json()
        rows = result["results"] + next_page["results"]
        self.assertEqual({row["setting_revision_id"] for row in rows},
            {str(first.variant.revisions.get(revision=2).pk), str(second.pk)})
        for row in rows:
            self.assertEqual(row["project_id"], str(self.project.pk))
            self.assertEqual(set(row), {"setting_revision_id", "appearance_id", "project_id", "project_title",
                "entity_name", "name", "reference_name", "revision"})
        self.assertNotIn("identity_anchor", " ".join(item["sql"] for item in queries.captured_queries))
        self.assertNotIn("appearance_anchor", " ".join(item["sql"] for item in queries.captured_queries))
        self.assertEqual(self.client.get(url, {"role": "primary", "search": "Invisible"}).json()["count"], 0)
        for query in ("&page=1&page=2", "&role=variant", "&page=0", "&search=" + "x" * 161):
            self.assertEqual(self.client.get(url + query).status_code, 400)
        original_options = self.client.get("/api/v1/asset-generation-jobs/options/?job_type=asset&media_type=image&role=primary")
        self.assertEqual(original_options.status_code, 400, original_options.data)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(url).status_code, (401, 403))
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assert_no_call()

    def test_http_variant_exact_primary_and_difference_create_replay_and_conflict(self):
        primary = self.primary_version()
        target = declaration("variant", primary=primary)
        response = self.post_target(target, key="variant-http")
        self.assertEqual(response.status_code, 202, response.data)
        job = AssetGenerationJob.objects.get(pk=response.data["id"])
        prep = job.execution_preparations.get()
        self.assertEqual(prep.contract.intent["request"]["requested"]["appearance_target"], target)
        self.assertEqual(read_media_generation_target(prep).value["appearance"]["role"], "variant")
        replay = self.post_target(target, key="variant-http")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(str(replay.data["id"]), str(job.pk))
        conflict = self.post_target(declaration(), key="variant-http")
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count(), ProductionRun.objects.count()), (1, 1, 0))
        self.assert_no_call()

    def test_http_variant_invalid_nested_sources_and_missing_text_roll_back(self):
        target = declaration("variant", primary=self.primary_version())
        invalid = [
            {**target, "difference_input": {"kind": "manual_channel", "key": "positive_prompt", "text": "extra"}},
            {**target, "difference_input": {"kind": "reference_field", "key": "ref", "field_key": "detail"}},
            {**target, "difference_input": {"kind": "structured_field", "key": "ref", "field_key": "detail", "unit_key": "extra"}},
            {**target, "primary_input": {"input_key": "reference_1", "item_key": "image_2"}},
        ]
        for value in invalid:
            with self.subTest(value=value):
                response = self.post_target(value)
                self.assertEqual(response.status_code, 400, response.data)
        response = self.post_target(target, manual_channels={"positive_prompt": "", "negative_prompt": ""})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assert_no_call()

    def test_variant_directory_requires_role_and_filters_current_owned_revisions(self):
        from studio.services.media_generation_appearances import appearance_options
        primary = create_setting(self.user, self.project, self.character)
        variant = create_setting(self.user, self.project, self.character, primary_setting=primary)
        url = "/api/v1/asset-generation-jobs/appearance-options/"
        self.assertEqual(self.client.get(url).status_code, 400)
        response = self.client.get(url, {"role": "variant"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["setting_revision_id"] for row in response.json()["results"]], [str(variant.pk)])
        query = appearance_options(owner=self.user, role="variant")
        self.assertNotIn("identity_anchor", str(query.query))
        self.assertNotIn("appearance_anchor", str(query.query))
        self.assertTrue(query.explain())
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assert_no_call()

    def test_http_variant_missing_primary_version_is_rejected_before_creation(self):
        response = self.client.post("/api/v1/asset-generation-jobs/",
            self.http_payload(declaration("variant")), format="json", HTTP_IDEMPOTENCY_KEY="variant-not-ready")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("appearance_target", str(response.data))
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assert_no_call()

    def test_http_variant_unmapped_primary_input_reports_key_before_resolving_assets(self):
        target = declaration("variant", primary=self.primary_version())
        target["primary_input"] = {"input_key": "context_photo", "item_key": "image_1"}
        with patch("studio.services.media_generation_targets.resolve_execution_request_facts",
                side_effect=AssertionError("Invalid port must fail before input resolution")) as resolve:
            response = self.post_target(target, key="unmapped-primary-http")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("appearance_target", str(response.data))
        self.assertIn("context_photo", str(response.data))
        self.assertIn("主图片端口", str(response.data))
        resolve.assert_not_called()
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assert_no_call()

    @security_boundary_resolution_cache()
    def test_frozen_verifier_rejects_input_and_target_tampering_without_database_reads(self):
        # The original request warms configured size limits in this operation;
        # verification must not resolve a current Asset, setting or instance.
        job, _ = self.create_job(declaration("variant", primary=self.primary_version()))
        facts = deepcopy(job.execution_preparations.get().contract.resolved_inputs)
        request = deepcopy(job.execution_preparations.get().contract.intent["request"]["requested"])
        appearance = facts["media_generation_target"]["appearance"]
        with self.assertNumQueries(0):
            verify_media_appearance_target(appearance, facts["inputs"], definition=self.definition,
                resolved_inputs=facts, request=request)
            for logical in (None, [], {}, {"reference_1": []}):
                with self.subTest(logical=logical), self.assertRaises(ValidationError):
                    verify_media_appearance_target(appearance, {"logical_values": logical}, definition=self.definition, request=request)
            for key, value in (("asset_id", str(uuid4())), ("media_facts", None), ("kind", "text")):
                changed = deepcopy(facts["inputs"])
                changed["logical_values"]["reference_1"][0][key] = value
                with self.subTest(key=key), self.assertRaises(ValidationError):
                    verify_media_appearance_target(appearance, changed, definition=self.definition, request=request)
            repeated = deepcopy(facts["inputs"])
            repeated["logical_values"]["reference_1"] *= 2
            with self.assertRaises(ValidationError):
                verify_media_appearance_target(appearance, repeated, definition=self.definition, request=request)
            for slots in (None, {}, {**facts["inputs"]["workflow_slots"], "asset.image.source_1": []}):
                changed = {**facts["inputs"], "workflow_slots": slots}
                with self.subTest(slots=slots), self.assertRaises(ValidationError):
                    verify_media_appearance_target(appearance, changed, definition=self.definition, request=request)
            changed = deepcopy(facts["inputs"])
            changed["logical_values"]["reference_1"][0]["source_sha256"] = "0" * 64
            with self.assertRaisesMessage(ValidationError, "素材来源事实摘要不一致"):
                verify_media_appearance_target(appearance, changed, definition=self.definition, request=request)
        changed = deepcopy(facts)
        changed["media_generation_target"]["setting"] = {"invented": True}
        with self.assertRaises(ValidationError):
            verify_media_generation_target(self.definition, changed, request=request)
        self.assert_no_call()


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class AppearanceRecipePreparationTests(TestCase):
    setUpTestData = classmethod(recipe_fixtures.RecipeBibleRegenerationTests.setUpTestData.__func__)
    write_contract = staticmethod(recipe_fixtures.RecipeBibleRegenerationTests.write_contract)
    seed_rule = recipe_fixtures.RecipeBibleRegenerationTests.seed_rule

    def setUp(self):
        self.external = self.enterContext(patch("requests.sessions.Session.request",
            side_effect=AssertionError("Isolated recipe preparation must not contact a model")))
        recipe_fixtures.RecipeBibleRegenerationTests.setUp(self)

    def prepare(self, number):
        # The original fixture calls this after creating its real entity and
        # before first preparation: no deletion, fake rows or fabricated results.
        if not hasattr(self, "setting"):
            self.setting = create_setting(self.actor, self.project, self.character)
        return self.prepare_target(declaration(setting=self.setting), number=number)

    def prepare_target(self, target, *, number=1):
        return prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
            scope=self.scope, candidate_number=number, actor=self.actor, appearance_target=target)

    def test_recipe_detail_api_reads_same_contract_and_original_stage_presentation(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.actor)
        prep = self.initial[0]
        target = read_media_generation_target(prep).value
        counts = (ProductionRun.objects.count(), ExecutionPreparation.objects.count())
        response = client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        body = response.json()
        self.assertEqual(body["resolved_inputs"]["media_generation_target"], target)
        self.assertEqual(body["stage_run"], str(self.stage.pk))
        self.assertEqual(body["presentation"]["kind"], "primary")
        self.assertEqual(target["candidates"]["count"], 2)
        self.assertEqual(target["setting"]["setting_revision_id"], str(self.setting.pk))
        self.assertEqual((ProductionRun.objects.count(), ExecutionPreparation.objects.count()), counts)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.external.assert_not_called()

    def test_original_roots_share_primary_target_and_intent_with_no_new_run(self):
        recipe = self.initial[0]
        before = ProductionRun.objects.count()
        job, _ = create_asset_generation_job(owner=self.actor, client_idempotency_key="image-target",
            execution_instance_version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            job_type="asset", media_type="image", image_category="reference", name="Independent primary",
            field_values=[], inputs={}, parameters={"width": 512, "height": 512, "seed": 7},
            manual_channels={}, subject_facts=[], source_language=self.project.language, target_language=self.project.language,
            appearance_target=declaration(setting=self.setting))
        asset = job.execution_preparations.get()
        a, b = read_media_generation_target(asset).value, read_media_generation_target(recipe).value
        self.assertEqual({**b, "candidates": a["candidates"]}, a)
        self.assertEqual(a["appearance"]["role"], "primary")
        self.assertEqual(a["setting"]["setting_revision_id"], str(self.setting.pk))
        self.assertEqual(ProductionRun.objects.count(), before)
        self.assertIsNone(asset.stage_run_id)
        self.assertIsNone(recipe.asset_job_id)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.external.assert_not_called()

    def test_recipe_replay_retains_original_and_changed_declaration_conflicts(self):
        with patch("studio.services.media_generation_appearances.resolve_media_appearance_target",
                side_effect=AssertionError("Do not reread target on replay")):
            self.assertEqual(self.prepare(1).pk, self.initial[0].pk)
        with self.assertRaises(ValidationError):
            self.prepare_target(declaration())
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_variant_without_its_own_routing_cannot_reuse_primary_entity_unit(self):
        with self.assertRaisesMessage(ValidationError, "variant_routing"):
            self.prepare_target({**declaration("variant"), "primary_version_id": str(uuid4())})
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_unreviewed_entity_cannot_allocate_unit_by_claiming_another_primary(self):
        other = CharacterBible.objects.create(project=self.project, name="Another entity",
            slug="another-entity", stable_speaker_id="S2")
        with self.assertRaisesMessage(ValidationError, "没有此单元的唯一主图目标"):
            prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
                scope={"kind": "character", "id": str(other.pk)}, candidate_number=1,
                actor=self.actor, appearance_target=declaration(setting=self.setting))
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipePrimaryTargetWiringTests(TestCase):
    """Use the scheduler's ordinary author path, without a target override."""
    setUpTestData = classmethod(recipe_fixtures.RecipeBibleRegenerationTests.setUpTestData.__func__)
    write_contract = staticmethod(recipe_fixtures.RecipeBibleRegenerationTests.write_contract)
    seed_rule = recipe_fixtures.RecipeBibleRegenerationTests.seed_rule
    request = recipe_fixtures.RecipeBibleRegenerationTests.request

    def setUp(self):
        self.external = self.enterContext(patch("requests.sessions.Session.request",
            side_effect=AssertionError("Recipe target tests must not contact a model")))
        recipe_fixtures.RecipeBibleRegenerationTests.setUp(self)

    def prepare(self, number, generation_request=None):
        if not hasattr(self, "setting"):
            self.setting = create_setting(self.actor, self.project, self.character)
        return recipe_fixtures.RecipeBibleRegenerationTests.prepare(self, number, generation_request)

    def revise_setting(self):
        variant = revise_variant(actor=self.actor, variant=self.setting.variant, expected_revision=1,
            value={**setting_payload(self.setting), "appearance_anchor": "红色外套"})
        return variant.revisions.get(revision=2)

    def test_regular_recipe_path_and_standalone_declaration_share_exact_primary_target(self):
        prep = self.initial[0]
        target = read_media_generation_target(prep).value
        intent = prep.contract.intent["request"]["requested"]
        self.assertEqual(intent["appearance_target_source"], "bible_primary")
        self.assertNotIn("appearance_target", intent)
        self.assertEqual(target["appearance"], {"role": "primary", "setting": target["setting"],
            "primary": None, "primary_input": None})
        self.assertEqual(target["setting"]["setting_revision_id"], str(self.setting.pk))
        self.assertEqual(target["setting"], prep.contract.resolved_inputs["bible_target"]["primary_setting"])
        self.assertEqual(target, read_media_generation_target(self.initial[1]).value)
        job, _ = create_asset_generation_job(owner=self.actor, client_idempotency_key="explicit-primary",
            execution_instance_version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            job_type="asset", media_type="image", image_category="reference", name="Isolated primary",
            field_values=[], inputs={}, parameters={"width": 512, "height": 512, "seed": 7},
            manual_channels={}, subject_facts=[], source_language=self.project.language, target_language=self.project.language,
            appearance_target=declaration(setting=self.setting))
        asset_target = read_media_generation_target(job.execution_preparations.get()).value
        self.assertEqual({**target, "candidates": asset_target["candidates"]}, asset_target)
        self.assertEqual(ProductionRun.objects.count(), 1)
        self.assertEqual(ExecutionPreparation.objects.count(), 3)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(self.stage.artifact_selection_decisions.exists())
        self.external.assert_not_called()

    def test_replay_and_detail_keep_original_setting_after_new_revision(self):
        from rest_framework.test import APIClient
        self.revise_setting()
        client = APIClient()
        client.force_authenticate(self.actor)
        with patch("studio.services.production_bible_targets.capture_bible_target",
                side_effect=AssertionError("Replay must not recapture current primary")), patch(
                "studio.services.media_generation_targets.resolve_execution_request_facts",
                side_effect=AssertionError("Replay must not resolve again")):
            self.assertEqual(self.prepare(1).pk, self.initial[0].pk)
            response = client.get(f"/api/v1/execution-preparations/{self.initial[0].pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        target = response.json()["resolved_inputs"]["media_generation_target"]
        self.assertEqual(target["appearance"]["setting"]["setting_revision_id"], str(self.setting.pk))
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.external.assert_not_called()

    def test_original_scheduler_replays_targets_without_creating_another_preparation(self):
        from studio.services.production_recipe_media_stages import advance_recipe_media_stage
        from studio.services.production_runs import _freeze_stage_input
        _freeze_stage_input(self.run, self.stage)
        with patch("studio.services.production_recipe_media_stages.advance_execution_preparation",
                side_effect=lambda preparation_id, **kwargs: ExecutionPreparation.objects.get(pk=preparation_id)) as advance:
            self.assertIsNone(advance_recipe_media_stage(self.run, self.stage))
            self.assertIsNone(advance_recipe_media_stage(self.run, self.stage))
        self.assertEqual(advance.call_count, 4)
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.external.assert_not_called()

    def test_regeneration_cannot_bypass_stale_content_review_with_new_setting(self):
        from studio.services.production_runs import _materialize_bible_regeneration
        self.revise_setting()
        intent = self.request()
        before = self.stage.route_events.count()
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run):
            with self.assertRaisesMessage(ValidationError, "已审核内容已变更"), transaction.atomic():
                _materialize_bible_regeneration(self.run, self.stage, self.review, intent=intent)
        for prep in self.initial:
            self.assertEqual(read_media_generation_target(prep).value["setting"]["setting_revision_id"], str(self.setting.pk))
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertEqual(self.stage.route_events.count(), before)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.character.refresh_from_db()
        self.assertIsNone(self.character.anchor_asset_id)
        self.external.assert_not_called()

    def test_new_entity_without_frozen_demand_cannot_override_a_primary_target(self):
        other = CharacterBible.objects.create(project=self.project, name="Another", slug="other", stable_speaker_id="S2")
        create_setting(self.actor, self.project, other)
        with self.assertRaisesMessage(ValidationError, "没有此单元的唯一主图目标"):
            prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
                scope={"kind": "character", "id": str(other.pk)}, candidate_number=1, actor=self.actor,
                appearance_target=declaration())
        self.assertEqual(ExecutionPreparation.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.external.assert_not_called()
