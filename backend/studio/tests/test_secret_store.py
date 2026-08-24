from __future__ import annotations

import base64
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import ConfigDefinition, ConfigRevision, ProviderProfile, SecretAuditEvent, SecretVersion
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.json_contracts import assign_json_contract
from studio.services.secret_store import (
    SecretStoreError,
    create_secret_binding,
    resolve_secret,
    resolve_secret_version,
    rotate_secret_binding,
    set_secret_binding_active,
)
from studio.services.readiness import collect_readiness


KEY_ID = "test-v1"
KEY_VALUE = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
KEY_ENV = {
    "AUTOPROMPT_CONFIG_MASTER_KEYS": f"{KEY_ID}:{KEY_VALUE}",
    "AUTOPROMPT_CONFIG_ACTIVE_KEY_ID": KEY_ID,
}


class SecretStoreTests(TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, KEY_ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.superuser = get_user_model().objects.create_superuser(
            username="secret-root",
            email="secret-root@example.test",
            password="SecretRootPass123!",
        )

    def test_aes_gcm_roundtrip_rotation_and_tamper_detection(self):
        first_value = "first-secret-material"
        binding = create_secret_binding(
            key="provider.example.api-key",
            label="Example API key",
            purpose="provider_api",
            secret_value=first_value,
            actor=self.superuser,
            reason="credential_create",
        )
        first_version = binding.active_version
        self.assertEqual(resolve_secret(binding), first_value)
        self.assertNotIn(first_value, first_version.ciphertext_b64)
        self.assertNotIn(first_value, str(SecretAuditEvent.objects.values().first()))

        rotate_secret_binding(
            binding,
            secret_value="second-secret-material",
            actor=self.superuser,
            reason="credential_rotate",
        )
        binding.refresh_from_db()
        self.assertEqual(resolve_secret(binding), "second-secret-material")
        self.assertNotEqual(binding.active_version_id, first_version.id)

        SecretVersion.objects.filter(pk=binding.active_version_id).update(
            ciphertext_sha256="0" * 64,
        )
        with self.assertRaises(SecretStoreError) as context:
            resolve_secret(binding)
        self.assertEqual(context.exception.code, "credential_ciphertext_mismatch")

    def test_binding_identity_is_immutable_and_api_is_superuser_only_write_only(self):
        staff = get_user_model().objects.create_user(
            username="secret-staff",
            password="SecretStaffPass123!",
            is_staff=True,
        )
        staff_client = APIClient()
        staff_client.force_authenticate(staff)
        denied = staff_client.post("/api/v1/secret-bindings/", {
            "key": "provider.denied.api-key",
            "label": "Denied",
            "purpose": "provider_api",
            "secret_value": "must-not-be-written",
        }, format="json")
        self.assertEqual(denied.status_code, 403)

        client = APIClient()
        client.force_authenticate(self.superuser)
        created = client.post("/api/v1/secret-bindings/", {
            "key": "provider.allowed.api-key",
            "label": "Allowed",
            "purpose": "provider_api",
            "secret_value": "api-super-secret",
            "reason": "credential_create",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        serialized = str(created.data)
        self.assertNotIn("api-super-secret", serialized)
        self.assertNotIn("ciphertext", serialized)
        binding_id = created.data["id"]

        from studio.models import SecretBinding
        binding = SecretBinding.objects.get(pk=binding_id)
        binding.key = "provider.changed.api-key"
        with self.assertRaises(ValidationError):
            binding.save()

    def test_config_revision_captures_immutable_secret_version(self):
        binding = create_secret_binding(
            key="config.external.api-key",
            label="External API key",
            purpose="system_config",
            secret_value="version-one",
            actor=self.superuser,
        )
        definition = ConfigDefinition(
            key="security.external_api_key",
            domain="security",
            value_type=ConfigDefinition.ValueType.STRING,
            allowed_scopes=ConfigRevision.ScopeType.SYSTEM,
            is_secret=True,
            is_required=False,
            is_active=True,
            initial_value=None,
        )
        assign_json_contract(definition, "value_schema", {})
        definition.initial_value_schema_version = 1
        definition.initial_value_size_bytes = 0
        definition.initial_value_sha256 = ""
        definition.save()
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={definition.key: {"credential_id": str(binding.id)}},
            created_by=self.superuser,
            clone_published=True,
        )
        publish_config_revision(revision, published_by=self.superuser)
        captured = revision.values.get(definition=definition).credential_version
        original_hash = revision.values_sha256

        rotate_secret_binding(
            binding,
            secret_value="version-two",
            actor=self.superuser,
        )
        revision.refresh_from_db()
        captured.refresh_from_db()
        self.assertEqual(revision.values_sha256, original_hash)
        self.assertEqual(resolve_secret(captured.binding), "version-two")
        self.assertEqual(resolve_secret_version(captured), "version-one")
        self.assertNotEqual(captured.id, captured.binding.active_version_id)

    def test_staff_may_carry_but_cannot_add_rotate_or_remove_secret_revision_values(self):
        staff = get_user_model().objects.create_user(
            username="config-secret-staff", password="SecretStaffPass123!", is_staff=True,
        )
        binding = create_secret_binding(
            key="config.governed.api-key", label="Governed API key",
            purpose="system_config", secret_value="governed-v1",
            actor=self.superuser,
        )
        definition = ConfigDefinition(
            key="security.governed_api_key", domain="security",
            value_type=ConfigDefinition.ValueType.STRING,
            allowed_scopes=ConfigRevision.ScopeType.SYSTEM,
            is_secret=True, is_required=False, is_active=True,
        )
        assign_json_contract(definition, "value_schema", {})
        definition.initial_value_schema_version = 1
        definition.initial_value_size_bytes = 0
        definition.initial_value_sha256 = ""
        definition.save()
        initial = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={definition.key: {"credential_id": str(binding.id)}},
            created_by=self.superuser,
        )
        publish_config_revision(initial, published_by=self.superuser)

        client = APIClient()
        client.force_authenticate(staff)
        carry = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={"production.default_multi_shot": True},
            created_by=staff,
        )
        carried = client.post(
            f"/api/v1/config-revisions/{carry.id}/publish/", {}, format="json",
        )
        self.assertEqual(carried.status_code, 200, carried.data)

        rotate_secret_binding(
            binding, secret_value="governed-v2", actor=self.superuser,
            reason="governed_rotation",
        )
        rotated = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={definition.key: {"credential_id": str(binding.id)}},
            created_by=self.superuser,
        )
        denied_rotate = client.post(
            f"/api/v1/config-revisions/{rotated.id}/publish/", {}, format="json",
        )
        self.assertEqual(denied_rotate.status_code, 403, denied_rotate.data)
        rotated.refresh_from_db()
        self.assertEqual(rotated.status, ConfigRevision.Status.DRAFT)

        root_client = APIClient()
        root_client.force_authenticate(self.superuser)
        allowed_rotate = root_client.post(
            f"/api/v1/config-revisions/{rotated.id}/publish/", {}, format="json",
        )
        self.assertEqual(allowed_rotate.status_code, 200, allowed_rotate.data)
        removed = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={}, unset_keys=(definition.key,), created_by=self.superuser,
        )
        denied_remove = client.post(
            f"/api/v1/config-revisions/{removed.id}/publish/", {}, format="json",
        )
        self.assertEqual(denied_remove.status_code, 403, denied_remove.data)
        removed.refresh_from_db()
        self.assertEqual(removed.status, ConfigRevision.Status.DRAFT)

    def test_cross_binding_active_version_fails_closed(self):
        first = create_secret_binding(
            key="provider.first.api-key", label="First", purpose="provider_api",
            secret_value="first-value", actor=self.superuser,
        )
        second = create_secret_binding(
            key="provider.second.api-key", label="Second", purpose="provider_api",
            secret_value="second-value", actor=self.superuser,
        )
        type(first).objects.filter(pk=first.pk).update(active_version=second.active_version)
        first.refresh_from_db()
        with self.assertRaises(SecretStoreError) as context:
            resolve_secret(first)
        self.assertEqual(context.exception.code, "credential_version_binding_mismatch")

    def test_status_and_keyring_errors_are_explicit_and_readiness_fails_closed(self):
        binding = create_secret_binding(
            key="provider.readiness.api-key", label="Readiness", purpose="provider_api",
            secret_value="readiness-value", actor=self.superuser,
        )
        provider = ProviderProfile(
            key="secret-readiness-provider", name="Secret readiness provider",
            driver_type=ProviderProfile.DriverType.OPENAI_COMPATIBLE,
            modalities="text", operations="prompt.normalize", credential=binding,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "http://127.0.0.1:12345", "chat_path": "/v1/chat/completions",
        })
        provider.save()

        healthy = collect_readiness()
        self.assertTrue(healthy["checks"]["secret_store"]["ok"], healthy["checks"]["secret_store"])
        set_secret_binding_active(
            binding, is_active=False, actor=self.superuser, reason="readiness_test",
        )
        inactive = collect_readiness()
        self.assertFalse(inactive["checks"]["secret_store"]["ok"])
        self.assertEqual(inactive["status"], "not_ready")
        self.assertNotIn(str(binding.id), str(inactive["checks"]["secret_store"]))

        client = APIClient()
        client.force_authenticate(self.superuser)
        with patch.dict(os.environ, {
            "AUTOPROMPT_CONFIG_MASTER_KEYS": "",
            "AUTOPROMPT_CONFIG_ACTIVE_KEY_ID": "",
        }, clear=False):
            failed = client.post("/api/v1/secret-bindings/", {
                "key": "provider.no-keyring.api-key", "label": "No keyring",
                "purpose": "provider_api", "secret_value": "not-persisted",
                "reason": "keyring_failure_test",
            }, format="json")
        self.assertEqual(failed.status_code, 400, failed.data)
        self.assertIn("secret_keyring_unconfigured", str(failed.data))
        self.assertFalse(type(binding).objects.filter(key="provider.no-keyring.api-key").exists())
