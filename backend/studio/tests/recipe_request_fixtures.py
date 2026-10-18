"""Current immutable receipt for isolated stage tests, not root admission."""
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_run_plans import get_run_execution_plan


def freeze_recipe_request_fixture(run, *, authorized):
    plan = get_run_execution_plan(run)
    intent = {"schema_version": 4, "plan_source": "recipe", "project_id": str(run.project_id),
        "recipe_version_id": str(run.recipe_version_id), "unattended_confirmed": authorized,
        "creation_inputs": {"idea": "", "style_hint": "", "episode_count": None,
            "shots_per_episode": None, "episode_duration_seconds": None}}
    run.request_sha256 = json_contract(intent).sha256
    assign_json_contract(run, "input_snapshot", {"schema_version": 4, "request_intent": intent,
        "run_plan_sha256": plan.sha256}, schema_version=4)
    run.save(update_fields=("request_sha256", "input_snapshot", "input_snapshot_schema_version",
        "input_snapshot_size_bytes", "input_snapshot_sha256"))
