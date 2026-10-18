from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0208_profile_structured_input")]

    operations = [migrations.AddField(
        model_name="executioninputspec", name="media_source_policy",
        field=models.JSONField(blank=True, null=True),
    )]
