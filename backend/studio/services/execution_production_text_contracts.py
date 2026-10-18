"""Production text artifact roles, shared by queue, writer and verified reader."""

PRODUCTION_TEXT_ROLES = {
    "script.generate": "generated_script",
    "script.structure": "structured_script",
    "visual_rules.generate": "visual_rules_candidate",
    "text.structured": "structured_text_candidate",
}
PRODUCTION_TEXT_OPERATIONS = tuple(PRODUCTION_TEXT_ROLES)
