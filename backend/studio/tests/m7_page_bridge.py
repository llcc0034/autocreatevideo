"""Opt-in M7 page/API/worker test bridge; NEVER a platform business entry.

Run from backend with python -B, disabled dotenv and DATABASE_URL=sqlite:///:memory:.
Only a temporary loopback server is exposed. Real Django APIs and workers use
synthetic test records and temporary media; remote protocol/credentials stay denied.
"""
import argparse
from copy import deepcopy
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit
from unittest.mock import patch


def finish_fixture(cleanups):
    """Run every teardown, then fail loudly even when unittest swallowed errors."""
    failures = []
    for name, cleanup in cleanups:
        try:
            cleanup()
        except Exception as exc:
            failures.append(f"{name}: {exc}")
    if failures:
        raise AssertionError("Fixture cleanup failed: " + "; ".join(failures))


def instance_cleanups(case):
    if not case.doCleanups():
        raise AssertionError("unittest instance cleanup assertion failed")


def class_cleanups(case_class):
    case_class.doClassCleanups()
    if case_class.tearDown_exceptions:
        raise AssertionError("; ".join(str(exc) for _, exc, _ in case_class.tearDown_exceptions))


def retain_active_guards(case, guards):
    """Assert the inner mocks actually used by a fixture, not only outer patches."""
    for guard in guards:
        case.addCleanup(guard.assert_not_called)
    return lambda: [guard.assert_not_called() for guard in guards]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("manual", "manual-resource", "asset-correction", "stage-retry"), default="manual")
    scenario = parser.parse_args().scenario
    if (not sys.dont_write_bytecode or os.environ.get("PYTHON_DOTENV_DISABLED") != "1"
            or os.environ.get("DATABASE_URL") != "sqlite:///:memory:"):
        raise SystemExit("Explicit -B / disabled dotenv / in-memory database required")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "autoprompt_backend.settings")
    import django
    django.setup()
    from django.core.serializers.json import DjangoJSONEncoder
    from autoprompt_backend.test_runner import IsolatedMediaDiscoverRunner
    from studio.models import (ApprovalDecision, ArtifactSelectionDecision, Asset, AssetGenerationJob,
        ExecutionCapacityObservation, ExecutionTextResponseReview, ExecutionPreparation,
        GeneratedArtifact, GenerationAttempt, ProductionManualCommand)
    from studio.services.provider_registry import ProviderDriverError
    from studio.tests.test_m7_recovery_journeys import ManualRecoveryJourneyTests
    from studio.tests.test_asset_text_response_correction import AssetTextResponseCorrectionTests
    from studio.tests.test_m7_diagnostic_stages import IsolatedDiagnosticReads
    from studio.tests.test_m7_stage_recovery_journey import StageRecoveryJourneyTests

    class AssetCorrectionBridge(IsolatedDiagnosticReads, AssetTextResponseCorrectionTests):
        pass

    case_class = {"asset-correction": AssetCorrectionBridge, "manual": ManualRecoveryJourneyTests,
        "manual-resource": ManualRecoveryJourneyTests,
        "stage-retry": StageRecoveryJourneyTests}[scenario]

    runner = IsolatedMediaDiscoverRunner(verbosity=0, interactive=False)
    runner.setup_test_environment()
    database = runner.setup_databases()
    case = None
    server = None
    class_ready = False
    try:
        case_class.setUpClass()
        class_ready = True
        case = case_class()
        case._pre_setup()
        case.setUp()
        from studio.services import provider_registry
        active_guards = [provider_registry.resolve_credential]
        if scenario == "asset-correction":
            active_guards.append(provider_registry._request_json)
        check_active_guards = retain_active_guards(case, active_guards)
        if scenario == "asset-correction":
            failed = case.failed()  # Original independent worker; only its protocol reply is synthetic.
            prep = case.prep
            review_snapshot = deepcopy({key: getattr(case.row, key) for key in case.row._SNAPSHOT_FIELDS})
            original_text = case.row.original_text  # Verified file read, including NUL and CRLF.
            original = deepcopy(case.client.get(f"/api/v1/task-center/preparation/{prep.pk}/").data)
            request_hash = prep.request_sha256
            deny_driver = case.enterContext(patch("studio.services.asset_generation.driver_for",
                side_effect=AssertionError("A03 correction must not call a model")))
            case.addCleanup(deny_driver.assert_not_called)
            identity = {"scenario": scenario, "job_id": str(failed.pk), "preparation_id": str(prep.pk)}
            allowed_posts = {f"/api/v1/execution-preparations/{prep.pk}/complete-text-response/"}
        elif scenario == "stage-retry":
            failed, good = case.failed_stage()  # Same project unit, two real candidate attempts.
            prep = failed.preparation
            original = case.evidence(prep, failed)
            request_hash = failed.request_sha256
            good_snapshot = deepcopy(GenerationAttempt.objects.values().get(pk=good.pk))
            prep_ids = set(ExecutionPreparation.objects.filter(stage_run=case.stage).values_list("id", flat=True))
            identity = {"scenario": scenario, "project_id": str(case.project.pk), "run_id": str(case.run.pk),
                "stage_id": str(case.stage.pk), "preparation_id": str(prep.pk), "successful_attempt": str(good.pk)}
            allowed_posts = {f"/api/v1/production-runs/{case.run.pk}/retry/"}
        else:
            prep, failed = case.failed()  # Real preparation + worker with synthetic failure reply.
            if scenario == "manual-resource":
                case.policy(calls=1)
                # Seed first; from here on the original frozen request must be reused.
                for target in ("studio.services.execution_preparation_requests.compile_prepared_execution_transport",
                        "studio.services.execution_parameter_preparation.randbelow"):
                    guard = case.enterContext(patch(target, side_effect=AssertionError("Frozen request changed")))
                    active_guards.append(guard)
                    case.addCleanup(guard.assert_not_called)
            original = case.evidence(prep, failed)
            request_hash = failed.request_sha256
            identity = {"scenario": scenario, "project_id": str(case.project.pk), "run_id": str(case.run.pk),
                "stage_id": str(case.stage.pk), "preparation_id": str(prep.pk)}
            allowed_posts = {
                f"/api/v1/production-runs/{case.run.pk}/manual-actions/",
                f"/api/v1/execution-preparations/{prep.pk}/cancel/",
                f"/api/v1/production-runs/{case.run.pk}/cancel/",
            }
            if scenario == "manual-resource":
                allowed_posts.add(f"/api/v1/execution-preparations/{prep.pk}/confirm-resources/")
        initial_artifacts = list(GeneratedArtifact.objects.values())
        calls = []
        api_log = []
        stop = False
        def evidence():
            check_active_guards()
            failed.refresh_from_db()
            if scenario == "asset-correction":
                prep.refresh_from_db(); case.row.refresh_from_db()
                detail = case.client.get(f"/api/v1/task-center/preparation/{prep.pk}/")
                case.assertEqual(detail.status_code, 200)
                case.assertEqual(detail.data["source_id"], str(failed.pk))
                case.assertEqual(detail.data["input"], original["input"])
                case.assertEqual(prep.request_sha256, request_hash)
                case.assertEqual({key: getattr(case.row, key) for key in review_snapshot}, review_snapshot)
                case.assertEqual(case.row.original_text, original_text)
                case.assertEqual(failed.remote_status, "response_rejected")
                case.assertIsNone(failed.actual_cost)
                case.driver.invoke.assert_called_once()
                deny_driver.assert_not_called()
                counts = {model.__name__: model.objects.count() for model in (AssetGenerationJob,
                    ExecutionTextResponseReview, GenerationAttempt, GeneratedArtifact, Asset,
                    ExecutionCapacityObservation, ApprovalDecision, ArtifactSelectionDecision, ProductionManualCommand)}
                case.assertEqual(counts, {"AssetGenerationJob": 1, "ExecutionTextResponseReview": 1,
                    "GenerationAttempt": 0, "GeneratedArtifact": 0, "Asset": 0, "ExecutionCapacityObservation": 0,
                    "ApprovalDecision": 0, "ArtifactSelectionDecision": 0, "ProductionManualCommand": 0})
                return {"isolated": True, "counts": counts, "job_status": failed.status,
                    "remote_status": failed.remote_status, "result_text": failed.result_text,
                    "original_request_sha256": request_hash, "original_input_preserved": True,
                    "original_text": original_text, "original_sha256": case.row.original_sha256,
                    "review_id": str(case.row.pk), "corrected_text": case.row.corrected_text,
                    "confirmed_at": case.row.confirmed_at, "confirmed_by": case.row.confirmed_by_id,
                    "preparation_revision": prep.revision, "seed_revision": case.payload["expected_preparation_revision"],
                    "synthetic_seed_invocations": case.driver.invoke.call_count,
                    "model_invocations_after_seed": deny_driver.call_count, "api_log": api_log}
            case.assertEqual(failed.request_sha256, request_hash)
            case.assertEqual(case.evidence(prep, failed)["input"], original["input"])
            case.assertEqual(failed.status, "failed")
            case.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=case.run).exists())
            case.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run=case.stage).exists())
            if scenario == "stage-retry":
                case.assertEqual(GenerationAttempt.objects.values().get(pk=good.pk), good_snapshot)
                case.assertEqual(set(ExecutionPreparation.objects.filter(stage_run=case.stage).values_list("id", flat=True)), prep_ids)
                for artifact in initial_artifacts:
                    case.assertEqual(GeneratedArtifact.objects.values().get(pk=artifact["id"]), artifact)
                for successor in GenerationAttempt.objects.filter(preparation=prep).exclude(pk=failed.pk):
                    case.assertEqual(successor.request_snapshot, failed.request_snapshot)
                    case.assertEqual(successor.request_sha256, request_hash)
                    case.assertEqual(successor.retry_requested_by_id, case.actor.pk)
            else:
                case.assertEqual(initial_artifacts, [])
                for successor in GenerationAttempt.objects.filter(preparation=prep).exclude(pk=failed.pk):
                    case.assertEqual(successor.request_snapshot, failed.request_snapshot)
                    case.assertEqual(successor.request_sha256, request_hash)
            prep.refresh_from_db()
            return {
                "isolated": True, "original_attempt": str(failed.pk), "original_request_sha256": request_hash,
                "attempts": list(GenerationAttempt.objects.filter(stage_run=case.stage).order_by("attempt_number").values(
                    "id", "status", "error_code", "retry_of_id", "request_sha256")),
                "retry_commands": list(ProductionManualCommand.objects.filter(run=case.run, action="retry").values(
                    "id", "idempotency_key", "target_id")),
                "artifacts": list(GeneratedArtifact.objects.filter(production_run=case.run).values("id", "status")),
                "worker_invocations_after_seed": calls, "api_log": api_log,
                "original_input_preserved": True, "review_or_adoption_count": 0,
                "initial_artifact_ids": [str(row["id"]) for row in initial_artifacts],
                "successful_candidate_preserved": scenario == "stage-retry",
                "preparation": {"id": str(prep.pk), "revision": prep.revision, "status": prep.status,
                    "reason_code": prep.reason_code, "next_check_at": prep.next_check_at,
                    "evaluation_resource_review": prep.contract.evaluation.get("root_checks", {}).get("resource_review")},
            }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, data):
                raw = json.dumps(data, cls=DjangoJSONEncoder, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:5173")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_OPTIONS(self):
                self.reply(200, {})

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                nonlocal stop
                try:
                    origin = self.headers.get("Origin")
                    if origin and origin != "http://127.0.0.1:5173":
                        return self.reply(403, {"detail": "Only the local isolated page is allowed"})
                    size = int(self.headers.get("Content-Length", "0"))
                    if size > 1048576:
                        return self.reply(413, {"detail": "Fixture request too large"})
                    body = json.loads(self.rfile.read(size)) if size else {}
                    path = urlsplit(self.path).path
                    if path == "/__fixture__/state" and self.command == "GET":
                        return self.reply(200, {**identity, **evidence()})
                    if path == "/__fixture__/worker" and self.command == "POST":
                        if scenario not in ("manual", "manual-resource", "stage-retry"):
                            return self.reply(403, {"detail": "No worker control in asset correction scenario"})
                        mode = body.get("mode")
                        modes = ("success", "failure") if scenario == "stage-retry" else ("success", "unknown", "cancelled", "late")
                        if mode not in modes:
                            return self.reply(400, {"detail": "Explicit synthetic protocol mode required"})
                        from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
                        from unittest.mock import Mock, patch
                        claim = claim_next_text_attempt("isolated-page-bridge")
                        if claim is None:
                            return self.reply(409, {"detail": "No queued synthetic attempt"})
                        peer = Mock()
                        case.operation_plan = claim.request_snapshot["transport"]["operation_plan"]["plan"]
                        response = {**case.response(), "actual_cost": "0.03", "currency": "USD"}
                        if mode == "failure":
                            response["text"] = None
                        if mode == "cancelled":
                            case.cancel(prep)
                        if mode == "unknown":
                            peer.invoke.side_effect = ProviderDriverError("isolated lost response", submission_uncertain=True)
                        elif mode == "late":
                            def late(*_):
                                case.cancel(prep)
                                return response
                            peer.invoke.side_effect = late
                        else:
                            peer.invoke.return_value = response
                        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
                            result = process_text_attempt(claim)
                        if scenario == "stage-retry":
                            from studio.services.production_runs import advance_production_run
                            case.run = advance_production_run(case.run)
                        calls.append({"attempt": str(result.pk), "mode": mode, "invoke_count": peer.invoke.call_count})
                        return self.reply(200, evidence())
                    if path == "/__fixture__/stop" and self.command == "POST":
                        stop = True
                        return self.reply(200, evidence())
                    if not path.startswith("/api/v1/") or ".." in path:
                        return self.reply(404, {"detail": "Outside test API"})
                    # Real router/serializers; writes only to the selected synthetic owner.
                    if self.command == "POST" and (path not in allowed_posts or
                            (path.endswith("/manual-actions/") and body.get("action") != "retry")):
                        return self.reply(403, {"detail": "Write outside isolated recovery scope"})
                    response = (case.client.get(self.path) if self.command == "GET" else
                                case.client.post(self.path, body, format="json"))
                    api_log.append({"method": self.command, "path": self.path, "status": response.status_code,
                        "idempotency_key": body.get("idempotency_key"),
                        "expected_preparation_revision": (body.get("payload", {}) if path.endswith("/manual-actions/") else body).get("expected_preparation_revision"),
                        "expected_attempt_id": body.get("payload", {}).get("expected_attempt_id"),
                        "resource_fingerprint": body.get("fingerprint"),
                        "expected_stage_id": body.get("expected_stage_id"),
                        "expected_attempt_ids": body.get("expected_attempt_ids"),
                        "review_id": body.get("review_id"), "original_sha256": body.get("original_sha256")})
                    response.render()
                    self.reply(response.status_code, json.loads(response.content))
                except Exception as exc:
                    import traceback
                    traceback.print_exc()
                    self.reply(500, {"detail": str(exc), "isolated_fixture_error": True})

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = 1
        print(json.dumps({"ready": True, "port": server.server_port, **identity}), flush=True)
        deadline = time.monotonic() + 1800
        while not stop and time.monotonic() < deadline:
            server.handle_request()
    finally:
        cleanups = []
        if server:
            cleanups.append(("loopback server", server.server_close))
        if case:
            cleanups.extend((("instance guards", lambda: instance_cleanups(case)),
                ("test transaction", case._post_teardown)))
        if class_ready:
            cleanups.append(("class transaction", case_class.tearDownClass))
        cleanups.extend((("class guards", lambda: class_cleanups(case_class)),
            ("test database", lambda: runner.teardown_databases(database)),
            ("temporary media/environment", runner.teardown_test_environment)))
        finish_fixture(cleanups)


if __name__ == "__main__":
    main()
