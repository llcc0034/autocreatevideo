from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("studio", "0191_smoke_chat_http_body_evidence")]

    operations = [
        migrations.AlterField(model_name="assetgenerationjobresolution", name="outcome",
            field=models.CharField(max_length=40, choices=[
                ("http_body_prepared", "HTTP正文已序列化（未证明送达）"),
                ("confirmed_not_submitted", "确认未提交"),
                ("confirmed_remote_failed", "确认远端失败"),
                ("confirmed_remote_cancelled", "确认远端取消"),
                ("confirmed_remote_succeeded", "确认远端成功"),
            ])),
        migrations.AlterField(model_name="assetgenerationjobresolution", name="decided_by",
            field=models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                on_delete=django.db.models.deletion.PROTECT, related_name="asset_generation_job_resolutions")),
    ]
