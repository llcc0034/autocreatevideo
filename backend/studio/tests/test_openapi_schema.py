from studio.models import FieldDefinitionVersion
import re

from django.conf import settings
from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator

class OpenApiEnumNamingTests(SimpleTestCase):
    def test_video_catalog_is_separate_from_production_configuration(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        models = schema["components"]["schemas"]
        production = models["ProductionCapability"]
        for key in ("conditioning_modes", "conditioning_contract_sha256"):
            self.assertNotIn(key, production["properties"])
            self.assertNotIn(key, production["required"])
        self.assertEqual(set(models["VideoConditioningCapability"]["required"]),
            {"conditioning_modes", "conditioning_contract_sha256"})
        operation = schema["paths"]["/api/v1/capabilities/video-conditioning/"]["get"]
        self.assertEqual(set(operation["responses"]), {"200", "409"})

    def test_shot_crud_exposes_explicit_roles_without_the_mixed_character_pool(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        models = schema["components"]["schemas"]
        for name in ("Shot", "ShotRequest", "PatchedShotRequest"):
            for field in ("narrative", "detailed_description", "camera", "action", "sound_effects", "music", "continuity_notes"):
                self.assertNotIn(field, models[name]["properties"])
            self.assertNotIn("characters",models[name]["properties"])
            self.assertNotIn("reference_bindings", models[name]["properties"])
            self.assertNotIn("reference_assets", models[name]["properties"])
            self.assertEqual(models[name]["properties"]["character_relations"]["type"],"array")
        for retired in ("ReferenceBindingsSchema", "ReferenceBindingsSchemaRequest",
                "ReferenceEntityBindingSchema", "ReferenceEntityBindingSchemaRequest"):
            self.assertNotIn(retired, models)
        for name in ("ShotCharacterRelationValue", "ShotCharacterRelationValueRequest"):
            self.assertEqual(set(models[name]["required"]),{"role","position","character_id"})
        self.assertEqual(models["ShotCharacterRelationValueRoleEnum"]["enum"],["visible","speaker"])

    def test_single_shot_selection_keeps_original_command_payload_and_separate_read_baseline(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        models = schema["components"]["schemas"]
        query = schema["paths"]["/api/v1/production-runs/{id}/manual-shot-references/"]["get"]
        self.assertEqual({row["name"] for row in query["parameters"] if row.get("required")}, {"id", "shot_id"})
        self.assertEqual(query["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ManualShotReferenceDetail")
        command = models["ProductionManualActionInputRequest"]
        self.assertIn("payload", command["required"])
        self.assertIn("storyboard_review/select", command["properties"]["payload"]["description"])
        action_enum = command["properties"]["action"]["$ref"].split("/")[-1]
        self.assertIn("select", models[action_enum]["enum"])
        self.assertIn("editability", models["ManualShotReferenceDetail"]["required"])

    def test_material_fulfillment_gateway_pins_requirement_and_returns_only_original_selection(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths, models = schema["paths"], schema["components"]["schemas"]
        context = paths["/api/v1/production-runs/{id}/material-fulfillment-context/"]["get"]
        self.assertEqual({row["name"] for row in context["parameters"] if row.get("required")},
            {"id", "stage_run_id", "unit_key"})
        action = paths["/api/v1/production-runs/{id}/fulfill-material/"]["post"]
        self.assertEqual(set(action["responses"]), {"200", "201", "400", "403", "404"})
        body = models["RecipeMaterialFulfillmentInputRequest"]
        self.assertEqual(set(body["required"]), {"stage_run_id", "unit_key", "requirement_key",
            "expected_run_plan_sha256"})
        self.assertEqual(body["properties"]["reference_version_id"]["format"], "uuid")
        self.assertIn("互斥", body["properties"]["reference_version_id"]["description"])
        self.assertEqual(body["properties"]["source"]["$ref"], "#/components/schemas/AssetReferenceInputSourceEnum")
        source_mode = models["RecipeMaterialFulfillmentContext"]["properties"]["source_mode"]["$ref"].split("/")[-1]
        self.assertEqual(models[source_mode]["enum"], ["generate", "existing", "upload"])
        self.assertIn("source_mode", models["RecipeMaterialFulfillmentContext"]["required"])
        self.assertIn("material_source_options", models["ProductionRecipeEditorOptions"]["required"])
        self.assertIn("material_selection_options", models["ProductionRecipeEditorOptions"]["required"])
        result = models["RecipeMaterialFulfillmentReceipt"]["properties"]
        self.assertEqual(result["selected_by_id"]["type"], "integer")
        self.assertTrue(result["selected_by_id"]["nullable"])
        mode = result["selection_mode"]["$ref"].split("/")[-1]
        self.assertEqual(models[mode]["enum"], ["manual", "automatic"])
        self.assertEqual(set(result), {"id", "reference_version_id", "series_id", "asset_id", "source",
            "target_sha256", "selection_mode", "selected_by_id", "created_at"})

    def test_material_selection_context_and_observed_decision_extend_original_selection(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths, models = schema["paths"], schema["components"]["schemas"]
        context = paths["/api/v1/production-runs/{id}/material-selection-context/"]["get"]
        self.assertTrue(next(row for row in context["parameters"] if row["name"] == "artifact_id")["required"])
        select = paths["/api/v1/production-runs/{id}/select-artifact/"]["post"]
        self.assertIn("409", select["responses"])
        key = next(row for row in select["parameters"] if row["name"] == "Idempotency-Key")
        self.assertEqual((key["in"], key["required"]), ("header", True))
        self.assertEqual(select["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductionManualActionResult")
        self.assertIn("material_selection", models["SelectArtifactInputRequest"]["properties"])
        baseline = models["RecipeMaterialSelectionInputRequest"]
        self.assertEqual(baseline["required"], ["expected_decision_id"])
        self.assertTrue(baseline["properties"]["expected_decision_id"]["nullable"])
        self.assertEqual(models["RecipeMaterialDependency"]["properties"]["status"]["$ref"],
            "#/components/schemas/ExecutionPreparationStatusEnum")

    def test_role_route_choices_reuse_appearance_enum_and_expose_separate_stage_fact(self):
        components = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        request = components["SelectRouteInputRequest"]
        self.assertEqual(request["properties"]["appearance_role"]["default"], "primary")
        self.assertEqual(request["properties"]["appearance_role"]["allOf"],
            [{"$ref": "#/components/schemas/AppearanceRoleEnum"}])
        self.assertEqual(set(request["required"]), {"stage_key", "route_index"})
        stage = components["StageRun"]
        for name in ("selected_route_index", "selected_variant_route_index"):
            self.assertTrue(stage["properties"][name]["readOnly"])
            self.assertTrue(stage["properties"][name]["nullable"])
            self.assertIn(name, stage["required"])

    def test_consistency_actions_describe_snapshots_and_lock_lists_not_project_writes(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths, models = schema["paths"], schema["components"]["schemas"]
        continuity = paths["/api/v1/projects/{id}/continuity/"]["get"]
        sync = paths["/api/v1/projects/{id}/sync-locks/"]["post"]
        self.assertNotIn("requestBody", sync)
        self.assertNotIn("requestBody", continuity)
        self.assertEqual([item["name"] for item in sync["parameters"]], ["id"])
        self.assertEqual(continuity["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ConsistencySnapshot")
        response = sync["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(response, {"type": "array", "items": {"$ref": "#/components/schemas/ConsistencyLock"}})
        for endpoint in (continuity, sync):
            self.assertIn("409", endpoint["responses"])
        for kind in ("Character", "Scene", "Prop"):
            fields = models[f"Consistency{kind}Snapshot"]["properties"]
            self.assertNotIn("visual_identity", fields)
            self.assertTrue(fields["primary_setting"]["nullable"])
        self.assertEqual(set(models["ConsistencyPrimarySetting"]["properties"]), {
            "appearance_id", "setting_revision_id", "revision", "content_sha256", "identity_anchor", "appearance_anchor"})

    def test_asset_reference_contract_keeps_associations_separate_from_generated_origins(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths, models = schema["paths"], schema["components"]["schemas"]
        series, versions = "/api/v1/asset-reference-series/", "/api/v1/asset-reference-versions/"
        self.assertEqual(set(paths[series]), {"get", "post"})
        self.assertEqual(set(paths[series + "{id}/"]), {"get", "patch", "delete"})
        self.assertEqual(set(paths[versions + "{id}/"]), {"get", "delete"})
        self.assertEqual(set(paths[series + "{id}/bible-options/"]), {"get"})
        for root in (series, versions):
            self.assertEqual(set(paths[root + "{id}/removal-impact/"]), {"get"})
        self.assertEqual(set(models["AssetReferenceRemovalImpact"]["properties"]),
            {"target_type", "id", "name", "version_count", "asset_count", "variant_count", "dependent_version_count", "production_use_count", "can_remove"})
        query = {item["name"]: item for item in paths[series]["get"]["parameters"]}
        self.assertEqual(set(query["media_type"]["schema"]["enum"]), {"image", "video", "audio"})
        self.assertEqual(query["bible_linked"]["schema"]["type"], "boolean")
        self.assertEqual(query["asset_id"]["schema"]["format"], "uuid")
        self.assertEqual(set(models["AssetReferenceVersionCreateRequest"]["properties"]),
            {"series_id", "asset_id", "source", "setting_revision_id"})
        source_ref = models["AssetReferenceVersionCreateRequest"]["properties"]["source"]["$ref"].split("/")[-1]
        self.assertEqual(set(models[source_ref]["enum"]), {"upload", "existing"})
        series_ref = models["AssetReferenceSeriesCreateRequest"]["properties"]["role"]["$ref"]
        self.assertEqual(series_ref, "#/components/schemas/AppearanceRoleEnum")
        self.assertEqual(set(models["PatchedAssetReferenceSeriesInputRequest"]["properties"]),
            {"name", "primary_series_id", "bible_variant_id"})
        self.assertTrue(next(p for p in paths[versions]["get"]["parameters"] if p["name"] == "series_id")["required"])
        self.assertEqual(set(models["AssetReferenceAssetSummary"]["properties"]),
            {"id", "name", "kind", "mime_type", "is_active"})
        self.assertIn("200", paths[versions]["post"]["responses"])
        self.assertIn("201", paths[versions]["post"]["responses"])

    def test_artifact_selection_reports_automatic_mode_without_human_actor(self):
        components = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        properties = components["ArtifactSelectionDecision"]["properties"]
        self.assertTrue(properties["selected_by"]["nullable"])
        self.assertTrue(properties["selection_mode"]["readOnly"])
        self.assertEqual(set(components["SelectionModeEnum"]["enum"]), {"manual", "automatic"})

    def test_business_output_requires_shared_structure_without_inline_schema(self):
        components = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        self.assertIn("data_structure_version", components["OutputWriteRequest"]["required"])
        self.assertEqual(components["OutputWriteRequest"]["properties"]["data_structure_version"]["format"], "uuid")
        self.assertNotIn("json_schema", components["OutputWriteRequest"]["properties"])
        self.assertNotIn("parser_contract", components["BusinessOutputList"]["properties"])
        self.assertNotIn("nodes", components["BusinessOutputList"]["properties"])
        self.assertTrue(components["BusinessOutput"]["properties"]["data_structure_version"]["readOnly"])

    def test_content_source_requires_structure_and_separates_summary_from_verified_detail(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]
        self.assertIn("data_structure_version", components["SourceWriteRequest"]["required"])
        self.assertEqual(components["SourceWriteRequest"]["properties"]["data_structure_version"]["format"], "uuid")
        self.assertNotIn("reader", components["ContentSourceSummary"]["properties"])
        self.assertNotIn("description", components["ContentSourceSummary"]["properties"])
        self.assertIn("reader", components["ContentSourceVersion"]["properties"])
        self.assertTrue(components["ContentSourceVersion"]["properties"]["data_structure_version"]["readOnly"])
        self.assertNotIn("json_schema", components["SourceWriteRequest"]["properties"])

    def test_structure_contract_is_one_summary_detail_and_read_only_projection(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        root = "/api/v1/data-structure-versions/"
        self.assertEqual(set(schema["paths"][root]), {"get", "post"})
        self.assertEqual(set(schema["paths"][root + "{id}/"]), {"get", "put", "patch"})
        self.assertEqual(set(schema["paths"][root + "{id}/schema-preview/"]), {"get"})
        self.assertEqual(set(schema["paths"][root + "authoring-options/"]), {"get"})
        self.assertEqual(set(schema["paths"][root + "mapping-options/"]), {"get"})
        components = schema["components"]["schemas"]
        self.assertEqual(set(components["StructureMappingAuthoringOptions"]["properties"]), {"schema_version", "nodes", "scopes"})
        authoring = schema["paths"][root + "authoring-options/"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(authoring, {"$ref": "#/components/schemas/DataStructureAuthoringOptions"})
        self.assertEqual(set(components["DataStructureAuthoringOptions"]["properties"]), {"node_defaults", "statuses", "constraint_schemas", "value_schemas"})
        summary = components["DataStructureSummary"]["properties"]
        self.assertNotIn("nodes", summary)
        self.assertNotIn("description", summary)
        request = components["DataStructureVersionRequest"]["properties"]
        self.assertEqual(set(request), {"name", "description", "nodes", "source_version"})
        self.assertEqual(set(components["DataStructurePreview"]["properties"]), {"valid", "json_schema", "errors"})

    def test_field_enum_override_resolves_the_definition_version_model(self):
        from django.utils.module_loading import import_string
        from studio.models import FieldDefinition

        path = settings.SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]["FieldDefinitionValueTypeEnum"]
        self.assertEqual(path, "studio.models.FieldDefinitionVersion.ValueType")
        owner_path, member = path.rsplit(".", 1)
        self.assertEqual(getattr(import_string(owner_path), member), FieldDefinitionVersion.ValueType)
        schemas = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        self.assertEqual(schemas["FieldDefinitionValueTypeEnum"]["enum"], list(FieldDefinitionVersion.ValueType.values))
        self.assertNotIn("value_type", schemas["FieldDefinition"]["properties"])

    def test_bible_variant_contract_separates_summary_detail_revision_and_explicit_review(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        root = "/api/v1/bible-variants/"
        self.assertEqual(set(schema["paths"][root]), {"get", "post"})
        self.assertEqual(set(schema["paths"][root + "{id}/"]), {"get"})
        for action in ("review", "revise"):
            path = schema["paths"][root + "{id}/" + action + "/"]
            self.assertEqual(set(path), {"post"})
            self.assertTrue({"200", "400", "404", "409"} <= set(path["post"]["responses"]))
        self.assertEqual(set(schema["paths"][root + "{id}/revisions/"]), {"get"})
        source = schema["paths"][root + "{id}/source/"]
        self.assertEqual(set(source), {"get"})
        self.assertTrue({"200", "400", "404", "409"} <= set(source["get"]["responses"]))
        revision = next(row for row in source["get"]["parameters"] if row["name"] == "revision")
        self.assertTrue(revision["required"])
        self.assertEqual(revision["schema"]["minimum"], 1)
        components = schema["components"]["schemas"]
        self.assertNotIn("appearance_anchor", components["BibleVariantSummary"]["properties"])
        self.assertIn("setting", components["BibleVariantDetail"]["properties"])
        fields = components["BibleVariantReviewInputRequest"]["properties"]
        self.assertEqual(set(fields), {"expected_revision", "expected_sha256", "decision", "comment"})
        revise_fields = components["BibleVariantReviseInputRequest"]["properties"]
        self.assertNotIn("minLength", revise_fields["appearance_anchor"])
        self.assertNotIn("primary_setting_revision_id", revise_fields)
        self.assertNotIn("source_binding_id", revise_fields)

    def test_bulk_field_review_has_explicit_preview_and_receipt_contracts(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        for action, request, response in (("preview", "ExecutionBulkFieldPreviewRequest", "ExecutionBulkFieldPreview"),
                                         ("confirm", "ExecutionBulkFieldConfirmRequest", "ExecutionBulkFieldResult")):
            path = schema["paths"][f"/api/v1/execution-preparations/bulk-field-{action}/"]
            self.assertEqual(set(path), {"post"})
            operation = path["post"]
            self.assertTrue({"200", "400", "404", "409"} <= set(operation["responses"]))
            self.assertIn(request, str(operation["requestBody"]))
            self.assertIn(response, str(operation["responses"]["200"]))
        props = schema["components"]["schemas"]["ExecutionBulkFieldReceiptRequest"]["properties"]
        self.assertEqual(set(props), {"preparation_id", "expected_preparation_revision", "preview_sha256"})

    def test_production_budget_options_are_read_only_with_typed_currency_choices(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertEqual(set(schema["paths"]["/api/v1/production-runs/budget-options/"]), {"get"})
        self.assertIn("currencies", schema["components"]["schemas"]["ProductionBudgetOptions"]["properties"])
        self.assertEqual(set(schema["components"]["schemas"]["ProductionBudgetCurrencyOption"]["properties"]), {"value", "label"})

    def test_project_creation_capability_is_read_only_and_shares_defaults_schema(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        path = schema["paths"]["/api/v1/capabilities/project-creation/"]
        self.assertEqual(set(path), {"get"})
        self.assertIn("409", path["get"]["responses"])
        props = schema["components"]["schemas"]["ProjectCreationCapability"]["properties"]
        self.assertEqual(set(props), {"creation_defaults", "configuration_public_sha256"})
        self.assertEqual(props["creation_defaults"]["$ref"], "#/components/schemas/ProductionCreationDefaults")

    def test_current_registry_and_recommendation_contracts_have_no_retired_composition(self):
        from studio.platform_serializers import RegistryDeletionPreviewInputSerializer
        from studio.models import RecommendationDecision
        choices = set(RegistryDeletionPreviewInputSerializer().fields["resource_type"].choices)
        self.assertTrue({"model_execution_instance", "model_execution_instance_version"} <= choices)
        self.assertNotIn("prompt_profile_binding", choices)
        self.assertNotIn("recipe_version", {field.name for field in RecommendationDecision._meta.fields})
        schemas = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        decision = schemas["RecommendationDecision"]["properties"]
        self.assertNotIn("recipe_version", decision)
        self.assertTrue({"candidate", "config_revision", "decision", "override_values"} <= set(decision))

    def test_manual_review_only_state_is_part_of_the_single_api_contract(self):
        from studio.production_manual_serializers import ManualContentReplacementSerializer
        for code in ("review_only", "review_and_stopped", "media_review_required", "text_review_required"):
            serializer = ManualContentReplacementSerializer(data={"allowed": True, "code": code, "reason": "逐项重新审核"})
            self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertFalse(ManualContentReplacementSerializer(data={"allowed": True, "code": "storyboard_review_only", "reason": "旧别名"}).is_valid())
        schemas = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        ref = schemas["ManualContentReplacement"]["properties"]["code"]["$ref"].split("/")[-1]
        self.assertTrue({"review_only", "review_and_stopped", "media_review_required", "text_review_required"}.issubset(schemas[ref]["enum"]))
        self.assertNotIn("storyboard_review_only", schemas[ref]["enum"])

    def test_assembly_reports_frozen_media_count_not_legacy_tasks(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        fields = schema["components"]["schemas"]["AssemblyJob"]["properties"]
        self.assertNotIn("source_tasks", fields)
        self.assertEqual(fields["source_media_count"]["type"], "integer")
        self.assertTrue(fields["source_media_count"]["readOnly"])
        self.assertTrue(fields["ai_generated"]["nullable"])
        release = schema["components"]["schemas"]["ReleaseBundle"]
        self.assertIn("production_run", release["required"])
        self.assertFalse(release["properties"]["production_run"].get("nullable", False))

    def test_manual_bible_image_read_keeps_candidates_targets_and_review_distinct(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertEqual(set(schema["paths"]["/api/v1/production-runs/{id}/manual-bible-image-review/"]), {"get"})
        fields = schema["components"]["schemas"]["ManualBibleImageReview"]["properties"]
        self.assertEqual(set(fields), {"run_id", "run_plan_sha256", "candidates", "candidates_sha256", "targets",
            "targets_sha256", "content_basis", "content_basis_sha256", "maximum_selected_images", "approval"})
        self.assertEqual(set(schema["components"]["schemas"]["ManualBibleImageContentBasis"]["properties"]),
            {"script_sha256", "bible_sha256", "visual_rules_sha256", "upstream_reviews_sha256"})
        self.assertIn("changes", schema["components"]["schemas"]["ManualBibleImageDecision"]["properties"])

    def test_manual_storyboard_read_is_not_a_model_or_mutation_endpoint(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        path = schema["paths"]["/api/v1/production-runs/{id}/manual-storyboard/"]
        self.assertEqual(set(path), {"get"})
        fields = schema["components"]["schemas"]["ManualStoryboardDetail"]["properties"]
        self.assertEqual(set(fields), {"run_id", "run_plan_sha256", "content", "content_sha256", "adoption", "materialization"})
        proposed = schema["components"]["schemas"]["ManualStoryboardMaterialization"]["properties"]
        self.assertEqual(set(proposed), {"ready", "reason", "contract", "sha256"})

    def test_manual_bible_read_is_typed_and_cannot_materialize(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        path = schema["paths"]["/api/v1/production-runs/{id}/bible-review/"]
        self.assertEqual(set(path), {"get"})
        self.assertNotIn("/api/v1/production-runs/{id}/manual-bible-review/", schema["paths"])
        fields = schema["components"]["schemas"]["ManualBibleReview"]["properties"]
        self.assertEqual(set(fields), {"plan_source", "run_id", "run_plan_sha256", "current_script", "source",
            "project_bible", "project_bible_sha256", "candidates", "settings", "approval"})
        recipe = schema["components"]["schemas"]["RecipeBibleReview"]["properties"]
        self.assertIn("source_snapshot_sha256", recipe)
        self.assertIn("settings", recipe)
        approval = schema["components"]["schemas"]["ApprovalDecisionInputRequest"]["properties"]
        confirmation_ref = approval["bible_review"]["allOf"][0]["$ref"].split("/")[-1]
        confirmation_schema = schema["components"]["schemas"][confirmation_ref]
        confirmation = confirmation_schema["properties"]
        self.assertEqual(set(confirmation), {"expected_source_snapshot_sha256", "expected_project_bible_sha256",
            "reviewed_candidate_ids", "reviewed_setting_keys", "reviewed_project_bible"})
        self.assertEqual(set(confirmation_schema["required"]), set(confirmation))
        source = schema["components"]["schemas"]["ManualBibleSource"]["properties"]
        self.assertTrue({"revision", "decision_id", "snapshot_sha256", "candidate_sha256"} <= set(source))

    def test_manual_reference_review_is_readonly_and_distinct_from_instance_execution(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        path = schema["paths"]["/api/v1/production-runs/{id}/manual-storyboard-review/"]
        self.assertEqual(set(path), {"get"})
        fields = schema["components"]["schemas"]["ManualStoryboardReview"]["properties"]
        self.assertEqual(set(fields), {"run_id", "run_plan_sha256", "ready", "reason", "contract",
            "contract_sha256", "references", "source_options", "approval", "display"})
        self.assertTrue(fields["contract"]["nullable"])
        self.assertEqual(fields["references"]["type"], "array")
        self.assertIn("ShotReferenceUse", fields["references"]["description"])
        self.assertEqual(fields["source_options"]["type"], "array")
        self.assertIn("reference_source_options", fields["source_options"]["description"])

    def test_task_observations_are_paginated_typed_readonly_checks(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        path = schema["paths"]["/api/v1/tasks/{id}/observations/"]
        self.assertEqual(set(path), {"get"})
        response = path["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertIn("PaginatedGenerationTaskObservation", str(response))
        fields = schema["components"]["schemas"]["GenerationTaskObservation"]["properties"]
        self.assertTrue({"claim_number", "request_sha256", "status", "node_count", "policy_sha256", "http_status"}.issubset(fields))
        self.assertTrue(all(field.get("readOnly") for field in fields.values()))
        self.assertFalse({"capability_snapshot", "payload", "prepared_request"} & fields.keys())

    def test_execution_field_api_separates_explicit_actions_from_dispatch(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        base = "/api/v1/execution-field-values/"
        self.assertEqual(set(paths[base]), {"get", "post"})
        self.assertEqual(set(paths[base + "{id}/"]), {"get"})
        for action in ("confirm", "reject"):
            self.assertEqual(set(paths[base + "{id}/" + action + "/"]), {"post"})
        self.assertEqual(set(paths["/api/v1/execution-preparations/{id}/"]), {"get"})
        components = schema["components"]["schemas"]
        self.assertNotIn("text", components["ExecutionFieldSummary"]["properties"])
        self.assertIn("text", components["ExecutionFieldDetail"]["properties"])
        create = components["ExecutionFieldCreateRequest"]
        self.assertNotIn("origin_kind", create["properties"])
        self.assertNotIn("approval_kind", create["properties"])
        confirm = components["ExecutionFieldConfirmRequest"]
        self.assertFalse(confirm["properties"]["select_for_execution"]["default"])
        self.assertTrue({"expected_text_sha256", "expected_source_sha256", "expected_preparation_revision"}.issubset(confirm["required"]))

    def test_execution_registry_is_typed_and_keeps_prompt_purpose_readonly(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        base = "/api/v1/model-execution-instance-versions/"
        self.assertEqual(set(paths[base]), {"get", "post"})
        self.assertEqual(set(paths[base + "{id}/"]), {"get", "patch"})
        self.assertIn(base + "{id}/clone-successor/", paths)
        self.assertEqual(set(paths[base + "{id}/input-contract/"]), {"get"})
        self.assertEqual(set(paths[base + "{id}/authoring-options/"]), {"get"})
        for action in ("publish", "retire"):
            self.assertEqual(set(paths[base + "{id}/" + action + "/"]), {"post"})
        components = schema["components"]["schemas"]
        create = components["ExecutionVersionCreateRequest"]
        self.assertTrue({"instance_id", "expected_revision", "deployment_id", "operation", "mode", "prompt_mode",
                         "prompt_profile_version_id", "workflow_version_id"}.issubset(create["required"]))
        self.assertNotIn("prompt_operation", create["properties"])
        self.assertTrue(components["ExecutionVersion"]["properties"]["prompt_operation"]["readOnly"])
        self.assertNotIn("configuration", components["ExecutionVersion"]["properties"])
        self.assertIn("configuration", components["ExecutionVersionDetail"]["properties"])
        self.assertIn("seed_policy", components["ExecutionParameterSpecRequest"]["required"])
        self.assertEqual(components["ExecutionSeedPolicyEnum"]["enum"], ["fixed", "generate_once"])
        self.assertEqual(components["ExecutionSubjectKindEnum"]["enum"], ["character", "scene", "prop", "external"])
        self.assertEqual(components["ExecutionSubjectCategoryEnum"]["enum"], ["character", "scene", "prop"])
        subject = components["ExecutionSubjectFactRequest"]
        self.assertEqual(set(subject["required"]), {"id", "category", "name", "description"})
        self.assertIn("ExecutionSubjectCategoryEnum", str(subject["properties"]["category"]))
        self.assertEqual(components["ExecutionSmokeFixtureRequest"]["properties"]["subject_facts"]["items"]["$ref"],
                         "#/components/schemas/ExecutionSubjectFactRequest")
        for name in ("ExecutionInputContract", "ResourceSmokeContract"):
            self.assertEqual(components[name]["properties"]["subject_categories"]["items"]["$ref"],
                             "#/components/schemas/ExecutionSubjectCategoryOption")
        self.assertEqual(components["ExecutionScopeKindEnum"]["enum"], ["request", "shot", "episode", "project"])
        semantics = components["ExecutionInputSemanticRequest"]["properties"]
        self.assertEqual(semantics["allowed_subject_kinds"]["items"]["$ref"], "#/components/schemas/ExecutionSubjectKindEnum")
        self.assertEqual(semantics["allowed_scope_kinds"]["items"]["$ref"], "#/components/schemas/ExecutionScopeKindEnum")
        self.assertEqual(components["ExecutionVersionStatusEnum"]["enum"], ["draft", "published", "retired"])
        self.assertTrue({"expected_revision", "expected_definition_sha256"}.issubset(components["ExecutionCloneRequest"]["required"]))
        self.assertEqual(set(components["ExecutionPublishRequest"]["required"]), {"expected_revision", "expected_definition_sha256"})
        self.assertEqual(set(components["ExecutionRetireRequest"]["required"]), {"expected_revision", "expected_definition_sha256", "reason"})
        detail = components["ExecutionInputContract"]["properties"]
        self.assertTrue({"version_id", "revision", "frozen", "schema_version", "size_bytes", "sha256", "target_fingerprint", "definition"} <= detail.keys())
        self.assertTrue(detail["definition"]["readOnly"])
        self.assertNotIn("can_submit", detail)
        self.assertNotIn("definition", components["ExecutionAuthoringOptions"]["properties"])

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

    def test_reviewed_visual_sources_include_the_original_producer_review_contract(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        component = schema['components']['schemas']['ReviewedVisualSources']
        self.assertIn('review_contract', component['required'])
        self.assertIn('原生产请求冻结', component['properties']['review_contract']['description'])
        self.assertIn('/api/v1/production-runs/{id}/reviewed-visual-sources/', schema['paths'])

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
                "active_translation_jobs",
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

    def test_translation_review_has_no_shot_writeback_contract(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertNotIn("/api/v1/translations/promote-shot/", schema["paths"])
        components = schema["components"]["schemas"]
        self.assertNotIn("promoted_fields", components["PromptFieldTranslation"]["properties"])
        self.assertFalse(any(name.startswith("PromptTranslationPromot") for name in components))

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
        for resource in ("prompts", "workflows", "deferred-generation-requests"):
            self.assertFalse(any(path.startswith(f"/api/v1/{resource}/") for path in paths))
        for name in ("PromptArtifact", "GeneratedWorkflow", "DeferredGenerationRequest"):
            self.assertNotIn(name, schema["components"]["schemas"])
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
            "project_id", "plan_source", "idempotency_key",
        }.issubset(request["required"]))
        self.assertTrue({
            "allow_external_calls",
            "config_overrides", "recipe_version_id", "manual_plan",
        }.issubset(request["properties"]))
        self.assertNotIn("recipe_version_id", request["required"])
        self.assertTrue({"workflow_selection", "bible_image_prompt_profile_binding_id", "reference_inputs", "budget_limit", "max_generation_tasks"}.isdisjoint(request["properties"]))
        manual_ref = request["properties"]["manual_plan"]["$ref"]
        manual = schema["components"]["schemas"][manual_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(manual["required"]), {"stage_catalog_key", "expected_stage_catalog_sha256", "scope", "delivery", "initial_content"})
        targets = paths["/api/v1/production-runs/{id}/targets/"]
        self.assertEqual(set(targets), {"get"})
        self.assertEqual({row["name"] for row in targets["get"]["parameters"]}, {"id", "kind", "page", "page_size"})
        target_ref = targets["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        target_page = schema["components"]["schemas"][target_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(target_page["required"]), {"count", "next", "previous", "results", "run_id", "run_plan_sha256"})
        self.assertEqual(set(schema["components"]["schemas"]["ManualRegisteredAction"]["required"]),
            {"stage_key", "action", "operation", "output_kinds", "scope_kinds"})
        self.assertNotIn("/api/v1/production-recipes/{id}/media-route-options/", paths)

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
        self.assertNotIn("adopt_generated_metadata", approve_request["properties"])
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
            {"200", "202", "400", "403", "404", "409"},
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
        preview_path = paths["/api/v1/business-output-versions/preview-visual-rules-output/"]
        self.assertEqual(set(preview_path), {"post"})
        self.assertEqual(set(preview_path["post"]["responses"]), {"200", "400", "403"})
        self.assertEqual(set(components["VisualRulesOutputPreviewInputRequest"]["properties"]),
            {"response_text", "quality_advisories", "allowed_values", "field_sources", "response_keys",
             "output_version_id", "expected_revision"})
        output_layout = components["OutputWriteRequest"]["properties"]["response_keys"]
        self.assertEqual(output_layout["additionalProperties"]["type"], "string")
        self.assertIn("最多4层", output_layout["description"])
        self.assertIn("从0连续完整声明的整数下标", output_layout["description"])
        self.assertIn("preview_only", components["VisualRulesOutputPreviewResponse"]["properties"])
        self.assertIn("response_example", components["VisualRulesOutputEditorOptions"]["properties"])
        self.assertTrue({
            "specification_schema_version", "provider_drivers", "modalities",
            "artifact_roles", "capabilities", "model_families",
            "media_constraints", "execution_monitoring", "visual_rules_output",
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
        selection_ref = smoke_request["properties"]["selection"]["$ref"]
        selection = schema["components"]["schemas"][selection_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(selection["properties"]), {"operation", "mode", "prompt_mode", "prompt_profile_version_id", "output_version_id"})
        self.assertEqual(selection["properties"]["operation"]["type"], "string")
        self.assertEqual(selection["properties"]["operation"]["maxLength"], 100)
        self.assertNotIn("enum", selection["properties"]["operation"])
        self.assertEqual(set(paths["/api/v1/model-deployments/{id}/smoke-options/"]), {"get"})

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
            "preparation", "parent_preparation", "role", "execution_instance_version",
            "request_snapshot_sha256", "request_snapshot_size_bytes",
        }.issubset(generation_attempt))
        self.assertTrue({"repair_of_attempt_id", "retry_feedback_sha256"}.isdisjoint(generation_attempt))

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
        fields = {"selection", "expected_definition_sha256", "fixture", "field_values", "allow_external_calls"}
        self.assertEqual(set(request["required"]), fields)
        self.assertEqual(set(request["properties"]), fields)
        selection_ref = request["properties"]["selection"]["$ref"]
        selection = components[selection_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(selection["properties"]), {"operation", "mode", "prompt_mode", "prompt_profile_version_id", "output_version_id"})
        self.assertEqual(set(selection["required"]), {"operation", "mode", "prompt_mode", "prompt_profile_version_id"})
        self.assertEqual(selection["properties"]["output_version_id"]["format"], "uuid")
        self.assertTrue(selection["properties"]["output_version_id"]["nullable"])
        self.assertTrue(any(row["name"] == "Idempotency-Key" and row["in"] == "header" and row["required"]
            for row in operation["parameters"]))

        fixture_ref = request["properties"]["fixture"]["$ref"]
        fixture = components[fixture_ref.rsplit("/", 1)[-1]]
        self.assertEqual(
            set(fixture["properties"]),
            {"inputs", "parameters", "manual_channels", "subject_facts", "source_language", "target_language"},
        )
        self.assertEqual(set(fixture["required"]), set(fixture["properties"]))

        response_ref = operation["responses"]["202"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        created = components[response_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(created["properties"]), {"attempt_kind", "attempt_id", "status", "preparation"})
        self.assertEqual(set(created["required"]), set(created["properties"]))
        self.assertEqual(operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"], response_ref)
        attempt_ref = schema["paths"]["/api/v1/workflow-smoke-attempts/{id}/"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        attempt = components[attempt_ref.rsplit("/", 1)[-1]]
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
            "/api/v1/field-definitions/",
            "/api/v1/prompt-profile-versions/",
            "/api/v1/prompt-profile-field-rules/",
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
        field_definition = components["FieldDefinition"]["properties"]
        self.assertNotIn("preview_example", field_definition)
        definition_version = components["FieldDefinitionVersion"]["properties"]
        self.assertIn("preview_example", definition_version)
        self.assertIs(definition_version["preview_example"]["readOnly"], True)
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
            "execution_instance_version", "preparation", "prompt_profile_version",
            "prompt_profile_contract_sha256", "role",
        }.issubset(generation_attempt))
        self.assertNotIn("PromptArtifact", components)
        self.assertNotIn("/api/v1/prompts/", paths)
        self.assertIn("/api/v1/tasks/{id}/input-prompt/", paths)
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
            "/api/v1/field-definitions/{id}/",
            "/api/v1/prompt-profile-versions/{id}/",
            "/api/v1/prompt-profile-field-rules/{id}/",
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

        assembly_schema = components["ProductionRecipeEditorOptions"]["properties"]["assembly_output_schema"]
        self.assertEqual(assembly_schema["type"], "object")
        self.assertTrue(assembly_schema["readOnly"])
        timing_schema = components["ProductionRecipeEditorOptions"]["properties"]["assembly_timing_schema"]
        self.assertEqual(timing_schema["type"], "object")
        self.assertTrue(timing_schema["readOnly"])
        media_schema = components["ProductionRecipeEditorOptions"]["properties"]["assembly_media_schema"]
        self.assertEqual(media_schema["type"], "object")
        self.assertTrue(media_schema["readOnly"])
        delivery_schema = components["ProductionRecipeEditorOptions"]["properties"]["delivery_metadata_schema"]
        self.assertEqual(delivery_schema["type"], "object")
        self.assertTrue(delivery_schema["readOnly"])
        for component in ("ProductionRecipeVersionRequest", "PatchedProductionRecipeVersionRequest"):
            description = components[component]["properties"]["definition"]["description"]
            self.assertIn("assembly_output", description)
            self.assertIn("assembly_timing", description)
            self.assertIn("assembly_media", description)
            self.assertIn("delivery_metadata", description)
            self.assertIn("episode_start_ms", description)
            self.assertIn("expected_contract_sha256", description)

        option_paths = {
            "/api/v1/production-recipes/options/": "ProductionRecipeEditorOptions",
            "/api/v1/prompt-profile-versions/options/": "PromptProfileOptions",
            "/api/v1/field-definitions/options/": "FieldDefinitionOptions",
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
            "FieldDefinitionRequest",
            "PatchedFieldDefinitionRequest",
        ):
            self.assertNotIn("display_order", components[component]["properties"])
        create_contracts = (
            (
                "/api/v1/field-definitions/",
                "FieldDefinitionCreateInputRequest",
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

    def test_script_bible_preview_apply_and_sync_contracts_are_absent(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        self.assertNotIn("/api/v1/projects/{id}/script-bible-candidates/", paths)
        self.assertNotIn(
            "/api/v1/projects/{id}/script-bible-candidates/apply/",
            paths,
        )
        self.assertNotIn("/api/v1/projects/{id}/sync-bibles/", paths)
        for name in (
            "ScriptBibleEvidence", "ScriptBibleDiagnostic", "ScriptBibleCandidate",
            "ScriptBibleCandidatesResponse", "ScriptBibleReviewedSelection",
            "ScriptBibleErrorDetails", "ScriptBibleError", "ScriptBibleErrorEnvelope",
        ):
            self.assertNotIn(name, schema["components"]["schemas"])

    def test_bible_image_candidates_are_read_only_and_task_promotion_is_absent(self):
        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        candidate_path = "/api/v1/projects/{id}/bible-image-candidates/"
        self.assertEqual(set(paths[candidate_path]), {"get"})
        self.assertNotIn("/api/v1/tasks/{id}/promote-output-to-asset/", paths)

    def test_bible_and_storyboard_approval_requests_have_exact_machine_contracts(self):
        schemas = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]

        approval = schemas["ApprovalDecisionInputRequest"]
        self.assertIn("bible_image_bindings", approval["properties"])
        self.assertNotIn("shot_reference_bindings", approval["properties"])
        self.assertIn("expected_selection_sha256", approval["properties"])
        self.assertIn("expected_source_snapshot_sha256", approval["properties"])
        self.assertEqual(
            approval["properties"]["bible_image_bindings"]["items"]["$ref"],
            "#/components/schemas/BibleImageBindingSelectionRequest",
        )
        self.assertEqual(approval["properties"]["expected_selection_sha256"]["pattern"], "^[0-9a-f]{64}$")

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

        self.assertNotIn("StoryboardShotReferenceSelectionRequest", schemas)
        self.assertEqual(set(paths["/api/v1/production-runs/{id}/storyboard-reference-selection/"]), {"get"})
        review = schemas["RecipeStoryboardReferenceSelection"]
        self.assertEqual(set(review["required"]), {"source_snapshot_sha256", "selection_sha256", "shot_count", "selection", "display"})

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
                "preparation_id",
                "execution_instance_version_id",
                "execution_instance_name",
                "request_sha256",
                "workflow_version_id",
                "model_profile_version_id",
                "prompt_profile_version_id",
                "target_snapshot_sha256",
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
            "prompt_artifact_id", "prompt_profile_binding_key", "prompt_profile_contract_sha256", "source_snapshot_sha256",
            "task_status",
            "promoted_assets",
            "prompt_inputs",
        ):
            self.assertNotIn(retired_field, history["properties"])

    def test_deferred_generation_has_no_historical_read_or_write_contract(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertFalse(any("deferred-generation" in path for path in schema["paths"]))
        self.assertFalse(any("DeferredGeneration" in name for name in schema["components"]["schemas"]))

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
        self.assertIn("manual_media_candidate", components["GeneratedArtifactDetail"]["properties"])
        self.assertNotIn("manual_media_candidate", components["GeneratedArtifactList"]["properties"])
        media = components["ManualMediaCandidate"]["properties"]
        self.assertEqual(media["selection_revision"]["minimum"], 0)
        self.assertTrue(media["selection_decision_id"]["nullable"])
        self.assertIn("scope", media)
        self.assertIn("run_plan_sha256", media)

    def test_generation_task_contract_exposes_safe_production_group_identity(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        properties = schema["components"]["schemas"]["GenerationTask"]["properties"]

        self.assertTrue(properties["execution"]["nullable"])
        identity = schema["components"]["schemas"]["GenerationTaskExecution"]["properties"]
        self.assertEqual(identity["production_run_id"]["format"], "uuid")
        self.assertEqual(identity["stage_run_id"]["format"], "uuid")
        self.assertEqual(identity["attempt_number"]["type"], "integer")
        self.assertEqual(identity["unit_key"]["type"], "string")
        for retired in ("production_run_id", "production_variant_number", "batch_slots", "batch_id", "shot_id",
                "generated_workflow", "seed", "effective_frames", "max_attempts", "staged_inputs", "outputs", "bible_target"):
            self.assertNotIn(retired, properties)
        self.assertNotIn("occurred_at", schema["components"]["schemas"]["GenerationTaskTimelineEvent"]["properties"])
        filters = {item["name"] for item in schema["paths"]["/api/v1/tasks/"]["get"]["parameters"]}
        self.assertTrue({"production_run_id", "stage_run_id", "unit_key", "preparation_id", "execution_instance_version_id"} <= filters)
        self.assertFalse({"generated_workflow", "batch_id", "shot_id"} & filters)

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
            {"frozen_contract_invalid"},
        )
        self.assertIn("#/components/schemas/BlankEnum", {
            item["$ref"] for item in properties["input_contract_code"]["oneOf"]
        })

    def test_manual_media_review_exposes_exact_members_and_original_approval(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        operation = schema["paths"]["/api/v1/production-runs/{id}/manual-media-review/"]["get"]
        self.assertIn("200", operation["responses"])
        schemas = schema["components"]["schemas"]
        self.assertEqual(set(schemas["ManualMediaReview"]["properties"]),
            {"run_id", "run_plan_sha256", "selection", "selection_sha256", "approval"})
        self.assertEqual(set(schemas["ManualMediaSelectionMember"]["properties"]),
            {"artifact_id", "kind", "artifact_sha256", "manifest_sha256", "decision_id", "decision_revision", "variant_group", "scope", "unit_key"})
        self.assertTrue(schemas["ManualMediaApproval"]["properties"]["decision_id"]["nullable"])
        self.assertEqual(set(schemas["ManualMediaSelection"]["properties"]),
            {"schema_version", "run_plan_sha256", "source_stage_id", "items", "delivery_timeline", "content_basis"})
        self.assertEqual(set(schemas["ManualMediaContentBasis"]["properties"]),
            {"script_sha256", "storyboard_sha256", "bible_sha256", "visual_rules_sha256", "upstream_reviews_sha256"})
        self.assertEqual(schemas["ManualMediaApproval"]["properties"]["changes"]["type"], "array")
        self.assertEqual(set(schemas["ManualReviewTimelineShot"]["properties"]),
            {"shot_id", "number", "title", "duration_ms", "media_kind", "episode_start_ms", "episode_end_ms"})

    def test_generation_task_inputs_and_prompt_use_preparation_contract_only(self):
        schemas = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        inputs = schemas["GenerationTaskInputParameters"]["properties"]
        self.assertEqual(set(inputs), {"preparation_id", "attempt_id", "execution_instance_version_id", "workflow_version_id",
            "request_sha256", "operation", "output_kind", "parameters", "media_uses", "prompt_mode",
            "prompt_profile_version_id", "prompt_channels"})
        media = schemas["GenerationTaskMediaUse"]["properties"]
        self.assertTrue({"item_key", "artifact_id", "asset_id", "ports", "staging_status", "paired_to"} <= set(media))
        self.assertNotIn("local_path", media)
        prompt = schemas["GenerationTaskInputPrompt"]["properties"]
        self.assertEqual(set(prompt), {"task_id", "attempt_id", "preparation_id", "execution_instance_version_id",
            "request_sha256", "prompt_mode", "prompt_profile_version_id", "channels", "declared_reference_labels"})

    def test_task_center_describes_original_system_layout_without_delivery_claim(self):
        schemas = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        self.assertIn("submission", schemas["TaskCenterDetail"]["required"])
        fields = schemas["TaskSubmissionEvidence"]["properties"]
        self.assertEqual(set(fields), {"state", "started_at", "registered_at", "identifier_state", "message", "notes"})
        self.assertTrue(fields["started_at"]["nullable"])
        self.assertTrue(fields["registered_at"]["nullable"])
        self.assertIn("人工核对", fields["registered_at"]["description"])
        self.assertIn("不返回标识", fields["identifier_state"]["description"])
        description = schemas["TaskCenterDetail"]["properties"]["input"]["description"]
        for value in ("system_message_trace", "system_message_layout", "字段生成/翻译", "Unicode", "start/end", "无记录不反推历史", "不证明送达", "port_routes", "edge_indexes", "最短路径", "processing_summary", "byte_identity", "shared_with", "不重新探测"):
            self.assertIn(value, description)

    def test_manual_assembly_is_readonly_inventory_with_real_job_and_contract(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        operation = schema["paths"]["/api/v1/production-runs/{id}/manual-assembly/"]
        self.assertEqual(set(operation), {"get"})
        props = schema["components"]["schemas"]["ManualAssemblyDetail"]["properties"]
        self.assertEqual(set(props), {"run_id", "run_plan_sha256", "contract_sha256", "frozen", "blockers", "approval", "episodes", "output"})
        self.assertIn("job", schema["components"]["schemas"]["ManualAssemblyEpisode"]["properties"])
