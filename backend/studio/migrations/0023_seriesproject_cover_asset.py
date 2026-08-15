from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0022_seriesproject_is_favorite"),
    ]

    operations = [
        migrations.AddField(
            model_name="seriesproject",
            name="cover_asset",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="cover_projects",
                to="studio.asset",
            ),
        ),
    ]
