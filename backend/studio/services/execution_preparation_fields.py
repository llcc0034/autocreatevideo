"""Locked field revisions/choices for an already authorized frozen preparation.

Field APIs delegate here; worker advance/finalize remain separate. Inputs are existing frozen field-request contracts,
not caller-authored rules. No model invocation, automatic approval or latest
revision selection occurs in this service.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Max, OuterRef, Subquery
from django.utils import timezone

from studio.models import ExecutionFieldSelection, ExecutionFieldValue, ExecutionPreparation
from studio.services.execution_field_contracts import validate_execution_field, verify_execution_field_request
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.json_contracts import JsonContract, json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.execution_preparation_owners import locked_preparation_owners, preparation_owner_ref


def _conflict(message):
    raise ValidationError(message, code="execution_preparation_conflict")


def _revision(actual, expected, label):
    if type(expected) is not int or expected < 1:
        raise ValidationError({label: "必须提供正整数修订号"})
    if actual != expected:
        _conflict("准备或采用记录已更新，请重新读取")


def _editable(preparation):
    if preparation.request_frozen_at is not None or preparation.status == "cancelled":
        _conflict("准备已冻结或取消，需要建立新请求")


@contextmanager
def _locked_preparations(ids, actor):
    """Root Run/owner -> Preparation; rows/choices are locked by callers next."""
    ids = {str(value) for value in ids}
    rows = list(ExecutionPreparation.objects.filter(pk__in=ids))
    if len(rows) != len(ids):
        raise ExecutionPreparation.DoesNotExist("执行准备不存在")
    refs = {str(row.pk): preparation_owner_ref(row) for row in rows}
    with locked_preparation_owners(refs.values(), actor):
        locked = list(ExecutionPreparation.objects.select_for_update().filter(pk__in=ids).order_by("id"))
        if len(locked) != len(ids):
            raise ExecutionPreparation.DoesNotExist("执行准备已删除")
        for row in locked:
            if preparation_owner_ref(row) != refs[str(row.pk)]:
                _conflict("执行准备所属任务已变化")
        yield {str(row.pk): row for row in locked}


def frozen_preparation_field_requests(requests):
    """Encode verified internal auxiliary contracts into resolved-input detail.

    Freezing a field request does not schedule it: a user can supply the field
    text manually and use exactly the same frozen source/rule validation.
    """
    if not isinstance(requests, (tuple, list)):
        raise ValidationError("字段请求必须为明确集合")
    result, destinations = [], set()
    for contract in requests:
        value = verify_execution_field_request(contract)
        for row in value["items"]:
            key = (row["unit_key"], row["field_contract"]["field_key"])
            if key in destinations:
                raise ValidationError("同一准备的单元字段只能有一个冻结来源")
            destinations.add(key)
        result.append(asdict(contract))
    return json_contract(result, max_bytes=execution_request_max_bytes()).value


def _field_contexts(preparation):
    try:
        contract = preparation.contract
    except ObjectDoesNotExist as exc:
        raise ValidationError("准备缺少已冻结详情，不能推断当前来源") from exc
    contract.clean()
    if type(contract.resolved_inputs.get("schema_version")) is not int or contract.resolved_inputs["schema_version"] != 1:
        raise ValidationError("字段来源需要当前解析事实合同")
    requests = contract.resolved_inputs.get("field_requests")
    if not isinstance(requests, list):
        raise ValidationError("字段来源合同缺失，不能重新选取当前策略")
    if preparation.execution_instance_version_id:
        profile = preparation.execution_instance_version.prompt_profile_version
    elif preparation.deployment_smoke_id:
        profile = preparation.deployment_smoke.prompt_profile_version
    else:
        # Basic Workflow fixtures do not acquire a prompt strategy implicitly.
        profile = None
    if profile is None:
        if not requests:
            return {}
        raise ValidationError("此准备没有精确主提示词策略，不能填写策略字段")
    contexts = {}
    for envelope in requests:
        if not isinstance(envelope, dict) or set(envelope) != {"value", "schema_version", "size_bytes", "sha256"}:
            raise ValidationError("字段来源合同或摘要缺失")
        request = verify_execution_field_request(JsonContract(**envelope))
        if request["profile"] != {"version_id": str(profile.pk), "contract_sha256": profile.contract_sha256}:
            raise ValidationError("字段来源与本次精确主策略不一致")
        for row in request["items"]:
            identity = (row["unit_key"], row["field_contract"]["field_key"])
            if identity in contexts:
                raise ValidationError("准备中存在重复字段来源")
            source = {key: value for key, value in row.items() if key not in {"field_contract", "request_id"}}
            contexts[identity] = {
                "request": request, "item": row,
                "source_sha256": json_contract({"profile": request["profile"], **source}, max_bytes=execution_request_max_bytes()).sha256,
                "field_contract_sha256": json_contract(row["field_contract"], max_bytes=execution_configuration_max_bytes()).sha256,
            }
    return contexts


def _field_context(preparation, unit_key, field_key):
    context = _field_contexts(preparation).get((unit_key, field_key))
    if context is None:
        raise ValidationError("当前准备未声明此单元字段")
    return context


def read_preparation_field_slots(preparation):
    """Bounded form catalog from frozen rules; never choose or create values.

    Caller holds the original owner and Preparation locks. Max revision is a
    single grouped query, independent of the paginated revision-history page.
    """
    contexts = _field_contexts(preparation)
    if not contexts:
        return []
    latest_id = preparation.field_values.filter(unit_key=OuterRef("unit_key"), field_key=OuterRef("field_key")).order_by("-revision").values("id")[:1]
    latest = {(row["unit_key"], row["field_key"]): row
        for row in preparation.field_values.order_by().values("unit_key", "field_key").annotate(
            latest=Max("revision"), latest_id=Subquery(latest_id))}
    return [{"unit_key": unit, "field_key": key, "field_label": context["item"]["field_contract"]["field_definition"]["label"],
        "next_revision": latest.get((unit, key), {}).get("latest", 0) + 1,
        "latest_value_id": latest.get((unit, key), {}).get("latest_id"),
        "effective_language": preparation_field_language(context["item"]),
        "field_contract": context["item"]["field_contract"],
        "source_language": context["item"]["source_language"], "target_language": context["item"]["target_language"]}
        for (unit, key), context in contexts.items()]


def preparation_field_language(item):
    """Display the frozen field policy, not the project's generic target language."""
    policy = item["field_contract"]["language_policy"]
    if policy == "inherit":
        return item["target_language"]
    if policy == "preserve_source":
        return item["source_language"]
    return policy


def _validate_text(context, text):
    row = context["item"]
    return validate_execution_field(
        field_contract=row["field_contract"], profile_field_keys=context["request"]["profile_field_keys"],
        source_text=row["source_text"], source_language=row["source_language"], target_language=row["target_language"],
        protected_tokens=row["reference_facts"]["protected_tokens"], text=text, request_id=row["request_id"],
        reference_facts=row["reference_facts"],
    )


def _verify_value(context, value, *, validate_text=True):
    value.clean()
    if any(getattr(value, key) != context[key] for key in ("source_sha256", "field_contract_sha256")):
        _conflict("字段来源或规则与本次准备不一致")
    if validate_text:
        _validate_text(context, value.text)


def _verify_reuse_identity(preparation, source):
    if preparation.execution_instance_version_id != source.execution_instance_version_id:
        _conflict("不能跨执行实例复用字段结果")
    if preparation.execution_instance_version_id is None and (
        not preparation.deployment_smoke_id or not source.deployment_smoke_id
        or preparation.deployment_smoke.deployment_id != source.deployment_smoke.deployment_id
        or preparation.deployment_smoke.operation != source.deployment_smoke.operation
    ):
        _conflict("基础验收复用字段必须属于相同部署与能力")


def _touch(preparation):
    preparation.revision += 1
    preparation.status = "preparing"
    preparation.reason_code = ""
    preparation.reason = ""
    preparation.next_check_at = timezone.now()
    preparation.save(update_fields=("revision", "status", "reason_code", "reason", "next_check_at", "updated_at"))


@security_boundary_resolution_cache()
def create_manual_execution_field(*, preparation_id, unit_key, field_key, text, revision, expected_preparation_revision, actor):
    with _locked_preparations([preparation_id], actor) as rows:
        preparation = rows[str(preparation_id)]
        if type(revision) is not int or revision < 1:
            raise ValidationError({"revision": "必须提供明确字段修订号"})
        context = _field_context(preparation, unit_key, field_key)
        if context["item"]["field_contract"]["generation_mode"] == "server_locked":
            raise ValidationError("服务器事实字段不能手动覆盖")
        _validate_text(context, text)
        fields = preparation.field_values.filter(unit_key=unit_key, field_key=field_key)
        existing = fields.filter(revision=revision).first()
        if existing:
            _verify_value(context, existing)
            if existing.text != text or existing.origin_kind != "manual":
                _conflict("同一字段修订已有不同内容或来源")
            return existing  # Same explicit revision is an idempotent receipt.
        _editable(preparation)
        _revision(preparation.revision, expected_preparation_revision, "expected_preparation_revision")
        if revision != (fields.aggregate(latest=Max("revision"))["latest"] or 0) + 1:
            _conflict("新字段修订必须紧接当前修订，不能覆盖或跳号")
        value = ExecutionFieldValue.objects.create(
            preparation=preparation, unit_key=unit_key, field_key=field_key, revision=revision,
            text=text, text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            source_sha256=context["source_sha256"], field_contract_sha256=context["field_contract_sha256"],
            origin_kind="manual", status="pending", created_by=actor,
        )
        _touch(preparation)
        return value


@security_boundary_resolution_cache()
def review_execution_field(*, preparation_id, field_value_id, approve, expected_text_sha256, expected_source_sha256,
                           expected_preparation_revision, actor):
    if type(approve) is not bool:
        raise ValidationError({"approve": "必须明确确认或拒绝"})
    with _locked_preparations([preparation_id], actor) as rows:
        preparation = rows[str(preparation_id)]
        value = preparation.field_values.select_for_update().get(pk=field_value_id)
        if value.text_sha256 != expected_text_sha256 or value.source_sha256 != expected_source_sha256:
            _conflict("提交复核的正文或来源摘要已不匹配")
        context = _field_context(preparation, value.unit_key, value.field_key)
        _verify_value(context, value, validate_text=approve)
        target = "confirmed" if approve else "rejected"
        if value.status == target:
            return value
        _editable(preparation)
        _revision(preparation.revision, expected_preparation_revision, "expected_preparation_revision")
        if value.status != "pending":
            _conflict("字段已复核，不能改写原批准事实")
        value.status = target
        value.approval_kind = "human" if approve else None
        value.reviewed_by, value.reviewed_at = actor, timezone.now()
        value.save(update_fields=("status", "approval_kind", "reviewed_by", "reviewed_at", "updated_at"))
        _touch(preparation)
        return value


@security_boundary_resolution_cache()
def select_execution_field(*, preparation_id, source_preparation_id, field_value_id, expected_preparation_revision,
                           expected_selection_revision, replace, actor):
    if type(replace) is not bool:
        raise ValidationError({"replace": "必须明确是否替换已采用修订"})
    with _locked_preparations([preparation_id, source_preparation_id], actor) as rows:
        preparation, source = rows[str(preparation_id)], rows[str(source_preparation_id)]
        _verify_reuse_identity(preparation, source)
        value = source.field_values.select_for_update().get(pk=field_value_id)
        if value.status != "confirmed":
            _conflict("只能采用明确已确认的字段修订")
        context = _field_context(preparation, value.unit_key, value.field_key)
        _verify_value(context, value)
        chosen = preparation.field_selections.select_for_update().filter(unit_key=value.unit_key, field_key=value.field_key).first()
        if chosen and chosen.field_value_id == value.pk:
            return chosen
        _editable(preparation)
        _revision(preparation.revision, expected_preparation_revision, "expected_preparation_revision")
        if chosen:
            if not replace:
                _conflict("已有采用修订，必须明确替换")
            _revision(chosen.revision, expected_selection_revision, "expected_selection_revision")
            chosen.field_value, chosen.revision = value, chosen.revision + 1
            chosen.selected_by, chosen.selected_at = actor, timezone.now()
            chosen.save()
        else:
            if replace or expected_selection_revision is not None:
                _conflict("此字段尚无采用记录，请按首次选择提交")
            chosen = ExecutionFieldSelection.objects.create(
                preparation=preparation, unit_key=value.unit_key, field_key=value.field_key, field_value=value,
                revision=1, selected_by=actor, selected_at=timezone.now(),
            )
        _touch(preparation)
        return chosen


@security_boundary_resolution_cache()
def confirm_execution_field(*, preparation_id, field_value_id, expected_preparation_revision,
        expected_text_sha256, expected_source_sha256, select_for_execution, expected_selection_revision, replace, actor):
    """Atomically confirm and optionally select; never finalize or dispatch.

    A failed selection rolls back a new confirmation. A replay can ignore an
    old preparation revision only when that exact value is already confirmed
    AND selected, not when confirmation alone happened on an earlier request.
    """
    if type(select_for_execution) is not bool or type(replace) is not bool:
        raise ValidationError("必须明确提供确认/采用标志")
    if not select_for_execution and (replace or expected_selection_revision is not None):
        raise ValidationError("仅确认字段不能同时修改采用记录")
    with _locked_preparations([preparation_id], actor) as rows:
        preparation = rows[str(preparation_id)]
        if select_for_execution:
            value = preparation.field_values.get(pk=field_value_id)
            choice = preparation.field_selections.filter(unit_key=value.unit_key, field_key=value.field_key).first()
            if value.status != "confirmed" or choice is None or choice.field_value_id != value.pk:
                _revision(preparation.revision, expected_preparation_revision, "expected_preparation_revision")
        value = review_execution_field(preparation_id=preparation.pk, field_value_id=field_value_id, approve=True,
            expected_text_sha256=expected_text_sha256, expected_source_sha256=expected_source_sha256,
            expected_preparation_revision=expected_preparation_revision, actor=actor)
        choice = None
        if select_for_execution:
            preparation.refresh_from_db()
            choice = select_execution_field(preparation_id=preparation.pk, source_preparation_id=preparation.pk,
                field_value_id=value.pk, expected_preparation_revision=preparation.revision,
                expected_selection_revision=expected_selection_revision, replace=replace, actor=actor)
        return value, choice


@contextmanager
def locked_execution_field_selection_snapshot(*, preparation_id, expected_preparation_revision, actor):
    """Hold all source roots, preparations and selected fields in stable order.

    Finalization uses this context to retain the verified choice locks through
    its commit. Callers must not already hold a subset of the source roots.
    """
    observed = list(ExecutionFieldSelection.objects.filter(preparation_id=preparation_id).values_list(
        "id", "revision", "field_value_id", "field_value__preparation_id"))
    parent_ids = {str(preparation_id), *(str(row[3]) for row in observed)}
    with _locked_preparations(parent_ids, actor) as preparations:
        preparation = preparations[str(preparation_id)]
        _revision(preparation.revision, expected_preparation_revision, "expected_preparation_revision")
        selections = list(preparation.field_selections.select_for_update().order_by("id"))
        values = {row.pk: row for row in ExecutionFieldValue.objects.select_for_update().filter(
            pk__in=[row.field_value_id for row in selections]).order_by("id")}
        current = {(row.pk, row.revision, row.field_value_id, values[row.field_value_id].preparation_id) for row in selections}
        if current != set(observed):
            _conflict("字段采用记录在读取期间变化，请重新读取")
        contexts = _field_contexts(preparation)
        # The original appearance declaration makes this particular use
        # necessary even when the reusable Profile rule is generally optional.
        # _field_contexts verified the frozen contract; do not resolve a new
        # registry definition under these preparation/field locks.
        target = preparation.contract.resolved_inputs.get("media_generation_target")
        appearance = target.get("appearance") if isinstance(target, dict) else None
        difference = appearance.get("difference") if isinstance(appearance, dict) else None
        difference_field = None
        if isinstance(difference, dict) and difference.get("kind") == "reference_field":
            difference_field = (difference.get("unit_key"), difference.get("field_key"))
            if difference_field not in contexts:
                _conflict("变体差异缺少原单元的确切字段准备")
        fields, selected = [], set()
        for choice in selections:
            value = values[choice.field_value_id]
            source = preparations[str(value.preparation_id)]
            _verify_reuse_identity(preparation, source)
            identity = (choice.unit_key, choice.field_key)
            if identity != (value.unit_key, value.field_key) or identity not in contexts or identity in selected:
                _conflict("已采用字段与本次单元或规则不一致")
            if value.status != "confirmed":
                _conflict("已采用字段不是明确确认的修订")
            context = contexts[identity]
            _verify_value(context, value)
            rule = context["item"]["field_contract"]
            fields.append({
                "unit_key": value.unit_key, "field_key": value.field_key,
                "selection_id": str(choice.pk), "selection_revision": choice.revision,
                "field_value_id": str(value.pk), "field_revision": value.revision,
                "source_preparation_id": str(value.preparation_id),
                "source_sha256": value.source_sha256, "field_contract_sha256": value.field_contract_sha256,
                "text": value.text, "text_sha256": value.text_sha256,
                "generation_output_semantics": rule["generation_output_semantics"],
                "generation_source_selector": rule["generation_source_selector"],
                "origin": {"kind": value.origin_kind,
                    "attempt_id": str(value.origin_attempt_id) if value.origin_attempt_id else None,
                    "asset_job_id": str(value.origin_asset_job_id) if value.origin_asset_job_id else None},
                "approval": {"kind": value.approval_kind, "policy_sha256": value.approval_policy_sha256,
                    "reviewed_by": str(value.reviewed_by_id) if value.reviewed_by_id else None,
                    "reviewed_at": value.reviewed_at.isoformat() if value.reviewed_at else None},
                "selected_by": str(choice.selected_by_id), "selected_at": choice.selected_at.isoformat(),
            })
            selected.add(identity)
        missing = []
        for identity, context in contexts.items():
            if identity not in selected:
                rule = context["item"]["field_contract"]
                missing.append({"unit_key": identity[0], "field_key": identity[1],
                    "required": rule["required"] or rule["generation_required"] or identity == difference_field})
        snapshot = json_contract({"schema_version": 1, "preparation_id": str(preparation.pk),
            "preparation_revision": preparation.revision, "fields": sorted(fields, key=lambda row: (row["unit_key"], row["field_key"])),
            "unselected_fields": sorted(missing, key=lambda row: (row["unit_key"], row["field_key"]))},
            max_bytes=execution_configuration_max_bytes())
        yield preparation, snapshot


@security_boundary_resolution_cache()
def read_execution_field_selection_snapshot(*, preparation_id, expected_preparation_revision, actor):
    """Read explicit choices without finalizing or selecting newer revisions."""
    with locked_execution_field_selection_snapshot(preparation_id=preparation_id,
            expected_preparation_revision=expected_preparation_revision, actor=actor) as (_preparation, snapshot):
        return snapshot
