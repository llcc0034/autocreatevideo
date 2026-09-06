import hashlib
import json

from django.db import migrations
from django.utils import timezone


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


SECURITY_DEFINITIONS = {
    "security.provider.max_response_bytes": (1_048_576, "bytes", "Provider 响应正文最大字节数 / Maximum provider response body bytes"),
    "security.provider.async_rest_timeout_max_seconds": (600.0, "seconds", "异步 REST 单次请求超时绝对上限 / Absolute async REST request timeout"),
    "security.provider.async_rest_poll_interval_max_seconds": (300.0, "seconds", "异步 REST 轮询间隔绝对上限 / Absolute async REST poll interval"),
    "security.provider.async_rest_max_poll_seconds": (604_800, "seconds", "异步 REST 总轮询时长绝对上限 / Absolute async REST polling duration"),
    "security.provider.smoke_max_poll_seconds": (60, "seconds", "媒体 Smoke 总轮询时长绝对上限 / Absolute media smoke polling duration"),
    "security.provider.smoke_max_poll_interval_seconds": (2.0, "seconds", "媒体 Smoke 轮询间隔绝对上限 / Absolute media smoke poll interval"),
    "security.remote.max_artifacts": (8, "items", "单次远端响应可接收产物数量上限 / Maximum remote artifacts per response"),
    "security.snapshot.stage_max_bytes": (8_388_608, "bytes", "生产阶段快照最大字节数 / Maximum production stage snapshot bytes"),
    "security.snapshot.text_max_bytes": (8_388_608, "bytes", "文本任务快照最大字节数 / Maximum text-task snapshot bytes"),
    "security.prompt.media_max_characters": (60_000, "characters", "媒体提示词绝对字符上限 / Absolute media prompt character limit"),
    "security.prompt.max_usage_count": (1_000_000_000, "units", "Provider 用量计数可接受绝对上限 / Maximum accepted provider usage count"),
    "security.prompt_profile.contract_max_bytes": (65_536, "bytes", "PromptProfile 合同最大字节数 / Maximum PromptProfile contract bytes"),
    "security.prompt_profile.preview_max_bytes": (65_536, "bytes", "PromptProfile 预览最大字节数 / Maximum PromptProfile preview bytes"),
    "security.prompt_normalization.batch_max_items": (100, "items", "提示词规范化单批最大项数 / Maximum prompt-normalization batch items"),
    "security.prompt_normalization.profile_max_rules": (100, "items", "PromptProfile 最大字段规则数 / Maximum PromptProfile field rules"),
    "security.prompt_normalization.profile_max_snapshots": (100, "items", "单次规范化可携带的 Profile 快照数 / Maximum normalization profile snapshots"),
    "security.prompt_normalization.request_max_bytes": (524_288, "bytes", "提示词规范化 Provider 请求最大字节数 / Maximum normalization provider-request bytes"),
    "security.prompt_normalization.text_max_characters": (30_000, "characters", "规范化单字段文本绝对字符上限 / Absolute normalization field text limit"),
    "security.prompt_normalization.output_max_units": (131_072, "tokens", "规范化输出 token 绝对上限 / Absolute normalization output-token limit"),
    "security.prompt_normalization.timeout_max_seconds": (600.0, "seconds", "规范化调用超时绝对上限 / Absolute normalization timeout"),
    "security.prompt_normalization.chat_options_max_bytes": (4_096, "bytes", "Chat 选项最大字节数 / Maximum chat-options bytes"),
    "security.prompt_normalization.chat_options_max_depth": (4, "levels", "Chat 选项最大嵌套深度 / Maximum chat-options depth"),
    "security.prompt_normalization.chat_options_max_fields": (32, "fields", "Chat 选项最大字段数 / Maximum chat-options fields"),
    "security.prompt_normalization.chat_options_max_array_items": (32, "items", "Chat 选项数组最大项数 / Maximum chat-options array items"),
    "security.prompt_normalization.chat_option_string_max_characters": (1_024, "characters", "Chat 选项字符串最大字符数 / Maximum chat-option string characters"),
    "security.comfy.client.saved_graph_max_bytes": (5_242_880, "bytes", "ComfyUI 已保存图最大字节数 / Maximum saved ComfyUI graph bytes"),
    "security.comfy.client.list_response_max_bytes": (1_048_576, "bytes", "ComfyUI 列表响应最大字节数 / Maximum ComfyUI list response bytes"),
    "security.comfy.client.list_max_items": (2_000, "items", "ComfyUI 普通列表最大项数 / Maximum ComfyUI list items"),
    "security.comfy.client.text_response_max_bytes": (65_536, "bytes", "ComfyUI 文本响应最大字节数 / Maximum ComfyUI text response bytes"),
    "security.comfy.client.object_info_max_bytes": (33_554_432, "bytes", "ComfyUI object_info 最大字节数 / Maximum ComfyUI object_info bytes"),
    "security.comfy.client.object_info_max_depth": (32, "levels", "ComfyUI object_info 最大嵌套深度 / Maximum ComfyUI object_info depth"),
    "security.comfy.client.object_info_max_entries": (250_000, "entries", "ComfyUI object_info 最大条目数 / Maximum ComfyUI object_info entries"),
    "security.comfy.client.model_list_max_bytes": (8_388_608, "bytes", "ComfyUI 模型列表最大字节数 / Maximum ComfyUI model-list bytes"),
    "security.comfy.client.model_list_max_items": (10_000, "items", "ComfyUI 模型列表最大项数 / Maximum ComfyUI model-list items"),
    "security.comfy.canvas.max_nodes": (500, "nodes", "ComfyUI 画布最大节点数 / Maximum ComfyUI canvas nodes"),
    "security.comfy.canvas.max_inputs_per_node": (128, "inputs", "ComfyUI 单节点最大输入数 / Maximum inputs per ComfyUI node"),
    "security.comfy.canvas.max_outputs_per_node": (64, "outputs", "ComfyUI 单节点最大输出数 / Maximum outputs per ComfyUI node"),
    "security.comfy.canvas.max_subgraph_depth": (8, "levels", "ComfyUI 子图最大深度 / Maximum ComfyUI subgraph depth"),
    "security.comfy.contract.max_issues": (40, "items", "ComfyUI 合同一次返回的最大问题数 / Maximum ComfyUI contract issues"),
    "security.comfy.contract.max_autogrow_inputs": (128, "inputs", "ComfyUI 自动扩展输入绝对上限 / Maximum ComfyUI autogrow inputs"),
    "security.comfy.contract.max_dynamic_widgets": (64, "widgets", "ComfyUI 动态组件绝对上限 / Maximum ComfyUI dynamic widgets"),
    "security.comfy.import.scan_max_items": (250, "items", "已保存工作流单次扫描上限 / Maximum saved-workflow scan items"),
    "security.comfy.import.batch_max_items": (20, "items", "已保存工作流单批导入上限 / Maximum saved-workflow import batch"),
    "security.comfy.import.request_max_bytes": (67_108_864, "bytes", "已保存工作流导入请求最大字节数 / Maximum saved-workflow import request bytes"),
    "security.comfy.import.scan_max_seconds": (30.0, "seconds", "已保存工作流扫描时长上限 / Maximum saved-workflow scan duration"),
    "security.comfy.import.batch_max_seconds": (60.0, "seconds", "已保存工作流单批处理时长上限 / Maximum saved-workflow batch duration"),
    "security.workflow_smoke.asset_max_items": (100, "items", "工作流 Smoke 候选素材最大项数 / Maximum workflow-smoke asset items"),
    "security.workflow_smoke.asset_max_file_bytes": (268_435_456, "bytes", "工作流 Smoke 单素材最大字节数 / Maximum workflow-smoke asset bytes"),
    "security.workflow_smoke.asset_max_total_bytes": (536_870_912, "bytes", "工作流 Smoke 素材总字节上限 / Maximum workflow-smoke total asset bytes"),
    "security.workflow_smoke.asset_scan_max_seconds": (5.0, "seconds", "工作流 Smoke 素材扫描时长上限 / Maximum workflow-smoke asset scan duration"),
    "security.managed_value.max_plaintext_bytes": (65_536, "bytes", "受管秘密正文最大字节数 / Maximum managed-secret bytes"),
    "security.script.docx_max_uncompressed_bytes": (20_971_520, "bytes", "DOCX 解压后最大字节数 / Maximum uncompressed DOCX bytes"),
    "security.script.docx_max_members": (1_000, "items", "DOCX 压缩包最大成员数 / Maximum DOCX archive members"),
    "security.reference.max_bindings": (64, "items", "单镜头结构参考绑定绝对上限 / Maximum structural reference bindings"),
    "security.release.max_shots": (10_000, "shots", "单发布包镜头绝对上限 / Maximum shots per release bundle"),
    "security.media.max_duration_ms": (86_400_000, "milliseconds", "单媒体时长绝对上限 / Absolute media duration limit"),
    "security.media.max_fps": (480, "fps", "媒体帧率绝对上限 / Absolute media frame-rate limit"),
    "security.media.max_dimension": (65_536, "pixels", "媒体单边尺寸绝对上限 / Absolute media dimension limit"),
    "security.execution.max_idle_timeout_seconds": (86_400, "seconds", "执行空闲超时绝对上限 / Absolute execution idle-timeout limit"),
    "security.execution.max_type_overrides": (128, "items", "执行监测类型覆盖最大项数 / Maximum execution-monitoring type overrides"),
    "security.execution.max_node_overrides": (256, "items", "执行监测节点覆盖最大项数 / Maximum execution-monitoring node overrides"),
    "security.artifact.comparison_parameters_max_bytes": (16_384, "bytes", "产物比较参数最大字节数 / Maximum artifact comparison-parameter bytes"),
    "security.deferred.provisional_prompt_max_characters": (65_536, "characters", "延迟生成临时提示词绝对上限 / Absolute deferred provisional-prompt limit"),
    "security.production.max_automatic_retries": (10, "retries", "任何自动修复或重试的绝对次数上限 / Absolute automatic retry limit"),
    "security.production.max_generation_tasks": (1_000, "tasks", "单次生产运行生成任务绝对上限 / Absolute generation-task limit"),
    "security.production.max_concurrency": (64, "workers", "生产并发绝对上限 / Absolute production concurrency limit"),
    "security.runtime.max_timeout_seconds": (86_400, "seconds", "通用运行超时绝对上限 / Absolute runtime timeout limit"),
    "security.runtime.max_interval_seconds": (3_600, "seconds", "运行轮询与宽限间隔绝对上限 / Absolute runtime interval limit"),
    "security.runtime.max_capability_ttl_seconds": (86_400, "seconds", "能力事实有效期绝对上限 / Absolute capability TTL limit"),
    "security.workflow.max_graph_bytes": (65_536, "bytes", "工作流执行图最大字节数 / Maximum workflow graph bytes"),
    "security.script.max_upload_bytes": (10_485_760, "bytes", "剧本上传最大字节数 / Maximum script upload bytes"),
    "security.artifact.max_bytes": (8_589_934_592, "bytes", "单产物最大字节数 / Maximum artifact bytes"),
    "security.workflow.smoke_max_seconds": (86_400, "seconds", "工作流 Smoke 总时长绝对上限 / Absolute workflow-smoke duration"),
    "security.api.max_page_size": (500, "items", "同步列表 API 单页绝对上限 / Absolute API page-size limit"),
}


RECLASSIFIED_SECURITY_KEYS = {
    "access.public_registration_enabled",
    "access.throttle_anon_rate",
    "access.throttle_asset_upload_rate",
    "access.throttle_registration_rate",
    "access.throttle_signed_media_rate",
    "access.throttle_user_rate",
    "artifact.audio_upload_max_bytes",
    "artifact.image_max_height",
    "artifact.image_max_pixels",
    "artifact.image_max_width",
    "artifact.signed_url_ttl_seconds",
    "artifact.tenant_quota_bytes",
    "artifact.upload_max_bytes",
    "artifact.video_upload_max_bytes",
}


CEILING_LINKS = {
    "artifact.max_bytes": "security.artifact.max_bytes",
    "delivery.assembly_timeout_seconds": "security.runtime.max_timeout_seconds",
    "production.default_concurrency": "security.production.max_concurrency",
    "production.default_resolution_height": "security.media.max_dimension",
    "production.default_resolution_width": "security.media.max_dimension",
    "production.default_shot_duration_ms": "security.media.max_duration_ms",
    "production.max_generation_tasks": "security.production.max_generation_tasks",
    "production.prompt_field_retry_limit": "security.production.max_automatic_retries",
    "production.task_max_attempts": "security.production.max_automatic_retries",
    "production.task_timeout_seconds": "security.runtime.max_timeout_seconds",
    "production.text_contract_retry_limit": "security.production.max_automatic_retries",
    "production.video_shot_max_duration_ms": "security.media.max_duration_ms",
    "production.video_shot_min_duration_ms": "security.media.max_duration_ms",
    "runtime.capability_ttl_seconds": "security.runtime.max_capability_ttl_seconds",
    "runtime.task_cancel_grace_seconds": "security.runtime.max_interval_seconds",
    "runtime.task_health_check_interval_seconds": "security.runtime.max_interval_seconds",
    "runtime.task_heartbeat_seconds": "security.runtime.max_interval_seconds",
    "runtime.task_idle_timeout_seconds": "security.execution.max_idle_timeout_seconds",
    "runtime.task_remote_missing_grace_seconds": "security.runtime.max_interval_seconds",
    "runtime.task_remote_poll_seconds": "security.runtime.max_interval_seconds",
    "runtime.task_stale_recovery_interval_seconds": "security.runtime.max_interval_seconds",
    "runtime.task_stall_threshold_seconds": "security.execution.max_idle_timeout_seconds",
    "runtime.task_worker_poll_seconds": "security.runtime.max_interval_seconds",
    "script.max_upload_bytes": "security.script.max_upload_bytes",
    "workflow.max_graph_bytes": "security.workflow.max_graph_bytes",
    "workflow.smoke_hard_timeout_seconds": "security.workflow.smoke_max_seconds",
    "workflow.smoke_idle_timeout_seconds": "security.workflow.smoke_max_seconds",
    "workflow.smoke_timeout_seconds": "security.workflow.smoke_max_seconds",
}


def _definition_defaults(value, unit, description):
    value_type = "number" if type(value) is float else "integer"
    value_schema = {"minimum": 0.001 if value_type == "number" else 1}
    schema_size, schema_sha = _contract(value_schema)
    value_size, value_sha = _contract(value)
    return {
        "domain": "security",
        "configuration_class": "security_boundary",
        "value_type": value_type,
        "description": (
            description
            + "。命中后 fail closed；提高该值会增加资源占用或攻击面。"
        ),
        "unit": unit,
        "impact_modules": ["security", "runtime"],
        "restart_required": False,
        "allowed_scopes": "system",
        "allowed_default_sources": "",
        "is_required": True,
        "is_secret": False,
        "is_active": True,
        "needs_review": False,
        "review_reason": "",
        "value_schema": value_schema,
        "value_schema_version": 1,
        "value_schema_size_bytes": schema_size,
        "value_schema_sha256": schema_sha,
        "initial_value": value,
        "initial_value_schema_version": 1,
        "initial_value_size_bytes": value_size,
        "initial_value_sha256": value_sha,
    }


def configure_security_boundaries(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    ConfigRevision = apps.get_model("studio", "ConfigRevision")
    ConfigValue = apps.get_model("studio", "ConfigValue")

    for key in sorted(RECLASSIFIED_SECURITY_KEYS):
        definition = ConfigDefinition.objects.get(key=key)
        schema = dict(definition.value_schema or {})
        if definition.value_type in {"integer", "number"}:
            schema.pop("maximum", None)
        schema_size, schema_sha = _contract(schema)
        ConfigDefinition.objects.filter(pk=definition.pk).update(
            domain="security",
            configuration_class="security_boundary",
            allowed_scopes="system",
            allowed_default_sources="",
            is_required=True,
            value_schema=schema,
            value_schema_size_bytes=schema_size,
            value_schema_sha256=schema_sha,
        )

    created_definitions = {}
    for key, (value, unit, description) in SECURITY_DEFINITIONS.items():
        defaults = _definition_defaults(value, unit, description)
        definition, created = ConfigDefinition.objects.get_or_create(
            key=key,
            defaults=defaults,
        )
        if not created:
            immutable = (
                "domain", "configuration_class", "value_type", "unit",
                "restart_required", "allowed_scopes", "allowed_default_sources",
                "is_required", "is_secret", "value_schema", "initial_value",
            )
            conflicts = [
                name for name in immutable
                if getattr(definition, name) != defaults[name]
            ]
            if conflicts or not definition.is_active:
                raise RuntimeError(
                    f"0105 安全边界定义 {key} 与既有合同冲突: "
                    + ", ".join(conflicts or ["is_active"]),
                )
        created_definitions[key] = definition

    for key, security_key in CEILING_LINKS.items():
        definition = ConfigDefinition.objects.get(key=key)
        schema = dict(definition.value_schema or {})
        schema.pop("maximum", None)
        schema["security_ceiling_key"] = security_key
        schema_size, schema_sha = _contract(schema)
        ConfigDefinition.objects.filter(pk=definition.pk).update(
            value_schema=schema,
            value_schema_size_bytes=schema_size,
            value_schema_sha256=schema_sha,
        )

    active = ConfigRevision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).order_by("-revision").first()
    existing = {}
    if active:
        existing = {
            row.definition.key: row
            for row in ConfigValue.objects.filter(revision=active).select_related(
                "definition", "credential_version",
            )
        }
    missing_keys = sorted(set(SECURITY_DEFINITIONS) - set(existing))
    if not missing_keys:
        return

    now = timezone.now()
    next_revision = (
        ConfigRevision.objects.filter(scope_type="system")
        .order_by("-revision")
        .values_list("revision", flat=True)
        .first()
        or 0
    ) + 1
    revision = ConfigRevision.objects.create(
        scope_type="system",
        revision=next_revision,
        status="published",
        based_on_revision_id=active.pk if active else None,
        activation_state="inactive",
        change_summary="0105 建立超级管理员专属安全边界并迁移当前运行值",
        published_at=now,
    )
    digest_values = {}
    rows = []
    for key, row in existing.items():
        rows.append(ConfigValue(
            revision=revision,
            definition_id=row.definition_id,
            value=row.value,
            value_schema_version=row.value_schema_version,
            value_size_bytes=row.value_size_bytes,
            value_sha256=row.value_sha256,
            credential_version_id=row.credential_version_id,
        ))
        if row.credential_version_id:
            identity = f"version:{row.credential_version_id}:{row.credential_version.version}"
            digest_values[key] = {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(
                    identity.encode("utf-8"),
                ).hexdigest(),
            }
        else:
            digest_values[key] = row.value
    for key in missing_keys:
        definition = created_definitions[key]
        value = SECURITY_DEFINITIONS[key][0]
        value_size, value_sha = _contract(value)
        rows.append(ConfigValue(
            revision=revision,
            definition=definition,
            value=value,
            value_schema_version=1,
            value_size_bytes=value_size,
            value_sha256=value_sha,
        ))
        digest_values[key] = value
    ConfigValue.objects.bulk_create(rows)
    revision.values_sha256 = hashlib.sha256(_canonical(digest_values)).hexdigest()
    revision.save(update_fields=("values_sha256", "updated_at"))
    if active:
        ConfigRevision.objects.filter(pk=active.pk).update(
            status="superseded",
            activation_state="inactive",
            updated_at=now,
        )
    ConfigRevision.objects.filter(pk=revision.pk).update(
        activation_state="active",
        activated_at=now,
        updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0104_config_definition_security_class")]

    operations = [
        migrations.RunPython(
            configure_security_boundaries,
            migrations.RunPython.noop,
        ),
    ]
