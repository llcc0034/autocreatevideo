from django.contrib import admin
from django.test import RequestFactory, SimpleTestCase

from studio.models import (
    AssemblyJob,
    ConfigDefinition,
    GenerationAttempt,
    GeneratedArtifact,
    GenerationTask,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    PromptFieldTranslation,
    ProviderProfile,
    ReleaseBundle,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)


class ImmutableProductionAuditAdminTests(SimpleTestCase):
    def test_production_evidence_models_are_view_only_in_admin(self):
        request = RequestFactory().get("/admin/")

        for model in (
            GenerationAttempt,
            GeneratedArtifact,
            GenerationTask,
            AssemblyJob,
            ReleaseBundle,
            PromptFieldTranslation,
            ModelProfileVersion,
            ModelArtifact,
            WorkflowTemplate,
            WorkflowTemplateVersion,
            ProviderProfile,
            ModelDeployment,
            ConfigDefinition,
        ):
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))
                persisted_fields = {field.name for field in model._meta.fields}
                self.assertEqual(
                    set(model_admin.get_readonly_fields(request)),
                    persisted_fields,
                )

    def test_sensitive_candidate_fields_are_explicitly_read_only(self):
        request = RequestFactory().get("/admin/")
        expected = {
            GenerationAttempt: {
                "stage_run", "preparation", "execution_instance_version", "role",
                "request_snapshot", "request_sha256", "prompt_profile_version",
            },
            GeneratedArtifact: {
                "production_run", "stage_run", "generation_attempt", "generation_task",
                "manifest_metadata", "manifest_metadata_sha256", "sha256",
            },
            GenerationTask: {
                "owner", "project", "episode", "outputs", "status",
                "submission_token", "remote_state", "distribution_state",
            },
        }
        for model, fields in expected.items():
            with self.subTest(model=model.__name__):
                readonly = set(
                    admin.site._registry[model].get_readonly_fields(request)
                )
                self.assertTrue(fields <= readonly)
