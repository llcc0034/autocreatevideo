from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0189_add_reviewed_camera_source")]

    operations = [migrations.AlterField(
        model_name="generationattemptresolution", name="outcome",
        field=models.CharField(max_length=40, choices=[
            ("confirmed_not_submitted", "确认未提交"),
            ("confirmed_remote_submitted", "确认远端已提交"),
            ("confirmed_remote_failed", "确认远端失败"),
            ("actual_cost_confirmed", "确认实际费用"),
            ("cancel_requested_ignore_late", "取消并忽略晚到结果"),
            ("late_result_ignored", "已忽略晚到结果"),
            ("known_response_rejected", "已知响应未通过校验"),
            ("response_received", "已记录服务响应用量"),
            ("http_body_prepared", "已记录HTTP库序列化正文摘要"),
        ]),
    )]
