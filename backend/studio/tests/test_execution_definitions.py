"""Complete-definition and lifecycle tests in an isolated database, no AI calls."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import (
    ExecutionVersionContract, ModelDeployment, ModelDeploymentSmokeAttempt,
    ModelProfileVersion, PlatformLifecycleAuditEvent, ProviderProfile,
)
from studio.services.execution_definitions import (
    build_execution_definition, execution_resource_interface, verify_current_execution_definition, verify_stored_execution_definition,
)
from studio.services.execution_instances import (
    ExecutionResourceSelection, resolve_execution_resources,
    clone_execution_draft, create_execution_draft, create_execution_instance, publish_execution_version,
    retire_execution_version, update_execution_draft,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters, resolve_execution_definition_parameters
from studio.tests.test_async_media_p0 import _async_spec, _provider_config


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionDefinitionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = get_user_model().objects.create_user(username="definition-admin", is_staff=True)
        cls.provider = ProviderProfile(key="definition-provider", name="Fixture", driver_type="async_rest", modalities="video")
        cls.config = _provider_config()
        cls.config["request_field_map"].update(fps="fps", duration_seconds="duration")
        cls.write_contract(cls.provider, "driver_config", cls.config)
        cls.model = ModelProfileVersion(
            profile_key="definition-model", version=1, model_identifier="cloud-v1",
            modality="video", provider=cls.provider, status="published",
        )
        cls.write_contract(cls.model, "specification", _async_spec())
        cls.deployment = ModelDeployment(key="definition-target", name="Fixture", profile_version=cls.model)
        cls.write_contract(cls.deployment, "runtime_config", {})
        cls.instance = create_execution_instance(key="definition-instance", name="Fixture", description="", actor=cls.actor)
        cls.version = create_execution_draft(
            instance_id=cls.instance.pk, expected_revision=cls.instance.revision, actor=cls.actor,
            selection={"deployment": cls.deployment, "operation": "video.text_to_video", "mode": "T2VA",
                       "prompt_mode": "manual", "prompt_profile_version": None, "workflow_version": None},
        )

    @staticmethod
    def write_contract(obj, field, value):
        assign_json_contract(obj, field, deepcopy(value))
        obj.save()

    def setUp(self):
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential", "studio.services.comfyui.ComfyUIClient"):
            guard = patch(target, side_effect=AssertionError("Definition must not call a provider or decrypt"))
            guard.start()
            self.addCleanup(guard.stop)

    def definition(self):
        return build_execution_definition(self.version.pk)

    def test_project_modes_use_published_exact_instances_not_base_smoke(self):
        from studio.services.execution_catalog_readiness import configured_video_conditioning_modes
        from studio.capability_views import VideoConditioningCapabilityView

        self.assertEqual(configured_video_conditioning_modes(self.actor), [])
        published = self.publish()
        modes = configured_video_conditioning_modes(self.actor)
        self.assertEqual([mode["key"] for mode in modes], ["T2VA"])
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        self.assertEqual(configured_video_conditioning_modes(None), [])
        result, digest = VideoConditioningCapabilityView._conditioning_capability(self.actor)
        self.assertEqual(result, modes)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.instance.is_active = False
        self.instance.save(update_fields=("is_active",))
        self.assertEqual(configured_video_conditioning_modes(self.actor), [])
        self.instance.is_active = True
        self.instance.save(update_fields=("is_active",))
        ExecutionVersionContract.objects.filter(version=published).update(sha256="0" * 64)
        with self.assertRaises(ValidationError):
            configured_video_conditioning_modes(self.actor)

    def publish(self, **changes):
        args = {"version_id": self.version.pk, "expected_revision": self.version.revision,
                "expected_definition_sha256": self.definition().sha256, "actor": self.actor}
        return publish_execution_version(**{**args, **changes})

    def test_complete_definition_is_deterministic_readonly_and_has_no_smoke_defaults(self):
        before = PlatformLifecycleAuditEvent.objects.count()
        first = self.definition()
        self.assertEqual(first.sha256, self.definition().sha256)
        self.assertFalse(ExecutionVersionContract.objects.exists())
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        value = first.value
        self.assertEqual(value["configuration"]["operation"], "video.text_to_video")
        self.assertEqual(value["interface"]["output"], {"adapter": "async_rest", "kind": "video"})
        self.assertEqual(value["interface"]["prompt"]["mode"], "manual")
        self.assertEqual(value["interface"]["parameters"]["fps"]["rule"]["enum"], [24])
        self.assertNotIn("default", value["interface"]["parameters"]["width"]["rule"])
        encoded = json.dumps(value)
        for hidden in (self.config["base_url"], "smoke_inputs", "capability_ttl_seconds", "request_field_map"):
            self.assertNotIn(hidden, encoded)

    def test_definition_reads_do_not_acquire_registry_write_guard(self):
        from studio.services.execution_definitions import execution_authoring_options, read_execution_input_contract
        with patch("studio.services.registry_reference_lock.registry_reference_write_guard",
                side_effect=AssertionError("read-only definition must not lock database writers")):
            expected = self.definition()
            self.assertEqual(execution_authoring_options(self.version.pk)["version_id"], str(self.version.pk))
            self.assertFalse(read_execution_input_contract(self.version.pk)["frozen"])
        published = self.publish()
        with patch("studio.services.registry_reference_lock.registry_reference_write_guard",
                side_effect=AssertionError("read-only evidence must not lock database writers")):
            self.assertEqual(verify_stored_execution_definition(published.contract).sha256, expected.sha256)
            self.assertEqual(verify_current_execution_definition(published.contract).sha256, expected.sha256)
            contract = read_execution_input_contract(published.pk)
            self.assertTrue(contract["frozen"])
            self.assertEqual(contract["subject_categories"], [
                {"value": "character", "label": "人物"},
                {"value": "scene", "label": "场景"},
                {"value": "prop", "label": "道具"},
            ])
            self.assertEqual(contract["sha256"], expected.sha256)

    def test_definition_read_rejects_a_concurrently_changed_draft(self):
        from studio.models import ModelExecutionInstanceVersion
        from studio.services.execution_definitions import execution_version_configuration
        def changed(version):
            result = execution_version_configuration(version)
            ModelExecutionInstanceVersion.objects.filter(pk=version.pk).update(revision=version.revision + 1)
            return result
        with patch("studio.services.execution_definitions.execution_version_configuration", side_effect=changed):
            with self.assertRaisesMessage(ValidationError, "实例配置在读取期间变化"):
                self.definition()

    def test_resource_selection_has_no_instance_identity_and_shares_the_existing_projection(self):
        from dataclasses import replace
        from studio.models import ModelExecutionInstanceVersion
        before = ModelExecutionInstanceVersion.objects.count()
        selection = ExecutionResourceSelection.from_version(self.version)
        self.assertFalse(hasattr(selection, "pk"))
        self.assertFalse(hasattr(selection, "instance"))
        self.assertFalse(hasattr(selection, "parameters"))
        resources = resolve_execution_resources(selection)
        self.assertEqual(resources, resolve_execution_resources(self.version))
        interface, _, _ = execution_resource_interface(selection, resources)
        definition = self.definition().value
        self.assertEqual(interface["parameters"], definition["interface"]["parameters"])
        self.assertEqual(interface["output"], definition["interface"]["output"])
        self.assertEqual(interface["input_targets"], definition["interface"]["input_targets"])
        self.assertNotIn("smoke_inputs", json.dumps(interface))
        self.assertEqual(ModelExecutionInstanceVersion.objects.count(), before)
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        with self.assertRaises(ValidationError):
            execution_resource_interface(self.version, resources)
        with self.assertRaises(ValidationError):
            resolve_execution_resources(replace(selection, operation="video.unsupported"))

    def test_missing_optional_driver_mapping_is_a_real_publish_gap(self):
        config = deepcopy(self.config)
        del config["request_field_map"]["fps"]
        self.write_contract(self.provider, "driver_config", config)
        with self.assertRaisesMessage(ValidationError, "fps"):
            self.definition()
        self.assertFalse(ExecutionVersionContract.objects.exists())

    def test_complete_actual_model_definition_resolves_without_smoke_defaults(self):
        definition = self.definition()
        supplied = {"width": 512, "height": 512, "fps": 24, "duration_seconds": 1}
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("No automatic seed declared")):
            resolved = initialize_execution_definition_parameters(definition, supplied, policies=[], resolve_source=lambda _: self.fail("No source declared"))
            replay = resolve_execution_definition_parameters(definition, supplied, policies=[], resolve_source=lambda _: self.fail("No source declared"), generated_values={})
        self.assertEqual(resolved, replay)
        self.assertEqual(resolved["values"], supplied)
        with self.assertRaises(ValidationError):
            initialize_execution_definition_parameters(definition, {}, policies=[], resolve_source=lambda _: None)
        with self.assertRaises(ValidationError):
            initialize_execution_definition_parameters(definition, {**supplied, "fps": 25}, policies=[], resolve_source=lambda _: None)

    def test_publish_freezes_once_and_replays_without_smoke_or_duplicate_audit(self):
        expected = self.definition()
        published = self.publish()
        contract = published.contract
        self.assertEqual(published.status, "published")
        self.assertEqual(contract.sha256, expected.sha256)
        self.assertEqual(verify_current_execution_definition(contract).sha256, expected.sha256)
        self.assertEqual(published.published_by_id, self.actor.pk)
        self.assertEqual(published.revision, self.version.revision + 1)
        before = PlatformLifecycleAuditEvent.objects.count()
        replay = self.publish()
        self.assertEqual(replay.pk, published.pk)
        self.assertEqual(replay.revision, published.revision)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)
        self.assertEqual(ExecutionVersionContract.objects.count(), 1)
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())

    def test_stale_definition_or_revision_cannot_publish(self):
        for changes in ({"expected_definition_sha256": "0" * 64}, {"expected_revision": 900}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError) as caught:
                self.publish(**changes)
            self.assertEqual(caught.exception.code, "execution_definition_conflict")
        self.assertFalse(ExecutionVersionContract.objects.exists())
        self.version.refresh_from_db()
        self.assertEqual(self.version.status, "draft")

    def test_publish_replay_still_requires_a_typed_positive_revision(self):
        published = self.publish()
        for revision in (None, True, "1", 0):
            with self.subTest(revision=revision), self.assertRaises(ValidationError):
                self.publish(expected_revision=revision, expected_definition_sha256=published.contract.sha256)

    def test_target_change_after_review_requires_another_review(self):
        expected = self.definition().sha256
        self.write_contract(self.provider, "driver_config", {**self.config, "timeout_seconds": 29})
        with self.assertRaises(ValidationError):
            self.publish(expected_definition_sha256=expected)
        self.assertFalse(ExecutionVersionContract.objects.exists())

    def test_audit_failure_rolls_back_definition_status_and_revisions(self):
        self.instance.refresh_from_db()
        before_revision = self.instance.revision
        before_audit = PlatformLifecycleAuditEvent.objects.count()
        with patch("studio.services.execution_instances.record_platform_lifecycle_event", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.publish()
        self.version.refresh_from_db()
        self.instance.refresh_from_db()
        self.assertEqual(self.version.status, "draft")
        self.assertIsNone(self.version.published_at)
        self.assertEqual(self.instance.revision, before_revision)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before_audit)
        self.assertFalse(ExecutionVersionContract.objects.exists())

    def test_published_configuration_is_not_editable(self):
        published = self.publish()
        with self.assertRaises(ValidationError):
            update_execution_draft(version_id=published.pk, expected_revision=published.revision, changes={"mode": ""}, actor=self.actor)

    def test_published_successor_reuses_only_verified_author_choices(self):
        published = self.publish()
        clone = clone_execution_draft(source_id=published.pk, expected_revision=published.revision,
                                     expected_definition_sha256=published.contract.sha256, actor=self.actor)
        self.assertEqual(clone.status, "draft")
        self.assertEqual(clone.version, published.version + 1)
        self.assertEqual(clone.deployment_id, published.deployment_id)
        self.assertFalse(ExecutionVersionContract.objects.filter(version=clone).exists())
        self.assertIsNone(clone.published_at)

    def test_frozen_integrity_and_current_target_are_different_checks(self):
        published = self.publish()
        contract = published.contract
        self.write_contract(self.provider, "driver_config", {**self.config, "timeout_seconds": 29})
        self.assertEqual(verify_stored_execution_definition(contract).sha256, contract.sha256)
        with self.assertRaises(ValidationError) as caught:
            verify_current_execution_definition(contract)
        self.assertEqual(caught.exception.code, "execution_target_drift")
        with self.assertRaises(ValidationError):
            self.publish(expected_definition_sha256=contract.sha256)
        contract.refresh_from_db()
        self.assertEqual(contract.sha256, published.contract.sha256)

    def test_corrupt_or_incomplete_frozen_definition_is_not_accepted(self):
        contract = self.publish().contract
        original = deepcopy(contract.definition)
        contract.definition["interface"] = {}
        with self.assertRaises(ValidationError):
            verify_stored_execution_definition(contract)
        for value in ({"source": "placeholder"}, {**original, "version_id": "wrong-object"}, {**original, "interface": {}}):
            checked = json_contract(value)
            contract.definition, contract.sha256, contract.size_bytes = value, checked.sha256, checked.size_bytes
            with self.assertRaises(ValidationError):
                verify_stored_execution_definition(contract)

    def test_retire_keeps_frozen_contract_even_when_provider_config_is_broken(self):
        published = self.publish()
        contract = published.contract
        ProviderProfile.objects.filter(pk=self.provider.pk).update(driver_config={"invalid": True})
        args = {"version_id": published.pk, "expected_revision": published.revision,
                "expected_definition_sha256": contract.sha256, "reason": "替换配置", "actor": self.actor}
        retired = retire_execution_version(**args)
        self.assertEqual(retired.status, "retired")
        self.assertEqual(retired.contract.sha256, contract.sha256)
        before = PlatformLifecycleAuditEvent.objects.count()
        self.assertEqual(retire_execution_version(**args).revision, retired.revision)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)
        with self.assertRaises(ValidationError):
            retire_execution_version(**{**args, "reason": "different request"})

    def test_retirement_audit_failure_rolls_back_status(self):
        published = self.publish()
        before = PlatformLifecycleAuditEvent.objects.count()
        with patch("studio.services.execution_instances.record_platform_lifecycle_event", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                retire_execution_version(version_id=published.pk, expected_revision=published.revision,
                    expected_definition_sha256=published.contract.sha256, reason="替换配置", actor=self.actor)
        revision = published.revision
        published.refresh_from_db()
        self.assertEqual(published.status, "published")
        self.assertEqual(published.revision, revision)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)

    def test_unsupported_mode_is_explicit_not_a_partial_definition(self):
        update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision,
                               changes={"prompt_mode": "none"}, actor=self.actor)
        with self.assertRaisesMessage(ValidationError, "仍声明提示词通道"):
            self.definition()
        self.assertFalse(ExecutionVersionContract.objects.exists())


class WorkflowExecutionDefinitionTests(TestCase):
    """Synthetic published dependency; real local import/integrity validators.

    No mock of the Workflow/Projector verifier, no remote Smoke certification.
    """
    def setUp(self):
        from studio.tests.test_workflow_semantic_compiler import GenericWorkflowSemanticCompilerTests

        GenericWorkflowSemanticCompilerTests.setUp(self)
        assign_json_contract(self.provider, "driver_config", {
            "base_url": "http://127.0.0.1:8188", "capability_ttl_seconds": 3600,
            "timeout_seconds": 30, "poll_interval_seconds": 1, "max_poll_seconds": 60,
        })
        self.provider.save()
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential", "studio.services.comfyui.ComfyUIClient"):
            guard = patch(target, side_effect=AssertionError("No external call"))
            guard.start()
            self.addCleanup(guard.stop)
        self.deployment = ModelDeployment(key="workflow-definition", name="Fixture", profile_version=self.profile)
        assign_json_contract(self.deployment, "runtime_config", {})
        self.deployment.save()
        self.workflow = GenericWorkflowSemanticCompilerTests._version(
            self,
            graph={
                "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": self.artifact.filename}},
                "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "imported sample only", "clip": ["1", 1]}},
                "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
                "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
                "5": {"class_type": "KSampler", "inputs": {
                    "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0],
                    "seed": 0, "steps": 10, "cfg": 5.0, "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                }},
                "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
                "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "fixture"}},
            },
            parameters={
                "model.base.filename": {"type": "model_artifact", "requirement_alias": "base"},
                "prompt.positive": {"type": "string", "required": True, "default": "imported sample only"},
                "prompt.negative": {"type": "string", "required": False},
                "image.width": {"type": "integer", "required": True, "multiple_of": 8},
                "image.height": {"type": "integer", "required": True, "multiple_of": 8},
                "sampling.seed": {"type": "integer", "required": True, "min": 0},
            },
            bindings=[
                {"semantic_key": key, "node_id": node, "input_name": field, "value_type": kind}
                for key, node, field, kind in (
                    ("model.base.filename", "1", "ckpt_name", "model_artifact"),
                    ("prompt.positive", "2", "text", "string"), ("prompt.negative", "3", "text", "string"),
                    ("image.width", "4", "width", "integer"), ("image.height", "4", "height", "integer"),
                    ("sampling.seed", "5", "seed", "integer"),
                )
            ],
            outputs={"node_id": "7", "kind": "image", "required": True},
        )
        self.workflow.status = "published"
        self.workflow.validated_deployment = self.deployment
        self.workflow.save()
        self.instance = create_execution_instance(key="workflow-instance", name="Fixture", description="", actor=self.user)
        self.version = create_execution_draft(
            instance_id=self.instance.pk, expected_revision=self.instance.revision, actor=self.user,
            selection={"deployment": self.deployment, "operation": "image.text_to_image", "mode": "",
                       "prompt_mode": "manual", "prompt_profile_version": None, "workflow_version": self.workflow},
        )

    def test_definition_and_publish_use_exact_projector_output_and_parameter_contracts(self):
        definition = build_execution_definition(self.version.pk)
        interface = definition.value["interface"]
        self.assertEqual(interface["output"]["adapter"], self.workflow.input_contract["contract_projector"])
        self.assertEqual(interface["output"]["contract"]["primary"], {"node_id": "7", "kind": "image", "required": True})
        self.assertEqual(interface["model_inputs"], ["model.base.filename"])
        self.assertEqual(interface["parameters"]["image.width"]["rule"]["alignments"], [{"modulus": 8, "remainder": 0}])
        self.assertNotIn("default", interface["prompt"]["channels"]["positive_prompt"]["rule"])
        published = publish_execution_version(
            version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=definition.sha256, actor=self.user,
        )
        self.assertEqual(verify_current_execution_definition(published.contract).sha256, definition.sha256)

    def test_graph_corruption_cannot_be_published(self):
        self.workflow.graph["7"]["class_type"] = "UnsupportedOutput"
        self.workflow.save(update_fields=("graph",))
        with self.assertRaises(ValidationError):
            build_execution_definition(self.version.pk)
        self.assertFalse(ExecutionVersionContract.objects.exists())

    def test_definition_freezes_model_and_workflow_monitoring_with_its_exact_identity(self):
        from studio.services.execution_monitoring import idle_timeout_for_node
        from studio.services.workflow_versions import import_workflow_version, workflow_version_execution_snapshot

        assign_json_contract(self.profile, "specification", {**self.profile.specification,
            "execution_monitoring": {"schema_version": 1, "node_type_overrides": {"KSampler": 3600}}})
        self.profile.save()
        snapshot = workflow_version_execution_snapshot(self.workflow)
        workflow = import_workflow_version(template=self.workflow.template, graph=snapshot["graph"],
            input_contract={**snapshot["input_contract"], "execution_monitoring": {
                "schema_version": 1, "node_type_overrides": {}, "node_overrides": {"5": 5400}}},
            semantic_bindings=snapshot["semantic_bindings"], model_requirements=snapshot["model_requirements"],
            provider=self.provider, model_profile_version=self.profile)
        workflow.status = "published"
        workflow.validated_deployment = self.deployment
        workflow.save()
        self.instance.refresh_from_db()
        version = create_execution_draft(instance_id=self.instance.pk, expected_revision=self.instance.revision,
            actor=self.user, selection={"deployment": self.deployment, "operation": "image.text_to_image",
                "mode": "", "prompt_mode": "manual", "prompt_profile_version": None, "workflow_version": workflow})
        definition = build_execution_definition(version.pk)
        monitoring = definition.value["interface"]["execution_monitoring"]
        self.assertEqual(monitoring, {"schema_version": 1, "default_idle_timeout_seconds": None,
            "node_type_overrides": {"KSampler": 3600}, "node_overrides": {"5": 5400}})
        self.assertEqual(idle_timeout_for_node(monitoring, node_id="5", node_type="KSampler", frozen_runtime_idle_timeout_seconds=120), 5400)
        published = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.user)
        self.assertEqual(verify_current_execution_definition(published.contract).sha256, definition.sha256)
        # Stored evidence stays frozen; changing model policy invalidates current execution.
        assign_json_contract(self.profile, "specification", {**self.profile.specification,
            "execution_monitoring": {"schema_version": 1, "node_type_overrides": {"KSampler": 7200}}})
        self.profile.save()
        self.assertEqual(verify_stored_execution_definition(published.contract).value["interface"]["execution_monitoring"], monitoring)
        with self.assertRaises(ValidationError):
            verify_current_execution_definition(published.contract)

    def test_workflow_cannot_be_selected_with_another_deployment(self):
        other = ModelDeployment(key="workflow-other-target", name="Unvalidated target", profile_version=self.profile)
        assign_json_contract(other, "runtime_config", {})
        other.save()
        self.instance.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "校验部署与所选模型部署不一致"):
            create_execution_draft(
                instance_id=self.instance.pk, expected_revision=self.instance.revision, actor=self.user,
                selection={"deployment": other, "operation": "image.text_to_image", "mode": "",
                    "prompt_mode": "manual", "prompt_profile_version": None, "workflow_version": self.workflow},
            )
