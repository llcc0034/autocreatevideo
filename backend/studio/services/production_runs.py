from __future__ import annotations

import hashlib
import logging
import re
import uuid
from contextvars import ContextVar
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import ApprovalDecision, ArtifactSelectionDecision, Asset, CharacterBible, Episode, GeneratedArtifact, GenerationAttempt, GenerationAttemptResolution, GenerationTask, ModelDeployment, ProviderProfile, ProductionAssemblyLink, ProductionRecipeVersion, ProductionRun, PromptFieldTranslation, PropBible, ReleaseBundle, SceneBible, ScriptRevision, SeriesProject, Shot, StageRun, StageRouteEvent, WorkflowTemplateVersion
from studio.services.assembly import AssemblyError, create_assembly
from studio.services.configuration import ConfigurationResolutionError, redact_sensitive_bindings, resolve_configuration_from_snapshot
from studio.services.content_contracts import normalize_optional_audit_comment
from studio.services.generated_artifacts import (
    record_assembly_artifact,
    select_artifact as select_generated_artifact,
    verified_local_media_facts,
)
from studio.services.json_contracts import canonical_json_bytes, json_contract, verify_json_contract
from studio.services.production_stage_catalog import ROUTED_STAGE_KEYS, UNATTENDED_INTERACTION_MODE, UNATTENDED_DECISION_POLICY_REGISTRY, production_stage_catalog
from studio.services.provenance import ProvenanceError
from studio.services.provider_registry import ProviderDriverError, driver_for
from studio.services.release_bundles import (
    ReleaseBundleError,
    create_release_draft,
    seal_release_bundle,
    live_verified_release_bundle,
)
from studio.services.consistency import sync_consistency_locks
from studio.services.visual_reference_policy import (
    visual_reference_characters,
)
from studio.services.registry_reference_lock import (
    registry_reference_writer,
)
from studio.services.bible_reference_contract import (
    BIBLE_IMAGE_GENERATION_PURPOSE,
    BIBLE_IMAGE_GENERATION_STAGE_KEY,
    BIBLE_IMAGE_REVIEW_STAGE_KEY,
)
from studio.services.script_bible_materialization import freeze_script_reference_bible
from studio.services.production_storyboard_content import storyboard_content
from studio.services.script_review import approve_frozen_script_candidate
from studio.services.production_visual_rules import VISUAL_RULES_OPERATION, approve_verified_visual_rules_artifact, current_rules_snapshot
from studio.services.script_structure import SCRIPT_STRUCTURE_OPERATION
from studio.services.text_generation_queue import TEXT_OPERATION, append_attempt_resolution
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache
from studio.services.async_media_contract import (
    ASYNC_MEDIA_OPERATIONS,
)


TERMINAL_RUN_STATUSES = frozenset({
    ProductionRun.Status.SUCCEEDED,
    ProductionRun.Status.FAILED,
    ProductionRun.Status.CANCELLED,
})
logger = logging.getLogger(__name__)
ROUTING_MODES = frozenset({"fixed", "ordered_fallback", "manual_choice"})




def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")












def _bible_candidate_max_items() -> int:
    return boundary_int("security.production.max_bible_candidates_per_entity")


def _bible_reference_asset_max_items() -> int:
    return boundary_int(
        "security.production.max_bible_reference_assets_per_entity"
    )


def _bible_regeneration_entity_max_items() -> int:
    return boundary_int(
        "security.production.max_bible_regeneration_entities_per_request"
    )


def _episode_max_items() -> int:
    return boundary_int("security.production.max_episodes")


def _shots_per_episode_max_items() -> int:
    return boundary_int("security.production.max_shots_per_episode")


def _total_shot_max_items() -> int:
    return boundary_int("security.production.max_total_shots")


def _generation_task_absolute_limit() -> int:
    return boundary_int("security.production.max_generation_tasks")


def _effective_generation_task_limit(run: ProductionRun) -> int:
    """Finite stage-snapshot safety boundary, not a business call allowance."""

    return _generation_task_absolute_limit()

# One server-authored catalog drives both recipe validation and the structured
# settings form.  The browser may localize labels, but it must not invent stage
# keys, modes, approval gates, routing modes, or schema defaults.
RECIPE_EDITOR_DEFINITION_SCHEMA_VERSION = 3
RECIPE_APPROVAL_OPTIONS = ("auto", "required", "on_warning", "policy")


def _recipe_editor_stage(
    key: str,
    mode: str,
    approval: str,
) -> dict[str, Any]:
    stage: dict[str, Any] = {"key": key, "mode": mode, "approval": approval}
    if key in ROUTED_STAGE_KEYS and mode != "manual":
        stage["routing"] = {
            "mode": "fixed", "variant_count": 1, "routes": [], "evaluator": None,
        }
    return stage


def _recipe_editor_preset(script_mode: str) -> list[dict[str, Any]]:
    script = (
        [_recipe_editor_stage("script_generate", "ai", "auto")]
        if script_mode == "ai"
        else [
            _recipe_editor_stage("script_input", "confirmed_revision", "auto"),
            _recipe_editor_stage("script_structure", "ai", "auto"),
        ]
    )
    return [
        *script,
        _recipe_editor_stage("script_review", "review", "required"),
        _recipe_editor_stage("bible_materialize", "deterministic", "auto"),
        _recipe_editor_stage("bible_review", "review", "required"),
        _recipe_editor_stage("visual_rules_generate", "ai", "auto"),
        _recipe_editor_stage("visual_rules_review", "review", "required"),
        _recipe_editor_stage("storyboard", "deterministic", "auto"),
        _recipe_editor_stage(BIBLE_IMAGE_GENERATION_STAGE_KEY, "existing_pipeline", "auto"),
        _recipe_editor_stage(BIBLE_IMAGE_REVIEW_STAGE_KEY, "review", "required"),
        _recipe_editor_stage("storyboard_review", "review", "required"),
        _recipe_editor_stage("media_generation", "existing_pipeline", "auto"),
        _recipe_editor_stage("artifact_selection", "review", "required"),
        _recipe_editor_stage("assembly", "existing_pipeline", "auto"),
        _recipe_editor_stage("delivery", "existing_pipeline", "auto"),
    ]


def production_recipe_editor_options() -> dict[str, Any]:
    from studio.services.assembly_contracts import assembly_output_schema, assembly_timing_schema, assembly_media_schema
    from studio.services.delivery_contracts import delivery_metadata_schema
    from studio.services.production_budget_configuration import generation_call_limit
    from studio.services.execution_input_catalog import execution_text_output_options
    from studio.services.production_reference_mapping import reference_source_options
    from studio.services.execution_field_review import field_review_policy_options
    from studio.services.production_parameter_sources import recipe_parameter_source_options
    from studio.services.execution_structure_mapping import structure_node_options
    from studio.services.production_stage_catalog import REPEATABLE_STAGE_LABELS, stage_requires_execution_route
    catalog = production_stage_catalog().value
    from studio.services.production_recipe_material_sources import MATERIAL_SOURCE_OPTIONS, MATERIAL_SELECTION_OPTIONS
    from studio.services.production_recipe_shot_references import SHOT_REFERENCE_SOURCE_OPTIONS
    return {
        "definition_schema_version": RECIPE_EDITOR_DEFINITION_SCHEMA_VERSION,
        "assembly_output_schema": assembly_output_schema(),
        "assembly_timing_schema": assembly_timing_schema(),
        "assembly_media_schema": assembly_media_schema(),
        "delivery_metadata_schema": delivery_metadata_schema(),
        "material_source_options": list(MATERIAL_SOURCE_OPTIONS),
        "material_selection_options": list(MATERIAL_SELECTION_OPTIONS),
        "shot_reference_source_options": deepcopy(list(SHOT_REFERENCE_SOURCE_OPTIONS)),
        "effective_limits": {
            "max_routing_routes": boundary_int("security.production.max_routing_routes"),
            "max_variants": boundary_int("security.production.max_variants"),
            "max_generation_tasks": generation_call_limit(),
        },
        "stage_options": [
            {
                "value": stage["key"],
                "modes": stage["modes"],
                "requires_routing": stage["requires_routing"],
                "routing_modes": [mode for mode in stage["modes"] if stage_requires_execution_route(stage["key"], mode)],
                "decision_policy": UNATTENDED_DECISION_POLICY_REGISTRY.get(stage["key"]),
                "repeatable": stage.get("repeatable", False),
                "label": REPEATABLE_STAGE_LABELS.get(stage["key"], stage["key"]),
                "candidate_evaluator_options": ([
                    {"value": None, "label": "人工选择候选"},
                    {"value": "first_succeeded", "label": "按候选序号自动选择成功项"},
                ] if stage.get("repeatable", False) or stage["key"] == "bible_image_generation" else []),
            }
            for stage in catalog["stages"]
        ],
        "approval_options": [
            {"value": value, "label": value}
            for value in RECIPE_APPROVAL_OPTIONS
        ],
        "interaction_mode_options": [
            {"value": "assisted", "label": "人工辅助制作"},
            {"value": "unattended", "label": "无人值守制作"},
        ],
        "field_review_policy_options": field_review_policy_options(),
        "source_kind_options": [
            {"value": "structured", "label": "按合同逐字段映射"},
            {"value": "literal", "label": "固定内容"},
            {"value": "context_field", "label": "已确认的内容上下文"},
            {"value": "project_snapshot", "label": "创建本轮时预览确认的项目内容"},
            {"value": "selected_output", "label": "上游阶段已采用的输出"},
            {"value": "reviewed_references", "label": "当前镜头已审核的参考清单"},
            {"value": "media_selection", "label": "固定素材（素材库／上传入库）"},
            {"value": "material_primary", "label": "本轮确切已选主图"},
            {"value": "material_difference", "label": "当前已审核变体的差异"},
        ],
        "text_output_options": execution_text_output_options(),
        "reference_source_options": reference_source_options(),
        "parameter_source_options": recipe_parameter_source_options(),
        "structure_node_options": structure_node_options(),
        "parameter_policy_options": [
            {"value": "default", "label": "步骤默认值"},
            {"value": "locked_value", "label": "固定值"},
            {"value": "locked_source", "label": "从项目字段取值并固定"},
        ],
        "scope_options": [{"value": value, "label": label} for value, label in
            (("project", "本轮项目"), ("episode", "本轮剧集"), ("shot", "本轮镜头"))],
        "routing_mode_options": [
            {"value": value, "label": value}
            for value in sorted(ROUTING_MODES)
        ],
        "presets": [
            {
                "value": "ai_draft",
                "label": "人工智能起稿链路",
                "stages": _recipe_editor_preset("ai"),
            },
            {
                "value": "confirmed_script",
                "label": "已确认剧本链路",
                "stages": _recipe_editor_preset("confirmed"),
            },
        ],
    }

_UNATTENDED_CANONICAL_WORD_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:['-][A-Za-z0-9]+)*",
)
_UNATTENDED_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_UNATTENDED_PROVISIONAL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"S[1-9][0-9]*|"
    r"scene:[A-Za-z0-9][A-Za-z0-9_-]*|"
    r"prop:[A-Za-z0-9][A-Za-z0-9_-]*"
    r")(?![A-Za-z0-9_-])",
    flags=re.IGNORECASE,
)
_UNATTENDED_CANONICAL_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "at", "by", "for", "from", "has", "have",
    "in", "is", "of", "on", "or", "the", "to", "under", "with", "wearing",
    "wears", "holding", "holds", "featuring", "features", "standing", "stands",
    "character", "scene", "prop",
})


class ProductionRunConflict(ValidationError):
    def __init__(self, message, *, code: str = "production_run_conflict"):
        self.code = str(code)
        super().__init__(message)


class ProductionRunCreateRejected(ValidationError):
    """Stable 400 preflight error emitted before any run/audit row exists."""

    def __init__(self, message, *, code: str):
        self.code = str(code)
        super().__init__(message)


class ProductionRunRetryConflict(ValidationError):
    pass


class StageBlocked(RuntimeError):
    def __init__(self, code: str, reason: str, *, awaiting_approval: bool = False):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.awaiting_approval = awaiting_approval


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _non_secret_config(value: Any) -> Any:
    """Freeze operational configuration while excluding credential-shaped values."""

    if isinstance(value, list):
        return [_non_secret_config(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        normalized = key.casefold().replace("-", "_")
        sensitive = (
            normalized in {"secret", "token", "password", "api_key", "authorization", "credential"}
            or normalized.endswith((
                "_secret", "_token", "_password", "_api_key", "_authorization",
                "_credential", "_credential_ref",
            ))
        )
        if sensitive:
            continue
        result[key] = _non_secret_config(item)
    return result


def _snapshot_config_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    # Run-scoped secret definitions are rejected by configuration validation.
    # Keep the idempotency payload free of credential-shaped nested input too.
    return _non_secret_config(overrides)










def _recipe_stages(recipe: ProductionRecipeVersion) -> list[dict[str, Any]]:
    """Read only schema3; never expand it into a legacy deployment route."""
    from studio.services.production_recipe_contracts import read_recipe_definition
    verify_json_contract(
        recipe.definition,
        schema_version=recipe.definition_schema_version,
        size_bytes=recipe.definition_size_bytes,
        sha256=recipe.definition_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    return read_recipe_definition(recipe.definition, require_complete=True)


















def _routing_requires_entity_reference_bible(routing: object) -> bool:
    """Read only the precise selected mode, never all modes or old routes."""
    from studio.services.execution_definitions import stored_execution_contract, verify_stored_execution_definition

    if routing is None:
        return False
    if not isinstance(routing, dict) or not isinstance(routing.get("routes"), list) or not routing["routes"]:
        raise ValidationError("媒体路线缺少明确执行实例合同")
    required = False
    for route in routing["routes"]:
        if not isinstance(route, dict) or set(route) != {
            "execution_instance_version_id", "expected_definition_sha256", "input_sources", "parameter_policies",
        }:
            raise ValidationError("媒体路线必须引用精确执行实例，不接受旧资源快照")
        contract = stored_execution_contract(route["execution_instance_version_id"])
        if contract.sha256 != route["expected_definition_sha256"]:
            raise ValidationError("媒体路线引用的执行定义摘要不一致")
        definition = verify_stored_execution_definition(contract)
        mode = definition.value["resources"]["conditioning_mode"]
        if mode is not None and not isinstance(mode, dict):
            raise ValidationError("精确实例的冻结模式合同无效")
        required = required or (isinstance(mode, dict) and mode.get("requires_entity_reference_bible") is True)
    return required


def _validate_bible_source_contract(
    by_key: dict[str, dict[str, Any]],
    *,
    required_review_approval: str,
) -> str:
    """Validate the configured source of formal text Bible entities.

    Human-operated recipes may maintain Character/Scene/Prop Bible rows directly
    and confirm them at ``bible_review/manual``.  Generated or normalized script
    recipes instead freeze a structured candidate block at
    ``bible_materialize/deterministic`` and approve it at
    ``bible_review/review``.  Keeping these as two explicit, mutually-exclusive
    contracts avoids forcing a hidden SCRIPT_BIBLE_V1 preamble on ordinary
    pasted scripts while preserving provenance for model-generated candidates.
    """

    materialize = by_key.get("bible_materialize")
    review = by_key.get("bible_review")
    if materialize is None:
        if (
            isinstance(review, dict)
            and review.get("mode") == "manual"
            and review.get("approval") == "required"
        ):
            return "manual"
        raise ValidationError(
            "Bible 来源必须配置为 bible_review/manual(required)，或 "
            "bible_materialize/deterministic + bible_review/review。",
        )
    if (
        materialize.get("mode") == "deterministic"
        and isinstance(review, dict)
        and review.get("mode") == "review"
        and review.get("approval") == required_review_approval
    ):
        return "materialized"
    raise ValidationError(
        "Bible 候选物化必须使用 bible_materialize/deterministic，并由匹配的 "
        "bible_review/review 审核；纯人工 Bible 不得同时声明 bible_materialize。",
    )


def _validate_delivery_recipe_closure(stages: list[dict[str, Any]]) -> None:
    """Require the complete, formally-gated production-to-delivery chain."""

    by_key = {item["key"]: item for item in stages}
    policy_review = any(item.get("approval") == "policy" for item in stages)
    required_review_approval = "policy" if policy_review else "required"
    required = (
        "storyboard", "media_generation",
        "artifact_selection", "assembly", "delivery",
    )
    missing = [key for key in required if key not in by_key]
    if missing:
        raise ValidationError(
            "published recipe 缺少统一交付闭包阶段: " + ", ".join(missing),
        )
    content_inputs = [
        item for item in stages
        if (item["key"], item["mode"]) in {
            ("script_input", "confirmed_revision"),
            ("script_input", "manual"),
            ("script_generate", "ai"),
        }
    ]
    if len(content_inputs) != 1:
        raise ValidationError(
            "published recipe 必须且只能包含一个正式 content 输入门："
            "script_input/confirmed_revision、script_input/manual 或 script_generate/ai",
        )
    if content_inputs[0]["key"] == "script_generate" and not (
        by_key.get("script_review", {}).get("mode") == "review"
        and by_key.get("script_review", {}).get("approval") == required_review_approval
    ):
        raise ValidationError("script_generate/ai 后必须有匹配审核方式的 script_review/review 正式确认门")
    structure = by_key.get("script_structure")
    if structure:
        if (
            (content_inputs[0]["key"], content_inputs[0]["mode"])
            != ("script_input", "confirmed_revision")
        ):
            raise ValidationError("script_structure/ai 只能跟在 script_input/confirmed_revision 后")
        review = by_key.get("script_review")
        if (
            structure.get("mode") != "ai"
            or not isinstance(review, dict)
            or review.get("mode") != "review"
            or review.get("approval") != required_review_approval
        ):
            raise ValidationError(
                "script_structure 必须使用 ai，并由 approval=required 的 script_review 确认",
            )
    visual_rules_generate = by_key.get("visual_rules_generate")
    visual_rules_review = by_key.get("visual_rules_review")
    has_bible = "bible_materialize" in by_key or "bible_review" in by_key
    if has_bible:
        _validate_bible_source_contract(by_key, required_review_approval=required_review_approval)
    if bool(visual_rules_generate) != bool(visual_rules_review):
        raise ValidationError(
            "visual_rules_generate/ai 与 visual_rules_review/review 必须成对出现",
        )
    if visual_rules_generate:
        if (
            visual_rules_generate.get("mode") != "ai"
            or visual_rules_review.get("mode") != "review"
            or visual_rules_review.get("approval") != required_review_approval
        ):
            raise ValidationError(
                "全剧影视规则必须使用 ai 生成，再由匹配的 review 门确认",
            )
    bible_image_generation = by_key.get(BIBLE_IMAGE_GENERATION_STAGE_KEY)
    bible_image_review = by_key.get(BIBLE_IMAGE_REVIEW_STAGE_KEY)
    if bool(bible_image_generation) != bool(bible_image_review):
        raise ValidationError(
            "bible_image_generation 与 bible_image_review 必须成对出现",
        )
    if (
        _routing_requires_entity_reference_bible(
            by_key.get("media_generation", {}).get("routing")
        )
        and not bible_image_generation
    ):
        raise ValidationError(
            "声明 requires_entity_reference_bible 的媒体 route 必须包含 "
            "bible_image_generation/bible_image_review；"
            "仅绑定动态参考视频不能作为人物、场景和道具的跨镜头一致性锚点",
        )
    if bible_image_generation:
        storyboard_review = by_key.get("storyboard_review")
        _validate_bible_source_contract(
            by_key,
            required_review_approval=required_review_approval,
        )
        if (
            bible_image_generation.get("mode") != "existing_pipeline"
            or bible_image_review.get("mode") != "review"
            or bible_image_review.get("approval") != required_review_approval
        ):
            raise ValidationError(
                "Bible 参考图必须由 existing_pipeline 生成并经 required review 审批",
            )
        if (
            not isinstance(storyboard_review, dict)
            or storyboard_review.get("mode") != "review"
            or storyboard_review.get("approval") != required_review_approval
        ):
            raise ValidationError(
                "Bible 参考图生产必须在 bible_image_review 后、media_generation 前经过 "
                "required storyboard_review，逐镜冻结精确素材绑定",
            )
    expected_modes = {
        "storyboard": {"deterministic", "manual"},
        **({
            **({"bible_materialize": "deterministic"} if "bible_materialize" in by_key else {}),
            "bible_review": {"review", "manual"},
            BIBLE_IMAGE_GENERATION_STAGE_KEY: "existing_pipeline",
            BIBLE_IMAGE_REVIEW_STAGE_KEY: "review",
            "storyboard_review": "review",
        } if bible_image_generation else ({
            **({"bible_materialize": "deterministic"} if "bible_materialize" in by_key else {}),
            "bible_review": {"review", "manual"},
        } if has_bible else {})),
        **({
            "visual_rules_generate": "ai",
            "visual_rules_review": "review",
        } if visual_rules_generate else {}),
        "media_generation": "existing_pipeline",
        "artifact_selection": "review",
        "assembly": "existing_pipeline",
        "delivery": "existing_pipeline",
    }
    for key, expected in expected_modes.items():
        actual = by_key[key]["mode"]
        allowed = expected if isinstance(expected, set) else {expected}
        if actual not in allowed:
            raise ValidationError(
                f"published recipe 阶段 {key} 必须使用 " + "/".join(sorted(allowed)),
            )
    for key in ("storyboard_review", "artifact_selection"):
        if key in by_key and by_key[key]["approval"] != required_review_approval:
            raise ValidationError(f"published recipe 阶段 {key} 必须明确配置匹配的审核方式")
    positions = {item["key"]: index for index, item in enumerate(stages)}
    content_key = content_inputs[0]["key"]
    ordered = [content_key]
    if content_key == "script_generate":
        ordered.append("script_review")
    elif structure:
        ordered.extend(("script_structure", "script_review"))
    if has_bible:
        if "bible_materialize" in by_key:
            ordered.append("bible_materialize")
        ordered.append("bible_review")
    if visual_rules_generate:
        ordered.extend(("visual_rules_generate", "visual_rules_review"))
    ordered.append("storyboard")
    if bible_image_generation:
        ordered.extend((
            BIBLE_IMAGE_GENERATION_STAGE_KEY,
            BIBLE_IMAGE_REVIEW_STAGE_KEY,
            "storyboard_review",
        ))
    elif "storyboard_review" in by_key:
        ordered.append("storyboard_review")
    ordered.extend((
        "media_generation", "artifact_selection", "assembly", "delivery",
    ))
    if [positions[key] for key in ordered] != sorted(positions[key] for key in ordered):
        raise ValidationError("published recipe 的 content/storyboard/media/selection/assembly/delivery 顺序无效")


def _assert_frozen_bible_run_closure(stages: list[StageRun]) -> None:
    """Verify registered Recipe duties from the sole immutable RunPlan."""
    if not stages:
        return
    from studio.services.production_run_plans import get_run_execution_plan

    run = stages[0].production_run
    try:
        plan = get_run_execution_plan(run)
        if plan.value["source"] != "recipe":
            raise ValidationError("人工制作不能使用配方阶段闭包")
        declarations = plan.value["recipe"]["definition"]["stages"]
        expected = [(row["key"], row["mode"], index) for index, row in enumerate(declarations, start=1)]
        actual = [(row.stage_key, row.mode, row.ordinal) for row in stages]
        if actual != expected or any(row.production_run_id != run.pk for row in stages):
            raise ValidationError("实际阶段与本轮冻结计划的职责或顺序不一致")
        media = next((row for row in declarations if row["key"] == "media_generation"), None)
        requires_bible = _routing_requires_entity_reference_bible(media.get("routing")) if media else False
        has_images = any(row["key"] == BIBLE_IMAGE_GENERATION_STAGE_KEY for row in declarations)
        if requires_bible and not has_images:
            raise ValidationError("精确实例要求实体参考 Bible，配方却缺少参考图生成和审核阶段")
        if has_images:
            _validate_delivery_recipe_closure(declarations)
    except ValidationError as exc:
        raise ProductionRunConflict({"detail": exc.messages}, code="recipe_stage_closure_invalid") from exc


def validate_production_recipe_executability(
    recipe: ProductionRecipeVersion,
) -> list[dict[str, Any]]:
    """Validate the exact configuration and complete stage chain, not live readiness."""
    stages = _recipe_stages(recipe)
    _validate_delivery_recipe_closure(stages)
    return stages


def _verify_run_snapshots(run: ProductionRun) -> None:
    from studio.services.production_creation_inputs import verified_recipe_creation_request, verify_recipe_stage_rows
    _, plan, _ = verified_recipe_creation_request(run)
    verify_recipe_stage_rows(run, plan)
    verify_json_contract(
        run.config_snapshot,
        schema_version=run.config_snapshot_schema_version,
        size_bytes=run.config_snapshot_size_bytes,
        sha256=run.config_snapshot_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )




def _selected_episodes(run: ProductionRun) -> list[Episode]:
    from studio.services.production_run_plans import get_run_execution_plan
    plan = get_run_execution_plan(run)
    scope = plan.value["scope"]
    queryset = Episode.objects.filter(project_id=plan.value["project_id"])
    if scope["kind"] == "episodes":
        ids = scope["episode_ids"]
        episodes = list(queryset.filter(pk__in=ids).order_by("number", "id"))
        if {str(item.id) for item in episodes} != set(ids):
            raise StageBlocked("frozen_episode_selection_drift", "冻结选集中的 Episode 已缺失或不再属于项目")
        return episodes
    # A project-scope plan allows content to be added by explicit production
    # actions. Each subsequent Preparation freezes the exact input revision;
    # final assembly freezes its own explicit adopted members.
    return list(queryset.order_by("number", "id"))


def _canonical_script(run: ProductionRun) -> dict[str, Any]:
    revision = run.script_revision
    if revision is None:
        return {"revision": None}
    return {
        "id": str(revision.id),
        "source_id": str(revision.source_id),
        "revision": revision.revision,
        "status": revision.status,
        "content_sha256": revision.content_sha256,
        "content_size_bytes": revision.content_size_bytes,
    }


def _canonical_existing_episode_selection(run: ProductionRun) -> dict[str, Any]:
    """Freeze the approved identities used by an existing-project run.

    Shot content remains editable until the explicit storyboard/manual gate;
    that later gate freezes the complete creative storyboard contract.
    """

    return {
        "project_id": str(run.project_id),
        "episodes": [
            {"id": str(item.id), "number": item.number}
            for item in _selected_episodes(run)
        ],
    }


def _canonical_bible(run: ProductionRun) -> dict[str, Any]:
    from studio.services.bible_setting_evidence import attach_project_setting_evidence
    common = (
        "id", "slug", "name", "description",
        "negative_constraints", "anchor_asset_id", "metadata", "is_locked",
    )
    typed = {
        "characters": (CharacterBible, common + (
            "stable_speaker_id", "role", "age_appearance", "face_identity",
            "wardrobe", "voice_profile", "personality",
        )),
        "scenes": (SceneBible, common + (
            "location", "time_of_day", "lighting", "color_palette", "ambient_sound",
        )),
        "props": (PropBible, common + ("material", "distinctive_marks", "continuity_state")),
    }
    result: dict[str, Any] = {}
    for key, (model, fields) in typed.items():
        rows = []
        for obj in model.objects.filter(project_id=run.project_id).only(*fields).prefetch_related("reference_assets").order_by("slug", "id"):
            row = {}
            for field in fields:
                value = getattr(obj, field)
                row[field] = str(value) if field in {"id", "anchor_asset_id"} and value else value
            row["reference_asset_ids"] = sorted(str(item.pk) for item in obj.reference_assets.all())
            rows.append(row)
        result[key] = rows
    return attach_project_setting_evidence(result, run.project_id)


def _bible_binding_asset_fact(
    asset: Asset,
    *,
    project: SeriesProject,
    entity_type: str,
    entity,
    material_facts=None,
) -> dict[str, Any]:
    """Verify the original Asset and, if generated, its real preparation."""
    from studio.services.production_bible_targets import (
        bible_target_content_matches, read_bible_candidate_target,
    )

    if entity.project_id != project.pk:
        raise ValidationError("Bible 目标不属于当前项目")
    if not material_facts or str(asset.pk) not in material_facts:
        from studio.services.bible_material_bindings import read_preserved_material_fact
        preserved = read_preserved_material_fact(entity, entity_type, asset.pk)
        if preserved is not None:
            material_facts = {str(asset.pk): preserved}
    if material_facts and str(asset.pk) in material_facts:
        fact = material_facts[str(asset.pk)]
        if (asset.owner_id not in (None, project.owner_id) or not asset.is_active
                or asset.checksum != fact["checksum"] or asset.mime_type != fact["mime_type"]
                or asset.kind != fact["kind"]):
            raise ValidationError("本轮已履约素材在审核过程中发生变化")
        return deepcopy(fact)
    _assert_bible_asset_eligible_for_entity(project=project, asset=asset, entity_type=entity_type)
    source_value = asset.file.path if asset.file else asset.local_path
    if not source_value:
        raise StageBlocked("bible_image_binding_asset_integrity", "Bible 绑定素材缺少可执行本地文件。")
    try:
        source = Path(source_value).resolve()
        facts = verified_local_media_facts(source, expected_kind="image", max_bytes=source.stat().st_size)
    except (OSError, ValueError, ProvenanceError) as exc:
        raise StageBlocked("bible_image_binding_asset_integrity", "Bible 绑定素材的真实图片字节不可验证。") from exc
    if facts["sha256"] != str(asset.checksum or "") or facts["mime_type"] != str(asset.mime_type or ""):
        raise StageBlocked("bible_image_binding_asset_integrity", "Bible 绑定素材的 MIME 或 checksum 已漂移。")
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    result = {
        "asset_id": str(asset.id), "kind": asset.kind, "mime_type": facts["mime_type"],
        "size_bytes": facts["size_bytes"], "checksum": facts["sha256"],
        "generated_artifact_id": None, "preparation_id": None,
        "execution_instance_version_id": None, "request_sha256": None,
        "target_snapshot_sha256": None, "source_artifact_sha256": None,
        "source_manifest_sha256": None,
    }
    artifacts = list(GeneratedArtifact.objects.filter(asset=asset,
        stage_run__stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY).select_related(
            "production_run__project", "production_run__owner", "stage_run", "generation_attempt__preparation")[:2])
    if not artifacts:
        if metadata.get("generation_purpose") in {BIBLE_IMAGE_GENERATION_PURPOSE, "production_media"}:
            raise ValidationError("Bible 绑定素材不再对应本项目的真实生成候选")
        return result
    if len(artifacts) != 1 or artifacts[0].production_run.project_id != project.pk:
        raise ValidationError("Bible 绑定素材不再对应本项目的真实生成候选")
    artifact = artifacts[0]
    artifact.asset = asset
    from studio.services.production_media_assets import read_registered_production_image
    read_registered_production_image(artifact)
    preparation, target = read_bible_candidate_target(artifact)
    target_sha = json_contract(target, max_bytes=_stage_snapshot_max_bytes()).sha256
    if target["scope"] != {"kind": entity_type, "id": str(entity.pk)} or target["project_id"] != str(project.pk):
        raise ValidationError("Bible 素材、目标、执行准备及原始归档不一致")
    if not bible_target_content_matches(target, entity):
        raise ProductionRunConflict({"detail": "Bible 目标内容已变化，请重新核对或生成参考图。"},
            code="bible_image_source_stale")
    result.update({
        "generated_artifact_id": str(artifact.pk), "preparation_id": str(preparation.pk),
        "execution_instance_version_id": str(preparation.execution_instance_version_id),
        "request_sha256": preparation.request_sha256, "target_snapshot_sha256": target_sha,
        "source_artifact_sha256": artifact.sha256, "source_manifest_sha256": artifact.manifest_metadata_sha256,
    })
    return result


def _canonical_bible_image_bindings(run: ProductionRun, *, material_context=None) -> dict[str, Any]:
    from studio.services.production_execution_units import ENTITY_SCOPE_MODELS, production_entity_queryset
    from studio.services.production_run_plans import get_run_execution_plan
    plan = get_run_execution_plan(run)
    expected = _bible_image_target_keys(run)
    bindings = []
    entities = ((kind, entity) for kind in ENTITY_SCOPE_MODELS
        for entity in production_entity_queryset(run, plan, kind).filter(
            pk__in=[pk for target_kind, pk in expected if target_kind == kind]
        ).select_related("anchor_asset").prefetch_related("reference_assets").order_by("id"))
    for entity_type, entity in entities:
        material_facts = (material_context or {}).get((entity_type, str(entity.pk)), {}).get("assets", {})
        anchor = (
            entity.anchor_asset
            if entity.anchor_asset_id else None
        )
        references = sorted(entity.reference_assets.all(), key=lambda asset: str(asset.pk))
        bindings.append({
            "entity_type": entity_type,
            "entity_id": str(entity.id),
            "anchor_asset_id": str(anchor.id) if anchor else None,
            "reference_asset_ids": [str(item.id) for item in references],
            "anchor_asset": (
                _bible_binding_asset_fact(
                    anchor,
                    project=run.project,
                    entity_type=entity_type,
                    entity=entity,
                    material_facts=material_facts,
                ) if anchor else None
            ),
            "reference_assets": [
                _bible_binding_asset_fact(
                    asset,
                    project=run.project,
                    entity_type=entity_type,
                    entity=entity,
                    material_facts=material_facts,
                )
                for asset in references
            ],
        })
    return {"bindings": sorted(bindings, key=lambda item: (
        item["entity_type"], item["entity_id"],
    ))}


def _canonical_storyboard(run: ProductionRun) -> dict[str, Any]:
    episodes = _selected_episodes(run)
    # Review freezes creative/reference facts. Execution capability comes from
    # the selected immutable instance, never an old route/model lookup here.
    return storyboard_content(project_id=run.project_id, episodes=episodes)






def _canonical_visual_rules(run: ProductionRun) -> dict[str, Any]:
    project = SeriesProject.objects.get(pk=run.project_id)
    snapshot = current_rules_snapshot(project)
    return {
        "project_id": str(run.project_id),
        "script_revision": _canonical_script(run),
        "rules": snapshot["rules"],
        "rules_sha256": snapshot["rules_sha256"],
    }


def _canonical_media(run: ProductionRun) -> dict[str, Any]:
    return {"artifacts": [
        {
            "id": str(item.id),
            "variant_group": item.variant_group,
            "variant_index": item.variant_index,
            "status": item.status,
            "kind": item.kind,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "object_key": item.object_key,
        }
        for item in GeneratedArtifact.objects.filter(
            production_run=run,
            stage_run__stage_key="media_generation",
            kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        ).order_by("variant_group", "variant_index", "id")
    ]}


def _bible_image_target_keys(run: ProductionRun, stage: StageRun | None = None) -> set[tuple[str, str]]:
    """Recipe scope comes from its frozen requirements, never today's project inventory."""
    from studio.services.production_execution_units import ENTITY_SCOPE_MODELS, production_entity_queryset
    from studio.services.production_run_plans import get_run_execution_plan
    plan = get_run_execution_plan(run)
    if run.plan_source == "recipe":
        from studio.services.production_recipe_material_units import frozen_recipe_material_units
        generation = stage if stage is not None and stage.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY else run.stages.filter(
            stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY, status=StageRun.Status.SUCCEEDED).first()
        if generation is None or generation.production_run_id != run.pk:
            raise ValidationError("缺少本轮冻结素材需求所属的生成阶段")
        units, _ = frozen_recipe_material_units(generation, plan)
        return {(unit["scope"]["kind"], unit["scope"]["id"]) for unit in units}
    return {(kind, str(pk)) for kind in ENTITY_SCOPE_MODELS
        for pk in production_entity_queryset(run, plan, kind).values_list("pk", flat=True)}


def _canonical_bible_image_candidates(run: ProductionRun, stage: StageRun) -> dict[str, Any]:
    """Freeze reviewed eligibility and exact targets, never an old prompt graph."""
    from studio.services.execution_production_candidates import read_production_media_candidate
    from studio.services.generated_artifacts import _artifact_limit
    from studio.services.production_bible_targets import (
        bible_target_content_matches, read_bible_target_contents, read_bible_candidate_target,
    )
    from studio.services.production_execution_units import ENTITY_SCOPE_MODELS, production_entity_queryset
    from studio.services.production_run_plans import get_run_execution_plan

    if stage.production_run_id != run.pk or stage.stage_key != BIBLE_IMAGE_GENERATION_STAGE_KEY:
        raise ValidationError("参考图候选目录必须属于同一运行的原生成步骤")
    plan = get_run_execution_plan(run)
    expected = _bible_image_target_keys(run, stage)
    entities, rows = {}, {}
    for kind in ENTITY_SCOPE_MODELS:
        for entity in production_entity_queryset(run, plan, kind).filter(
                pk__in=[pk for target_kind, pk in expected if target_kind == kind]).only(
                "id", "project_id", "name", "description").order_by("id"):
            key = (kind, str(entity.pk))
            entities[key] = entity
    if set(entities) != expected:
        raise ValidationError("本轮冻结素材需求引用的实体不存在或已离开运行范围")
    contents = read_bible_target_contents(entities, project_id=run.project_id)
    for key, entity in entities.items():
        rows[key] = {"entity_type": key[0], "entity_id": key[1], "entity_name": entity.name,
            "target_content_sha256": json_contract(contents[key], max_bytes=_stage_snapshot_max_bytes()).sha256,
            "candidates": []}
    artifacts = GeneratedArtifact.objects.filter(production_run__project=run.project,
        stage_run__stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY, kind="image").select_related(
            "production_run__project", "stage_run", "generation_attempt__preparation__execution_instance_version",
        ).order_by("created_at", "id")
    for artifact in artifacts.iterator(chunk_size=100):
        current = artifact.stage_run_id == stage.pk
        # Other in-progress rounds are not implicitly offered to this review.
        if not current and artifact.created_at >= stage.created_at:
            continue
        try:
            from studio.services.production_bible_targets import is_recipe_variant_candidate
            if is_recipe_variant_candidate(artifact):
                # Variants remain material versions, not entity-primary candidates.
                read_production_media_candidate(artifact, max_media_bytes=_artifact_limit(artifact.production_run))
                continue
            preparation, target = read_bible_candidate_target(artifact)
            key = (target["scope"]["kind"], target["scope"]["id"])
            entity = entities.get(key)
            if entity is None or not bible_target_content_matches(target, entity, content=contents[key]):
                raise ValidationError("参考图候选目标不在本轮范围或创作内容已变化")
            read_production_media_candidate(artifact, max_media_bytes=_artifact_limit(artifact.production_run))
        except ValidationError:
            if current:
                raise
            # Ineligible history stays in the read-only history surface with
            # its actual integrity/freshness status, not in the approval list.
            continue
        version = preparation.execution_instance_version
        rows[key]["candidates"].append({
            "artifact_id": str(artifact.pk), "variant_group": artifact.variant_group,
            "variant_index": artifact.variant_index, "eligibility": "eligible",
            "sha256": artifact.sha256, "manifest_sha256": artifact.manifest_metadata_sha256,
            "size_bytes": artifact.size_bytes, "mime_type": artifact.mime_type,
            "attempt_id": str(artifact.generation_attempt_id), "preparation_id": str(preparation.pk),
            "execution_instance_version_id": str(version.pk), "request_sha256": preparation.request_sha256,
            "prompt_profile_version_id": str(version.prompt_profile_version_id) if version.prompt_profile_version_id else None,
            "target_snapshot_sha256": json_contract(target, max_bytes=_stage_snapshot_max_bytes()).sha256,
            "origin_production_run_id": str(artifact.production_run_id),
            "origin_stage_run_id": str(artifact.stage_run_id), "historical": not current,
        })
    result = {"generation_stage_id": str(stage.pk), "generation_stage_attempt_count": stage.attempt_count,
        "entities": [rows[key] for key in sorted(rows)]}
    if run.plan_source == "recipe":
        from studio.services.production_recipe_material_fulfillment import (
            material_fulfillment_reference, read_recipe_material_fulfillment_choices,
        )
        # References to the original Run choices, not generated candidates
        # or another approval. Freeze and recheck through the same reader.
        from studio.services.production_recipe_material_review import (
            material_generation_reference, material_review_display, read_recipe_material_generations,
        )
        for field, choices, reference_for in (
            ("material_fulfillments", read_recipe_material_fulfillment_choices(stage=stage), material_fulfillment_reference),
            ("material_generations", read_recipe_material_generations(stage), material_generation_reference),
        ):
            if choices:
                result[field] = []
            for choice in choices:
                scope = choice["unit"]["scope"]
                key = (scope["kind"], scope["id"])
                if key not in rows:
                    raise ValidationError("已履约素材不属于本轮审核实体")
                reference = reference_for(choice)
                result[field].append(reference)
                rows[key].setdefault("fulfilled_materials", []).append(material_review_display(choice, reference))
    return result


def _formal_snapshot(run: ProductionRun, stage: StageRun, output_reference: str, *, review_payload=None) -> dict[str, Any]:
    from studio.services.production_project_text_stages import is_project_text_stage
    if is_project_text_stage(stage):
        candidates = _verify_stage_snapshot(stage, "output_snapshot")
        if candidates.get("kind") != "execution_candidates" or output_reference != f"stage-candidates:{stage.pk}":
            raise ValidationError("文本生成阶段缺少独立候选目录；不能用正式剧本代替")
        return {"schema_version": 1, "stage_key": stage.stage_key, "output_reference": output_reference,
            "formal_object_type": "", "canonical": {}, "canonical_sha256": _sha256({}), "review_payload": candidates}
    formal_type = ""
    canonical: dict[str, Any] = {}
    if stage.stage_key == "script_input" and stage.mode == "manual":
        formal_type, canonical = (
            "existing_episode_selection",
            _canonical_existing_episode_selection(run),
        )
    elif stage.stage_key in {"script_input", "script_review", "script_generate", "script_structure"}:
        formal_type, canonical = "script", _canonical_script(run)
    elif stage.stage_key == "visual_rules_review":
        formal_type, canonical = "visual_rules", _canonical_visual_rules(run)
    elif stage.stage_key in {"bible", "bible_generation", "bible_review"}:
        formal_type, canonical = "bible", _canonical_bible(run)
    elif stage.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY:
        formal_type, canonical = (
            "bible_image_candidate_manifest",
            _canonical_bible_image_candidates(run, stage),
        )
        if run.plan_source == "recipe":
            from studio.services.production_recipe_material_review import require_complete_recipe_material_results
            require_complete_recipe_material_results(stage, canonical)
    elif stage.stage_key == BIBLE_IMAGE_REVIEW_STAGE_KEY:
        existing = stage.output_snapshot if isinstance(stage.output_snapshot, dict) else {}
        if existing.get("kind") != "bible_image_bindings":
            raise ValidationError("Bible 参考图审核缺少冻结绑定结果")
        formal_type, canonical = "bible_image_bindings", deepcopy(existing)
    elif stage.stage_key in {"storyboard", "storyboard_review"}:
        formal_type, canonical = "storyboard", _canonical_storyboard(run)
    elif stage.stage_key in {"media_generation", "media_review", "artifact_selection"}:
        formal_type, canonical = "media_manifest", _canonical_media(run)
    payload = {
        "schema_version": 1,
        "stage_key": stage.stage_key,
        "output_reference": str(output_reference),
        "formal_object_type": formal_type,
        "canonical": canonical,
        "canonical_sha256": _sha256(canonical),
    }
    existing = stage.output_snapshot if isinstance(stage.output_snapshot, dict) else {}
    if review_payload is not None:
        payload["review_payload"] = deepcopy(review_payload)
    elif existing.get("kind") in {
        "script_reference_bible",
        "visual_rules_review_decision",
        "bible_image_bindings",
    }:
        payload["review_payload"] = existing
    return payload


def _save_stage_snapshot(stage: StageRun, field: str, value: dict[str, Any]) -> str:
    contract = json_contract(value, max_bytes=_stage_snapshot_max_bytes())
    setattr(stage, field, contract.value)
    setattr(stage, f"{field}_schema_version", contract.schema_version)
    setattr(stage, f"{field}_size_bytes", contract.size_bytes)
    setattr(stage, f"{field}_sha256", contract.sha256)
    stage.save(update_fields=(
        field, f"{field}_schema_version", f"{field}_size_bytes",
        f"{field}_sha256", "updated_at",
    ))
    return contract.sha256


def _verify_stage_snapshot(stage: StageRun, field: str) -> dict[str, Any]:
    value = getattr(stage, field)
    verify_json_contract(
        value,
        schema_version=getattr(stage, f"{field}_schema_version"),
        size_bytes=getattr(stage, f"{field}_size_bytes"),
        sha256=getattr(stage, f"{field}_sha256"),
        max_bytes=_stage_snapshot_max_bytes(),
    )
    return value


def _storyboard_drift_projection(value: dict[str, Any]) -> dict[str, Any]:
    episodes = value.get("episodes")
    if not isinstance(episodes, list):
        return value
    return {
        **value,
        "episodes": [
            {key: child for key, child in episode.items() if key != "status"}
            if isinstance(episode, dict) else episode
            for episode in episodes
        ],
    }


def _storyboard_creative_projection(value: dict[str, Any]) -> dict[str, Any]:
    """Selection is downstream of creative materialization, not new content.

    Only the four mutable exact-use choices are excluded at the source stage.
    The completed review still freezes and compares those same choices.
    """
    projected = deepcopy(_storyboard_drift_projection(value))
    for episode in projected.get("episodes", []):
        for shot in episode.get("shots", []):
            if "reference_uses" not in shot:
                continue
            shot["reference_uses"] = [{key: child for key, child in row.items()
                if key not in {"source_mode", "reference_version_id", "asset_id", "asset_sha256"}}
                for row in shot["reference_uses"]]
    return projected


def _bible_creative_drift_projection(value: dict[str, Any]) -> dict[str, Any]:
    """Exclude the separately approved Asset binding contract from creative Bible drift."""

    projected: dict[str, Any] = {}
    for section in ("characters", "scenes", "props"):
        rows = value.get(section)
        if not isinstance(rows, list):
            return value
        projected[section] = [
            {
                key: child
                for key, child in row.items()
                if key not in {"anchor_asset_id", "reference_asset_ids"}
            }
            if isinstance(row, dict) else row
            for row in rows
        ]
    return projected


def _bible_binding_drift_projection(value: dict[str, Any]) -> dict[str, Any]:
    rows = value.get("bindings")
    if not isinstance(rows, list):
        return value
    return {
        "bindings": sorted(({
            "entity_type": str(row.get("entity_type") or ""),
            "entity_id": str(row.get("entity_id") or ""),
            "anchor_asset": row.get("anchor_asset"),
            "reference_assets": sorted(
                (item for item in (row.get("reference_assets") or []) if isinstance(item, dict)),
                key=lambda item: str(item.get("asset_id") or ""),
            ),
        } for row in rows if isinstance(row, dict)), key=lambda item: (
            item["entity_type"], item["entity_id"],
        )),
    }


def _assert_frozen_formal_objects(run: ProductionRun) -> None:
    factories = {
        "script": _canonical_script,
        "existing_episode_selection": _canonical_existing_episode_selection,
        "bible": _canonical_bible,
        "storyboard": _canonical_storyboard,
        "visual_rules": _canonical_visual_rules,
        "media_manifest": _canonical_media,
        "bible_image_bindings": _canonical_bible_image_bindings,
    }
    latest: dict[str, StageRun] = {}
    for item in run.stages.filter(status=StageRun.Status.SUCCEEDED).order_by("ordinal"):
        snapshot = _verify_stage_snapshot(item, "output_snapshot")
        formal_type = str(snapshot.get("formal_object_type") or "")
        if formal_type in factories:
            latest[formal_type] = item
    for formal_type, item in latest.items():
        snapshot = item.output_snapshot
        frozen = snapshot.get("canonical")
        if not isinstance(frozen, dict) or _sha256(frozen) != snapshot.get("canonical_sha256"):
            raise StageBlocked(
                f"{formal_type}_snapshot_invalid",
                f"已审批/完成的 {formal_type} 正式对象快照无效。",
            )
        if formal_type == "bible_image_bindings":
            from studio.services.production_recipe_material_review import material_review_context
            current = _canonical_bible_image_bindings(run, material_context=material_review_context(run, frozen))
        elif formal_type == "storyboard":
            try:
                current = factories[formal_type](run)
            except ValidationError as exc:
                raise StageBlocked("storyboard_snapshot_drift",
                    "已审批/完成的分镜关系或原内容已无效：" + "; ".join(exc.messages)) from exc
        else:
            current = factories[formal_type](run)
        if formal_type == "storyboard":
            # Episode.status is a downstream lifecycle projection. Assembly
            # completion legitimately changes it from draft to completed, so it
            # must not participate in the immutable creative-input comparison.
            # Exact-use selection belongs to the later review, not its creative
            # source. A completed review still compares every selected fact.
            project_storyboard = (_storyboard_creative_projection if item.stage_key == "storyboard"
                else _storyboard_drift_projection)
            frozen_for_drift = project_storyboard(frozen)
            current_for_drift = project_storyboard(current)
        elif formal_type == "bible":
            frozen_for_drift = _bible_creative_drift_projection(frozen)
            current_for_drift = _bible_creative_drift_projection(current)
        elif formal_type == "bible_image_bindings":
            frozen_for_drift = _bible_binding_drift_projection(frozen)
            current_for_drift = _bible_binding_drift_projection(current)
        else:
            frozen_for_drift = frozen
            current_for_drift = current
        if _sha256(current_for_drift) != _sha256(frozen_for_drift):
            raise StageBlocked(
                f"{formal_type}_snapshot_drift",
                f"已审批/完成的 {formal_type} 正式对象发生漂移；请新建 ProductionRun。",
            )


def _assert_storyboard_review_source(
    run: ProductionRun,
    *,
    expected_source_snapshot_sha256: str,
) -> None:
    source_stage = run.stages.select_for_update().filter(
        stage_key="storyboard",
        status=StageRun.Status.SUCCEEDED,
    ).order_by("-ordinal").first()
    if source_stage is None:
        raise ProductionRunConflict(
            {"detail": "缺少已成功的 storyboard 来源快照，不能批准分镜。"},
            code="storyboard_snapshot_invalid",
        )
    snapshot = _verify_stage_snapshot(source_stage, "output_snapshot")
    public_snapshot_sha256 = json_contract(
        redact_sensitive_bindings(snapshot),
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256
    if expected_source_snapshot_sha256 != public_snapshot_sha256:
        raise ProductionRunConflict(
            {"detail": "页面展示的分镜来源快照已过期，请刷新后重新核对。"},
            code="storyboard_snapshot_stale",
        )
    frozen = snapshot.get("canonical")
    if (
        snapshot.get("formal_object_type") != "storyboard"
        or not isinstance(frozen, dict)
        or _sha256(frozen) != snapshot.get("canonical_sha256")
    ):
        raise ProductionRunConflict(
            {"detail": "冻结的 storyboard 正式对象快照无效。"},
            code="storyboard_snapshot_invalid",
        )
    current = _canonical_storyboard(run)
    if _sha256(_storyboard_creative_projection(current)) != _sha256(_storyboard_creative_projection(frozen)):
        raise ProductionRunConflict(
            {"detail": "分镜正式对象已在审核证据生成后变化；请重新生成审核证据后再批准。"},
            code="storyboard_snapshot_drift",
        )


def _lock_relation_rows(manager) -> None:
    list(manager.through.objects.select_for_update().filter(**{
        f"{manager.source_field_name}_id": manager.instance.pk,
    }).values_list("pk", flat=True))


def _freeze_stage_input(run: ProductionRun, stage: StageRun) -> None:
    if stage.production_run_id != run.pk:
        raise ValidationError("阶段不属于当前运行")
    from studio.services.production_recipe_media_stages import is_recipe_media_stage, recipe_media_input_contract
    if is_recipe_media_stage(stage):
        expected = recipe_media_input_contract(stage)
        if stage.input_snapshot_sha256:
            if _verify_stage_snapshot(stage, "input_snapshot") != expected.value:
                raise ValidationError("媒体阶段制作对象或冻结计划已不一致")
        else:
            _save_stage_snapshot(stage, "input_snapshot", expected.value)
        return
    from studio.services.production_project_text_stages import is_project_text_stage, is_project_text_review, project_text_input_contract
    if is_project_text_stage(stage) or is_project_text_review(stage):
        expected = project_text_input_contract(stage)
        if stage.input_snapshot_sha256:
            if _verify_stage_snapshot(stage, "input_snapshot") != expected.value:
                raise ValidationError("文本阶段输入引用与冻结RunPlan不一致")
        else:
            _save_stage_snapshot(stage, "input_snapshot", expected.value)
        return
    from studio.services.production_recipe_stage_snapshots import recipe_non_model_input_contract
    expected = recipe_non_model_input_contract(stage)
    if stage.input_snapshot_sha256:
        if _verify_stage_snapshot(stage, "input_snapshot") != expected.value:
            raise ValidationError("阶段输入与本轮计划或前置阶段已不一致")
    else:
        _save_stage_snapshot(stage, "input_snapshot", expected.value)


def _freeze_stage_config(run: ProductionRun, stage: StageRun) -> None:
    if stage.production_run_id != run.pk:
        raise ValidationError("阶段不属于当前运行")
    from studio.services.production_recipe_media_stages import is_recipe_media_stage, recipe_media_stage_contract
    if is_recipe_media_stage(stage):
        expected = recipe_media_stage_contract(stage)
        if stage.config_snapshot_sha256:
            if _verify_stage_snapshot(stage, "config_snapshot") != expected.value:
                raise ValidationError("媒体阶段配置与冻结实例路线已不一致")
        else:
            _save_stage_snapshot(stage, "config_snapshot", expected.value)
        return
    from studio.services.production_project_text_stages import is_project_text_stage, is_project_text_review, project_text_stage_contract
    if is_project_text_stage(stage) or is_project_text_review(stage):
        expected = project_text_stage_contract(stage)
        if stage.config_snapshot_sha256:
            if _verify_stage_snapshot(stage, "config_snapshot") != expected.value:
                raise ValidationError("文本阶段配置引用与冻结RunPlan不一致")
        else:
            _save_stage_snapshot(stage, "config_snapshot", expected.value)
        return
    from studio.services.production_recipe_stage_snapshots import recipe_non_model_config_contract
    expected = recipe_non_model_config_contract(stage)
    if stage.config_snapshot_sha256:
        if _verify_stage_snapshot(stage, "config_snapshot") != expected.value:
            raise ValidationError("阶段配置与本轮计划或冻结运行配置已不一致")
    else:
        _save_stage_snapshot(stage, "config_snapshot", expected.value)




def _save_routing_decision(stage: StageRun, decision: dict[str, Any]) -> None:
    contract = json_contract(decision, max_bytes=_stage_snapshot_max_bytes())
    stage.routing_decision = contract.value
    stage.routing_decision_schema_version = contract.schema_version
    stage.routing_decision_size_bytes = contract.size_bytes
    stage.routing_decision_sha256 = contract.sha256
    stage.save(update_fields=(
        "routing_decision", "routing_decision_schema_version",
        "routing_decision_size_bytes", "routing_decision_sha256", "updated_at",
    ))


def _append_route_event(
    stage: StageRun,
    *,
    unit_key: str,
    event_type: str,
    route_index: int | None = None,
    code: str = "",
) -> StageRouteEvent:
    sequence = (stage.route_events.aggregate(value=Max("sequence"))["value"] or 0) + 1
    return StageRouteEvent.objects.create(
        stage_run=stage,
        sequence=sequence,
        unit_key=unit_key[:191],
        route_index=route_index,
        event_type=event_type[:64],
        code=code[:100],
    )






















def _mark_route_selected(stage: StageRun, *, unit_key: str, route_index: int, reason: str, event_type="route_selected") -> None:
    _append_route_event(
        stage,
        unit_key=unit_key,
        route_index=route_index,
        event_type=event_type,
        code=reason,
    )
    decision = dict(stage.routing_decision or {})
    decision.update({"schema_version": 1, "state": "selected"})
    decision["last_unit_key"] = unit_key
    decision["selected_route_index"] = route_index
    decision["event_count"] = stage.route_events.count()
    _save_routing_decision(stage, decision)




def _decimal_amount(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise StageBlocked("cost_estimate_invalid", "driver 返回的费用估算不是有效数值") from exc
    if not amount.is_finite() or amount < 0:
        raise StageBlocked("cost_estimate_invalid", "driver 返回的费用估算必须是非负有限数")
    return amount.quantize(Decimal("0.000001"))


def _is_unattended_run(run: ProductionRun) -> bool:
    from studio.services.production_creation_inputs import recipe_unattended_authorized
    return recipe_unattended_authorized(run)






def _frozen_recipe_definition_sha256(run: ProductionRun) -> str:
    from studio.services.production_run_plans import get_run_execution_plan
    plan = get_run_execution_plan(run)
    if plan.value["source"] != "recipe":
        raise StageBlocked("run_plan_source_conflict", "人工制作没有配方来源，应读取本轮执行计划摘要")
    return plan.value["recipe"]["sha256"]








@registry_reference_writer
@security_boundary_resolution_cache()
def create_production_run(
    *,
    user,
    project,
    plan_source: str,
    script_revision: ScriptRevision | None,
    recipe: ProductionRecipeVersion | None,
    idempotency_key: str,
    allow_external_calls: bool | None,
    config_overrides: dict[str, Any] | None = None,
    idea: str = "",
    style_hint: str = "",
    episode_count: int | None = None,
    shots_per_episode: int | None = None,
    episode_duration_seconds: int | None = None,
    episode_ids: list[Any] | None = None,
    unattended_confirmed: bool = False,
    manual_plan: dict[str, Any] | None = None,
    project_source_confirmations: list[dict[str, Any]] | None = None,
) -> tuple[ProductionRun, bool]:
    if plan_source == "manual":
        if (recipe is not None or script_revision is not None or idea or style_hint or episode_count is not None
                or shots_per_episode is not None or episode_duration_seconds is not None or episode_ids
                or unattended_confirmed or project_source_confirmations):
            raise ValidationError("人工制作的初始内容和范围只在 manual_plan 中声明，不接收配方或独立模型/工作流选择")
        from studio.services.production_manual_creation import create_manual_run_root
        return create_manual_run_root(user=user, project=project, manual_plan=manual_plan, idempotency_key=idempotency_key,
            allow_external_calls=allow_external_calls,
            config_overrides={} if config_overrides is None else config_overrides)
    if plan_source != "recipe" or recipe is None or manual_plan is not None:
        raise ValidationError("必须明确选择配方生产或无配方人工制作，二者来源不能混合")
    # The registry writer serializes this check with creation/retirement. An
    # accepted request is verified by the sole initializer below, including its
    # exact intent and stage rows; mutable Recipe state cannot rewrite a replay.
    replay = isinstance(idempotency_key, str) and ProductionRun.objects.filter(
        owner=user, idempotency_key=idempotency_key.strip()).exists()
    if not replay:
        if recipe.definition_schema_version != 3:
            raise ValidationError("生产仅接受 schema3 配方；不转换或运行历史配方合同")
        if recipe.status != ProductionRecipeVersion.Status.PUBLISHED:
            raise ValidationError("只能使用 published recipe")
        validate_production_recipe_executability(recipe)
    from studio.services.production_recipe_creation import create_recipe_run_root
    return create_recipe_run_root(user=user, project=project, recipe=recipe, script_revision=script_revision,
        idempotency_key=idempotency_key, allow_external_calls=allow_external_calls,
        config_overrides={} if config_overrides is None else config_overrides,
        idea=idea, style_hint=style_hint, episode_count=episode_count, shots_per_episode=shots_per_episode,
        episode_duration_seconds=episode_duration_seconds, episode_ids=[] if episode_ids is None else episode_ids,
        unattended_confirmed=unattended_confirmed, project_source_confirmations=project_source_confirmations)


def _script_artifact(run: ProductionRun, stage: StageRun, *, create=False) -> GeneratedArtifact:
    from studio.services.production_run_plans import get_run_execution_plan, confirmed_script_reference
    from studio.services.execution_instances import execution_configuration_max_bytes
    from studio.services.script_revision_structures import freeze_script_input_structure
    plan = get_run_execution_plan(run)
    reference = plan.value["initial_content"]
    revision = run.script_revision
    if (stage.production_run_id != run.pk or stage.stage_key != "script_input" or stage.mode != "confirmed_revision"
            or reference is None or revision is None or reference["id"] != str(revision.pk)):
        raise StageBlocked("script_revision_missing", "输入阶段没有本轮明确冻结的已确认剧本")
    actual = confirmed_script_reference(plan.value["project_id"], {
        "kind": "script_revision", "id": reference["id"], "expected_sha256": reference["sha256"]})
    if actual != reference:
        raise ValidationError("本轮初始剧本确认事实已变化，不能替换或静默重新选择")
    metadata = {
        "schema_version": 2,
        "script_source_id": str(revision.source_id),
        "script_revision_id": str(revision.id),
        "revision": revision.revision,
        "structure_source": freeze_script_input_structure(project_id=run.project_id, reference=reference,
            max_bytes=execution_configuration_max_bytes()),
    }
    contract = json_contract(metadata, schema_version=2, max_bytes=_stage_snapshot_max_bytes())
    key = f"run:{run.id}:script:{revision.id}"
    existing = GeneratedArtifact.objects.only("id", "manifest_metadata", "manifest_schema_version",
        "manifest_metadata_size_bytes", "manifest_metadata_sha256", "production_run_id", "stage_run_id", "sha256",
        "size_bytes", "kind", "role", "status", "generation_attempt_id").filter(manifest_key=key).first()
    if existing is not None:
        saved = verify_json_contract(existing.manifest_metadata, schema_version=existing.manifest_schema_version,
            size_bytes=existing.manifest_metadata_size_bytes, sha256=existing.manifest_metadata_sha256,
            max_bytes=_stage_snapshot_max_bytes())
        if (saved.value != contract.value or existing.manifest_schema_version != 2
                or existing.production_run_id != run.pk or existing.stage_run_id != stage.pk
                or existing.sha256 != revision.content_sha256 or existing.size_bytes != revision.content_size_bytes
                or existing.kind != "script" or existing.role != "confirmed_script" or existing.status != "selected"
                or existing.generation_attempt_id is not None):
            raise ValidationError("初始剧本产物与已冻结的采用来源不一致，不能替换或重新生成")
        return existing
    if not create:
        raise ValidationError("初始剧本缺少原输入产物，不能在读取或审核中补造")
    artifact, _ = GeneratedArtifact.objects.get_or_create(
        manifest_key=key,
        defaults={
            "production_run": run,
            "stage_run": stage,
            "kind": GeneratedArtifact.Kind.SCRIPT,
            "role": "confirmed_script",
            "variant_group": "script",
            "variant_index": 1,
            "status": GeneratedArtifact.Status.SELECTED,
            "storage_backend": "database",
            "object_key": f"script-revisions/{revision.id}",
            "mime_type": "text/plain; charset=utf-8",
            "size_bytes": revision.content_size_bytes,
            "sha256": revision.content_sha256,
            "manifest_metadata": metadata,
            "manifest_schema_version": contract.schema_version,
            "manifest_metadata_size_bytes": contract.size_bytes,
            "manifest_metadata_sha256": contract.sha256,
        },
    )
    return artifact


def _execute_storyboard(run: ProductionRun, stage: StageRun) -> str:
    from studio.services.production_recipe_stage_snapshots import recipe_non_model_input_contract
    from studio.services.production_storyboard_materialization import materialize_declared_storyboard
    try:
        if stage.production_run_id != run.pk:
            raise ValidationError("阶段不属于当前运行")
        expected = recipe_non_model_input_contract(stage)
        if _verify_stage_snapshot(stage, "input_snapshot") != expected.value:
            raise ValidationError("拆稿来源或冻结参数已变化，不重新冻结或覆盖已有内容")
        source = expected.value["materialization"]
        result = materialize_declared_storyboard(run=run, stage=stage,
            expected_contract_sha256=source["sha256"])
    except (ValidationError, ConfigurationResolutionError) as exc:
        raise StageBlocked(getattr(exc, "code", None) or "storyboard_materialization_invalid", str(exc)) from exc
    return f"episodes:{len(result.episodes)}"






def _bible_entities(project) -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    characters = list(
        project.characters.select_related("project").order_by("slug", "id")
    )
    rows.extend(
        ("character", item)
        for item in visual_reference_characters(characters)
    )
    for entity_type, relation in (
        ("scene", project.scenes),
        ("prop", project.props),
    ):
        rows.extend(
            (entity_type, item)
            for item in relation.select_related("project").order_by("slug", "id")
        )
    return rows


















def _definitive_media_failure(attempt: GenerationAttempt) -> tuple[bool, str]:
    if attempt.status != GenerationAttempt.Status.FAILED:
        return False, attempt.error_code or attempt.status
    if attempt.operation in ASYNC_MEDIA_OPERATIONS:
        code = attempt.error_code or "async_generation_failed"
        return code in {
            "provider_request_rejected",
            "remote_generation_failed",
            "remote_absence_confirmed",
        }, code
    task = attempt.generation_task
    if task is None:
        return False, "generation_task_missing"
    if task.status != GenerationTask.Status.FAILED:
        return False, "generation_task_not_definitive_failure"
    from studio.services.execution_production_attempts import attempt_has_artifacts
    has_outputs = attempt.retry_task_has_outputs if hasattr(attempt, "retry_task_has_outputs") else bool(task.outputs)
    has_task_artifacts = (attempt.retry_task_has_artifacts if hasattr(attempt, "retry_task_has_artifacts")
        else task.generated_artifacts.exists())
    if has_outputs or attempt_has_artifacts(attempt) or has_task_artifacts:
        return False, "generation_result_or_archive_present"
    if (
        task.submission_state == GenerationTask.SubmissionState.UNPREPARED
        and not task.submission_token
        and not task.comfy_prompt_id
    ):
        return True, "comfy_pre_submit_failed"
    if task.submission_state == GenerationTask.SubmissionState.TERMINAL_FAILED:
        return True, "comfy_remote_terminal_failed"
    return False, "comfy_submission_state_uncertain"




def _poll_media_stage(run: ProductionRun, stage: StageRun) -> str | None:
    from studio.services.production_recipe_media_stages import advance_recipe_media_stage
    return advance_recipe_media_stage(run, stage)


def _selected_assembly_sources(run: ProductionRun) -> dict[str, dict[str, str]]:
    """Resolve current formal selections, never task names or last completion."""
    from studio.services.assembly import _assembly_artifact_evidence, _assembly_source_label

    shots = {str(shot.pk): shot for shot in Shot.objects.filter(
        episode__in=_selected_episodes(run)).select_related("episode").order_by("episode_id", "number", "id")}
    if not shots:
        raise AssemblyError("本轮没有可装配的正式镜头")
    selected_sources: dict[str, dict[str, str]] = {}
    candidates = GeneratedArtifact.objects.filter(production_run=run, stage_run__stage_key="media_generation",
        kind__in=("image", "video"), status="selected").select_related("production_run__project", "stage_run")
    for artifact in candidates:
        _, shot = _assembly_artifact_evidence(artifact)
        key = str(shot.pk)
        if key not in shots or key in selected_sources:
            raise AssemblyError("已采用产物越过本轮镜头范围或重复覆盖同一镜头，请明确重新选片")
        selected_sources[key] = {"generated_artifact_id": str(artifact.pk)}
    if set(selected_sources) != set(shots):
        missing = [shot for key, shot in shots.items() if key not in selected_sources]
        first = min(missing, key=lambda shot: (shot.episode.number, shot.number))
        raise AssemblyError(f"本轮逐镜已采用媒体不完整，缺少 {len(missing)} 项；"
            f"首项：{_assembly_source_label(first)}。不能使用最新任务或其他候选补齐")
    return selected_sources


def _execute_assembly(run: ProductionRun, stage: StageRun) -> str | None:
    from studio.models import AssemblyJob
    from studio.services.production_recipe_stage_snapshots import recipe_assembly_configuration, recipe_assembly_sources

    configuration = recipe_assembly_configuration(stage)
    episodes = _selected_episodes(run)
    if not episodes:
        raise StageBlocked("assembly_episodes_missing", "逐集组装前没有正式 Episode")
    selected_sources = recipe_assembly_sources(stage)
    for episode in episodes:
        if not episode.shots.exists():
            raise StageBlocked(
                "episode_master_source_missing",
                f"Episode {episode.number} 没有 Shot，不能创建母版。",
            )
        link = ProductionAssemblyLink.objects.filter(
            production_run=run,
            episode=episode,
        ).select_related("assembly_job").first()
        # A terminal worker failure is a durable operator-visible fact. Never
        # manufacture a new FFmpeg job while merely refreshing orchestration;
        # the explicit ProductionRun retry action performs that new enqueue.
        if link is not None:
            continue
        try:
            job = create_assembly(
                owner=run.project.owner,
                project=run.project,
                episode=episode,
                dry_run=False,
                frozen_sources={"schema_version": 1, "episodes": [row for row in selected_sources["episodes"]
                    if row["episode_id"] == str(episode.pk)]},
                frozen_configuration=configuration,
                consumer_stage=stage,
            )
        except AssemblyError as exc:
            raise StageBlocked("assembly_enqueue_failed", str(exc)) from exc
        ProductionAssemblyLink.objects.create(
            production_run=run,
            stage_run=stage,
            episode=episode,
            assembly_job=job,
        )
    links = list(ProductionAssemblyLink.objects.filter(
        production_run=run,
        stage_run=stage,
    ).select_related("episode", "assembly_job").order_by("episode__number"))
    if len(links) != len(episodes):
        raise StageBlocked("assembly_links_incomplete", "逐集 AssemblyJob 绑定不完整")
    active = [
        link for link in links
        if link.assembly_job.status in {
            AssemblyJob.Status.PLANNED,
            AssemblyJob.Status.QUEUED,
            AssemblyJob.Status.RUNNING,
        }
    ]
    failures = [link for link in links if link.assembly_job.status == AssemblyJob.Status.FAILED]
    succeeded = [link for link in links if link.assembly_job.status == AssemblyJob.Status.SUCCEEDED]
    for link in succeeded:
        record_assembly_artifact(
            link.assembly_job,
            production_run=run,
            stage_run=stage,
        )
    if failures:
        budgets = "、".join(f"第{link.episode.number}集 {link.assembly_job.attempts}/{link.assembly_job.max_attempts}次"
            for link in failures)
        raise StageBlocked(
            "assembly_partial_failure" if succeeded else "assembly_failed",
            f"逐集组装 {len(succeeded)} 集成功、{len(failures)} 集失败（{budgets}，含首次）。自动恢复已停止，等待人工处理；成功母版已归档，不会重做。",
        )
    if active:
        stage.output_reference = f"episode-assembly-jobs:{len(links)}"
        stage.save(update_fields=("output_reference", "updated_at"))
        return None
    masters = GeneratedArtifact.objects.filter(
        production_run=run,
        kind=GeneratedArtifact.Kind.ASSEMBLY,
        role="episode_master",
        status=GeneratedArtifact.Status.SELECTED,
    )
    if masters.count() != len(episodes):
        raise StageBlocked("episode_master_manifest_incomplete", "逐集母版 artifact 清单不完整")
    return f"episode-masters:{masters.count()}"


def _execute_delivery(run: ProductionRun, stage: StageRun) -> str:
    links = list(ProductionAssemblyLink.objects.filter(
        production_run=run,
    ).select_related("episode", "assembly_job").order_by("episode__number"))
    episodes = _selected_episodes(run)
    if len(links) != len(episodes) or any(
        link.assembly_job.status != "succeeded" for link in links
    ):
        raise StageBlocked("episode_master_incomplete", "逐集母版任务未全部成功，不能交付")
    bundle = _seal_run_release_bundle(run, stage, links)
    return f"release-bundle:{bundle.id}"


def _seal_run_release_bundle(
    run: ProductionRun,
    stage: StageRun,
    links: list[ProductionAssemblyLink],
) -> ReleaseBundle:
    existing = ReleaseBundle.objects.filter(production_run=run).first()
    if existing is not None and existing.status == ReleaseBundle.Status.SEALED:
        try:
            live_verified_release_bundle(existing)
        except ReleaseBundleError as exc:
            raise StageBlocked("release_bundle_evidence_invalid", str(exc)) from exc
        return existing
    selected_ids = [item.id for item in _selected_episodes(run)]
    shots = list(run.project.episodes.filter(pk__in=selected_ids).order_by("number").prefetch_related("shots"))
    ordered_shots = [shot for episode in shots for shot in episode.shots.all()]
    if not ordered_shots:
        raise StageBlocked("delivery_shots_missing", "交付清单没有正式 Shot")
    try:
        source_refs = _selected_assembly_sources(run)
    except AssemblyError as exc:
        raise StageBlocked("delivery_artifact_selection_incomplete", str(exc)) from exc
    artifacts = {str(item.pk): item for item in GeneratedArtifact.objects.filter(
        pk__in=[ref["generated_artifact_id"] for ref in source_refs.values()], production_run=run,
    ).select_related("generation_attempt__preparation", "generation_task", "production_run__project", "stage_run")}
    if len(artifacts) != len({ref["generated_artifact_id"] for ref in source_refs.values()}):
        raise StageBlocked("delivery_artifact_selection_incomplete", "本轮交付引用的正式媒体已缺失")
    by_shot = {key: artifacts[ref["generated_artifact_id"]] for key, ref in source_refs.items()}
    shot_selection = []
    for shot in ordered_shots:
        artifact = by_shot[str(shot.id)]
        # Encoding belongs to the original worker. seal_release_bundle verifies
        # existing copies and fails explicitly if one is missing or corrupt.
        shot_selection.append({
            "shot_id": str(shot.id),
            "artifact_id": str(artifact.id),
        })
    episode_selection = []
    for link in links:
        master = GeneratedArtifact.objects.filter(
            production_run=run,
            stage_run_id=link.stage_run_id,
            manifest_key=f"assembly:{link.assembly_job_id}",
            kind=GeneratedArtifact.Kind.ASSEMBLY,
            role="episode_master",
            status=GeneratedArtifact.Status.SELECTED,
        ).first()
        if master is None:
            raise StageBlocked(
                "delivery_episode_master_missing",
                f"Episode {link.episode_id} 缺少已归档的 selected 母版产物。",
            )
        episode_selection.append({
            "episode_id": str(link.episode_id),
            "artifact_id": str(master.id),
        })
    selection = {"shots": shot_selection, "episodes": episode_selection, "series_artifact_id": None}
    bundle = existing or create_release_draft(
        production_run=run, selection=selection,
        project=run.project,
        owner=run.project.owner,
        created_by=run.owner,
        title=f"{run.project.title} · ProductionRun 逐集交付",
        notes=f"由 ProductionRun {run.id} 生成；不包含默认整季长视频。",
    )
    if bundle.status != ReleaseBundle.Status.DRAFT:
        raise StageBlocked("release_bundle_state_invalid", "ProductionRun ReleaseBundle 状态无效")
    bundle.production_run = run
    bundle.selection = selection
    bundle.save(update_fields=("production_run", "selection", "updated_at"))
    try:
        return seal_release_bundle(bundle)
    except ReleaseBundleError as exc:
        raise StageBlocked("release_bundle_seal_failed", str(exc)) from exc




def _execute_bible_materialize(run: ProductionRun, stage: StageRun) -> str:
    from studio.services.execution_instances import execution_configuration_max_bytes
    from studio.services.script_revision_structures import read_recipe_script_structure
    try:
        adopted = read_recipe_script_structure(stage=stage, max_bytes=execution_configuration_max_bytes())
        frozen = freeze_script_reference_bible(revision=run.script_revision, project_id=run.project_id,
            expected_structure_source=adopted["source"])
    except ValidationError as exc:
        raise StageBlocked("script_bible_invalid", "; ".join(exc.messages)) from exc
    _save_stage_snapshot(stage, "output_snapshot", frozen)
    return f"script-reference-bible:{frozen['declaration_sha256']}"


def _approve_generated_script(
    run: ProductionRun,
    stage: StageRun,
    user,
    *,
    text_candidate,
    expected_source_snapshot_sha256: str,
    script_content=None,
) -> dict[str, Any]:
    evidence = approve_frozen_script_candidate(
        run=run,
        review_stage=stage,
        user=user,
        text_candidate=text_candidate,
        expected_source_snapshot_sha256=expected_source_snapshot_sha256,
        script_content=script_content,
    )
    # The approval transaction stores one formal snapshot after validation;
    # do not persist a different intermediate shape for downstream readers.
    return evidence


def _approve_bible_content(run: ProductionRun, *, user, confirmation, comment):
    from studio.services.bible_content_review import approve_bible_content, recipe_bible_review_source
    if not isinstance(confirmation, dict) or set(confirmation) != {
            "expected_source_snapshot_sha256", "expected_project_bible_sha256", "reviewed_candidate_ids",
            "reviewed_setting_keys", "reviewed_project_bible"}:
        raise ValidationError("Bible审核必须提交已展示的来源、项目基线和完整确认集合")
    frozen, source_sha = recipe_bible_review_source(run)
    if confirmation["expected_source_snapshot_sha256"] != source_sha:
        raise ValidationError("Bible声明来源已变化，请重新读取后审核")
    return approve_bible_content(run=run, actor=user, frozen=frozen, comment=comment,
        **{key: value for key, value in confirmation.items() if key != "expected_source_snapshot_sha256"})


class _StoredAssetFileGuard:
    """Delete newly written Asset files when the surrounding DB commit fails."""

    _current = ContextVar("stored_asset_file_guard", default=None)

    def __init__(self) -> None:
        self._stored: list[tuple[Any, str]] = []

    def track(self, field_file) -> None:
        name = str(getattr(field_file, "name", "") or "")
        if name:
            self._stored.append((field_file.storage, name))

    def track_path(self, path) -> None:
        """Only newly created files under the formal media root can roll back."""
        from django.core.files.storage import FileSystemStorage
        candidate = Path(path).resolve()
        root = Path(settings.MEDIA_ROOT).resolve()
        relative = candidate.relative_to(root)
        if candidate.exists():
            raise ValidationError("不能将已有媒体文件登记为本次事务的新文件")
        self._stored.append((FileSystemStorage(location=str(root)), relative.as_posix()))

    def __enter__(self):
        self._parent = self._current.get()
        self._token = self._current.set(self)
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        self._current.reset(self._token)
        if exc_type is not None:
            for storage, name in reversed(self._stored):
                try:
                    storage.delete(name)
                except Exception:
                    logger.exception("Failed to rollback Bible reference Asset file")
        elif self._parent is not None:
            # A nested selection/repreparation succeeded, but its enclosing
            # command receipt and commit can still fail. Keep those files in
            # the outer guard until that transaction has also succeeded.
            self._parent._stored.extend(self._stored)
        return False


def _locked_bible_entity(run: ProductionRun, entity_type: str, entity_id: str):
    model = {
        "character": CharacterBible,
        "scene": SceneBible,
        "prop": PropBible,
    }.get(entity_type)
    if model is None:
        raise ValidationError("Bible 参考图实体类型无效")
    try:
        return model.objects.select_for_update().prefetch_related(
            "reference_assets",
        ).get(pk=entity_id, project=run.project)
    except model.DoesNotExist as exc:
        raise ValidationError("Bible 参考图实体不存在或不属于当前项目") from exc


def _lock_bible_reference_rows(entity) -> None:
    manager = entity.reference_assets
    list(manager.through.objects.select_for_update().filter(**{
        f"{manager.source_field_name}_id": entity.pk,
    }).values_list("pk", flat=True))


def _assert_bible_asset_eligible_for_entity(
    *,
    project: SeriesProject,
    asset: Asset,
    entity_type: str,
) -> None:
    expected_kind = {
        "character": Asset.Kind.CHARACTER,
        "scene": Asset.Kind.SCENE,
        "prop": Asset.Kind.PROP,
    }[entity_type]
    if asset.owner_id not in (None, project.owner_id):
        raise ValidationError("Bible 素材不属于项目所有者或全局素材库")
    if not asset.is_active:
        raise ValidationError(f"Bible 素材 {asset.name} 已停用，不能选择或保留")
    if str((asset.metadata or {}).get("usage") or "") == "project_cover":
        raise ValidationError(f"Bible 素材 {asset.name} 是项目封面，不能用作实体参考图")
    if not str(asset.mime_type or "").casefold().startswith("image/"):
        raise ValidationError(f"Bible 素材 {asset.name} 不是已验证图片")
    if asset.kind == Asset.Kind.REFERENCE:
        category = str((asset.metadata or {}).get("reference_category") or "").casefold()
        if category != expected_kind:
            raise ValidationError(
                f"Bible 通用素材 {asset.name} 未声明 reference_category={expected_kind}",
            )
    elif asset.kind != expected_kind:
        raise ValidationError(
            f"Bible 素材 {asset.name} 类别 {asset.kind} 与 {expected_kind} 不一致",
        )


def _locked_selected_bible_library_assets(
    run: ProductionRun,
    *,
    entity,
    entity_type: str,
    asset_selections: list[dict[str, Any]],
    material_facts=None,
) -> dict[str, Asset]:
    if not asset_selections:
        return {}
    expected_by_id = {
        str(item["asset_id"]): str(item["expected_sha256"])
        for item in asset_selections
    }
    asset_ids = list(expected_by_id)
    assets = {
        str(item.id): item
        for item in Asset.objects.select_for_update().filter(pk__in=asset_ids)
    }
    if set(assets) != set(asset_ids):
        raise ValidationError("所选素材库参考图不存在或已删除")
    for asset in assets.values():
        fact = _bible_binding_asset_fact(
            asset,
            project=run.project,
            entity_type=entity_type,
            entity=entity,
            material_facts=material_facts,
        )
        if fact["checksum"] != expected_by_id[str(asset.id)]:
            raise ProductionRunConflict(
                {"detail": f"素材库图片 {asset.name} 已变化，请刷新后重新核对。"},
                code="bible_library_asset_snapshot_stale",
            )
    return assets


def _locked_preserved_bible_assets(
    run: ProductionRun,
    *,
    entity,
    entity_type: str,
    material_facts=None,
) -> dict[str, Asset]:
    _lock_bible_reference_rows(entity)
    ids = {
        str(item) for item in entity.reference_assets.values_list("id", flat=True)
    }
    if entity.anchor_asset_id:
        ids.add(str(entity.anchor_asset_id))
    if not ids:
        return {}
    assets = {
        str(item.id): item
        for item in Asset.objects.select_for_update().filter(pk__in=ids)
    }
    if set(assets) != ids:
        raise ValidationError("Bible 现有 anchor/reference_assets 包含已删除素材")
    # The common fact reader validates original versioned receipts as well as
    # ordinary library eligibility when partitioning below; do not pre-reject
    # a formally adopted generic upload merely because of its Asset category.
    return assets


def _partition_current_preserved_bible_assets(
    run: ProductionRun,
    *,
    entity,
    entity_type: str,
    assets: dict[str, Asset],
    material_facts=None,
) -> tuple[dict[str, Asset], list[str]]:
    """Keep only bindings whose formal prompt source still matches the entity.

    Stale generated images remain immutable Assets and historical artifacts, but
    must not silently continue as current semantic references after a Bible or
    style edit.  Integrity/provenance failures remain hard errors.
    """

    current: dict[str, Asset] = {}
    removed_stale: list[str] = []
    for asset_id, asset in sorted(assets.items()):
        try:
            _bible_binding_asset_fact(
                asset,
                project=run.project,
                entity_type=entity_type,
                entity=entity,
                material_facts=material_facts,
            )
        except ProductionRunConflict as exc:
            if exc.code != "bible_image_source_stale":
                raise
            removed_stale.append(asset_id)
        else:
            current[asset_id] = asset
    return current, removed_stale


def _register_bible_candidate_asset(
    *,
    run: ProductionRun,
    review_stage: StageRun,
    artifact: GeneratedArtifact,
    entity_type: str,
    entity,
    binding_role: str,
    asset_guard: _StoredAssetFileGuard,
) -> Asset:
    from studio.services.production_bible_targets import bible_target_value, read_bible_candidate_target

    if (not transaction.get_connection().in_atomic_block or artifact.asset_id is not None
            or review_stage.production_run_id != run.pk or review_stage.stage_key != "bible_image_review"
            or artifact.production_run.project_id != run.project_id or entity.project_id != run.project_id
            or binding_role not in {"anchor", "reference"}):
        raise ValidationError("参考图入库必须在本轮审核事务中执行，不能重复复制或改派来源")
    _, target = read_bible_candidate_target(artifact)
    current = bible_target_value(entity, {"kind": entity_type, "id": str(entity.pk)},
        entity.reference_assets.values_list("id", flat=True))
    if target != current:
        raise ProductionRunConflict({"detail": "参考图目标内容或原素材关系已变化，请重新核对。"},
            code="bible_image_source_stale")
    from studio.services.production_media_assets import register_production_image_asset
    return register_production_image_asset(artifact=artifact, kind=entity_type,
        name=str(entity.name or "").strip(), description=str(entity.description or "").strip(),
        asset_guard=asset_guard)


def _approve_bible_image_bindings(
    run: ProductionRun,
    review_stage: StageRun,
    user,
    *,
    bindings: list[dict[str, Any]],
    expected_source_snapshot_sha256: str,
    asset_guard: _StoredAssetFileGuard,
    selection_mode="manual",
) -> None:
    locked_project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    run.project = locked_project
    generation_stage = run.stages.select_for_update().filter(
        stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
        status=StageRun.Status.SUCCEEDED,
    ).first()
    if generation_stage is None:
        raise ValidationError("缺少已完成的 bible_image_generation 阶段")
    generation_snapshot = _verify_stage_snapshot(generation_stage, "output_snapshot")
    public_sha = json_contract(
        redact_sensitive_bindings(generation_snapshot),
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256
    if expected_source_snapshot_sha256 != public_sha:
        raise ProductionRunConflict(
            {"detail": "页面展示的 Bible 参考图候选快照已过期，请刷新后重新核对。"},
            code="bible_image_snapshot_stale",
        )
    canonical = generation_snapshot.get("canonical")
    if (
        generation_snapshot.get("formal_object_type") != "bible_image_candidate_manifest"
        or not isinstance(canonical, dict)
        or canonical.get("generation_stage_id") != str(generation_stage.id)
    ):
        raise ValidationError("Bible 参考图生成快照无效")
    current_canonical = _canonical_bible_image_candidates(run, generation_stage)
    if _sha256(current_canonical) != generation_snapshot.get("canonical_sha256"):
        raise ProductionRunConflict(
            {"detail": "Bible 参考图候选清单已变化，请刷新后重新核对。"},
            code="bible_image_candidate_manifest_stale",
        )
    from studio.services.production_recipe_material_review import material_review_context
    result_rows = _apply_bible_image_bindings(run, review_stage, user, bindings=bindings,
        canonical=canonical, asset_guard=asset_guard, material_context=material_review_context(run, canonical),
        selection_mode=selection_mode)
    payload = {
        "schema_version": 1,
        "kind": "bible_image_bindings",
        "generation_stage_id": str(generation_stage.id),
        "generation_snapshot_sha256": generation_stage.output_snapshot_sha256,
        "public_generation_snapshot_sha256": public_sha,
        "bindings": result_rows,
    }
    for field in ("material_fulfillments", "material_generations"):
        if canonical.get(field):
            payload[field] = deepcopy(canonical[field])
    _save_stage_snapshot(review_stage, "output_snapshot", payload)
    from studio.services.bible_material_bindings import record_binding_review
    record_binding_review(review_stage, result_rows)


def _apply_bible_image_bindings(run, review_stage, user, *, bindings, canonical, asset_guard,
        material_context=None, selection_mode="manual"):
    """Common original multi-reference writer, called inside a reviewed command.

    The caller validates its own Recipe/manual review revision and candidate
    snapshot. This writer never completes a producer or manufactures one.
    """
    from studio.services.bible_image_selections import normalize_bible_image_selection
    from studio.services.execution_production_candidates import read_production_media_candidate
    from studio.services.generated_artifacts import _artifact_limit
    from studio.services.production_bible_targets import bible_target_content_matches, read_bible_candidate_target
    bindings = [normalize_bible_image_selection(item, maximum_assets=_bible_reference_asset_max_items()) for item in bindings]
    if selection_mode not in {"manual", "automatic"}:
        raise ValidationError("参考图选择来源必须明确为人工或自动")
    from studio.services.production_recipe_material_review import (
        normalize_material_review_selection, validate_material_review_selection, require_complete_recipe_material_results,
    )
    if run.plan_source == "recipe":
        generation = run.stages.get(stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY)
        require_complete_recipe_material_results(generation, canonical)
    bindings = [normalize_material_review_selection(selection, material_context or {}) for selection in bindings]
    for selection in bindings:
        validate_material_review_selection(selection, material_context or {})
    if (not transaction.get_connection().in_atomic_block or review_stage.production_run_id != run.pk
            or review_stage.stage_key != BIBLE_IMAGE_REVIEW_STAGE_KEY):
        raise ValidationError("参考图写回需要同轮正式审核事务")
    run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    expected_entities = {
        (str(item.get("entity_type") or ""), str(item.get("entity_id") or ""))
        for item in canonical.get("entities", [])
        if isinstance(item, dict)
    }
    provided_entities = {
        (str(item.get("entity_type") or ""), str(item.get("entity_id") or ""))
        for item in bindings
        if isinstance(item, dict)
    }
    if (
        len(provided_entities) != len(bindings)
        or provided_entities != expected_entities
        or expected_entities != _bible_image_target_keys(run, review_stage)
    ):
        raise ValidationError("Bible 参考图审核必须且只能逐实体提交完整选择")
    allowed_by_entity: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for item in canonical.get("entities", []):
        key = (str(item["entity_type"]), str(item["entity_id"]))
        allowed_by_entity[key] = {str(candidate["artifact_id"]): candidate for candidate in item["candidates"]}
    locked_artifacts = {
        str(item.id): item
        for item in GeneratedArtifact.objects.select_for_update().select_related(
            "generation_attempt__preparation__execution_instance_version",
            "production_run__project", "stage_run",
            "asset",
        ).filter(
            pk__in={
                artifact_id
                for artifact_ids in allowed_by_entity.values()
                for artifact_id in artifact_ids
            },
            production_run__project=run.project,
            stage_run__stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
            kind=GeneratedArtifact.Kind.IMAGE,
        )
    }
    result_rows: list[dict[str, Any]] = []
    claimed_final_asset_ids: set[str] = set()
    for selection in sorted(bindings, key=lambda item: (item["entity_type"], str(item["entity_id"]))):
        entity_type = str(selection["entity_type"])
        entity_id = str(selection["entity_id"])
        material_facts = (material_context or {}).get((entity_type, entity_id), {}).get("assets", {})
        selected_ids = [str(item) for item in (selection.get("selected_artifact_ids") or [])]
        selected_library_rows = list(selection.get("selected_library_assets") or [])
        selected_library_ids = [str(item["asset_id"]) for item in selected_library_rows]
        anchor_artifact_id = str(selection.get("anchor_artifact_id") or "")
        anchor_library_asset_id = str(
            selection.get("anchor_library_asset_id") or ""
        )
        if set(selected_ids) - set(allowed_by_entity[(entity_type, entity_id)]):
            raise ValidationError("所选参考图候选不属于本次已核对目录")
        entity = _locked_bible_entity(run, entity_type, entity_id)
        selected_library_assets = _locked_selected_bible_library_assets(
            run,
            entity=entity,
            entity_type=entity_type,
            asset_selections=selected_library_rows,
            material_facts=material_facts,
        )
        preserved_assets = _locked_preserved_bible_assets(
            run,
            entity=entity,
            entity_type=entity_type,
            material_facts=material_facts,
        )
        preserved_assets, removed_stale_asset_ids = (
            _partition_current_preserved_bible_assets(
                run,
                entity=entity,
                entity_type=entity_type,
                assets=preserved_assets,
                material_facts=material_facts,
            )
        )
        created_assets: dict[str, Asset] = {}
        for artifact_id in selected_ids:
            artifact = locked_artifacts.get(artifact_id)
            expected_candidate = allowed_by_entity[(entity_type, entity_id)][artifact_id]
            if (artifact is None or artifact.sha256 != expected_candidate["sha256"]
                    or artifact.manifest_metadata_sha256 != expected_candidate["manifest_sha256"]):
                raise ValidationError("所选 Bible 参考图不存在或与已核对的归档摘要不一致")
            preparation, target = read_bible_candidate_target(artifact)
            if target["scope"] != {"kind": entity_type, "id": entity_id} or not bible_target_content_matches(target, entity):
                raise ValidationError("所选参考图与当前明确目标及创作内容不一致")
            read_production_media_candidate(artifact, max_media_bytes=_artifact_limit(artifact.production_run))
            if artifact.asset_id:
                asset = Asset.objects.select_for_update().get(pk=artifact.asset_id)
                fact = _bible_binding_asset_fact(
                    asset,
                    project=run.project,
                    entity_type=entity_type,
                    entity=entity,
                )
                if fact["generated_artifact_id"] != str(artifact.id):
                    raise ValidationError(
                        "历史 Bible 参考图 Asset 与 GeneratedArtifact provenance 不一致",
                    )
            else:
                asset = _register_bible_candidate_asset(
                    run=run,
                    review_stage=review_stage,
                    artifact=artifact,
                    entity_type=entity_type,
                    entity=entity,
                    binding_role=("anchor" if artifact_id == anchor_artifact_id else "reference"),
                    asset_guard=asset_guard,
                )
                artifact.asset = asset
            artifact.status = GeneratedArtifact.Status.SELECTED
            artifact.save(update_fields=("asset", "status", "updated_at"))
            selection_group = f"bible-candidate:{artifact.id}"
            revision = (
                ArtifactSelectionDecision.objects.filter(
                    stage_run=review_stage,
                    variant_group=selection_group,
                ).aggregate(value=Max("revision"))["value"] or 0
            ) + 1
            ArtifactSelectionDecision.objects.create(
                stage_run=review_stage,
                variant_group=selection_group,
                revision=revision,
                artifact=artifact,
                selected_by=user if selection_mode == "manual" else None,
                selection_mode=selection_mode,
            )
            created_assets[artifact_id] = asset
        generated_asset_ids = {str(asset.id) for asset in created_assets.values()}
        if generated_asset_ids.intersection(selected_library_assets):
            raise ValidationError(
                "同一参考图不能同时作为模型候选和素材库图片重复提交",
            )
        old_anchor_id = str(entity.anchor_asset_id) if entity.anchor_asset_id else ""
        new_anchor = (
            created_assets[anchor_artifact_id]
            if anchor_artifact_id
            else selected_library_assets[anchor_library_asset_id]
        )
        selected_resolved_assets = [
            *[created_assets[item] for item in selected_ids],
            *[selected_library_assets[item] for item in selected_library_ids],
        ]
        selected_resolved_ids = {str(item.id) for item in selected_resolved_assets}
        if claimed_final_asset_ids.intersection(selected_resolved_ids):
            raise ValidationError(
                "同一最终 Asset 不能绑定到多个 Bible 实体",
            )
        claimed_final_asset_ids.update(selected_resolved_ids)
        reference_ids = {
            str(item) for item in preserved_assets
            if str(item) != old_anchor_id
        }
        if (
            old_anchor_id
            and old_anchor_id in preserved_assets
            and old_anchor_id != str(new_anchor.id)
        ):
            reference_ids.add(old_anchor_id)
        reference_ids.update(
            str(asset.id)
            for asset in selected_resolved_assets
            if str(asset.id) != str(new_anchor.id)
        )
        reference_ids.discard(str(new_anchor.id))
        entity.anchor_asset = new_anchor
        entity.save(update_fields=("anchor_asset", "updated_at"))
        binding_assets = {
            **preserved_assets,
            **{str(asset.id): asset for asset in created_assets.values()},
            **selected_library_assets,
        }
        entity.reference_assets.set([
            binding_assets[item]
            for item in sorted(reference_ids)
        ])
        actual_reference_ids = sorted(
            str(item) for item in entity.reference_assets.values_list("id", flat=True)
        )
        if actual_reference_ids != sorted(reference_ids):
            raise ValidationError("Bible reference_assets 提交事实与审核结果不一致")
        actual_reference_assets = list(
            entity.reference_assets.all().order_by("id")
        )
        result_rows.append({
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_name": entity.name,
            "selected_artifact_ids": selected_ids,
            "selected_library_asset_ids": selected_library_ids,
            "selected_library_assets": [
                {
                    "asset_id": str(item["asset_id"]),
                    "expected_sha256": str(item["expected_sha256"]),
                }
                for item in selected_library_rows
            ],
            "anchor_artifact_id": anchor_artifact_id or None,
            "anchor_library_asset_id": anchor_library_asset_id or None,
            "selected_asset_ids": [str(item.id) for item in selected_resolved_assets],
            "anchor_asset_id": str(new_anchor.id),
            "selection_sources": [
                *[
                    {
                        "source_type": "generated_artifact",
                        "source_id": artifact_id,
                        "asset_id": str(created_assets[artifact_id].id),
                    }
                    for artifact_id in selected_ids
                ],
                *[
                    {
                        "source_type": "generated_artifact" if material_facts.get(asset_id, {}).get("material_generations") else "asset_library",
                        "source_id": (material_facts[asset_id]["material_generations"][0]["artifact_id"]
                            if material_facts.get(asset_id, {}).get("material_generations") else asset_id),
                        "asset_id": asset_id,
                    }
                    for asset_id in selected_library_ids
                ],
            ],
            "reference_asset_ids": actual_reference_ids,
            "removed_stale_asset_ids": removed_stale_asset_ids,
            "anchor_asset": _bible_binding_asset_fact(
                new_anchor,
                project=run.project,
                entity_type=entity_type,
                entity=entity,
                material_facts=material_facts,
            ),
            "reference_assets": [
                _bible_binding_asset_fact(
                    asset,
                    project=run.project,
                    entity_type=entity_type,
                    entity=entity,
                    material_facts=material_facts,
                )
                for asset in actual_reference_assets
            ],
        })
    sync_consistency_locks(run.project)
    return result_rows


def _approve_visual_rules(run, review_stage, *, candidate_overrides,
        expected_source_snapshot_sha256, text_candidate, user):
    from studio.services.execution_production_candidates import reviewed_text_candidate

    source, artifact, detail = reviewed_text_candidate(run=run, review_stage=review_stage,
        selection=text_candidate, expected_source_snapshot_sha256=expected_source_snapshot_sha256)
    evidence = approve_verified_visual_rules_artifact(run=run, artifact=artifact, detail=detail,
        reviewed_rules=candidate_overrides, user=user)
    evidence.update({"generation_snapshot_sha256": source.output_snapshot_sha256,
        "public_generation_snapshot_sha256": expected_source_snapshot_sha256})
    _save_stage_snapshot(review_stage, "output_snapshot", evidence)


def _verified_unattended_decision_policy(
    run: ProductionRun,
    stage: StageRun,
) -> tuple[str, str]:
    if not _is_unattended_run(run) or stage.mode != "review":
        raise StageBlocked(
            "unattended_policy_context_invalid",
            "decision_policy 只能在已显式确认的 unattended review stage 执行。",
        )
    policy = UNATTENDED_DECISION_POLICY_REGISTRY.get(stage.stage_key)
    from studio.services.production_recipe_preparations import recipe_stage_context
    _, declaration = recipe_stage_context(stage)
    if declaration.get("approval") != "policy" or declaration.get("decision_policy") != stage.decision_policy:
        raise StageBlocked("unattended_policy_context_invalid", "本轮冻结计划未授权该阶段审核策略。")
    if not policy or stage.decision_policy != policy:
        raise StageBlocked(
            "unattended_policy_unregistered",
            f"阶段 {stage.stage_key} 没有匹配的已注册 decision_policy。",
        )
    policy_contract = {
        "schema_version": 1,
        "interaction_mode": UNATTENDED_INTERACTION_MODE,
        "stage_key": stage.stage_key,
        "decision_policy": policy,
        "recipe_definition_sha256": _frozen_recipe_definition_sha256(run),
    }
    policy_sha256 = _sha256(policy_contract)
    if stage.decision_policy_sha256 != policy_sha256:
        raise StageBlocked(
            "unattended_policy_snapshot_drift",
            f"阶段 {stage.stage_key} 的 decision_policy hash 已漂移。",
        )
    return policy, policy_sha256


def _unique_policy_text_candidate(stage):
    from studio.services.production_project_text_stages import text_review_source
    from studio.services.execution_production_candidates import read_production_text_candidate
    source = text_review_source(stage)
    candidates = list(GeneratedArtifact.objects.filter(stage_run=source,
        production_run_id=stage.production_run_id, status="available").order_by("id")[:2])
    if len(candidates) != 1:
        raise StageBlocked("unattended_text_candidate_count_invalid", "自动审核策略只接受唯一候选，不会替用户从多个结果中任选一个。")
    artifact = candidates[0]
    return ({"artifact_id": str(artifact.pk), "expected_sha256": artifact.sha256},
        source.output_snapshot_sha256, read_production_text_candidate(artifact))


@transaction.atomic
def _execute_unattended_policy_review(
    run: ProductionRun,
    stage: StageRun,
) -> str | None:
    policy, policy_sha256 = _verified_unattended_decision_policy(run, stage)
    policy_evidence: dict[str, Any] | None = None
    script_decision = None
    if stage.stage_key == "script_review":
        selection, source_sha256, _ = _unique_policy_text_candidate(stage)
        script_decision = _approve_generated_script(run, stage, run.owner,
            text_candidate=selection, expected_source_snapshot_sha256=source_sha256)
    elif stage.stage_key == "bible_review":
        from studio.services.bible_content_review import apply_policy_bible_content
        from studio.services.bible_variants import BibleVariantConflict
        try:
            policy_evidence = apply_policy_bible_content(run=run, stage=stage)
        except (ValidationError, BibleVariantConflict) as exc:
            raise StageBlocked("bible_content_policy_invalid", str(exc)) from exc
    elif stage.stage_key == BIBLE_IMAGE_REVIEW_STAGE_KEY:
        generation_stage = run.stages.select_for_update().filter(
            stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
            status=StageRun.Status.SUCCEEDED,
        ).first()
        if generation_stage is None:
            raise StageBlocked(
                "unattended_bible_image_generation_missing",
                "unattended Bible 图片策略缺少已成功的生成阶段。",
            )
        source_snapshot = _verify_stage_snapshot(
            generation_stage,
            "output_snapshot",
        )
        canonical = source_snapshot.get("canonical")
        if (
            source_snapshot.get("formal_object_type")
            != "bible_image_candidate_manifest"
            or not isinstance(canonical, dict)
            or _sha256(canonical) != source_snapshot.get("canonical_sha256")
        ):
            raise StageBlocked(
                "unattended_bible_image_snapshot_invalid",
                "unattended Bible 图片策略缺少有效的冻结候选清单。",
            )
        selections: list[dict[str, Any]] = []
        selected_artifact_ids: list[str] = []
        from studio.services.production_recipe_material_review import material_review_context
        try:
            fulfilled = material_review_context(run, canonical)
        except ValidationError as exc:
            raise StageBlocked("unattended_bible_material_invalid", str(exc)) from exc
        for entity in canonical.get("entities") or []:
            if not isinstance(entity, dict):
                raise StageBlocked(
                    "unattended_bible_image_snapshot_invalid",
                    "unattended Bible 图片候选实体结构无效。",
                )
            current_candidates = [
                item for item in (entity.get("candidates") or [])
                if isinstance(item, dict)
                and not item.get("historical")
                and str(item.get("origin_production_run_id") or "") == str(run.id)
                and str(item.get("origin_stage_run_id") or "")
                == str(generation_stage.id)
            ]
            material = fulfilled.get((str(entity.get("entity_type") or ""), str(entity.get("entity_id") or "")), {})
            primary_asset_id = material.get("primary_asset_id")
            library = [{"asset_id": asset_id, "expected_sha256": fact["checksum"]}
                for asset_id, fact in sorted(material.get("assets", {}).items())]
            registered_primary = {row["artifact_id"] for row in material.get("assets", {}).get(
                primary_asset_id, {}).get("material_generations", [])}
            if primary_asset_id is not None and current_candidates and not registered_primary:
                raise StageBlocked("unattended_bible_material_conflict", "本轮主图同时存在履约选择与生成候选，不能自动择一。")
            if registered_primary and not registered_primary <= {str(row.get("artifact_id")) for row in current_candidates}:
                raise StageBlocked("unattended_bible_material_invalid", "已选主图缺少本轮冻结生成候选。")
            if primary_asset_id is None and len(current_candidates) != 1:
                raise StageBlocked(
                    "unattended_bible_image_candidate_count_invalid",
                    "全自动 Bible 图片策略要求每个实体恰好一个本 Run 冻结候选。",
                )
            artifact_id = str(current_candidates[0].get("artifact_id") or "") if primary_asset_id is None else None
            if primary_asset_id is None and not artifact_id:
                raise StageBlocked(
                    "unattended_bible_image_candidate_invalid",
                    "全自动 Bible 图片候选缺少 GeneratedArtifact identity。",
                )
            if artifact_id:
                selected_artifact_ids.append(artifact_id)
            selections.append({
                "entity_type": str(entity.get("entity_type") or ""),
                "entity_id": str(entity.get("entity_id") or ""),
                "selected_artifact_ids": [artifact_id] if artifact_id else [],
                "selected_library_assets": library,
                "anchor_artifact_id": artifact_id,
                "anchor_library_asset_id": primary_asset_id,
            })
        if not selections:
            raise StageBlocked(
                "unattended_bible_image_candidates_missing",
                "全自动 Bible 图片策略没有可绑定的实体候选。",
            )
        expected_source_snapshot_sha256 = json_contract(
            redact_sensitive_bindings(source_snapshot),
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        try:
            with _StoredAssetFileGuard() as asset_guard:
                with transaction.atomic():
                    _approve_bible_image_bindings(
                        run,
                        stage,
                        run.project.owner,
                        bindings=selections,
                        expected_source_snapshot_sha256=(
                            expected_source_snapshot_sha256
                        ),
                        asset_guard=asset_guard,
                        selection_mode="automatic",
                    )
                    _assert_stage_output_integrity(run, stage)
        except (ValidationError, ProductionRunConflict) as exc:
            raise StageBlocked(
                "unattended_bible_image_binding_failed",
                "全自动 Bible 图片所选结果未通过来源、字节或实体绑定校验。",
            ) from exc
        policy_evidence = {
            "schema_version": 1,
            "kind": "unattended_bible_image_policy",
            "source_snapshot_sha256": expected_source_snapshot_sha256,
            "selected_artifact_ids": sorted(selected_artifact_ids),
        }
        if canonical.get("material_fulfillments"):
            policy_evidence["material_fulfillment_ids"] = [row["fulfillment_id"]
                for row in canonical["material_fulfillments"]]
        if canonical.get("material_generations"):
            policy_evidence["material_generation_decision_ids"] = [row["decision_id"]
                for row in canonical["material_generations"]]
    elif stage.stage_key == "visual_rules_review":
        selection, source_sha256, detail = _unique_policy_text_candidate(stage)
        _approve_visual_rules(run, stage, candidate_overrides=detail["candidate"],
            expected_source_snapshot_sha256=source_sha256, text_candidate=selection, user=run.owner)
        policy_evidence = {"schema_version": 1, "kind": "unattended_visual_rules_policy",
            "source_snapshot_sha256": source_sha256, "artifact_id": selection["artifact_id"],
            "artifact_sha256": selection["expected_sha256"], "result_sha256": detail["result_sha256"]}
    elif stage.stage_key == "storyboard_review":
        from studio.services.production_storyboard_policy import approve_recipe_storyboard_references
        try:
            policy_evidence = approve_recipe_storyboard_references(run)
            if policy_evidence is None:
                # Keep the original stage pending selection, not falsely
                # approved or failed. A later command must supply exact uses.
                stage.refresh_from_db(fields=("reason_code", "reason"))
                return None
        except (ValidationError, ProductionRunConflict) as exc:
            raise StageBlocked("unattended_storyboard_reference_invalid",
                "自动分镜参考审核未通过当前来源、逐镜用途或精确执行实例输入合同校验。") from exc
    elif stage.stage_key == "artifact_selection":
        from studio.services.execution_production_candidates import read_production_media_candidate
        from studio.services.generated_artifacts import _artifact_limit
        media_stage = run.stages.select_for_update().filter(
            stage_key="media_generation",
            status=StageRun.Status.SUCCEEDED,
        ).first()
        if media_stage is None:
            raise StageBlocked(
                "unattended_media_missing",
                "unattended 单产物策略缺少已成功的 media_generation。",
            )
        artifacts = GeneratedArtifact.objects.select_for_update().filter(
            production_run=run,
            stage_run=media_stage,
            kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        )
        groups = list(artifacts.values_list("variant_group", flat=True).distinct())
        if not groups:
            raise StageBlocked(
                "unattended_artifact_missing",
                "unattended 单产物策略没有可审核的媒体产物。",
            )
        selected_candidates = []
        for group in groups:
            candidates = list(artifacts.filter(variant_group=group))
            if (
                len(candidates) != 1
                or candidates[0].status not in {GeneratedArtifact.Status.AVAILABLE, GeneratedArtifact.Status.SELECTED}
            ):
                raise StageBlocked(
                    "unattended_multiple_artifacts",
                    f"产物组 {group} 不是唯一且可采用的正式产物，策略拒绝自动决定。",
                )
            read_production_media_candidate(candidates[0], max_media_bytes=_artifact_limit(run))
            selected_candidates.append(candidates[0])
        # The frozen policy authorizes this decision, not the generation
        # worker. Validate every group before writing any original selection;
        # the surrounding transaction rolls back selections if review fails.
        selected = []
        for candidate in selected_candidates:
            candidate = select_generated_artifact(candidate, user=run.owner, selection_mode="automatic")
            decision = media_stage.artifact_selection_decisions.filter(artifact=candidate).order_by("-revision").first()
            selected.append({"artifact_id": str(candidate.pk), "sha256": candidate.sha256, "decision_id": str(decision.pk)})
        policy_evidence = {"schema_version": 1, "kind": "unattended_artifact_policy",
            "source_snapshot_sha256": media_stage.output_snapshot_sha256, "selected_artifacts": selected}
    else:
        raise StageBlocked(
            "unattended_policy_unregistered",
            f"阶段 {stage.stage_key} 没有 unattended policy executor。",
        )
    _assert_stage_output_integrity(run, stage, script_review_decision=script_decision)
    formal_snapshot = _formal_snapshot(run, stage, f"recipe-policy:{policy}", review_payload=script_decision)
    if policy_evidence is not None:
        formal_snapshot["policy_evidence"] = policy_evidence
    output_snapshot_sha256 = _save_stage_snapshot(
        stage,
        "output_snapshot",
        formal_snapshot,
    )
    revision = (
        stage.approval_decisions.aggregate(value=Max("revision"))["value"]
        or 0
    ) + 1
    approval = ApprovalDecision.objects.create(
        stage_run=stage,
        revision=revision,
        decision=ApprovalDecision.Decision.APPROVE,
        comment=f"由已冻结配方策略 {policy} 自动决定。",
        output_snapshot_sha256=output_snapshot_sha256,
        actor=ApprovalDecision.Actor.RECIPE_POLICY,
        decision_policy=policy,
        decision_policy_sha256=policy_sha256,
        decided_by=None,
    )
    return f"approval:{approval.id}"


def _execute_stage(run: ProductionRun, stage: StageRun, *, human_initiated=False) -> str | None:
    if stage.mode == "skip":
        return "skipped"
    if stage.mode == "manual":
        raise StageBlocked(
            "manual_stage_confirmation_required",
            "该阶段配置为人工处理；完成页面操作后审批继续。",
            awaiting_approval=True,
        )
    from studio.services.production_project_text_stages import is_project_text_stage, advance_recipe_project_text_stage
    if is_project_text_stage(stage):
        return advance_recipe_project_text_stage(run, stage, human_initiated=human_initiated)
    if stage.mode == "ai":
        raise StageBlocked(
            "driver_contract_missing",
            (
                f"阶段 {stage.stage_key} 的正式文本结果 schema/对象物化适配器"
                "尚未注册；不会调用模型或伪造完成。"
            ),
            awaiting_approval=True,
        )
    if stage.mode == "review" and stage.decision_policy:
        return _execute_unattended_policy_review(run, stage)
    if stage.approval_required or stage.mode == "review":
        if stage.stage_key == "storyboard_review" and stage.mode == "review":
            from studio.services.production_storyboard_policy import prepare_required_storyboard_reference_choices
            code, reason = prepare_required_storyboard_reference_choices(run, stage)
            raise StageBlocked(code, reason, awaiting_approval=True)
        raise StageBlocked(
            "stage_approval_required",
            f"阶段 {stage.stage_key} 需要人工审批。",
            awaiting_approval=True,
        )
    if stage.stage_key == "script_input":
        return f"generated-artifact:{_script_artifact(run, stage, create=True).id}"
    if stage.stage_key == "bible_materialize" and stage.mode == "deterministic":
        return _execute_bible_materialize(run, stage)
    if stage.stage_key == "storyboard" and stage.mode == "deterministic":
        return _execute_storyboard(run, stage)
    if stage.stage_key == "media_generation" and stage.mode == "existing_pipeline":
        return _poll_media_stage(run, stage)
    if (
        stage.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY
        and stage.mode == "existing_pipeline"
    ):
        return _poll_media_stage(run, stage)
    if stage.stage_key == "assembly" and stage.mode == "existing_pipeline":
        return _execute_assembly(run, stage)
    if stage.stage_key == "delivery" and stage.mode == "existing_pipeline":
        return _execute_delivery(run, stage)
    raise StageBlocked(
        "stage_executor_unavailable",
        f"阶段 {stage.stage_key}/{stage.mode} 尚未接入受控执行器。",
    )


def _assert_stage_output_integrity(run: ProductionRun, stage: StageRun, *, script_review_decision=None) -> None:
    if stage.reason_code in {
        "routing_not_configured", "route_selection_required", "unknown_cost",
        "external_call_not_authorized", "driver_contract_missing",
    }:
        raise ValidationError("该门禁必须通过对应的 route/费用/外发操作解决，不能用审批跳过")
    if stage.stage_key == "script_input" and stage.mode == "manual":
        episodes = _selected_episodes(run)
        if not episodes:
            raise ValidationError("沿用现有分集前必须选择至少一个正式 Episode")
        return
    if stage.stage_key == "script_review":
        evidence = script_review_decision
        if evidence is None:
            snapshot = _verify_stage_snapshot(stage, "output_snapshot")
            if snapshot.get("formal_object_type") != "script" or not isinstance(snapshot.get("review_payload"), dict):
                raise ValidationError("剧本审核缺少已保存的正式审核快照")
            approval = stage.approval_decisions.order_by("-revision").first()
            if (approval is None or approval.decision != "approve"
                    or approval.output_snapshot_sha256 != stage.output_snapshot_sha256):
                raise ValidationError("剧本审核缺少与正式快照一致的通过决定")
            evidence = snapshot["review_payload"]
        revision = run.script_revision
        if (evidence.get("kind") != "script_review_decision" or revision is None
                or revision.status != ScriptRevision.Status.CONFIRMED
                or evidence.get("script_revision_id") != str(revision.pk)
                or evidence.get("script_content_sha256") != revision.content_sha256
                or not GeneratedArtifact.objects.filter(pk=evidence.get("artifact_id"), production_run=run,
                    status="selected", sha256=evidence.get("artifact_sha256"), selection_decisions__isnull=False).exists()):
            raise ValidationError("剧本审核缺少本次明确采用的候选、选择记录或已确认修订")
        from studio.services.script_revision_structures import read_confirmed_script_structure
        from studio.services.execution_instances import execution_configuration_max_bytes
        source = evidence.get("structure_source")
        if not isinstance(source, dict):
            raise ValidationError("剧本审核缺少本次采用的确切结构化依据")
        read_confirmed_script_structure(project_id=run.project_id, reference=source.get("script"),
            expected_source=source, max_bytes=execution_configuration_max_bytes())
        if source["script"]["id"] != str(revision.pk):
            raise ValidationError("结构化依据不是本轮采用的剧本修订")
        return
    if stage.stage_key == "script_input":
        revision = run.script_revision
        if revision is None or revision.status != ScriptRevision.Status.CONFIRMED:
            raise ValidationError("剧本审批前必须存在已确认的正式 ScriptRevision")
        _script_artifact(run, stage)
        return
    if stage.stage_key in {"script_generate", "script_structure", "visual_rules_generate"} and stage.mode == "ai":
        raise ValidationError("生成阶段只提供候选目录；请在对应审核阶段明确采用，不在生成阶段直接批准")
    if stage.stage_key == "visual_rules_review":
        existing = stage.output_snapshot if isinstance(stage.output_snapshot, dict) else {}
        if existing.get("kind") != "visual_rules_review_decision":
            raise ValidationError("全剧影视规则审核尚未原子写入正式项目字段")
        canonical = _canonical_visual_rules(run)
        if canonical["rules_sha256"] != existing.get("after_rules_sha256"):
            raise ValidationError("全剧影视规则审核结果与当前项目正式字段不一致")
        return
    if stage.stage_key in {"bible", "bible_generation", "bible_review"}:
        invalid = []
        for label, model in (
            ("CharacterBible", CharacterBible),
            ("SceneBible", SceneBible),
            ("PropBible", PropBible),
        ):
            if model.objects.filter(project=run.project, name="").exists():
                invalid.append(label)
        if invalid:
            raise ValidationError(f"Bible 审批前存在未命名实体: {', '.join(invalid)}")
        # Materialized candidates were atomically verified/applied above;
        # manual review explicitly accepts current project facts. Empty
        # collections are valid, not permission to manufacture extra entities.
        return
    if stage.stage_key in {"storyboard", "storyboard_review"}:
        episodes = _selected_episodes(run)
        if not episodes:
            raise ValidationError("分镜审批前必须存在正式 Episode")
        missing_shots = [str(item.id) for item in episodes if not item.shots.exists()]
        if missing_shots:
            raise ValidationError(f"以下 Episode 没有正式 Shot: {', '.join(missing_shots)}")
        return
    if stage.stage_key in {"media_generation", "media_review"}:
        artifacts = GeneratedArtifact.objects.filter(
            production_run=run,
            stage_run__stage_key="media_generation",
            kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        )
        if not artifacts.exists():
            raise ValidationError("媒体审批前没有正式 GeneratedArtifact")
        return
    if stage.stage_key in {BIBLE_IMAGE_GENERATION_STAGE_KEY, BIBLE_IMAGE_REVIEW_STAGE_KEY}:
        expected_bible_targets = _bible_image_target_keys(run, stage)
    if stage.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY:
        candidates = _canonical_bible_image_candidates(run, stage)
        if run.plan_source == "recipe":
            from studio.services.production_recipe_material_review import require_complete_recipe_material_results
            require_complete_recipe_material_results(stage, candidates)
            return
        actual = {
            (str(item.get("entity_type") or ""), str(item.get("entity_id") or ""))
            for item in candidates.get("entities", [])
            if isinstance(item, dict) and item.get("candidates")
        }
        if actual != expected_bible_targets:
            raise ValidationError("Bible 参考图候选未完整覆盖当前实体")
        return
    if stage.stage_key == BIBLE_IMAGE_REVIEW_STAGE_KEY:
        snapshot = stage.output_snapshot if isinstance(stage.output_snapshot, dict) else {}
        if snapshot.get("kind") != "bible_image_bindings":
            raise ValidationError("Bible 参考图审核尚未物化绑定结果")
        bindings = snapshot.get("bindings")
        if (not isinstance(bindings, list) or len(bindings) != len(expected_bible_targets)
                or {(item.get("entity_type"), item.get("entity_id")) for item in bindings if isinstance(item, dict)} != expected_bible_targets):
            raise ValidationError("Bible 参考图审核绑定覆盖不完整")
        for item in bindings:
            if (
                not isinstance(item, dict)
                or not item.get("anchor_asset_id")
                or not isinstance(item.get("reference_asset_ids"), list)
                or not item.get("selected_asset_ids")
                or not (
                    item.get("selected_artifact_ids")
                    or item.get("selected_library_asset_ids")
                )
            ):
                raise ValidationError("Bible 参考图审核绑定结果无效")
        return
    if stage.stage_key == "artifact_selection":
        media_stage = run.stages.filter(stage_key="media_generation").first()
        if media_stage is None or media_stage.status != StageRun.Status.SUCCEEDED:
            raise ValidationError("选片前 media_generation 尚未成功")
        artifacts = GeneratedArtifact.objects.filter(
            production_run=run,
            stage_run=media_stage,
            kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        )
        if not artifacts.exists():
            raise ValidationError("选片前没有正式 GeneratedArtifact")
        groups = list(artifacts.values_list("variant_group", flat=True).distinct())
        for group in groups:
            if not artifacts.filter(
                variant_group=group,
                status=GeneratedArtifact.Status.SELECTED,
            ).exists():
                raise ValidationError(f"产物组 {group} 尚未选择正式版本")
        from studio.services.production_recipe_preparations import recipe_stage_context
        from studio.services.production_recipe_routing import recipe_routing
        _, declaration = recipe_stage_context(media_stage)
        routing = recipe_routing(declaration)
        if routing["variant_count"] > 1 and not routing.get("evaluator"):
            decided_groups = set(media_stage.artifact_selection_decisions.values_list(
                "variant_group", flat=True,
            ))
            if set(groups) - decided_groups:
                raise ValidationError("多版本产物未配置 evaluator，每个产物组都必须执行显式选片")
        return
    if stage.stage_key == "assembly":
        episodes = _selected_episodes(run)
        masters = GeneratedArtifact.objects.filter(
            production_run=run,
            kind=GeneratedArtifact.Kind.ASSEMBLY,
            role="episode_master",
            status=GeneratedArtifact.Status.SELECTED,
        )
        if masters.count() != len(episodes):
            raise ValidationError("逐集母版 artifact 清单不完整")
        return
    if stage.stage_key in {"delivery", "release"}:
        bundle = ReleaseBundle.objects.filter(production_run=run).first()
        if bundle is None or bundle.status != ReleaseBundle.Status.SEALED:
            raise ValidationError("交付审批前必须存在已封存 ReleaseBundle")
        return
    if stage.mode in {"manual", "review"}:
        raise ValidationError(f"阶段 {stage.stage_key} 尚未注册正式对象完整性断言")


def _assert_delivery_complete(run: ProductionRun) -> ReleaseBundle:
    bundle = ReleaseBundle.objects.filter(
        production_run=run,
        status=ReleaseBundle.Status.SEALED,
    ).first()
    if bundle is None:
        raise StageBlocked("delivery_manifest_missing", "运行没有已封存的 ReleaseBundle")
    try:
        document = live_verified_release_bundle(bundle)
    except ReleaseBundleError as exc:
        raise StageBlocked("delivery_manifest_invalid", str(exc)) from exc
    episodes = _selected_episodes(run)
    links = list(ProductionAssemblyLink.objects.filter(
        production_run=run,
    ).select_related("assembly_job"))
    if len(links) != len(episodes) or any(
        link.assembly_job.status != "succeeded"
        for link in links
    ):
        raise StageBlocked("episode_master_incomplete", "逐集母版任务未全部成功")
    masters = GeneratedArtifact.objects.filter(
        production_run=run,
        kind=GeneratedArtifact.Kind.ASSEMBLY,
        role="episode_master",
        status=GeneratedArtifact.Status.SELECTED,
    )
    if masters.count() != len(episodes):
        raise StageBlocked("episode_master_manifest_incomplete", "逐集母版 artifact 清单不完整")
    selected_episode_ids = {
        str(item.get("episode_id") or "")
        for item in (document.get("selection", {}).get("episodes") or [])
        if isinstance(item, dict)
    }
    if selected_episode_ids != {str(item.id) for item in episodes}:
        raise StageBlocked("release_episode_manifest_incomplete", "ReleaseBundle 未覆盖全部 Episode")
    return bundle




@registry_reference_writer
@transaction.atomic
@security_boundary_resolution_cache()
def advance_production_run(run: ProductionRun, *, automatic: bool = False, human_initiated: bool = False) -> ProductionRun:
    run = ProductionRun.objects.select_for_update().select_related(
        "project", "owner", "script_revision__source", "recipe_version",
    ).get(pk=run.pk)
    # Recheck under the same registry/run locks as every explicit advance.
    # Background wakes must not resume manual work, review gates or failures.
    if automatic and (run.plan_source != "recipe" or run.status not in {
        ProductionRun.Status.QUEUED, ProductionRun.Status.RUNNING,
    }):
        return run
    if run.status in TERMINAL_RUN_STATUSES:
        return run
    if (run.plan_source == "recipe" and run.status == ProductionRun.Status.AWAITING_INPUT
            and run.blocked_code == "waiting_shot_reference_selection"
            and (automatic or not human_initiated)):
        # Saving a use/receiving a result is not consent to resume this paused
        # review. Only the existing explicit Continue action rechecks it.
        return run
    if run.plan_source == "manual":
        from studio.services.production_run_plans import get_run_execution_plan
        from studio.services.production_manual_assembly import refresh_manual_assembly
        get_run_execution_plan(run)
        # Manual action adapters own only explicitly authorized work. Merely
        # pressing Continue must never enter the recipe scheduler or skip all
        # unperformed stages and report a successful Run.
        return refresh_manual_assembly(run)
    _verify_run_snapshots(run)
    # Only an explicit continuation of this paused stage may recheck its
    # reviewed preparations. Ordinary recipe scans must not spend on consent.
    resume_review_stage = run.current_stage_key if human_initiated and not automatic and run.status == "awaiting_input" and run.blocked_code == "execution_preparation_review_required" else None
    stages = list(run.stages.select_for_update().order_by("ordinal"))
    if not stages:
        run.status = ProductionRun.Status.BLOCKED
        run.blocked_code = "recipe_stages_missing"
        run.blocked_reason = "ProductionRun 没有持久化 StageRun。"
        run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
        return run
    try:
        _assert_frozen_bible_run_closure(stages)
    except ProductionRunConflict as exc:
        run.status = ProductionRun.Status.BLOCKED
        run.blocked_code = exc.code
        run.blocked_reason = str(exc.messages[0] if exc.messages else exc)
        run.save(update_fields=(
            "status", "blocked_code", "blocked_reason", "updated_at",
        ))
        return run
    if run.started_at is None:
        run.started_at = timezone.now()
    for stage in stages:
        if stage.status == StageRun.Status.SUCCEEDED:
            continue
        run.current_stage_key = stage.stage_key
        if (human_initiated and not automatic and run.status == ProductionRun.Status.BLOCKED
                and stage.status == StageRun.Status.BLOCKED and stage.reason_code == "stage_validation_failed"):
            from studio.services.production_project_text_stages import is_project_text_stage
            from studio.services.production_recipe_media_stages import is_recipe_media_stage
            if is_project_text_stage(stage) or is_recipe_media_stage(stage):
                # Explicit Continue rechecks corrected inputs/contracts through
                # the same idempotent preparation path. It does not retry a
                # completed call, reset its fields, or reopen approval gates.
                stage.status, stage.reason_code, stage.reason = StageRun.Status.RUNNING, "", ""
                stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
        if stage.status in {StageRun.Status.AWAITING_APPROVAL, StageRun.Status.BLOCKED, StageRun.Status.FAILED}:
            if (stage.status == StageRun.Status.AWAITING_APPROVAL and stage.stage_key == "storyboard_review"
                    and stage.mode == "review" and not stage.decision_policy and human_initiated and not automatic):
                from studio.services.production_storyboard_policy import prepare_required_storyboard_reference_choices
                # Explicit Continue can apply the frozen selection rule to a
                # waiting review, but cannot approve it or enter media work.
                stage.reason_code, stage.reason = prepare_required_storyboard_reference_choices(run, stage)
                stage.save(update_fields=("reason_code", "reason", "updated_at"))
            run.status = (
                ProductionRun.Status.AWAITING_APPROVAL
                if stage.status == StageRun.Status.AWAITING_APPROVAL
                else ProductionRun.Status.BLOCKED
                if stage.status == StageRun.Status.BLOCKED
                else ProductionRun.Status.FAILED
            )
            run.blocked_code = stage.reason_code
            run.blocked_reason = stage.reason
            break
        if stage.status == StageRun.Status.CANCELLED:
            run.status = ProductionRun.Status.CANCELLED
            run.finished_at = timezone.now()
            break
        if stage.status == StageRun.Status.PENDING:
            stage.status = StageRun.Status.RUNNING
            from studio.services.production_project_text_stages import is_project_text_stage
            from studio.services.production_recipe_media_stages import is_recipe_media_stage
            if not (is_project_text_stage(stage) or is_recipe_media_stage(stage)):
                stage.attempt_count += 1
            stage.started_at = stage.started_at or timezone.now()
            stage.reason_code = ""
            stage.reason = ""
            stage.save(update_fields=(
                "status", "attempt_count", "started_at", "reason_code", "reason", "updated_at",
            ))
        run.status = ProductionRun.Status.RUNNING
        run.blocked_code = ""
        run.blocked_reason = ""
        run.save(update_fields=(
            "status", "current_stage_key", "blocked_code", "blocked_reason",
            "started_at", "updated_at",
        ))
        try:
            if stage.stage_key in {
                BIBLE_IMAGE_GENERATION_STAGE_KEY, BIBLE_IMAGE_REVIEW_STAGE_KEY,
                "visual_rules_generate", "visual_rules_review", "storyboard",
                "media_generation", "artifact_selection",
                "assembly", "delivery", "release",
            }:
                _assert_frozen_formal_objects(run)
            _freeze_stage_config(run, stage)
            _freeze_stage_input(run, stage)
            output_reference = _execute_stage(run, stage, human_initiated=stage.stage_key == resume_review_stage)
        except StageBlocked as exc:
            policy_fail_closed = _is_unattended_run(run)
            stage.status = (
                StageRun.Status.AWAITING_APPROVAL
                if exc.awaiting_approval and not policy_fail_closed
                else StageRun.Status.BLOCKED
            )
            stage.reason_code = exc.code
            stage.reason = exc.reason
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.status = (
                ProductionRun.Status.AWAITING_APPROVAL
                if exc.awaiting_approval and not policy_fail_closed
                else ProductionRun.Status.BLOCKED
            )
            from studio.services.production_execution_recovery import MANUAL_RECOVERY
            if exc.code in MANUAL_RECOVERY:
                run.status = ProductionRun.Status.AWAITING_INPUT
            run.blocked_code = exc.code
            run.blocked_reason = exc.reason
            break
        except ValidationError as exc:
            stage.status = StageRun.Status.BLOCKED
            stage.reason_code = "stage_validation_failed"
            stage.reason = str(exc)[:2000]
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.status = ProductionRun.Status.BLOCKED
            run.blocked_code = stage.reason_code
            run.blocked_reason = stage.reason
            break
        except Exception as exc:
            logger.exception("ProductionRun stage failed", extra={
                "production_run_id": str(run.id),
                "stage_key": stage.stage_key,
            })
            stage.status = StageRun.Status.FAILED
            stage.reason_code = "stage_execution_failed"
            stage.reason = "阶段执行发生未分类错误；请查看服务器日志。"
            stage.finished_at = timezone.now()
            stage.save(update_fields=(
                "status", "reason_code", "reason", "finished_at", "updated_at",
            ))
            run.status = ProductionRun.Status.FAILED
            run.blocked_code = stage.reason_code
            run.blocked_reason = stage.reason
            run.finished_at = timezone.now()
            break
        if output_reference is None:
            stage.status = StageRun.Status.RUNNING
            from studio.services.production_project_text_stages import is_project_text_stage
            from studio.services.production_recipe_media_stages import is_recipe_media_stage
            # A policy review can await explicit selection without an external
            # task. Preserve that reason instead of manufacturing a task wait.
            if not (is_project_text_stage(stage) or is_recipe_media_stage(stage) or (
                    stage.mode == "review" and stage.decision_policy
                    and stage.reason_code == "waiting_shot_reference_selection")):
                stage.reason_code = "waiting_external_task"
                stage.reason = "已创建持久化任务，后台等待结果并自动推进；如需人工处理会明确暂停。"
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.status = (ProductionRun.Status.AWAITING_INPUT if stage.reason_code in {
                "execution_output_review_required", "execution_preparation_review_required",
                "execution_candidate_selection_required",
                "waiting_material_recovery",
                "waiting_shot_reference_selection",
            } else ProductionRun.Status.RUNNING)
            run.blocked_code = stage.reason_code
            run.blocked_reason = stage.reason
            break
        if not (stage.mode == "review" and stage.decision_policy):
            _save_stage_snapshot(
                stage,
                "output_snapshot",
                _formal_snapshot(run, stage, str(output_reference)),
            )
        stage.status = StageRun.Status.SUCCEEDED
        stage.output_reference = str(output_reference)[:255]
        stage.reason_code = ""
        stage.reason = ""
        stage.finished_at = timezone.now()
        stage.save(update_fields=(
            "status", "output_reference", "reason_code", "reason", "finished_at", "updated_at",
        ))
    else:
        try:
            _assert_delivery_complete(run)
        except StageBlocked as exc:
            run.status = ProductionRun.Status.BLOCKED
            run.blocked_code = exc.code
            run.blocked_reason = exc.reason
        else:
            run.status = ProductionRun.Status.SUCCEEDED
            run.current_stage_key = ""
            run.blocked_code = ""
            run.blocked_reason = ""
            run.finished_at = timezone.now()
    run.save(update_fields=(
        "status", "current_stage_key", "blocked_code", "blocked_reason",
        "started_at", "finished_at", "updated_at",
    ))
    # Preparation dispatch settles reservations on locked, freshly read Run
    # rows. Return those ledger facts, not this scheduler object's old totals.
    run.refresh_from_db(fields=("estimated_cost_total", "committed_cost_total", "actual_cost_total", "cost_currency"))
    return run


@registry_reference_writer
@security_boundary_resolution_cache()
def decide_stage(
    run: ProductionRun,
    *,
    stage_key: str,
    decision: str,
    comment: str,
    user,
    candidate_overrides: dict[str, str] | None = None,
    expected_source_snapshot_sha256: str = "",
    text_candidate: dict[str, Any] | None = None,
    script_content: str | None = None,
    bible_image_bindings: list[dict[str, Any]] | None = None,
    expected_selection_sha256: str = "",
    bible_review: dict[str, Any] | None = None,
) -> ProductionRun:
    _require_recipe_control(run)
    # The file guard encloses the database commit itself.  A late constraint or
    # storage-backed transaction failure therefore removes every newly copied
    # Asset file instead of leaving owner directories out of sync with DB facts.
    try:
        with _StoredAssetFileGuard() as asset_guard:
            with transaction.atomic():
                return _decide_stage_locked(
                    run,
                    stage_key=stage_key,
                    decision=decision,
                    comment=comment,
                    user=user,
                    candidate_overrides=candidate_overrides,
                    expected_source_snapshot_sha256=expected_source_snapshot_sha256,
                    text_candidate=text_candidate,
                    script_content=script_content,
                    bible_image_bindings=bible_image_bindings,
                    expected_selection_sha256=expected_selection_sha256,
                    bible_review=bible_review,
                    asset_guard=asset_guard,
                )
    except StageBlocked as exc:
        raise ProductionRunConflict(
            {"detail": exc.reason},
            code=exc.code,
        ) from exc


def _decide_stage_locked(
    run: ProductionRun,
    *,
    stage_key: str,
    decision: str,
    comment: str,
    user,
    candidate_overrides: dict[str, str] | None,
    expected_source_snapshot_sha256: str,
    text_candidate: dict[str, Any] | None,
    script_content: str | None,
    bible_image_bindings: list[dict[str, Any]] | None,
    expected_selection_sha256: str,
    bible_review: dict[str, Any] | None,
    asset_guard: _StoredAssetFileGuard,
) -> ProductionRun:
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    _assert_frozen_bible_run_closure(
        list(run.stages.select_for_update().order_by("ordinal")),
    )
    stage = StageRun.objects.select_for_update().filter(
        production_run=run,
        stage_key=stage_key,
    ).first()
    if stage is None:
        raise ValidationError({"stage_key": "ProductionRun 不包含该阶段"})
    if _is_unattended_run(run):
        raise ValidationError(
            "unattended ProductionRun 的阶段审核由冻结策略执行；字段人工确认请使用对应执行准备。",
        )
    if stage.status != StageRun.Status.AWAITING_APPROVAL:
        raise ValidationError("该阶段当前不处于 awaiting_approval")
    if run.status != ProductionRun.Status.AWAITING_APPROVAL or run.current_stage_key != stage.stage_key:
        raise ValidationError("本轮已结束或审核阶段已变化，请重新读取当前运行")
    if decision not in ApprovalDecision.Decision.values:
        raise ValidationError("decision 必须为 approve/reject")
    comment = normalize_optional_audit_comment(comment)
    if script_content is not None and (stage.stage_key != "script_review" or decision != "approve"):
        raise ValidationError({"script_content": "编辑稿只允许在剧本审核批准时提交"})
    if (stage.stage_key == "bible_review" and decision == "approve") != (bible_review is not None):
        raise ValidationError("批准Bible必须提交完整核对集合；其他操作不接受Bible核对字段")
    if text_candidate is not None and (decision != "approve" or stage.stage_key not in {"script_review", "visual_rules_review"}):
        raise ValidationError("text_candidate 只允许用于文本候选审核采用")
    if expected_selection_sha256 and (stage.stage_key != "storyboard_review" or decision != "approve"):
        raise ValidationError("选择摘要仅用于批准分镜参考审核")
    if decision == ApprovalDecision.Decision.APPROVE:
        script_decision = None
        bible_decision = None
        storyboard_decision = None
        if stage.stage_key == "storyboard_review":
            from studio.services.production_storyboard_policy import assert_recipe_storyboard_reference_selection
            reviewed = assert_recipe_storyboard_reference_selection(run, actor=user,
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
                expected_selection_sha256=expected_selection_sha256)
            storyboard_decision = {"schema_version": 1, "kind": "human_storyboard_reference_review",
                **{key: reviewed[key] for key in ("source_snapshot_sha256", "selection_sha256", "shot_count")}}
        elif expected_source_snapshot_sha256 and stage.stage_key not in {
            "script_review", "visual_rules_review", BIBLE_IMAGE_REVIEW_STAGE_KEY,
        }:
            raise ValidationError(
                "expected_source_snapshot_sha256 只允许用于 script_review、storyboard_review、"
                "visual_rules_review 或 bible_image_review"
            )
        if stage.stage_key == "storyboard_review":
            if candidate_overrides or bible_image_bindings:
                raise ValidationError(
                    "storyboard_review 只确认已保存的确切选择及来源摘要，不在批准时改选",
                )
        elif stage.stage_key == "script_review":
            if candidate_overrides:
                raise ValidationError(
                    "只有 visual_rules_review 支持 candidate_overrides",
                )
            script_decision = _approve_generated_script(
                run,
                stage,
                user,
                text_candidate=text_candidate,
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
                script_content=script_content,
            )
        elif stage.stage_key == "visual_rules_review":
            if bible_image_bindings:
                raise ValidationError(
                    "visual_rules_review 只接受三项文本候选与来源快照 hash",
                )
            _approve_visual_rules(
                run,
                stage,
                candidate_overrides=candidate_overrides,
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
                text_candidate=text_candidate,
                user=user,
            )
        elif stage.stage_key == "bible_review":
            if candidate_overrides or bible_image_bindings:
                raise ValidationError("Bible内容审核不接受其他阶段的覆盖或素材选择")
            bible_decision = _approve_bible_content(run, user=user, confirmation=bible_review, comment=comment)
        elif stage.stage_key == BIBLE_IMAGE_REVIEW_STAGE_KEY:
            if candidate_overrides:
                raise ValidationError("Bible 参考图审核不接受文本候选覆盖")
            _approve_bible_image_bindings(
                run,
                stage,
                user,
                bindings=list(bible_image_bindings or []),
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
                asset_guard=asset_guard,
            )
        elif (
            candidate_overrides
            or bible_image_bindings
        ):
            raise ValidationError(
                "candidate_overrides 只允许用于 visual_rules_review；"
                "bible_image_bindings 只允许用于 bible_image_review",
            )
        if stage.stage_key == "script_input" and stage.mode == "confirmed_revision":
            _script_artifact(run, stage, create=True)
        _assert_stage_output_integrity(run, stage, script_review_decision=script_decision)
        output_snapshot_sha256 = _save_stage_snapshot(
            stage,
            "output_snapshot",
            _formal_snapshot(run, stage, "approval", review_payload=storyboard_decision if storyboard_decision is not None else bible_decision if bible_decision is not None else script_decision),
        )
    else:
        if candidate_overrides:
            raise ValidationError("拒绝阶段时不能提交 candidate_overrides")
        if expected_source_snapshot_sha256:
            raise ValidationError("拒绝阶段时不能提交 expected_source_snapshot_sha256")
        if bible_image_bindings:
            raise ValidationError("拒绝阶段时不能提交 bible_image_bindings")
        output_snapshot_sha256 = ""
    revision = (
        stage.approval_decisions.aggregate(value=Max("revision"))["value"]
        or 0
    ) + 1
    approval = ApprovalDecision.objects.create(
        stage_run=stage,
        revision=revision,
        decision=decision,
        comment=comment,
        actor=ApprovalDecision.Actor.HUMAN,
        decision_policy="",
        decision_policy_sha256="",
        decided_by=user,
        output_snapshot_sha256=output_snapshot_sha256,
    )
    stage.finished_at = timezone.now()
    if decision == ApprovalDecision.Decision.REJECT:
        if stage.stage_key == "script_review" and run.script_revision_id:
            GeneratedArtifact.objects.filter(
                production_run=run,
                generation_attempt_id=run.script_revision.origin_generation_attempt_id,
                kind=GeneratedArtifact.Kind.SCRIPT,
                status=GeneratedArtifact.Status.AVAILABLE,
            ).update(status=GeneratedArtifact.Status.REJECTED, updated_at=timezone.now())
        stage.status = StageRun.Status.FAILED
        stage.reason_code = "approval_rejected"
        stage.reason = comment or "阶段被拒绝"
        run.status = ProductionRun.Status.FAILED
        run.finished_at = stage.finished_at
        run.blocked_code = stage.reason_code
        run.blocked_reason = stage.reason
    else:
        stage.status = StageRun.Status.SUCCEEDED
        stage.output_reference = f"approval:{approval.id}"
        stage.reason_code = ""
        stage.reason = ""
        run.status = ProductionRun.Status.QUEUED
        run.blocked_code = ""
        run.blocked_reason = ""
    stage.save(update_fields=(
        "status", "output_reference", "reason_code", "reason", "finished_at", "updated_at",
    ))
    run.save(update_fields=(
        "status", "blocked_code", "blocked_reason", "finished_at", "updated_at",
    ))
    if decision == ApprovalDecision.Decision.REJECT:
        return run
    return advance_production_run(run)


@registry_reference_writer
@transaction.atomic
def resolve_uncertain_attempt(
    run: ProductionRun,
    *,
    attempt_id,
    outcome: str,
    note: str,
    user,
    provider_request_id: str = "",
    actual_cost: Decimal | None = None,
    cost_currency: str = "",
) -> ProductionRun:
    if not getattr(user, "is_staff", False):
        raise ValidationError("只有管理员可以核验不确定的远端提交")
    allowed = {
        GenerationAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED,
        GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED,
        GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_FAILED,
        GenerationAttemptResolution.Outcome.ACTUAL_COST_CONFIRMED,
    }
    if outcome not in allowed:
        raise ValidationError({"outcome": "不支持的 attempt 核验结果"})
    normalized_note = str(note or "").strip()
    if not normalized_note:
        raise ValidationError({"note": "必须填写远端控制台或账单核验证据说明"})
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    attempt = GenerationAttempt.objects.select_for_update().select_related("stage_run").filter(
        pk=attempt_id,
        stage_run__production_run=run,
    ).first()
    if attempt is None:
        raise ValidationError({"attempt_id": "该运行不存在此 attempt"})
    if attempt.operation not in (
        {
            TEXT_OPERATION, SCRIPT_STRUCTURE_OPERATION, VISUAL_RULES_OPERATION,
        }
        | ASYNC_MEDIA_OPERATIONS
    ):
        raise ValidationError({"attempt_id": "该 attempt transport 不支持人工核验"})
    is_cost_resolution = outcome == GenerationAttemptResolution.Outcome.ACTUAL_COST_CONFIRMED
    if run.status in TERMINAL_RUN_STATUSES and outcome in {
        GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED,
        GenerationAttemptResolution.Outcome.ACTUAL_COST_CONFIRMED,
    }:
        raise ProductionRunRetryConflict("终态 ProductionRun 不能恢复执行或确认新费用")
    if is_cost_resolution:
        if (
            attempt.operation not in ASYNC_MEDIA_OPERATIONS
            or attempt.status != GenerationAttempt.Status.FAILED
            or attempt.error_code != "actual_cost_reconciliation_required"
            or not attempt.artifacts.exists()
        ):
            raise ProductionRunRetryConflict("该 attempt 当前不处于实际费用待对账状态")
        if actual_cost is None or Decimal(str(actual_cost)) < 0:
            raise ValidationError({"actual_cost": "必须提供非负实际费用"})
        normalized_currency = str(cost_currency or attempt.cost_currency or "").strip().upper()
        if Decimal(str(actual_cost)) > 0 and not normalized_currency:
            raise ValidationError({"cost_currency": "非零实际费用必须提供币种"})
    elif (
        attempt.status != GenerationAttempt.Status.FAILED
        or attempt.error_code != "uncertain_remote_response"
    ):
        raise ProductionRunRetryConflict("该 attempt 当前不处于待核验的不确定提交状态")
    normalized_request_id = str(provider_request_id or "").strip()
    if len(normalized_request_id) > 191:
        raise ValidationError({"provider_request_id": "provider request id 过长"})
    if outcome == GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED:
        if attempt.operation not in ASYNC_MEDIA_OPERATIONS:
            raise ValidationError({"outcome": "只有 async media attempt 可恢复远端轮询"})
        if not normalized_request_id or len(normalized_request_id) > 191:
            raise ValidationError({"provider_request_id": "确认远端已提交时必须绑定 request_id"})
    if outcome == GenerationAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED:
        if normalized_request_id:
            raise ValidationError({"provider_request_id": "确认未提交时不能绑定 request_id"})
        if actual_cost is not None and Decimal(str(actual_cost)) != 0:
            raise ValidationError({"actual_cost": "确认未提交时实际费用必须为空或 0"})
    resolution_cost: Decimal | None = None
    resolution_currency = str(cost_currency or attempt.cost_currency or "").strip().upper()
    if outcome == GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_FAILED:
        if actual_cost is None:
            raise ValidationError({"actual_cost": "确认远端失败时必须核对实际费用（可填 0）"})
        try:
            resolution_cost = Decimal(str(actual_cost)).quantize(Decimal("0.000001"))
        except Exception as exc:
            raise ValidationError({"actual_cost": "实际费用必须是有限非负数"}) from exc
        if not resolution_cost.is_finite() or resolution_cost < 0:
            raise ValidationError({"actual_cost": "实际费用必须是有限非负数"})
        if resolution_cost > 0 and not resolution_currency:
            raise ValidationError({"cost_currency": "非零实际费用必须提供币种"})
        if run.cost_currency and resolution_currency and run.cost_currency != resolution_currency:
            raise ValidationError({"cost_currency": "实际费用币种与运行币种冲突"})
    provider_request_hash = (
        hashlib.sha256(
            (normalized_request_id or attempt.provider_request_id).encode("utf-8"),
        ).hexdigest()
        if (normalized_request_id or attempt.provider_request_id) else ""
    )
    evidence = {
        "schema_version": 1,
        "outcome": outcome,
        "attempt_id": str(attempt.id),
        "request_sha256": attempt.request_sha256,
        "request_snapshot_sha256": attempt.request_snapshot_sha256,
        "provider_request_id_sha256": provider_request_hash,
        "previous_error_code": attempt.error_code,
        "operator_note": normalized_note,
    }
    if is_cost_resolution:
        evidence.update({
            "actual_cost": str(Decimal(str(actual_cost)).quantize(Decimal("0.000001"))),
            "cost_currency": normalized_currency,
            "artifact_sha256": list(attempt.artifacts.order_by("manifest_key").values_list("sha256", flat=True)),
        })
    elif resolution_cost is not None:
        evidence.update({
            "actual_cost": str(resolution_cost),
            "cost_currency": resolution_currency,
        })
    if normalized_request_id:
        attempt.provider_request_id = normalized_request_id
    append_attempt_resolution(
        attempt,
        outcome=outcome,
        evidence=evidence,
        note=normalized_note,
        decided_by=user,
    )
    if outcome == GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED:
        attempt.status = GenerationAttempt.Status.SUBMITTED
        attempt.remote_status = "operator_confirmed_submitted"
        attempt.submitted_at = attempt.submitted_at or timezone.now()
        attempt.next_poll_at = timezone.now()
        attempt.finished_at = None
        attempt.error_code = ""
        attempt.error_message = ""
    elif is_cost_resolution:
        amount = Decimal(str(actual_cost)).quantize(Decimal("0.000001"))
        if run.cost_currency and normalized_currency and run.cost_currency != normalized_currency:
            raise ValidationError({"cost_currency": "实际费用币种与运行币种冲突"})
        if attempt.actual_cost is None:
            attempt.actual_cost = amount
            attempt.cost_currency = normalized_currency
            run.actual_cost_total += amount
            if normalized_currency and not run.cost_currency:
                run.cost_currency = normalized_currency
        attempt.status = GenerationAttempt.Status.SUCCEEDED
        attempt.finished_at = timezone.now()
        attempt.error_code = ""
        attempt.error_message = ""
        if attempt.stage_run.stage_key != BIBLE_IMAGE_GENERATION_STAGE_KEY:
            first = attempt.artifacts.order_by("variant_index", "created_at").first()
            if first and not GeneratedArtifact.objects.filter(
                production_run=run,
                variant_group=first.variant_group,
                status=GeneratedArtifact.Status.SELECTED,
            ).exists():
                first.status = GeneratedArtifact.Status.SELECTED
                first.save(update_fields=("status", "updated_at"))
    else:
        attempt.error_code = "remote_absence_confirmed"
        attempt.error_message = "管理员已核验远端未形成可用结果；允许显式创建新的 attempt。"
        if outcome == GenerationAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED:
            released = attempt.committed_cost or Decimal("0")
            run.committed_cost_total = max(Decimal("0"), run.committed_cost_total - released)
            attempt.committed_cost = Decimal("0")
        elif outcome == GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_FAILED:
            reserved = attempt.committed_cost or Decimal("0")
            run.committed_cost_total = max(
                Decimal("0"), run.committed_cost_total - reserved,
            )
            attempt.committed_cost = Decimal("0")
            attempt.actual_cost = resolution_cost
            attempt.cost_currency = resolution_currency
            run.actual_cost_total += resolution_cost or Decimal("0")
            if resolution_currency and not run.cost_currency:
                run.cost_currency = resolution_currency
    attempt.save(update_fields=(
        "status", "provider_request_id", "remote_status", "submitted_at", "next_poll_at",
        "finished_at", "error_code", "error_message", "committed_cost", "actual_cost",
        "cost_currency", "updated_at",
    ))
    run.save(update_fields=("committed_cost_total", "actual_cost_total", "cost_currency", "updated_at"))
    stage = attempt.stage_run
    if run.status not in TERMINAL_RUN_STATUSES:
        resumable = outcome in {
            GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED,
            GenerationAttemptResolution.Outcome.ACTUAL_COST_CONFIRMED,
        }
        stage.status = StageRun.Status.RUNNING if resumable else StageRun.Status.BLOCKED
        stage.reason_code = "" if resumable else attempt.error_code
        stage.reason = "" if resumable else attempt.error_message
        stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
        run.status = ProductionRun.Status.RUNNING if resumable else ProductionRun.Status.BLOCKED
        run.blocked_code = "" if resumable else attempt.error_code
        run.blocked_reason = "" if resumable else attempt.error_message
        run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
        if is_cost_resolution:
            return advance_production_run(run)
    return run




def _normalized_bible_regeneration_entities(
    entities: list[dict[str, Any]],
) -> list[dict[str, str]]:
    maximum_entities = _bible_regeneration_entity_max_items()
    if not isinstance(entities, list) or len(entities) > maximum_entities:
        raise ValidationError({
            "entities": f"单次最多重新生成 {maximum_entities} 个 Bible 实体",
        })
    normalized: list[dict[str, str]] = []
    for item in entities:
        entity_type = str(item.get("entity_type") or "").strip().lower()
        if entity_type not in {"character", "scene", "prop"}:
            raise ValidationError({"entities": "Bible 重新生成实体类型无效"})
        try:
            entity_id = str(uuid.UUID(str(item.get("entity_id") or "")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValidationError({"entities": "Bible 重新生成实体 ID 无效"}) from exc
        normalized.append({"entity_type": entity_type, "entity_id": entity_id})
    keys = [(item["entity_type"], item["entity_id"]) for item in normalized]
    if not keys or len(keys) != len(set(keys)):
        raise ValidationError({"entities": "必须提交至少一个且不能重复的 Bible 实体"})
    return sorted(normalized, key=lambda item: (item["entity_type"], item["entity_id"]))


def _bible_regeneration_request_contract(
    *,
    request_id: Any,
    expected_source_snapshot_sha256: str,
    entities: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    try:
        normalized_request_id = str(uuid.UUID(str(request_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError({"request_id": "重新生成 request_id 无效"}) from exc
    expected_sha256 = str(expected_source_snapshot_sha256 or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValidationError({
            "expected_source_snapshot_sha256": "页面候选快照 hash 无效",
        })
    payload = {
        "schema_version": 1,
        "request_id": normalized_request_id,
        "expected_source_snapshot_sha256": expected_sha256,
        "entities": _normalized_bible_regeneration_entities(entities),
    }
    return payload, _sha256(payload)


def _verify_bible_regeneration_source(
    run: ProductionRun,
    generation_stage: StageRun,
    *,
    expected_source_snapshot_sha256: str,
    entities: list[dict[str, str]],
) -> None:
    try:
        _assert_frozen_formal_objects(run)
    except StageBlocked as exc:
        raise ProductionRunConflict(
            {"detail": exc.reason},
            code=exc.code,
        ) from exc
    snapshot = _verify_stage_snapshot(generation_stage, "output_snapshot")
    public_sha256 = json_contract(
        redact_sensitive_bindings(snapshot),
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256
    if expected_source_snapshot_sha256 != public_sha256:
        raise ProductionRunConflict(
            {"detail": "页面展示的 Bible 参考图候选快照已过期，请刷新后重试。"},
            code="bible_image_snapshot_stale",
        )
    canonical = snapshot.get("canonical")
    if (
        snapshot.get("formal_object_type") != "bible_image_candidate_manifest"
        or not isinstance(canonical, dict)
        or canonical.get("generation_stage_id") != str(generation_stage.id)
        or _sha256(canonical) != snapshot.get("canonical_sha256")
    ):
        raise ProductionRunConflict(
            {"detail": "Bible 参考图生成快照无效，不能追加候选。"},
            code="bible_image_snapshot_invalid",
        )
    current = _canonical_bible_image_candidates(run, generation_stage)
    if _sha256(current) != snapshot.get("canonical_sha256"):
        raise ProductionRunConflict(
            {"detail": "Bible 候选或其来源已变化，请刷新后重试。"},
            code="bible_image_candidate_manifest_stale",
        )
    available = {
        (str(item.get("entity_type") or ""), str(item.get("entity_id") or ""))
        for item in canonical.get("entities", [])
        if isinstance(item, dict)
    }
    requested = {(item["entity_type"], item["entity_id"]) for item in entities}
    if not requested.issubset(available):
        raise ValidationError({"entities": "重新生成目标不属于当前冻结候选清单"})


def _next_bible_regeneration_variants(
    generation_stage: StageRun, *, entities: list[dict[str, str]],
) -> dict[tuple[str, str], list[int]]:
    from studio.models import ExecutionPreparation
    from studio.services.production_recipe_media_preparations import recipe_media_stage_context, read_recipe_media_candidate
    plan, declaration = recipe_media_stage_context(generation_stage, None)
    if generation_stage.generation_attempts.filter(status__in={
        "queued", "running", "submitting", "submitted", "cancel_requested",
    }).exists() or generation_stage.generation_attempts.filter(error_code="uncertain_remote_response").exists():
        raise ProductionRunConflict({"detail": "Bible阶段仍有在途或待消歧调用，不能追加候选。"},
            code="bible_regeneration_already_running")
    used = {}
    from studio.services.production_recipe_material_units import frozen_recipe_material_unit
    keys = [frozen_recipe_material_unit(generation_stage, plan,
        {"kind": item["entity_type"], "id": item["entity_id"]})[0]["unit_key"] for item in entities]
    for preparation in ExecutionPreparation.objects.filter(stage_run=generation_stage,
            unit_key__in=keys).order_by("unit_key", "request_key"):
        scope, number = read_recipe_media_candidate(preparation, stage=generation_stage, plan=plan, declaration=declaration)
        used.setdefault((scope["kind"], scope["id"]), set()).add(number)
    result = {}
    for item in entities:
        key = (item["entity_type"], item["entity_id"])
        previous = used.get(key, set())
        if not previous:
            raise ValidationError("追加候选必须具有本轮原执行准备，不能把历史任务当作当前实例候选")
        number = max(previous) + 1
        if number > _bible_candidate_max_items():
            raise ProductionRunConflict({"detail": "该实体已达到本轮候选数量上限，请调整配置或新建制作运行。"},
                code="bible_regeneration_variant_limit_reached")
        result[key] = [number]
    return result


def _clear_bible_review_for_regeneration(review_stage: StageRun) -> None:
    if (review_stage.stage_key != BIBLE_IMAGE_REVIEW_STAGE_KEY
            or review_stage.approval_decisions.exists()):
        raise ValidationError("只能重置尚无审核决定的 Bible 参考图审核阶段")
    # Regeneration invalidates an undecided review; it does not freeze an
    # empty contract. A digest of {} would be treated as an existing frozen
    # input and reject the completed candidate set on re-entry.
    snapshot_fields = []
    for field in ("input_snapshot", "output_snapshot"):
        for name in (field, f"{field}_schema_version", f"{field}_size_bytes", f"{field}_sha256"):
            setattr(review_stage, name, StageRun._meta.get_field(name).get_default())
            snapshot_fields.append(name)
    review_stage.status = StageRun.Status.PENDING
    review_stage.output_reference = ""
    review_stage.reason_code = ""
    review_stage.reason = ""
    review_stage.finished_at = None
    review_stage.save(update_fields=(
        *snapshot_fields,
        "status", "output_reference", "reason_code", "reason", "finished_at",
        "updated_at",
    ))


def _materialize_bible_regeneration(
    run: ProductionRun, generation_stage: StageRun, review_stage: StageRun, *, intent: dict[str, Any],
) -> ProductionRun:
    from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit, recipe_media_stage_context
    plan, _ = recipe_media_stage_context(generation_stage, None)
    # The original action records intent and allocates preparations atomically.
    # Admission, unknown prices and external authorization use the SAME Run gate
    # as initial generation; this action cannot approve or enqueue around it.
    _append_route_event(generation_stage, unit_key=f"bible-regenerate:{intent['request_id']}",
        event_type="bible_regeneration_preparing", code=intent["request_sha256"])
    generation_stage.status = StageRun.Status.RUNNING
    generation_stage.output_reference = ""
    generation_stage.reason_code = "waiting_execution_preparation"
    generation_stage.reason = "新增候选已进入执行准备，费用、字段审核与准入仍需通过。"
    generation_stage.finished_at = None
    generation_stage.save(update_fields=("status", "output_reference", "reason_code", "reason", "finished_at", "updated_at"))
    run.status = ProductionRun.Status.RUNNING
    run.current_stage_key = BIBLE_IMAGE_GENERATION_STAGE_KEY
    run.blocked_code = ""
    run.blocked_reason = ""
    run.finished_at = None
    run.save(update_fields=("status", "current_stage_key", "blocked_code", "blocked_reason", "finished_at", "updated_at"))
    for target in intent["targets"]:
        prepare_recipe_media_unit(stage_run_id=generation_stage.pk, expected_run_plan_sha256=plan.sha256,
            scope={"kind": target["entity_type"], "id": target["entity_id"]}, candidate_number=target["variant_number"],
            generation_request=intent, actor=run.owner)
    _clear_bible_review_for_regeneration(review_stage)
    _save_stage_snapshot(generation_stage, "output_snapshot", {})
    return advance_production_run(run)


@registry_reference_writer
@transaction.atomic
def regenerate_bible_image_candidates(
    run: ProductionRun,
    *,
    request_id: Any,
    expected_source_snapshot_sha256: str,
    entities: list[dict[str, Any]],
    user,
) -> ProductionRun:
    _require_recipe_control(run)
    request, request_sha256 = _bible_regeneration_request_contract(
        request_id=request_id,
        expected_source_snapshot_sha256=expected_source_snapshot_sha256,
        entities=entities,
    )
    run = ProductionRun.objects.select_for_update().select_related(
        "project", "project__owner",
    ).get(pk=run.pk)
    if not getattr(user, "is_staff", False) and user.pk != run.project.owner_id:
        raise ValidationError("无权为该项目重新生成 Bible 候选")
    generation_stage = run.stages.select_for_update().filter(
        stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
    ).first()
    review_stage = run.stages.select_for_update().filter(
        stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
    ).first()
    if generation_stage is None or review_stage is None:
        raise ValidationError("ProductionRun 不包含完整 Bible 参考图生成/审核阶段")
    event_unit_key = f"bible-regenerate:{request['request_id']}"
    previous_events = list(generation_stage.route_events.select_for_update().filter(
        unit_key=event_unit_key,
        event_type="bible_regeneration_preparing",
    ).order_by("sequence"))
    if previous_events:
        if any(item.code != request_sha256 for item in previous_events):
            raise ProductionRunConflict(
                {"detail": "同一 request_id 已绑定另一份 Bible 重新生成请求。"},
                code="bible_regeneration_idempotency_conflict",
            )
        return run
    if _is_unattended_run(run):
        raise ValidationError("全自动运行不能注入人工 Bible 候选重新生成请求")
    if (
        run.status != ProductionRun.Status.AWAITING_APPROVAL
        or run.current_stage_key != BIBLE_IMAGE_REVIEW_STAGE_KEY
        or generation_stage.status != StageRun.Status.SUCCEEDED
        or review_stage.status != StageRun.Status.AWAITING_APPROVAL
    ):
        raise ProductionRunConflict(
            {"detail": "只有 bible_image_review 待人工审核时才能追加模型候选。"},
            code="bible_regeneration_state_conflict",
        )
    if review_stage.approval_decisions.exists():
        raise ProductionRunConflict(
            {"detail": "Bible 审核已产生决定，不能再向该审核快照追加候选。"},
            code="bible_regeneration_review_already_decided",
        )
    _verify_bible_regeneration_source(
        run,
        generation_stage,
        expected_source_snapshot_sha256=request[
            "expected_source_snapshot_sha256"
        ],
        entities=request["entities"],
    )
    variants = _next_bible_regeneration_variants(
        generation_stage,
        entities=request["entities"],
    )
    intent = {
        **request,
        "request_sha256": request_sha256,
        "requested_by_id": str(user.pk),
        "targets": [
            {
                **item,
                "variant_number": variants[(item["entity_type"], item["entity_id"])][0],
            }
            for item in request["entities"]
        ],
    }
    return _materialize_bible_regeneration(
        run,
        generation_stage,
        review_stage,
        intent=intent,
    )


def retry_production_run(run: ProductionRun, *, user, expected_stage_id, expected_attempt_ids,
        expected_assembly_job_ids=None, expected_input_sha256=None) -> ProductionRun:
    # Guard outlives the transaction, including an advance/commit failure.
    from studio.services.registry_reference_lock import registry_reference_write_guard
    with _StoredAssetFileGuard() as guard, registry_reference_write_guard():
        return _retry_production_run(run, user=user, expected_stage_id=expected_stage_id,
            expected_attempt_ids=expected_attempt_ids, expected_assembly_job_ids=expected_assembly_job_ids,
            expected_input_sha256=expected_input_sha256, file_guard=guard)


def _retry_production_run(run, *, user, expected_stage_id, expected_attempt_ids,
        expected_assembly_job_ids, expected_input_sha256, file_guard):
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    _require_recipe_control(run)
    stage = run.stages.select_for_update().filter(stage_key=run.current_stage_key).first()
    if stage is None or str(stage.pk) != str(expected_stage_id):
        raise ValidationError("当前阶段已变化，请重新读取后确认。")
    if stage.stage_key != "assembly" and (expected_assembly_job_ids is not None or expected_input_sha256 is not None):
        raise ValidationError("非装配阶段不能提交装配重试范围。")
    if stage.stage_key == "assembly" and expected_attempt_ids:
        raise ValidationError("装配重试不能包含模型调用。")
    from studio.services.production_project_text_stages import is_project_text_stage
    from studio.services.production_recipe_media_stages import is_recipe_media_stage
    if expected_attempt_ids or stage.generation_attempts.filter(role="primary").exists():
        from studio.services.production_execution_retry import retry_stage_executions
        return retry_stage_executions(run, stage, expected_attempt_ids=expected_attempt_ids, actor=user)
    from studio.services.production_execution_retry import require_stage_retry_status
    require_stage_retry_status(run, stage)
    if is_project_text_stage(stage) or is_recipe_media_stage(stage):
        # No primary was submitted. Re-enter the original preparation path;
        # pending auxiliary batches still require their own explicit action.
        stage.status = StageRun.Status.PENDING
    elif stage.stage_key == "assembly":
        from studio.services.production_assembly_retry import assembly_retry_links, require_assembly_retry

        # Same Project -> Job order as the assembly completion worker.
        run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
        failed_links, configuration, selected_sources = require_assembly_retry(stage,
            list(assembly_retry_links(stage, lock=True)), expected_assembly_job_ids=expected_assembly_job_ids,
            expected_input_sha256=expected_input_sha256)
        for link in failed_links:
            try:
                job = create_assembly(
                    owner=run.project.owner,
                    project=run.project,
                    episode=link.episode,
                    dry_run=False,
                    frozen_sources={"schema_version": 1, "episodes": [row for row in selected_sources["episodes"]
                        if row["episode_id"] == str(link.episode_id)]},
                    frozen_configuration=configuration,
                    consumer_stage=stage,
                    file_guard=file_guard,
                )
            except AssemblyError as exc:
                # All confirmed episodes succeed together or retain their old
                # links/jobs. The outer guard removes only new manifests.
                raise ValidationError("装配重试未提交：" + str(exc)) from exc
            link.assembly_job = job
            link.save(update_fields=("assembly_job", "updated_at"))
        stage.status = StageRun.Status.PENDING
    else:
        stage.status = StageRun.Status.PENDING
    stage.reason_code = ""
    stage.reason = ""
    stage.finished_at = None
    stage.save(update_fields=("status", "reason_code", "reason", "finished_at", "updated_at"))
    run.status = ProductionRun.Status.QUEUED
    run.blocked_code = ""
    run.blocked_reason = ""
    run.finished_at = None
    run.save(update_fields=(
        "status", "blocked_code", "blocked_reason", "finished_at", "updated_at",
    ))
    return advance_production_run(run)


@transaction.atomic
def cancel_production_run(run: ProductionRun, *, user=None) -> ProductionRun:
    from studio.models import ExecutionPreparation
    from studio.services.execution_production_cancellation import cancellation_attempts, cancel_production_attempts, stop_production_preparations
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    if run.status in TERMINAL_RUN_STATUSES:
        raise ValidationError("ProductionRun 已终态，不能取消")
    now = timezone.now()
    # Same root -> stages -> preparations -> attempts order as dispatch/workers.
    list(StageRun.objects.select_for_update().filter(production_run=run).only("id").order_by("id"))
    preparations = list(ExecutionPreparation.objects.select_for_update().filter(stage_run__production_run=run).order_by("id"))
    attempts = list(cancellation_attempts(GenerationAttempt.objects.select_for_update(of=("self",)).filter(stage_run__production_run=run)))
    completed = {row.preparation_id for row in attempts if row.role == "primary" and row.status == "succeeded"}
    stop_production_preparations([row for row in preparations if row.pk not in completed], actor=user)
    cancel_production_attempts(run=run, attempts=attempts, actor=user)
    run.stages.exclude(status=StageRun.Status.SUCCEEDED).update(
        status=StageRun.Status.CANCELLED, reason_code="production_run_cancelled",
        reason="ProductionRun 已取消", finished_at=now, updated_at=now,
    )
    run.status = ProductionRun.Status.CANCELLED
    run.blocked_code = "production_run_cancelled"
    run.blocked_reason = "ProductionRun 已停止；已提交任务由原队列确认取消或结算，未知结果不会自动重发。"
    run.finished_at = now
    run.save(update_fields=("status", "blocked_code", "blocked_reason", "finished_at", "updated_at"))
    return run

def _require_recipe_control(run: ProductionRun) -> None:
    # Manual generation/adoption/approval uses the command receipt transaction;
    # these recipe control APIs must not become a second manual write path.
    source = ProductionRun.objects.values_list("plan_source", flat=True).get(pk=run.pk)
    if source != ProductionRun.PlanSource.RECIPE:
        raise ValidationError("无配方制作请使用对应步骤的人工动作，不能执行配方控制动作", code="run_plan_source_conflict")


def select_run_artifact(run: ProductionRun, artifact: GeneratedArtifact, *, user, material_selection=None):
    _require_recipe_control(run)
    if artifact.production_run_id != run.id:
        raise ValidationError("artifact 不属于该 ProductionRun")
    if artifact.storage_backend == "database" and artifact.generation_attempt_id and artifact.role != "structured_text_candidate":
        raise ValidationError("文本候选只能在对应审核阶段确认采用，不能通过通用选片跳过审核")
    if artifact.stage_run_id and artifact.stage_run.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY:
        from studio.services.production_recipe_material_selection import choose_recipe_material_candidate
        if material_selection is None or not artifact.generation_attempt_id:
            raise ValidationError("阶段内素材选择必须先查看影响并提交确切选择基线；此操作不是 Bible 审核采用。")
        decision = choose_recipe_material_candidate(stage_run_id=artifact.stage_run_id,
            unit_key=artifact.generation_attempt.preparation.unit_key, actor=user, artifact_id=artifact.pk,
            observed_selection=material_selection)
        return GeneratedArtifact.objects.get(pk=decision.artifact_id)
    if material_selection is not None:
        raise ValidationError("此产物不是配方素材阶段候选，不接受素材选择基线")
    if artifact.kind in {"image", "video", "audio"}:
        from studio.services.execution_production_candidates import read_production_media_candidate
        from studio.services.generated_artifacts import _artifact_limit
        # New manual choices use the same archived-file/source reader as the
        # manual-production entry. Historical receipts replay before this path.
        read_production_media_candidate(artifact, max_media_bytes=_artifact_limit(run))
    selected = select_generated_artifact(artifact, user=user)
    return selected


@registry_reference_writer
@transaction.atomic
def select_stage_route(
    run: ProductionRun,
    *,
    stage_key: str,
    route_index: int,
    user,
    appearance_role: str = "primary",
) -> ProductionRun:
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    _require_recipe_control(run)
    try:
        stage = StageRun.objects.select_for_update().get(
            production_run=run,
            stage_key=stage_key,
        )
    except StageRun.DoesNotExist as exc:
        raise ValidationError({"stage_key": "ProductionRun 不包含该阶段"}) from exc
    from studio.services.production_recipe_preparations import recipe_stage_context
    from studio.services.production_recipe_routing import recipe_routing
    from studio.models import ExecutionPreparation
    _, declaration = recipe_stage_context(stage)
    routing = recipe_routing(declaration, appearance_role=appearance_role)
    if routing.get("mode") != "manual_choice":
        raise ValidationError("只有 manual_choice 阶段可以显式选择 route")
    routes = routing.get("routes") or []
    if type(route_index) is not int or route_index < 0 or route_index >= len(routes):
        raise ValidationError({"route_index": "不在冻结 routes 范围内"})
    if stage.status not in {StageRun.Status.AWAITING_APPROVAL, StageRun.Status.PENDING}:
        raise ValidationError("当前阶段状态不能选择 route")
    if run.status in {ProductionRun.Status.CANCELLED, ProductionRun.Status.SUCCEEDED, ProductionRun.Status.FAILED}:
        raise ValidationError("已结束的运行不能更换路线")
    if ExecutionPreparation.objects.filter(stage_run=stage).exists():
        raise ValidationError("本阶段已创建执行准备，不能改写其路线；明确重试仍使用原准备")
    selection_field = "selected_variant_route_index" if appearance_role == "variant" else "selected_route_index"
    if getattr(stage, selection_field) == route_index:
        return run
    future = bool(run.current_stage_key and run.current_stage_key != stage.stage_key)
    setattr(stage, selection_field, route_index)
    stage.status = StageRun.Status.PENDING
    stage.reason_code = ""
    stage.reason = ""
    stage.finished_at = None
    stage.save(update_fields=(
        selection_field, "status", "reason_code", "reason", "finished_at", "updated_at",
    ))
    _mark_route_selected(
        stage,
        unit_key=f"stage:{stage.stage_key}" + (":variant" if appearance_role == "variant" else ""),
        route_index=route_index,
        reason=f"manual_choice_by:{user.id}",
    )
    # A media route may be selected before the preceding reference review.
    # Selecting it must not approve/unblock that other active stage.
    if future:
        return run
    run.status = ProductionRun.Status.QUEUED
    run.blocked_code = ""
    run.blocked_reason = ""
    run.finished_at = None
    run.save(update_fields=(
        "status", "blocked_code", "blocked_reason", "finished_at", "updated_at",
    ))
    return advance_production_run(run)


@registry_reference_writer
@transaction.atomic
def authorize_stage_cost(
    run: ProductionRun,
    *,
    stage_key: str,
    allow_external_calls: bool | None,
    approved_estimated_cost: Decimal | None,
    currency: str,
    comment: str,
    user,
) -> ProductionRun:
    comment = normalize_optional_audit_comment(comment)
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    from studio.services.production_budget_configuration import validate_budget_currency
    normalized_currency = str(currency or run.cost_currency or "").strip().upper()
    if normalized_currency:
        validate_budget_currency(Decimal("0"), normalized_currency)
    if run.cost_currency and normalized_currency != run.cost_currency and (run.generation_task_count or run.actual_cost_total or run.committed_cost_total):
        raise ValidationError({"currency": "已有调用记录，不能修改本轮记账币种或自动换算"})
    if not stage_key:
        if run.plan_source != "manual" or run.status not in {"running", "awaiting_input", "awaiting_approval", "blocked", "failed"}:
            raise ValidationError("仅未结束或待处理的无配方制作可以设置本轮外发授权；已完成、已取消或取消中的运行不可调整")
        if approved_estimated_cost is not None:
            raise ValidationError("本轮外发授权设置不批准任何阶段预估费用")
        if GenerationAttempt.objects.filter(stage_run__production_run=run,
                status__in=("queued", "running", "submitting", "submitted", "cancel_requested")).exists():
            raise ValidationError("本轮仍有进行中的调用，请结束后再调整授权")
        from studio.services.platform_lifecycle_audit import lifecycle_summary, record_platform_lifecycle_event
        before = lifecycle_summary(run)
        if allow_external_calls is not None:
            run.allow_external_calls = allow_external_calls
        run.cost_currency = normalized_currency
        run.save(update_fields=("allow_external_calls", "cost_currency", "updated_at"))
        # Creating a manual preparation marks the Run running, before any
        # actual call. Authorization saves must not implicitly wake its recheck.
        # Dispatch/claims share the Run lock; revisions reject in-flight local
        # evaluations. The user continues the same preparation explicitly.
        from studio.models import ExecutionPreparation
        for preparation in ExecutionPreparation.objects.select_for_update().filter(
                stage_run__production_run=run,
                status__in=("preparing", "blocked", "ready", "awaiting_input", "awaiting_review")).order_by("id"):
            preparation.revision += 1
            preparation.next_check_at = None
            preparation.claim_token = preparation.claim_until = None
            preparation.save(update_fields=("revision", "next_check_at", "claim_token", "claim_until", "updated_at"))
        record_platform_lifecycle_event(run, actor=user, action="authorize_budget", reason_code="production.budget_authorized",
            before_summary=before, after_summary=lifecycle_summary(run))
        return run
    try:
        stage = StageRun.objects.select_for_update().get(
            production_run=run,
            stage_key=stage_key,
        )
    except StageRun.DoesNotExist as exc:
        raise ValidationError({"stage_key": "ProductionRun 不包含该阶段"}) from exc
    allowed_codes = {
        "unknown_cost",
        "external_call_not_authorized", "cost_currency_missing", "actual_budget_exceeded",
    }
    prepared_retry_gate = stage.status == StageRun.Status.BLOCKED and stage.reason_code in {
        "execution_external_authorization_required",
    }
    if not prepared_retry_gate and (stage.status != StageRun.Status.AWAITING_APPROVAL or stage.reason_code not in allowed_codes):
        raise ValidationError("当前阶段不处于费用或外发授权门禁")
    if approved_estimated_cost is not None:
        if approved_estimated_cost < 0:
            raise ValidationError({"approved_estimated_cost": "不能为负数"})
        if approved_estimated_cost > 0 and not normalized_currency:
            raise ValidationError({"currency": "非零费用授权必须声明币种"})
        if run.cost_currency and normalized_currency and run.cost_currency != normalized_currency:
            raise ValidationError({"currency": "与运行已有费用币种不一致"})
        stage.approved_estimated_cost = approved_estimated_cost
        stage.approved_cost_currency = normalized_currency
    if allow_external_calls is not None:
        run.allow_external_calls = bool(allow_external_calls)
    run.cost_currency = normalized_currency
    revision = (stage.approval_decisions.aggregate(value=Max("revision"))["value"] or 0) + 1
    ApprovalDecision.objects.create(
        stage_run=stage,
        revision=revision,
        decision=ApprovalDecision.Decision.APPROVE,
        comment=normalize_optional_audit_comment(
            f"费用/外发授权: allow_external_calls={run.allow_external_calls}, "
            f"recorded_actual_cost={run.actual_cost_total} {run.cost_currency}, "
            f"approved_estimated_cost="
            f"{stage.approved_estimated_cost} {stage.approved_cost_currency}; {comment or ''}"
        ),
        decided_by=user,
    )
    if stage.reason_code == "actual_budget_exceeded" and run.plan_source == "manual":
        # The bill and successful candidate already exist. Acknowledgement
        # neither adopts that candidate nor enqueues another call.
        stage.status, stage.reason_code, stage.reason = "awaiting_input", "", ""
        stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
        other = run.stages.filter(status="awaiting_approval", reason_code="actual_budget_exceeded").order_by("id").first()
        run.status = "awaiting_approval" if other else "awaiting_input"
        run.blocked_code = other.reason_code if other else ""
        run.blocked_reason = other.reason if other else ""
        if other:
            run.current_stage_key = other.stage_key
        run.save(update_fields=("status", "blocked_code", "blocked_reason", "current_stage_key",
            "allow_external_calls", "cost_currency", "updated_at"))
        return run
    if prepared_retry_gate:
        stage.status = StageRun.Status.BLOCKED
        stage.reason_code = "execution_retry_confirmation_required"
        stage.reason = "预算/外发授权已更新；请明确确认失败调用重试，提交时仍会复核准入。"
        stage.save(update_fields=("approved_estimated_cost", "approved_cost_currency", "status",
            "reason_code", "reason", "updated_at"))
        run.status, run.blocked_code, run.blocked_reason = "blocked", stage.reason_code, stage.reason
        run.save(update_fields=("allow_external_calls", "cost_currency", "status", "blocked_code", "blocked_reason", "updated_at"))
        return run
    stage.status = StageRun.Status.PENDING
    stage.reason_code = ""
    stage.reason = ""
    stage.finished_at = None
    stage.save(update_fields=(
        "approved_estimated_cost", "approved_cost_currency", "status",
        "reason_code", "reason", "finished_at", "updated_at",
    ))
    run.status = ProductionRun.Status.QUEUED
    run.blocked_code = ""
    run.blocked_reason = ""
    run.finished_at = None
    run.save(update_fields=(
        "allow_external_calls", "cost_currency", "status", "blocked_code",
        "blocked_reason", "finished_at", "updated_at",
    ))
    return advance_production_run(run)
