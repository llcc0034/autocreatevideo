"""Native recipe source -> Bible approval -> declared storyboard writer.

Only peer transport is replaced. Raw creative fields keep their exact source
binding; this suite does not approve references or dispatch downstream media.
"""
from copy import deepcopy

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ApprovalDecision, BibleVariantReview, Episode, GeneratedArtifact, GenerationAttempt, Shot
from studio.services.production_runs import decide_stage
from studio.services.bible_content_review import recipe_bible_review_detail
from studio.services.script_storyboard_plan import read_storyboard_source_plan
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
from studio.tests import test_production_creation_inputs as creation_fixtures
from studio.tests import test_execution_production_dispatch as dispatch_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class RecipeContentChainTests(TestCase):
    setUpTestData = classmethod(creation_fixtures.ProductionCreationInputTests.setUpTestData.__func__)
    setUp = creation_fixtures.ProductionCreationInputTests.setUp
    profile_instance = creation_fixtures.ProductionCreationInputTests.profile_instance
    root = creation_fixtures.ProductionCreationInputTests.root
    receipt = creation_fixtures.ProductionCreationInputTests.receipt
    create_root = creation_fixtures.ProductionCreationInputTests.create_root
    structure_instance = dispatch_fixtures.ProductionDispatchTests.structure_instance
    pass_smoke = dispatch_fixtures.ProductionDispatchTests.pass_smoke
    advance = dispatch_fixtures.ProductionDispatchTests.advance
    response = dispatch_fixtures.ProductionDispatchTests.response
    process = dispatch_fixtures.ProductionDispatchTests.process

    def content_stages(self):
        return [{"key": "script_review", "mode": "review", "approval": "required"},
            {"key": "bible_materialize", "mode": "deterministic", "approval": "auto"},
            {"key": "bible_review", "mode": "review", "approval": "required"},
            {"key": "storyboard", "mode": "deterministic", "approval": "auto"},
            {"key": "storyboard_review", "mode": "review", "approval": "required"}]

    def test_generated_source_materializes_declared_storyboard_after_bible_review(self):
        values = {**self.values, "shots_per_episode": 1, "episode_duration_seconds": 5}
        self.candidate["episodes"][0]["shots"][0]["duration_ms"] = 5000
        self.fixture["inputs"] = {key: {"kind": "literal", "value": value} for key, value in values.items()}
        self.run = self.root(source=self.sources, variants=1, following=self.content_stages())
        self.pass_smoke()
        with self.captureOnCommitCallbacks(execute=True):
            self.run, created = self.create_root(allow_external_calls=True, **values)
        self.assertTrue(created)
        self.finish_content_chain(revision=None)

    def test_structured_source_materializes_declared_storyboard_after_bible_review(self):
        self.structure_instance(scopes=("project",), planning=True)
        self.profile_instance()
        _, preview = create_pasted_script_source(project=self.project, user=self.actor,
            content="旅人来到房间。\n旅人放下盒子。")
        revision = confirm_script_revision(preview, user=self.actor)
        values = {"idea": "", "style_hint": "", "episode_count": 1,
            "shots_per_episode": 1, "episode_duration_seconds": 5}
        self.candidate["episodes"][0]["shots"][0]["duration_ms"] = 5000
        counts = {key: values[key] for key in ("episode_count", "shots_per_episode", "episode_duration_seconds")}
        self.fixture["inputs"]["plan"] = {"kind": "literal", "value": counts}
        source = {"script": {"kind": "context_field", "selector_key": "script.content", "scope": {"kind": "project"}},
            "plan": {"kind": "structured", "template": {"kind": "object", "fields": {
                key: {"kind": "context_field", "selector_key": f"run.{key}", "scope": {"kind": "project"}} for key in counts}}}}
        self.run = self.root(stage_key="script_structure", source=source, variants=1,
            prior=[{"key": "script_input", "mode": "confirmed_revision", "approval": "auto"}],
            following=self.content_stages(),
            initial={"kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256})
        self.pass_smoke()
        with self.captureOnCommitCallbacks(execute=True):
            self.run, created = self.create_root(script_revision=revision, allow_external_calls=True, **values)
        self.assertTrue(created)
        self.finish_content_chain(revision=revision, planning=counts)

    def finish_content_chain(self, *, revision, planning=None):
        self.run.refresh_from_db()
        self.assertEqual(self.run.blocked_code, "waiting_external_task", self.run.blocked_reason)
        attempt = GenerationAttempt.objects.get(stage_run__production_run=self.run)
        operation_plan = attempt.request_snapshot["transport"]["operation_plan"]["plan"]
        if planning is not None:
            self.assertEqual(operation_plan["inputs"]["planning"], planning)
        else:
            self.assertEqual(operation_plan["inputs"]["idea"], self.values["idea"])
        frozen_request = deepcopy(attempt.request_snapshot)
        with self.captureOnCommitCallbacks(execute=True):
            result, peer = self.process()
        self.assertEqual(result.status, "succeeded", result.error_code)
        peer.invoke.assert_called_once()
        self.run.refresh_from_db()
        self.assertEqual((self.run.status, self.run.current_stage_key), ("awaiting_approval", "script_review"), self.run.blocked_reason)
        self.assertFalse(Episode.objects.exists())
        self.assertFalse(Shot.objects.exists())
        candidate = GeneratedArtifact.objects.get(generation_attempt=result)
        self.assertEqual(candidate.status, "available")
        initial_id = revision.pk if revision else None
        self.assertEqual(self.run.script_revision_id, initial_id)
        generated = self.run.stages.get(stage_key="script_structure" if planning is not None else "script_generate")
        self.run = decide_stage(self.run, stage_key="script_review", decision="approve", comment="Explicit candidate",
            user=self.actor, text_candidate={"artifact_id": str(candidate.pk), "expected_sha256": candidate.sha256},
            expected_source_snapshot_sha256=generated.output_snapshot_sha256)
        self.assertEqual(self.run.current_stage_key, "bible_review", self.run.blocked_reason)
        self.assertEqual(self.run.status, "awaiting_approval", self.run.blocked_reason)
        self.assertNotEqual(self.run.script_revision_id, initial_id)
        if revision is not None:
            revision.refresh_from_db()
            self.assertEqual(revision.content, "旅人来到房间。\n旅人放下盒子。")
        client = APIClient(); client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/production-runs/{self.run.pk}/bible-review/")
        self.assertEqual(response.status_code, 200, response.data)
        detail = response.data
        self.assertEqual(detail, recipe_bible_review_detail(self.run))
        confirmation = {"expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
            "expected_project_bible_sha256": detail["project_bible_sha256"], "reviewed_project_bible": True,
            "reviewed_candidate_ids": [row["id"] for row in detail["candidates"]],
            "reviewed_setting_keys": [row["review_key"] for row in detail["settings"]]}
        body = {"stage_key": "bible_review", "decision": "approve", "comment": "Explicit Bible", "bible_review": confirmation}
        for key, value in (("reviewed_setting_keys", []), ("reviewed_setting_keys", confirmation["reviewed_setting_keys"] * 2),
                ("expected_project_bible_sha256", "f" * 64), ("expected_source_snapshot_sha256", "f" * 64)):
            bad = deepcopy(body); bad["bible_review"][key] = value
            response = client.post(f"/api/v1/production-runs/{self.run.pk}/approve/", bad, format="json")
            self.assertIn(response.status_code, (400, 409), response.data)
            self.assertFalse(BibleVariantReview.objects.exists())
        response = client.post(f"/api/v1/production-runs/{self.run.pk}/approve/", body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()
        self.assertEqual(self.run.stages.get(stage_key="bible_review").status, "succeeded")
        self.assertEqual(BibleVariantReview.objects.count(), len(detail["settings"]))
        self.assertFalse(BibleVariantReview.objects.exclude(decided_by=self.actor).exists())
        source_plan = read_storyboard_source_plan(run=self.run, stage=self.run.stages.get(stage_key="storyboard"))
        self.assertEqual(source_plan.basis["script"]["id"], str(self.run.script_revision_id))
        self.assertEqual(source_plan.basis["bible_review"]["snapshot_sha256"],
            self.run.stages.get(stage_key="bible_review").output_snapshot_sha256)
        self.assertEqual(source_plan.episodes[0]["shots"][0]["body"]["action"],
            self.candidate["episodes"][0]["shots"][0]["action"])
        self.assertEqual(source_plan.episodes[0]["shots"][0]["relations"]["visible_characters"], [])
        self.assertEqual(len(source_plan.episodes[0]["shots"][0]["relations"]["uses"]),
            len(self.candidate["episodes"][0]["shots"][0]["references"]))
        self.assertEqual((self.run.status, self.run.current_stage_key, self.run.blocked_code),
            ("awaiting_approval", "storyboard_review", "stage_approval_required"), self.run.blocked_reason)
        from studio.services.script_storyboard_fields import read_shot_source_fields
        shots = Shot.objects.filter(episode__project=self.project).select_related("script_source")
        self.assertEqual(shots.count(), sum(len(episode["shots"]) for episode in self.candidate["episodes"]))
        for shot in shots:
            self.assertFalse({"narrative", "action", "detailed_description"} & {field.name for field in Shot._meta.get_fields()})
            self.assertTrue(read_shot_source_fields(shot_id=shot.pk, project_id=self.project.pk,
                max_bytes=1_000_000)["fields"])
            self.assertEqual(shot.script_source.structure_id, self.run.script_revision_id)
        self.assertEqual(ApprovalDecision.objects.filter(stage_run__production_run=self.run).count(), 2)
        attempt.refresh_from_db()
        self.assertEqual(attempt.request_snapshot, frozen_request)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run__production_run=self.run).count(), 1)
