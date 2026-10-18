import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0212_manual_primary_retry")]

    operations = [migrations.CreateModel(
        name="ScriptRevisionStructure",
        fields=[
            ("revision", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True,
                related_name="structured_content", serialize=False, to="studio.scriptrevision")),
            ("output_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="script_revision_structures", to="studio.businessoutputversion")),
            ("document", models.JSONField()),
            ("document_schema_version", models.PositiveIntegerField()),
            ("document_size_bytes", models.PositiveIntegerField()),
            ("document_sha256", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
        ],
    )]
