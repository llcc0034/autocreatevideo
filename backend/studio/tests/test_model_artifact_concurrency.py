from __future__ import annotations

import threading
import time
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIClient

from studio.models import ModelArtifact, ModelProfileVersion, ProviderProfile
from studio.platform_serializers import ModelArtifactSerializer
from studio.services.json_contracts import assign_json_contract


@skipUnlessDBFeature("has_select_for_update")
class ModelArtifactPublishConcurrencyTests(TransactionTestCase):
    """Prove the immutable publish boundary with real database row locks.

    SQLite deliberately skips this suite because its select_for_update is a
    no-op.  CI's PostgreSQL lane exercises separate connections and actual
    blocking, rather than a mocked queryset call order.
    """

    reset_sequences = True

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username=f"artifact-race-{uuid.uuid4().hex[:10]}",
            password="StrongPass123!",
            email="artifact-race@example.test",
        )
        self.provider = ProviderProfile(
            key=f"race-{uuid.uuid4().hex[:12]}",
            name="Artifact race provider",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image",
            operations="image.text_to_image",
        )
        assign_json_contract(self.provider, "driver_config", {
            "schema_version": 1,
            "runtime_source": "comfyui",
            "base_url": "http://127.0.0.1:8188",
        })
        self.provider.save()

    def _profile(self, suffix: str) -> ModelProfileVersion:
        profile = ModelProfileVersion(
            profile_key=f"artifact-race-{suffix}-{uuid.uuid4().hex[:8]}",
            version=1,
            provider=self.provider,
            model_identifier=f"race-{suffix}",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["image.text_to_image"],
        })
        profile.save()
        return profile

    def _client(self) -> APIClient:
        client = APIClient()
        client.force_authenticate(self.admin)
        return client

    @staticmethod
    def _run_request(result: dict, key: str, request):
        close_old_connections()
        try:
            response = request()
            result[key] = (response.status_code, getattr(response, "data", None))
        except Exception as exc:  # surfaced by the owning test thread
            result[key] = exc
        finally:
            close_old_connections()

    def _assert_publish_wins_over_mutation(self, *, operation: str):
        profile = self._profile(operation)
        artifact = None
        if operation != "create":
            artifact = ModelArtifact.objects.create(
                profile_version=profile,
                role=ModelArtifact.Role.CHECKPOINT,
                logical_name=f"race-{operation}",
                filename=f"race-{operation}.safetensors",
            )
        delete_review = None
        if operation == "delete":
            preview = self._client().post("/api/v1/registry-deletion-preview/", {
                "resource_type": "model_artifact",
                "object_id": str(artifact.id),
            }, format="json")
            self.assertEqual(preview.status_code, 200, preview.data)
            delete_review = preview.data

        publish_locked = threading.Event()
        allow_publish = threading.Event()
        mutation_started = threading.Event()
        result: dict = {}

        def block_after_publish_locks(_profile, *, artifacts=None):
            self.assertEqual(
                [str(item.pk) for item in artifacts or []],
                [str(item.pk) for item in ([artifact] if artifact else [])],
            )
            publish_locked.set()
            self.assertTrue(allow_publish.wait(timeout=10))

        def publish_request():
            return self._client().post(
                f"/api/v1/model-profile-versions/{profile.id}/publish/",
                {},
                format="json",
            )

        def mutation_request():
            mutation_started.set()
            client = self._client()
            if operation == "create":
                return client.post("/api/v1/model-artifacts/", {
                    "profile_version": str(profile.id),
                    "role": ModelArtifact.Role.CHECKPOINT,
                    "logical_name": "racing-create",
                    "filename": "racing-create.safetensors",
                }, format="json")
            if operation == "update":
                return client.patch(
                    f"/api/v1/model-artifacts/{artifact.id}/",
                    {"filename": "racing-update.safetensors"},
                    format="json",
                )
            return client.post("/api/v1/registry-deletion-execute/", {
                "resource_type": "model_artifact",
                "object_id": str(artifact.id),
                "expected_impact_sha256": delete_review["impact_sha256"],
                "selected_dependency_keys": [],
                "confirmation_phrase": delete_review["confirmation_phrase"],
            }, format="json", HTTP_X_AUDIT_REASON_CODE="lifecycle_cleanup")

        validator_patches = (
            patch("studio.platform_views.validate_version_configuration_defaults"),
            patch("studio.platform_views.validate_async_model_profile"),
            patch("studio.platform_views.validate_prompt_normalization_model_profile"),
            patch("studio.platform_views.validate_script_generation_model_profile"),
            patch("studio.platform_views.validate_script_structure_model_profile"),
            patch(
                "studio.platform_views.validate_draft_profile_artifacts_for_publish",
                side_effect=block_after_publish_locks,
            ),
        )
        for patcher in validator_patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        publish_thread = threading.Thread(
            target=self._run_request,
            args=(result, "publish", publish_request),
            daemon=True,
        )
        publish_thread.start()
        self.assertTrue(publish_locked.wait(timeout=10))

        mutation_thread = threading.Thread(
            target=self._run_request,
            args=(result, "mutation", mutation_request),
            daemon=True,
        )
        mutation_thread.start()
        self.assertTrue(mutation_started.wait(timeout=10))
        time.sleep(0.2)
        self.assertTrue(
            mutation_thread.is_alive(),
            "artifact mutation crossed the locked publish boundary",
        )

        allow_publish.set()
        publish_thread.join(timeout=10)
        mutation_thread.join(timeout=10)
        self.assertFalse(publish_thread.is_alive())
        self.assertFalse(mutation_thread.is_alive())
        self.assertNotIsInstance(result.get("publish"), Exception)
        self.assertNotIsInstance(result.get("mutation"), Exception)
        self.assertEqual(result["publish"][0], 200, result["publish"])
        self.assertEqual(result["mutation"][0], 409 if operation == "delete" else 400, result["mutation"])
        profile.refresh_from_db()
        self.assertEqual(profile.status, ModelProfileVersion.Status.PUBLISHED)
        if operation == "create":
            self.assertFalse(profile.artifacts.exists())
        else:
            artifact.refresh_from_db()
            self.assertEqual(artifact.filename, f"race-{operation}.safetensors")

    def test_publish_blocks_concurrent_artifact_create_update_and_delete(self):
        for operation in ("create", "update", "delete"):
            with self.subTest(operation=operation):
                self._assert_publish_wins_over_mutation(operation=operation)

    def test_artifact_create_commits_before_publish_and_enters_frozen_closure(self):
        profile = self._profile("create-first")
        create_holds_profile = threading.Event()
        allow_create = threading.Event()
        publish_started = threading.Event()
        result: dict = {}
        original_save = ModelArtifactSerializer.save

        def blocking_save(serializer, *args, **kwargs):
            create_holds_profile.set()
            self.assertTrue(allow_create.wait(timeout=10))
            return original_save(serializer, *args, **kwargs)

        def create_request():
            return self._client().post("/api/v1/model-artifacts/", {
                "profile_version": str(profile.id),
                "role": ModelArtifact.Role.CHECKPOINT,
                "logical_name": "create-first",
                "filename": "create-first.safetensors",
            }, format="json")

        def publish_request():
            publish_started.set()
            return self._client().post(
                f"/api/v1/model-profile-versions/{profile.id}/publish/",
                {},
                format="json",
            )

        with (
            patch.object(ModelArtifactSerializer, "save", blocking_save),
            patch("studio.platform_views.validate_version_configuration_defaults"),
            patch("studio.platform_views.validate_async_model_profile"),
            patch("studio.platform_views.validate_prompt_normalization_model_profile"),
            patch("studio.platform_views.validate_script_generation_model_profile"),
            patch("studio.platform_views.validate_script_structure_model_profile"),
        ):
            create_thread = threading.Thread(
                target=self._run_request,
                args=(result, "create", create_request),
                daemon=True,
            )
            create_thread.start()
            self.assertTrue(create_holds_profile.wait(timeout=10))
            publish_thread = threading.Thread(
                target=self._run_request,
                args=(result, "publish", publish_request),
                daemon=True,
            )
            publish_thread.start()
            self.assertTrue(publish_started.wait(timeout=10))
            time.sleep(0.2)
            self.assertTrue(
                publish_thread.is_alive(),
                "publish did not wait for the artifact profile lock",
            )
            allow_create.set()
            create_thread.join(timeout=10)
            publish_thread.join(timeout=10)

        self.assertFalse(create_thread.is_alive())
        self.assertFalse(publish_thread.is_alive())
        self.assertNotIsInstance(result.get("create"), Exception)
        self.assertNotIsInstance(result.get("publish"), Exception)
        self.assertEqual(result["create"][0], 201, result["create"])
        self.assertEqual(result["publish"][0], 400, result["publish"])
        profile.refresh_from_db()
        self.assertEqual(profile.status, ModelProfileVersion.Status.DRAFT)
        self.assertEqual(profile.artifacts.count(), 1)
