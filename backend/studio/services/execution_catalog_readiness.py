"""Read-only configuration/evidence summary, never authorization to submit.

No concrete request exists here: capacity stays unknown and budget, ownership
and actual input checks remain with the original root's Preparation. The same
admission policy decides Smoke/TTL; a resource probe cannot certify a version.
"""
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from studio.services.security_boundaries import security_boundary_resolution_cache


OUTPUT_KINDS = ("text", "script", "image", "video", "audio")


@security_boundary_resolution_cache()
def configured_video_conditioning_modes(actor):
    """Visible, published combinations supply options, never Smoke admission."""
    from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
    from studio.services.execution_instances import selectable_execution_versions
    from studio.services.media_capabilities import merge_conditioning_contract_modes
    from studio.services.model_profile_options import MODEL_CAPABILITY_CATALOG

    # Scope the catalog before checking complete definitions. A text/image
    # combination is not a video planning dependency. Keep potentially invalid
    # video declarations in scope too: either their registered operation or
    # explicit resource output may identify them as video. Never catch and skip
    # validation failures of these candidates, or infer eligibility from names.
    video_operations = [row["value"] for row in MODEL_CAPABILITY_CATALOG if "video" in row["modalities"]]
    versions = selectable_execution_versions(actor).filter(
        deployment__status="active", deployment__profile_version__status="published",
        deployment__profile_version__provider__is_active=True,
    ).filter(
        Q(operation__in=video_operations)
        | Q(workflow_version__template__output_kind="video")
        | Q(workflow_version__isnull=True, deployment__profile_version__modality="video")
    ).order_by("instance__key", "version").values_list("pk", flat=True)
    contracts = []
    for identifier in versions.iterator():
        definition = verify_current_execution_definition(stored_execution_contract(identifier))
        if output_kind(definition) != "video":
            continue
        selected = definition.value["resources"]["conditioning_mode"]
        if selected is not None:
            contracts.append({"modes": [selected]})
    return merge_conditioning_contract_modes(contracts)


def output_kind(definition):
    interface = definition.value["interface"]
    if definition.value["target"]["driver_type"] == "comfyui":
        return interface["output"]["contract"]["primary"]["kind"]
    return interface["output"]["kind"]


def worker_kind(definition):
    driver = definition.value["target"]["driver_type"]
    return "generation" if driver == "comfyui" else "async_media" if driver == "async_rest" else "text_generation"


@dataclass
class ExecutionRequirements:
    versions: dict = field(default_factory=dict)
    primary_version_ids: set = field(default_factory=set)
    route_groups: list = field(default_factory=list)
    assembly: bool = False
    invalid_recipes: dict = field(default_factory=dict)

    @property
    def invalid_recipe_ids(self):
        return set(self.invalid_recipes)

    @property
    def needs_text_worker(self):
        return any(worker_kind(row[1]) == "text_generation" for row in self.versions.values())


def execution_requirements(stages, *, into=None):
    """Consume current stage declarations only; do not expand legacy routes."""
    from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition, execution_definition_version
    from studio.services.production_recipe_contracts import recipe_stage_kind, recipe_stage_routings, read_recipe_execution_routing, recipe_validation_context
    from studio.services.execution_input_catalog import execution_text_output_options
    from studio.services.production_stage_catalog import stage_requires_execution_route

    result = into if into is not None else ExecutionRequirements()

    def include(identifier, digest, *, leaf=False):
        if not leaf:
            result.primary_version_ids.add(identifier)
        if identifier in result.versions:
            if result.versions[identifier][1].sha256 != digest:
                raise ValidationError("同一实例版本被引用为不同的冻结合同")
            return
        contract = stored_execution_contract(identifier)
        if contract.sha256 != digest:
            raise ValidationError("配方或辅助引用与精确实例定义不一致")
        definition = verify_current_execution_definition(contract)
        version = execution_definition_version(identifier)
        if output_kind(definition) not in OUTPUT_KINDS:
            raise ValidationError("实例输出类型未接入当前就绪目录")
        auxiliaries = definition.value["interface"].get("auxiliaries", {})
        if leaf and auxiliaries:
            raise ValidationError("辅助实例不能再声明递归依赖")
        result.versions[identifier] = (version, definition)
        for auxiliary in auxiliaries.values():
            include(auxiliary["version_id"], auxiliary["definition_sha256"], leaf=True)

    prior = []
    text_outputs = execution_text_output_options(stages)
    for index, stage in enumerate(stages):
        if stage["key"] == "assembly" and stage["mode"] != "skip":
            result.assembly = True
        required = stage_requires_execution_route(recipe_stage_kind(stage), stage["mode"])
        if required:
            from studio.services.production_recipe_material_sources import recipe_material_source
            primary_generated = recipe_stage_kind(stage) != "bible_image_generation" or recipe_material_source(stage, "primary") == "generate"
            if "routing" not in stage and primary_generated:
                raise ValidationError("模型调用阶段缺少精确实例路线，不能按无需执行处理")
            for key, value in recipe_stage_routings(stage):
                with recipe_validation_context(stage_index=index, stage_key=stage["key"], stage_kind=recipe_stage_kind(stage), routing_key=key):
                    routing = read_recipe_execution_routing(value, prior_stages=prior,
                        require_complete=True, stage_key=recipe_stage_kind(stage), text_outputs=text_outputs)
                    for route_index, route in enumerate(routing["routes"]):
                        with recipe_validation_context(route_index=route_index):
                            include(route["execution_instance_version_id"], route["expected_definition_sha256"])
                result.route_groups.append(tuple(dict.fromkeys(route["execution_instance_version_id"] for route in routing["routes"])))
        elif "routing" in stage:
            raise ValidationError("无需模型的阶段不能声明执行路线")
        prior.append(stage["key"])
    return result


def published_execution_requirements():
    from studio.models import ProductionRecipeVersion
    from studio.services.production_runs import _recipe_stages
    from studio.services.production_recipe_contracts import recipe_validation_errors

    result = ExecutionRequirements()
    # Low-frequency catalog contracts are read once per version, not returned
    # as payload. Neither graph bodies nor Run/task history are selected here.
    recipes = ProductionRecipeVersion.objects.filter(status="published").only(
        "id", "definition", "definition_schema_version", "definition_size_bytes", "definition_sha256"
    ).order_by("id")
    for recipe in recipes.iterator():
        phase = "definition"
        try:
            stages = _recipe_stages(recipe)
            phase = "requirements"
            current = execution_requirements(stages)
            for identifier, row in current.versions.items():
                if identifier in result.versions and result.versions[identifier][1].sha256 != row[1].sha256:
                    raise ValidationError("已发布配方的实例引用不一致")
            result.versions.update(current.versions)
            result.primary_version_ids.update(current.primary_version_ids)
            result.route_groups.extend(current.route_groups)
            result.assembly = result.assembly or current.assembly
        except ValidationError as exc:
            # Invalid/old contracts are visible, never an empty requirement.
            result.invalid_recipes[str(recipe.pk)] = recipe_validation_errors(exc, phase=phase, stages=recipe.definition.get("stages", []) if isinstance(recipe.definition, dict) else ())
    return result


def catalog_capability_readiness(requirements, *, worker_check, toolchain_check):
    from studio.services.execution_admission import evaluate_execution_admission
    from studio.services.execution_policy_configuration import read_execution_policies
    from studio.services.execution_resource_admission import read_execution_resource_state
    from studio.services.execution_smoke_evidence import read_instance_smoke_facts, instance_smoke_identity

    policies = read_execution_policies() if requirements.versions else None
    rows = {kind: [] for kind in OUTPUT_KINDS}
    workers = {}
    now = timezone.now()
    for identifier, (version, definition) in requirements.versions.items():
        kind = output_kind(definition)
        worker = worker_kind(definition)
        if worker not in workers:
            workers[worker] = worker_check(worker)
        blockers = [] if workers[worker]["ok"] else [
            {"code": worker + "_worker_unavailable", "message": "对应原执行 Worker 不可用"}]
        try:
            blockers.extend(read_execution_resource_state(version).value["blockers"])
            decision = evaluate_execution_admission(identity=instance_smoke_identity(version.pk, definition),
                smoke_facts=read_instance_smoke_facts(version, definition), purpose="production",
                smoke_policy=policies.smoke_policy, capacity_policy=policies.capacity_policy,
                capacity_state="unknown", hard_blockers=blockers, now=now)
            codes = [item["code"] for item in decision["blockers"]]
            warnings = [item["code"] for item in decision["warnings"]]
            state = decision["evidence_state"]
        except ValidationError:
            codes, warnings, state = ["execution_evidence_unverifiable"], [], "unverifiable"
        rows[kind].append({"execution_instance_version_id": identifier,
            "definition_sha256": definition.sha256, "state": "blocked" if codes else "ready",
            "evidence_state": state, "reason_codes": codes, "warning_codes": warnings,
            "conditional": identifier not in requirements.primary_version_ids})
    if policies is not None and read_execution_policies().configuration.sha256 != policies.configuration.sha256:
        raise ValidationError("执行策略在读取期间变化，请重新核对")

    by_id = {row["execution_instance_version_id"]: row for entries in rows.values() for row in entries}
    if {identifier for group in requirements.route_groups for identifier in group} != requirements.primary_version_ids:
        raise ValidationError("实例路线组与精确引用目录不一致")
    grouped_reasons, alternative_warnings = {kind: set() for kind in OUTPUT_KINDS}, set()
    for group in requirements.route_groups:
        entries = [by_id[identifier] for identifier in group]
        if not any(row["state"] == "ready" for row in entries):
            codes = {code for row in entries for code in row["reason_codes"]}
            for identifier in group:
                grouped_reasons[output_kind(requirements.versions[identifier][1])].update(codes)
        elif any(row["state"] == "blocked" for row in entries):
            alternative_warnings.update(output_kind(requirements.versions[identifier][1]) for identifier in group)

    def summary(entries, *, required, extra_reasons=()):
        reasons = set(extra_reasons)
        if requirements.invalid_recipe_ids:
            reasons.add("recipe_contract_invalid")
            required = True
        warnings = {code for row in entries for code in row["warning_codes"]}
        if any(row["conditional"] and row["reason_codes"] for row in entries):
            warnings.add("execution_auxiliary_attention")
        return {"scope": "configuration_only", "request_checked": False,
            "state": "blocked" if reasons else "ready" if required else "not_required", "required": required,
            "reason_codes": sorted(reasons), "warning_codes": sorted(warnings),
            "instance_count": None if requirements.invalid_recipe_ids else len(entries),
            "viable_instance_count": None if requirements.invalid_recipe_ids else sum(row["state"] == "ready" for row in entries)}

    report = {kind: summary(entries, required=bool(entries), extra_reasons=grouped_reasons[kind]) for kind, entries in rows.items()}
    for kind in alternative_warnings:
        report[kind]["warning_codes"] = sorted({*report[kind]["warning_codes"], "execution_route_alternative_attention"})
    assembly_reasons = []
    if requirements.assembly:
        if not toolchain_check()["ok"]:
            assembly_reasons.append("toolchain_unavailable")
        if not worker_check("assembly")["ok"]:
            assembly_reasons.append("assembly_worker_unavailable")
    report["assembly"] = summary([], required=requirements.assembly, extra_reasons=assembly_reasons)
    return {"schema_version": 2, **report}
