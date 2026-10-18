"""Store each Smoke root's authorization/accounting on the original attempt.

No historical authorization, cost, identity or acceptance is inferred.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0146_execution_smoke_scope")]

    operations = [
        migrations.AddField(model_name="workflowsmokeattempt", name="idempotency_key", field=models.CharField(max_length=191, blank=True)),
        *[migrations.AddField(model_name=model, name=name, field=field)
          for model in ("modeldeploymentsmokeattempt", "workflowsmokeattempt")
          for name, field in (
              ("allow_external_calls", models.BooleanField(default=False)),
              ("budget_limit", models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)),
              ("actual_cost", models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)),
          )],
        migrations.AddField(model_name="workflowsmokeattempt", name="estimated_cost", field=models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)),
        migrations.AddField(model_name="workflowsmokeattempt", name="cost_currency", field=models.CharField(max_length=12, blank=True)),
        *[migrations.AddConstraint(model_name=model, constraint=models.UniqueConstraint(
            fields=("execution_instance_version", "idempotency_key"), condition=models.Q(scope="instance"), name=name))
          for model, name in (("modeldeploymentsmokeattempt", "uq_deploy_instance_smoke_key"), ("workflowsmokeattempt", "uq_workflow_instance_smoke_key"))],
        *[migrations.AddConstraint(model_name=model, constraint=models.CheckConstraint(
            condition=models.Q(budget_limit__isnull=True) | models.Q(budget_limit__gte=0), name=name))
          for model, name in (("modeldeploymentsmokeattempt", "ck_deployment_smoke_budget"), ("workflowsmokeattempt", "ck_workflow_smoke_budget"))],
    ]
