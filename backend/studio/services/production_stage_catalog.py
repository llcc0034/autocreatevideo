"""Single registered stage vocabulary for recipes and manual Run plans.

This catalog names implemented platform responsibilities, not model defaults,
an arbitrary DAG, a recipe, or proof that a particular project can execute.
"""
from studio.services.bible_reference_contract import BIBLE_IMAGE_GENERATION_STAGE_KEY, BIBLE_IMAGE_REVIEW_STAGE_KEY
from studio.services.json_contracts import json_contract, DATABASE_JSON_FIELD_MAX_BYTES


SUPPORTED_STAGE_MODES = frozenset({
    "confirmed_revision",
    "review",
    "deterministic",
    "existing_pipeline",
    "manual",
    "skip",
    "ai",
})

ROUTED_STAGE_KEYS = frozenset({
    "media_generation",
    BIBLE_IMAGE_GENERATION_STAGE_KEY,
    "script_generate",
    "script_structure",
    "visual_rules_generate",
    "structured_text",
})

EXECUTABLE_STAGE_MODES = frozenset({
    ("script_input", "confirmed_revision"),
    ("script_generate", "ai"),
    ("script_structure", "ai"),
    ("script_review", "review"),
    ("visual_rules_generate", "ai"),
    ("structured_text", "ai"),
    ("visual_rules_review", "review"),
    ("bible_materialize", "deterministic"),
    ("bible_review", "review"),
    (BIBLE_IMAGE_GENERATION_STAGE_KEY, "existing_pipeline"),
    (BIBLE_IMAGE_REVIEW_STAGE_KEY, "review"),
    ("storyboard", "deterministic"),
    ("storyboard_review", "review"),
    ("media_generation", "existing_pipeline"),
    ("artifact_selection", "review"),
    ("assembly", "existing_pipeline"),
    ("delivery", "existing_pipeline"),
    ("script_input", "manual"),
    ("bible_review", "manual"),
    ("storyboard", "manual"),
    ("storyboard_review", "manual"),
})

PRODUCTION_STAGE_ORDER = (
    "script_input", "script_generate", "script_structure", "script_review",
    "bible_materialize", "bible_review", "visual_rules_generate",
    "visual_rules_review", "storyboard", BIBLE_IMAGE_GENERATION_STAGE_KEY,
    BIBLE_IMAGE_REVIEW_STAGE_KEY, "storyboard_review",
    "media_generation", "artifact_selection", "assembly", "delivery", "structured_text",
)

PRODUCTION_STAGE_CATALOG_KEY = "media_asset_production"
PRODUCTION_STAGE_CATALOG_VERSION = 5

REPEATABLE_STAGE_LABELS = {"structured_text": "通用结构化文本"}


def stage_requires_execution_route(key, mode):
    """The stage's actual AI duty, not provider/model-name inference."""
    return key in ROUTED_STAGE_KEYS and mode not in {"manual", "review", "skip"}


# Original business-output protocols of the stage materializers. These are not
# provider/model choices; changing a model or Workflow does not change them.
STAGE_EXECUTION_OPERATIONS = {
    "script_generate": "script.generate",
    "script_structure": "script.structure",
    "visual_rules_generate": "visual_rules.generate",
    "structured_text": "text.structured",
}

STAGE_MEDIA_OUTPUT_KINDS = {
    "media_generation": ("image", "video", "audio"),
    BIBLE_IMAGE_GENERATION_STAGE_KEY: ("image",),
}

STAGE_MEDIA_SCOPE_KINDS = {
    "media_generation": ("project", "episode", "shot"),
    BIBLE_IMAGE_GENERATION_STAGE_KEY: ("character", "scene", "prop"),
}


def validate_stage_execution_definition(stage_key, definition):
    """One business-output check for Recipe and explicit manual preparation."""
    from django.core.exceptions import ValidationError
    operation = STAGE_EXECUTION_OPERATIONS.get(stage_key)
    if operation and definition.value["configuration"]["operation"] != operation:
        raise ValidationError(f"阶段 {stage_key} 需要 {operation} 的业务输出协议")
    if stage_key in STAGE_MEDIA_OUTPUT_KINDS:
        output = definition.value["interface"]["output"]
        kind = (output.get("contract", {}).get("primary", {}).get("kind")
            if definition.value["target"]["workflow"] is not None else output.get("kind"))
        if kind not in STAGE_MEDIA_OUTPUT_KINDS[stage_key]:
            raise ValidationError(f"阶段 {stage_key} 的产物类型不匹配")

# Registered review responsibilities, not model/instance routing decisions.
PROJECT_TEXT_REVIEW_SOURCES = {
    "script_review": frozenset({"script_generate", "script_structure"}),
    "visual_rules_review": frozenset({"visual_rules_generate"}),
}

# Explicit user actions, not an automatic recipe or proof that prerequisites
# are satisfied. Prompt assembly belongs to each actual call's Preparation.
MANUAL_STAGE_ACTIONS = {
    "script_input": ("adopt",),
    "script_generate": ("generate", "retry"),
    "script_structure": ("generate", "retry", "adopt"),
    "script_review": ("approve",),
    "bible_materialize": ("adopt",),
    "bible_review": ("approve",),
    "visual_rules_generate": ("generate", "retry", "adopt"),
    "visual_rules_review": ("approve",),
    "storyboard": ("adopt",),
    BIBLE_IMAGE_GENERATION_STAGE_KEY: ("generate", "retry", "adopt"),
    BIBLE_IMAGE_REVIEW_STAGE_KEY: ("approve",),
    "storyboard_review": ("select", "approve"),
    "media_generation": ("generate", "retry", "adopt"),
    "artifact_selection": ("approve",),
    "assembly": ("assemble",),
    "delivery": ("deliver",),
}

# This is the implemented delivery product, not a model-specific preset.
PRODUCTION_DELIVERY_KINDS = ("episode_masters",)

UNATTENDED_INTERACTION_MODE = "unattended"
UNATTENDED_DECISION_POLICY_REGISTRY = {
    "script_review": "approve_single_frozen_script_v1",
    "visual_rules_review": "accept_single_configured_visual_rules_v2",
    "bible_review": "approve_frozen_bible_v1",
    BIBLE_IMAGE_REVIEW_STAGE_KEY: "select_single_bible_candidate_per_entity_v1",
    "storyboard_review": "bind_reviewed_shot_references_by_instance_v3",
    "artifact_selection": "accept_single_artifact_per_group_v1",
}
UNATTENDED_STAGE_CONTRACTS = (
    ("script_generate", "ai"),
    ("script_review", "review"),
    ("bible_materialize", "deterministic"),
    ("bible_review", "review"),
    ("storyboard", "deterministic"),
    (BIBLE_IMAGE_GENERATION_STAGE_KEY, "existing_pipeline"),
    (BIBLE_IMAGE_REVIEW_STAGE_KEY, "review"),
    ("storyboard_review", "review"),
    ("media_generation", "existing_pipeline"),
    ("artifact_selection", "review"),
    ("assembly", "existing_pipeline"),
    ("delivery", "existing_pipeline"),
)
UNATTENDED_VISUAL_RULES_STAGE_CONTRACTS = (
    ("script_generate", "ai"),
    ("script_review", "review"),
    ("bible_materialize", "deterministic"),
    ("bible_review", "review"),
    ("visual_rules_generate", "ai"),
    ("visual_rules_review", "review"),
    ("storyboard", "deterministic"),
    (BIBLE_IMAGE_GENERATION_STAGE_KEY, "existing_pipeline"),
    (BIBLE_IMAGE_REVIEW_STAGE_KEY, "review"),
    ("storyboard_review", "review"),
    ("media_generation", "existing_pipeline"),
    ("artifact_selection", "review"),
    ("assembly", "existing_pipeline"),
    ("delivery", "existing_pipeline"),
)


def production_stage_catalog():
    """Deterministic local catalog; no resource lookup, publication or writes."""
    return json_contract({
        "schema_version": 1,
        "key": PRODUCTION_STAGE_CATALOG_KEY,
        "version": PRODUCTION_STAGE_CATALOG_VERSION,
        "stages": [{"key": key, "ordinal": ordinal,
            "modes": sorted(mode for stage_key, mode in EXECUTABLE_STAGE_MODES if stage_key == key),
            "requires_routing": key in ROUTED_STAGE_KEYS,
            "manual_actions": list(MANUAL_STAGE_ACTIONS.get(key, ())),
            **({"repeatable": True} if key in REPEATABLE_STAGE_LABELS else {})}
            for ordinal, key in enumerate(PRODUCTION_STAGE_ORDER, start=1)],
    }, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
