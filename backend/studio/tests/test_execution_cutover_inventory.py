"""Maintenance preview on isolated 0144 tables; never an operational deletion."""
import io
import json
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from studio.management.execution_cutover_inventory import execution_cutover_inventory
from studio.management.commands.purge_incompatible_workflows import _incoming_fields
from studio.tests.test_configuration_migrations import _MigrationTestCase


class CutoverInventoryEntryTests(SimpleTestCase):
    def test_preview_rejects_all_mutating_or_other_scope_options_before_any_query(self):
        for options in ({"execute": True}, {"irreversible_without_backup": True},
                {"confirmation": "not-an-execution-plan"}, {"expected_plan_sha256": "a" * 64}):
            with self.subTest(options=options), self.assertRaisesMessage(CommandError, "只读"):
                call_command("purge_incompatible_workflows", execution_cutover_preview=True, **options)


class CutoverInventorySchemaGuardTests(TestCase):
    def test_current_schema_is_not_read_using_the_old_inventory(self):
        with self.assertRaisesMessage(CommandError, "实际Schema不是已核对的0144"):
            execution_cutover_inventory()


class ExecutionCutoverInventoryTests(_MigrationTestCase):
    migrate_from = "0144_execution_policy_configuration"
    migrate_to = migrate_from

    def set_up_before_migration(self, apps):
        self.actor = apps.get_model("auth", "User").objects.create(username="cutover-inventory-owner")
        self.project = apps.get_model("studio", "SeriesProject").objects.create(
            owner=self.actor, title="Preserved project", language="zh", aspect_ratio="16:9",
            width=640, height=360, fps=24, default_shot_duration_ms=1000, continuity_seed=1)
        self.prompt = apps.get_model("studio", "PromptArtifact").objects.create(
            project=self.project, created_by=self.actor, is_multi_shot=False, include_audio=False,
            total_duration_ms=1000, prompt_type="video", prompt_text="PRIVATE PROMPT MUST NOT LEAK")
        self.Binding = apps.get_model("studio", "PromptProfileBinding")
        self.models = list(apps.get_models())

    def test_preview_uses_historical_registry_and_never_calls_the_write_path(self):
        before = {model._meta.label: model.objects.count() for model in self.models}
        output = io.StringIO()
        with patch("studio.management.commands.purge_incompatible_workflows._hard_delete_plan",
                side_effect=AssertionError("read-only")), patch(
                "studio.management.commands.purge_incompatible_workflows.build_purge_plan",
                side_effect=AssertionError("do not query current workflow model")):
            call_command("purge_incompatible_workflows", execution_cutover_preview=True, stdout=output)
        report = json.loads(output.getvalue())
        self.assertFalse(report["executable"])
        self.assertEqual(report["source_migration"], self.migrate_from)
        self.assertEqual(report["counts"]["studio.PromptArtifact"], 1)
        self.assertIn("studio.ProductionRun", {root["model"] for root in report["roots"]})
        self.assertFalse(report["runtime_state"]["remote_state_checked"])
        self.assertIn("explicit_new_configuration_baseline", report["pending_checks"])
        self.assertNotIn("PRIVATE PROMPT", output.getvalue())
        self.assertEqual({model._meta.label: model.objects.count() for model in self.models}, before)
        self.prompt.refresh_from_db()
        self.assertEqual(self.prompt.prompt_text, "PRIVATE PROMPT MUST NOT LEAK")

    def test_same_ids_with_changed_payload_or_shared_resource_change_inventory_hash(self):
        first = execution_cutover_inventory()
        self.assertEqual(execution_cutover_inventory()["inventory_sha256"], first["inventory_sha256"])
        self.prompt.prompt_text = "A changed private payload"
        self.prompt.save()
        changed = execution_cutover_inventory()
        self.assertNotEqual(first["inventory_sha256"], changed["inventory_sha256"])
        self.assertEqual(first["counts"], changed["counts"])
        self.project.title = "Changed shared project"
        self.project.save()
        shared_changed = execution_cutover_inventory()
        self.assertNotEqual(shared_changed["inventory_sha256"], changed["inventory_sha256"])
        self.assertEqual(shared_changed["counts"], changed["counts"])
        self.assertEqual(shared_changed["shared_dependency_conflicts"], {})

    def test_reverse_relations_belong_to_the_schema_being_inspected(self):
        relations = _incoming_fields(self.Binding)
        self.assertTrue(any(model._meta.model_name == "modeldeploymentsmokeattempt"
            and field.name == "prompt_profile_binding" for model, field in relations))
        self.assertTrue(all(model._meta.apps is self.Binding._meta.apps for model, _ in relations))

    def test_corrupt_configuration_is_reported_and_never_rehashed_as_a_new_baseline(self):
        baseline, = execution_cutover_inventory()["configuration"]["active_baselines"]
        self.assertEqual(baseline["storage_integrity"], "valid")
        row = self.apps.get_model("studio", "ConfigValue").objects.get(
            revision_id=baseline["id"], definition__key="api.default_page_size")
        original_sha = row.value_sha256
        row.value = 123
        row.save()
        broken, = execution_cutover_inventory()["configuration"]["active_baselines"]
        self.assertEqual(broken["storage_integrity"], "invalid")
        self.assertIsNone(broken["retained_values_sha256"])
        self.assertTrue(broken["current_contract_review_required"])
        self.assertIn({"key": "api.default_page_size", "code": "configuration_value_integrity_invalid"}, broken["integrity_errors"])
        row.refresh_from_db()
        self.assertEqual(row.value_sha256, original_sha)
        self.assertEqual(row.value, 123)

    def test_semantic_reference_is_reported_without_expanding_deletion_to_shared_resources(self):
        profile = self.apps.get_model("studio", "ModelProfileVersion").objects.first()
        profile.specification = {"notes": {"source": str(self.prompt.pk), "text": "PRIVATE SHARED NOTES"}}
        profile.save()
        audit = self.apps.get_model("studio", "PlatformLifecycleAuditEvent").objects.create(
            action="create", object_type="prompt_artifact", object_id=self.prompt.pk, reason_code="isolated_inventory",
            before_summary={"source": str(self.prompt.pk)}, after_summary={"source": str(self.prompt.pk)})
        report = execution_cutover_inventory()
        hits = report["semantic_references_requiring_review"]
        self.assertIn({"model": profile._meta.label, "id": str(profile.pk), "field": "specification",
            "already_in_fk_closure": False}, hits)
        self.assertIn({"model": audit._meta.label, "id": str(audit.pk), "field": "object_id",
            "already_in_fk_closure": False}, hits)
        self.assertEqual({hit["field"] for hit in hits if hit["model"] == audit._meta.label and hit["id"] == str(audit.pk)},
            {"object_id", "before_summary", "after_summary"})
        self.assertNotIn(profile._meta.label, report["counts"])
        self.assertNotIn("PRIVATE SHARED NOTES", json.dumps(report))
        self.assertTrue(self.apps.get_model("studio", "ModelProfileVersion").objects.filter(pk=profile.pk).exists())

    def test_configuration_failure_does_not_classify_all_shared_resources_as_incompatible(self):
        from studio.services.configuration import ConfigurationResolutionError
        with patch("studio.services.configuration.resolve_security_boundaries",
                side_effect=ConfigurationResolutionError("PRIVATE CONFIGURATION DETAIL", code="config_not_ready")), patch(
                "studio.services.model_profile_contracts.verified_model_profile_specification",
                side_effect=AssertionError("cannot inspect resources with unresolved configuration")):
            report = execution_cutover_inventory()
        self.assertEqual(report["native_resource_contract_checks"], {
            "status": "blocked_by_configuration", "resources": {}, "configuration_fields": [], "code": "config_not_ready"})
        self.assertNotIn("studio.ModelProfileVersion", report["counts"])
        self.assertNotIn("PRIVATE CONFIGURATION DETAIL", json.dumps(report))

    def test_assembly_scope_matches_migration_guard_and_reports_unsettled_work(self):
        assembly = self.apps.get_model("studio", "AssemblyJob")
        obsolete = assembly.objects.create(owner_id=self.actor.pk, project_id=self.project.pk, scope="series",
            source_contract_version=4, max_attempts=1, status="running")
        preserved = assembly.objects.create(owner_id=self.actor.pk, project_id=self.project.pk, scope="series",
            source_contract_version=5, max_attempts=1, status="planned")
        report = execution_cutover_inventory()
        self.assertEqual([row["id"] for row in report["rows"][assembly._meta.label]], [str(obsolete.pk)])
        self.assertIn({"model": assembly._meta.label, "id": str(obsolete.pk), "status": "running"},
            report["runtime_state"]["unsettled_rows"])
        self.assertEqual(report["runtime_state"]["status_counts"]["AssemblyJob"], {"planned": 1, "running": 1})
        self.assertTrue(assembly.objects.filter(pk=preserved.pk).exists())
        self.assertTrue(assembly.objects.filter(pk=obsolete.pk).exists())
