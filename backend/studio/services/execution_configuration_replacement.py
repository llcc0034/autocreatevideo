"""Explicit policy-only successors for unsubmitted recipe media preparations.

Reuse the original supersedes chain, owner locks and queue. Inputs, parameters,
writeback basis and review policy are copied from the verified parent, never
resolved from today's project. Admission still belongs to normal advancement.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.models import ExecutionPreparation, ModelExecutionInstanceVersion, ProductionAssemblyLink
from studio.services.execution_definitions import (
    execution_definition_version, stored_execution_contract,
    verify_current_execution_definition, verify_stored_execution_definition,
)
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_preparation_fields import _locked_preparations, _revision
from studio.services.execution_preparations import ResolvedExecutionPreparation, read_execution_preparation_detail
from studio.services.json_contracts import json_contract
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.security_boundaries import security_boundary_resolution_cache


POLICY = "explicit_prompt_strategy_successor_v1"


def _contract(value):
    return json_contract(value, max_bytes=execution_configuration_max_bytes())


def _policy_independent_definition(definition):
    value = deepcopy(definition.value)
    version_id = value.pop("version_id")
    profile_id = value["configuration"].pop("prompt_profile_version_id")
    value["resources"].pop("prompt_profile")
    for key in ("profile_id", "profile_sha256"):
        value["interface"]["prompt"].pop(key)
    for entry in value["interface"].get("profile_inputs", {}).values():
        if entry.get("source") == f"profile.{profile_id}.rules":
            entry["source"] = "profile.self.rules"
    # The source label includes the enclosing version, not a parameter change.
    _parameter_source_identity(value["interface"]["parameters"], {},
        f"instance.{version_id}.parameters", "instance.self.parameters")
    return value


def _parameter_source_identity(contracts, sources, before, after):
    for entry in contracts.values():
        if entry["value_source"] == before:
            entry["value_source"] = after
        for layer in entry["layers"]:
            if layer["source"] == before:
                layer["source"] = after
    for key, source in sources.items():
        if source == before:
            sources[key] = after


def _successor_parameters(parent, detail, definition):
    from studio.services.execution_parameter_preparation import verify_prepared_execution_parameters
    original = verify_stored_execution_definition(stored_execution_contract(parent.execution_instance_version_id))
    parameters = deepcopy(verify_prepared_execution_parameters(original, detail.resolved_inputs["parameters"]))
    _parameter_source_identity(parameters["contracts"], parameters["sources"],
        f"instance.{parent.execution_instance_version_id}.parameters", f"instance.{definition.value['version_id']}.parameters")
    # Revalidate the identical values/rules with only the explicit owner label
    # changed. No new seed, policy resolution or current-project read occurs.
    return verify_prepared_execution_parameters(definition, parameters)


def compatible_successor(parent, version_id, *, current=False):
    old_version = execution_definition_version(parent.execution_instance_version_id)
    version = execution_definition_version(version_id)
    old_profile, profile = old_version.prompt_profile_version, version.prompt_profile_version
    if (version.instance_id != old_version.instance_id or version.version <= old_version.version
            or not old_profile or not profile or profile.profile_key != old_profile.profile_key
            or profile.version <= old_profile.version or version.prompt_mode != "profile"):
        raise ValidationError("必须选择同一执行实例及同一提示词策略的明确后继版本")
    if current and (version.status != "published" or not version.instance.is_active or profile.status != "published"):
        raise ValidationError("后继实例及策略必须已发布且实例启用")
    before = verify_stored_execution_definition(stored_execution_contract(old_version.pk))
    after = (verify_current_execution_definition if current else verify_stored_execution_definition)(stored_execution_contract(version.pk))
    if _policy_independent_definition(before) != _policy_independent_definition(after):
        raise ValidationError("这里只允许策略后继；模型、工作流、输入接线、参数、输出和辅助实例必须保持不变")
    return version, before, after


def _replacement_facts(parent, detail, definition, version):
    from studio.services.execution_profile_preparation import prepare_execution_profile
    from studio.services.media_generation_targets import _target_value
    facts = deepcopy(detail.resolved_inputs)
    facts["parameters"] = _successor_parameters(parent, detail, definition)
    profile = facts["profile_sources"]
    scopes = {(row["kind"], row["id"]): row["unit_keys"] for row in profile["scope_units"]} if "scope_units" in profile else None
    prepared = prepare_execution_profile(definition,
        json_contract(facts["inputs"], max_bytes=execution_resolved_inputs_max_bytes()),
        profile=version.prompt_profile_version, unit_key=parent.unit_key,
        source_language=profile["source_language"], target_language=profile["target_language"],
        prepared_parameters=facts["parameters"], scope_units=scopes)
    facts.update({key: prepared.value[key] for key in ("profile_sources", "field_requests")})
    target = facts["media_generation_target"]
    facts["media_generation_target"] = _target_value(definition, facts,
        candidate_count=target["candidates"]["count"], candidate_evaluator=target["candidates"]["evaluator"],
        appearance=target.get("appearance"), request=detail.intent["request"]["requested"])
    return ResolvedExecutionPreparation(
        json_contract(facts, max_bytes=execution_resolved_inputs_max_bytes()), _contract(detail.review_contract))


def _base_requested(value):
    return {key: child for key, child in value.items() if key not in {"supersedes_preparation_id", "configuration_replacement"}}


def verify_replacement_parent(parent, requested):
    from studio.services.production_recipe_material_repreparation import require_unexecuted_preparation
    detail = require_unexecuted_preparation(parent, stopped=True)
    evidence = requested.get("configuration_replacement")
    if (not isinstance(evidence, dict) or set(evidence) != {"preview", "sha256"}
            or not isinstance(evidence["preview"], dict) or _contract(evidence["preview"]).sha256 != evidence["sha256"]):
        raise ValidationError("策略后继切换缺少明确确认摘要")
    preview = evidence["preview"]
    if (preview.get("policy") != POLICY or preview.get("preparation_id") != str(parent.pk)
            or preview.get("intent_sha256") != parent.intent_sha256 or parent.request_frozen_at is not None
            or _base_requested(requested) != _base_requested(detail.intent["request"]["requested"])):
        raise ValidationError("策略切换不能改变原单元、来源、参数或已冻结请求")
    _, before, after = compatible_successor(parent, preview["version_id"])
    if (preview.get("before_definition_sha256") != before.sha256
            or preview.get("definition_sha256") != after.sha256
            or preview.get("resolved_inputs_sha256") != detail.resolved_inputs_sha256
            or preview.get("review_contract_sha256") != detail.review_contract_sha256):
        raise ValidationError("策略切换确认与原准备或精确后继合同不一致")
    return detail


def replacement_route_identity(preparation, *, stage, plan, declaration, detail, visited):
    """Follow the explicit edge to the frozen route, never pick a latest version."""
    from studio.services.production_recipe_routing import verify_recipe_preparation_route
    parent = preparation.supersedes
    verify_recipe_preparation_route(parent, stage=stage, plan=plan, declaration=declaration, _visited=visited)
    requested = detail.intent["request"]["requested"]
    if "configuration_replacement" not in requested:
        # A later primary-image reselection may keep this already chosen policy.
        prior = read_execution_preparation_detail(parent).intent["request"]
        return str(parent.execution_instance_version_id), prior["definition_sha256"]
    old = verify_replacement_parent(parent, requested)
    preview = requested["configuration_replacement"]["preview"]
    version, _, definition = compatible_successor(parent, preview["version_id"])
    facts = _replacement_facts(parent, old, definition, version)
    if facts.resolved_inputs.sha256 != detail.resolved_inputs_sha256 or facts.review_contract.sha256 != detail.review_contract_sha256:
        raise ValidationError("策略替代准备改变了原输入、参数或审核规则")
    return str(version.pk), definition.sha256


def _context(parent):
    from studio.services.production_recipe_material_repreparation import require_unexecuted_preparation
    from studio.services.production_recipe_routing import require_current_recipe_preparation
    if (not parent.stage_run_id or parent.stage_run.production_run.plan_source != "recipe"
            or parent.stage_run.stage_key not in {"bible_image_generation", "media_generation"}):
        raise ValidationError("此入口适用于配方主图、变体及镜头媒体准备")
    stage = parent.stage_run
    if (stage.production_run.status != "running" or stage.status != "running"
            or ProductionAssemblyLink.objects.filter(production_run_id=stage.production_run_id).exists()):
        raise ValidationError("只允许当前执行中且尚未装配的阶段，不能改变已结束制作")
    detail = require_unexecuted_preparation(parent)
    if parent.request_frozen_at is not None:
        raise ValidationError("主请求已冻结，不能通过策略切换重新生成")
    require_current_recipe_preparation(parent, stage=stage)
    return detail


def _changes(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict):
        return [row for key in sorted(before.keys() | after.keys())
            for row in _changes(before.get(key), after.get(key), f"{path}/{key}")]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [row for index, (left, right) in enumerate(zip(before, after))
            for row in _changes(left, right, f"{path}/{index}")]
    return [] if before == after else [{"path": path, "before": before, "after": after}]


def _preview(parent, version_id, expected_preparation_revision):
    from studio.services.production_recipe_material_repreparation import _auxiliary_dependencies
    from studio.services.prompt_profiles import verify_prompt_profile_contract
    _revision(parent.revision, expected_preparation_revision, "expected_preparation_revision")
    detail = _context(parent)
    version, before, after = compatible_successor(parent, version_id, current=True)
    resolved = _replacement_facts(parent, detail, after, version)  # Dry preparation only, no allocation or call.
    children, attempts = _auxiliary_dependencies(parent, detail)
    dependencies = {"preparations": [{"id": str(row.pk), "revision": row.revision, "status": row.status} for row in children],
        "attempts": [{"id": str(row.pk), "status": row.status, "request_sha256": row.request_sha256,
            "error_code": row.error_code} for row in attempts]}
    identity = {"policy": POLICY, "preparation_id": str(parent.pk), "expected_preparation_revision": parent.revision,
        "intent_sha256": parent.intent_sha256, "resolved_inputs_sha256": detail.resolved_inputs_sha256,
        "review_contract_sha256": detail.review_contract_sha256, "before_definition_sha256": before.sha256,
        "version_id": str(version.pk), "definition_sha256": after.sha256,
        "dependencies_sha256": _contract(dependencies).sha256}
    changes = _changes(verify_prompt_profile_contract(parent.execution_instance_version.prompt_profile_version),
        verify_prompt_profile_contract(version.prompt_profile_version))
    return {"preview": identity, "sha256": _contract(identity).sha256}, changes, resolved, children, attempts


@registry_reference_writer
@security_boundary_resolution_cache()
def replacement_options(*, preparation_id, actor):
    with _locked_preparations([preparation_id], actor) as rows:
        parent = rows[str(preparation_id)]
        result = {"preparation_id": str(parent.pk), "revision": parent.revision, "allowed": False,
            "reason": "", "versions": [], "replacement_id": None}
        successor = ExecutionPreparation.objects.filter(supersedes=parent).first()
        if successor:
            result.update(reason="此准备已被明确替代，请查看后继准备。", replacement_id=str(successor.pk))
            return result
        try:
            _context(parent)
        except ValidationError as exc:
            result["reason"] = "；".join(exc.messages)
            return result
        versions = ModelExecutionInstanceVersion.objects.filter(instance_id=parent.execution_instance_version.instance_id,
            status="published", version__gt=parent.execution_instance_version.version).order_by("version")
        for version in versions:
            try:
                version, _, definition = compatible_successor(parent, version.pk, current=True)
            except ValidationError:
                continue
            result["versions"].append({"id": str(version.pk), "version": version.version,
                "name": version.instance.name, "profile_name": version.prompt_profile_version.name,
                "profile_version": version.prompt_profile_version.version, "definition_sha256": definition.sha256})
        result.update(allowed=True, reason="只切换明确策略后继；原输入、素材、种子与其他采样参数不变。")
        return result


@registry_reference_writer
@security_boundary_resolution_cache()
def preview_configuration_replacement(*, preparation_id, version_id, expected_preparation_revision, actor):
    with _locked_preparations([preparation_id], actor) as rows:
        parent = rows[str(preparation_id)]
        evidence, changes, *_ = _preview(parent, version_id, expected_preparation_revision)
        return {"preparation_id": str(parent.pk), "version_id": str(version_id),
            "revision": parent.revision, "preview_sha256": evidence["sha256"], "changes": changes}


@registry_reference_writer
@security_boundary_resolution_cache()
def apply_configuration_replacement(*, preparation_id, version_id, expected_preparation_revision, preview_sha256, actor):
    from studio.services.execution_preparations import create_execution_instance_preparation
    from studio.services.execution_production_cancellation import cancel_production_attempts, stop_production_preparations
    from studio.services.production_recipe_material_repreparation import _queued_before_submission
    from studio.services.production_recipe_routing import candidate_request_key
    with _locked_preparations([preparation_id], actor) as rows:
        parent = rows[str(preparation_id)]
        existing = ExecutionPreparation.objects.filter(supersedes=parent).first()
        if existing:
            detail = read_execution_preparation_detail(existing)
            evidence = detail.intent["request"]["requested"].get("configuration_replacement", {})
            if (evidence.get("sha256") != preview_sha256 or str(existing.execution_instance_version_id) != str(version_id)
                    or evidence.get("preview", {}).get("expected_preparation_revision") != expected_preparation_revision):
                raise ValidationError("此准备已被另一项确认替代，请重新读取", code="execution_preparation_conflict")
            verify_replacement_parent(parent, detail.intent["request"]["requested"])
            return existing  # Lost-response replay never allocates or dispatches again.
        evidence, _, resolved, children, attempts = _preview(parent, version_id, expected_preparation_revision)
        if evidence["sha256"] != preview_sha256:
            raise ValidationError("配置或准备状态已变化，请重新预览", code="execution_preparation_conflict")
        original = read_execution_preparation_detail(parent).intent["request"]["requested"]
        requested = {**_base_requested(original), "supersedes_preparation_id": str(parent.pk), "configuration_replacement": evidence}
        queued = [row for row in attempts if _queued_before_submission(row)]
        terminal = {row.preparation_id for row in attempts if row not in queued}
        stop_production_preparations([parent, *(row for row in children if row.pk not in terminal)], actor=actor)
        cancel_production_attempts(run=parent.stage_run.production_run, attempts=queued, actor=actor)
        return create_execution_instance_preparation(owner_ref=("stage_run_id", parent.stage_run_id),
            version_id=version_id, expected_definition_sha256=evidence["preview"]["definition_sha256"],
            unit_key=parent.unit_key, request_key=candidate_request_key(original["route_index"], original["candidate_number"], supersedes_id=parent.pk),
            intent=_contract(requested), resolve_facts=lambda definition, stage: resolved, actor=actor, supersedes=parent)
