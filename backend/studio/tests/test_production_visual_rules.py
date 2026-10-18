import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import (
    CharacterBible,
    SeriesProject,
)
from studio.services.production_visual_rules import COLOR_DESIGN_CHOICES, COLOR_VALUES, VisualRulesConflict, VisualRulesError, apply_visual_rules_candidate, build_visual_rules_plan, finalize_visual_rules, validate_visual_rules_model_profile, validate_reviewed_rules, visual_rules_provider_payload, visual_rules_resource_estimate
from studio.services.provider_registry import ProviderDriverError, _invoke_chat_completion
from studio.services.script_sources import confirm_script_revision, create_pasted_script_source
from studio.tests.factories import build_story
from studio.services.visual_rules_inputs import capture_visual_rules_baseline


def configured_plan(*, project, model_identifier, policy):
    inputs = {"logical_values": {"theme": "A traveler follows a mysterious letter.", "style": "Restrained cool realism."},
        "input_provenance": []}
    plan = build_visual_rules_plan(baseline=capture_visual_rules_baseline(project_id=project.pk, owner=project.owner,
        inputs=inputs), model_identifier=model_identifier, policy=policy, seed=42)
    plan["prompt_profile_assembly"] = {"channels": {"positive_prompt": inputs["logical_values"]["theme"] + " " +
        inputs["logical_values"]["style"]}, "structured_fields": {}}
    visual_rules_provider_payload(plan)
    return plan


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
    def test_json_schema_wire_format_uses_exact_business_output_without_rewriting_plan(self):
        from studio.services.business_outputs import write_output, output_reference
        from studio.services.data_structures import validate_data_structure_snapshot
        from studio.tests.output_structure_fixtures import response_structure
        output = write_output(actor=self.user, data={'name': 'Exact visual output', 'parser_key': 'visual_rules.v2',
            'data_structure_version': response_structure().pk})
        output = write_output(actor=self.user, output_id=output.pk, expected_revision=1, action='publish')
        plan = deepcopy(self.plan)
        plan['business_output'] = output_reference(output)
        plan['request']['chat_request_options'] = {'temperature': 0, 'response_format': {
            'type': 'json_schema', 'json_schema': {'strict': False}}}
        original = deepcopy(plan)
        payload = visual_rules_provider_payload(plan)
        metadata = payload['chat_request_options']['response_format']['json_schema']
        self.assertEqual(metadata['schema'], validate_data_structure_snapshot(plan['business_output']['data_structure']))
        self.assertFalse(metadata['strict'])
        self.assertEqual(plan, original)
        plan.pop('business_output')
        with self.assertRaisesMessage(ValidationError, '确切业务输出'):
            visual_rules_provider_payload(plan)

    def test_named_output_contract_drives_request_preview_and_finalizer_for_all_formats(self):
        self.assert_named_output_contract({'continuity_summary': 'rules', 'color_script': 'palette',
            'negative_prompt': 'avoid', 'camera_guidance': 'camera'})

    def test_nested_output_contract_drives_request_preview_and_finalizer_for_all_formats(self):
        self.assert_named_output_contract({'continuity_summary': 'visual.rules', 'color_script': 'visual.palette',
            'negative_prompt': 'constraints.avoid', 'camera_guidance': 'visual.camera'})

    def test_array_output_contract_drives_request_preview_and_finalizer_for_all_formats(self):
        self.assert_named_output_contract({'continuity_summary': 'rules.0.summary', 'color_script': 'rules.1.palette',
            'negative_prompt': 'rules.2.avoid', 'camera_guidance': 'rules.3.camera'})

    def assert_named_output_contract(self, configured_keys):
        from itertools import product
        from studio.services.business_outputs import write_output, output_reference
        from studio.tests.output_structure_fixtures import response_structure
        from studio.services.visual_rules_output_contract import visual_rule_field_source_options
        from studio.services.visual_rules_output_preview import preview_visual_rules_output
        options = visual_rule_field_source_options()
        self.assertEqual(set(configured_keys), {row['field'] for row in options})
        # Optional outputs need both selected and absent cases. Every selected
        # field must have an explicit response position in the same contract.
        for choices in product(*(([None, *row['choices']] if row.get('optional') else row['choices']) for row in options)):
            sources = {row['field']: choice['response_field'] for row, choice in zip(options, choices) if choice is not None}
            keys = {field: configured_keys[field] for field in sources}
            with self.subTest(sources=sources):
                quality = {'schema_version': 1, 'negative_constraints_max_items': 0,
                    **({'base_colors_max_items': 1} if sources['color_script'] == 'color_design' else {})}
                output = write_output(actor=self.user, data={'name': 'Named output', 'parser_key': 'visual_rules.v2',
                    'data_structure_version': response_structure(field_sources=sources, response_keys=keys).pk,
                    'quality_advisories': quality, 'field_sources': sources, 'response_keys': keys})
                output = write_output(actor=self.user, output_id=output.pk, expected_revision=1, action='publish')
                plan = deepcopy(self.plan)
                plan['business_output'] = output_reference(output)
                plan['resource_estimate']['quality_advisories'] = quality
                system = visual_rules_provider_payload(plan)['messages'][0]['content']
                sample = output.parser_contract['response_example']
                self.assertEqual(json.loads(system.split('CONTRACT=', 1)[1].split('\n', 1)[0]), sample)
                self.assertIn(keys['continuity_summary'] + ' MUST', system)
                self.assertIn(keys['negative_prompt'] + ' MUST', system)
                if sources['color_script'] == 'color_design':
                    enums = json.loads(system.split('OUTPUT_ENUMS=', 1)[1].split('\n', 1)[0])
                    self.assertTrue(all(key.startswith(keys['color_script'] + '.') for key in enums))
                else:
                    self.assertIn(keys['color_script'] + ' MUST', system)
                preview = preview_visual_rules_output(response_text=json.dumps(sample), field_sources=sources, response_keys=keys, quality_advisories=quality)
                result = finalize_visual_rules(plan=plan, payload=sample)
                self.assertEqual(preview['candidate'], result['candidate'])
                self.assertEqual(result['business_output'], plan['business_output'])
                self.assertEqual('camera_guidance' in result['candidate'], 'camera_guidance' in sources)
                if 'camera_guidance' in sources:
                    from studio.services.visual_rules_output_contract import visual_rule_response_value
                    self.assertEqual(result['candidate']['camera_guidance'],
                        visual_rule_response_value(sample, keys['camera_guidance']))
                self.assertIn(keys['negative_prompt'], [row['field'] for row in result['advisory_audit']])
                if sources['color_script'] == 'color_design':
                    self.assertIn(keys['color_script'] + '.base_colors', [row['field'] for row in result['advisory_audit']])
                with self.assertRaises(VisualRulesError):
                    finalize_visual_rules(plan=plan, payload={**sample, 'continuity_summary': 'Not an alias.'})

    def test_sample_preview_and_real_finalizer_parse_identically_without_bypassing_baseline(self):
        from studio.services.visual_rules_output_preview import preview_visual_rules_output
        from studio.services.visual_rules_output_contract import visual_rule_response_example
        quality = {"schema_version": 1, "continuity_summary_max_chars": 1,
            "recommended_negative_clauses": ["identity drift"]}
        self.plan["resource_estimate"]["quality_advisories"] = quality
        sample = visual_rule_response_example()
        report = preview_visual_rules_output(response_text=json.dumps(sample), quality_advisories=quality)
        result = finalize_visual_rules(plan=self.plan, payload=sample)
        self.assertEqual(report["candidate"], result["candidate"])
        self.assertEqual(report["warnings"], result["warnings"])
        prompt = visual_rules_provider_payload(self.plan)["messages"][0]["content"]
        self.assertEqual(json.loads(prompt.split("CONTRACT=", 1)[1].split("\nOUTPUT_ENUMS=", 1)[0]), sample)
        self.plan["business_input_sha256"] = "0" * 64
        with self.assertRaises(VisualRulesError):
            finalize_visual_rules(plan=self.plan, payload=sample)

    def test_output_editor_catalog_shares_validator_bounds_and_reviewed_readers(self):
        from studio.services.production_visual_rules import visual_rules_output_editor_options, _normalize_visual_rules_quality_advisories
        from studio.services.execution_source_readers import VISUAL_RULE_SOURCE_FIELDS
        from studio.services.visual_rules_output_contract import visual_rule_field_source_options
        from studio.platform_serializers import ModelProfileRegistryOptionsSerializer
        from studio.services.model_profile_options import model_profile_registry_options
        options = visual_rules_output_editor_options()
        self.assertEqual(ModelProfileRegistryOptionsSerializer(model_profile_registry_options()).data['visual_rules_output'], options)
        field_options = visual_rule_field_source_options()
        self.assertEqual({row['source_selector'] for row in options['outputs']},
            {'reviewed_visual_rules.' + row['field'] for row in field_options if not row.get('optional')})
        self.assertEqual({'reviewed_visual_rules.' + row['field'] for row in field_options}, set(VISUAL_RULE_SOURCE_FIELDS))
        self.assertEqual(options['color_choices']['base_colors'], list(COLOR_VALUES))
        for key, choices in COLOR_DESIGN_CHOICES.items():
            self.assertEqual(options['color_choices'][key], list(choices))
        with patch('studio.services.production_visual_rules._provider_response_max_bytes', return_value=80):
            bounded = visual_rules_output_editor_options()
            self.assertEqual(bounded['clauses_max_bytes'], 80)
            for row in bounded['number_fields']:
                for valid in (row['minimum'], row['maximum']):
                    self.assertEqual(_normalize_visual_rules_quality_advisories({'schema_version': 1, row['key']: valid})[row['key']], valid)
                for invalid in (row['minimum'] - 1, row['maximum'] + 1, True):
                    with self.assertRaises(ValidationError):
                        _normalize_visual_rules_quality_advisories({'schema_version': 1, row['key']: invalid})

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
        self.plan = configured_plan(
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
        return {
            "schema_version": 2,
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

    def test_only_configured_inputs_enter_request_not_script_bible_or_old_rules(self):
        provider_payload = visual_rules_provider_payload(self.plan)
        user_payload = json.loads(provider_payload["messages"][1]["content"])
        self.assertEqual(set(user_payload), {"prompt_profile"})
        rendered = json.dumps(provider_payload, ensure_ascii=False)
        self.assertNotIn(self.revision.content, rendered)
        self.assertNotIn(str(self.project.pk), rendered)
        self.assertNotIn("costume drift", rendered)
        self.assertNotIn("source_revision", rendered)
        self.assertNotIn("confirmed_bible", rendered)
        self.assertIn("Restrained cool realism.", rendered)

    def test_profile_snapshot_remains_in_plan_not_duplicated_in_provider_messages(self):
        assembly = self.plan["prompt_profile_assembly"]
        assembly["channels"].update(system_instruction="REVIEWED_STYLE_GUARD", negative_prompt="NO_UNIQUE_DEFECT")
        assembly["structured_fields"] = {
            "theme": assembly["channels"]["positive_prompt"],
            "system_guidance": "REVIEWED_STYLE_GUARD", "negative": "NO_UNIQUE_DEFECT",
        }
        original = deepcopy(assembly)
        payload = visual_rules_provider_payload(self.plan)
        user = json.loads(payload["messages"][1]["content"])
        self.assertEqual(set(user["prompt_profile"]), {"positive_prompt", "negative_prompt"})
        for text in original["structured_fields"].values():
            self.assertEqual(sum(message["content"].count(text) for message in payload["messages"]), 1)
        self.assertNotIn("REVIEWED_STYLE_GUARD", payload["messages"][1]["content"])
        self.assertEqual(self.plan["prompt_profile_assembly"], original)

    def test_field_snapshot_cannot_replace_missing_positive_channel(self):
        self.plan["prompt_profile_assembly"] = {"channels": {"system_instruction": "rules"},
            "structured_fields": {"theme": "must be rendered"}}
        with self.assertRaisesRegex(VisualRulesError, "缺少已配置的实际输入"):
            visual_rules_provider_payload(self.plan)

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
        plan = configured_plan(
            project=SeriesProject.objects.select_related("style").get(pk=self.project.pk),
            model_identifier="fixture-model",
            policy={
                "max_input_bytes": 64_000,
                "input_byte_warning_threshold": 1,
                "max_output_tokens": 131_072,
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
        self.assertNotIn("output_token_warning_threshold", plan["resource_estimate"])
        frozen = finalize_visual_rules(plan=plan, payload=self._payload())
        self.assertIn(
            "本次未截断",
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
        plan = configured_plan(
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
        for field in ("schema_version",):
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
        self.assertNotIn("costume drift", negative)
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
        self.assertEqual(negative, "")

    def test_review_atomically_applies_rules_and_preserves_other_settings(self):
        frozen = finalize_visual_rules(plan=self.plan, payload=self._payload())
        reviewed = dict(frozen["candidate"])
        reviewed["continuity_summary"] += " Preserve eyeline continuity."
        evidence = apply_visual_rules_candidate(
            project=self.project,
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

    def test_unselected_bible_change_does_not_invalidate_candidate(self):
        frozen = finalize_visual_rules(plan=self.plan, payload=self._payload())
        character = CharacterBible.objects.get(project=self.project)
        character.wardrobe = "白色风衣"
        character.save(update_fields=("wardrobe", "updated_at"))
        apply_visual_rules_candidate(project=self.project, frozen=frozen, reviewed_rules=frozen["candidate"])
        self.project.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, frozen["candidate"]["continuity_summary"])

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


class ChatCompletionResponseDiagnosticsTests(TestCase):
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
