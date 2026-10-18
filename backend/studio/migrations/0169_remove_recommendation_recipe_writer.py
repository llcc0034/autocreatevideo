from django.db import migrations


def require_old_recommendation_cleanup(apps, schema_editor):
    alias = schema_editor.connection.alias
    decisions = apps.get_model("studio", "RecommendationDecision").objects.using(alias)
    candidates = apps.get_model("studio", "RecommendationCandidate").objects.using(alias)
    if (decisions.filter(recipe_version__isnull=False).exists()
            or candidates.filter(suggested_values__has_key="route").exists()):
        raise RuntimeError(
            "仍存在旧直连路由推荐或推荐派生配方；须先完成R7精确依赖清理。"
            "不自动拆分执行实例，不将旧推荐改写为新实例或配方。"
        )


class Migration(migrations.Migration):
    dependencies = [("studio", "0168_remove_retired_prompt_workflow_storage")]
    operations = [
        migrations.RunPython(require_old_recommendation_cleanup, migrations.RunPython.noop),
        migrations.RemoveField(model_name="recommendationdecision", name="recipe_version"),
    ]
