"""Remote completion and managed-artifact acceptance are separate facts."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0152_generation_task_primary_attempt")]
    operations = [migrations.AlterField(model_name="generationtask", name="submission_state",
        field=models.CharField(max_length=20, default="unprepared", choices=[
            ("unprepared", "未准备"), ("prepared", "已持久化提交标识"), ("submitted", "已提交"),
            ("recovered", "已恢复远端提交"), ("terminal_succeeded", "远端执行已成功，平台产物另行验收"),
            ("terminal_failed", "远端执行已明确失败"), ("terminal_cancelled", "远端执行已明确取消"),
        ]))]
