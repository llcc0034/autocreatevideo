from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0020_generationtask_progress_telemetry"),
    ]

    operations = [
        migrations.RemoveField(model_name="asset", name="attribution"),
        migrations.RemoveField(model_name="asset", name="license_type"),
        migrations.DeleteModel(name="ContentModerationEvent"),
        migrations.DeleteModel(name="ContentAppeal"),
        migrations.DeleteModel(name="PublicContentReportGrant"),
        migrations.DeleteModel(name="ContentReport"),
        migrations.DeleteModel(name="ContentPolicyAcceptance"),
    ]
