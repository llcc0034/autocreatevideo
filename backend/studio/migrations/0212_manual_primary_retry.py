from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0211_asset_reference_relations")]

    operations = [migrations.AlterField(
        model_name="productionmanualcommand", name="action",
        field=models.CharField(choices=[("generate", "生成候选"), ("retry", "重试失败主调用"),
            ("adopt", "采用已有内容"), ("approve", "批准"), ("assemble", "装配"), ("deliver", "交付")], max_length=8),
    )]
