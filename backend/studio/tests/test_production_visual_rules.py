import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import (
    CharacterBible,
    GenerationAttempt,
    GenerationAttemptResolution,
    ProductionRecipeVersion,
    ProductionRun,
    SeriesProject,
    StageRun,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_runs import (
    StageBlocked,
    _prepare_visual_rules,
    _queue_visual_rules_attempt_for_route,
)
from studio.services.production_visual_rules import (
    COLOR_DESIGN_CHOICES,
    COLOR_VALUES,
    VisualRulesConflict,
    VisualRulesError,
    apply_visual_rules_candidate,
    build_visual_rules_smoke_plan,
    build_visual_rules_plan,
    finalize_visual_rules,
    validate_visual_rules_model_profile,
    validate_reviewed_rules,
    visual_rules_provider_payload,
    visual_rules_resource_estimate,
    VISUAL_RULES_OPERATION,
    VISUAL_RULES_UNIT_KEY,
)
from studio.services.deployment_smoke import execute_visual_rules_smoke_request
from studio.services.provider_registry import ProviderDriverError, _invoke_chat_completion
from studio.services.script_sources import confirm_script_revision, create_pasted_script_source
from studio.tests.factories import build_story


RECOMMENDED_NEGATIVE_CLAUSES = (
    "identity drift",
    "readable text",
)


def _visual_chat_request_options(**extra) -> dict:
    return {
        "temperature": 0,
        "response_format": {"type": "json_object"},
        **extra,
    }


class ProductionVisualRulesContractTests(TestCase):
    def setUp(self):
        self.user, self.project, *_ = build_story("visual-rules-owner")
        self.project.production_settings = {
            "default_conditioning_mode": "ref2va",
            "translation_glossary": {"林默": "Lin Mo"},
            "negative_prompt": "costume drift",
            "color_script": "",
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        _source, revision = create_pasted_script_source(
            project=self.project,
            user=self.user,
            content=(
                "第一集：停电前\n"
                "林默穿炭灰风衣进入旧档案室。\n"
                "绿色台灯照亮烧焦的信纸，闪电从窗外划过。"
            ),
        )
        self.revision = confirm_script_revision(revision, user=self.user)
        self.plan = build_visual_rules_plan(
            revision=self.revision,
            project=SeriesProject.objects.select_related("style").get(pk=self.project.pk),
            model_identifier="fixture-model",
            policy={
                "max_input_bytes": 64_000,
                "max_output_tokens": 1_024,
                "timeout_seconds": 30,
                "chat_request_options": _visual_chat_request_options(),
            },
        )

    def _payload(self):
        source = self.plan["source_revision"]
        return {
            "schema_version": 1,
            "source_revision_id": source["id"],
            "source_sha256": source["content_sha256"],
            "source_size_bytes": source["content_size_bytes"],
            "color_design": {
                "base_colors": ["charcoal", "green"],
                "accent_color": "amber",
                "palette_relation": "cinematic",
                "lighting": "low_key",
                "contrast": "high",
                "saturation": "muted",
                "exposure": "dark",
                "progression": "dark_to_bright",
                "preserve_natural_skin_exposure": True,
            },
            "negative_constraints": [
                "anachronistic props",
                "inconsistent lightning direction",
            ],
            "continuity_summary": (
                "Keep Lin Mo's face, charcoal trench coat, archive-room geometry, "
                "green desk lamp, and burned letter state consistent across every episode."
            ),
        }

    def test_full_script_and_confirmed_bible_enter_frozen_request(self):
        provider_payload = visual_rules_provider_payload(self.plan)
        user_payload = json.loads(provider_payload["messages"][1]["content"])
        self.assertEqual(self.revision.content, user_payload["source_text"])
        context = self.plan["project_context"]
        self.assertTrue(context["confirmed_bible"]["characters"])
        self.assertTrue(context["confirmed_bible"]["scenes"])
        self.assertEqual(len(context["confirmed_bible_sha256"]), 64)

    def test_provider_prompt_keeps_structure_hard_without_hidden_length_advisories(self):
        provider_payload = visual_rules_provider_payload(self.plan)
        system = provider_payload["messages"][0]["content"]
        enum_payload = json.loads(system.split("\nOUTPUT_ENUMS=", 1)[1].split(
            "\nNEGATIVE_CONSTRAINT_GRAMMAR=", 1,
        )[0])
        grammar = json.loads(system.split("\nNEGATIVE_CONSTRAINT_GRAMMAR=", 1)[1])

        self.assertEqual(
            enum_payload["color_design.base_colors"]["allowed"],
            list(COLOR_VALUES),
        )
        for field, choices in COLOR_DESIGN_CHOICES.items():
            self.assertEqual(enum_payload[f"color_design.{field}"], list(choices))
        self.assertEqual(
            enum_payload["color_design.base_colors"]["uniqueness"],
            "required",
        )
        self.assertNotIn("cardinality", enum_payload["color_design.base_colors"])
        self.assertNotIn("max_chars", grammar)
        self.assertNotIn("required_exact_token_one_of", grammar)
        self.assertNotIn("valid_examples", grammar)
        self.assertEqual(grammar["forbidden_item_delimiters"], [",", ";", "CR", "LF"])
        self.assertIn("MAY be an empty array", system)
        self.assertNotIn("QUALITY_ADVISORIES=", system)

    def test_model_chat_options_and_advisory_thresholds_are_frozen_not_hardcoded(self):
        plan = build_visual_rules_plan(
            revision=self.revision,
            project=SeriesProject.objects.select_related("style").get(pk=self.project.pk),
            model_identifier="fixture-model",
            policy={
                "max_input_bytes": 64_000,
                "input_byte_warning_threshold": 1,
                "max_output_tokens": 131_072,
                "output_token_warning_threshold": 32_768,
                "timeout_seconds": 600,
                "chat_request_options": _visual_chat_request_options(
                    thinking={"type": "disabled"},
                ),
            },
        )

        provider_payload = visual_rules_provider_payload(plan)
        self.assertEqual(provider_payload["max_tokens"], 131_072)
        self.assertEqual(
            provider_payload["chat_request_options"],
            _visual_chat_request_options(thinking={"type": "disabled"}),
        )
        self.assertEqual(
            plan["resource_estimate"]["output_token_warning_threshold"],
            32_768,
        )
        frozen = finalize_visual_rules(plan=plan, payload=self._payload())
        self.assertIn(
            "仍使用完整正文生成",
            " ".join(frozen["warnings"]),
        )

    def test_quality_advisories_are_frozen_into_prompt_and_only_warn(self):
        policy = {
            "schema_version": 1,
            "base_colors_min_items": 1,
            "base_colors_max_items": 3,
            "negative_constraints_min_items": 0,
            "negative_constraints_max_items": 32,
            "negative_constraint_max_chars": 120,
            "recommended_negative_clauses": list(RECOMMENDED_NEGATIVE_CLAUSES),
            "continuity_summary_max_chars": 10,
            "negative_prompt_max_chars": 10,
            "color_script_max_chars": 10,
        }
        plan = build_visual_rules_plan(
            revision=self.revision,
            project=SeriesProject.objects.select_related("style").get(pk=self.project.pk),
            model_identifier="fixture-model",
            policy={
                "max_input_bytes": 64_000,
                "max_output_tokens": 1_024,
                "timeout_seconds": 30,
                "chat_request_options": _visual_chat_request_options(),
                "quality_advisories": policy,
            },
        )
        payload = self._payload()
        payload["color_design"]["base_colors"] = ["charcoal", "green", "amber", "slate"]
        payload["negative_constraints"] = [
            f"compression banding defect variant {index}"
            for index in range(33)
        ]
        payload["negative_constraints"][0] = "x" * 121

        provider_payload = visual_rules_provider_payload(plan)
        system = provider_payload["messages"][0]["content"]
        configured = json.loads(system.split("QUALITY_ADVISORIES=", 1)[1])
        self.assertEqual(configured, policy)

        frozen = finalize_visual_rules(plan=plan, payload=payload)

        self.assertEqual(frozen["kind"], "visual_rules_candidates")
        self.assertEqual(frozen["color_design"]["base_colors"], payload["color_design"]["base_colors"])
        codes = {item["code"] for item in frozen["advisory_audit"]}
        self.assertTrue({
            "base_colors_count_outside_recommendation",
            "negative_constraints_count_outside_recommendation",
            "negative_constraint_length_exceeds_recommendation",
            "continuity_summary_length_exceeds_recommendation",
            "negative_prompt_length_exceeds_recommendation",
            "color_script_length_exceeds_recommendation",
        }.issubset(codes))
        self.assertTrue(all(
            item["enforcement"] == "advisory"
            for item in frozen["advisory_audit"]
        ))
        for clause in RECOMMENDED_NEGATIVE_CLAUSES:
            self.assertIn(clause, frozen["candidate"]["negative_prompt"])

    def test_missing_quality_advisories_do_not_enable_implicit_recommendations(self):
        payload = self._payload()
        payload["color_design"]["base_colors"] = []
        payload["negative_constraints"] = [
            f"compression banding defect variant {index}"
            for index in range(33)
        ]
        payload["negative_constraints"][0] = "x" * 121
        payload["continuity_summary"] = "K" * (70 * 1_024)

        frozen = finalize_visual_rules(plan=self.plan, payload=payload)

        self.assertEqual(frozen["advisory_audit"], [])
        self.assertEqual(frozen["warnings"], [])
        self.assertIn("no declared base color", frozen["candidate"]["color_script"])
        self.assertNotIn("identity drift", frozen["candidate"]["negative_prompt"])

    def test_resource_estimate_rebuild_keeps_only_configured_quality_policy(self):
        policy = {
            "max_output_tokens": 1_024,
            "timeout_seconds": 30,
            "quality_advisories": {
                "schema_version": 1,
                "negative_constraints_max_items": 32,
            },
        }

        estimate = visual_rules_resource_estimate(
            source_size_bytes=123,
            policy=policy,
        )
        self.assertEqual(
            estimate["quality_advisories"],
            policy["quality_advisories"],
        )
        self.assertIsNot(
            estimate["quality_advisories"],
            policy["quality_advisories"],
        )
        del policy["quality_advisories"]
        self.assertNotIn(
            "quality_advisories",
            visual_rules_resource_estimate(
                source_size_bytes=123,
                policy=policy,
            ),
        )

    def test_fallback_rebuild_freezes_selected_profile_quality_policy(self):
        quality_advisories = {
            "schema_version": 1,
            "negative_constraints_max_items": 32,
            "recommended_negative_clauses": list(
                RECOMMENDED_NEGATIVE_CLAUSES,
            ),
        }
        policy = {
            "max_input_bytes": 64_000,
            "max_output_tokens": 2_048,
            "timeout_seconds": 45,
            "chat_request_options": _visual_chat_request_options(),
            "quality_advisories": quality_advisories,
        }
        profile_version = SimpleNamespace(model_identifier="fallback-model")
        deployment = SimpleNamespace(profile_version=profile_version)
        live_deployment = SimpleNamespace(profile_version=profile_version)
        run = SimpleNamespace(
            script_revision=self.revision,
            stages=MagicMock(),
            project_id=self.project.id,
            input_snapshot={},
            generation_task_count=0,
            max_generation_tasks=10,
            save=MagicMock(),
        )
        run.stages.filter.return_value.first.return_value = object()
        resolved_profile = SimpleNamespace(
            profile_version=SimpleNamespace(contract_sha256="a" * 64),
            binding=SimpleNamespace(key="fixture-visual-rules-binding"),
        )
        route = {
            "index": 1,
            "model_deployment_id": "11111111-1111-1111-1111-111111111111",
            "is_external": True,
        }
        select_related = MagicMock()
        select_related.get.return_value = live_deployment
        current_context = {
            "rules_sha256": self.plan["project_context"]["current_rules_sha256"],
            "bible_sha256": self.plan["project_context"]["confirmed_bible_sha256"],
        }

        with (
            patch(
                "studio.services.production_runs.ModelDeployment.objects.select_related",
                return_value=select_related,
            ),
            patch(
                "studio.services.production_runs.frozen_text_deployment",
                return_value=deployment,
            ),
            patch(
                "studio.services.production_runs.deployment_supports_script_generation",
                return_value=True,
            ),
            patch(
                "studio.services.production_runs._route_with_revalidated_text_smoke",
                return_value=route,
            ),
            patch("studio.services.production_runs.verify_visual_rules_smoke"),
            patch(
                "studio.services.production_runs.validate_visual_rules_model_profile",
                return_value=policy,
            ),
            patch(
                "studio.services.production_runs.SeriesProject.objects.select_related",
            ) as project_query,
            patch(
                "studio.services.production_runs.current_visual_context_snapshot",
                return_value=current_context,
            ),
            patch(
                "studio.services.production_runs.require_prompt_profile_binding",
                return_value=resolved_profile,
            ),
            patch(
                "studio.services.production_runs."
                "validate_frozen_prompt_profile_binding_reference",
            ),
            patch(
                "studio.services.production_runs.model_family_for_profile",
                return_value="fixture-family",
            ),
            patch(
                "studio.services.production_runs.text_profile_sources",
                return_value={},
            ),
            patch(
                "studio.services.production_runs.assemble_prompt_profile",
                return_value={"channels": {}, "structured_fields": {}},
            ),
            patch(
                "studio.services.production_runs.visual_rules_provider_payload",
                return_value={"messages": []},
            ),
            patch(
                "studio.services.production_runs.GenerationAttempt.objects.create",
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
            patch("studio.services.production_runs._estimate_and_reserve"),
            patch("studio.services.production_runs._mark_route_selected"),
        ):
            project_query.return_value.get.return_value = self.project
            attempt = _queue_visual_rules_attempt_for_route(
                run,
                MagicMock(),
                route=route,
                attempt_number=2,
                reason="ordered_fallback",
                frozen_plan=self.plan,
            )

        frozen_estimate = attempt.request_snapshot["plan"]["resource_estimate"]
        self.assertEqual(
            frozen_estimate["quality_advisories"],
            quality_advisories,
        )
        self.assertEqual(frozen_estimate["configured_output_token_hint"], 2_048)

    def test_duplicate_negative_constraints_remain_a_hard_uniqueness_error(self):
        payload = self._payload()
        payload["negative_constraints"] = ["compression banding", "Compression Banding"]

        with self.assertRaises(VisualRulesError) as captured:
            finalize_visual_rules(plan=self.plan, payload=payload)

        self.assertEqual(captured.exception.code, "negative_constraints_duplicate")

    def test_model_profile_quality_advisories_are_optional_and_strict(self):
        specification = {
            "schema_version": 1,
            "capabilities": ["visual_rules.generate"],
            "visual_rules": {
                "max_input_bytes": 64_000,
                "max_output_tokens": 1_024,
                "timeout_seconds": 30,
                "chat_request_options": _visual_chat_request_options(),
                "quality_advisories": {
                    "schema_version": 1,
                    "base_colors_min_items": 1,
                    "base_colors_max_items": 3,
                    "negative_constraints_max_items": 32,
                    "negative_constraint_max_chars": 120,
                    "recommended_negative_clauses": list(
                        RECOMMENDED_NEGATIVE_CLAUSES,
                    ),
                },
            },
        }
        profile = SimpleNamespace(modality="text", provider=object())
        driver = SimpleNamespace(capabilities=lambda: {"operations": ["chat_completion"]})
        with (
            patch(
                "studio.services.production_visual_rules.verified_model_profile_specification",
                return_value=specification,
            ),
            patch("studio.services.production_visual_rules.driver_for", return_value=driver),
        ):
            normalized = validate_visual_rules_model_profile(profile)

        self.assertEqual(
            normalized["quality_advisories"],
            specification["visual_rules"]["quality_advisories"],
        )

        del specification["visual_rules"]["quality_advisories"]
        with (
            patch(
                "studio.services.production_visual_rules.verified_model_profile_specification",
                return_value=specification,
            ),
            patch("studio.services.production_visual_rules.driver_for", return_value=driver),
        ):
            normalized = validate_visual_rules_model_profile(profile)
        self.assertNotIn("quality_advisories", normalized)

        specification["visual_rules"]["quality_advisories"] = {
            "schema_version": 1,
            "base_colors_min_items": 4,
            "base_colors_max_items": 3,
        }
        with (
            patch(
                "studio.services.production_visual_rules.verified_model_profile_specification",
                return_value=specification,
            ),
            patch("studio.services.production_visual_rules.driver_for", return_value=driver),
            self.assertRaises(ValidationError),
        ):
            validate_visual_rules_model_profile(profile)

        specification["visual_rules"]["quality_advisories"] = {
            "schema_version": True,
        }
        with (
            patch(
                "studio.services.production_visual_rules.verified_model_profile_specification",
                return_value=specification,
            ),
            patch("studio.services.production_visual_rules.driver_for", return_value=driver),
            self.assertRaises(ValidationError),
        ):
            validate_visual_rules_model_profile(profile)

        specification["visual_rules"]["quality_advisories"] = {
            "schema_version": 1,
            "recommended_negative_clauses": [
                "identity drift",
                "Identity Drift",
            ],
        }
        with (
            patch(
                "studio.services.production_visual_rules.verified_model_profile_specification",
                return_value=specification,
            ),
            patch("studio.services.production_visual_rules.driver_for", return_value=driver),
            self.assertRaises(ValidationError),
        ):
            validate_visual_rules_model_profile(profile)

    def test_response_schema_version_and_source_size_require_exact_integer_types(self):
        for field in ("schema_version", "source_size_bytes"):
            payload = self._payload()
            payload[field] = True
            with self.subTest(field=field), self.assertRaises(VisualRulesError) as captured:
                finalize_visual_rules(plan=self.plan, payload=payload)
            self.assertEqual(captured.exception.code, "visual_rules_source_mismatch")

    def test_strict_result_builds_three_reviewable_rules(self):
        frozen = finalize_visual_rules(plan=self.plan, payload=self._payload())
        self.assertEqual(frozen["kind"], "visual_rules_candidates")
        self.assertEqual(set(frozen["candidate"]), {
            "negative_prompt", "color_script", "continuity_summary",
        })
        negative = frozen["candidate"]["negative_prompt"].casefold()
        self.assertIn("costume drift", negative)
        self.assertIn("anachronistic props", negative)
        self.assertNotIn("identity drift", negative)
        self.assertIn("charcoal", frozen["candidate"]["color_script"])
        self.assertEqual(len(frozen["candidate_sha256"]), 64)

    def test_model_candidate_accepts_empty_negative_constraints(self):
        payload = self._payload()
        payload["negative_constraints"] = []

        frozen = finalize_visual_rules(plan=self.plan, payload=payload)

        self.assertEqual(frozen["kind"], "visual_rules_candidates")
        negative = frozen["candidate"]["negative_prompt"].casefold()
        self.assertEqual(negative, "costume drift")

    def test_review_atomically_applies_rules_and_preserves_other_settings(self):
        frozen = finalize_visual_rules(plan=self.plan, payload=self._payload())
        reviewed = dict(frozen["candidate"])
        reviewed["continuity_summary"] += " Preserve eyeline continuity."
        evidence = apply_visual_rules_candidate(
            project=self.project,
            revision=self.revision,
            frozen=frozen,
            reviewed_rules=reviewed,
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, reviewed["continuity_summary"])
        self.assertEqual(
            self.project.production_settings["negative_prompt"],
            reviewed["negative_prompt"],
        )
        self.assertEqual(self.project.production_settings["default_conditioning_mode"], "ref2va")
        self.assertEqual(
            self.project.production_settings["translation_glossary"],
            {"林默": "Lin Mo"},
        )
        self.assertEqual(evidence["modified_fields"], ["continuity_summary"])
        self.assertEqual(evidence["advisories"], [])

    def test_bible_drift_rejects_old_candidate_without_writing(self):
        frozen = finalize_visual_rules(plan=self.plan, payload=self._payload())
        character = CharacterBible.objects.get(project=self.project)
        character.wardrobe = "白色风衣"
        character.save(update_fields=("wardrobe", "updated_at"))
        with self.assertRaises(VisualRulesConflict):
            apply_visual_rules_candidate(
                project=self.project,
                revision=self.revision,
                frozen=frozen,
                reviewed_rules=frozen["candidate"],
            )
        self.project.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, "")

    def test_review_can_remove_recommended_negative_clauses(self):
        reviewed = validate_reviewed_rules({
            "negative_prompt": "watermark",
            "color_script": "Muted charcoal palette with low-key lighting.",
            "continuity_summary": "Keep visual identity consistent.",
        })

        self.assertEqual(reviewed["negative_prompt"], "watermark")

    def test_review_can_remove_all_recommended_negative_clauses(self):
        reviewed = validate_reviewed_rules({
            "negative_prompt": "",
            "color_script": "Muted charcoal palette with low-key lighting.",
            "continuity_summary": "Keep visual identity consistent.",
        })

        self.assertEqual(reviewed["negative_prompt"], "")

    def test_non_empty_reviewed_negative_prompt_keeps_character_validation(self):
        with self.assertRaises(VisualRulesError) as captured:
            validate_reviewed_rules({
                "negative_prompt": "watermark\x00",
                "color_script": "Muted charcoal palette with low-key lighting.",
                "continuity_summary": "Keep visual identity consistent.",
            })

        self.assertEqual(captured.exception.code, "negative_prompt_text_invalid")

    def test_review_omitted_recommendations_are_audited_without_blocking(self):
        plan = dict(self.plan)
        plan["resource_estimate"] = {
            **self.plan["resource_estimate"],
            "quality_advisories": {
                "schema_version": 1,
                "recommended_negative_clauses": list(
                    RECOMMENDED_NEGATIVE_CLAUSES,
                ),
            },
        }
        frozen = finalize_visual_rules(plan=plan, payload=self._payload())
        reviewed = dict(frozen["candidate"])
        reviewed["negative_prompt"] = "watermark"

        evidence = apply_visual_rules_candidate(
            project=self.project,
            revision=self.revision,
            frozen=frozen,
            reviewed_rules=reviewed,
        )

        self.assertEqual(
            evidence["advisories"][0]["code"],
            "recommended_negative_clauses_omitted",
        )
        self.assertIn("readable text", evidence["advisories"][0]["omitted"])

    def test_review_can_remove_every_recommendation_and_persist_warning(self):
        plan = dict(self.plan)
        plan["resource_estimate"] = {
            **self.plan["resource_estimate"],
            "quality_advisories": {
                "schema_version": 1,
                "recommended_negative_clauses": list(
                    RECOMMENDED_NEGATIVE_CLAUSES,
                ),
            },
        }
        frozen = finalize_visual_rules(plan=plan, payload=self._payload())
        reviewed = dict(frozen["candidate"])
        reviewed["negative_prompt"] = ""

        evidence = apply_visual_rules_candidate(
            project=self.project,
            revision=self.revision,
            frozen=frozen,
            reviewed_rules=reviewed,
        )

        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings["negative_prompt"], "")
        self.assertEqual(len(evidence["advisories"]), 1)
        self.assertEqual(
            evidence["advisories"][0]["code"],
            "recommended_negative_clauses_omitted",
        )
        self.assertEqual(
            set(evidence["advisories"][0]["omitted"]),
            set(RECOMMENDED_NEGATIVE_CLAUSES),
        )

    def test_negative_constraint_rejects_multiple_clauses_in_one_array_item(self):
        payload = self._payload()
        payload["negative_constraints"] = ["identity drift, wardrobe change"]

        with self.assertRaisesMessage(Exception, "不得包含列表分隔符") as captured:
            finalize_visual_rules(plan=self.plan, payload=payload)

        self.assertEqual(captured.exception.code, "negative_constraint_delimiter_invalid")


class VisualRulesTextContractRetryTests(TestCase):
    """Regression coverage for finite, audit-bound visual-rules retries."""

    def setUp(self):
        self.user, self.project, *_ = build_story("visual-rules-contract-retry")
        self.recipe = ProductionRecipeVersion(
            recipe_key="visual-rules-contract-retry",
            version=1,
            name="Visual rules contract retry",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(self.recipe, "definition", {"schema_version": 1})
        self.recipe.save()

        empty = json_contract({"schema_version": 1})
        self.run = ProductionRun.objects.create(
            owner=self.user,
            project=self.project,
            recipe_version=self.recipe,
            idempotency_key="visual-rules-contract-retry-run",
            request_sha256="a" * 64,
            status=ProductionRun.Status.RUNNING,
            allow_external_calls=True,
            max_generation_tasks=10,
            config_snapshot=empty.value,
            config_snapshot_schema_version=empty.schema_version,
            config_snapshot_size_bytes=empty.size_bytes,
            config_snapshot_sha256=empty.sha256,
            input_snapshot=empty.value,
            input_snapshot_schema_version=empty.schema_version,
            input_snapshot_size_bytes=empty.size_bytes,
            input_snapshot_sha256=empty.sha256,
        )
        self.route = {
            "index": 0,
            "model_deployment_id": "11111111-1111-1111-1111-111111111111",
            "is_external": True,
        }
        routing = json_contract({
            "schema_version": 1,
            "mode": "fixed",
            "routes": [self.route],
            "variant_count": 1,
            "evaluator": None,
        })
        frozen_config = json_contract({
            "schema_version": 1,
            "base": {
                "values": {"production.text_contract_retry_limit": 2},
            },
            "routes": {
                "0": {
                    "values": {"production.text_contract_retry_limit": 2},
                },
            },
        })
        self.stage = StageRun.objects.create(
            production_run=self.run,
            stage_key="visual_rules_generate",
            ordinal=1,
            mode="ai",
            status=StageRun.Status.RUNNING,
            idempotency_key=f"{self.run.id}:1:visual_rules_generate",
            routing=routing.value,
            routing_schema_version=routing.schema_version,
            routing_size_bytes=routing.size_bytes,
            routing_sha256=routing.sha256,
            routing_decision=empty.value,
            routing_decision_schema_version=empty.schema_version,
            routing_decision_size_bytes=empty.size_bytes,
            routing_decision_sha256=empty.sha256,
            config_snapshot=frozen_config.value,
            config_snapshot_schema_version=frozen_config.schema_version,
            config_snapshot_size_bytes=frozen_config.size_bytes,
            config_snapshot_sha256=frozen_config.sha256,
            input_snapshot=empty.value,
            input_snapshot_schema_version=empty.schema_version,
            input_snapshot_size_bytes=empty.size_bytes,
            input_snapshot_sha256=empty.sha256,
            output_snapshot=empty.value,
            output_snapshot_schema_version=empty.schema_version,
            output_snapshot_size_bytes=empty.size_bytes,
            output_snapshot_sha256=empty.sha256,
            selected_route_index=0,
        )
        self.frozen_plan = {
            "schema_version": 1,
            "source_revision": {"id": "frozen-script"},
            "project_context": {"confirmed_bible_sha256": "b" * 64},
            "request": {"source_text": "frozen full script"},
        }

    def _failed_attempt(
        self,
        attempt_number: int,
        *,
        classifier: str = "response_output_budget_exhausted",
        finish_reason: str = "length",
    ) -> tuple[GenerationAttempt, GenerationAttemptResolution]:
        snapshot = json_contract({
            "schema_version": 1,
            "plan": self.frozen_plan,
            "route": self.route,
        })
        attempt = GenerationAttempt.objects.create(
            stage_run=self.stage,
            unit_key=VISUAL_RULES_UNIT_KEY,
            attempt_number=attempt_number,
            status=GenerationAttempt.Status.FAILED,
            route_index=0,
            operation=VISUAL_RULES_OPERATION,
            is_external=True,
            request_sha256=snapshot.sha256,
            request_snapshot=snapshot.value,
            request_snapshot_schema_version=snapshot.schema_version,
            request_snapshot_size_bytes=snapshot.size_bytes,
            request_snapshot_sha256=snapshot.sha256,
            error_code="provider_response_invalid",
            error_message="safe provider response rejection",
        )
        evidence = json_contract({
            "schema_version": 1,
            "attempt_id": str(attempt.id),
            "request_sha256": attempt.request_sha256,
            "failure_code": attempt.error_code,
            "contract_error_code": classifier,
            "finish_reason": finish_reason,
        })
        resolution = GenerationAttemptResolution.objects.create(
            attempt=attempt,
            revision=1,
            outcome=(
                GenerationAttemptResolution.Outcome.KNOWN_RESPONSE_REJECTED
            ),
            evidence=evidence.value,
            evidence_schema_version=evidence.schema_version,
            evidence_size_bytes=evidence.size_bytes,
            evidence_sha256=evidence.sha256,
        )
        return attempt, resolution

    def test_budget_exhaustion_queues_same_fixed_route_with_audit_bound_retry(self):
        first, resolution = self._failed_attempt(1)

        with patch(
            "studio.services.production_runs._queue_visual_rules_attempt_for_route",
        ) as queue_retry:
            result = _prepare_visual_rules(self.run, self.stage)

        self.assertIsNone(result)
        queue_retry.assert_called_once()
        args, kwargs = queue_retry.call_args
        self.assertEqual(args, (self.run, self.stage))
        self.assertEqual(kwargs["route"], self.route)
        self.assertEqual(kwargs["attempt_number"], 2)
        self.assertEqual(kwargs["reason"], "known_text_contract_retry")
        self.assertEqual(kwargs["frozen_plan"], self.frozen_plan)
        self.assertEqual(kwargs["retry_contract"], {
            "schema_version": 1,
            "config_key": "production.text_contract_retry_limit",
            "retry_limit": 2,
            "max_attempts": 3,
            "attempts_used": 1,
            "repair_of_attempt_id": str(first.id),
            "contract_error_code": "response_output_budget_exhausted",
            "resolution_id": str(resolution.id),
            "resolution_evidence_sha256": resolution.evidence_sha256,
        })
        route_event = self.stage.route_events.get()
        self.assertEqual(route_event.event_type, "known_text_contract_retry_queued")
        self.assertEqual(route_event.route_index, 0)
        self.assertEqual(route_event.code, "response_output_budget_exhausted")

    def test_second_known_rejection_queues_final_allowed_attempt(self):
        self._failed_attempt(1)
        second, resolution = self._failed_attempt(2)

        with patch(
            "studio.services.production_runs._queue_visual_rules_attempt_for_route",
        ) as queue_retry:
            result = _prepare_visual_rules(self.run, self.stage)

        self.assertIsNone(result)
        queue_retry.assert_called_once()
        retry_call = queue_retry.call_args.kwargs
        self.assertEqual(retry_call["attempt_number"], 3)
        self.assertEqual(retry_call["route"], self.route)
        self.assertEqual(retry_call["frozen_plan"], self.frozen_plan)
        self.assertEqual(retry_call["retry_contract"]["attempts_used"], 2)
        self.assertEqual(
            retry_call["retry_contract"]["repair_of_attempt_id"],
            str(second.id),
        )
        self.assertEqual(
            retry_call["retry_contract"]["resolution_id"],
            str(resolution.id),
        )
        self.assertEqual(
            retry_call["retry_contract"]["resolution_evidence_sha256"],
            resolution.evidence_sha256,
        )

    def test_third_known_rejection_stops_without_queue_and_requires_human(self):
        self._failed_attempt(1)
        self._failed_attempt(2)
        self._failed_attempt(3)

        with (
            patch(
                "studio.services.production_runs._queue_visual_rules_attempt_for_route",
            ) as queue_retry,
            self.assertRaises(StageBlocked) as captured,
        ):
            _prepare_visual_rules(self.run, self.stage)

        self.assertEqual(
            captured.exception.code,
            "text_contract_human_confirmation_required",
        )
        self.assertIn("首次调用 + 2 次自动重试", str(captured.exception))
        queue_retry.assert_not_called()
        self.assertFalse(self.stage.route_events.exists())

    def test_content_filter_never_auto_retries_and_requires_human(self):
        self._failed_attempt(
            1,
            classifier="response_content_filtered",
            finish_reason="content_filter",
        )

        with (
            patch(
                "studio.services.production_runs._queue_visual_rules_attempt_for_route",
            ) as queue_retry,
            self.assertRaises(StageBlocked) as captured,
        ):
            _prepare_visual_rules(self.run, self.stage)

        self.assertEqual(
            captured.exception.code,
            "text_contract_human_confirmation_required",
        )
        self.assertIn("内容过滤", str(captured.exception))
        queue_retry.assert_not_called()
        self.assertFalse(self.stage.route_events.exists())


class VisualRulesSmokeFailureEvidenceTests(TestCase):
    def setUp(self):
        self.plan = build_visual_rules_smoke_plan(
            model_identifier="fixture-model",
            policy={
                "max_input_bytes": 64_000,
                "max_output_tokens": 2_048,
                "timeout_seconds": 30,
                "chat_request_options": _visual_chat_request_options(),
            },
        )
        self.request_snapshot = {
            "payload": visual_rules_provider_payload(self.plan),
            "plan": self.plan,
            "plan_sha256": json_contract(self.plan).sha256,
        }
        self.deployment = SimpleNamespace(provider=object())

    def test_smoke_uses_full_published_output_budget(self):
        self.assertEqual(self.plan["request"]["max_output_tokens"], 2_048)
        self.assertEqual(
            self.plan["resource_estimate"]["configured_output_token_hint"],
            2_048,
        )
        self.assertEqual(self.request_snapshot["payload"]["max_tokens"], 2_048)

    def test_empty_length_response_has_safe_budget_classifier(self):
        deployment = SimpleNamespace(
            profile_version=SimpleNamespace(model_identifier="fixture-model"),
            credential=None,
            provider=SimpleNamespace(credential=None),
        )
        response = {
            "choices": [{
                "finish_reason": "length",
                "message": {
                    "content": "",
                    "reasoning_content": "provider-authored secret reasoning",
                },
            }],
        }
        with (
            patch(
                "studio.services.provider_registry._combined_config",
                return_value={
                    "base_url": "http://localhost",
                    "chat_path": "/v1/chat/completions",
                    "timeout_seconds": 30,
                },
            ),
            patch(
                "studio.services.provider_registry.validate_endpoint",
                return_value="http://localhost",
            ),
            patch(
                "studio.services.provider_registry._request_json",
                return_value=response,
            ),
            self.assertRaises(ProviderDriverError) as captured,
        ):
            _invoke_chat_completion(
                deployment,
                {
                    "messages": [{"role": "user", "content": "fixture"}],
                    "chat_request_options": {},
                },
                credential_optional=True,
            )

        self.assertEqual(captured.exception.code, "provider_response_invalid")
        self.assertEqual(
            captured.exception.metadata["contract_error_code"],
            "response_output_budget_exhausted",
        )
        self.assertNotIn(
            "provider-authored secret reasoning",
            json.dumps(captured.exception.metadata),
        )

    def test_reasoning_without_final_text_has_safe_classifier(self):
        deployment = SimpleNamespace(
            profile_version=SimpleNamespace(model_identifier="fixture-model"),
            credential=None,
            provider=SimpleNamespace(credential=None),
        )
        response = {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": "",
                    "reasoning_content": "provider-authored secret reasoning",
                },
            }],
        }
        with (
            patch(
                "studio.services.provider_registry._combined_config",
                return_value={
                    "base_url": "http://localhost",
                    "chat_path": "/v1/chat/completions",
                    "timeout_seconds": 30,
                },
            ),
            patch(
                "studio.services.provider_registry.validate_endpoint",
                return_value="http://localhost",
            ),
            patch(
                "studio.services.provider_registry._request_json",
                return_value=response,
            ),
            self.assertRaises(ProviderDriverError) as captured,
        ):
            _invoke_chat_completion(
                deployment,
                {
                    "messages": [{"role": "user", "content": "fixture"}],
                    "chat_request_options": {},
                },
                credential_optional=True,
            )

        self.assertEqual(
            captured.exception.metadata["contract_error_code"],
            "response_reasoning_without_text",
        )
        self.assertNotIn(
            "provider-authored secret reasoning",
            json.dumps(captured.exception.metadata),
        )

    def test_driver_reason_is_persisted_as_safe_code_without_message(self):
        failure = ProviderDriverError(
            "provider raw response TOP-SECRET",
            code="provider_response_invalid",
            submission_uncertain=False,
        )
        failure.metadata = {"contract_error_code": "response_missing_text"}
        driver = SimpleNamespace(invoke=lambda *_args: (_ for _ in ()).throw(failure))

        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            result = execute_visual_rules_smoke_request(
                self.deployment,
                self.request_snapshot,
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["evidence"]["contract_error_code"], "response_missing_text")
        persisted = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("TOP-SECRET", persisted)
        self.assertNotIn("provider raw response", persisted)

    def test_invalid_enum_records_exact_safe_contract_reason_without_response(self):
        response_payload = {
            "schema_version": 1,
            "source_revision_id": self.plan["source_revision"]["id"],
            "source_sha256": self.plan["source_revision"]["content_sha256"],
            "source_size_bytes": self.plan["source_revision"]["content_size_bytes"],
            "color_design": {
                "base_colors": ["teal", "slate"],
                "accent_color": "provider-secret-purple",
                "palette_relation": "cinematic",
                "lighting": "soft_diffuse",
                "contrast": "restrained",
                "saturation": "muted",
                "exposure": "balanced",
                "progression": "stable",
                "preserve_natural_skin_exposure": True,
            },
            "negative_constraints": ["identity drift"],
            "continuity_summary": "Keep the frozen visual identities consistent.",
        }
        driver = SimpleNamespace(invoke=lambda *_args: {
            "schema_version": 1,
            "request_id": "provider-secret-request-id",
            "text": json.dumps(response_payload),
            "usage": {},
        })

        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            result = execute_visual_rules_smoke_request(
                self.deployment,
                self.request_snapshot,
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "visual_rules_response_invalid")
        self.assertEqual(result["evidence"]["contract_error_code"], "color_design_enum_invalid")
        persisted = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("provider-secret-purple", persisted)
        self.assertNotIn("provider-secret-request-id", persisted)

    def test_invalid_continuity_text_records_field_specific_safe_reason(self):
        response_payload = {
            "schema_version": 1,
            "source_revision_id": self.plan["source_revision"]["id"],
            "source_sha256": self.plan["source_revision"]["content_sha256"],
            "source_size_bytes": self.plan["source_revision"]["content_size_bytes"],
            "color_design": {
                "base_colors": ["teal", "slate"],
                "accent_color": "amber",
                "palette_relation": "cinematic",
                "lighting": "soft_diffuse",
                "contrast": "restrained",
                "saturation": "muted",
                "exposure": "balanced",
                "progression": "stable",
                "preserve_natural_skin_exposure": True,
            },
            "negative_constraints": ["identity drift"],
            "continuity_summary": "<script>provider-secret</script>",
        }
        driver = SimpleNamespace(invoke=lambda *_args: {
            "schema_version": 1,
            "request_id": "",
            "text": json.dumps(response_payload),
            "usage": {},
        })

        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            result = execute_visual_rules_smoke_request(
                self.deployment,
                self.request_snapshot,
            )

        self.assertEqual(
            result["evidence"]["contract_error_code"],
            "continuity_summary_text_invalid",
        )
        self.assertNotIn("provider-secret", json.dumps(result))

    def test_untrusted_driver_metadata_cannot_enter_failure_evidence(self):
        failure = ProviderDriverError(
            "rejected",
            code="provider_response_invalid",
            submission_uncertain=False,
        )
        failure.metadata = {"contract_error_code": "secret=value from raw response"}
        driver = SimpleNamespace(invoke=lambda *_args: (_ for _ in ()).throw(failure))

        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            result = execute_visual_rules_smoke_request(
                self.deployment,
                self.request_snapshot,
            )

        self.assertEqual(
            result["evidence"]["contract_error_code"],
            "provider_contract_validation_failed",
        )
        self.assertNotIn("secret=value", json.dumps(result))
