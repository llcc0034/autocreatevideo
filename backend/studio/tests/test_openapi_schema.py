import re

from django.conf import settings
from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator

from studio.models import EpisodeCreativeDraft, ShotDetailDraft


class OpenApiEnumNamingTests(SimpleTestCase):
    def test_creative_draft_status_has_stable_component_name(self):
        overrides = settings.SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]
        self.assertEqual(
            overrides["CreativeDraftStatusEnum"],
            "studio.models.EpisodeCreativeDraft.Status",
        )

        self.assertEqual(
            list(EpisodeCreativeDraft.Status.values),
            list(ShotDetailDraft.Status.values),
        )

        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]
        self.assertEqual(
            components["CreativeDraftStatusEnum"]["enum"],
            list(EpisodeCreativeDraft.Status.values),
        )
        self.assertEqual(
            components["EpisodeCreativeDraft"]["properties"]["status"]["allOf"][0]["$ref"],
            "#/components/schemas/CreativeDraftStatusEnum",
        )
        self.assertEqual(
            components["ShotDetailDraft"]["properties"]["status"]["allOf"][0]["$ref"],
            "#/components/schemas/CreativeDraftStatusEnum",
        )
        self.assertFalse(
            any(re.fullmatch(r"Status[0-9A-Fa-f]+Enum", name) for name in components),
        )

    def test_platform_decision_enums_have_stable_component_names(self):
        overrides = settings.SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]
        self.assertEqual(
            overrides["ApprovalDecisionEnum"],
            "studio.models.ApprovalDecision.Decision",
        )
        self.assertEqual(
            overrides["RecommendationDecisionEnum"],
            "studio.models.RecommendationDecision.Decision",
        )

        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]
        self.assertEqual(
            components["ApprovalDecisionEnum"]["enum"],
            ["approve", "reject"],
        )
        self.assertEqual(
            components["RecommendationDecisionEnum"]["enum"],
            ["accept", "override", "reject"],
        )
        self.assertFalse(
            any(re.fullmatch(r"Decision[0-9A-Fa-f]+Enum", name) for name in components),
        )

    def test_security_is_inferred_per_operation_without_global_override(self):
        self.assertNotIn("SECURITY", settings.SPECTACULAR_SETTINGS)
        schema = SchemaGenerator().get_schema(request=None, public=True)

        self.assertNotIn("security", schema["paths"]["/api/v1/auth/token/"]["post"])
        self.assertEqual(
            schema["paths"]["/api/v1/production-runs/"]["post"]["security"],
            [{"jwtAuth": []}],
        )
        self.assertEqual(
            schema["components"]["securitySchemes"]["jwtAuth"],
            {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        )

    def test_project_delete_documents_conflict_and_not_found_responses(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        responses = schema["paths"]["/api/v1/projects/{id}/"]["delete"]["responses"]

        self.assertEqual(set(responses), {"204", "404", "409"})
        for response_code in ("404", "409"):
            self.assertEqual(
                responses[response_code]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/ProjectDeleteErrorResponse",
            )

        components = schema["components"]["schemas"]
        code_ref = components["ProjectDeleteError"]["properties"]["code"]["$ref"]
        self.assertEqual(
            set(components[code_ref.rsplit("/", 1)[-1]]["enum"]),
            {"not_found", "project_has_active_work", "protected_resource"},
        )
        self.assertEqual(
            set(components["ProjectDeleteErrorDetails"]["properties"]),
            {
                "detail",
                "active_generation_tasks",
                "unresolved_remote_tasks",
                "active_assembly_jobs",
            },
        )

    def test_project_cover_contract_is_writable_by_id_and_returns_signed_url(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]

        project = components["SeriesProject"]["properties"]
        self.assertEqual(project["cover_asset"]["format"], "uuid")
        self.assertTrue(project["cover_asset"]["nullable"])
        self.assertTrue(project["cover_url"]["readOnly"])

        request = components["SeriesProjectRequest"]["properties"]
        patched = components["PatchedSeriesProjectRequest"]["properties"]
        self.assertEqual(request["cover_asset"]["format"], "uuid")
        self.assertTrue(request["cover_asset"]["nullable"])
        self.assertNotIn("cover_url", request)
        self.assertEqual(patched["cover_asset"]["format"], "uuid")

    def test_creative_generate_write_path_is_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        self.assertNotIn(
            "/api/v1/projects/{project_id}/creative-drafts/generate/",
            paths,
        )

    def test_translation_confirm_documents_structured_conflict_response(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        responses = schema["paths"][
            "/api/v1/translations/{id}/confirm/"
        ]["post"]["responses"]

        self.assertEqual(set(responses), {"200", "409"})
        self.assertEqual(
            responses["409"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PromptTranslationConflictErrorResponse",
        )

    def test_creative_draft_mutation_paths_are_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        for path in (
            "/api/v1/creative-drafts/{id}/",
            "/api/v1/creative-drafts/{id}/regenerate/",
            "/api/v1/creative-drafts/{id}/confirm/",
            "/api/v1/creative-drafts/{id}/apply/",
        ):
            self.assertNotIn(path, paths)

    def test_fixed_creative_provider_capability_is_absent(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)

        self.assertNotIn("/api/v1/capabilities/creative-drafts/", schema["paths"])
        self.assertNotIn("CreativeDraftCapability", schema["components"]["schemas"])
        self.assertNotIn(
            "CreativeDraftProviderCapability",
            schema["components"]["schemas"],
        )
        for history_schema in ("EpisodeCreativeDraft", "ShotDetailDraft"):
            properties = schema["components"]["schemas"][history_schema]["properties"]
            self.assertTrue({
                "context_current", "can_edit", "can_regenerate", "can_confirm", "can_apply",
            }.isdisjoint(properties))

    def test_series_concept_write_path_is_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        self.assertNotIn("/api/v1/series-concept-drafts/generate/", paths)

    def test_shot_detail_batch_is_read_only_and_apply_path_is_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        list_path = "/api/v1/projects/{project_id}/shot-detail-draft-batches/"
        self.assertEqual(set(paths[list_path]), {"get"})
        self.assertIn("/api/v1/shot-detail-draft-batches/{batch_id}/", paths)
        self.assertNotIn(
            "/api/v1/shot-detail-draft-batches/{batch_id}/apply/",
            paths,
        )

    def test_episode_shot_sync_path_is_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        self.assertNotIn("/api/v1/episodes/{id}/sync-shots/", paths)

    def test_production_run_is_the_only_generation_entry_contract(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]

        self.assertNotIn("/api/v1/projects/{id}/build-production/", paths)
        self.assertNotIn("/api/v1/prompts/generate/", paths)
        self.assertNotIn("/api/v1/workflows/generate/", paths)

        task_collection = paths["/api/v1/tasks/"]
        self.assertEqual(set(task_collection), {"get"})
        self.assertEqual(set(paths["/api/v1/tasks/{id}/"]), {"get"})
        self.assertNotIn("/api/v1/tasks/{id}/retry/", paths)
        self.assertNotIn("/api/v1/tasks/{id}/cancel/", paths)
        self.assertEqual(
            set(paths["/api/v1/tasks/{id}/input-prompt/"]),
            {"get"},
        )
        self.assertEqual(
            set(paths["/api/v1/tasks/{id}/comfyui-workflow/"]),
            {"get"},
        )

        create = paths["/api/v1/production-runs/"]["post"]
        request_ref = create["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        request = schema["components"]["schemas"][request_ref.rsplit("/", 1)[-1]]
        self.assertTrue({
            "project_id", "recipe_version_id", "idempotency_key",
        }.issubset(request["required"]))
        self.assertTrue({
            "allow_external_calls", "budget_limit", "max_generation_tasks",
            "config_overrides", "workflow_selection",
        }.issubset(request["properties"]))
        workflow_selection = schema["components"]["schemas"][
            request["properties"]["workflow_selection"]["allOf"][0]["$ref"].rsplit("/", 1)[-1]
        ]
        self.assertEqual(workflow_selection["properties"]["schema_version"]["default"], 1)
        self.assertEqual(workflow_selection["properties"]["route_index"]["maximum"], 15)
        workflow_mode_ref = workflow_selection["properties"]["mode"]["$ref"]
        self.assertEqual(
            set(schema["components"]["schemas"][workflow_mode_ref.rsplit("/", 1)[-1]]["enum"]),
            {"uniform", "per_shot"},
        )
        shot_route_ref = workflow_selection["properties"]["shot_routes"]["items"]["$ref"]
        shot_route = schema["components"]["schemas"][shot_route_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(shot_route["required"]), {"shot_id", "route_index"})
        route_options = paths[
            "/api/v1/production-recipes/{id}/media-route-options/"
        ]["get"]
        route_options_ref = route_options["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        route_options_schema = schema["components"]["schemas"][
            route_options_ref.rsplit("/", 1)[-1]
        ]
        self.assertTrue({
            "recipe_version_id", "recipe_definition_sha256", "routes",
        }.issubset(route_options_schema["required"]))
        self.assertEqual(set(create["responses"]), {"200", "201", "400", "409"})
        for status in ("400", "409"):
            self.assertEqual(
                create["responses"][status]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/ConfigurationErrorResponse",
            )

        for action in (
            "approve", "continue", "retry", "resolve-uncertain", "cancel",
            "select-artifact", "select-route", "authorize-cost",
        ):
            path = f"/api/v1/production-runs/{{id}}/{action}/"
            self.assertIn(path, paths)
            self.assertIn("post", paths[path])

        approve = paths["/api/v1/production-runs/{id}/approve/"]["post"]
        approve_ref = approve["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        approve_request = schema["components"]["schemas"][approve_ref.rsplit("/", 1)[-1]]
        self.assertIn("adopt_generated_metadata", approve_request["properties"])
        self.assertFalse(approve_request["properties"]["adopt_generated_metadata"]["default"])
        self.assertNotIn("/api/v1/script-structure/", paths)
        self.assertNotIn("/api/v1/script-structure/preview/", paths)

        for action in ("retry", "resolve-uncertain"):
            responses = paths[
                f"/api/v1/production-runs/{{id}}/{action}/"
            ]["post"]["responses"]
            self.assertEqual(set(responses), {"200", "409"})
            self.assertEqual(
                responses["409"]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/ProductionRunConflictResponse",
            )

    def test_control_plane_actions_and_diagnostic_surfaces_are_explicit(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]

        expected_actions = {
            "/api/v1/production-readiness/": "get",
            "/api/v1/model-profile-versions/{id}/publish/": "post",
            "/api/v1/model-profile-versions/{id}/retire/": "post",
            "/api/v1/model-deployments/{id}/probe/": "post",
            "/api/v1/model-deployments/{id}/smoke-test/": "post",
            "/api/v1/model-deployments/{id}/resolve-smoke-uncertain/": "post",
            "/api/v1/model-deployments/{id}/evidence/": "get",
            "/api/v1/model-artifacts/{id}/verify/": "post",
            "/api/v1/workflow-template-versions/{id}/validate/": "post",
            "/api/v1/workflow-template-versions/{id}/smoke-test/": "post",
            "/api/v1/workflow-template-versions/{id}/comfyui-workflow/": "get",
            "/api/v1/workflow-smoke-attempts/{id}/": "get",
            "/api/v1/workflow-smoke-attempts/{id}/cancel/": "post",
            "/api/v1/model-deployment-smoke-attempts/{id}/": "get",
            "/api/v1/model-deployment-smoke-attempts/{id}/cancel/": "post",
            "/api/v1/workflow-template-versions/{id}/publish/": "post",
            "/api/v1/workflow-template-versions/{id}/retire/": "post",
            "/api/v1/config-revisions/{id}/publish/": "post",
            "/api/v1/config-revisions/{id}/activate/": "post",
            "/api/v1/config-revisions/resolved/": "get",
            "/api/v1/config-definitions/{id}/resource-options/": "get",
            "/api/v1/runtime-hosts/{id}/probe/": "post",
            "/api/v1/runtime-host-probe-attempts/": "get",
            "/api/v1/runtime-host-probe-attempts/{id}/": "get",
            "/api/v1/runtime-host-probe-attempts/{id}/cancel/": "post",
            "/api/v1/runtime-hosts/{id}/latest-snapshot/": "get",
            "/api/v1/runtime-hosts/{id}/status/": "post",
            "/api/v1/runtime-hosts/{id}/recommend/": "post",
            "/api/v1/runtime-hosts/{id}/readiness/": "get",
            "/api/v1/runtime-hosts/node-metadata/": "get",
            "/api/v1/recommendations/{id}/decide/": "post",
            "/api/v1/script-revisions/{id}/confirm/": "post",
            "/api/v1/production-recipes/{id}/publish/": "post",
            "/api/v1/production-recipes/{id}/retire/": "post",
        }
        for path, method in expected_actions.items():
            with self.subTest(path=path, method=method):
                self.assertIn(path, paths)
                self.assertIn(method, paths[path])

        self.assertEqual(
            set(paths["/api/v1/workflow-template-versions/{id}/smoke-test/"]["post"]["responses"]),
            {"202"},
        )
        workflow_export = paths[
            "/api/v1/workflow-template-versions/{id}/comfyui-workflow/"
        ]["get"]
        self.assertEqual(set(workflow_export["responses"]), {"200", "400", "403", "404", "503"})
        self.assertEqual(
            {parameter["name"] for parameter in workflow_export["parameters"]},
            {"deployment_id", "download", "id"},
        )
        export_ref = workflow_export["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        export_properties = schema["components"]["schemas"][export_ref.rsplit("/", 1)[-1]]["properties"]
        self.assertTrue({
            "workflow_version_id", "format", "filename", "graph_sha256", "workflow",
            "import_ready", "direct_open_supported", "direct_open_url", "open_mode",
            "download_url", "deployment_id", "deployment_name", "deployment_source",
        }.issubset(export_properties))
        self.assertIs(export_properties["direct_open_supported"]["default"], False)
        open_mode_ref = export_properties["open_mode"]["allOf"][0]["$ref"]
        self.assertEqual(
            schema["components"]["schemas"][open_mode_ref.rsplit("/", 1)[-1]]["enum"],
            ["download_import"],
        )
        deployment_source_ref = export_properties["deployment_source"]["allOf"][0]["$ref"]
        self.assertEqual(
            set(schema["components"]["schemas"][deployment_source_ref.rsplit("/", 1)[-1]]["enum"]),
            {"validated", "explicit_draft"},
        )
        host_probe = paths["/api/v1/runtime-hosts/{id}/probe/"]["post"]
        self.assertEqual(set(host_probe["responses"]), {"202", "409"})
        self.assertEqual(
            host_probe["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/RuntimeHostProbeAttempt",
        )
        self.assertEqual(
            set(paths["/api/v1/runtime-host-probe-attempts/{id}/cancel/"]["post"]["responses"]),
            {"202"},
        )
        self.assertIn(
            "202",
            paths["/api/v1/model-deployments/{id}/smoke-test/"]["post"]["responses"],
        )
        deployment_smoke = paths[
            "/api/v1/model-deployments/{id}/smoke-test/"
        ]["post"]
        smoke_request_ref = deployment_smoke[
            "requestBody"
        ]["content"]["application/json"]["schema"]["$ref"]
        smoke_request = schema["components"]["schemas"][
            smoke_request_ref.rsplit("/", 1)[-1]
        ]
        operation_refs = smoke_request["properties"]["operation"]["oneOf"]
        operation_enum_ref = next(
            item["$ref"] for item in operation_refs
            if item["$ref"].rsplit("/", 1)[-1] != "NullEnum"
        )
        self.assertIn(
            "script.structure",
            schema["components"]["schemas"][
                operation_enum_ref.rsplit("/", 1)[-1]
            ]["enum"],
        )

        node_metadata = paths["/api/v1/runtime-hosts/node-metadata/"]["get"]
        self.assertEqual(
            {parameter["name"] for parameter in node_metadata["parameters"]},
            {"deployment_id", "search", "offset", "limit"},
        )
        node_response_ref = node_metadata["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        node_properties = schema["components"]["schemas"][node_response_ref.rsplit("/", 1)[-1]]["properties"]
        self.assertTrue({
            "schema_version", "total_count", "offset", "limit", "next_offset",
            "catalog_sha256", "items",
        }.issubset(node_properties))
        self.assertTrue({"count", "truncated"}.isdisjoint(node_properties))

        generation_attempt = schema["components"]["schemas"]["GenerationAttempt"]["properties"]
        self.assertTrue({
            "provider_request_id_sha256", "provider_request_id_suffix",
            "request_snapshot_sha256", "request_snapshot_size_bytes",
        }.issubset(generation_attempt))

    def test_prompt_profile_control_plane_and_frozen_runtime_references_are_explicit(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        collections = (
            "/api/v1/prompt-field-definitions/",
            "/api/v1/prompt-profile-versions/",
            "/api/v1/prompt-profile-field-rules/",
            "/api/v1/prompt-profile-bindings/",
        )
        for path in collections:
            with self.subTest(path=path):
                self.assertEqual(set(paths[path]), {"get", "post"})
        for action in ("publish", "retire", "preview"):
            path = f"/api/v1/prompt-profile-versions/{{id}}/{action}/"
            self.assertIn(path, paths)
            self.assertEqual(set(paths[path]), {"post"})
        self.assertEqual(
            set(paths["/api/v1/prompt-profile-versions/options/"]),
            {"get"},
        )

        components = schema["components"]["schemas"]
        profile = components["PromptProfileVersion"]["properties"]
        self.assertTrue({
            "model_family", "operation", "mode", "review_state",
            "contract_sha256", "field_rule_count",
        }.issubset(profile))
        rule = components["PromptProfileFieldRule"]["properties"]
        self.assertTrue({
            "field_definition", "source_selector", "channel", "position",
            "label_style", "separator", "required", "max_chars",
        }.issubset(rule))
        generation_attempt = components["GenerationAttempt"]["properties"]
        self.assertTrue({
            "prompt_profile_version", "prompt_profile_binding_key",
            "prompt_profile_contract_sha256",
        }.issubset(generation_attempt))
        prompt_artifact = components["PromptArtifact"]["properties"]
        self.assertTrue({
            "prompt_profile_version", "prompt_profile_binding_key",
            "prompt_profile_contract_sha256",
        }.issubset(prompt_artifact))
        self.assertTrue({
            "provider_request_id", "request_snapshot", "result", "error_message",
        }.isdisjoint(generation_attempt))

        availability = schema["components"]["schemas"]["RuntimeModelAvailability"]["properties"]
        self.assertTrue({
            "observed_size_bytes", "observed_sha256", "observation_source",
        }.issubset(availability))
        self.assertFalse(any("path" in key.lower() for key in availability))

        production_readiness = paths["/api/v1/production-readiness/"]["get"]
        self.assertEqual(production_readiness["security"], [{"jwtAuth": []}])
        self.assertEqual(
            production_readiness["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductionReadinessResponse",
        )

        self.assertEqual(set(paths["/api/v1/tasks/"]), {"get"})
        self.assertEqual(set(paths["/api/v1/tasks/{id}/"]), {"get"})

    def test_catalog_lifecycle_errors_and_audit_surface_use_one_envelope(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]

        delete_paths = (
            "/api/v1/provider-profiles/{id}/",
            "/api/v1/model-profile-versions/{id}/",
            "/api/v1/model-deployments/{id}/",
            "/api/v1/model-artifacts/{id}/",
            "/api/v1/workflow-template-versions/{id}/",
            "/api/v1/config-definitions/{id}/",
            "/api/v1/production-recipes/{id}/",
        )
        action_paths = (
            "/api/v1/model-profile-versions/{id}/publish/",
            "/api/v1/model-profile-versions/{id}/retire/",
            "/api/v1/workflow-template-versions/{id}/publish/",
            "/api/v1/workflow-template-versions/{id}/retire/",
            "/api/v1/config-revisions/{id}/publish/",
            "/api/v1/config-revisions/{id}/activate/",
            "/api/v1/production-recipes/{id}/publish/",
            "/api/v1/production-recipes/{id}/retire/",
            "/api/v1/runtime-hosts/{id}/status/",
        )
        for path in delete_paths:
            with self.subTest(path=path):
                self.assertEqual(
                    set(paths[path]["delete"]["responses"]),
                    {"204", "400", "403", "404", "409"},
                )
        for path in action_paths:
            with self.subTest(path=path):
                self.assertEqual(
                    set(paths[path]["post"]["responses"]),
                    {"200", "400", "403", "404", "409"},
                )

        error = schema["components"]["schemas"]["ConfigurationErrorResponse"]
        self.assertEqual(set(error["properties"]), {"error"})
        error_body_ref = error["properties"]["error"]["$ref"]
        error_body = schema["components"]["schemas"][error_body_ref.rsplit("/", 1)[-1]]
        self.assertEqual(
            set(error_body["properties"]),
            {"status_code", "code", "details"},
        )

        self.assertIn("/api/v1/platform-lifecycle-audit-events/", paths)
        self.assertEqual(
            set(paths["/api/v1/platform-lifecycle-audit-events/"]),
            {"get"},
        )
        audit_properties = schema["components"]["schemas"][
            "PlatformLifecycleAuditEvent"
        ]["properties"]
        self.assertTrue({
            "actor", "actor_username", "action", "object_type", "object_id",
            "reason_code", "before_sha256", "after_sha256", "before_summary",
            "after_summary", "created_at",
        }.issubset(audit_properties))
        self.assertTrue({
            "secret", "endpoint", "path", "raw_json",
        }.isdisjoint(audit_properties))
        self.assertEqual(set(paths["/api/v1/assemblies/"]), {"get"})
        self.assertEqual(set(paths["/api/v1/assemblies/{id}/"]), {"get"})
        self.assertEqual(set(paths["/api/v1/release-bundles/"]), {"get"})
        self.assertEqual(set(paths["/api/v1/release-bundles/{id}/"]), {"get"})
        self.assertEqual(set(paths["/api/v1/release-bundles/{id}/verify/"]), {"get"})

        self.assertNotIn("/api/v1/tasks/{id}/cancel/", paths)
        self.assertNotIn("/api/v1/tasks/{id}/retry/", paths)
        for action in ("input-prompt", "comfyui-workflow"):
            self.assertEqual(
                set(paths[f"/api/v1/tasks/{{id}}/{action}/"]),
                {"get"},
            )

        decision_responses = paths[
            "/api/v1/recommendations/{id}/decide/"
        ]["post"]["responses"]
        self.assertEqual(set(decision_responses), {"201", "409"})

        self.assertEqual(
            set(paths["/api/v1/model-deployments/{id}/"]["delete"]["responses"]),
            {"204", "400", "403", "404", "409"},
        )
        self.assertEqual(
            set(paths["/api/v1/model-artifacts/{id}/verify/"]["post"]["responses"]),
            {"200", "400", "404", "409"},
        )
        evidence_schema = paths[
            "/api/v1/model-deployments/{id}/evidence/"
        ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            evidence_schema["$ref"],
            "#/components/schemas/PaginatedModelDeploymentCapabilitySnapshotList",
        )
        evidence_fields = schema["components"]["schemas"][
            "ModelDeploymentCapabilitySnapshot"
        ]["properties"]
        self.assertIn("credential_bound", evidence_fields)
        self.assertNotIn("credential_binding_sha256", evidence_fields)
        for path in (
            "/api/v1/model-profile-versions/{id}/retire/",
            "/api/v1/workflow-template-versions/{id}/retire/",
            "/api/v1/production-recipes/{id}/retire/",
            "/api/v1/runtime-hosts/{id}/status/",
        ):
            self.assertTrue({"200", "400", "409"}.issubset(
                paths[path]["post"]["responses"],
            ))
        self.assertEqual(
            set(paths["/api/v1/workflow-template-versions/{id}/"]),
            {"get", "delete"},
        )

    def test_platform_configuration_contracts_are_typed(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]

        resolved = paths["/api/v1/config-revisions/resolved/"]["get"]
        self.assertEqual(
            {parameter["name"] for parameter in resolved["parameters"]},
            {"project_id", "run_id"},
        )
        self.assertEqual(set(resolved["responses"]), {"200", "409"})
        self.assertEqual(
            resolved["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ResolvedConfigurationResponse",
        )

        project_config = paths["/api/v1/projects/{project_id}/config/"]
        self.assertEqual(
            project_config["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProjectEffectiveConfigurationResponse",
        )
        for method, request_component in (
            ("put", "ProjectConfigWriteRequest"),
            ("patch", "PatchedProjectConfigWriteRequest"),
        ):
            self.assertEqual(
                project_config[method]["requestBody"]["content"]["application/json"]["schema"]["$ref"],
                f"#/components/schemas/{request_component}",
            )
            self.assertEqual(
                project_config[method]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/ProjectConfigResponse",
            )

        readiness = paths["/api/v1/runtime-hosts/{id}/readiness/"]["get"]
        self.assertEqual(
            readiness["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/RuntimeReadinessResponse",
        )

    def test_all_retired_write_operations_are_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        retired_paths = (
            "/api/v1/series-concept-drafts/generate/",
            "/api/v1/projects/{project_id}/creative-drafts/generate/",
            "/api/v1/projects/{project_id}/shot-detail-drafts/generate/",
            "/api/v1/projects/{project_id}/asset-bible-draft/",
            "/api/v1/projects/{project_id}/bible-drafts/from-assets/",
            "/api/v1/projects/{project_id}/visual-rules-draft/generate/",
            "/api/v1/projects/{project_id}/visual-rules-draft/apply/",
            "/api/v1/projects/{project_id}/visual-rules-drafts/generate/",
            "/api/v1/projects/{project_id}/visual-rules-drafts/apply/",
            "/api/v1/projects/{id}/script-bible-candidates/apply/",
            "/api/v1/projects/{id}/sync-bibles/",
            "/api/v1/projects/{id}/breakdown-script/",
            "/api/v1/episodes/{id}/sync-shots/",
            "/api/v1/projects/{id}/build-production/",
            "/api/v1/prompts/generate/",
            "/api/v1/workflows/generate/",
            "/api/v1/creative-drafts/",
            "/api/v1/creative-drafts/{id}/",
            "/api/v1/creative-drafts/{id}/regenerate/",
            "/api/v1/creative-drafts/{id}/confirm/",
            "/api/v1/creative-drafts/{id}/apply/",
            "/api/v1/shot-detail-drafts/",
            "/api/v1/shot-detail-drafts/{id}/",
            "/api/v1/shot-detail-drafts/{id}/regenerate/",
            "/api/v1/shot-detail-drafts/{id}/confirm/",
            "/api/v1/shot-detail-drafts/{id}/apply/",
            "/api/v1/shot-detail-draft-batches/{batch_id}/apply/",
            "/api/v1/tasks/{id}/promote-output-to-asset/",
            "/api/v1/tasks/{id}/publish-output/",
            "/api/v1/tasks/{id}/sync-comfyui-workflow/",
            "/api/v1/tasks/{id}/resume-batch/",
            "/api/v1/tasks/{id}/cancel-batch/",
            "/api/v1/tasks/{id}/delete-batch/",
            "/api/v1/assemblies/episode/",
            "/api/v1/assemblies/series/",
            "/api/v1/release-bundles/propose/",
            "/api/v1/release-bundles/{id}/seal/",
            "/api/v1/release-bundles/{id}/release/",
            "/api/v1/workflow-templates/production-options/",
            "/api/v1/workflow-templates/{id}/validate-comfyui/",
        )
        for path in retired_paths:
            with self.subTest(path=path):
                self.assertNotIn(path, paths)

        for path in (
            "/api/v1/projects/{project_id}/shot-detail-draft-batches/",
            "/api/v1/projects/{id}/bible-image-candidates/",
            "/api/v1/tasks/",
            "/api/v1/release-bundles/",
            "/api/v1/workflow-templates/",
        ):
            with self.subTest(path=path, method="post"):
                self.assertNotIn("post", paths[path])

        for path in (
            "/api/v1/tasks/{id}/",
            "/api/v1/release-bundles/{id}/",
            "/api/v1/workflow-templates/{id}/",
        ):
            for method in ("put", "patch", "delete"):
                with self.subTest(path=path, method=method):
                    self.assertNotIn(method, paths[path])

    def test_script_breakdown_path_is_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        self.assertNotIn("/api/v1/projects/{id}/breakdown-script/", paths)

    def test_script_bible_apply_and_sync_paths_are_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        self.assertNotIn(
            "/api/v1/projects/{id}/script-bible-candidates/apply/",
            paths,
        )
        self.assertNotIn("/api/v1/projects/{id}/sync-bibles/", paths)

    def test_bible_image_candidates_are_read_only_and_task_promotion_is_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        candidate_path = "/api/v1/projects/{id}/bible-image-candidates/"
        self.assertEqual(set(paths[candidate_path]), {"get"})
        self.assertNotIn("/api/v1/tasks/{id}/promote-output-to-asset/", paths)

    def test_deferred_generation_request_contract_is_closed_and_explicit(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        collection = paths["/api/v1/deferred-generation-requests/"]
        self.assertEqual(set(collection), {"get"})
        list_response = collection["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            list_response["$ref"],
            "#/components/schemas/PaginatedDeferredGenerationRequestListList",
        )
        detail_response = schema["paths"][
            "/api/v1/deferred-generation-requests/{id}/"
        ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            detail_response["$ref"],
            "#/components/schemas/DeferredGenerationRequestDetail",
        )
        self.assertEqual(
            set(paths["/api/v1/deferred-generation-requests/{id}/"]),
            {"get"},
        )
        self.assertNotIn("/api/v1/deferred-generation-requests/{id}/cancel/", paths)
        self.assertNotIn("/api/v1/deferred-generation-requests/{id}/retry/", paths)

    def test_generated_artifact_contract_exposes_frozen_comparison_parameters(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        self.assertEqual(set(paths["/api/v1/generated-artifacts/"]), {"get"})
        self.assertEqual(set(paths["/api/v1/generated-artifacts/{id}/"]), {"get"})

        components = schema["components"]["schemas"]
        self.assertIn(
            "comparison_parameters",
            components["GeneratedArtifactList"]["properties"],
        )
        self.assertIn(
            "comparison_parameters",
            components["GeneratedArtifactDetail"]["properties"],
        )
