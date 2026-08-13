from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0014_content_compliance"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PublicContentReportGrant",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("target_type", models.CharField(choices=[
                    ("generation_task", "Generation task output"),
                    ("assembly", "Assembled output"),
                    ("asset", "Uploaded asset"),
                ], max_length=32)),
                ("target_id", models.UUIDField(db_index=True)),
                ("output_index", models.PositiveIntegerField(blank=True, null=True)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("owner", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="public_content_report_grants",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("project", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="public_content_report_grants",
                    to="studio.seriesproject",
                )),
            ],
            options={
                "ordering": ("-updated_at",),
                "indexes": [models.Index(
                    fields=["target_type", "target_id", "output_index"],
                    name="studio_publ_target__92b2eb_idx",
                )],
                "constraints": [models.CheckConstraint(
                    condition=models.Q(
                        ("target_type", "generation_task"),
                        ("output_index__isnull", True),
                        _connector="OR",
                    ),
                    name="public_report_grant_output_only_for_task",
                )],
            },
        ),
        migrations.AlterField(
            model_name="contentreport",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="owned_content_reports",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="contentreport",
            name="reported_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="submitted_content_reports",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="contentreport",
            name="report_source",
            field=models.CharField(
                choices=[
                    ("authenticated", "Authenticated user"),
                    ("public_token", "Anonymous signed report link"),
                ],
                default="authenticated",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="contentreport",
            name="reporter_contact_email",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="contentreport",
            name="public_submission_key",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=64,
                null=True,
                unique=True,
            ),
        ),
    ]
