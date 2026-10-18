"""Speech-language facts are independent of generated direction language."""
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from studio.services.dialogue import DialogueContractError, normalize_dialogue_entry
from studio.services.execution_reference_inputs import reference_context_input_schema
from studio.services.execution_prompt_display import _sources
from studio.services.production_storyboard_materialization import _declared_dialogue
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.reference_context import compile_reference_context
from studio.services.reference_context_facts import freeze_reference_context_facts, read_reference_context_facts
from studio.services.script_semantics import freeze_script_semantics, preview_script_semantics
from studio.tests.output_structure_fixtures import frozen_response_structure
from studio.tests.reference_rendering_fixtures import reference_context_fixture, reference_unit_fixture, rendering_config
from studio.tests.script_semantics_fixtures import script_example


class DialogueLanguageTests(SimpleTestCase):
    def setUp(self):
        guard = patch("studio.services.reference_context.boundary_int", return_value=65536)
        guard.start()
        self.addCleanup(guard.stop)
        self.rows = [dict(speaker_id="S1", text="  等我一下。  ", language="Chinese", delivery="Calm.",
            voiceover=False, start_ms=0, end_ms=900, subject="原名", voice_profile=""),
            dict(speaker_id="S1", text="I'll be right back.", language="English", delivery="Quietly.",
                voiceover=True, start_ms=1000, end_ms=3000, subject="原名", voice_profile="")]
        self.context = reference_context_fixture(ordered_units=(reference_unit_fixture(dialogue=tuple(self.rows)),),
            language="Japanese")
        self.policy = rendering_config()["primitive_options"]["reference_context_ir_v1"]["rendering"]

    def test_same_speaker_can_switch_language_and_voiceover_without_translation(self):
        original = deepcopy(self.context)
        text = str(compile_reference_context(self.context, self.policy)["fields"]["detailed_description"])
        self.assertIn("<d>[Chinese]   等我一下。  </d>", text)
        self.assertIn("<d>[English] I'll be right back.</d>", text)
        self.assertNotIn("[Japanese]", text)
        self.assertEqual(self.context, original)
        frozen = freeze_reference_context_facts(self.context, max_bytes=65536)
        restored = read_reference_context_facts(frozen, max_bytes=65536)
        self.assertEqual(restored.ordered_units[0].dialogue, tuple(self.rows))

    def test_missing_language_errors_only_when_consumed_and_never_uses_global(self):
        for language in (None, "", "  "):
            line = deepcopy(self.rows[0])
            if language is None:
                del line["language"]
            else:
                line["language"] = language
            context = replace(self.context, ordered_units=(reference_unit_fixture(dialogue=(line,)),))
            # Reading old frozen facts is truthful: no invented language and no rewrite.
            restored = read_reference_context_facts(freeze_reference_context_facts(context, max_bytes=65536), max_bytes=65536)
            self.assertEqual(restored.ordered_units[0].dialogue[0], line)
            with self.assertRaisesRegex(PromptBuildError, r"dialogue\[0\].language 未声明"):
                compile_reference_context(context, self.policy)
            policy = deepcopy(self.policy)
            policy["templates"]["dialogue"] = "{speaker_id}: <d>{text}</d>"
            self.assertIn(line["text"], str(compile_reference_context(context, policy)["fields"]))
            compile_reference_context(context, self.policy, result_fields={"overall_soundscape"})

    def test_language_is_a_safe_explicit_label_and_never_guessed_from_text(self):
        limits = dict(speaker_id_max_characters=20, text_max_characters=2000, delivery_max_characters=1000)
        for value in ("zh-Hans", "English", "", "French"):
            row = dict(speaker_id="S1", text="hello", language=value)
            self.assertEqual(normalize_dialogue_entry(row, _limits=limits), row)
        for value in (None, 1, True, [], "English\n", "[Chinese]", "{language}", "<d>"):
            with self.subTest(value=value), self.assertRaises(DialogueContractError):
                normalize_dialogue_entry(dict(speaker_id="S1", text="hello", language=value), _limits=limits)

    def test_saved_prompt_display_preserves_declared_and_missing_language(self):
        from dataclasses import asdict
        line = deepcopy(self.rows[0])
        del line["language"]
        context = replace(self.context, ordered_units=(reference_unit_fixture(dialogue=(line, self.rows[1])),))
        facts = freeze_reference_context_facts(context, max_bytes=65536)
        detail = SimpleNamespace(resolved_inputs={"profile_sources": {"reference_context": asdict(facts)}})
        with patch("studio.services.execution_prompt_display.execution_configuration_max_bytes", return_value=65536):
            result = _sources(detail)["dialogue"]
        self.assertNotIn("language", result[0])
        self.assertEqual(result[1]["language"], "English")
        self.assertEqual([row["text"] for row in result], [row["text"] for row in (line, self.rows[1])])

    def test_script_mapping_materialization_and_reference_form_keep_per_line_language(self):
        for layout, episodes, shots, dialogue in (("a", "episodes", "shots", "dialogue"), ("b", "chapters", "frames", "speech")):
            raw, _, mapping = script_example(layout)
            for ep in raw[episodes]:
                for index, shot in enumerate(ep[shots]):
                    shot[dialogue]["language"] = ("Chinese", "English")[index]
                    shot[dialogue]["text"] = self.rows[index]["text"]
            source = frozen_response_structure(raw)
            result = preview_script_semantics(configuration=freeze_script_semantics(
                source_contract=source, mapping=mapping, max_bytes=65536),
                response_text=json.dumps(raw, ensure_ascii=False), max_bytes=65536)
            self.assertTrue(result["valid"], result["errors"])
            for index, shot in enumerate(result["value"]["episodes"][0]["shots"]):
                line = _declared_dialogue({"body": shot, "relations": {"speaker": {"speaker_id": "S1"}}})[0]
                self.assertEqual(line["language"], self.rows[index]["language"])
                self.assertEqual(line["text"], self.rows[index]["text"])
        props = reference_context_input_schema()["properties"]["ordered_units"]["items"]["properties"]
        language = props["dialogue"]["items"]["properties"]["language"]
        self.assertEqual(language["title"], "本句对白语言")
        self.assertNotIn("default", language)
