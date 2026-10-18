from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0228_manual_shot_reference_selection_action")]

    operations = [
        migrations.RemoveConstraint(model_name="shotreferenceuse", name="ck_shot_use_identity"),
        migrations.AlterField(model_name="shotreferenceuse", name="purpose",
            field=models.CharField(blank=True, max_length=160)),
        migrations.AddConstraint(model_name="shotreferenceuse",
            constraint=models.CheckConstraint(condition=~models.Q(use_key=""), name="ck_shot_use_identity")),
    ]
