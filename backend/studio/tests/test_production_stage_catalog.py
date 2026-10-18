from django.test import TestCase

from studio.services.production_stage_catalog import production_stage_catalog, EXECUTABLE_STAGE_MODES, PRODUCTION_STAGE_ORDER
from studio.services.production_runs import production_recipe_editor_options


class ProductionStageCatalogTests(TestCase):
    def test_retired_normalization_stages_have_no_catalog_worker_or_review_entry(self):
        from studio.platform_serializers import ApprovalDecisionInputSerializer
        from studio.services import production_runs, text_generation_queue
        from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY
        for key in ("script_normalize", "script_normalize_review", "prompt_compile"):
            self.assertNotIn(key, PRODUCTION_STAGE_ORDER)
            self.assertNotIn(key, UNATTENDED_DECISION_POLICY_REGISTRY)
            # Use the actual decision envelope, so this asserts the retired
            # stage rejection rather than an unrelated unknown action field.
            serializer = ApprovalDecisionInputSerializer(data={"stage_key": key, "decision": "approve"})
            self.assertFalse(serializer.is_valid())
            self.assertIn("stage_key", serializer.errors)
            self.assertNotIn(key, {row["key"] for row in production_stage_catalog().value["stages"]})
        self.assertNotIn("prompt.normalize", text_generation_queue.TEXT_OPERATIONS)
        self.assertFalse(hasattr(production_runs, "_prepare_prompt_normalization"))
        self.assertFalse(hasattr(production_runs, "_approve_prompt_normalization"))
        self.assertFalse(hasattr(text_generation_queue, "_process_prompt_normalization_attempt"))
        self.assertFalse(hasattr(production_runs, "_execute_prompt_compile"))

    def test_prompt_assembly_is_not_a_manual_action_or_delivery_dependency(self):
        from studio.services.production_stage_catalog import (
            MANUAL_STAGE_ACTIONS, UNATTENDED_STAGE_CONTRACTS, UNATTENDED_VISUAL_RULES_STAGE_CONTRACTS,
        )
        from studio.services.production_runs import _validate_delivery_recipe_closure
        self.assertNotIn("prompt_compile", MANUAL_STAGE_ACTIONS)
        self.assertEqual(production_stage_catalog().value["version"], 5)
        self.assertEqual(MANUAL_STAGE_ACTIONS["storyboard_review"], ("select", "approve"))
        for closure in (UNATTENDED_STAGE_CONTRACTS, UNATTENDED_VISUAL_RULES_STAGE_CONTRACTS):
            self.assertNotIn("prompt_compile", [key for key, _ in closure])
            self.assertLess([key for key, _ in closure].index("storyboard_review"),
                [key for key, _ in closure].index("media_generation"))
        stages = [{"key": key, "mode": mode, "approval": "required" if mode == "review" else "auto"}
            for key, mode in (("script_input", "confirmed_revision"), ("storyboard", "deterministic"),
                ("media_generation", "existing_pipeline"), ("artifact_selection", "review"),
                ("assembly", "existing_pipeline"), ("delivery", "existing_pipeline"))]
        _validate_delivery_recipe_closure(stages)

    def test_single_catalog_is_readonly_deterministic_and_shared_with_recipe_forms(self):
        from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY, stage_requires_execution_route
        with self.assertNumQueries(0):
            first = production_stage_catalog()
            second = production_stage_catalog()
        options = production_recipe_editor_options()
        self.assertEqual(first, second)
        self.assertEqual([row['key'] for row in first.value['stages']], list(PRODUCTION_STAGE_ORDER))
        self.assertEqual({(row['key'], mode) for row in first.value['stages'] for mode in row['modes']}, set(EXECUTABLE_STAGE_MODES))
        self.assertEqual(options['stage_options'], [{
            'value': row['key'], 'modes': row['modes'], 'requires_routing': row['requires_routing'],
            'routing_modes': [mode for mode in row['modes'] if stage_requires_execution_route(row['key'], mode)],
            'decision_policy': UNATTENDED_DECISION_POLICY_REGISTRY.get(row['key']),
            'repeatable': row.get('repeatable', False),
            'label': '通用结构化文本' if row['key'] == 'structured_text' else row['key'],
            'candidate_evaluator_options': ([{'value': None, 'label': '人工选择候选'},
                {'value': 'first_succeeded', 'label': '按候选序号自动选择成功项'}] if row.get('repeatable') or row['key'] == 'bible_image_generation' else []),
        } for row in first.value['stages']])
        first.value['stages'].clear()
        self.assertEqual(production_stage_catalog(), second)
        self.assertLess(second.size_bytes, 65536)
