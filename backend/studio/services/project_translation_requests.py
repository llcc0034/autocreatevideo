"""Independent project translation sources on the original text Job/Preparation.

This module never invokes a model or confirms/promotes a translation. Explicit
adoption creates a draft in the existing review flow, not a second result store.
Source rows bind candidates to an exact project field and observed revision.
"""
from copy import deepcopy
import hashlib
import json
import re
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import AssetGenerationJob, CharacterBible, PropBible, SceneBible, Shot, Style, ProjectTranslationRequest, ProjectTranslationSource, PromptFieldTranslation, SeriesProject
from studio.services.asset_generation import create_asset_generation_job, _snapshot
from studio.services.content_contracts import translation_request_max_entries
from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_normalization_text import normalization_request
from studio.services.json_contracts import assign_json_contract, json_contract, verify_json_contract
from studio.services.prompt_translation import (
    build_translation_context, resolve_current_source, source_text_hash, translation_context_hash,
    _protect_translation_contract, _restore_authored_blocks, validate_manual_translation, PromptTranslationError,
    append_translation_history, TRANSLATION_CONTEXT_VERSION,
)
from studio.services.registry_reference_lock import registry_reference_writer


def _project(project_id, actor, *, lock=False):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("需要有效登录身份")
    rows = SeriesProject.objects.all()
    if lock:
        rows = rows.select_for_update()
    if not actor.is_staff:
        rows = rows.filter(owner=actor)
    project = rows.filter(pk=project_id).first()
    if project is None:
        raise PermissionDenied("无权读取该项目的翻译来源")
    return project


def _entries(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= translation_request_max_entries():
        raise ValidationError("请选择当前批次的明确字段，数量不能超过配置范围")
    result, seen = [], set()
    for entry in entries:
        required = {"scope_type", "scope_id", "field_key", "source_text", "expected_entry_id", "expected_revision"}
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValidationError("翻译来源需明确作用域、字段、原文及已读取的候选身份/修订")
        if (entry["scope_type"] not in PromptFieldTranslation.ScopeType.values
                or any(not isinstance(entry[key], str) for key in ("field_key", "source_text"))
                or not entry["source_text"].strip() or type(entry["expected_revision"]) is not int):
            raise ValidationError("翻译来源字段类型无效")
        try:
            if not isinstance(entry["scope_id"], (str, uuid.UUID)) or (entry["expected_entry_id"] is not None
                    and not isinstance(entry["expected_entry_id"], (str, uuid.UUID))):
                raise ValueError()
            scope_id = uuid.UUID(str(entry["scope_id"]))
            entry_id = uuid.UUID(str(entry["expected_entry_id"])) if entry["expected_entry_id"] is not None else None
        except (TypeError, ValueError) as exc:
            raise ValidationError("翻译作用域和候选必须使用真实对象ID") from exc
        if (entry_id is None and entry["expected_revision"] != 0) or (entry_id is not None and entry["expected_revision"] < 1):
            raise ValidationError("尚无候选应明确为null/0，已有候选需提交真实ID和修订")
        identity = (entry["scope_type"], scope_id, entry["field_key"])
        if identity in seen:
            raise ValidationError("同批次不能重复提交同一字段")
        seen.add(identity)
        result.append({**entry, "scope_id": str(scope_id), "expected_entry_id": str(entry_id) if entry_id else None})
    return result


def _assert_current(project, source):
    current = resolve_current_source(project=project, scope_type=source["scope_type"],
        scope_id=source["scope_id"], field_key=source["field_key"])
    if current != source["source_text"]:
        raise ValidationError("原文已变更，请重新读取后创建新候选", code="translation_source_conflict")
    entry = PromptFieldTranslation.objects.filter(project=project, scope_type=source["scope_type"],
        scope_id=source["scope_id"], field_key=source["field_key"]).only("id", "revision").first()
    if ((str(entry.pk) if entry else None) != source["expected_entry_id"]
            or (entry.revision if entry else 0) != source["expected_revision"]):
        raise ValidationError("人工候选已变更，请重新读取；不会覆盖当前译文", code="translation_source_conflict")
    return build_translation_context(project, scope_type=source["scope_type"], scope_id=source["scope_id"],
        field_key=source["field_key"], source_text=current)


def _source_identity(project, source, *, lock=False):
    models = {"shot": Shot, "character": CharacterBible, "scene": SceneBible, "prop": PropBible}
    identity = {"scope_type": source["scope_type"], "scope_id": str(source["scope_id"]), "field_key": source["field_key"]}
    model = models.get(source["scope_type"])
    if model is not None:
        rows = model.objects.filter(pk=source["scope_id"], **({"episode__project": project} if model is Shot else {"project": project}))
        if lock:
            rows = rows.select_for_update()
        obj = rows.only("id", "dialogue").first() if model is Shot else rows.only("id").first()
        if obj is None:
            raise ValidationError("翻译字段不存在或不属于当前项目")
        match = re.fullmatch(r"shot\.dialogue\.([0-9]+)\.delivery", source["field_key"])
        if match:
            index = int(match.group(1))
            if not isinstance(obj.dialogue, list) or index >= len(obj.dialogue) or not isinstance(obj.dialogue[index], dict):
                raise ValidationError("对白位置已变更")
            dialogue = obj.dialogue[index]
            if not dialogue.get("speaker_id") or not dialogue.get("text"):
                raise ValidationError("对白身份缺少说话人或原文，不能猜测翻译所属对象")
            identity["dialogue_identity"] = {"source_order": index, "speaker_id": str(dialogue.get("speaker_id") or ""),
                "text_sha256": source_text_hash(str(dialogue.get("text") or "")), "voiceover": bool(dialogue.get("voiceover"))}
    elif lock and source["field_key"].startswith("style.") and project.style_id:
        Style.objects.select_for_update().get(pk=project.style_id)
    return identity


def translation_input_key(definition):
    if definition.value["configuration"]["operation"] != "prompt.normalize":
        raise ValidationError("请选择独立翻译能力的精确执行实例，不使用生产字段辅助实例")
    # Mapping is configured on the selected instance, not a fixed input name.
    rows = [row for row in definition.value["configuration"]["inputs"] if any(
        target["target_kind"] == "operation_input" and target["target_key"] == "items" for target in row["targets"])]
    if len(rows) != 1 or rows[0]["kind"] != "json":
        raise ValidationError("所选实例必须明确配置唯一的翻译条目JSON输入")
    return rows[0]["key"]


@registry_reference_writer
def create_project_translation_request(*, project_id, entries, source_lang, target_lang, actor,
        execution_instance_version_id, expected_definition_sha256, request, field_values,
        allow_external_calls, client_idempotency_key):
    project = _project(project_id, actor)
    get_user_model().objects.select_for_update().get(pk=actor.pk)
    project = _project(project_id, actor, lock=True)
    sources = _entries(entries)
    # This surface creates the project's existing English review overlays. The
    # general native protocol remains language-neutral; no language is defaulted.
    if source_lang != "Chinese" or target_lang != "English":
        raise ValidationError("当前项目翻译审核为中文原文/英文候选，请明确选择语言")
    if (not isinstance(request, dict) or set(request) != {"inputs", "parameters", "manual_channels", "subject_facts", "source_language", "target_language"}
            or not isinstance(request["inputs"], dict)):
        raise ValidationError("需要完整的共同执行请求；条目来源由服务器填入")
    if (not isinstance(client_idempotency_key, str) or not client_idempotency_key.strip()
            or len(client_idempotency_key) > 191):
        raise ValidationError("需要1至191字符的稳定幂等键")
    intent = json_contract({"schema_version": 1, "project_id": str(project.pk), "entries": sources,
        "source_lang": source_lang, "target_lang": target_lang, "request": request, "field_values": field_values,
        "execution_instance_version_id": str(execution_instance_version_id), "definition_sha256": expected_definition_sha256,
        "allow_external_calls": allow_external_calls},
        max_bytes=execution_configuration_max_bytes())
    key = f"asset-generation:{actor.pk}:{hashlib.sha256(client_idempotency_key.encode('utf-8')).hexdigest()}"
    existing = AssetGenerationJob.objects.filter(idempotency_key=key).only("id").first()
    if existing is not None:
        saved = ProjectTranslationRequest.objects.filter(job=existing, project=project).first()
        if saved is None or saved.request_sha256 != intent.sha256:
            raise ValidationError("幂等键已用于不同请求", code="translation_request_conflict")
        from studio.services.execution_preparations import read_execution_preparation_detail
        read_execution_preparation_detail(existing.execution_preparations.get())
        return saved, False
    definition = verify_current_execution_definition(stored_execution_contract(execution_instance_version_id))
    if definition.sha256 != expected_definition_sha256:
        raise ValidationError("执行实例定义已变化，请重新读取")
    input_key = translation_input_key(definition)
    if input_key in request["inputs"]:
        raise ValidationError("翻译条目来自所选项目字段，不能注入另一份模型输入")
    items, literals = [], []
    for position, source in enumerate(sources):
        identity = _source_identity(project, source, lock=True)
        context = _assert_current(project, source)
        protected, blocks = _protect_translation_contract(source["source_text"], context)
        item = {"request_id": str(position), "text": protected, "source_lang": source_lang, "target_lang": target_lang,
            "field_key": source["field_key"], "label": "", "context": context, "protected_tokens": list(blocks)}
        items.append(item)
        literals.append((blocks, context, identity))
    normalization_request(items)
    prepared = deepcopy(request)
    prepared["inputs"][input_key] = {"kind": "literal", "value": items}
    job, _ = create_asset_generation_job(owner=actor, execution_instance_version_id=execution_instance_version_id,
        expected_definition_sha256=expected_definition_sha256, job_type="prompt", media_type="text", **prepared,
        field_values=field_values, allow_external_calls=allow_external_calls,
        client_idempotency_key=client_idempotency_key)
    saved = ProjectTranslationRequest.objects.create(project=project, job=job, request_sha256=intent.sha256,
        source_lang=source_lang, target_lang=target_lang)
    for position, source in enumerate(sources):
        blocks, context, identity = literals[position]
        row = ProjectTranslationSource(request=saved, position=position, **source,
            source_sha256=source_text_hash(source["source_text"]), context_sha256=translation_context_hash(context),
            item_sha256=json_contract(items[position], max_bytes=execution_configuration_max_bytes()).sha256)
        assign_json_contract(row, "literals", {"schema_version": 1, "blocks": blocks, "source_identity": identity}, max_bytes=execution_configuration_max_bytes())
        row.save()
    return saved, True


def read_project_translation_candidate(*, request_id, source_id, actor):
    saved = ProjectTranslationRequest.objects.select_related("job").get(pk=request_id)
    project = _project(saved.project_id, actor)
    if actor.pk != saved.job.owner_id and not actor.is_staff:
        raise PermissionDenied("无权读取该翻译调用")
    source = saved.sources.get(pk=source_id)
    if saved.job.status != "succeeded" or saved.job.ignore_late_result:
        raise ValidationError("原翻译任务尚未成功，不存在可采用候选")
    execution = _snapshot(saved.job)
    transport = execution.transport.value
    if (saved.job.operation != "prompt.normalize" or saved.job.job_type != "prompt"
            or transport["output"]["adapter"] != "text.prompt_normalize/v1"):
        raise ValidationError("翻译结果不属于本次独立翻译合同")
    try:
        item = transport["operation_plan"]["plan"]["request"]["items"][source.position]
        normalization_request([item])
    except (KeyError, TypeError, IndexError, ValidationError) as exc:
        raise ValidationError("原翻译条目位置与冻结请求不可核验") from exc
    if (item["request_id"] != str(source.position)
            or json_contract(item, max_bytes=execution_configuration_max_bytes()).sha256 != source.item_sha256
            or source_text_hash(source.source_text) != source.source_sha256):
        raise ValidationError("原文与本次实际模型输入不可核验")
    encoded = saved.job.result_text.encode("utf-8")
    if len(encoded) != saved.job.result_text_size_bytes or hashlib.sha256(encoded).hexdigest() != saved.job.result_text_sha256:
        raise ValidationError("原任务候选正文摘要不一致")
    try:
        result = json.loads(saved.job.result_text)
        rows = result["items"]
        if set(result) != {"schema_version", "items"} or type(result["schema_version"]) is not int or result["schema_version"] != 1:
            raise ValueError()
        if (not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) != {"request_id", "translation"}
                or not isinstance(row["request_id"], str) or not isinstance(row["translation"], str) for row in rows)
                or len(rows) != saved.sources.count()
                or {row["request_id"] for row in rows} != {str(position) for position in range(len(rows))}):
            raise ValueError()
        candidate = next(row["translation"] for row in rows if row["request_id"] == str(source.position))
        if not isinstance(candidate, str):
            raise ValueError()
    except (TypeError, KeyError, ValueError, StopIteration) as exc:
        raise ValidationError("原翻译候选结构不可核验") from exc
    literals = verify_json_contract(source.literals, schema_version=source.literals_schema_version,
        size_bytes=source.literals_size_bytes, sha256=source.literals_sha256, max_bytes=execution_configuration_max_bytes()).value
    if (not isinstance(literals, dict) or set(literals) != {"schema_version", "blocks", "source_identity"}
            or type(literals["schema_version"]) is not int or literals["schema_version"] != 1
            or not isinstance(literals["blocks"], dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in literals["blocks"].items())
            or list(literals["blocks"]) != item["protected_tokens"]):
        raise ValidationError("原文保护快照不可核验")
    restored = validate_manual_translation(source.source_text, _restore_authored_blocks(candidate, literals["blocks"]))
    # Read-only candidate viewing remains possible after the source changes;
    # adoption must compare these identities again under the original lock.
    target = {"scope_type": source.scope_type, "scope_id": str(source.scope_id), "field_key": source.field_key,
        "source_text": source.source_text, "expected_entry_id": str(source.expected_entry_id) if source.expected_entry_id else None,
        "expected_revision": source.expected_revision}
    current, reason = True, ""
    try:
        if _source_identity(project, target) != literals["source_identity"]:
            raise ValidationError("字段所属对象或对白身份已变化，不能采用旧候选")
        context = _assert_current(project, target)
        if translation_context_hash(context) != source.context_sha256:
            raise ValidationError("来源说明或关联术语已变化，需要重新生成或人工审核")
    except (ValidationError, PromptTranslationError) as exc:
        current, reason = False, "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
    return {"source": source, "text": restored, "current": current, "reason": reason,
        "identity": literals["source_identity"], "execution": execution}


@transaction.atomic
def adopt_project_translation_candidate(*, request_id, source_id, actor):
    saved = ProjectTranslationRequest.objects.get(pk=request_id)
    project = _project(saved.project_id, actor, lock=True)
    if actor.pk != saved.job.owner_id and not actor.is_staff:
        raise PermissionDenied("无权采用该翻译调用")
    source = saved.sources.select_for_update().get(pk=source_id)
    # Receipt replay never overwrites a later edit or asks for another model run.
    if source.adopted_entry_id is not None:
        return source, False
    target = {"scope_type": source.scope_type, "scope_id": str(source.scope_id), "field_key": source.field_key}
    _source_identity(project, target, lock=True)
    entry = PromptFieldTranslation.objects.select_for_update().filter(project=project, **target).first()
    value = read_project_translation_candidate(request_id=saved.pk, source_id=source.pk, actor=actor)
    if not value["current"]:
        raise ValidationError(value["reason"], code="translation_source_conflict")
    if entry is None:
        entry = PromptFieldTranslation(project=project, **target, revision=1)
    else:
        append_translation_history(entry, event="adopt_translation_candidate", actor=actor)
        entry.revision += 1
    entry.source_text, entry.source_hash = source.source_text, source.source_sha256
    entry.translated_text, entry.status = value["text"], PromptFieldTranslation.Status.DRAFT_TRANSLATION
    entry.provider = value["execution"].transport.value["driver_type"]
    entry.model = value["execution"].definition.value["target"]["model"]["identifier"]
    entry.error_message = ""
    entry.origin_generation_attempt = None
    entry.runtime_metadata = {"asset_generation_job_id": str(saved.job_id), "translation_source_id": str(source.pk),
        "translation_context_hash": source.context_sha256, "translation_context_version": TRANSLATION_CONTEXT_VERSION}
    if "dialogue_identity" in value["identity"]:
        entry.runtime_metadata["dialogue_identity"] = value["identity"]["dialogue_identity"]
    entry.confirmed_at, entry.confirmed_by = None, None
    # Existing review/history is preserved but cannot grow beyond the governed
    # JSON ceiling. A rejected adoption leaves the original Job result intact.
    json_contract(entry.revision_history, max_bytes=execution_configuration_max_bytes())
    json_contract(entry.runtime_metadata, max_bytes=execution_configuration_max_bytes())
    entry.save()
    source.adopted_entry, source.adopted_revision = entry, entry.revision
    source.adopted_by, source.adopted_at = actor, timezone.now()
    source.save(update_fields=("adopted_entry", "adopted_revision", "adopted_by", "adopted_at", "updated_at"))
    return source, True


def copy_translation_sources_for_retry(source_job, retry_job):
    """Original Job retry reuses the same frozen source facts, never current data."""
    saved = ProjectTranslationRequest.objects.filter(job=source_job).first()
    if saved is None:
        return
    if not transaction.get_connection().in_atomic_block or retry_job.retry_of_id != source_job.pk:
        raise ValidationError("翻译来源只能在原任务明确重试事务中复制")
    copied = ProjectTranslationRequest.objects.create(project_id=saved.project_id, job=retry_job,
        request_sha256=saved.request_sha256, source_lang=saved.source_lang, target_lang=saved.target_lang)
    names = ("position", "scope_type", "scope_id", "field_key", "expected_entry_id", "expected_revision",
        "source_text", "source_sha256", "context_sha256", "item_sha256", "literals", "literals_schema_version", "literals_size_bytes", "literals_sha256")
    for source in saved.sources.all():
        ProjectTranslationSource.objects.create(request=copied, **{name: deepcopy(getattr(source, name)) for name in names})
