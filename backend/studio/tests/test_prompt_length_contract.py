"""M6-06: compiler counts, frozen limits and original request share one path."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings

from studio.services.execution_prompt_display import _assembly_display
from studio.services.execution_prompts import normalize_compiled_profile_channels, profile_channel_lengths
from studio.services.prompt_profiles import PromptProfileChannelLengthError
from studio.tests import test_prompt_final_assembly_contract as fixtures
from studio.tests.test_execution_prompt_length_display import readonly


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class CompleteLengthContractTests(fixtures.AssemblyAssertions, TestCase):
    setUp = fixtures.MultiChannelFinalAssemblyTests.setUp
    positive_text = "中😀" * 1000
    extra_fragments = (("configured_system_section", "positive_prompt", "系统条件" * 300),)
    channel_limits = {"positive_prompt": {"max_chars": 4000, "warn_at_chars": 3000}}

    def test_successful_preview_and_frozen_request_keep_exact_counts_limits_and_versions(self):
        with connection.execute_wrapper(readonly):
            before = self.display()
        rows = {row["channel"]: row for row in before["length_diagnostics"]}
        positive = rows["positive_prompt"]
        self.assertEqual(positive["fragments"][0]["text_chars"], 2000)
        self.assertEqual(positive["fragments"][1]["text_chars"], 1200)
        self.assertGreater(positive["character_count"], 3200)  # configured separators count too
        self.assertEqual(sum(row["text_chars"] + row["separator_chars"] for row in positive["fragments"]), positive["character_count"])
        self.assertEqual(positive["max_chars"], 4000)
        self.assertEqual(positive["over_by"], 0)
        self.assertIn({"source": f"profile.{self.prompt_profile.pk}.positive_prompt", "max_chars": 4000}, positive["limit_sources"])
        self.assertIsNone(rows["negative_prompt"]["max_chars"])
        expected = {rule.field_definition.key: str(rule.field_definition_version_id)
            for rule in self.prompt_profile.field_rules.select_related("field_definition")}
        for row in before["length_diagnostics"]:
            for fragment in row["fragments"]:
                self.assertEqual(fragment["field_definition_version_id"], expected[fragment["field_key"]])
                self.assertEqual(fragment["source_selector"], "fixed.value")
                self.assertEqual(fragment["max_chars"], 2000)
        self.assert_same_frozen_assembly()
        with patch("studio.services.execution_prompt_display._compile_preparation_profile", side_effect=AssertionError("no recompile")):
            with connection.execute_wrapper(readonly):
                after = self.display()
        self.assertEqual(before["length_diagnostics"], after["length_diagnostics"])
        self.assert_no_dispatch()

    def test_target_limit_is_stricter_than_profile_and_normalizer_still_rejects(self):
        compilation = self.preview().compilation
        configuration = deepcopy(self.definition.value["interface"]["prompt"])
        target = configuration["channels"]["positive_prompt"]
        target["rule"]["max_length"] = 2500
        target["layers"][0]["rule"]["max_length"] = 2500
        with self.assertRaises(PromptProfileChannelLengthError) as rejected:
            normalize_compiled_profile_channels(configuration, compilation, declared_reference_labels=[])
        evidence = rejected.exception.diagnostic
        rows = profile_channel_lengths(channels=evidence["channels"], lengths=evidence["lengths"], configuration=configuration)
        positive = next(row for row in rows if row["channel"] == "positive_prompt")
        self.assertEqual(positive["max_chars"], 2500)
        self.assertEqual(positive["over_by"], len(evidence["channels"]["positive_prompt"]) - 2500)
        self.assertEqual(positive["limit_sources"][0]["max_chars"], 2500)
        self.assertEqual(compilation.assembly["channel_limits"]["positive_prompt"]["max_chars"], 4000)
        self.assert_no_dispatch()

    def test_missing_or_inconsistent_saved_breakdown_is_not_rebuilt(self):
        result = self.preview()
        channels = result.resolved_channels["values"]
        configuration = self.definition.value["interface"]["prompt"]
        for change in ("missing", "wrong_count", "wrong_version", "wrong_text", "duplicate"):
            assembly = deepcopy(result.compilation.assembly)
            if change == "missing":
                assembly.pop("length_diagnostics")
            elif change == "wrong_count":
                row = next(row for row in assembly["length_diagnostics"] if row["channel"] == "positive_prompt")
                row["fragments"][0]["text_chars"] -= 1
            elif change == "wrong_version":
                assembly["contract_sha256"] = "0" * 64
            elif change == "wrong_text":
                assembly["channels"]["positive_prompt"] = "different"
            else:
                assembly["length_diagnostics"].append(deepcopy(assembly["length_diagnostics"][0]))
            with self.subTest(change=change), self.assertRaises(ValidationError):
                _assembly_display(assembly, configuration, channels)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class CumulativeLengthRejectionTests(fixtures.AssemblyAssertions, TestCase):
    setUp = fixtures.MultiChannelFinalAssemblyTests.setUp
    positive_text = "中😀" * 1000
    extra_fragments = (("configured_system_section", "positive_prompt", "系统条件" * 300),)
    channel_limits = {"positive_prompt": {"max_chars": 3000, "warn_at_chars": None}}

    def test_public_read_preserves_complete_cumulative_overflow_and_cannot_finalize(self):
        with connection.execute_wrapper(readonly):
            result = self.display()
        self.assertEqual(result["mode"], "diagnostic")
        text = next(row["text"] for row in result["channels"] if row["key"] == "positive_prompt")
        positive = next(row for row in result["length_diagnostics"] if row["channel"] == "positive_prompt")
        self.assertIn(self.positive_text, text)
        self.assertIn(self.extra_fragments[0][2], text)
        self.assertTrue(all(row["text_chars"] <= row["max_chars"] for row in positive["fragments"]))
        self.assertEqual(positive["over_by"], len(text) - 3000)
        with self.assertRaises(PromptProfileChannelLengthError) as rejected:
            self.finalize(collector=lambda *_: self.fail("No admission or actual call"))
        self.assertEqual(text, rejected.exception.diagnostic["channels"]["positive_prompt"])
        self.prep.refresh_from_db()
        self.assertIsNone(self.prep.request_frozen_at)
        self.assert_no_dispatch()
