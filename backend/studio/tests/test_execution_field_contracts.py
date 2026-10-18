from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.execution_field_contracts import (
    ExecutionFieldInput, freeze_execution_field_request, validate_execution_field, validate_execution_field_response,
    verify_execution_field_request,
)
from studio.services.json_contracts import JsonContract, canonical_json_bytes, json_contract
from studio.services.prompt_normalization_contract import PromptNormalizationContractError
from studio.tests.field_definition_fixtures import frozen_field_definition


def field_rule(**changes):
    return {
        "field_definition": frozen_field_definition("direction"),
        "field_key": "direction", "field_value_type": "long_text", "fixed_value": None, "has_default": False, "default_value": None,
        "field_value_options": [], "source_selector": "request.text",
        "channel": "positive_prompt", "position": 1, "label_style": "label_block",
        "separator": "newline", "required": True, "omit_if_empty": False, "max_chars": 7000,
        "generation_mode": "model_generated", "generation_source_selector": "request.action",
        "generation_instruction": "Describe only the supplied visible facts.",
        "generation_output_semantics": "final", "generation_required": True, "language_policy": "en",
        "length_unit": "none", "generation_min": None, "generation_max": None,
        "generation_validator_keys": ["non_empty", "no_control_characters", "no_unresolved_placeholders", "protected_tokens_preserve_count"],
        "generation_context_selectors": ["target_shot.core"], "projection_primitive": "", **changes,
    }


class ExecutionFieldContractTests(SimpleTestCase):
    def setUp(self):
        self.profile = SimpleNamespace(pk=uuid4(), status="published", contract_sha256="a" * 64)
        self.contract = {"schema_version": 8, "operation": "video.generate", "rules": [field_rule()]}
        self.entry = ExecutionFieldInput(
            request_id="request-one", unit_key="asset-unit-one", field_key="direction",
            source_text="A person walks past a window.", source_language="English", target_language="English",
            context_facts={"target_shot.core": {"action": "Walk past a window"}},
            reference_facts={"protected_tokens": [], "logical_inputs": [], "subject_facts": []},
        )
        self.verify = self.enterContext(patch("studio.services.execution_field_contracts.verify_prompt_profile_contract", return_value=self.contract))
        self.enterContext(patch("studio.services.execution_instances.boundary_int", return_value=65536))
        self.enterContext(patch("studio.services.execution_request_storage.boundary_int", return_value=8 * 1024 * 1024))
        self.enterContext(patch("studio.services.prompt_normalization_contract.boundary_int", return_value=65536))

    def freeze(self, *entries, operation="prompt.fields.generate"):
        return freeze_execution_field_request(operation=operation, profile=self.profile, inputs=entries or [self.entry])

    def response(self, frozen=None, text="A person walks past a window."):
        return validate_execution_field_response(frozen or self.freeze(), {"items": [{"request_id": "request-one", "text": text}]})

    def validate_text(self, text, **changes):
        kwargs = dict(field_contract=field_rule(), profile_field_keys=["direction"],
            source_text=self.entry.source_text, source_language="English", target_language="English",
            protected_tokens=[], request_id="manual-field", text=text)
        return validate_execution_field(**{**kwargs, **changes})

    def test_manual_revision_and_model_response_use_identical_pure_validation(self):
        text = "A person walks past a window."
        self.assertEqual(self.validate_text(text), self.response(text=text)["warnings"])
        for invalid in ("", "A person\u0000walks."):
            with self.subTest(invalid=invalid), self.assertRaises((ValidationError, PromptNormalizationContractError)):
                self.validate_text(invalid)
        self.assertNotIn("approved", self.response(text=text))

    def test_strategy_prose_is_not_an_execution_grammar(self):
        self.contract["rules"][0]["generation_validator_keys"] += ["no_shot_headers", "single_line", "unique_items"]
        self.contract["rules"][0].update(length_unit="items", generation_min=1)
        for text in (
            "人物经过窗户。", "<Subject 2> turns toward the window.",
            "[Shot 2] English direction.\n<d>[Chinese] 请进。</d>",
            "sequence: Act II — こんにちは。", "A person {{unknown}}. <image 9>",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.response(text=text)["result"]["items"][0]["text"], text)
                self.assertEqual(self.validate_text(text), [])

    def test_literal_use_instructions_do_not_reject_generated_or_manual_prose(self):
        source = "Keep <Picture 1>."
        for text in ("Remove the picture.", "<Picture 1> and <Picture 1>."):
            self.assertEqual(self.validate_text(text, source_text=source, protected_tokens=["<Picture 1>"]), [])
        # Malformed request facts remain errors, independent of prompt wording.
        with self.assertRaises(ValidationError):
            self.validate_text(source, field_contract=field_rule(language_policy="inherit"), target_language="")

    def test_manual_value_must_match_declared_field_and_bounded_text(self):
        for changes in ({"profile_field_keys": []}, {"text": {}}, {"field_contract": field_rule(max_chars=3)},
                        {"protected_tokens": ["not in source"]}, {"text": "broken\ud800"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.validate_text(**{"text": "A person walks.", **changes})

    def test_language_guidance_is_frozen_but_never_classifies_output(self):
        text = "English direction；人物说：欢迎。日本語。"
        for policy in ("en", "zh-CN", "any", "preserve_source", "inherit", "bilingual:zh-CN+en"):
            with self.subTest(policy=policy):
                self.assertEqual(self.validate_text(text, field_contract=field_rule(language_policy=policy),
                    source_language="Chinese", target_language="English"), [])

    def test_exact_rule_frozen_without_orm_owner_or_equal_operation(self):
        frozen = self.freeze()
        self.assertEqual(frozen.value["operation"], "prompt.fields.generate")
        self.assertEqual(self.contract["operation"], "video.generate")
        self.assertEqual(frozen.value["items"][0]["unit_key"], "asset-unit-one")
        self.assertEqual(frozen.value["items"][0]["field_contract"], self.contract["rules"][0])
        self.contract["rules"][0]["generation_instruction"] = "changed source object"
        self.entry.context_facts["target_shot.core"]["action"] = "changed fact object"
        self.assertNotIn("changed", str(frozen.value))
        result = self.response(frozen)
        self.assertEqual(result["warnings"], [])
        self.assertNotIn("approved", result)
        self.assertEqual(self.verify.call_count, 1)  # replay never loads current Profile

    def test_field_contract_cannot_be_injected_by_plain_caller_dict(self):
        with self.assertRaisesMessage(ValidationError, "来源适配器"):
            freeze_execution_field_request(operation="prompt.fields.generate", profile=self.profile, inputs=[asdict(self.entry)])
        with self.assertRaisesMessage(ValidationError, "未声明"):
            self.freeze(replace(self.entry, field_key="invented"))

    def test_published_and_current_contract_are_required(self):
        for state in ("draft", "retired"):
            self.profile.status = state
            with self.assertRaisesMessage(ValidationError, "已发布"):
                self.freeze()
        self.profile.status = "published"
        self.contract["schema_version"] = 5
        with self.assertRaisesMessage(ValidationError, "不转换历史"):
            self.freeze()

    def test_operation_is_not_borrowed_or_guessed_from_generation_mode(self):
        for operation in ("prompt.normalize", "script.generate", "prompt.fields.translate", [], None):
            with self.assertRaises(ValidationError):
                self.freeze(operation=operation)
        self.contract["rules"][0]["generation_mode"] = "translate_only"
        self.freeze(operation="prompt.fields.translate")
        with self.assertRaisesMessage(ValidationError, "来源正文"):
            self.freeze(replace(self.entry, source_text=""), operation="prompt.fields.translate")

    def test_unknown_or_incomplete_context_is_not_filled_or_silently_dropped(self):
        for context in ({}, {"all_shots.core": []}, {**self.entry.context_facts, "project.core": {}}):
            with self.assertRaisesMessage(ValidationError, "精确对应"):
                self.freeze(replace(self.entry, context_facts=context))

    def test_language_is_explicit_when_inherited_or_preserved(self):
        for policy, field in (("inherit", "target_language"), ("preserve_source", "source_language")):
            self.contract["rules"][0]["language_policy"] = policy
            with self.assertRaises(ValidationError):
                self.freeze(replace(self.entry, **{field: ""}))

    def test_request_identity_and_unit_field_pair_are_unique(self):
        for second in (self.entry, replace(self.entry, request_id="request-two")):
            with self.assertRaises(ValidationError):
                self.freeze(self.entry, second)
        self.freeze(self.entry, replace(self.entry, unit_key="asset-unit-two", request_id="request-two"))

    def test_response_reordered_by_exact_ids_not_array_position(self):
        second = replace(self.entry, unit_key="asset-unit-two", request_id="request-two")
        result = validate_execution_field_response(self.freeze(self.entry, second), {"items": [
            {"request_id": "request-two", "text": "A second person sits."},
            {"request_id": "request-one", "text": "A first person walks."},
        ]})
        self.assertEqual([row["request_id"] for row in result["result"]["items"]], ["request-one", "request-two"])
        self.assertEqual(result["result"]["items"][0]["text"], "A first person walks.")

    def test_partial_duplicate_extra_ids_and_extra_response_fields_rejected(self):
        frozen = self.freeze(self.entry, replace(self.entry, unit_key="asset-unit-two", request_id="request-two"))
        row = {"request_id": "request-one", "text": "A person walks."}
        invalid = [
            {"items": [row]}, {"items": [row, row]},
            {"items": [row, {**row, "request_id": "other"}]},
            {"items": [row, {"request_id": "request-two", "translation": "Text"}]},
            {"items": [row, {"request_id": "request-two", "text": 42}]},
            {"items": [], "fallback": "success"}, "Plain text is not structured output",
        ]
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValidationError):
                validate_execution_field_response(frozen, response)

    def test_generic_text_safety_remains_enforced(self):
        for text in ("A\u200b person walks.", ""):
            with self.subTest(text=text), self.assertRaises(PromptNormalizationContractError):
                self.response(text=text)

    def test_field_headings_and_new_marker_syntax_are_not_rewritten(self):
        text = "direction: A person <Picture 99> walks. {{authored notation}}"
        self.assertEqual(self.response(text=text)["result"]["items"][0]["text"], text)
        self.assertEqual(self.validate_text(text), [])

    def test_protected_text_count_is_guidance_not_admission(self):
        token = "<image 1>"
        entry = replace(self.entry, source_text=f"{token} walks. {token} turns.",
                        reference_facts={**self.entry.reference_facts, "protected_tokens": [token]})
        for text in ("A person walks.", f"{token} walks.", f"{token} walks. {token} turns."):
            self.assertEqual(self.response(self.freeze(entry), text)["result"]["items"][0]["text"], text)

    def test_unrequested_tokens_or_missing_reference_facts_rejected(self):
        for facts in ({}, {**self.entry.reference_facts, "protected_tokens": ["unknown"]},
                      {**self.entry.reference_facts, "logical_inputs": {}}):
            with self.assertRaises(ValidationError):
                self.freeze(replace(self.entry, reference_facts=facts))

    def test_hard_field_size_and_advisory_generation_length_are_distinct(self):
        rule = self.contract["rules"][0]
        rule.update(length_unit="english_words", generation_min=10, generation_max=20)
        self.assertEqual(self.response(text="Short description.")["warnings"][0]["code"], "recommended_length_out_of_range")
        rule["generation_validator_keys"].append("length_advisory")
        result = self.response(text="Short description.")
        self.assertEqual(result["warnings"][0]["code"], "recommended_length_out_of_range")
        rule["max_chars"] = 8
        with self.assertRaisesMessage(ValidationError, "硬字符"):
            self.response(text="Short description.")

    def test_no_whitespace_rewrite_and_optional_empty_is_not_failure(self):
        text = "  A person walks.\n"
        self.assertEqual(self.response(text=text)["result"]["items"][0]["text"], text)
        rule = self.contract["rules"][0]
        rule["generation_required"] = False
        rule["generation_validator_keys"].remove("non_empty")
        self.assertEqual(self.response(text="")["result"]["items"][0]["text"], "")

    def test_frozen_request_digest_and_storage_bound_cannot_be_bypassed(self):
        frozen = self.freeze()
        frozen.value["items"][0]["field_contract"]["language_policy"] = "any"
        with self.assertRaises(ValidationError):
            self.response(frozen)
        with patch("studio.services.execution_request_storage.boundary_int", return_value=65536), self.assertRaises(ValidationError):
            self.freeze(replace(self.entry, source_text="x" * 65536))
        with self.assertRaises(ValidationError):
            validate_execution_field_response(deepcopy(self.freeze().value), {"items": []})

    def test_snapshot_schema_must_match_the_registered_protocol(self):
        frozen = self.freeze()
        changed = JsonContract(frozen.value, 2, frozen.size_bytes, frozen.sha256)
        with self.assertRaises(ValidationError):
            self.response(changed)

    def test_long_generation_instruction_is_retained_in_explicit_field_contract(self):
        instruction = "Use only these facts. " + "Keep every constraint explicit. " * 150
        self.assertGreater(len(instruction), 4000)
        self.contract["rules"][0]["generation_instruction"] = instruction
        frozen = self.freeze()
        self.assertEqual(frozen.value["items"][0]["field_contract"]["generation_instruction"], instruction)
        self.assertEqual(verify_execution_field_request(frozen)["items"][0]["context_facts"], self.entry.context_facts)
        self.assertNotIn("target_prompt_contract", frozen.value)

    def test_large_request_uses_configured_boundary_not_fixed_64_kib(self):
        source = "源文" * 14000
        frozen = self.freeze(replace(self.entry, source_text=source))
        self.assertGreater(frozen.size_bytes, 65536)
        self.assertEqual(verify_execution_field_request(frozen)["items"][0]["source_text"], source)
        self.response(frozen)

    def test_common_facts_stored_once_per_unit_and_resolve_exactly(self):
        self.entry.reference_facts["logical_inputs"] = [{"name": "same image facts"}]
        self.entry.reference_facts["subject_facts"] = [{"description": "unique shared subject"}]
        self.contract["rules"].append(field_rule(field_key="summary", field_definition=frozen_field_definition("summary"), position=2))
        second = replace(self.entry, field_key="summary", request_id="request-two")
        frozen = self.freeze(self.entry, second)
        raw = canonical_json_bytes(frozen.value)
        self.assertEqual(raw.count(b"unique shared subject"), 1)
        self.assertEqual(len(frozen.value["shared_facts"]), 3)
        self.assertEqual(frozen.value["items"][0]["context_refs"], frozen.value["items"][1]["context_refs"])
        expanded = verify_execution_field_request(frozen)
        for item in expanded["items"]:
            self.assertEqual(item["reference_facts"], self.entry.reference_facts)
            self.assertEqual(item["context_facts"], self.entry.context_facts)
        self.assertLess(frozen.size_bytes, len(canonical_json_bytes(expanded)) + 500)
        self.assertIn("context_facts", asdict(self.entry))

    def test_cross_unit_missing_wrong_type_and_unused_references_rejected(self):
        second = replace(self.entry, unit_key="another-shot", request_id="request-two")
        frozen = self.freeze(self.entry, second)
        self.assertEqual(len(frozen.value["shared_facts"]), 6)
        first = frozen.value["items"][0]
        for ref in ("missing", frozen.value["items"][1]["reference_facts"]["subject_facts_ref"],
                    first["reference_facts"]["logical_inputs_ref"]):
            value = deepcopy(frozen.value)
            value["items"][0]["reference_facts"]["subject_facts_ref"] = ref
            with self.subTest(ref=ref), self.assertRaises(ValidationError):
                verify_execution_field_request(json_contract(value))
        value = deepcopy(frozen.value)
        value["shared_facts"]["f99"] = deepcopy(next(iter(value["shared_facts"].values())))
        with self.assertRaisesMessage(ValidationError, "未被字段使用"):
            verify_execution_field_request(json_contract(value))

    def test_legacy_batch_is_not_silently_rewritten_or_resent(self):
        legacy = verify_execution_field_request(self.freeze())
        legacy["schema_version"] = 1
        original = canonical_json_bytes(legacy)
        with self.assertRaisesMessage(ValidationError, "旧请求不自动转换"):
            verify_execution_field_request(json_contract(legacy))
        self.assertEqual(canonical_json_bytes(legacy), original)

    def test_explicit_field_word_boundaries_and_diagnostics_remain_exact(self):
        rule = self.contract["rules"][0]
        rule.update(length_unit="english_words", generation_min=140, generation_max=220)
        for count in (140, 220):
            with self.subTest(count=count):
                self.response(text=" ".join(["cinematic"] * count))
        for count in (139, 221):
            with self.subTest(count=count):
                warning = self.response(text=" ".join(["cinematic"] * count))["warnings"][0]
                self.assertEqual(warning["code"], "recommended_length_out_of_range")
                self.assertEqual(warning["measured"], count)
                self.assertEqual(warning["request_index"], 0)
        rule["generation_validator_keys"].append("length_advisory")
        result = self.response(text=" ".join(["cinematic"] * 139))
        self.assertEqual(result["warnings"][0]["code"], "recommended_length_out_of_range")
        self.assertEqual(result["warnings"][0]["measured"], 139)

    def test_explicit_json_shape_is_required_but_prose_repetition_is_not(self):
        self.contract["rules"][0].update(language_policy="any", length_unit="items",
            generation_min=2, generation_max=2,
            generation_validator_keys=["non_empty", "valid_json", "json_array", "unique_items"])
        self.response(text='["rain", "clock"]')
        self.response(text='["rain", "rain"]')
        for text, reason in (("not-json", "json_invalid"), ('{"rain":"clock"}', "json_type_invalid")):
            with self.subTest(text=text), self.assertRaises(PromptNormalizationContractError) as captured:
                self.response(text=text)
            self.assertEqual(captured.exception.reason_code, reason)

    def test_explicit_field_generic_validator_reason_codes_remain_enforced(self):
        self.contract["rules"][0]["generation_validator_keys"].append("single_line")
        for text, reason in (("", "empty"), ("A person\u200b walks.", "control_characters_invalid")):
            with self.subTest(reason=reason), self.assertRaises(PromptNormalizationContractError) as captured:
                self.response(text=text)
            self.assertEqual(captured.exception.reason_code, reason)

    def test_conflicting_protected_token_policies_remain_invalid(self):
        self.contract["rules"][0]["generation_validator_keys"].append("protected_tokens_at_least_once")
        with self.assertRaises((ValidationError, PromptNormalizationContractError)):
            self.freeze()
