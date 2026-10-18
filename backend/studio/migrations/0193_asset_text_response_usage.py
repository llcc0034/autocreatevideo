from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0192_asset_chat_http_body_evidence")]

    operations = [
        migrations.AlterField(model_name="assetgenerationjobresolution", name="outcome",
            field=models.CharField(max_length=40, choices=[
                ("http_body_prepared", "HTTP正文已序列化（未证明送达）"),
                ("response_received", "文本响应已返回（非扣费或采用证明）"),
                ("confirmed_not_submitted", "确认未提交"),
                ("confirmed_remote_failed", "确认远端失败"),
                ("confirmed_remote_cancelled", "确认远端取消"),
                ("confirmed_remote_succeeded", "确认远端成功"),
            ])),
    ]
