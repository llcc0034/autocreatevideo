from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0227_retire_script_breakdown_policies")]

    operations = [migrations.AlterField(model_name="productionmanualcommand", name="action",
        field=models.CharField(max_length=8, choices=[("generate", "生成候选"),
            ("retry", "重试失败主调用"), ("adopt", "采用已有内容"), ("select", "选择镜头素材"),
            ("approve", "批准"), ("assemble", "装配"), ("deliver", "交付")]))]
