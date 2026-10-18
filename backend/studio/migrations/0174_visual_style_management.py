from django.db import migrations, models
import django.db.models.deletion


DESCRIPTIONS = {
    "wuxia": ("武侠", "水墨氛围与写实人物结合，青玉、朱红配色，飘逸衣料与凌厉武打构图。"),
    "xianxia": ("玄幻仙侠", "仙山云海、灵光与华丽古装，强调宏大空间和电影级奇幻质感。"),
    "anime": ("国创动漫", "精细线稿、鲜明表情与分层背景，强调统一角色造型和动画式构图。"),
    "thriller": ("心理惊悚", "压迫性光影、斑驳材质与克制线索，以心理紧张感营造悬疑氛围。"),
    "detective": ("民国侦探", "民国黑色电影质感，雨窗、明暗对照与琥珀深蓝色调，镜头沉稳。"),
    "urban-drama": ("都市现实剧情", "自然窗光、真实生活空间与细腻微表情，呈现贴近人物的都市写实感。"),
    "cyberpunk": ("未来科幻", "近未来城市与功能性工业设计，雨夜霓虹、精密科技细节和大画幅构图。"),
    "palace-intrigue": ("古装权谋", "考究宫廷礼制、木构殿堂与克制华服，以烛光和沉稳构图表现权谋张力。"),
    "post-apocalypse": ("末日废土", "风沙、工业废墟与磨损装备，强调严酷自然光和写实生存氛围。"),
    "healing-fantasy": ("治愈奇幻", "温暖田园光线、柔和自然色与手作质感，营造亲切、舒缓的奇幻世界。"),
    "glamour-wuxia": ("魅影风华", "优雅武侠与黑色电影结合，精致服装、戏剧性夜景与成熟人物气质。"),
}


def replace_bootstrap_descriptions(apps, schema_editor):
    Style = apps.get_model("studio", "Style")
    for slug, (name, description) in DESCRIPTIONS.items():
        # Exact old bootstrap text only: preserve all authored descriptions and
        # all model-facing prompt content, even for renamed system presets.
        Style.objects.using(schema_editor.connection.alias).filter(
            owner__isnull=True, is_system=True, slug=slug,
            description=f"{name}商业短剧预设，可绑定本地人物、场景与道具参考图。",
        ).update(description=description)


class Migration(migrations.Migration):
    dependencies = [("studio", "0173_recipe_run_budget_defaults")]
    operations = [
        migrations.AlterField(model_name="asset", name="style", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="assets", to="studio.style")),
        migrations.AlterField(model_name="seriesproject", name="style", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="projects", to="studio.style")),
        migrations.AlterField(model_name="assetgenerationjob", name="style", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="asset_generation_jobs", to="studio.style")),
        migrations.RunPython(replace_bootstrap_descriptions, migrations.RunPython.noop),
    ]
