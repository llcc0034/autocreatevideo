from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient


class RetiredRouteAdapterTests(SimpleTestCase):
    def test_request_preparation_has_no_old_route_or_prompt_replay_adapters(self):
        from studio.services import production_runs
        for name in (
            "_selected_conditioning_mode_contract_for_shot",
            "_frozen_media_frame_policy_for_shot",
            "_verify_existing_prompt_media_profile",
            "_freeze_project_execution_contract",
            "_effective_route_resolution",
            "_record_actual_cost",
            "_allocated_stage_unknown_cost",
            "_assert_initialized_production_run",
            "_assert_replayable_production_run",
            "_series_policy",
            "_frozen_route_timeout_seconds",
            "_safe_media_route_projection",
            "_execution_monitoring_from_frozen_route",
            "_media_execution_identity",
            "_apply_structured_shot_duration_contract",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(production_runs, name))

    def test_retired_bible_prompt_has_no_history_reader(self):
        from studio.services import bible_reference_contract
        self.assertFalse(hasattr(bible_reference_contract, "retired_bible_image_candidate_snapshot"))

    def test_compilers_have_no_project_prompt_or_binding_replay_path(self):
        from importlib.util import find_spec
        from studio.services import production_pipeline, prompt_profiles, workflow_compiler, workflow_semantics, workflow_resolution, prompt_normalization, async_media_contract
        removed = (
            (production_pipeline, ("compile_prompt_input", "CompiledPromptInput", "_profiled_media_quality",
                "materialize_compiled_workflow_version", "materialize_generation_task")),
            (workflow_compiler, ("compile_frozen_workflow", "_compile_generic", "build_frozen_workflow_runtime_overrides")),
            (workflow_semantics, ("resolve_workflow_semantics", "_asset_for_semantic")),
            (workflow_resolution, ("resolve_source_image_resolution",)),
            (prompt_profiles, ("validate_frozen_prompt_profile_binding_reference", "verify_frozen_profile_assembly",
                "profile_fingerprint", "frozen_profile_fingerprint", "ResolvedPromptProfile",
                "validate_prompt_profile_binding", "resolve_prompt_profile_binding", "require_prompt_profile_binding",
                "compatible_prompt_profile_bindings", "require_explicit_prompt_profile_binding",
                "frozen_profile_reference", "load_frozen_prompt_profile_selection", "verify_frozen_profile_reference")),
            (prompt_normalization, ("route_state", "PromptNormalizationRoute")),
            (async_media_contract, ("fresh_smoke_snapshot", "frozen_async_deployment")),
        )
        for module, names in removed:
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertFalse(hasattr(module, name))
        self.assertIsNone(find_spec("studio.services.prompt_quality_gate"))
        self.assertIsNone(find_spec("studio.services.deployment_smoke_profiles"))


class RetiredGenerationEntrypointTests(TestCase):
    def test_no_deferred_read_write_or_compatibility_route(self):
        client = APIClient()
        client.force_authenticate(get_user_model().objects.create_user(username="retired-entry", is_staff=True))
        identifier = "11111111-1111-4111-8111-111111111111"
        for resource in ("deferred-generation-requests", "prompts", "workflows"):
            for suffix in ("", f"{identifier}/", "generate/", f"{identifier}/cancel/", f"{identifier}/retry/"):
                for method in ("get", "post", "patch", "delete"):
                    with self.subTest(resource=resource, suffix=suffix, method=method):
                        response = getattr(client, method)(f"/api/v1/{resource}/{suffix}", {}, format="json")
                        self.assertEqual(response.status_code, 404)
