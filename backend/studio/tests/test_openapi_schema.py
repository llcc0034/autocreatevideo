import re

from django.conf import settings
from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator

class OpenApiEnumNamingTests(SimpleTestCase):
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

    def test_workflow_projector_options_publish_typed_editor_contract(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        response = schema["paths"][
            "/api/v1/workflow-projector-versions/options/"
        ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            response["$ref"],
            "#/components/schemas/WorkflowProjectorOptions",
        )
        components = schema["components"]["schemas"]
        options = components["WorkflowProjectorOptions"]
        self.assertTrue({
            "definition_schema_version", "editor_schema_version", "engines",
            "definition_editor",
        }.issubset(options["required"]))
        engine = components["WorkflowProjectorEngineOption"]
        self.assertTrue({
            "description", "projection_kind", "adapter_contract",
            "locked_fields", "projection_fields",
        }.issubset(engine["required"]))
        editor = components["WorkflowProjectorDefinitionEditor"]
        self.assertEqual(
            editor["properties"]["sections"]["items"]["$ref"],
            "#/components/schemas/WorkflowProjectorEditorSection",
        )
        self.assertEqual(
            editor["properties"]["choices"]["$ref"],
            "#/components/schemas/WorkflowProjectorEditorChoiceCatalog",
        )

        discard = schema["paths"][
            "/api/v1/workflow-projector-versions/{id}/discard/"
        ]["post"]
        self.assertNotIn("requestBody", discard)
        self.assertTrue(
            {"204", "400", "403", "404", "409"}.issubset(
                discard["responses"],
            ),
        )

    def test_acceptance_smoke_contract_has_one_page_with_discriminated_rows(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        response = schema["paths"]["/api/v1/acceptance-smoke/"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            response["$ref"],
            "#/components/schemas/AcceptanceSmokePage",
        )

        components = schema["components"]["schemas"]
        page = components["AcceptanceSmokePage"]
        self.assertEqual(page["type"], "object")
        row_ref = page["properties"]["results"]["items"]["$ref"]
        self.assertEqual(row_ref, "#/components/schemas/AcceptanceSmokeRow")
        row = components[row_ref.rsplit("/", 1)[-1]]
        self.assertEqual(
            {item["$ref"] for item in row["oneOf"]},
            {
                "#/components/schemas/WorkflowAcceptanceRow",
                "#/components/schemas/DeploymentAcceptanceRow",
            },
        )
        self.assertEqual(
            row["discriminator"]["propertyName"],
            "subject",
        )
        self.assertEqual(
            row["discriminator"]["mapping"],
            {
                "workflow": "#/components/schemas/WorkflowAcceptanceRow",
                "deployment": "#/components/schemas/DeploymentAcceptanceRow",
            },
        )
        deployment = components["AcceptanceDeploymentSummary"]
        for field in (
            "current_target_sha256",
            "profile_contract_state",
            "profile_contract_reason_code",
            "target_state",
            "target_reason_code",
        ):
            self.assertIn(field, deployment["required"])
        self.assertIn(
            "evidence_deployment_id",
            components["WorkflowSmokeFreshness"]["required"],
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

    def test_asset_source_info_is_response_only(self):
        components = SchemaGenerator().get_schema(
            request=None,
            public=True,
        )["components"]["schemas"]

        source_info = components["Asset"]["properties"]["source_info"]
        self.assertTrue(source_info["readOnly"])
        self.assertIn("source_info", components["Asset"]["required"])
        self.assertNotIn("source_info", components["AssetRequest"]["properties"])
        self.assertNotIn(
            "source_info",
            components["PatchedAssetRequest"]["properties"],
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

    def test_translation_promotion_exposes_only_current_contract_fields(self):
        components = SchemaGenerator().get_schema(
            request=None, public=True,
        )["components"]["schemas"]

        self.assertEqual(
            set(components["PromptTranslationPromoteShotRequest"]["properties"]),
            {"shot_id", "entries"},
        )
        self.assertEqual(
            set(components["PromptTranslationPromoteShotResponse"]["properties"]),
            {"shot_id", "promoted_fields", "entries"},
        )
        self.assertFalse(
            any(name.startswith("legacy_") for name in components["Shot"]["properties"]),
        )

    def test_retired_draft_graph_is_absent_from_the_api_contract(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertNotIn("CreativeDraftStatusEnum", settings.SPECTACULAR_SETTINGS[
            "ENUM_NAME_OVERRIDES"
        ])
        for path in (
            "/api/v1/projects/{project_id}/creative-drafts/",
            "/api/v1/projects/{project_id}/shot-detail-drafts/",
            "/api/v1/projects/{project_id}/shot-detail-draft-batches/",
            "/api/v1/shot-detail-draft-batches/{batch_id}/",
        ):
            self.assertNotIn(path, schema["paths"])
        for component in (
            "EpisodeCreativeDraft",
            "ShotDetailDraft",
            "ShotDetailDraftBatch",
            "ShotDetailDraftBatchItem",
            "CreativeDraftStatusEnum",
        ):
            self.assertNotIn(component, schema["components"]["schemas"])

    def test_series_concept_write_path_is_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        self.assertNotIn("/api/v1/series-concept-drafts/generate/", paths)

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
            "bible_image_prompt_profile_binding_id",
        }.issubset(request["properties"]))
        workflow_selection = schema["components"]["schemas"][
            request["properties"]["workflow_selection"]["allOf"][0]["$ref"].rsplit("/", 1)[-1]
        ]
        self.assertEqual(
            workflow_selection["properties"]["schema_version"],
            {"type": "integer", "maximum": 2, "minimum": 2},
        )
        self.assertTrue({"schema_version", "mode"}.issubset(workflow_selection["required"]))
        self.assertNotIn(
            "maximum",
            workflow_selection["properties"]["route_index"],
        )
        workflow_mode_ref = workflow_selection["properties"]["mode"]["$ref"]
        self.assertEqual(
            set(schema["components"]["schemas"][workflow_mode_ref.rsplit("/", 1)[-1]]["enum"]),
            {"uniform", "per_shot"},
        )
        shot_route_ref = workflow_selection["properties"]["shot_routes"]["items"]["$ref"]
        shot_route = schema["components"]["schemas"][shot_route_ref.rsplit("/", 1)[-1]]
        self.assertEqual(
            set(shot_route["required"]),
            {"shot_id", "route_index", "prompt_profile_binding_id"},
        )
        profile_selection_ref = workflow_selection["properties"][
            "prompt_profile_selections"
        ]["items"]["$ref"]
        profile_selection = schema["components"]["schemas"][
            profile_selection_ref.rsplit("/", 1)[-1]
        ]
        self.assertEqual(
            set(profile_selection["required"]),
            {"operation", "prompt_profile_binding_id"},
        )
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
            "schema_version", "recipe_version_id", "recipe_definition_sha256",
            "routes", "bible_image_generation", "effective_limits",
        }.issubset(route_options_schema["required"]))
        self.assertEqual(
            route_options_schema["properties"]["schema_version"],
            {"type": "integer", "maximum": 2, "minimum": 2},
        )
        route_limits_ref = route_options_schema["properties"][
            "effective_limits"
        ]["allOf"][0]["$ref"]
        route_limits = schema["components"]["schemas"][
            route_limits_ref.rsplit("/", 1)[-1]
        ]
        self.assertEqual(
            route_limits["properties"]["max_routing_routes"]["minimum"],
            1,
        )
        route_option_ref = route_options_schema["properties"]["routes"]["items"]["$ref"]
        route_option_schema = schema["components"]["schemas"][
            route_option_ref.rsplit("/", 1)[-1]
        ]
        self.assertIn("prompt_profile_requirements", route_option_schema["required"])

        capability = paths["/api/v1/capabilities/production/"]["get"]
        capability_ref = capability["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        capability_schema = schema["components"]["schemas"][
            capability_ref.rsplit("/", 1)[-1]
        ]
        self.assertTrue({"effective_limits", "creation_defaults"}.issubset(capability_schema["required"]))
        production_limits_ref = capability_schema["properties"][
            "effective_limits"
        ]["$ref"]
        production_limits = schema["components"]["schemas"][
            production_limits_ref.rsplit("/", 1)[-1]
        ]
        self.assertEqual(
            set(production_limits["required"]),
            {
                "max_routing_routes",
                "max_variants",
                "max_episodes",
                "max_shots_per_episode",
                "max_total_shots",
                "max_generation_tasks",
                "max_reference_bindings",
                "max_bible_candidates_per_entity",
                "max_bible_reference_assets_per_entity",
                "max_bible_regeneration_entities_per_request",
                "dialogue_max_entries",
                "dialogue_speaker_id_max_characters",
                "dialogue_text_max_characters",
                "dialogue_delivery_max_characters",
                "execution_monitoring_max_node_type_overrides",
                "execution_monitoring_max_node_overrides",
                "execution_monitoring_max_idle_timeout_seconds",
                "prompt_review_max_candidates",
                "prompt_review_max_advisories",
                "prompt_review_max_input_bytes",
                "prompt_review_max_field_characters",
                "prompt_review_max_usage_count",
                "audit_note_min_characters",
                "audit_note_max_characters",
            },
        )
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
            "/api/v1/model-profile-versions/options/": "get",
            "/api/v1/model-profile-versions/{id}/draft-next/": "post",
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
        self.assertNotIn(
            "Handler method for HTTP 'OPTIONS' request.",
            paths["/api/v1/model-profile-versions/options/"]["get"].get("description", ""),
        )

        self.assertEqual(
            set(paths["/api/v1/workflow-template-versions/{id}/smoke-test/"]["post"]["responses"]),
            {"202", "400", "409"},
        )
        components = schema["components"]["schemas"]
        self.assertEqual(
            set(paths["/api/v1/workflow-template-versions/"]),
            {"get"},
        )
        self.assertNotIn("WorkflowTemplateVersionImportRequest", components)
        model_request = components["ModelProfileVersionRequest"]
        self.assertNotIn("version", model_request["properties"])
        self.assertNotIn("specification_schema_version", model_request["properties"])
        next_request = components["ModelProfileNextVersionInputRequest"]
        self.assertEqual(
            set(next_request["properties"]),
            {"provider", "model_identifier", "modality", "specification"},
        )
        registry_options = components["ModelProfileRegistryOptions"]
        self.assertTrue({
            "specification_schema_version", "provider_drivers", "modalities",
            "artifact_roles", "capabilities", "model_families",
        }.issubset(registry_options["required"]))
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
        self.assertIn(
            "400",
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
        self.assertEqual(
            set(schema["components"]["schemas"][
                operation_enum_ref.rsplit("/", 1)[-1]
            ]["enum"]),
            {
                "image.text_to_image",
                "video.text_to_video",
                "audio.text_to_audio",
                "prompt.normalize",
                "script.generate",
                "script.structure",
                "visual_rules.generate",
            },
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
            "repair_of_attempt_id", "retry_feedback_sha256",
            "request_snapshot_sha256", "request_snapshot_size_bytes",
        }.issubset(generation_attempt))

    def test_saved_comfy_workflow_import_and_attestation_contract_is_explicit(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        components = schema["components"]["schemas"]
        endpoints = {
            "/api/v1/comfy-workflow-sources/discover/": "get",
            "/api/v1/model-deployments/{id}/saved-workflows/preview/": "post",
            "/api/v1/model-deployments/{id}/saved-workflows/attest-model-artifacts/": "post",
            "/api/v1/model-deployments/{id}/saved-workflows/batch-import/": "post",
        }
        for path, method in endpoints.items():
            with self.subTest(path=path):
                self.assertEqual(set(paths[path]), {method})

        self.assertNotIn(
            "/api/v1/model-deployments/{id}/saved-workflows/",
            paths,
        )
        self.assertEqual(
            set(paths["/api/v1/comfy-workflow-sources/discover/"]["get"]["responses"]),
            {"200", "400", "403"},
        )
        scan_ref = paths[
            "/api/v1/comfy-workflow-sources/discover/"
        ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        scan_schema = components[scan_ref.rsplit("/", 1)[-1]]
        self.assertTrue({
            "status", "source_count", "succeeded_source_count", "failed_source_count",
            "matched_count", "review_count", "sources", "items", "effective_limits",
        }.issubset(scan_schema["required"]))
        self.assertEqual(
            set(paths["/api/v1/model-deployments/{id}/saved-workflows/preview/"]["post"]["responses"]),
            {"200", "400", "403", "404"},
        )
        self.assertEqual(
            set(paths[
                "/api/v1/model-deployments/{id}/saved-workflows/attest-model-artifacts/"
            ]["post"]["responses"]),
            {"202", "400", "403", "404", "409"},
        )
        self.assertEqual(
            set(paths[
                "/api/v1/model-deployments/{id}/saved-workflows/batch-import/"
            ]["post"]["responses"]),
            {"200", "400", "403", "404"},
        )

        preview = paths[
            "/api/v1/model-deployments/{id}/saved-workflows/preview/"
        ]["post"]
        preview_request_ref = preview["requestBody"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        preview_request = components[preview_request_ref.rsplit("/", 1)[-1]]
        self.assertTrue({
            "source_path", "expected_sha256",
            "expected_source_scope_schema_version", "expected_source_scope_sha256",
        }.issubset(preview_request["required"]))
        preview_ref = preview["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        preview_schema = components[preview_ref.rsplit("/", 1)[-1]]
        self.assertTrue({
            "primary_output_node_ids", "primary_api_node_ids", "output_candidates",
            "model_dependencies", "asset_dependencies", "object_info_schema_sha256",
            "converted_graph_sha256", "configurable_parameters",
            "parameter_schema_sha256", "structural_contract_sha256",
            "suggested_input_contract",
            "suggested_semantic_bindings", "suggested_model_requirements", "blockers",
            "effective_limits",
        }.issubset(preview_schema["required"]))
        self.assertTrue({
            "smoke_parameter_schema_sha256", "smoke_parameter_contract_sha256",
        }.isdisjoint(preview_schema["properties"]))
        self.assertTrue({"workflow", "api_graph"}.isdisjoint(preview_schema["properties"]))
        parameter_ref = preview_schema["properties"]["configurable_parameters"][
            "items"
        ]["$ref"]
        parameter = components[parameter_ref.rsplit("/", 1)[-1]]
        self.assertTrue({
            "semantic_key", "source_value", "recommended_safe_value", "default",
            "min", "max", "schema_min", "schema_max", "multiple_of",
            "alignment_modulus", "alignment_remainder", "options", "type",
            "selection_group", "target",
        }.issubset(parameter["required"]))
        self.assertEqual(
            {row["type"] for row in parameter["properties"]["source_value"]["oneOf"]},
            {"string", "integer", "number", "boolean"},
        )
        self.assertEqual(parameter["properties"]["min"]["type"], "number")
        self.assertTrue(parameter["properties"]["min"]["nullable"])
        self.assertEqual(parameter["properties"]["alignment_modulus"]["minimum"], 1)
        self.assertEqual(parameter["properties"]["alignment_remainder"]["minimum"], 0)
        self.assertEqual(
            {row["type"] for row in parameter["properties"]["options"]["items"]["oneOf"]},
            {"string", "integer", "number", "boolean"},
        )
        target_ref = parameter["properties"]["target"]["$ref"]
        target = components[target_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(target["required"]), {"node_id", "node_type", "input_name"})
        execution_parameter = components["WorkflowExecutionParameterSchema"]
        self.assertTrue({
            "alignment_modulus", "alignment_remainder",
        }.issubset(execution_parameter["required"]))
        self.assertEqual(
            execution_parameter["properties"]["alignment_modulus"]["minimum"],
            1,
        )

        attest = paths[
            "/api/v1/model-deployments/{id}/saved-workflows/attest-model-artifacts/"
        ]["post"]
        attest_request_ref = attest["requestBody"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        attest_request = components[attest_request_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(attest_request["required"]), {
            "source_path", "expected_sha256",
            "expected_source_scope_schema_version", "expected_source_scope_sha256",
            "primary_output_node_ids",
            "expected_object_info_schema_sha256", "expected_converted_graph_sha256",
            "model_profile_version",
        })
        self.assertTrue({
            "filename", "model_dependencies", "artifact_ids", "model_sha256",
        }.isdisjoint(attest_request["properties"]))
        self.assertEqual(
            attest["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/RuntimeHostProbeAttempt",
        )
        location_header = attest["responses"]["202"]["headers"]["Location"]
        self.assertEqual(location_header["schema"], {"type": "string", "format": "uri"})

        batch = paths[
            "/api/v1/model-deployments/{id}/saved-workflows/batch-import/"
        ]["post"]
        batch_request_ref = batch["requestBody"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        batch_request = components[batch_request_ref.rsplit("/", 1)[-1]]
        self.assertEqual(batch_request["properties"]["items"]["minItems"], 1)
        self.assertNotIn("maxItems", batch_request["properties"]["items"])
        effective_limits = components["ComfySavedWorkflowEffectiveLimits"]
        self.assertEqual(
            effective_limits["properties"]["scan_max_items"]["minimum"],
            1,
        )
        self.assertEqual(
            effective_limits["properties"]["batch_max_items"]["minimum"],
            1,
        )
        self.assertEqual(
            effective_limits["properties"]["scan_max_seconds"]["minimum"],
            0.001,
        )
        self.assertEqual(
            effective_limits["properties"]["request_max_bytes"]["minimum"],
            1,
        )
        self.assertEqual(
            effective_limits["properties"]["batch_max_seconds"]["minimum"],
            0.001,
        )
        self.assertEqual(
            effective_limits["properties"]["saved_workflow_directory"]["type"],
            "string",
        )
        batch_item_ref = batch_request["properties"]["items"]["items"]["$ref"]
        batch_item = components[batch_item_ref.rsplit("/", 1)[-1]]
        self.assertTrue({
            "source_path", "expected_sha256", "expected_source_scope_schema_version",
            "expected_source_scope_sha256", "import_kind",
        }.issubset(batch_item["required"]))
        self.assertTrue({
            "primary_output_node_ids", "expected_object_info_schema_sha256",
            "expected_converted_graph_sha256", "expected_parameter_schema_sha256",
            "expected_structural_contract_sha256", "template", "new_template",
            "model_profile_version",
        }.issubset(batch_item["properties"]))
        self.assertTrue({
            "expected_smoke_parameter_schema_sha256",
            "smoke_parameter_contract_sha256",
            "input_contract", "semantic_bindings", "model_requirements",
            "smoke_inputs", "smoke_asset_bindings",
        }.isdisjoint(batch_item["properties"]))
        batch_response_ref = batch["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        batch_response = components[batch_response_ref.rsplit("/", 1)[-1]]
        result_ref = batch_response["properties"]["items"]["items"]["$ref"]
        result_item = components[result_ref.rsplit("/", 1)[-1]]
        self.assertIn("structural_contract_sha256", result_item["required"])
        self.assertTrue({
            "smoke_parameter_schema_sha256", "smoke_parameter_contract_sha256",
        }.isdisjoint(result_item["properties"]))

        attempt = components["RuntimeHostProbeAttempt"]
        self.assertTrue({
            "probe_kind", "request_snapshot", "request_snapshot_sha256",
            "request_snapshot_size_bytes",
        }.issubset(attempt["properties"]))
        attempt_collection = paths["/api/v1/runtime-host-probe-attempts/"]["get"]
        self.assertIn(
            "probe_kind",
            {parameter["name"] for parameter in attempt_collection["parameters"]},
        )

    def test_workflow_smoke_candidates_are_staff_only_and_typed(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertNotIn(
            "/api/v1/assets/saved-workflow-smoke-candidates/",
            schema["paths"],
        )
        operation = schema["paths"][
            "/api/v1/assets/workflow-smoke-candidates/"
        ]["get"]
        self.assertEqual(operation["security"], [{"jwtAuth": []}])
        self.assertEqual(set(operation["responses"]), {"200", "403"})
        success_ref = operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        success = schema["components"]["schemas"][success_ref.rsplit("/", 1)[-1]]
        self.assertTrue({"count", "next", "previous", "results"}.issubset(
            success["properties"],
        ))
        candidate_ref = success["properties"]["results"]["items"]["$ref"]
        candidate = schema["components"]["schemas"][candidate_ref.rsplit("/", 1)[-1]]
        self.assertTrue({
            "id", "name", "mime_type", "checksum", "smoke_eligible",
            "smoke_ineligible_reason",
        }.issubset(candidate["properties"]))
        error_ref = operation["responses"]["403"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        error = schema["components"]["schemas"][error_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(error["properties"]), {"error"})

    def test_workflow_smoke_fixture_is_per_attempt_and_response_hides_raw_values(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]
        operation = schema["paths"][
            "/api/v1/workflow-template-versions/{id}/smoke-test/"
        ]["post"]

        request_ref = operation["requestBody"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        request = components[request_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(request["required"]), {"deployment_id", "fixture"})
        self.assertEqual(set(request["properties"]), {"deployment_id", "fixture"})

        fixture_ref = request["properties"]["fixture"]["$ref"]
        fixture = components[fixture_ref.rsplit("/", 1)[-1]]
        self.assertEqual(
            set(fixture["properties"]),
            {"schema_version", "values", "assets", "references"},
        )
        self.assertEqual(
            fixture["properties"]["schema_version"],
            {"type": "integer", "maximum": 2, "minimum": 2, "default": 2},
        )
        self.assertEqual(
            {row["type"] for row in fixture["properties"]["values"][
                "additionalProperties"
            ]["oneOf"]},
            {"string", "integer", "number", "boolean"},
        )
        fixture_asset_ref = fixture["properties"]["assets"]["items"]["$ref"]
        fixture_asset = components[fixture_asset_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(fixture_asset["required"]), {"semantic_key", "asset_id"})
        self.assertEqual(set(fixture_asset["properties"]), {"semantic_key", "asset_id"})
        reference_ref = fixture["properties"]["references"]["$ref"]
        references = components[reference_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(references["properties"]), {"pictures", "videos", "audios"})

        response_ref = operation["responses"]["202"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        attempt = components[response_ref.rsplit("/", 1)[-1]]
        self.assertNotIn("fixture_values", attempt["properties"])
        self.assertTrue({
            "fixture_values_schema_version", "fixture_values_size_bytes",
            "fixture_values_sha256", "fixture_sha256", "asset_bindings",
            "resolutions", "resolution_required",
        }.issubset(attempt["required"]))
        binding_ref = attempt["properties"]["asset_bindings"]["items"]["$ref"]
        binding = components[binding_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(binding["required"]), {
            "semantic_key", "input_slot", "asset", "media_kind", "category",
            "checksum", "mime_type", "size_bytes", "created_at",
        })

        resolve_operation = schema["paths"][
            "/api/v1/workflow-smoke-attempts/{id}/resolve-uncertain/"
        ]["post"]
        self.assertEqual(resolve_operation["security"], [{"jwtAuth": []}])
        self.assertEqual(
            set(resolve_operation["responses"]),
            {"200", "201", "400", "403", "404", "409"},
        )
        resolve_request_ref = resolve_operation["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        resolve_request = components[resolve_request_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(resolve_request["required"]), {"outcome", "note"})
        self.assertEqual(
            set(resolve_request["properties"]),
            {"outcome", "note", "provider_request_id"},
        )
        self.assertIs(resolve_request["properties"]["provider_request_id"]["writeOnly"], True)
        outcome_ref = resolve_request["properties"]["outcome"]["$ref"]
        outcome = components[outcome_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(outcome["enum"]), {
            "confirmed_not_submitted",
            "confirmed_remote_failed",
            "confirmed_remote_succeeded",
        })
        resolution_ref = resolve_operation["responses"]["201"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        resolution = components[resolution_ref.rsplit("/", 1)[-1]]
        self.assertTrue({
            "workflow_smoke_attempt_id", "outcome", "provider_request_id_sha256",
            "evidence_sha256", "note", "created_at",
        }.issubset(resolution["required"]))
        self.assertNotIn("provider_request_id", resolution["properties"])

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
        self.assertNotIn(
            "Handler method for HTTP 'OPTIONS' request.",
            paths["/api/v1/prompt-profile-versions/options/"]["get"].get("description", ""),
        )

        components = schema["components"]["schemas"]
        profile = components["PromptProfileVersion"]["properties"]
        self.assertTrue({
            "model_family", "operation", "mode", "review_state",
            "source_projection_key", "channel_limits", "contract_sha256",
            "field_rule_count",
        }.issubset(profile))
        field_definition = components["PromptFieldDefinition"]["properties"]
        self.assertIn("preview_example", field_definition)
        self.assertIs(field_definition["preview_example"]["readOnly"], True)
        preview = components["PromptProfilePreview"]["properties"]
        self.assertTrue({
            "channels", "channel_limits", "channel_audit", "warnings",
            "structured_fields", "trace", "ephemeral", "preview_scope",
            "base_contract_sha256", "executed_stages",
        }.issubset(preview))
        preview_request = components["PromptProfilePreviewInputRequest"]["properties"]
        self.assertTrue({
            "sources", "expected_base_contract_sha256", "candidate",
        }.issubset(preview_request))
        preview_candidate = components[
            "PromptProfilePreviewCandidateRequest"
        ]["properties"]
        self.assertTrue({
            "model_family", "operation", "mode", "source_projection_key",
            "source_projection_config", "channel_limits", "field_rules",
        }.issubset(preview_candidate))
        preview_rule = components[
            "PromptProfilePreviewRuleCandidateRequest"
        ]["properties"]
        self.assertTrue({
            "field_definition", "source_selector", "projection_primitive",
            "channel", "position", "label_style", "separator", "required",
            "omit_if_empty", "max_chars", "generation_mode",
            "generation_source_selector", "generation_context_selectors",
            "generation_instruction", "generation_output_semantics",
            "generation_required", "language_policy", "length_unit",
            "generation_min", "generation_max", "generation_validator_keys",
        }.issubset(preview_rule))
        rule = components["PromptProfileFieldRule"]["properties"]
        self.assertTrue({
            "field_definition", "source_selector", "channel", "position",
            "label_style", "separator", "required", "max_chars",
            "generation_mode", "generation_source_selector",
            "generation_instruction", "generation_output_semantics",
            "generation_required", "language_policy", "length_unit",
            "generation_min", "generation_max", "generation_validator_keys",
            "generation_context_selectors",
        }.issubset(rule))
        self.assertIn(
            "generation_context_selectors",
            components["PromptProfileFieldRule"]["required"],
        )
        request_rule = components["PromptProfileFieldRuleRequest"]
        self.assertIn(
            "generation_context_selectors",
            request_rule["required"],
        )
        self.assertIn(
            "generation_context_selectors",
            components["PatchedPromptProfileFieldRuleRequest"]["properties"],
        )
        selector_ref = request_rule["properties"][
            "generation_context_selectors"
        ]["items"]["$ref"]
        self.assertEqual(
            components[selector_ref.rsplit("/", 1)[-1]]["enum"],
            [
                "project.core",
                "project.style",
                "target_shot.core",
                "target_shot.dialogue_metadata",
                "target_shot.dialogue_authored_text",
                "target_shot.linked_bible",
                "adjacent_shots.core",
                "all_shots.core",
            ],
        )
        prompt_options = components["PromptProfileOptions"]["properties"]
        self.assertTrue({
            "generation_modes", "generation_output_semantics",
            "source_projection_keys", "language_policies", "length_units",
            "generation_validators", "generation_context_selectors",
            "generation_source_selectors",
            "effective_limits",
        }.issubset(prompt_options))
        self.assertIn(
            "generation_context_selectors",
            components["PromptProfileOptions"]["required"],
        )
        option_ref = prompt_options["generation_context_selectors"]["items"]["$ref"]
        option_schema = components[option_ref.rsplit("/", 1)[-1]]
        self.assertEqual(
            set(option_schema["required"]),
            {"value", "label", "description"},
        )
        self.assertTrue(all(
            option_schema["properties"][key]["readOnly"]
            for key in ("value", "label", "description")
        ))
        self.assertIn(
            "generation_source_selectors",
            components["PromptProfileOptions"]["required"],
        )
        generation_source_ref = prompt_options[
            "generation_source_selectors"
        ]["items"]["$ref"]
        generation_source_schema = components[
            generation_source_ref.rsplit("/", 1)[-1]
        ]
        self.assertEqual(
            set(generation_source_schema["required"]),
            {
                "value", "label", "scope_type", "group", "group_label",
                "description", "availability", "scope", "dialogue_exposure",
                "cross_shot_exposure", "cardinality", "status",
                "generation_modes",
            },
        )
        self.assertTrue(all(
            generation_source_schema["properties"][key]["readOnly"]
            for key in generation_source_schema["required"]
        ))
        limits_ref = prompt_options["effective_limits"]["allOf"][0]["$ref"]
        limits = components[limits_ref.rsplit("/", 1)[-1]]
        self.assertEqual(
            set(limits["required"]),
            {
                "field_text_max_characters",
                "max_validators",
                "max_context_selectors",
            },
        )
        self.assertTrue(all(
            limits["properties"][key]["minimum"] == 1
            for key in limits["required"]
        ))
        for field_name in (
            "max_chars",
            "generation_min",
            "generation_max",
        ):
            self.assertNotEqual(
                request_rule["properties"][field_name].get("maximum"),
                30_000,
            )
        self.assertNotIn(
            "maxItems",
            request_rule["properties"]["generation_validator_keys"],
        )
        self.assertNotIn(
            "maxItems",
            request_rule["properties"]["generation_context_selectors"],
        )
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

        registry_detail_paths = (
            "/api/v1/provider-profiles/{id}/",
            "/api/v1/model-profile-versions/{id}/",
            "/api/v1/model-deployments/{id}/",
            "/api/v1/model-artifacts/{id}/",
            "/api/v1/workflow-template-versions/{id}/",
            "/api/v1/config-definitions/{id}/",
            "/api/v1/production-recipes/{id}/",
            "/api/v1/prompt-field-definitions/{id}/",
            "/api/v1/prompt-profile-versions/{id}/",
            "/api/v1/prompt-profile-field-rules/{id}/",
            "/api/v1/prompt-profile-bindings/{id}/",
        )
        action_paths = (
            "/api/v1/model-profile-versions/{id}/publish/",
            "/api/v1/model-profile-versions/{id}/retire/",
            "/api/v1/model-deployments/{id}/retire/",
            "/api/v1/workflow-template-versions/{id}/publish/",
            "/api/v1/workflow-template-versions/{id}/retire/",
            "/api/v1/config-revisions/{id}/publish/",
            "/api/v1/config-revisions/{id}/activate/",
            "/api/v1/production-recipes/{id}/publish/",
            "/api/v1/production-recipes/{id}/retire/",
            "/api/v1/runtime-hosts/{id}/status/",
        )
        for path in registry_detail_paths:
            with self.subTest(path=path):
                self.assertNotIn("delete", paths[path])
        for path in action_paths:
            with self.subTest(path=path):
                self.assertEqual(
                    set(paths[path]["post"]["responses"]),
                    {"200", "400", "403", "404", "409"},
                )

        preview = paths["/api/v1/registry-deletion-preview/"]["post"]
        self.assertEqual(set(preview["responses"]), {"200", "400", "403", "404"})
        self.assertEqual(
            preview["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/RegistryDeletionPreviewInputRequest",
        )
        execute = paths["/api/v1/registry-deletion-execute/"]["post"]
        self.assertEqual(
            set(execute["responses"]),
            {"200", "400", "403", "404", "409"},
        )
        self.assertEqual(
            execute["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/RegistryDeletionExecuteInputRequest",
        )
        audit_header = next(
            item for item in execute["parameters"]
            if item["name"] == "X-Audit-Reason-Code"
        )
        self.assertEqual(audit_header["in"], "header")
        self.assertTrue(audit_header["required"])

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
            {"get"},
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

        components = schema["components"]["schemas"]
        self.assertEqual(
            components["ConfigurationClassEnum"]["enum"],
            ["standard", "security_boundary"],
        )
        for component_name in (
            "ConfigDefinition",
            "ConfigDefinitionRequest",
            "PatchedConfigDefinitionRequest",
        ):
            with self.subTest(component=component_name):
                self.assertEqual(
                    components[component_name]["properties"][
                        "configuration_class"
                    ]["$ref"],
                    "#/components/schemas/ConfigurationClassEnum",
                )
        changes_security_boundaries = components["ConfigRevision"][
            "properties"
        ]["changes_security_boundaries"]
        self.assertEqual(changes_security_boundaries["type"], "boolean")
        self.assertTrue(changes_security_boundaries["readOnly"])
        self.assertIn(
            "changes_security_boundaries",
            components["ConfigRevision"]["required"],
        )

        readiness = paths["/api/v1/runtime-hosts/{id}/readiness/"]["get"]
        self.assertEqual(
            readiness["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/RuntimeReadinessResponse",
        )

    def test_schema_driven_form_catalogs_and_server_owned_fields_are_explicit(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        components = schema["components"]["schemas"]

        option_paths = {
            "/api/v1/production-recipes/options/": "ProductionRecipeEditorOptions",
            "/api/v1/prompt-profile-versions/options/": "PromptProfileOptions",
            "/api/v1/prompt-field-definitions/options/": "PromptFieldDefinitionOptions",
            "/api/v1/config-definitions/options/": "ConfigDefinitionOptions",
        }
        for path, component in option_paths.items():
            with self.subTest(path=path):
                self.assertEqual(set(paths[path]), {"get"})
                self.assertEqual(
                    paths[path]["get"]["responses"]["200"]["content"][
                        "application/json"
                    ]["schema"]["$ref"],
                    f"#/components/schemas/{component}",
                )

        for path, request_component in (
            (
                "/api/v1/production-recipes/{id}/clone-successor/",
                "ProductionRecipeCloneSuccessorInputRequest",
            ),
            (
                "/api/v1/prompt-profile-versions/{id}/clone-successor/",
                "PromptProfileCloneSuccessorInputRequest",
            ),
        ):
            action = paths[path]["post"]
            self.assertIn("409", action["responses"])
            request_ref = action["requestBody"]["content"]["application/json"][
                "schema"
            ]["$ref"]
            self.assertEqual(request_ref, f"#/components/schemas/{request_component}")

        self.assertEqual(
            set(components["ProductionRecipeCloneSuccessorInputRequest"]["properties"]),
            {"expected_definition_sha256"},
        )
        self.assertEqual(
            set(components["PromptProfileCloneSuccessorInputRequest"]["properties"]),
            {"expected_source_contract_sha256", "name"},
        )
        for component in (
            "ProductionRecipeVersionRequest",
            "PatchedProductionRecipeVersionRequest",
            "PromptProfileVersionRequest",
            "PatchedPromptProfileVersionRequest",
        ):
            self.assertNotIn("version", components[component]["properties"])
        for component in (
            "PromptFieldDefinitionRequest",
            "PatchedPromptFieldDefinitionRequest",
        ):
            self.assertNotIn("display_order", components[component]["properties"])
        create_contracts = (
            (
                "/api/v1/prompt-field-definitions/",
                "PromptFieldDefinitionCreateInputRequest",
                {"is_active", "display_order"},
            ),
            (
                "/api/v1/prompt-profile-versions/",
                "PromptProfileVersionCreateInputRequest",
                {"version", "status", "review_state", "contract_schema_version"},
            ),
            (
                "/api/v1/config-definitions/",
                "ConfigDefinitionCreateInputRequest",
                {"is_active", "needs_review", "review_reason"},
            ),
        )
        for path, component, excluded_fields in create_contracts:
            with self.subTest(create_path=path):
                request_ref = paths[path]["post"]["requestBody"]["content"][
                    "application/json"
                ]["schema"]["$ref"]
                self.assertEqual(request_ref, f"#/components/schemas/{component}")
                self.assertTrue(
                    excluded_fields.isdisjoint(components[component]["properties"]),
                )
        for field in ("has_values", "mutable_fields"):
            self.assertTrue(components["ConfigDefinition"]["properties"][field]["readOnly"])
            self.assertNotIn(field, components["ConfigDefinitionRequest"]["properties"])
        self.assertIn(
            "custom_entry_fields",
            components["ConfigDefinitionOptions"]["required"],
        )
        self.assertIn(
            "rule_system_defaults",
            components["PromptProfileOptions"]["required"],
        )

    def test_all_retired_write_operations_are_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        retired_paths = (
            "/api/v1/series-concept-drafts/generate/",
            "/api/v1/projects/{project_id}/creative-drafts/generate/",
            "/api/v1/projects/{project_id}/shot-detail-drafts/generate/",
            "/api/v1/projects/{project_id}/creative-drafts/",
            "/api/v1/projects/{project_id}/shot-detail-drafts/",
            "/api/v1/projects/{project_id}/shot-detail-draft-batches/",
            "/api/v1/shot-detail-draft-batches/{batch_id}/",
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

    def test_bible_and_storyboard_approval_requests_have_exact_machine_contracts(self):
        schemas = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]

        approval = schemas["ApprovalDecisionInputRequest"]
        self.assertIn("bible_image_bindings", approval["properties"])
        self.assertIn("shot_reference_bindings", approval["properties"])
        self.assertIn("expected_source_snapshot_sha256", approval["properties"])
        self.assertEqual(
            approval["properties"]["bible_image_bindings"]["items"]["$ref"],
            "#/components/schemas/BibleImageBindingSelectionRequest",
        )
        self.assertEqual(
            approval["properties"]["shot_reference_bindings"]["items"]["$ref"],
            "#/components/schemas/StoryboardShotReferenceSelectionRequest",
        )

        bible_selection = schemas["BibleImageBindingSelectionRequest"]
        self.assertEqual(
            set(bible_selection["required"]),
            {"entity_type", "entity_id"},
        )
        selected_ids = bible_selection["properties"]["selected_artifact_ids"]
        self.assertNotIn("maxItems", selected_ids)
        self.assertEqual(selected_ids["items"]["format"], "uuid")
        selected_library = bible_selection["properties"]["selected_library_assets"]
        self.assertEqual(selected_library["minItems"], 0)
        self.assertNotIn("maxItems", selected_library)
        self.assertEqual(
            selected_library["items"]["$ref"],
            "#/components/schemas/BibleLibraryAssetSelectionRequest",
        )
        library_item = schemas["BibleLibraryAssetSelectionRequest"]
        self.assertEqual(
            set(library_item["required"]),
            {"asset_id", "expected_sha256"},
        )
        self.assertEqual(library_item["properties"]["asset_id"]["format"], "uuid")
        self.assertEqual(
            library_item["properties"]["expected_sha256"]["pattern"],
            "^[0-9a-f]{64}$",
        )
        self.assertEqual(bible_selection["properties"]["anchor_artifact_id"]["format"], "uuid")
        self.assertTrue(
            bible_selection["properties"]["anchor_artifact_id"]["nullable"],
        )
        self.assertEqual(
            bible_selection["properties"]["anchor_library_asset_id"]["format"],
            "uuid",
        )
        self.assertTrue(
            bible_selection["properties"]["anchor_library_asset_id"]["nullable"],
        )
        self.assertEqual(
            set(schemas["BibleEntityTypeEnum"]["enum"]),
            {"character", "scene", "prop"},
        )

        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        regenerate = paths[
            "/api/v1/production-runs/{id}/regenerate-bible-images/"
        ]["post"]
        self.assertEqual(set(regenerate["responses"]), {"200", "409"})
        self.assertEqual(
            regenerate["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/BibleImageRegenerationInputRequest",
        )
        regeneration = schemas["BibleImageRegenerationInputRequest"]
        self.assertEqual(
            set(regeneration["required"]),
            {"request_id", "expected_source_snapshot_sha256", "entities"},
        )
        self.assertEqual(regeneration["properties"]["request_id"]["format"], "uuid")
        regeneration_entities = regeneration["properties"]["entities"]
        self.assertEqual(regeneration_entities["minItems"], 1)
        self.assertNotIn("maxItems", regeneration_entities)
        self.assertEqual(
            regeneration_entities["items"]["$ref"],
            "#/components/schemas/BibleImageRegenerationEntityRequest",
        )

        shot_selection = schemas["StoryboardShotReferenceSelectionRequest"]
        self.assertEqual(set(shot_selection["required"]), {"shot_id", "reference_bindings"})
        self.assertEqual(shot_selection["properties"]["shot_id"]["format"], "uuid")
        self.assertEqual(
            shot_selection["properties"]["reference_bindings"]["$ref"],
            "#/components/schemas/StoryboardShotReferenceBindingsRequest",
        )
        shot_bindings = schemas["StoryboardShotReferenceBindingsRequest"]["properties"]
        self.assertTrue(
            {
                "first_frame_asset_id",
                "last_frame_asset_id",
                "characters",
                "scene",
                "props",
            }.issubset(shot_bindings)
        )

    def test_bible_candidate_history_schema_matches_formal_artifact_projection(self):
        schemas = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]

        projection = schemas["BibleImageCandidateProjection"]
        self.assertEqual(
            set(projection["required"]),
            {
                "entity_type",
                "entity_id",
                "entity_name",
                "anchor_asset",
                "reference_assets",
                "latest_candidate",
                "candidate_history",
            },
        )
        for retired_field in ("source_fingerprint", "current_prompt_inputs", "prompt_config"):
            self.assertNotIn(retired_field, projection["properties"])

        history = schemas["BibleImageCandidateHistory"]
        self.assertTrue(
            {
                "artifact_id",
                "production_run_id",
                "stage_run_id",
                "generation_attempt_id",
                "generation_task_id",
                "prompt_artifact_id",
                "workflow_version_id",
                "model_profile_version_id",
                "prompt_profile_version_id",
                "prompt_profile_binding_key",
                "prompt_profile_contract_sha256",
                "source_snapshot_sha256",
                "source_current",
                "integrity_status",
                "integrity_error_code",
                "binding_role",
                "materialized_asset",
                "preview_url",
                "download_url",
                "sha256",
                "size_bytes",
                "mime_type",
            }.issubset(history["required"])
        )
        for retired_field in (
            "generated_workflow_id",
            "task_status",
            "promoted_assets",
            "prompt_inputs",
        ):
            self.assertNotIn(retired_field, history["properties"])

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

    def test_generation_task_contract_exposes_safe_production_group_identity(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        properties = schema["components"]["schemas"]["GenerationTask"]["properties"]

        self.assertEqual(properties["production_run_id"]["format"], "uuid")
        self.assertTrue(properties["production_run_id"]["nullable"])
        self.assertEqual(properties["production_variant_number"]["type"], "integer")
        self.assertTrue(properties["production_variant_number"]["nullable"])

    def test_generation_task_list_contract_marks_unavailable_inputs_explicitly(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        list_response = schema["paths"]["/api/v1/tasks/"]["get"]["responses"]["200"]
        page_ref = list_response["content"]["application/json"]["schema"]["$ref"]
        page = schema["components"]["schemas"][page_ref.rsplit("/", 1)[-1]]
        row_ref = page["properties"]["results"]["items"]["$ref"]
        row = schema["components"]["schemas"][row_ref.rsplit("/", 1)[-1]]
        properties = row["properties"]

        self.assertEqual(row_ref, "#/components/schemas/GenerationTaskList")
        self.assertTrue(properties["input_parameters"]["nullable"])
        status_ref = properties["input_contract_status"]["allOf"][0]["$ref"]
        status_enum = schema["components"]["schemas"][status_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(status_enum["enum"]), {"available", "unavailable"})
        code_ref = properties["input_contract_code"]["oneOf"][0]["$ref"]
        code_enum = schema["components"]["schemas"][code_ref.rsplit("/", 1)[-1]]
        self.assertEqual(
            set(code_enum["enum"]),
            {"legacy_schema_unsupported", "frozen_contract_invalid"},
        )
        self.assertIn("#/components/schemas/BlankEnum", {
            item["$ref"] for item in properties["input_contract_code"]["oneOf"]
        })
