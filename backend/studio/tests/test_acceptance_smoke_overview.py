from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from datetime import timedelta

from django.utils import timezone
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory

from studio.models import (
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ModelDeploymentSmokeAttemptResolution,
    WorkflowSmokeAttempt,
    WorkflowTemplateVersion,
)
from studio.pagination import AcceptanceSmokePagination
from studio.platform_views import AcceptanceSmokeView
from studio.services.acceptance_smoke import (
    deployment_acceptance_row,
    safe_snapshot_summary,
)
from studio.services.json_contracts import assign_json_contract
from studio.tests.test_platform_foundation import (
    WorkflowVersionApiTests as _WorkflowVersionApiFixture,
)


_workflow_fixture_setup = _WorkflowVersionApiFixture.setUp
_workflow_fixture_import = _WorkflowVersionApiFixture._import
_workflow_canvas_object_info = _WorkflowVersionApiFixture._canvas_object_info
del _WorkflowVersionApiFixture


class AcceptanceSmokeApiTests(TestCase):
    def setUp(self):
        # Reuse the canonical valid generic.comfy workflow fixture without
        # inheriting the large API test suite itself.
        _workflow_fixture_setup(self)

    def _import(self):
        return _workflow_fixture_import(self)

    @staticmethod
    def _canvas_object_info():
        return _workflow_canvas_object_info()

    def _workflow_attempt(self, version, deployment):
        now = timezone.now()
        return WorkflowSmokeAttempt.objects.create(
            workflow_version=version,
            deployment=deployment,
            created_by=self.admin,
            status=WorkflowSmokeAttempt.Status.FAILED,
            graph_sha256=version.graph_sha256,
            execution_contract_sha256=version.execution_contract_sha256,
            validation_evidence_sha256="",
            target_sha256="a" * 64,
            deadline_started_at=now,
            hard_deadline_at=now,
            idle_deadline_at=now,
            last_meaningful_progress_at=now,
            error_code="provider_smoke_failed",
        )

    def test_subject_is_required_and_staff_permission_is_enforced(self):
        missing = self.client.get("/api/v1/acceptance-smoke/")
        self.assertEqual(missing.status_code, 400, missing.data)
        self.assertIn("subject", missing.data["error"]["details"])

        user = get_user_model().objects.create_user(
            username="acceptance-reader",
            password="StrongPass123!",
            is_staff=False,
        )
        client = APIClient()
        client.force_authenticate(user)
        forbidden = client.get(
            "/api/v1/acceptance-smoke/",
            {"subject": "deployment"},
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.data)

        invalid_page_size = self.client.get(
            "/api/v1/acceptance-smoke/",
            {"subject": "workflow", "page_size": "not-a-number"},
        )
        self.assertEqual(invalid_page_size.status_code, 400, invalid_page_size.data)
        unknown_query = self.client.get(
            "/api/v1/acceptance-smoke/",
            {"subject": "workflow", "surprise": "value"},
        )
        self.assertEqual(unknown_query.status_code, 400, unknown_query.data)

    def test_missing_deployment_filter_returns_an_empty_page_for_both_subjects(self):
        missing_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        for subject in ("workflow", "deployment"):
            response = self.client.get(
                "/api/v1/acceptance-smoke/",
                {"subject": subject, "deployment_id": missing_id},
            )
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["count"], 0)
            self.assertEqual(response.data["results"], [])

    def test_profile_match_is_only_compatible_and_get_never_calls_driver(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)

        with patch("studio.services.workflow_versions.driver_for") as driver_for:
            response = self.client.get(
                "/api/v1/acceptance-smoke/",
                {
                    "subject": "workflow",
                    "deployment_id": str(self.deployment.id),
                    "workflow_id": imported.data["id"],
                    "page_size": 200,
                },
            )

        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item for item in response.data["results"]
            if item["workflow"]["id"] == imported.data["id"]
        )
        self.assertEqual(row["association"]["level"], "compatible")
        self.assertTrue(row["association"]["profile_matches"])
        self.assertFalse(row["association"]["validated_deployment_matches"])
        self.assertEqual(row["validation"]["state"], "not_run")
        driver_for.assert_not_called()

        provider = self.deployment.profile_version.provider
        provider.is_active = False
        provider.save(update_fields=("is_active", "updated_at"))
        unavailable = self.client.get(
            "/api/v1/acceptance-smoke/",
            {
                "subject": "workflow",
                "deployment_id": str(self.deployment.id),
                "workflow_id": imported.data["id"],
            },
        )
        live_validate = next(
            item for item in unavailable.data["results"][0]["actions"]
            if item["kind"] == "live_validate"
        )
        self.assertFalse(live_validate["enabled"])

    def test_live_validate_action_reuses_business_graph_limit_and_preflight(self):
        imported = self._import()
        configuration = Mock(values={"workflow.max_graph_bytes": 4321})
        with patch(
            "studio.platform_views.resolve_configuration",
            return_value=configuration,
        ) as resolve, patch(
            "studio.services.acceptance_smoke.verify_workflow_version_integrity",
        ) as verify_integrity, patch(
            "studio.services.acceptance_smoke.workflow_execution_target_sha256",
            return_value="a" * 64,
        ):
            response = self.client.get(
                "/api/v1/acceptance-smoke/",
                {
                    "subject": "workflow",
                    "deployment_id": str(self.deployment.id),
                    "workflow_id": imported.data["id"],
                },
            )

        self.assertEqual(response.status_code, 200, response.data)
        resolve.assert_called_once_with(
            required_keys=("workflow.max_graph_bytes",),
        )
        self.assertEqual(
            verify_integrity.call_args.kwargs["graph_limit"],
            4321,
        )
        live_validate = next(
            action for action in response.data["results"][0]["actions"]
            if action["kind"] == "live_validate"
        )
        self.assertTrue(live_validate["enabled"])

        WorkflowTemplateVersion.objects.filter(pk=imported.data["id"]).update(
            graph_sha256="0" * 64,
        )
        corrupted = self.client.get(
            "/api/v1/acceptance-smoke/",
            {
                "subject": "workflow",
                "deployment_id": str(self.deployment.id),
                "workflow_id": imported.data["id"],
            },
        )
        self.assertEqual(corrupted.status_code, 200, corrupted.data)
        live_validate = next(
            action for action in corrupted.data["results"][0]["actions"]
            if action["kind"] == "live_validate"
        )
        self.assertFalse(live_validate["enabled"])
        self.assertEqual(
            live_validate["reason_code"],
            "workflow_validation_precheck_failed",
        )

    def test_historical_pass_is_preserved_while_runtime_drift_is_reported(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201, imported.data)
        version_id = imported.data["id"]
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )
        self.assertEqual(validated.status_code, 200, validated.data)

        current = self.client.get(
            "/api/v1/acceptance-smoke/",
            {"subject": "workflow", "deployment_id": str(self.deployment.id)},
        )
        self.assertEqual(current.status_code, 200, current.data)
        current_row = next(
            item for item in current.data["results"]
            if item["workflow"]["id"] == version_id
        )
        self.assertTrue(current_row["validation"]["fresh"])
        self.assertEqual(current_row["association"]["level"], "validated")

        deployment = ModelDeployment.objects.get(pk=self.deployment.id)
        drifted_config = dict(deployment.runtime_config)
        drifted_config["acceptance_test_revision"] = 2
        assign_json_contract(deployment, "runtime_config", drifted_config)
        deployment.save(update_fields=(
            "runtime_config",
            "runtime_config_schema_version",
            "runtime_config_size_bytes",
            "runtime_config_sha256",
            "updated_at",
        ))
        before = WorkflowTemplateVersion.objects.get(pk=version_id)
        evidence_sha256 = before.validation_evidence_sha256

        with patch("studio.services.workflow_versions.driver_for") as driver_for:
            drifted = self.client.get(
                "/api/v1/acceptance-smoke/",
                {"subject": "workflow", "deployment_id": str(self.deployment.id)},
            )

        self.assertEqual(drifted.status_code, 200, drifted.data)
        row = next(
            item for item in drifted.data["results"]
            if item["workflow"]["id"] == version_id
        )
        self.assertEqual(row["validation"]["stored_state"], "passed")
        self.assertEqual(row["validation"]["state"], "drifted")
        self.assertFalse(row["validation"]["fresh"])
        self.assertEqual(row["validation"]["reason_code"], "workflow_target_drifted")
        driver_for.assert_not_called()

        after = WorkflowTemplateVersion.objects.get(pk=version_id)
        self.assertEqual(after.validation_state, WorkflowTemplateVersion.ValidationState.PASSED)
        self.assertEqual(after.validation_evidence_sha256, evidence_sha256)

    def test_latest_workflow_attempt_is_a_safe_execution_summary(self):
        imported = self._import()
        version = WorkflowTemplateVersion.objects.get(pk=imported.data["id"])
        now = timezone.now()
        attempt = WorkflowSmokeAttempt.objects.create(
            workflow_version=version,
            deployment=self.deployment,
            created_by=self.admin,
            status=WorkflowSmokeAttempt.Status.FAILED,
            graph_sha256=version.graph_sha256,
            execution_contract_sha256=version.execution_contract_sha256,
            validation_evidence_sha256="",
            target_sha256="a" * 64,
            deadline_started_at=now,
            hard_deadline_at=now,
            idle_deadline_at=now,
            last_meaningful_progress_at=now,
            error_code="provider_smoke_failed",
            error_message="provider diagnostic must stay private",
        )

        response = self.client.get(
            "/api/v1/acceptance-smoke/",
            {
                "subject": "workflow",
                "workflow_id": str(version.id),
                "deployment_id": str(self.deployment.id),
            },
        )

        self.assertEqual(response.status_code, 200, response.data)
        latest = response.data["results"][0]["smoke"]["latest_attempt"]
        self.assertEqual(latest["id"], str(attempt.id))
        self.assertEqual(latest["error_code"], "provider_smoke_failed")
        self.assertIsNone(latest["started_at"])
        self.assertNotIn("error_message", latest)

    def test_unfiltered_workflow_attempt_stays_bound_to_the_row_deployment(self):
        imported = self._import()
        version = WorkflowTemplateVersion.objects.get(pk=imported.data["id"])
        version.validated_deployment = self.deployment
        version.save(update_fields=("validated_deployment", "updated_at"))
        validated_attempt = self._workflow_attempt(version, self.deployment)
        other_deployment = ModelDeployment.objects.create(
            key="acceptance-other-deployment",
            name="Acceptance other deployment",
            profile_version=self.deployment.profile_version,
            runtime_config=self.deployment.runtime_config,
            runtime_config_schema_version=(
                self.deployment.runtime_config_schema_version
            ),
            runtime_config_size_bytes=self.deployment.runtime_config_size_bytes,
            runtime_config_sha256=self.deployment.runtime_config_sha256,
            status=ModelDeployment.Status.DISABLED,
        )
        other_attempt = self._workflow_attempt(version, other_deployment)

        response = self.client.get(
            "/api/v1/acceptance-smoke/",
            {"subject": "workflow", "workflow_id": str(version.id)},
        )

        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["results"][0]
        self.assertEqual(row["deployment"]["id"], str(self.deployment.id))
        self.assertEqual(row["association"]["level"], "executed")
        self.assertEqual(
            row["smoke"]["latest_attempt"]["id"],
            str(validated_attempt.id),
        )
        self.assertNotEqual(
            row["smoke"]["latest_attempt"]["id"],
            str(other_attempt.id),
        )

    def test_selected_deployment_never_reuses_another_deployments_freshness(self):
        imported = self._import()
        version_id = imported.data["id"]
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )
        self.assertEqual(validated.status_code, 200, validated.data)

        other_deployment = ModelDeployment.objects.create(
            key="acceptance-selected-other",
            name="Acceptance selected other",
            profile_version=self.deployment.profile_version,
            runtime_config=self.deployment.runtime_config,
            runtime_config_schema_version=(
                self.deployment.runtime_config_schema_version
            ),
            runtime_config_size_bytes=self.deployment.runtime_config_size_bytes,
            runtime_config_sha256=self.deployment.runtime_config_sha256,
            status=ModelDeployment.Status.DISABLED,
        )
        before = self.client.get(
            "/api/v1/acceptance-smoke/",
            {
                "subject": "deployment",
                "deployment_id": str(other_deployment.id),
            },
        )
        self.assertEqual(before.status_code, 200, before.data)
        before_summary = before.data["results"][0][
            "workflow_association_summary"
        ]

        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        other_attempt = self._workflow_attempt(version, other_deployment)
        filtered = self.client.get(
            "/api/v1/acceptance-smoke/",
            {
                "subject": "workflow",
                "workflow_id": version_id,
                "deployment_id": str(other_deployment.id),
            },
        )

        self.assertEqual(filtered.status_code, 200, filtered.data)
        row = filtered.data["results"][0]
        self.assertEqual(row["deployment"]["id"], str(other_deployment.id))
        self.assertEqual(row["association"]["level"], "executed")
        self.assertFalse(row["association"]["validated_deployment_matches"])
        self.assertFalse(row["validation"]["fresh"])
        self.assertEqual(
            row["validation"]["state"],
            "not_current_for_selected_deployment",
        )
        self.assertEqual(
            row["validation"]["validated_deployment_id"],
            str(self.deployment.id),
        )
        self.assertEqual(
            row["smoke"]["latest_attempt"]["id"],
            str(other_attempt.id),
        )
        self.assertIn("evidence_deployment_id", row["smoke"])
        smoke_action = next(
            action for action in row["actions"]
            if action["kind"] == "workflow_smoke"
        )
        self.assertFalse(smoke_action["enabled"])
        self.assertEqual(
            smoke_action["reason_code"],
            "workflow_not_validated_for_selected_deployment",
        )

        after = self.client.get(
            "/api/v1/acceptance-smoke/",
            {
                "subject": "deployment",
                "deployment_id": str(other_deployment.id),
            },
        )
        self.assertEqual(after.status_code, 200, after.data)
        after_summary = after.data["results"][0][
            "workflow_association_summary"
        ]
        self.assertEqual(after_summary["smoke_history_count"], 1)
        self.assertEqual(
            after_summary["compatible_only_count"],
            before_summary["compatible_only_count"] - 1,
        )

    def test_unresolved_workflow_smoke_is_blocked_and_has_resolution_action(self):
        imported = self._import()
        version_id = imported.data["id"]
        driver = Mock()
        driver.invoke.return_value = self._canvas_object_info()
        with patch(
            "studio.services.workflow_versions.driver_for",
            return_value=driver,
        ):
            validated = self.client.post(
                f"/api/v1/workflow-template-versions/{version_id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )
        self.assertEqual(validated.status_code, 200, validated.data)

        version = WorkflowTemplateVersion.objects.get(pk=version_id)
        attempt = self._workflow_attempt(version, self.deployment)
        attempt.status = WorkflowSmokeAttempt.Status.UNCERTAIN
        attempt.save(update_fields=("status", "updated_at"))

        response = self.client.get(
            "/api/v1/acceptance-smoke/",
            {
                "subject": "workflow",
                "workflow_id": version_id,
                "deployment_id": str(self.deployment.id),
            },
        )

        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["results"][0]
        self.assertTrue(row["smoke"]["latest_attempt"]["resolution_required"])
        smoke_action = next(
            action for action in row["actions"]
            if action["kind"] == "workflow_smoke"
        )
        self.assertFalse(smoke_action["enabled"])
        self.assertEqual(
            smoke_action["reason_code"],
            "workflow_smoke_resolution_required",
        )
        resolve_action = next(
            action for action in row["actions"]
            if action["kind"] == "resolve_workflow_smoke_uncertain"
        )
        self.assertTrue(resolve_action["enabled"])
        self.assertEqual(resolve_action["method"], "POST")
        self.assertEqual(
            resolve_action["url"],
            f"/api/v1/workflow-smoke-attempts/{attempt.id}/resolve-uncertain/",
        )

    def test_failed_health_snapshot_is_current_history_but_not_fresh_pass(self):
        snapshot = ModelDeploymentCapabilitySnapshot(
            deployment=self.deployment,
            revision=1,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.HEALTH,
            status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            target_sha256="a" * 64,
            provider_config_sha256="b" * 64,
            deployment_config_sha256="c" * 64,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        assign_json_contract(snapshot, "operations", [])
        assign_json_contract(snapshot, "evidence", {})

        summary = safe_snapshot_summary(
            snapshot,
            current_target_sha256="a" * 64,
        )

        self.assertIsNotNone(summary)
        self.assertTrue(summary["target_matches"])
        self.assertFalse(summary["fresh"])

    def test_resolved_uncertain_deployment_attempt_does_not_block_retry(self):
        attempt = ModelDeploymentSmokeAttempt.objects.create(
            deployment=self.deployment,
            operation="script.generate",
            status=ModelDeploymentSmokeAttempt.Status.UNCERTAIN,
            request_sha256="a" * 64,
            target_sha256="b" * 64,
        )
        resolution = ModelDeploymentSmokeAttemptResolution(
            attempt=attempt,
            revision=1,
            outcome=(
                ModelDeploymentSmokeAttemptResolution.Outcome.CONFIRMED_REMOTE_FAILED
            ),
            decided_by=self.admin,
        )
        assign_json_contract(resolution, "evidence", {})
        resolution.save()

        with patch(
            "studio.services.acceptance_smoke.deployment_smoke_operations",
            return_value=("script.generate",),
        ), patch("studio.services.deployment_smoke_profiles.smoke_profile_choices", return_value=[]):
            row = deployment_acceptance_row(
                self.deployment,
                health_snapshot=None,
                smoke_snapshots=(),
                latest_attempts=(attempt,),
                profile_match_count=0,
                profile_evidence_match_count=0,
                validated_count=0,
                smoke_history_count=0,
                fresh_validation_count=0,
                drifted_validation_count=0,
                fresh_smoke_count=0,
                unresolved_smoke_count=0,
            )

        operation = row["deployment_smoke"]["operations"][0]
        self.assertFalse(operation["latest_attempt"]["resolution_required"])
        self.assertTrue(operation["action"]["enabled"])

    def test_invalid_contract_and_target_are_explicit_and_keep_health_history(self):
        snapshot = ModelDeploymentCapabilitySnapshot(
            deployment=self.deployment,
            revision=1,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.HEALTH,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            target_sha256="",
            provider_config_sha256="b" * 64,
            deployment_config_sha256="c" * 64,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        assign_json_contract(snapshot, "operations", [])
        assign_json_contract(snapshot, "evidence", {})
        counts = {
            "profile_match_count": 0,
            "profile_evidence_match_count": 0,
            "validated_count": 0,
            "smoke_history_count": 0,
            "fresh_validation_count": 0,
            "drifted_validation_count": 0,
            "fresh_smoke_count": 0,
            "unresolved_smoke_count": 0,
        }

        with patch(
            "studio.services.acceptance_smoke.verified_model_profile_specification",
            side_effect=ValidationError("invalid contract"),
        ):
            invalid_contract = deployment_acceptance_row(
                self.deployment,
                health_snapshot=snapshot,
                smoke_snapshots=(),
                latest_attempts=(),
                **counts,
            )

        self.assertEqual(
            invalid_contract["deployment"]["profile_contract_reason_code"],
            "model_profile_contract_invalid",
        )
        self.assertEqual(
            invalid_contract["deployment_smoke"]["reason_code"],
            "model_profile_contract_invalid",
        )
        self.assertIsNotNone(invalid_contract["health"])
        self.assertFalse(invalid_contract["health"]["target_matches"])
        self.assertFalse(invalid_contract["health"]["fresh"])

        with patch(
            "studio.services.acceptance_smoke.deployment_target_sha256",
            side_effect=ValidationError("invalid target"),
        ):
            invalid_target = deployment_acceptance_row(
                self.deployment,
                health_snapshot=snapshot,
                smoke_snapshots=(),
                latest_attempts=(),
                **counts,
            )

        self.assertEqual(
            invalid_target["deployment"]["profile_contract_state"],
            "current",
        )
        self.assertEqual(
            invalid_target["deployment"]["target_reason_code"],
            "deployment_target_invalid",
        )
        self.assertEqual(
            invalid_target["deployment_smoke"]["reason_code"],
            "deployment_target_invalid",
        )

    def test_comfyui_deployment_routes_smoke_through_workflow(self):
        response = self.client.get(
            "/api/v1/acceptance-smoke/",
            {"subject": "deployment", "deployment_id": str(self.deployment.id)},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertEqual(row["deployment"]["provider_driver"], "comfyui")
        self.assertFalse(row["deployment_smoke"]["applicable"])
        self.assertEqual(row["deployment_smoke"]["reason_code"], "use_workflow_smoke")
        action_kinds = {item["kind"] for item in row["actions"]}
        self.assertIn("runtime_host_probe", action_kinds)
        self.assertIn("view_workflow_associations", action_kinds)
        self.assertNotIn("deployment_probe", action_kinds)
        associations_action = next(
            item for item in row["actions"]
            if item["kind"] == "view_workflow_associations"
        )
        self.assertEqual(
            associations_action["ui_url"],
            (
                "/settings/acceptance-smoke?subject=workflow"
                f"&deployment_id={self.deployment.id}"
            ),
        )

    def test_direct_smoke_retains_each_operations_expiry_and_latest_task(self):
        now = timezone.now()
        operations = (
            "prompt.normalize", "script.generate", "visual_rules.generate",
            "script.structure", "video.text_to_video", "audio.text_to_audio",
            "image.text_to_image",
        )
        snapshots = []
        for revision, (operation, state, target, expires, submitted) in enumerate((
            ("prompt.normalize", "passed", "a", now - timedelta(minutes=1), True),
            ("script.generate", "passed", "a", now, True),
            ("visual_rules.generate", "passed", "a", now + timedelta(hours=1), True),
            ("script.structure", "failed", "a", now + timedelta(hours=1), True),
            ("video.text_to_video", "passed", "b", now + timedelta(hours=1), True),
            ("image.text_to_image", "passed", "a", now + timedelta(hours=1), False),
        ), 1):
            snapshot = ModelDeploymentCapabilitySnapshot(
                deployment=self.deployment, revision=revision, probe_kind="smoke",
                status=state, target_sha256=target * 64,
                provider_config_sha256="p" * 64, deployment_config_sha256="d" * 64,
                captured_at=now - timedelta(hours=2), expires_at=expires,
            )
            assign_json_contract(snapshot, "operations", [operation])
            assign_json_contract(snapshot, "evidence", {"execution_submitted": submitted})
            snapshot.save()
            snapshots.append(snapshot)
        queued = ModelDeploymentSmokeAttempt.objects.create(
            deployment=self.deployment, operation="visual_rules.generate", status="queued",
            request_sha256="r" * 64, target_sha256="a" * 64,
        )
        with patch("studio.services.acceptance_smoke.deployment_smoke_operations", return_value=operations), patch(
            "studio.services.acceptance_smoke.validate_deployment_execution_contract",
        ), patch("studio.services.acceptance_smoke.deployment_target_sha256", return_value="a" * 64), patch(
            "studio.platform_views.deployment_target_sha256", return_value="a" * 64,
        ), patch("studio.services.acceptance_smoke.timezone.now", return_value=now), patch(
            "studio.services.deployment_smoke_profiles.smoke_profile_choices", return_value=[],
        ), CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/v1/acceptance-smoke/", {
                "subject": "deployment", "deployment_id": str(self.deployment.id),
            })
        self.assertEqual(response.status_code, 200, response.data)
        rows = {item["operation"]: item for item in response.data["results"][0]["deployment_smoke"]["operations"]}
        self.assertEqual(set(rows), set(operations))
        self.assertEqual(rows["prompt.normalize"]["evidence_state"], "unverified", "unscoped history does not certify a strategy")
        self.assertEqual(rows["script.generate"]["evidence_state"], "unverified")
        self.assertEqual(rows["script.structure"]["evidence_state"], "failed")
        self.assertEqual(rows["video.text_to_video"]["evidence_state"], "drifted")
        self.assertEqual(rows["audio.text_to_audio"]["evidence_state"], "missing")
        self.assertEqual(rows["image.text_to_image"]["evidence_state"], "unverified")
        self.assertEqual(rows["visual_rules.generate"]["evidence_state"], "unverified")
        self.assertEqual(rows["visual_rules.generate"]["latest_attempt"]["id"], str(queued.id))
        self.assertEqual(rows["visual_rules.generate"]["latest_attempt"]["status"], "queued")
        self.assertFalse(rows["visual_rules.generate"]["action"]["enabled"])
        self.assertIsNone(rows["script.generate"]["fresh_passed_snapshot"])
        self.assertEqual(rows["script.generate"]["latest_snapshot"]["id"], str(snapshots[1].id))
        self.assertIn("expires_at", rows["script.generate"]["latest_snapshot"])
        self.assertNotIn("_execution_confirmed", rows["script.generate"]["latest_snapshot"])
        if connection.vendor == "sqlite":
            snapshot_sql = next(
                q["sql"] for q in queries
                if "ROW_NUMBER()" in q["sql"] and "operations_sha256" in q["sql"]
            )
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + snapshot_sql)
                plan = " ".join(str(row[-1]) for row in cursor.fetchall())
            self.assertIn("INDEX", plan)
            self.assertIn("deployment_id", plan)

    def test_page_size_is_capped_at_two_hundred(self):
        request = Request(APIRequestFactory().get(
            "/api/v1/acceptance-smoke/",
            {"page_size": 500},
        ))
        self.assertEqual(AcceptanceSmokePagination().get_page_size(request), 200)

    def test_deployment_projection_query_count_is_bounded_by_page_not_rows(self):
        for index in range(4):
            ModelDeployment.objects.create(
                key=f"acceptance-query-bound-{index}",
                name=f"Acceptance query bound {index}",
                profile_version=self.deployment.profile_version,
                runtime_config=self.deployment.runtime_config,
                runtime_config_schema_version=(
                    self.deployment.runtime_config_schema_version
                ),
                runtime_config_size_bytes=(
                    self.deployment.runtime_config_size_bytes
                ),
                runtime_config_sha256=self.deployment.runtime_config_sha256,
                status=ModelDeployment.Status.DISABLED,
            )

        with CaptureQueriesContext(connection) as one_row_queries:
            one_row = self.client.get(
                "/api/v1/acceptance-smoke/",
                {"subject": "deployment", "page_size": 1},
            )
        with CaptureQueriesContext(connection) as five_row_queries:
            five_rows = self.client.get(
                "/api/v1/acceptance-smoke/",
                {"subject": "deployment", "page_size": 5},
            )

        self.assertEqual(one_row.status_code, 200, one_row.data)
        self.assertEqual(five_rows.status_code, 200, five_rows.data)
        self.assertEqual(len(one_row.data["results"]), 1)
        self.assertEqual(len(five_rows.data["results"]), 5)
        self.assertLessEqual(
            len(five_row_queries),
            len(one_row_queries) + 2,
        )

    def test_deployment_projection_avoids_per_workflow_configuration_queries(self):
        def create_historical_pass():
            imported = self._import()
            self.assertEqual(imported.status_code, 201, imported.data)
            WorkflowTemplateVersion.objects.filter(pk=imported.data["id"]).update(
                status=WorkflowTemplateVersion.Status.VALIDATED,
                validation_state=WorkflowTemplateVersion.ValidationState.PASSED,
                validated_deployment=self.deployment,
            )

        create_historical_pass()
        with CaptureQueriesContext(connection) as one_workflow_queries:
            one_workflow = self.client.get(
                "/api/v1/acceptance-smoke/",
                {
                    "subject": "deployment",
                    "deployment_id": str(self.deployment.id),
                },
            )

        for _ in range(5):
            create_historical_pass()
        with CaptureQueriesContext(connection) as six_workflow_queries:
            six_workflows = self.client.get(
                "/api/v1/acceptance-smoke/",
                {
                    "subject": "deployment",
                    "deployment_id": str(self.deployment.id),
                },
            )
        with patch.object(
            AcceptanceSmokeView,
            "related_workflow_batch_size",
            2,
        ):
            multi_batch = self.client.get(
                "/api/v1/acceptance-smoke/",
                {
                    "subject": "deployment",
                    "deployment_id": str(self.deployment.id),
                },
            )

        self.assertEqual(one_workflow.status_code, 200, one_workflow.data)
        self.assertEqual(six_workflows.status_code, 200, six_workflows.data)
        self.assertEqual(multi_batch.status_code, 200, multi_batch.data)
        self.assertEqual(
            six_workflows.data["results"][0]["workflow_association_summary"][
                "drifted_validation_count"
            ],
            one_workflow.data["results"][0]["workflow_association_summary"][
                "drifted_validation_count"
            ] + 5,
        )
        self.assertLessEqual(
            len(six_workflow_queries),
            len(one_workflow_queries) + 2,
        )
        self.assertEqual(
            multi_batch.data["results"][0]["workflow_association_summary"],
            six_workflows.data["results"][0]["workflow_association_summary"],
        )
