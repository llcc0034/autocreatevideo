"""Opt-in M8 full parent/API/worker bridge. Test-only, no production entry.

Requires -B, disabled dotenv and explicit in-memory SQLite. Seeds new M7 worker
artifacts using synthetic transport, then a real missing-file assembly failure.
Only the isolated Run's retry and local encoder can write after seeding.
"""
import json
import os
import sys
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from studio.tests.m7_page_bridge import finish_fixture, instance_cleanups, class_cleanups


@contextmanager
def isolated_case(runner, case_class):
    cleanups = []
    try:
        runner.setup_test_environment()
        cleanups.insert(0, ("media", runner.teardown_test_environment))
        database = runner.setup_databases()
        cleanups.insert(0, ("database", lambda: runner.teardown_databases(database)))
        # setUpClass may register guards and then fail before opening a transaction.
        cleanups.insert(0, ("class guards", lambda: class_cleanups(case_class)))
        case_class.setUpClass()
        cleanups.insert(0, ("class transaction", case_class.tearDownClass))
        case = case_class()
        cleanups.insert(0, ("instance guards", lambda: instance_cleanups(case)))
        case._pre_setup()
        cleanups.insert(1, ("test transaction", case._post_teardown))
        case.setUp()
        yield case
    finally:
        finish_fixture(cleanups)


def main():
    if (not sys.dont_write_bytecode or os.environ.get("PYTHON_DOTENV_DISABLED") != "1"
            or os.environ.get("DATABASE_URL") != "sqlite:///:memory:"):
        raise SystemExit("Explicit -B / disabled dotenv / in-memory SQLite required")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "autoprompt_backend.settings")
    import django
    django.setup()
    from django.core.serializers.json import DjangoJSONEncoder
    from autoprompt_backend.test_runner import IsolatedMediaDiscoverRunner
    from studio.models import AssemblyJob, GenerationAttempt, ReleaseBundle
    from studio.tests.test_m8_full_delivery_journey import M8FullDeliveryJourneyTests

    runner = IsolatedMediaDiscoverRunner(verbosity=0, interactive=False)
    with isolated_case(runner, M8FullDeliveryJourneyTests) as case:
        case.prepare_failed_episode()
        api_log, workers = [], []
        stop = False
        identity = {"scenario": "m8-delivery", "isolated": True,
            "project_id": str(case.project.pk), "run_id": str(case.run.pk)}

        def state():
            case.run.refresh_from_db()
            return {**identity, "status": case.run.status, "stage": case.run.current_stage_key,
                "attempt_count": GenerationAttempt.objects.filter(stage_run__production_run=case.run).count(),
                "jobs": list(AssemblyJob.objects.order_by("created_at").values("id", "status", "episode_id", "consumer_input_sha256")),
                "bundles": list(ReleaseBundle.objects.filter(production_run=case.run).values_list("id", flat=True)),
                "worker_results": workers, "api_log": api_log}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def raw_reply(self, status, raw, headers=None):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    if key.lower() not in ("content-length", "connection", "transfer-encoding"):
                        self.send_header(key, value)
                self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:5311")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type,Idempotency-Key")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers(); self.wfile.write(raw)

            def reply(self, status, value):
                self.raw_reply(status, json.dumps(value, cls=DjangoJSONEncoder, ensure_ascii=False).encode(),
                    {"Content-Type": "application/json; charset=utf-8"})

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
                    if origin and origin != "http://127.0.0.1:5311":
                        return self.reply(403, {"detail": "Only isolated local frontend allowed"})
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= size <= 1048576:
                        return self.reply(413, {"detail": "Oversized test request"})
                    body = json.loads(self.rfile.read(size)) if size else {}
                    path = urlsplit(self.path).path
                    if path == "/__fixture__/state" and self.command == "GET":
                        return self.reply(200, state())
                    if path == "/__fixture__/worker" and self.command == "POST":
                        result = case.execute()
                        case.assertEqual(result.status, "succeeded", result.error_message)
                        workers.append({"id": str(result.pk), "status": result.status})
                        return self.reply(200, state())
                    if path == "/__fixture__/check" and self.command == "GET":
                        case.assert_sealed_delivery()
                        return self.reply(200, {**state(), "verified_downloads": case.download_facts, "document": case.document})
                    if path == "/__fixture__/stop" and self.command == "POST":
                        stop = True
                        return self.reply(200, state())
                    if not path.startswith("/api/v1/") or ".." in path:
                        return self.reply(404, {"detail": "Outside isolated API"})
                    if self.command == "POST" and path != f"/api/v1/production-runs/{case.run.pk}/retry/":
                        return self.reply(403, {"detail": "Only exact isolated recipe retry allowed"})
                    headers = {"HTTP_HOST": f"127.0.0.1:{server.server_port}"}
                    if self.headers.get("Range"):
                        headers["HTTP_RANGE"] = self.headers["Range"]
                    response = (case.client.get(self.path, **headers) if self.command == "GET" else
                        case.client.post(self.path, body, format="json", **headers))
                    api_log.append({"method": self.command, "path": path, "status": response.status_code,
                        "retry_context": body if self.command == "POST" else None})
                    if getattr(response, "streaming", False):
                        try:
                            raw = b"".join(response.streaming_content)
                        finally:
                            if not response.closed:
                                response.close()
                    else:
                        if hasattr(response, "render"):
                            response.render()
                        raw = response.content
                    self.raw_reply(response.status_code, raw, dict(response.items()))
                except Exception as exc:
                    import traceback
                    traceback.print_exc()
                    self.reply(500, {"detail": str(exc), "isolated_fixture_error": True})

        server = HTTPServer(("127.0.0.1", 0), Handler); server.timeout = 1
        print(json.dumps({"ready": True, "port": server.server_port, **identity}), flush=True)
        try:
            deadline = time.monotonic() + 1800
            while not stop and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
