import hashlib
import importlib
import json

from studio.tests.test_configuration_migrations import _MigrationTestCase


def _json_facts(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(encoded), hashlib.sha256(encoded).hexdigest()


class ComfyWorkflowSourceScopeMigrationTests(_MigrationTestCase):
    migrate_from = "0132_h3_declarative_prompt_profile_v19"
    migrate_to = "0133_comfy_workflow_source_scope"

    def set_up_before_migration(self, apps):
        User = apps.get_model("auth", "User")
        ProviderProfile = apps.get_model("studio", "ProviderProfile")
        ModelProfileVersion = apps.get_model("studio", "ModelProfileVersion")
        RuntimeHost = apps.get_model("studio", "RuntimeHost")
        ModelDeployment = apps.get_model("studio", "ModelDeployment")
        ComfyWorkflowSource = apps.get_model("studio", "ComfyWorkflowSource")

        user = User.objects.create(username="workflow-scope-migration")
        driver_config = {"base_url": "http://127.0.0.1:8188/"}
        driver_size, driver_sha = _json_facts(driver_config)
        provider = ProviderProfile.objects.create(
            key="workflow-scope-migration",
            name="Workflow scope migration",
            driver_type="comfyui",
            modalities="image",
            operations="workflow_library.list,workflow_library.read,object_info",
            driver_config=driver_config,
            driver_config_schema_version=1,
            driver_config_size_bytes=driver_size,
            driver_config_sha256=driver_sha,
            is_active=True,
        )
        specification = {"schema_version": 1}
        specification_size, specification_sha = _json_facts(specification)
        profile = ModelProfileVersion.objects.create(
            profile_key="workflow-scope-migration",
            version=1,
            provider=provider,
            model_identifier="workflow-scope-migration",
            modality="image",
            specification=specification,
            specification_schema_version=1,
            specification_size_bytes=specification_size,
            specification_sha256=specification_sha,
            status="published",
        )
        host = RuntimeHost.objects.create(
            key="workflow-scope-migration",
            name="Workflow scope migration",
            status="active",
        )
        runtime_config = {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 10,
        }
        runtime_size, runtime_sha = _json_facts(runtime_config)
        deployment = ModelDeployment.objects.create(
            key="workflow-scope-migration",
            name="Workflow scope migration",
            profile_version=profile,
            runtime_host=host,
            runtime_config=runtime_config,
            runtime_config_schema_version=1,
            runtime_config_size_bytes=runtime_size,
            runtime_config_sha256=runtime_sha,
            status="active",
        )

        migration = importlib.import_module(
            "studio.migrations.0133_comfy_workflow_source_scope",
        )
        fingerprint = migration._legacy_deployment_fingerprint(deployment)
        self.expected_live_scope = migration._scope_sha256(deployment)

        def source(path, report):
            report_size, report_sha = _json_facts(report)
            return ComfyWorkflowSource.objects.create(
                source_deployment=deployment,
                source_path=path,
                source_path_key=path,
                source_sha256=hashlib.sha256(path.encode("utf-8")).hexdigest(),
                source_size_bytes=128,
                source_workflow_id="",
                import_contract_sha256="a" * 64,
                import_kind="ignored",
                converted_graph_sha256="",
                conversion_report=report,
                conversion_report_schema_version=1,
                conversion_report_size_bytes=report_size,
                conversion_report_sha256=report_sha,
                imported_by=user,
            )

        live = source(
            "saved/live.json",
            {"source_deployment_fingerprint_sha256": fingerprint},
        )
        quarantined = source("saved/legacy.json", {})
        self.live_id = live.id
        self.quarantined_id = quarantined.id
        self.expected_quarantined_scope = migration._quarantined_legacy_scope(
            quarantined,
            "",
        )

    def test_backfill_maps_only_verified_history_to_current_scope(self):
        Source = self.apps.get_model("studio", "ComfyWorkflowSource")
        live = Source.objects.get(pk=self.live_id)
        quarantined = Source.objects.get(pk=self.quarantined_id)

        self.assertEqual(live.source_scope_schema_version, 1)
        self.assertEqual(live.source_scope_sha256, self.expected_live_scope)
        self.assertEqual(
            live.source_path_sha256,
            hashlib.sha256(live.source_path_key.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            quarantined.source_scope_sha256,
            self.expected_quarantined_scope,
        )
        self.assertNotEqual(
            quarantined.source_scope_sha256,
            self.expected_live_scope,
        )
        self.assertEqual(
            quarantined.source_path_sha256,
            hashlib.sha256(quarantined.source_path_key.encode("utf-8")).hexdigest(),
        )
