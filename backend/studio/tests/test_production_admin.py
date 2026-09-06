from django.contrib import admin
from django.test import RequestFactory, SimpleTestCase

from studio.models import (
    AssemblyJob,
    ConfigDefinition,
    DeferredGenerationRequest,
    GeneratedWorkflow,
    GenerationTask,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    PromptArtifact,
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
            PromptArtifact,
            GeneratedWorkflow,
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
            DeferredGenerationRequest,
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
            PromptArtifact: {
                "project", "episode", "shot", "created_by", "model",
                "prompt_text", "request_options", "structured_payload",
                "consistency_snapshot", "content_hash",
            },
            GeneratedWorkflow: {
                "project", "episode", "prompt_artifact", "template", "created_by",
                "workflow_json", "injected_values", "content_hash",
            },
            GenerationTask: {
                "owner", "project", "episode", "generated_workflow",
                "capability_snapshot", "outputs", "status",
            },
        }
        for model, fields in expected.items():
            with self.subTest(model=model.__name__):
                readonly = set(
                    admin.site._registry[model].get_readonly_fields(request)
                )
                self.assertTrue(fields <= readonly)
