from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ModelArtifact,
    ModelDeployment,
    ModelDeploymentSmokeAttempt,
    ModelProfileVersion,
    PlatformLifecycleAuditEvent,
    PromptFieldDefinition,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProductionRun,
    ProductionRecipeVersion,
    ProviderProfile,
    SeriesProject,
)
from studio.services.json_contracts import assign_json_contract, json_contract


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

    def _recipe(self, key: str, version: int, deployment: ModelDeployment, *, status: str):
        recipe = ProductionRecipeVersion(
            recipe_key=key,
            version=version,
            name=f"{key} v{version}",
            status=status,
            published_at=timezone.now() if status != ProductionRecipeVersion.Status.DRAFT else None,
            published_by=self.admin if status != ProductionRecipeVersion.Status.DRAFT else None,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 2,
            "stages": [{
                "key": "media",
                "routing": {"routes": [{"model_deployment_id": str(deployment.id)}]},
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

    def test_deployment_preview_lists_published_and_retired_recipe_paths(self):
        provider = self._provider("delete-preview-provider")
        profile = self._profile(provider, "delete-preview-profile", status=ModelProfileVersion.Status.PUBLISHED)
        deployment = self._deployment(profile, "delete-preview-deployment")
        self._recipe("delete-preview-recipe", 1, deployment, status=ProductionRecipeVersion.Status.RETIRED)
        self._recipe("delete-preview-recipe", 2, deployment, status=ProductionRecipeVersion.Status.PUBLISHED)

        impact = self._preview("model_deployment", deployment.id)

        recipe_rows = [
            row for row in impact["dependencies"]
            if row["resource_type"] == "production_recipe_version"
        ]
        self.assertEqual(len(recipe_rows), 2)
        self.assertEqual({row["status"] for row in recipe_rows}, {"published", "retired"})
        self.assertTrue(all(row["disposition"] == "blocking_reference" for row in recipe_rows))
        self.assertTrue(all(
            row["reference_paths"] == ["$.stages[0].routing.routes[0].model_deployment_id"]
            for row in recipe_rows
        ))
        self.assertFalse(impact["can_execute"])

        forged = self._delete(
            "model_deployment",
            deployment.id,
            impact,
            [row["dependency_key"] for row in recipe_rows],
        )
        self.assertEqual(forged.status_code, 409, forged.data)
        self.assertEqual(forged.data["error"]["code"], "deletion_blocked")
        self.assertTrue(ModelDeployment.objects.filter(pk=deployment.id).exists())

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

    def test_parent_review_cannot_bypass_active_binding_lifecycle_policy(self):
        provider = self._provider("delete-binding-provider")
        profile = self._profile(provider, "delete-binding-profile", status=ModelProfileVersion.Status.DRAFT)
        prompt_profile = PromptProfileVersion.objects.create(
            profile_key="delete.binding.prompt",
            version=1,
            name="Delete binding prompt",
            model_family="delete-binding-family",
            operation="image.generate",
            status=PromptProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        binding = PromptProfileBinding.objects.create(
            key="delete-binding-route",
            name="Delete binding route",
            operation="image.generate",
            model_family="delete-binding-family",
            model_profile_version=profile,
            prompt_profile_version=prompt_profile,
            is_active=True,
        )

        blocked = self._preview("model_profile_version", profile.id)
        binding_row = next(
            row for row in blocked["dependencies"]
            if row["object_id"] == str(binding.id)
        )
        self.assertEqual(binding_row["disposition"], "blocking_reference")
        self.assertFalse(blocked["can_execute"])

        binding.is_active = False
        binding.save(update_fields=["is_active", "updated_at"])
        selectable = self._preview("model_profile_version", profile.id)
        binding_row = next(
            row for row in selectable["dependencies"]
            if row["object_id"] == str(binding.id)
        )
        self.assertEqual(binding_row["disposition"], "selectable_delete")
        self.assertTrue(selectable["can_execute"])

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

    def test_owned_prompt_rule_is_listed_deleted_and_audited(self):
        field = PromptFieldDefinition.objects.create(
            key="delete.owned.rule",
            label="Owned rule",
            category="delete_test",
            value_type=PromptFieldDefinition.ValueType.TEXT,
        )
        prompt_profile = PromptProfileVersion.objects.create(
            profile_key="delete.owned.profile",
            version=1,
            name="Delete owned profile",
            model_family="delete-owned-family",
            operation="image.generate",
        )
        rule = PromptProfileFieldRule.objects.create(
            profile_version=prompt_profile,
            field_definition=field,
            source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
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

    def test_recipe_writer_rejects_missing_registry_reference(self):
        response = self.client.post("/api/v1/production-recipes/", {
            "recipe_key": "delete-stale-reference",
            "version": 1,
            "name": "Delete stale reference",
            "definition": {
                "schema_version": 2,
                "stages": [{
                    "key": "media",
                    "routing": {"routes": [{
                        "model_deployment_id": "00000000-0000-4000-8000-000000000000",
                    }]},
                }],
            },
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("不存在", str(response.data))

    def test_provider_and_prompt_binding_id_recipe_references_are_listed(self):
        provider = self._provider("delete-recipe-provider", active=False)
        prompt_profile = PromptProfileVersion.objects.create(
            profile_key="delete.recipe.binding.profile",
            version=1,
            name="Delete recipe binding profile",
            model_family="delete-recipe-family",
            operation="image.generate",
            status=PromptProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        binding = PromptProfileBinding.objects.create(
            key="delete-recipe-binding",
            name="Delete recipe binding",
            operation="image.generate",
            model_family="delete-recipe-family",
            prompt_profile_version=prompt_profile,
            is_active=False,
        )
        recipe = ProductionRecipeVersion(
            recipe_key="delete-provider-binding-reference",
            version=1,
            name="Delete provider binding reference",
            status=ProductionRecipeVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 2,
            "provider_profile_id": str(provider.id),
            "prompt_profile_binding_id": str(binding.id),
        })
        recipe.save()

        provider_impact = self._preview("provider_profile", provider.id)
        binding_impact = self._preview("prompt_profile_binding", binding.id)

        provider_row = next(
            row for row in provider_impact["dependencies"]
            if row["object_id"] == str(recipe.id)
        )
        binding_row = next(
            row for row in binding_impact["dependencies"]
            if row["object_id"] == str(recipe.id)
        )
        self.assertEqual(provider_row["reference_paths"], ["$.provider_profile_id"])
        self.assertEqual(binding_row["reference_paths"], ["$.prompt_profile_binding_id"])
        self.assertFalse(provider_impact["can_execute"])
        self.assertFalse(binding_impact["can_execute"])

    def test_prompt_binding_preview_lists_frozen_production_run_input_paths(self):
        prompt_profile = PromptProfileVersion.objects.create(
            profile_key="delete.run.binding.profile",
            version=1,
            name="Delete run binding profile",
            model_family="delete-run-family",
            operation="image.generate",
            status=PromptProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        binding = PromptProfileBinding.objects.create(
            key="delete-run-binding",
            name="Delete run binding",
            operation="image.generate",
            model_family="delete-run-family",
            prompt_profile_version=prompt_profile,
            is_active=False,
        )
        project = SeriesProject.objects.create(
            owner=self.admin,
            title="Delete run binding project",
            language="Chinese",
            aspect_ratio="9:16",
            width=768,
            height=1344,
            fps=24,
            default_shot_duration_ms=5000,
            continuity_seed=7,
        )
        recipe = ProductionRecipeVersion(
            recipe_key="delete-run-binding-recipe",
            version=1,
            name="Delete run binding recipe",
            status=ProductionRecipeVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assign_json_contract(recipe, "definition", {"schema_version": 2, "stages": []})
        recipe.save()
        config_contract = json_contract({"schema_version": 1})
        input_contract = json_contract({
            "schema_version": 3,
            "workflow_selection": {
                "prompt_profile": {
                    "prompt_profile_binding_id": str(binding.id),
                    "prompt_profile_binding_key": binding.key,
                },
            },
            "bible_image_prompt_profile": {
                "prompt_profile_binding_id": str(binding.id),
                "routes": [{
                    "prompt_profile": {
                        "prompt_profile_binding_key": binding.key,
                    },
                }],
            },
        })
        run = ProductionRun.objects.create(
            owner=self.admin,
            project=project,
            recipe_version=recipe,
            idempotency_key="delete-run-binding",
            request_sha256=input_contract.sha256,
            status=ProductionRun.Status.QUEUED,
            config_snapshot=config_contract.value,
            config_snapshot_schema_version=config_contract.schema_version,
            config_snapshot_size_bytes=config_contract.size_bytes,
            config_snapshot_sha256=config_contract.sha256,
            input_snapshot=input_contract.value,
            input_snapshot_schema_version=input_contract.schema_version,
            input_snapshot_size_bytes=input_contract.size_bytes,
            input_snapshot_sha256=input_contract.sha256,
        )

        impact = self._preview("prompt_profile_binding", binding.id)

        row = next(
            item for item in impact["dependencies"]
            if item["resource_type"] == "production_run" and item["object_id"] == str(run.id)
        )
        self.assertEqual(row["disposition"], "preserved_history")
        self.assertEqual(row["reference_paths"], [
            "$.bible_image_prompt_profile.prompt_profile_binding_id",
            "$.bible_image_prompt_profile.routes[0].prompt_profile.prompt_profile_binding_key",
            "$.workflow_selection.prompt_profile.prompt_profile_binding_id",
            "$.workflow_selection.prompt_profile.prompt_profile_binding_key",
        ])
        self.assertFalse(impact["can_execute"])

    def test_prompt_binding_preview_lists_deployment_smoke_request_paths(self):
        provider = self._provider("delete-smoke-binding-provider")
        model_profile = self._profile(
            provider,
            "delete-smoke-binding-model",
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        deployment = self._deployment(model_profile, "delete-smoke-binding-deployment")
        prompt_profile = PromptProfileVersion.objects.create(
            profile_key="delete.smoke.binding.profile",
            version=1,
            name="Delete smoke binding profile",
            model_family="delete-smoke-family",
            operation="video.generate",
            status=PromptProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        binding = PromptProfileBinding.objects.create(
            key="delete-smoke-binding",
            name="Delete smoke binding",
            operation="video.generate",
            model_family="delete-smoke-family",
            prompt_profile_version=prompt_profile,
            is_active=False,
        )
        assembly_profile = PromptProfileVersion.objects.create(
            profile_key="delete.smoke.assembly.profile",
            version=1,
            name="Delete smoke assembly profile",
            model_family="delete-smoke-text-family",
            operation="prompt.normalize",
            status=PromptProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
            published_by=self.admin,
        )
        assembly_binding = PromptProfileBinding.objects.create(
            key="delete-smoke-assembly-binding",
            name="Delete smoke assembly binding",
            operation="prompt.normalize",
            model_family="delete-smoke-text-family",
            prompt_profile_version=assembly_profile,
            is_active=False,
        )
        attempt = ModelDeploymentSmokeAttempt.objects.create(
            deployment=deployment,
            operation="prompt.normalize",
            status=ModelDeploymentSmokeAttempt.Status.FAILED,
            request_sha256="a" * 64,
            target_sha256="b" * 64,
            request_snapshot={
                "representative_contract": {
                    "prompt_profile_binding_key": binding.key,
                    "prompt_profile_binding_id": str(binding.id),
                    "assembly_prompt_profile": {
                        "prompt_profile_binding_key": assembly_binding.key,
                        "prompt_profile_binding_id": str(assembly_binding.id),
                    },
                },
                "payload": {
                    "target_prompt_contract": {
                        "profiles": [{"binding_key": binding.key}],
                    },
                },
            },
        )

        impact = self._preview("prompt_profile_binding", binding.id)

        row = next(
            item for item in impact["dependencies"]
            if item["resource_type"] == "model_deployment_smoke_attempt"
            and item["object_id"] == str(attempt.id)
        )
        self.assertEqual(row["disposition"], "preserved_history")
        self.assertEqual(row["reference_paths"], [
            "$.payload.target_prompt_contract.profiles[0].binding_key",
            "$.representative_contract.prompt_profile_binding_id",
            "$.representative_contract.prompt_profile_binding_key",
        ])
        self.assertFalse(impact["can_execute"])

        assembly_impact = self._preview(
            "prompt_profile_binding",
            assembly_binding.id,
        )
        assembly_row = next(
            item for item in assembly_impact["dependencies"]
            if item["resource_type"] == "model_deployment_smoke_attempt"
            and item["object_id"] == str(attempt.id)
        )
        self.assertEqual(assembly_row["disposition"], "preserved_history")
        self.assertEqual(assembly_row["reference_paths"], [
            "$.representative_contract.assembly_prompt_profile.prompt_profile_binding_id",
            "$.representative_contract.assembly_prompt_profile.prompt_profile_binding_key",
        ])
        self.assertFalse(assembly_impact["can_execute"])

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
