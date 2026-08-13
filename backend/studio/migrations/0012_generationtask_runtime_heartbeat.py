from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0011_alter_asset_kind_reference_media"),
    ]

    operations = [
        migrations.AddField(
            model_name="generationtask",
            name="heartbeat_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="generationtask",
            name="stalled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationtask",
            name="remote_state",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "未知"),
                    ("claimed", "工作线程已领取"),
                    ("preparing", "准备工作流"),
                    ("submitted", "已提交到远端"),
                    ("running", "远端执行中"),
                    ("degraded", "远端健康检查异常"),
                    ("cancelling", "正在取消远端任务"),
                    ("recovered", "已恢复远端任务"),
                    ("missing", "远端任务缺失"),
                    ("succeeded", "远端执行成功"),
                    ("failed", "远端执行失败"),
                    ("cancelled", "远端已取消"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="generationtask",
            name="remote_error",
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name="generationtask",
            name="submission_state",
            field=models.CharField(
                choices=[
                    ("unprepared", "未准备"),
                    ("prepared", "已持久化提交标识"),
                    ("submitted", "已提交"),
                    ("recovered", "已恢复远端提交"),
                    ("terminal_failed", "远端执行已明确失败"),
                    ("terminal_cancelled", "远端执行已明确取消"),
                ],
                default="unprepared",
                max_length=20,
            ),
        ),
    ]
