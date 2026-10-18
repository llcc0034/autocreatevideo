"""Remove the retired per-recipe/per-run resource configuration columns."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("studio", "0179_resource_review_json_integrity")]
    operations = [
        migrations.RemoveConstraint("productionrecipeversion", "recipe_default_budget_nonnegative"),
        migrations.RemoveConstraint("productionrecipeversion", "recipe_default_budget_currency_pair"),
        migrations.RemoveConstraint("productionrun", "production_run_budget_nonnegative"),
        migrations.RemoveField("productionrecipeversion", "default_budget_limit"),
        migrations.RemoveField("productionrecipeversion", "default_budget_currency"),
        migrations.RemoveField("productionrecipeversion", "default_max_generation_tasks"),
        migrations.RemoveField("productionrun", "budget_limit"),
        migrations.RemoveField("productionrun", "max_generation_tasks"),
    ]
