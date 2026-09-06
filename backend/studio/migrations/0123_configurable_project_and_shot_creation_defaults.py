import hashlib
import json

from django.db import migrations, models
from django.utils import timezone


POLICY_KEY = "production.project_creation_policy"


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


def _policy_v2(value):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError("0123 只能从已发布的 project_creation_policy v1 升级")
    required_v1 = {
        "schema_version",
        "width",
        "height",
        "fps",
        "default_shot_duration_ms",
        "default_episode_count",
        "default_shots_per_episode",
        "default_episode_duration_seconds",
        "default_max_generation_tasks",
        "continuity_seed_policy",
    }
    if set(value) != required_v1:
        raise RuntimeError("0123 project_creation_policy v1 字段与权威合同不一致")
    return {
        "schema_version": 2,
        "default_language": "Chinese",
        "default_aspect_ratio": "9:16",
        "aspect_ratio_options": [
            {"value": "9:16", "label": "9:16 · 竖屏短剧"},
            {"value": "16:9", "label": "16:9 · 横屏影视"},
            {"value": "1:1", "label": "1:1 · 方形内容"},
        ],
        "fps": value["fps"],
        "default_shot_media_type": "video",
        "default_episode_count": value["default_episode_count"],
        "default_shots_per_episode": value["default_shots_per_episode"],
        "default_episode_duration_seconds": value[
            "default_episode_duration_seconds"
        ],
        "default_max_generation_tasks": value["default_max_generation_tasks"],
        "continuity_seed_policy": value["continuity_seed_policy"],
    }


POLICY_SCHEMA_V2 = {
    "type": "object",
    "required": [
        "aspect_ratio_options",
        "continuity_seed_policy",
        "default_aspect_ratio",
        "default_episode_count",
        "default_episode_duration_seconds",
        "default_language",
        "default_max_generation_tasks",
        "default_shot_media_type",
        "default_shots_per_episode",
        "fps",
        "schema_version",
    ],
    "additionalProperties": False,
    "closed_contract": True,
    "properties": {
        "schema_version": {"type": "integer", "enum": [2]},
        "default_language": {"type": "string", "minLength": 1, "maxLength": 32},
        "default_aspect_ratio": {
            "type": "string",
            "pattern": "^[1-9][0-9]{0,3}:[1-9][0-9]{0,3}$",
            "maxLength": 16,
        },
        "aspect_ratio_options": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["label", "value"],
                "additionalProperties": False,
                "properties": {
                    "value": {
                        "type": "string",
                        "pattern": "^[1-9][0-9]{0,3}:[1-9][0-9]{0,3}$",
                        "maxLength": 16,
                    },
                    "label": {"type": "string", "minLength": 1, "maxLength": 80},
                },
            },
        },
        "fps": {"type": "integer", "minimum": 1},
        "default_shot_media_type": {
            "type": "string",
            "enum": ["video", "image"],
        },
        "default_episode_count": {"type": "integer", "minimum": 1},
        "default_shots_per_episode": {"type": "integer", "minimum": 1},
        "default_episode_duration_seconds": {"type": "integer", "minimum": 1},
        "default_max_generation_tasks": {"type": "integer", "minimum": 1},
        "continuity_seed_policy": {
            "type": "object",
            "required": ["mode", "value"],
            "additionalProperties": False,
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["random_per_project", "explicit"],
                },
                "value": {"type": "integer", "minimum": 1},
            },
        },
    },
    "security_ceiling_paths": {
        "fps": "security.media.max_fps",
        "default_episode_count": "security.production.max_episodes",
        "default_shots_per_episode": "security.production.max_shots_per_episode",
        "default_max_generation_tasks": "security.production.max_generation_tasks",
    },
}


def configure_project_and_shot_creation_defaults(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")
    Episode = apps.get_model("studio", "Episode")
    Shot = apps.get_model("studio", "Shot")

    try:
        definition = Definition.objects.get(key=POLICY_KEY)
    except Definition.DoesNotExist as exc:
        raise RuntimeError("0123 缺少 production.project_creation_policy 定义") from exc
    if (
        definition.configuration_class != "standard"
        or definition.domain != "production"
        or definition.allowed_scopes != "system"
    ):
        raise RuntimeError("0123 project_creation_policy 定义职责与预期不一致")

    active = Revision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).order_by("-revision").first()
    if active is None:
        raise RuntimeError("0123 缺少 active system revision")
    existing = list(
        Value.objects.filter(revision=active).select_related(
            "definition", "credential_version",
        )
    )
    current = next(
        (row for row in existing if row.definition_id == definition.id),
        None,
    )
    if current is None:
        raise RuntimeError("0123 active system revision 缺少 project_creation_policy")
    upgraded = _policy_v2(current.value)
    upgraded_initial = _policy_v2(definition.initial_value)

    schema_size, schema_sha = _contract(POLICY_SCHEMA_V2)
    value_size, value_sha = _contract(upgraded)
    initial_size, initial_sha = _contract(upgraded_initial)
    Definition.objects.filter(pk=definition.pk).update(
        description=(
            "项目、集与分镜创建的版本化语义默认合同；分辨率、镜头时长和音频"
            "分别读取既有单一配置源 / Versioned project and shot creation semantics"
        ),
        value_schema=POLICY_SCHEMA_V2,
        value_schema_version=2,
        value_schema_size_bytes=schema_size,
        value_schema_sha256=schema_sha,
        initial_value=upgraded_initial,
        initial_value_schema_version=2,
        initial_value_size_bytes=initial_size,
        initial_value_sha256=initial_sha,
    )

    now = timezone.now()
    next_revision = (
        Revision.objects.filter(scope_type="system")
        .order_by("-revision")
        .values_list("revision", flat=True)
        .first()
        or 0
    ) + 1
    revision = Revision.objects.create(
        scope_type="system",
        revision=next_revision,
        status="published",
        based_on_revision_id=active.id,
        activation_state="inactive",
        change_summary=(
            "0123 将语言、画幅选项、FPS 与镜头媒体类型默认值统一迁入"
            " project_creation_policy v2"
        ),
        published_at=now,
    )
    rows = []
    digest_values = {}
    for row in existing:
        if row.definition_id == definition.id:
            rows.append(Value(
                revision=revision,
                definition_id=row.definition_id,
                value=upgraded,
                value_schema_version=2,
                value_size_bytes=value_size,
                value_sha256=value_sha,
            ))
            digest_values[POLICY_KEY] = upgraded
            continue
        rows.append(Value(
            revision=revision,
            definition_id=row.definition_id,
            value=row.value,
            value_schema_version=row.value_schema_version,
            value_size_bytes=row.value_size_bytes,
            value_sha256=row.value_sha256,
            credential_version_id=row.credential_version_id,
        ))
        digest_values[row.definition.key] = (
            row.value
            if not row.credential_version_id
            else {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(
                    f"version:{row.credential_version_id}:{row.credential_version.version}".encode(
                        "utf-8"
                    )
                ).hexdigest(),
            }
        )
    Value.objects.bulk_create(rows)
    revision.values_sha256 = hashlib.sha256(_canonical(digest_values)).hexdigest()
    revision.save(update_fields=("values_sha256", "updated_at"))
    Revision.objects.filter(pk=active.pk).update(
        status="superseded",
        activation_state="inactive",
        updated_at=now,
    )
    Revision.objects.filter(pk=revision.pk).update(
        activation_state="active",
        activated_at=now,
        updated_at=now,
    )

    # Old UI semantics treated missing episode audio as enabled and image-shot
    # audio as disabled.  Materialize those facts before removing read fallbacks.
    for episode in Episode.objects.all().iterator():
        prompt_settings = (
            dict(episode.prompt_settings)
            if isinstance(episode.prompt_settings, dict)
            else {}
        )
        if "include_audio" not in prompt_settings:
            prompt_settings["include_audio"] = True
            Episode.objects.filter(pk=episode.pk).update(
                prompt_settings=prompt_settings,
            )
    Shot.objects.filter(media_type="image", include_audio=True).update(
        include_audio=False,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0122_remove_prompt_and_shot_duration_defaults"),
    ]

    operations = [
        migrations.AlterField(
            model_name="seriesproject",
            name="language",
            field=models.CharField(max_length=32),
        ),
        migrations.AlterField(
            model_name="seriesproject",
            name="aspect_ratio",
            field=models.CharField(max_length=16),
        ),
        migrations.AlterField(
            model_name="shot",
            name="media_type",
            field=models.CharField(
                choices=[("video", "视频"), ("image", "图片")],
                max_length=12,
            ),
        ),
        migrations.AlterField(
            model_name="shot",
            name="include_audio",
            field=models.BooleanField(),
        ),
        migrations.RunPython(
            configure_project_and_shot_creation_defaults,
            migrations.RunPython.noop,
        ),
    ]
