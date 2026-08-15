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
        } <= set(provider))
        self.assertIn("temporarily_busy", provider)

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
        request = schema["components"]["schemas"][request_ref.rsplit("/", 1)[-1]]["properties"]
        self.assertEqual(
            request["confirmation_acknowledged"],
            {"type": "boolean", "default": False},
        )
        self.assertNotIn("max_shots", request)

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
        } <= set(build_response))
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
        self.assertEqual(set(operation["responses"]), {"201", "409"})
        self.assertEqual(
            operation["responses"]["201"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ScriptBreakdownResponse",
        )
        self.assertEqual(
            operation["responses"]["409"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ScriptRebuildConflictResponse",
        )
        request = schema["components"]["schemas"]["ScriptBreakdownRequest"]["properties"]
        self.assertTrue({
            "default_shot_duration_ms",
            "default_first_frame_continuity",
            "default_last_frame_continuity",
            "discard_existing_production",
        } <= set(request))
        response = schema["components"]["schemas"]["ScriptBreakdownResponse"]["properties"]
        self.assertIn("cleared", response)
        shot = schema["components"]["schemas"]["Shot"]["properties"]
        self.assertTrue({
            "requires_first_frame_continuity",
            "requires_last_frame_continuity",
        } <= set(shot))
