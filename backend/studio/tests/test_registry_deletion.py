from studio.models import FieldDefinitionVersion
from studio.tests.field_definition_fixtures import create_field_definition
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import (
    ModelArtifact,
    ModelDeployment,
    ModelExecutionInstance,
    ModelExecutionInstanceVersion,
    ModelProfileVersion,
    PlatformLifecycleAuditEvent,
    FieldDefinition,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProductionRecipeVersion,
    ProviderProfile,
)
from studio.services.json_contracts import assign_json_contract


class RegistryDeletionReviewApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="registry-delete-admin",
            email="registry-delete@example.test",
            password="StrongPass123!",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _provider(self, key: str, *, active: bool = True) -> ProviderProfile:
        provider = ProviderProfile(
            key=key,
            name=key,
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image",
            operations="health,submit,poll",
            is_active=active,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 30,
            "poll_interval_seconds": 1,
            "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
        })
        provider.save()
        return provider

    def _profile(self, provider: ProviderProfile, key: str, *, status: str) -> ModelProfileVersion:
        profile = ModelProfileVersion(
            profile_key=key,
            version=1,
            provider=provider,
            model_identifier=f"{key}.safetensors",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=status,
            published_at=timezone.now() if status != ModelProfileVersion.Status.DRAFT else None,
            published_by=self.admin if status != ModelProfileVersion.Status.DRAFT else None,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["image.text_to_image"],
        })
        profile.save()
        return profile

    def _deployment(self, profile: ModelProfileVersion, key: str) -> ModelDeployment:
        deployment = ModelDeployment(
            key=key,
            name=key,
            profile_version=profile,
            status=ModelDeployment.Status.DISABLED,
        )
        assign_json_contract(deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 30,
            "poll_interval_seconds": 1,
            "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
        })
        deployment.save()
        return deployment

    def _recipe(self, key: str, version: int, execution: ModelExecutionInstanceVersion, *, status: str):
        recipe = ProductionRecipeVersion(
            recipe_key=key,
            version=version,
            name=f"{key} v{version}",
            status=status,
            published_at=timezone.now() if status != ProductionRecipeVersion.Status.DRAFT else None,
            published_by=self.admin if status != ProductionRecipeVersion.Status.DRAFT else None,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 3,
            "stages": [{
                "key": "media",
                "routing": {"routes": [{"execution_instance_version_id": str(execution.id)}]},
            }],
        })
        recipe.save()
        return recipe

    def _preview(self, resource_type: str, object_id) -> dict:
        response = self.client.post("/api/v1/registry-deletion-preview/", {
            "resource_type": resource_type,
            "object_id": str(object_id),
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def _delete(self, resource_type: str, object_id, impact: dict, selected_keys: list[str]):
        return self.client.post("/api/v1/registry-deletion-execute/", {
            "resource_type": resource_type,
            "object_id": str(object_id),
            "expected_impact_sha256": impact["impact_sha256"],
            "selected_dependency_keys": selected_keys,
            "confirmation_phrase": impact["confirmation_phrase"],
        }, format="json", HTTP_X_AUDIT_REASON_CODE="lifecycle_cleanup")

    def test_execution_preview_lists_published_and_retired_recipe_paths(self):
        provider = self._provider("delete-preview-provider")
        profile = self._profile(provider, "delete-preview-profile", status=ModelProfileVersion.Status.PUBLISHED)
        deployment = self._deployment(profile, "delete-preview-deployment")
        instance = ModelExecutionInstance.objects.create(key="review-execution", name="精确执行")
        execution = ModelExecutionInstanceVersion.objects.create(instance=instance, version=1,
            deployment=deployment, operation="image.generate", prompt_mode="manual")
        self._recipe("delete-preview-recipe", 1, execution, status=ProductionRecipeVersion.Status.RETIRED)
        self._recipe("delete-preview-recipe", 2, execution, status=ProductionRecipeVersion.Status.PUBLISHED)

        impact = self._preview("model_execution_instance_version", execution.id)

        recipe_rows = [
            row for row in impact["dependencies"]
            if row["resource_type"] == "production_recipe_version"
        ]
        self.assertEqual(len(recipe_rows), 2)
        self.assertEqual({row["status"] for row in recipe_rows}, {"published", "retired"})
        self.assertTrue(all(row["disposition"] == "blocking_reference" for row in recipe_rows))
        self.assertTrue(all(
            row["reference_paths"] == ["$.stages[0].routing.routes[0].execution_instance_version_id"]
            for row in recipe_rows
        ))
        self.assertFalse(impact["can_execute"])

        forged = self._delete(
            "model_execution_instance_version",
            execution.id,
            impact,
            [row["dependency_key"] for row in recipe_rows],
        )
        self.assertEqual(forged.status_code, 409, forged.data)
        self.assertEqual(forged.data["error"]["code"], "deletion_blocked")
        self.assertTrue(ModelDeployment.objects.filter(pk=deployment.id).exists())
        self.assertTrue(ModelExecutionInstanceVersion.objects.filter(pk=execution.id).exists())

    def test_model_profile_dependencies_require_explicit_selection_and_delete_atomically(self):
        provider = self._provider("delete-select-provider")
        profile = self._profile(provider, "delete-select-profile", status=ModelProfileVersion.Status.DRAFT)
        artifacts = [
            ModelArtifact.objects.create(
                profile_version=profile,
                role=ModelArtifact.Role.CHECKPOINT,
                logical_name=f"checkpoint-{index}",
                filename=f"checkpoint-{index}.safetensors",
                status=ModelArtifact.Status.DECLARED,
            )
            for index in range(2)
        ]
        impact = self._preview("model_profile_version", profile.id)
        selectable = [row for row in impact["dependencies"] if row["disposition"] == "selectable_delete"]
        self.assertEqual(len(selectable), 2)
        self.assertTrue(impact["can_execute"])

        incomplete = self._delete(
            "model_profile_version",
            profile.id,
            impact,
            [selectable[0]["dependency_key"]],
        )
        self.assertEqual(incomplete.status_code, 409, incomplete.data)
        self.assertEqual(incomplete.data["error"]["code"], "dependency_confirmation_required")
        self.assertTrue(ModelProfileVersion.objects.filter(pk=profile.id).exists())
        self.assertEqual(ModelArtifact.objects.filter(pk__in=[row.pk for row in artifacts]).count(), 2)

        completed = self._delete(
            "model_profile_version",
            profile.id,
            impact,
            [row["dependency_key"] for row in selectable],
        )
        self.assertEqual(completed.status_code, 200, completed.data)
        self.assertFalse(ModelProfileVersion.objects.filter(pk=profile.id).exists())
        self.assertFalse(ModelArtifact.objects.filter(pk__in=[row.pk for row in artifacts]).exists())
        self.assertEqual(
            PlatformLifecycleAuditEvent.objects.filter(
                action="delete",
                reason_code="lifecycle_cleanup",
            ).count(),
            3,
        )

    def test_impact_drift_returns_conflict_and_rolls_back(self):
        provider = self._provider("delete-drift-provider", active=False)
        impact = self._preview("provider_profile", provider.id)
        self.assertTrue(impact["can_execute"])
        self._profile(provider, "delete-drift-profile", status=ModelProfileVersion.Status.DRAFT)

        response = self._delete("provider_profile", provider.id, impact, [])

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "deletion_impact_changed")
        self.assertTrue(ProviderProfile.objects.filter(pk=provider.id).exists())
        self.assertTrue(ModelProfileVersion.objects.filter(profile_key="delete-drift-profile").exists())

    def test_parent_review_cannot_bypass_verified_artifact_policy(self):
        provider = self._provider("delete-artifact-provider")
        profile = self._profile(provider, "delete-artifact-profile", status="draft")
        artifact = ModelArtifact.objects.create(profile_version=profile, role="checkpoint",
            logical_name="checkpoint", filename="checkpoint.safetensors", status="declared")
        selectable = self._preview("model_profile_version", profile.pk)
        row = next(row for row in selectable["dependencies"] if row["object_id"] == str(artifact.pk))
        self.assertEqual(row["disposition"], "selectable_delete")
        artifact.status = ModelArtifact.Status.AVAILABLE
        artifact.save(update_fields=("status", "updated_at"))
        blocked = self._preview("model_profile_version", profile.pk)
        row = next(row for row in blocked["dependencies"] if row["object_id"] == str(artifact.pk))
        self.assertEqual(row["disposition"], "blocking_reference")
        self.assertFalse(blocked["can_execute"])
        rejected = self._delete("model_profile_version", profile.pk, selectable, [row["dependency_key"]])
        self.assertEqual(rejected.status_code, 409, rejected.data)
        self.assertTrue(ModelArtifact.objects.filter(pk=artifact.pk).exists())
        self.assertTrue(ModelProfileVersion.objects.filter(pk=profile.pk).exists())

    def test_non_display_state_change_invalidates_review(self):
        provider = self._provider("delete-state-provider", active=False)
        impact = self._preview("provider_profile", provider.id)
        assign_json_contract(provider, "driver_config", {
            **provider.driver_config,
            "timeout_seconds": 45,
        })
        provider.save(update_fields=[
            "driver_config", "driver_config_schema_version",
            "driver_config_size_bytes", "driver_config_sha256", "updated_at",
        ])

        response = self._delete("provider_profile", provider.id, impact, [])

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "deletion_impact_changed")
        self.assertTrue(ProviderProfile.objects.filter(pk=provider.id).exists())

    def test_identity_deletion_previews_and_removes_its_exact_versions(self):
        field = create_field_definition(key="delete.identity", label="定义", category="custom", value_type="text")
        version_id = field._fixture_version.pk
        impact = self._preview("field_definition", field.pk)
        owned = [row for row in impact["dependencies"] if row["resource_type"] == "field_definition_version"]
        self.assertEqual([(row["object_id"], row["disposition"]) for row in owned], [(str(version_id), "automatic_delete")])
        response = self._delete("field_definition", field.pk, impact, [])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(FieldDefinition.objects.filter(pk=field.pk).exists())
        self.assertFalse(FieldDefinitionVersion.objects.filter(pk=version_id).exists())

    def test_structure_usage_is_visible_and_not_implicitly_deleted_with_field(self):
        from studio.services.data_structures import write_data_structure
        field = create_field_definition(key="delete.structure", label="定义", category="custom", value_type="text")
        structure = write_data_structure(content={"name": "Shared shape", "nodes": [
            {"node_key": "root", "field_definition_version": str(field._fixture_version.pk)},
        ]})
        impact = self._preview("field_definition", field.pk)
        rows = [row for row in impact["dependencies"] if row["relationship"] == "structure_field_usage"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["object_id"], str(structure.pk))
        self.assertEqual(rows[0]["disposition"], "blocking_reference")
        self.assertEqual(rows[0]["reference_paths"], ["nodes.root.field_definition_version"])
        response = self._delete("field_definition", field.pk, impact, [])
        self.assertEqual(response.status_code, 409, response.data)
        self.assertTrue(FieldDefinition.objects.filter(pk=field.pk).exists())
        self.assertEqual(structure.nodes.count(), 1)

    def test_owned_prompt_rule_is_listed_deleted_and_audited(self):
        field = create_field_definition(
            key="delete.owned.rule",
            label="Owned rule",
            category="delete_test",
            value_type=FieldDefinitionVersion.ValueType.TEXT,
        )
        prompt_profile = PromptProfileVersion.objects.create(
            profile_key="delete.owned.profile",
            version=1,
            name="Delete owned profile",
            model_family="delete-owned-family",
            operation="image.generate",
        )
        rule = create_profile_field_rule(
            profile_version=prompt_profile,
            field_definition=field,
            source_selector=PromptProfileFieldRule.SourceSelector.FIXED_VALUE,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
        )
        impact = self._preview("prompt_profile_version", prompt_profile.id)
        automatic = [
            row for row in impact["dependencies"]
            if row["disposition"] == "automatic_delete"
        ]
        self.assertEqual([row["object_id"] for row in automatic], [str(rule.id)])

        response = self._delete("prompt_profile_version", prompt_profile.id, impact, [])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(PromptProfileVersion.objects.filter(pk=prompt_profile.id).exists())
        self.assertFalse(PromptProfileFieldRule.objects.filter(pk=rule.id).exists())
        self.assertEqual(
            PlatformLifecycleAuditEvent.objects.filter(
                action="delete",
                object_id__in=[prompt_profile.id, rule.id],
            ).count(),
            2,
        )

    def test_preview_missing_target_returns_structured_not_found(self):
        response = self.client.post("/api/v1/registry-deletion-preview/", {
            "resource_type": "model_deployment",
            "object_id": "00000000-0000-4000-8000-000000000000",
        }, format="json")

        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(response.data["error"]["code"], "registry_resource_not_found")

    def test_recipe_writer_rejects_missing_frozen_execution_reference(self):
        response = self.client.post("/api/v1/production-recipes/", {
            "recipe_key": "delete-stale-reference",
            "version": 1,
            "name": "Delete stale reference",
            "definition": {
                "schema_version": 3,
                "interaction_mode": "assisted",
                "stages": [{
                    "key": "script_generate", "mode": "ai", "approval": "required",
                    "routing": {"mode": "fixed", "variant_count": 1, "evaluator": None, "routes": [{
                        "execution_instance_version_id": "00000000-0000-4000-8000-000000000000",
                        "expected_definition_sha256": "a" * 64, "input_sources": {}, "parameter_policies": [],
                    }]},
                }],
            },
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["code"], "execution_definition_conflict")
        self.assertIn("缺少冻结合同", str(response.data))
        self.assertFalse(ProductionRecipeVersion.objects.filter(recipe_key="delete-stale-reference").exists())







    def test_delete_without_review_contract_is_rejected(self):
        provider = self._provider("delete-no-review-provider", active=False)
        response = self.client.delete(f"/api/v1/provider-profiles/{provider.id}/")
        self.assertEqual(response.status_code, 405, response.data)
        self.assertTrue(ProviderProfile.objects.filter(pk=provider.id).exists())

        impact = self._preview("provider_profile", provider.id)
        missing_reason = self.client.post("/api/v1/registry-deletion-execute/", {
            "resource_type": "provider_profile",
            "object_id": str(provider.id),
            "expected_impact_sha256": impact["impact_sha256"],
            "selected_dependency_keys": [],
            "confirmation_phrase": impact["confirmation_phrase"],
        }, format="json")
        self.assertEqual(missing_reason.status_code, 400, missing_reason.data)
        self.assertTrue(ProviderProfile.objects.filter(pk=provider.id).exists())
