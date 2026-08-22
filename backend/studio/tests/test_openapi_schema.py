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

    def test_creative_generate_documents_safe_dialogue_capacity_context(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        response = schema["paths"][
            "/api/v1/projects/{project_id}/creative-drafts/generate/"
        ]["post"]["responses"]["400"]
        self.assertEqual(
            response["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/CreativeDraftValidationErrorResponse",
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
        details = schema["components"]["schemas"][
            "CreativeDraftValidationErrorDetails"
        ]["properties"]
        self.assertEqual(
            set(details),
            {
                "code", "detail", "errors", "required_dialogue_count",
                "target_shot_count", "dialogue_capacity",
                "recommended_max_shots", "batch_shots",
                "source_char_count", "max_source_chars",
                "max_dialogue_per_shot", "effective_max_dialogue_per_shot",
                "episode_title_chars", "max_episode_title_chars",
                "dialogue_text_chars", "max_dialogue_text_chars",
                "dialogue_delivery_chars", "max_dialogue_delivery_chars",
                "required_output_tokens",
                "configured_max_output_tokens",
            },
        )

    def test_creative_requests_document_confirmation_without_a_fixed_shot_maximum(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]

        for name in ("CreativeDraftGenerateRequest", "CreativeDraftRegenerateRequest"):
            properties = components[name]["properties"]
            self.assertEqual(
                properties["extended_generation_confirmed"],
                {"type": "boolean", "default": False},
            )
            self.assertEqual(properties["target_shot_count"]["minimum"], 1)
            self.assertNotIn("maximum", properties["target_shot_count"])

    def test_creative_capability_documents_provider_policy_as_the_only_limit_source(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        response = schema["paths"][
            "/api/v1/capabilities/creative-drafts/"
        ]["get"]["responses"]["200"]
        self.assertEqual(
            response["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/CreativeDraftCapability",
        )

        components = schema["components"]["schemas"]
        capability = components["CreativeDraftCapability"]["properties"]
        self.assertEqual(
            set(capability),
            {
                "creative_generation_supported", "review_required",
                "default_provider", "providers",
                "series_concept_draft_preferred_provider",
                "series_concept_draft_supported",
                "series_concept_draft_schema_version",
                "series_concept_draft_limits",
                "series_concept_draft_review_required",
                "series_concept_draft_persisted",
                "shot_high_detail_supported", "shot_high_detail_schema_version",
                "shot_high_detail_policies",
                "shot_high_detail_shared_resource_busy",
                "shot_high_detail_review_required",
            },
        )
        self.assertEqual(
            capability["providers"]["items"]["$ref"],
            "#/components/schemas/CreativeDraftProviderCapability",
        )

        provider = components["CreativeDraftProviderCapability"]["properties"]
        self.assertTrue({
            "min_shots", "recommended_max_shots", "batch_shots",
            "max_dialogue_per_shot", "max_episode_title_chars",
            "max_dialogue_text_chars", "max_dialogue_delivery_chars",
            "max_dialogue_delivery_en_chars",
            "recommended_title_zh_chars", "recommended_title_en_chars",
            "recommended_narrative_zh_chars", "recommended_narrative_en_chars",
            "recommended_camera_zh_chars", "recommended_camera_en_chars",
            "recommended_action_zh_chars", "recommended_action_en_chars",
            "recommended_continuity_zh_chars", "recommended_continuity_en_chars",
            "recommended_soundscape_zh_chars", "recommended_soundscape_en_chars",
            "recommended_music_zh_chars", "recommended_music_en_chars",
            "max_camera_movements", "max_action_sequence_markers",
            "min_script_action_coverage_percent", "min_shot_duration_ms",
            "max_shot_duration_ms", "output_min_tokens",
            "output_base_tokens", "output_tokens_per_shot",
            "output_tokens_per_dialogue", "output_dialogue_char_threshold",
            "output_headroom_per_shot", "max_source_chars",
            "max_regeneration_context_chars", "max_input_chars",
            "baseline_max_new_tokens", "max_new_tokens", "max_output_tokens",
            "configured_max_new_tokens", "runtime_timeout_seconds",
            "min_context_size", "max_context_size", "reserved_input_tokens",
            "series_concept_draft_suitability",
            "series_concept_draft_suitability_reason_code",
            "series_concept_draft_suitability_notice",
            "series_concept_draft_timeout_seconds",
            "series_concept_draft_max_output_tokens",
        } <= set(provider))
        self.assertIn("temporarily_busy", provider)
        suitability_ref = provider["series_concept_draft_suitability"]["$ref"]
        self.assertEqual(
            components[suitability_ref.rsplit("/", 1)[-1]]["enum"],
            ["recommended", "experimental"],
        )
        reason_ref = provider[
            "series_concept_draft_suitability_reason_code"
        ]["$ref"]
        self.assertEqual(
            components[reason_ref.rsplit("/", 1)[-1]]["enum"],
            [
                "series_concept_strict_structure_recommended",
                "series_concept_strict_structure_experimental",
            ],
        )

    def test_series_concept_contract_documents_duration_and_unbounded_counts(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]
        operation = schema["paths"][
            "/api/v1/series-concept-drafts/generate/"
        ]["post"]

        request_ref = operation["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        request = components[request_ref.rsplit("/", 1)[-1]]["properties"]
        self.assertEqual(request["episode_count"]["minimum"], 1)
        self.assertNotIn("maximum", request["episode_count"])
        self.assertEqual(request["shots_per_episode"]["minimum"], 1)
        self.assertNotIn("maximum", request["shots_per_episode"])
        self.assertEqual(
            request["episode_duration_seconds"],
            {"type": "integer", "minimum": 1, "default": 60},
        )

        response_ref = operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        response = components[response_ref.rsplit("/", 1)[-1]]["properties"]
        candidate_ref = response["candidate"]["$ref"]
        candidate = components[candidate_ref.rsplit("/", 1)[-1]]
        self.assertIn("bible", candidate["properties"])
        self.assertEqual(
            set(candidate["required"]),
            {"title", "logline", "master_script", "bible"},
        )
        bible_ref = candidate["properties"]["bible"]["$ref"]
        bible = components[bible_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(bible["required"]), {"characters", "scenes", "props"})
        entity_ref = bible["properties"]["characters"]["items"]["$ref"]
        entity = components[entity_ref.rsplit("/", 1)[-1]]
        self.assertEqual(
            set(entity["required"]),
            {"name", "description", "visual_identity_en"},
        )
        estimate_ref = response["resource_estimate"]["$ref"]
        estimate = components[estimate_ref.rsplit("/", 1)[-1]]["properties"]
        self.assertEqual(set(estimate), {
            "total_shots",
            "estimated_input_chars",
            "estimated_output_tokens",
            "configured_output_token_hint",
            "output_hint_utilization_percent",
        })
        format_ref = response["format"]["$ref"]
        format_properties = components[
            format_ref.rsplit("/", 1)[-1]
        ]["properties"]
        self.assertEqual(format_properties["schema_version"], {
            "type": "integer", "maximum": 2, "minimum": 2,
        })
        template_ref = format_properties["template_id"]["$ref"]
        self.assertEqual(
            components[template_ref.rsplit("/", 1)[-1]]["enum"],
            ["deterministic-script-breakdown-v3"],
        )
        self.assertIn("episode_duration_seconds", format_properties)
        self.assertIn("total_duration_ms", format_properties)
        self.assertIn("shot_durations_ms", format_properties)

        for status_code in ("502", "503"):
            error_ref = operation["responses"][status_code]["content"][
                "application/json"
            ]["schema"]["$ref"]
            error = components[error_ref.rsplit("/", 1)[-1]]["properties"]
            nested_error = components[
                error["error"]["$ref"].rsplit("/", 1)[-1]
            ]["properties"]
            details = components[
                nested_error["details"]["$ref"].rsplit("/", 1)[-1]
            ]["properties"]
            diagnostic = components[
                details["diagnostic"]["$ref"].rsplit("/", 1)[-1]
            ]["properties"]
            self.assertEqual(set(diagnostic), {
                "diagnostic_id", "provider", "error_code", "failure_phase",
                "finish_reason", "usage", "details_available", "details",
            })
            expanded = components[
                diagnostic["details"]["$ref"].rsplit("/", 1)[-1]
            ]["properties"]
            self.assertIn("configured_timeout_seconds", expanded)
            self.assertIn("configured_max_tokens", expanded)
            self.assertIn("output", expanded)

    def test_shot_detail_batch_contract_is_async_confirmed_and_atomic(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        create_path = "/api/v1/projects/{project_id}/shot-detail-draft-batches/"
        detail_path = "/api/v1/shot-detail-draft-batches/{batch_id}/"
        apply_path = "/api/v1/shot-detail-draft-batches/{batch_id}/apply/"
        self.assertIn(create_path, paths)
        self.assertIn(detail_path, paths)
        self.assertIn(apply_path, paths)

        create = paths[create_path]["post"]
        self.assertEqual(set(create["responses"]), {"200", "202", "409", "503"})
        request_ref = create["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        request_schema = schema["components"]["schemas"][request_ref.rsplit("/", 1)[-1]]
        request = request_schema["properties"]
        self.assertIn("shot_version_key", request_schema["required"])
        self.assertEqual(
            request["confirmation_acknowledged"],
            {"type": "boolean", "default": False},
        )
        self.assertNotIn("max_shots", request)

        batch_component = schema["components"]["schemas"]["ShotDetailDraftBatch"]
        self.assertTrue({
            "shot_version_key", "shot_version_label", "version_scope_status",
        }.issubset(batch_component["properties"]))
        version_scope_schema = batch_component["properties"]["version_scope_status"]
        version_scope_ref = version_scope_schema.get("$ref")
        if not version_scope_ref:
            version_scope_ref = version_scope_schema.get("allOf", [{}])[0].get("$ref")
        self.assertTrue(version_scope_ref)
        version_scope_enum = schema["components"]["schemas"][
            version_scope_ref.rsplit("/", 1)[-1]
        ]["enum"]
        self.assertEqual(
            set(version_scope_enum),
            {"resolved", "legacy_mixed", "legacy_partial", "legacy_unknown"},
        )

        list_operation = paths[create_path]["get"]
        parameters = {item["name"]: item for item in list_operation["parameters"]}
        self.assertTrue(parameters["episode"]["required"])
        self.assertEqual(parameters["episode"]["schema"]["format"], "uuid")
        self.assertTrue(parameters["shot_version_key"]["required"])
        list_ref = list_operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        list_schema = schema["components"]["schemas"][list_ref.rsplit("/", 1)[-1]]
        self.assertIn("results", list_schema["properties"])
        self.assertEqual(
            list_schema["properties"]["results"]["items"]["$ref"],
            "#/components/schemas/ShotDetailDraftBatch",
        )

        apply = paths[apply_path]["post"]
        self.assertEqual(set(apply["responses"]), {"200", "400", "409"})
        apply_ref = apply["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        apply_request = schema["components"]["schemas"][
            apply_ref.rsplit("/", 1)[-1]
        ]["properties"]
        self.assertIn("expected_batch_revision", apply_request)
        self.assertIn("items", apply_request)
        apply_response_ref = apply["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        apply_response = schema["components"]["schemas"][
            apply_response_ref.rsplit("/", 1)[-1]
        ]["properties"]
        self.assertIn("applied_shots", apply_response)
        self.assertIn("translation_written", apply_response)

        detail_response_ref = paths[detail_path]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["$ref"]
        detail_response = schema["components"]["schemas"][
            detail_response_ref.rsplit("/", 1)[-1]
        ]["properties"]
        self.assertIn("applied_shots", detail_response)
        self.assertIn("translation_written", detail_response)

    def test_episode_shot_sync_requires_an_explicit_storyboard_version(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        operation = schema["paths"]["/api/v1/episodes/{id}/sync-shots/"]["post"]
        request_ref = operation["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        request_schema = schema["components"]["schemas"][
            request_ref.rsplit("/", 1)[-1]
        ]
        self.assertEqual(
            set(request_schema["required"]),
            {"shot_version_key", "shots"},
        )
        self.assertEqual(
            request_schema["properties"]["shot_version_key"]["maxLength"],
            191,
        )

    def test_production_queue_guidance_and_confirmation_are_explicit_contracts(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]

        capability_response = schema["paths"][
            "/api/v1/capabilities/production/"
        ]["get"]["responses"]["200"]
        self.assertEqual(
            capability_response["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductionCapability",
        )
        capability = components["ProductionCapability"]["properties"]
        self.assertEqual(
            set(capability),
            {
                "recommended_max_pending", "recommended_max_active_per_user",
                "max_concurrent", "task_timeout_seconds",
                "video_shot_min_duration_ms", "video_shot_max_duration_ms",
                "script_breakdown_default_shot_duration_ms",
                "script_breakdown_default_first_frame_continuity",
                "script_breakdown_default_last_frame_continuity",
                "script_breakdown_auto_split_trigger_chars",
                "script_breakdown_auto_split_target_chars",
                "active_task_count",
                "user_active_task_count", "confirmation_required",
                "business_volume_limited",
            },
        )

        request = components["ProductionBuildRequest"]["properties"]
        self.assertEqual(request["shot_version_key"]["maxLength"], 191)
        self.assertIn("whole_episode=true", request["shot_version_key"]["description"])
        self.assertNotIn(
            "shot_version_key",
            components["ProductionBuildRequest"].get("required", []),
        )
        self.assertEqual(
            request["extended_generation_confirmed"],
            {
                "type": "boolean",
                "default": False,
                "description": (
                    "超过当前任务队列推荐阈值时的显式确认。确认只允许继续排队，"
                    "不会提高 worker 并发，也不会改变单镜生成参数。"
                ),
            },
        )
        self.assertNotIn("max_episodes", request)
        self.assertNotIn("max_shots", request)

        responses = schema["paths"][
            "/api/v1/projects/{id}/build-production/"
        ]["post"]["responses"]
        self.assertEqual(set(responses), {"200", "201", "409"})
        for status_code in ("200", "201"):
            self.assertEqual(
                responses[status_code]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/ProductionBuildResponse",
            )
        self.assertEqual(
            responses["409"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductionBuildConflictErrorResponse",
        )
        self.assertEqual(
            components["ProductionBuildConflictErrorResponse"]["oneOf"],
            [
                {"$ref": "#/components/schemas/ProductionQueueConfirmationErrorResponse"},
                {"$ref": "#/components/schemas/ProductionIdempotencyConflictErrorResponse"},
            ],
        )

        build_response = components["ProductionBuildResponse"]["properties"]
        self.assertTrue({
            "batch_id", "batch_kind", "batch_size", "task_ids",
            "production_units", "extended_generation_confirmed",
            "deferred_generation_request_ids",
            "shot_version_key", "shot_version_label", "batch_shot_ids",
            "batch_slots",
        } <= set(build_response))
        self.assertEqual(
            build_response["batch_slots"]["items"]["$ref"],
            "#/components/schemas/GenerationTaskBatchSlot",
        )
        slot_properties = components["GenerationTaskBatchSlot"]["properties"]
        self.assertTrue({
            "shot_version_key", "shot_version_label", "batch_index",
            "batch_size", "slot_status", "shot_id", "task_id",
            "deferred_generation_request_id",
        } <= set(slot_properties))
        build_counts = components["ProductionBuildCounts"]["properties"]
        self.assertTrue({
            "prompts", "workflows", "tasks", "deferred_generation_requests",
        } <= set(build_counts))
        details = components["ProductionQueueConfirmationErrorDetails"]["properties"]
        self.assertEqual(
            set(details),
            {
                "detail", "requested_task_count", "active_task_count",
                "user_active_task_count", "recommended_max_pending",
                "recommended_max_active_per_user", "max_concurrent",
            },
        )
        idempotency_details = components[
            "ProductionIdempotencyConflictErrorDetails"
        ]["properties"]
        self.assertEqual(set(idempotency_details), {"detail", "idempotency_key"})
        idempotency_code = components[
            "ProductionIdempotencyConflictErrorEnvelopeCodeEnum"
        ]
        self.assertEqual(idempotency_code["enum"], ["production_idempotency_conflict"])

        for path, request_schema in (
            ("/api/v1/tasks/", "GenerationTaskCreateRequest"),
            ("/api/v1/tasks/{id}/retry/", "GenerationTaskRetryRequest"),
        ):
            operation = schema["paths"][path]["post"]
            self.assertEqual(
                operation["requestBody"]["content"]["application/json"]["schema"]["$ref"],
                f"#/components/schemas/{request_schema}",
            )
            self.assertEqual(set(operation["responses"]), {"201", "409"})
            self.assertEqual(
                operation["responses"]["409"]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/ProductionQueueConfirmationErrorResponse",
            )
            confirmation = components[request_schema]["properties"][
                "extended_generation_confirmed"
            ]
            self.assertEqual(confirmation["type"], "boolean")
            self.assertFalse(confirmation["default"])

        resume_batch = schema["paths"]["/api/v1/tasks/{id}/resume-batch/"]["post"]
        self.assertEqual(
            resume_batch["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/GenerationTaskRetryRequest",
        )
        self.assertEqual(set(resume_batch["responses"]), {"200", "201", "409"})
        for status_code in ("200", "201"):
            self.assertEqual(
                resume_batch["responses"][status_code]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/ProductionBatchResumeResponse",
            )
        self.assertEqual(
            resume_batch["responses"]["409"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductionQueueConfirmationErrorResponse",
        )

        cancel_batch = schema["paths"]["/api/v1/tasks/{id}/cancel-batch/"]["post"]
        self.assertNotIn("requestBody", cancel_batch)
        self.assertEqual(set(cancel_batch["responses"]), {"200"})
        self.assertEqual(
            cancel_batch["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductionBatchCancelResponse",
        )

        delete_batch = schema["paths"]["/api/v1/tasks/{id}/delete-batch/"]["delete"]
        self.assertNotIn("requestBody", delete_batch)
        self.assertEqual(set(delete_batch["responses"]), {"200", "400"})
        self.assertEqual(
            delete_batch["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductionBatchDeleteResponse",
        )
        self.assertEqual(
            delete_batch["responses"]["400"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/APIValidationErrorEnvelope",
        )

        project_create = schema["paths"]["/api/v1/projects/"]["post"]
        self.assertEqual(
            project_create["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/SeriesProjectRequest",
        )

    def test_script_breakdown_has_named_request_and_created_response_contracts(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        operation = schema["paths"][
            "/api/v1/projects/{id}/breakdown-script/"
        ]["post"]
        self.assertEqual(set(operation["responses"]), {"201", "400", "409"})
        self.assertEqual(
            operation["responses"]["201"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ScriptBreakdownResponse",
        )
        self.assertEqual(
            operation["responses"]["400"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/APIValidationErrorEnvelope",
        )
        self.assertEqual(
            operation["responses"]["409"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ScriptBreakdownConflictResponse",
        )
        request = schema["components"]["schemas"]["ScriptBreakdownRequest"]["properties"]
        self.assertTrue({
            "default_shot_duration_ms",
            "default_first_frame_continuity",
            "default_last_frame_continuity",
            "discard_existing_production",
            "continue_without_script_bible",
        } <= set(request))
        response = schema["components"]["schemas"]["ScriptBreakdownResponse"]["properties"]
        self.assertIn("cleared", response)
        shot = schema["components"]["schemas"]["Shot"]["properties"]
        self.assertTrue({
            "requires_first_frame_continuity",
            "requires_last_frame_continuity",
        } <= set(shot))

    def test_script_bible_apply_documents_the_global_validation_envelope(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        operation = schema["paths"][
            "/api/v1/projects/{id}/script-bible-candidates/apply/"
        ]["post"]
        self.assertEqual(
            operation["responses"]["400"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/APIValidationErrorEnvelope",
        )
        components = schema["components"]["schemas"]
        envelope = components["APIValidationErrorEnvelope"]
        self.assertEqual(set(envelope["required"]), {"error"})
        error = components["APIValidationError"]
        self.assertEqual(
            set(error["required"]), {"status_code", "code", "details"},
        )
        self.assertEqual(error["properties"]["details"], {})
        for component_name in (
            "CharacterBibleRequest",
            "SceneBibleRequest",
            "PropBibleRequest",
            "CharacterBibleSyncItemRequest",
        ):
            description = components[component_name]["properties"][
                "metadata"
            ]["description"]
            self.assertIn("script_bible_v1", description)
            self.assertIn("apply", description)

    def test_bible_image_candidate_and_promotion_contracts_are_closed_and_explicit(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]
        candidate_operation = schema["paths"][
            "/api/v1/projects/{id}/bible-image-candidates/"
        ]["post"]
        request_ref = candidate_operation["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        candidate_request = components[request_ref.rsplit("/", 1)[-1]]
        self.assertIs(candidate_request["additionalProperties"], False)
        self.assertEqual(
            set(candidate_request["required"]),
            {"entity_type", "entity_id", "idempotency_key"},
        )
        self.assertIn("prompt_overrides", candidate_request["properties"])
        latest = components["BibleImageLatestCandidate"]
        latest_properties = latest["properties"]
        projection = components["BibleImageCandidateProjection"]
        response_ref = schema["paths"][
            "/api/v1/projects/{id}/bible-image-candidates/"
        ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        response_schema = components[response_ref.rsplit("/", 1)[-1]]
        self.assertIn("prompt_config", response_schema["properties"])
        prompt_config_ref = response_schema["properties"]["prompt_config"]["$ref"]
        self.assertIs(
            components[prompt_config_ref.rsplit("/", 1)[-1]]["additionalProperties"],
            False,
        )
        self.assertIn("current_prompt_inputs", projection["properties"])
        prompt_inputs_ref = projection["properties"]["current_prompt_inputs"]["$ref"]
        prompt_inputs = components[prompt_inputs_ref.rsplit("/", 1)[-1]]
        self.assertIs(prompt_inputs["additionalProperties"], False)
        self.assertEqual(
            prompt_inputs["properties"]["positive"]["items"]["$ref"],
            "#/components/schemas/BibleImagePromptInput",
        )
        self.assertIn("prompt_inputs", latest_properties)
        prompt_input = components["BibleImagePromptInput"]
        self.assertIs(prompt_input["additionalProperties"], False)
        self.assertEqual(
            set(prompt_input["required"]),
            {"key", "label", "value", "role", "source", "editable"},
        )
        self.assertIn("candidate_history", projection["properties"])
        self.assertIn("candidate_history", projection["required"])
        self.assertEqual(
            projection["properties"]["candidate_history"]["items"]["$ref"],
            "#/components/schemas/BibleImageLatestCandidate",
        )
        quality_ref = latest_properties["quality"]["$ref"]
        quality = components[quality_ref.rsplit("/", 1)[-1]]
        self.assertIs(quality["additionalProperties"], False)
        self.assertIn("warnings", quality["properties"])
        self.assertIn("issues", quality["properties"])
        issue_ref = quality["properties"]["issues"]["items"]["$ref"]
        self.assertIs(components[issue_ref.rsplit("/", 1)[-1]]["additionalProperties"], False)
        output_ref = latest_properties["output_media"]["items"]["$ref"]
        output = components[output_ref.rsplit("/", 1)[-1]]
        self.assertIs(output["additionalProperties"], False)
        self.assertIn("promote_to_asset_url", output["properties"])

        task_schema = components["GenerationTask"]
        task_properties = task_schema["properties"]
        self.assertNotIn("capability_snapshot", task_properties)
        self.assertTrue(task_properties["generation_purpose"]["nullable"])
        self.assertTrue(task_properties["generation_purpose"]["readOnly"])
        purpose_ref = next(
            item["$ref"] for item in task_properties["generation_purpose"]["oneOf"]
            if item["$ref"] != "#/components/schemas/NullEnum"
        )
        self.assertEqual(
            components[purpose_ref.rsplit("/", 1)[-1]]["enum"],
            ["bible_image_candidate"],
        )
        self.assertTrue(task_properties["bible_target"]["nullable"])
        self.assertTrue(task_properties["bible_target"]["readOnly"])
        target_ref = task_properties["bible_target"]["allOf"][0]["$ref"]
        target = components[target_ref.rsplit("/", 1)[-1]]
        self.assertIs(target["additionalProperties"], False)
        self.assertEqual(
            set(target["required"]),
            {
                "schema_version", "reference_category", "entity_id",
                "entity_name", "source_fingerprint",
            },
        )
        self.assertEqual(target["properties"]["schema_version"]["minimum"], 1)
        self.assertEqual(target["properties"]["schema_version"]["maximum"], 1)
        self.assertEqual(target["properties"]["entity_id"]["format"], "uuid")
        self.assertEqual(
            target["properties"]["source_fingerprint"]["pattern"],
            "^[0-9a-f]{64}$",
        )
        category_ref = target["properties"]["reference_category"]["$ref"]
        self.assertEqual(
            set(components[category_ref.rsplit("/", 1)[-1]]["enum"]),
            {"character", "scene", "prop"},
        )
        self.assertEqual(
            schema["paths"]["/api/v1/tasks/"]["get"]["responses"]["200"]
            ["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PaginatedGenerationTaskList",
        )
        for path, method, status_code in (
            ("/api/v1/tasks/{id}/", "get", "200"),
            ("/api/v1/tasks/{id}/retry/", "post", "201"),
        ):
            self.assertEqual(
                schema["paths"][path][method]["responses"][status_code]
                ["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/GenerationTask",
            )

        promotion = schema["paths"][
            "/api/v1/tasks/{id}/promote-output-to-asset/"
        ]["post"]
        promotion_request_ref = promotion["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        promotion_request = components[promotion_request_ref.rsplit("/", 1)[-1]]
        self.assertIs(promotion_request["additionalProperties"], False)
        self.assertEqual(
            promotion_request["properties"]["accept_stale_bible_image_candidate"]["type"],
            "boolean",
        )
        self.assertEqual(set(promotion["responses"]), {"200", "201", "400", "404", "409"})
        self.assertEqual(
            promotion["responses"]["400"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/APIValidationErrorEnvelope",
        )
        self.assertEqual(
            promotion["responses"]["404"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/BibleImageCandidateErrorResponse",
        )

    def test_deferred_generation_request_contract_is_closed_and_explicit(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]
        create_operation = schema["paths"][
            "/api/v1/deferred-generation-requests/"
        ]["post"]
        create_request_ref = create_operation["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        create_request = components[create_request_ref.rsplit("/", 1)[-1]]
        self.assertIs(create_request["additionalProperties"], False)
        self.assertEqual(
            set(create_request["required"]),
            {
                "project_id",
                "source_task_id",
                "target_shot_id",
                "frame_slot",
                "idempotency_key",
            },
        )
        self.assertNotIn("output_index", create_request["properties"])
        self.assertNotIn("template_id", create_request["properties"])
        self.assertNotIn("workflow_id", create_request["properties"])
        self.assertNotIn("reference_asset", create_request["properties"])
        self.assertEqual(set(create_operation["responses"]), {"200", "201", "400", "404", "409", "503"})
        self.assertEqual(
            create_operation["responses"]["400"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/APIValidationErrorEnvelope",
        )
        self.assertEqual(
            create_operation["responses"]["409"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DeferredGenerationErrorResponse",
        )

        list_response = schema["paths"][
            "/api/v1/deferred-generation-requests/"
        ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
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
        cancel_operation = schema["paths"][
            "/api/v1/deferred-generation-requests/{id}/cancel/"
        ]["post"]
        cancel_request_ref = cancel_operation["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        cancel_request = components[cancel_request_ref.rsplit("/", 1)[-1]]
        self.assertIs(cancel_request["additionalProperties"], False)
        self.assertNotIn("properties", cancel_request)
        self.assertEqual(set(cancel_operation["responses"]), {"200", "400", "409"})
        self.assertEqual(
            cancel_operation["responses"]["400"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/APIValidationErrorEnvelope",
        )
