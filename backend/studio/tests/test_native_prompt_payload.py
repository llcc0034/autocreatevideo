from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.native_prompt_payload import native_prompt_payload


class NativePromptPayloadTests(SimpleTestCase):
    def test_rendered_text_is_literal_complete_and_does_not_mutate_audit(self):
        positive = "  一句话梗概 {{request.theme}}\n" + "完整内容" * 10000 + "\n末尾  "
        assembly = {
            "channels": {"system_instruction": "Reviewed rules", "positive_prompt": positive,
                         "negative_prompt": "do not crop", "structured_input": ""},
            "structured_fields": {"theme": positive, "system_guidance": "Reviewed rules"},
        }
        original = deepcopy(assembly)
        system, user = native_prompt_payload(assembly, operation="visual_rules.generate")
        self.assertEqual(system, "Reviewed rules")
        self.assertEqual(user, {"positive_prompt": positive, "negative_prompt": "do not crop"})
        self.assertEqual(assembly, original)

    def test_manual_channels_need_no_structured_values(self):
        self.assertEqual(native_prompt_payload({"channels": {"positive_prompt": "manual"},
            "structured_fields": {}}, operation="script.generate"),
            ("", {"positive_prompt": "manual", "negative_prompt": ""}))

    def test_invalid_or_unrendered_channels_are_not_silently_dropped(self):
        for assembly in (None, [], {}, {"channels": [], "structured_fields": {}},
                         {"channels": {}, "structured_fields": []}):
            with self.subTest(assembly=assembly), self.assertRaises(ValidationError):
                native_prompt_payload(assembly, operation="script.generate")
        for channels in ({"positive_prompt": None}, {"positive_prompt": ["text"]},
                         {"system_instruction": 0}, {"unknown": ""},
                         {"structured_input": "raw fields", "positive_prompt": "rendered"},
                         {"structured_input": " "}, {"positive_prompt": "  "}, {}):
            with self.subTest(channels=channels), self.assertRaises(ValidationError):
                native_prompt_payload({"channels": channels, "structured_fields": {"theme": "audit"}},
                    operation="script.generate")

    def test_explicitly_repeated_strategy_channels_are_not_fuzzy_deduplicated(self):
        assembly = {"channels": {"positive_prompt": "repeated", "negative_prompt": "repeated"},
                    "structured_fields": {"theme": "repeated"}}
        _, user = native_prompt_payload(assembly, operation="script.structure")
        self.assertEqual(user, {"positive_prompt": "repeated", "negative_prompt": "repeated"})
