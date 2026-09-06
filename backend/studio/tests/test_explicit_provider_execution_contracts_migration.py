from __future__ import annotations

import hashlib
import json

from studio.tests.test_configuration_migrations import _MigrationTestCase


def _contract(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


class ExplicitProviderExecutionContractsMigrationTests(_MigrationTestCase):
    migrate_from = "0116_runtime_policy_configuration"
    migrate_to = "0117_explicit_provider_execution_contracts"

    def set_up_before_migration(self, apps):
        Provider = apps.get_model("studio", "ProviderProfile")
        Profile = apps.get_model("studio", "ModelProfileVersion")
        Deployment = apps.get_model("studio", "ModelDeployment")
        Snapshot = apps.get_model("studio", "ModelDeploymentCapabilitySnapshot")
        Revision = apps.get_model("studio", "ConfigRevision")
        Value = apps.get_model("studio", "ConfigValue")
        active = Revision.objects.get(
            scope_type="system", status="published", activation_state="active",
        )
        self.expected_ttl = Value.objects.get(
            revision=active,
            definition__key="runtime.capability_ttl_seconds",
        ).value

        def create_provider(*, key, driver_type, config):
            size, digest = _contract(config)
            return Provider.objects.create(
                key=key,
                name=key,
                driver_type=driver_type,
                modalities="text" if driver_type == "deepseek_api" else "image",
                operations="",
                driver_config=config,
                driver_config_schema_version=1,
                driver_config_size_bytes=size,
                driver_config_sha256=digest,
            )

        def create_profile(*, key, provider, modality):
            specification = {"schema_version": 1, "capabilities": []}
            size, digest = _contract(specification)
            return Profile.objects.create(
                profile_key=key,
                version=1,
                provider=provider,
                model_identifier=key,
                modality=modality,
                status="published",
                specification=specification,
                specification_schema_version=1,
                specification_size_bytes=size,
                specification_sha256=digest,
            )

        comfy_provider = create_provider(
            key="migration-0117-comfy-provider",
            driver_type="comfyui",
            config={"runtime_source": "deployment"},
        )
        comfy_profile = create_profile(
            key="migration-0117-comfy",
            provider=comfy_provider,
            modality="image",
        )
        comfy_config = {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 30,
        }
        size, digest = _contract(comfy_config)
        comfy = Deployment.objects.create(
            key="migration-0117-comfy",
            name="migration-0117-comfy",
            profile_version=comfy_profile,
            status="active",
            runtime_config=comfy_config,
            runtime_config_schema_version=1,
            runtime_config_size_bytes=size,
            runtime_config_sha256=digest,
        )
        self.comfy_id = comfy.pk

        text_provider = create_provider(
            key="migration-0117-text-provider",
            driver_type="deepseek_api",
            config={"runtime_source": "deployment"},
        )
        text_profile = create_profile(
            key="migration-0117-text",
            provider=text_provider,
            modality="text",
        )
        text_config = {
            "runtime_source": "openai_compatible_endpoint",
            "base_url": "https://api.example.com",
            "chat_path": "/v1/chat/completions",
            "timeout_seconds": 180,
        }
        size, digest = _contract(text_config)
        text = Deployment.objects.create(
            key="migration-0117-text",
            name="migration-0117-text",
            profile_version=text_profile,
            status="active",
            runtime_config=text_config,
            runtime_config_schema_version=1,
            runtime_config_size_bytes=size,
            runtime_config_sha256=digest,
        )
        self.text_id = text.pk
        self.snapshot_facts = list(
            Snapshot.objects.order_by("id").values_list(
                "id", "target_sha256", "evidence_sha256",
            )
        )

    def test_forward_materializes_execution_fields_without_rewriting_smoke_facts(self):
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Snapshot = self.apps.get_model("studio", "ModelDeploymentCapabilitySnapshot")

        comfy = Deployment.objects.get(pk=self.comfy_id)
        self.assertEqual(comfy.runtime_config["poll_interval_seconds"], 2)
        self.assertEqual(comfy.runtime_config["max_poll_seconds"], 3_600)
        self.assertEqual(comfy.runtime_config["capability_ttl_seconds"], self.expected_ttl)
        text = Deployment.objects.get(pk=self.text_id)
        self.assertEqual(text.runtime_config["capability_ttl_seconds"], self.expected_ttl)

        for deployment in (comfy, text):
            size, digest = _contract(deployment.runtime_config)
            self.assertEqual(deployment.runtime_config_schema_version, 1)
            self.assertEqual(deployment.runtime_config_size_bytes, size)
            self.assertEqual(deployment.runtime_config_sha256, digest)
        self.assertEqual(
            list(Snapshot.objects.order_by("id").values_list(
                "id", "target_sha256", "evidence_sha256",
            )),
            self.snapshot_facts,
        )
