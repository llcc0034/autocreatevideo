from __future__ import annotations

import threading
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, transaction
from django.test import TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIClient

from studio.models import (
    ModelProfileVersion,
    PromptFieldDefinition,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProviderProfile,
)
from studio.services import registry_deletion
from studio.services.json_contracts import assign_json_contract


@skipUnlessDBFeature("has_select_for_update")
class RegistryDeletionConcurrencyTests(TransactionTestCase):
    """Exercise dependency lifecycle races with real PostgreSQL row locks."""

    reset_sequences = True

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username=f"registry-delete-race-{uuid.uuid4().hex[:10]}",
            password="StrongPass123!",
            email="registry-delete-race@example.test",
        )

    def _client(self) -> APIClient:
        client = APIClient()
        client.force_authenticate(self.admin)
        return client

    @staticmethod
    def _run(result: dict, key: str, operation):
        close_old_connections()
        try:
            result[key] = operation()
        except Exception as exc:  # surfaced by the owning test thread
            result[key] = exc
        finally:
            close_old_connections()

    def _preview(self, resource_type: str, object_id) -> dict:
        response = self._client().post(
            "/api/v1/registry-deletion-preview/",
            {"resource_type": resource_type, "object_id": str(object_id)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def _assert_lifecycle_change_wins_before_dependency_lock(
        self,
        *,
        resource_type: str,
        object_id,
        impact: dict,
        mutate,
    ) -> tuple[int, dict]:
        lock_discovery_finished = threading.Event()
        allow_dependency_lock = threading.Event()
        result: dict = {}
        original_lock = registry_deletion._lock_impact_dependencies

        def pause_before_dependency_lock(current_impact):
            lock_discovery_finished.set()
            self.assertTrue(allow_dependency_lock.wait(timeout=10))
            return original_lock(current_impact)

        selectable_keys = [
            row["dependency_key"]
            for row in impact["dependencies"]
            if row["disposition"] == "selectable_delete"
        ]

        def execute_request():
            return self._client().post(
                "/api/v1/registry-deletion-execute/",
                {
                    "resource_type": resource_type,
                    "object_id": str(object_id),
                    "expected_impact_sha256": impact["impact_sha256"],
                    "selected_dependency_keys": selectable_keys,
                    "confirmation_phrase": impact["confirmation_phrase"],
                },
                format="json",
                HTTP_X_AUDIT_REASON_CODE="lifecycle_cleanup",
            )

        with patch.object(
            registry_deletion,
            "_lock_impact_dependencies",
            side_effect=pause_before_dependency_lock,
        ):
            execute_thread = threading.Thread(
                target=self._run,
                args=(result, "execute", execute_request),
                daemon=True,
            )
            execute_thread.start()
            self.assertTrue(lock_discovery_finished.wait(timeout=10))

            mutation_thread = threading.Thread(
                target=self._run,
                args=(result, "mutation", mutate),
                daemon=True,
            )
            mutation_thread.start()
            mutation_thread.join(timeout=10)
            self.assertFalse(
                mutation_thread.is_alive(),
                "lifecycle mutation unexpectedly waited before dependency locks",
            )
            self.assertNotIsInstance(result.get("mutation"), Exception)

            allow_dependency_lock.set()
            execute_thread.join(timeout=10)

        self.assertFalse(execute_thread.is_alive())
        self.assertNotIsInstance(result.get("execute"), Exception)
        response = result["execute"]
        return response.status_code, response.data

    def test_inactive_binding_enabled_after_discovery_is_not_deleted(self):
        provider = ProviderProfile(
            key=f"delete-race-{uuid.uuid4().hex[:10]}",
            name="Deletion race provider",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image",
            operations="image.generate",
        )
        assign_json_contract(provider, "driver_config", {
            "schema_version": 1,
            "runtime_source": "comfyui",
            "base_url": "http://127.0.0.1:8188",
        })
        provider.save()
        model_profile = ModelProfileVersion(
            profile_key=f"delete-race-model-{uuid.uuid4().hex[:8]}",
            version=1,
            provider=provider,
            model_identifier="delete-race-model",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(model_profile, "specification", {"schema_version": 1})
        model_profile.save()
        prompt_profile = PromptProfileVersion.objects.create(
            profile_key=f"delete.race.prompt.{uuid.uuid4().hex[:8]}",
            version=1,
            name="Delete race prompt",
            model_family="delete-race-family",
            operation="image.generate",
            status=PromptProfileVersion.Status.PUBLISHED,
        )
        binding = PromptProfileBinding.objects.create(
            key=f"delete-race-binding-{uuid.uuid4().hex[:8]}",
            name="Delete race binding",
            operation="image.generate",
            model_family="delete-race-family",
            model_profile_version=model_profile,
            prompt_profile_version=prompt_profile,
            is_active=False,
        )
        impact = self._preview("model_profile_version", model_profile.id)
        self.assertTrue(impact["can_execute"])

        def enable_binding():
            with transaction.atomic():
                PromptProfileBinding.objects.filter(pk=binding.pk).update(is_active=True)

        status_code, data = self._assert_lifecycle_change_wins_before_dependency_lock(
            resource_type="model_profile_version",
            object_id=model_profile.id,
            impact=impact,
            mutate=enable_binding,
        )

        self.assertEqual(status_code, 409, data)
        self.assertEqual(data["error"]["code"], "deletion_impact_changed")
        binding.refresh_from_db()
        self.assertTrue(binding.is_active)
        self.assertTrue(ModelProfileVersion.objects.filter(pk=model_profile.pk).exists())

    def test_rule_parent_published_after_discovery_is_not_deleted(self):
        field = PromptFieldDefinition.objects.create(
            key=f"delete_race_field_{uuid.uuid4().hex[:8]}",
            label="Delete race field",
            category="test",
            value_type=PromptFieldDefinition.ValueType.TEXT,
            is_active=False,
        )
        prompt_profile = PromptProfileVersion.objects.create(
            profile_key=f"delete.race.rule.{uuid.uuid4().hex[:8]}",
            version=1,
            name="Delete race rule profile",
            model_family="delete-race-family",
            operation="image.generate",
            status=PromptProfileVersion.Status.DRAFT,
        )
        rule = PromptProfileFieldRule.objects.create(
            profile_version=prompt_profile,
            field_definition=field,
            source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
            channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
            position=1,
        )
        impact = self._preview("prompt_field_definition", field.id)
        self.assertTrue(impact["can_execute"])

        def publish_parent():
            with transaction.atomic():
                PromptProfileVersion.objects.filter(pk=prompt_profile.pk).update(
                    status=PromptProfileVersion.Status.PUBLISHED,
                )

        status_code, data = self._assert_lifecycle_change_wins_before_dependency_lock(
            resource_type="prompt_field_definition",
            object_id=field.id,
            impact=impact,
            mutate=publish_parent,
        )

        self.assertEqual(status_code, 409, data)
        self.assertEqual(data["error"]["code"], "deletion_impact_changed")
        prompt_profile.refresh_from_db()
        self.assertEqual(prompt_profile.status, PromptProfileVersion.Status.PUBLISHED)
        self.assertTrue(PromptProfileFieldRule.objects.filter(pk=rule.pk).exists())
        self.assertTrue(PromptFieldDefinition.objects.filter(pk=field.pk).exists())
