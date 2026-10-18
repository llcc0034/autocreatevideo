import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0223_bible_image_binding_review")]

    # Every pre-0224 fulfillment was written by the explicit manual action.
    # Preserve those real actors; do not infer or convert automatic decisions.
    operations = [
        migrations.AddField(model_name="productionmaterialfulfillment", name="selection_mode",
            field=models.CharField(choices=[("manual", "人工"), ("automatic", "自动")],
                default="manual", max_length=16)),
        migrations.AlterField(model_name="productionmaterialfulfillment", name="selected_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="production_material_fulfillments", to=settings.AUTH_USER_MODEL)),
        migrations.AddConstraint(model_name="productionmaterialfulfillment",
            constraint=models.CheckConstraint(
                condition=(models.Q(selection_mode="manual", selected_by__isnull=False)
                    | models.Q(selection_mode="automatic", selected_by__isnull=True)),
                name="material_fulfillment_actor_mode")),
    ]
