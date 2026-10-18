import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0235_asset_reference_history_cleanup"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(
        name="ComfyWorkflowPreviewReceipt",
        fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("request_key", models.CharField(max_length=191)),
            ("request_sha256", models.CharField(max_length=64)),
            ("response", models.JSONField()),
            ("response_schema_version", models.PositiveIntegerField(default=1)),
            ("response_size_bytes", models.PositiveIntegerField()),
            ("response_sha256", models.CharField(max_length=64)),
            ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ("deployment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="studio.modeldeployment")),
            ("contract_draft", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="studio.comfyworkflowcontractdraft")),
        ],
        options={"constraints": [models.UniqueConstraint(fields=("owner", "request_key"), name="uk_comfy_preview_receipt_key")]},
    )]
