from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0201_prompt_rule_field_versions")]

    operations = [
        migrations.RemoveIndex(model_name="fielddefinition", name="idx_field_category_active"),
        *[migrations.RemoveField(model_name="fielddefinition", name=name) for name in (
            "label", "category", "description", "impact", "value_type", "value_options",
        )],
        migrations.AddIndex(model_name="fielddefinition", index=models.Index(
            fields=("is_active", "display_order", "key"), name="idx_field_active_order",
        )),
    ]
