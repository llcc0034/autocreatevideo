"""Video planning must not validate unrelated execution combinations.

Corrupt records below are isolated fixtures, never changes to live history.
All real resource/definition validation remains enabled; network is forbidden.
"""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ExecutionVersionContract, ModelDeployment, ModelDeploymentSmokeAttempt,
    ModelExecutionInstanceVersion, ModelProfileVersion, PlatformLifecycleAuditEvent,
    ProviderProfile,
)
from studio.services.execution_catalog_readiness import configured_video_conditioning_modes
from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
from studio.services.execution_instances import create_execution_instance
from studio.tests import test_execution_definitions as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class VideoConditioningCatalogTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionDefinitionTests.setUpTestData.__func__)
    setUp = fixtures.ExecutionDefinitionTests.setUp
    write_contract = staticmethod(fixtures.ExecutionDefinitionTests.write_contract)
    definition = fixtures.ExecutionDefinitionTests.definition
    publish = fixtures.ExecutionDefinitionTests.publish

    def unrelated_broken_version(self, kind, operation):
        provider = ProviderProfile.objects.create(key=kind, name=kind, driver_type="openai_compatible" if kind == "text" else "async_rest", modalities=kind)
        model = ModelProfileVersion.objects.create(profile_key=kind, version=1, model_identifier=kind, provider=provider, modality=kind, status="published")
        deployment = ModelDeployment.objects.create(key=kind, name=kind, profile_version=model)
        instance = create_execution_instance(key=kind, name=kind, description="", actor=self.actor)
        # Intentionally no frozen contract. Even reading it would fail, and
        # the text row also lacks the mandatory independent output version.
        return ModelExecutionInstanceVersion.objects.create(instance=instance, version=1, status="published",
            published_at=timezone.now(), published_by=self.actor, deployment=deployment, operation=operation, prompt_mode="none")

    def test_unrelated_broken_versions_do_not_block_video_or_get_read(self):
        published = self.publish()
        text = self.unrelated_broken_version("text", "visual_rules.generate")
        image = self.unrelated_broken_version("image", "image.text_to_image")
        before = (ExecutionVersionContract.objects.count(), PlatformLifecycleAuditEvent.objects.count(), ModelDeploymentSmokeAttempt.objects.count())
        with patch("studio.services.execution_definitions.stored_execution_contract", wraps=stored_execution_contract) as read, CaptureQueriesContext(connection) as queries:
            modes = configured_video_conditioning_modes(self.actor)
        self.assertEqual([mode["key"] for mode in modes], ["T2VA"])
        self.assertEqual([call.args[0] for call in read.call_args_list], [published.pk])
        self.assertFalse(any(query["sql"].lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))
        self.assertEqual(before, (ExecutionVersionContract.objects.count(), PlatformLifecycleAuditEvent.objects.count(), ModelDeploymentSmokeAttempt.objects.count()))
        for broken in (text, image):
            with self.subTest(operation=broken.operation), self.assertRaises(ValidationError):
                verify_current_execution_definition(stored_execution_contract(broken.pk))
        client = APIClient(); client.force_authenticate(self.actor)
        response = client.get("/api/v1/capabilities/production/")
        self.assertEqual(response.status_code, 200, response.data)

    def test_only_unrelated_versions_is_an_empty_video_catalog_not_a_fallback(self):
        self.unrelated_broken_version("text", "visual_rules.generate")
        with patch("studio.services.execution_definitions.stored_execution_contract", side_effect=AssertionError("unrelated definition must not be read")):
            self.assertEqual(configured_video_conditioning_modes(self.actor), [])

    def test_video_contract_corruption_blocks_only_video_catalog_not_production_settings(self):
        published = self.publish()
        ExecutionVersionContract.objects.filter(version=published).update(sha256="0" * 64)
        with self.assertRaises(ValidationError):
            configured_video_conditioning_modes(self.actor)
        client = APIClient(); client.force_authenticate(self.actor)
        with patch("studio.capability_views.configured_video_conditioning_modes", side_effect=AssertionError("settings must not scan video")):
            settings_response = client.get("/api/v1/capabilities/production/")
        self.assertEqual(settings_response.status_code, 200, settings_response.data)
        self.assertNotIn("conditioning_modes", settings_response.data)
        self.assertNotIn("conditioning_contract_sha256", settings_response.data)
        response = client.get("/api/v1/capabilities/video-conditioning/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "production_conditioning_contract_invalid")

    def test_video_catalog_success_empty_and_auth_are_distinct(self):
        client = APIClient()
        self.assertIn(client.get("/api/v1/capabilities/video-conditioning/").status_code, (401, 403))
        client.force_authenticate(self.actor)
        empty = client.get("/api/v1/capabilities/video-conditioning/")
        self.assertEqual(empty.status_code, 200, empty.data)
        self.assertEqual(empty.data["conditioning_modes"], [])
        self.assertRegex(empty.data["conditioning_contract_sha256"], r"^[0-9a-f]{64}$")
        self.publish()
        with CaptureQueriesContext(connection) as queries:
            available = client.get("/api/v1/capabilities/video-conditioning/")
        self.assertEqual(available.status_code, 200, available.data)
        self.assertEqual([row["key"] for row in available.data["conditioning_modes"]], ["T2VA"])
        self.assertNotEqual(available.data["conditioning_contract_sha256"], empty.data["conditioning_contract_sha256"])
        self.assertFalse(any(row["sql"].lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))

    def test_video_operation_not_hidden_by_non_video_model_modality(self):
        published = self.publish()
        ModelProfileVersion.objects.filter(pk=self.model.pk).update(modality="multimodal")
        with patch("studio.services.execution_definitions.stored_execution_contract", side_effect=ValidationError("video candidate checked")) as read:
            with self.assertRaisesMessage(ValidationError, "video candidate checked"):
                configured_video_conditioning_modes(self.actor)
        read.assert_called_once_with(published.pk)

    def test_video_resource_not_hidden_by_invalid_operation(self):
        published = self.publish()
        ModelExecutionInstanceVersion.objects.filter(pk=published.pk).update(operation="unregistered.operation")
        with self.assertRaises(ValidationError):
            configured_video_conditioning_modes(self.actor)

    def test_auth_visibility_lifecycle_and_provider_status_remain_in_force(self):
        published = self.publish()
        ExecutionVersionContract.objects.filter(version=published).update(sha256="0" * 64)
        self.assertEqual(configured_video_conditioning_modes(None), [])
        for obj, field, unavailable, available in (
            (self.actor, "is_active", False, True),
            (self.instance, "is_active", False, True),
            (self.deployment, "status", "disabled", "active"),
            (self.provider, "is_active", False, True),
            (self.model, "status", "retired", "published"),
            (published, "status", "retired", "published"),
        ):
            with self.subTest(field=field, model=type(obj).__name__):
                setattr(obj, field, unavailable); obj.save(update_fields=[field])
                self.assertEqual(configured_video_conditioning_modes(self.actor), [])
                setattr(obj, field, available); obj.save(update_fields=[field])
        with self.assertRaises(ValidationError):
            configured_video_conditioning_modes(self.actor)


class WorkflowVideoCatalogScopeTests(TestCase):
    def setUp(self):
        fixtures.WorkflowExecutionDefinitionTests.setUp(self)
        # Compiler fixtures are inactive by default. These tests exercise the
        # selectable catalog, so the workflow must be explicitly enabled.
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=["is_active"])

    def freeze(self):
        from studio.services.execution_instances import publish_execution_version
        from studio.services.execution_definitions import build_execution_definition
        definition = build_execution_definition(self.version.pk)
        return publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=definition.sha256, actor=self.user)

    def test_workflow_output_keeps_a_conflicting_video_candidate_in_scope(self):
        # The template advertises video while the frozen primary is image.
        # This remains a candidate for full verification; a corrupt contract
        # cannot disappear just because its operation is not a video operation.
        published = self.freeze()
        ExecutionVersionContract.objects.filter(version=published).update(sha256="0" * 64)
        self.workflow.template.output_kind = "video"
        self.workflow.template.save(update_fields=["output_kind"])
        with self.assertRaises(ValidationError):
            configured_video_conditioning_modes(self.user)

    def test_image_workflow_is_not_a_video_dependency(self):
        published = self.freeze()
        ExecutionVersionContract.objects.filter(version=published).update(sha256="0" * 64)
        with patch("studio.services.execution_definitions.stored_execution_contract", side_effect=AssertionError("image workflow must not be read")):
            self.assertEqual(configured_video_conditioning_modes(self.user), [])
