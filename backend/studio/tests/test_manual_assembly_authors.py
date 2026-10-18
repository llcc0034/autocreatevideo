"""M8 manual author API -> original queue, isolated media; no model or assembly completion."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from studio.models import ArtifactSelectionDecision, AssemblyJob, BusinessOutputVersion, GeneratedArtifact, GenerationAttempt, GenerationTask, ProductionAssemblyLink, ProductionManualCommand
from studio.services.business_outputs import write_output
from studio.services.json_contracts import assign_json_contract
from studio.tests import test_production_manual_assembly as manual
from studio.tests import test_recipe_assembly_output as output
from studio.tests.output_structure_fixtures import response_structure
from studio.tests.test_structure_mapping_preview import field


class ManualAssemblyAuthorTests(TestCase):
    setUp = manual.ManualAssemblyTests.setUp
    mark_submitted = manual.ManualAssemblyTests.mark_submitted
    archive = manual.ManualAssemblyTests.archive
    resolve = manual.ManualAssemblyTests.resolve
    approve = manual.ManualAssemblyTests.approve
    detail = manual.ManualAssemblyTests.detail
    request = manual.ManualAssemblyTests.request
    post = manual.ManualAssemblyTests.post
    geometry = output.RecipeAssemblyOutputTests.rule

    def authors(self):
        geometry = self.geometry()
        timing = {"episodes": [{"number": self.episode.number, "shots": [{key: getattr(self.shot, key)
            for key in ("number", "duration_ms", "episode_start_ms", "episode_end_ms")}]}]}
        sample = deepcopy(timing)
        sample["episodes"][0]["shots"][0].update(episode_start_ms=0, episode_end_ms=2500)
        structure = response_structure(example={"时序": sample}, nullable_fields=("episode_start_ms", "episode_end_ms"))
        value = write_output(actor=self.user, data={"name": "Manual timing " + uuid4().hex,
            "parser_key": "structured_json.v1", "data_structure_version": str(structure.pk), "quality_advisories": {}})
        value = write_output(actor=self.user, output_id=value.pk, expected_revision=value.revision, action="publish")
        media = output.RecipeAssemblyOutputTests.rule(self, {"片段": {"episodes": [
            {"number": self.episode.number, "shots": [{"number": self.shot.number, "artifact_id": str(self.artifact.pk)}]}]},
            "保留": "  原文\r\n"}, field("片段"))
        return {"assembly_output": geometry, "assembly_media": media, "assembly_timing": {"output_version_id": str(value.pk),
            "expected_contract_sha256": value.contract_sha256, "source_text": json.dumps({"时序": timing}, ensure_ascii=False),
            "mapping": {"schema_version": 1, "node": field("时序")}}}

    def preview(self, authors, **extra):
        return self.client.post(self.read_url, {"expected_run_plan_sha256": self.plan.sha256, "authors": authors, **extra}, format="json")

    def authored_request(self, detail):
        body = self.request(detail)
        body["payload"]["authors"] = deepcopy(detail["authors"])
        return body

    def test_preview_preserves_exact_raw_values_without_snapshot_probe_or_enqueue(self):
        self.approve(); authors = self.authors()
        stage = self.run.stages.get(stage_key="assembly")
        before = deepcopy(stage.input_snapshot)
        attempts = GenerationAttempt.objects.count()
        with patch("studio.services.production_manual_assembly.create_assembly", side_effect=AssertionError("No preview enqueue")), patch(
                "studio.services.execution_production_candidates.read_production_media_candidate", side_effect=AssertionError("No preview probe")):
            result = self.preview(authors)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result["Cache-Control"], "private, no-store")
        self.assertEqual(result.data["authors"], authors)
        self.assertEqual(result.data["author_contracts"]["assembly_output"]["projection"]["raw_value"]["说明"], "  原值不清洗\r\n")
        self.assertEqual([result.data["output"][key] for key in ("width", "height", "fps")], [640, 360, 30])
        self.assertEqual(set(result.data["author_schemas"]), set(authors))
        stage.refresh_from_db(); self.assertEqual(stage.input_snapshot, before)
        self.assertFalse(AssemblyJob.objects.exists()); self.assertFalse(ProductionAssemblyLink.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.filter(action="assemble").exists())
        self.assertEqual(GenerationAttempt.objects.count(), attempts)

    def test_enqueue_replay_and_failed_retry_reuse_frozen_contract_after_retirement(self):
        self.approve(); authors = self.authors()
        preview = self.preview(authors); self.assertEqual(preview.status_code, 200, preview.data)
        body = self.authored_request(preview.data)
        first = self.post(body); self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(self.post(body).status_code, 200)
        job = AssemblyJob.objects.get(); self.assertEqual(job.source_items.get().duration_ms, self.shot.duration_ms)
        self.assertIn("640", " ".join(job.ffmpeg_command))
        frozen = deepcopy(self.run.stages.get(stage_key="assembly").input_snapshot)
        self.assertEqual(frozen["authors"], authors)
        BusinessOutputVersion.objects.filter(pk__in=[rule["output_version_id"] for rule in authors.values()]).update(status="retired")
        job.status = "failed"; job.save(update_fields=("status",))
        with patch("studio.services.assembly_author_contracts.output_reference", side_effect=AssertionError("No registry reread")), patch(
                "studio.services.production_manual_assembly.resolve_assembly_configuration", side_effect=AssertionError("No current config")):
            detail = self.detail()
            self.assertEqual(detail["contract_sha256"], preview.data["contract_sha256"])
            self.assertEqual(self.post(self.authored_request(detail)).status_code, 202)
        self.assertEqual(AssemblyJob.objects.count(), 2)
        self.assertEqual(self.run.stages.get(stage_key="assembly").input_snapshot, frozen)
        self.assertEqual(self.run.stages.get(stage_key="delivery").status, "pending")

    def test_invalid_author_scope_timing_and_permissions_do_not_write(self):
        self.approve(); authors = self.authors()
        self.assertEqual(self.preview(authors, expected_run_plan_sha256="b" * 64).status_code, 409)
        self.assertEqual(self.preview(authors, unrecognized=True).status_code, 400)
        self.assertEqual(self.preview(None).status_code, 400)
        self.assertEqual(self.preview({"surprise": authors["assembly_output"]}).status_code, 400)
        for change in ("duration", "missing", "extra"):
            bad = deepcopy(authors)
            value = json.loads(bad["assembly_timing"]["source_text"])
            if change == "duration": value["时序"]["episodes"][0]["shots"][0]["duration_ms"] += 1
            elif change == "missing": value["时序"]["episodes"] = []
            else: value["时序"]["episodes"].append({**value["时序"]["episodes"][0], "number": 2})
            bad["assembly_timing"]["source_text"] = json.dumps(value)
            self.assertEqual(self.preview(bad).status_code, 400)
        self.client.force_authenticate(get_user_model().objects.create_user(username="unrelated-author"))
        self.assertEqual(self.preview(authors).status_code, 404)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_changed_input_or_retired_version_between_preview_and_submit_rejects(self):
        self.approve(); authors = self.authors(); preview = self.preview(authors)
        self.assertEqual(preview.status_code, 200, preview.data)
        body = self.authored_request(preview.data)
        bad = deepcopy(body); bad["payload"]["authors"]["assembly_output"]["source_text"] += " "
        self.assertEqual(self.post(bad).status_code, 409)
        self.assertEqual(self.post(self.request(preview.data)).status_code, 409)
        BusinessOutputVersion.objects.filter(pk=authors["assembly_output"]["output_version_id"]).update(status="retired")
        self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(AssemblyJob.objects.exists()); self.assertFalse(ProductionManualCommand.objects.filter(action="assemble").exists())

    def test_frozen_corruption_even_rehashed_is_not_reconstructed(self):
        self.approve(); preview = self.preview(self.authors())
        self.assertEqual(self.post(self.authored_request(preview.data)).status_code, 202)
        stage = self.run.stages.get(stage_key="assembly"); original = deepcopy(stage.input_snapshot)
        replacement = deepcopy(original["authors"]); replacement["assembly_output"]["source_text"] += " "
        self.assertEqual(self.preview(replacement).status_code, 409)
        cases = []
        bad = deepcopy(original); bad.pop("author_contracts"); cases.append(bad)
        bad = deepcopy(original); bad["author_contracts"]["assembly_output"] = None; cases.append(bad)
        bad = deepcopy(original); bad["author_contracts"]["assembly_output"]["projection"]["raw_value"]["说明"] = "changed"; cases.append(bad)
        bad = deepcopy(original); bad["configuration"]["width"] = 800; cases.append(bad)
        bad = deepcopy(original); bad["episodes"][0]["shots"][0]["duration_ms"] += 1; cases.append(bad)
        for value in cases:
            assign_json_contract(stage, "input_snapshot", value)
            stage.save()
            with self.subTest(value=value), patch("studio.services.assembly_author_contracts.output_reference", side_effect=AssertionError("No repair")):
                self.assertEqual(self.client.get(self.read_url).status_code, 400)
        self.assertEqual(AssemblyJob.objects.count(), 1)

    def test_receipt_failure_rolls_back_authors_job_and_files(self):
        self.approve(); preview = self.preview(self.authors()); body = self.authored_request(preview.data)
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=RuntimeError("receipt failure")):
            with self.assertRaises(RuntimeError): self.post(body)
        self.assertFalse(AssemblyJob.objects.exists()); self.assertFalse(ProductionAssemblyLink.objects.exists())
        self.assertFalse(self.run.stages.get(stage_key="assembly").input_snapshot)
        self.assertEqual(list((Path(settings.MEDIA_ROOT) / "assemblies").rglob("*.ffconcat")), [])
        self.assertEqual(self.post(body).status_code, 202)

    def test_existing_video_adoption_media_author_and_original_queue_without_new_generation(self):
        from studio.tests.test_production_adopted_media import AdoptedMediaTests
        from studio.services.production_manual_media_review import manual_media_review_detail

        # Actual synthetic video bytes are encoded/probed only inside this test's temporary storage.
        # No encoder completion, provider, runtime service, or delivery success is fabricated.
        self.workspace, self.actor = settings.MEDIA_ROOT, self.user
        asset = AdoptedMediaTests.asset(self, "video")
        self.shot.media_type = "video"; self.shot.duration_ms = 1000
        self.shot.save(update_fields=("media_type", "duration_ms"))
        attempts, tasks = GenerationAttempt.objects.count(), GenerationTask.objects.count()
        adopted = self.post({"action": "adopt", "stage_key": "media_generation",
            "scope": {"kind": "shot", "id": str(self.shot.pk)}, "unit_key": f"shot:{self.shot.pk}",
            "expected_run_plan_sha256": self.plan.sha256, "expected_selection_revision": 0,
            "idempotency_key": str(uuid4()), "payload": {"source": {"kind": "asset", "asset_id": str(asset.pk),
                "expected_sha256": asset.checksum, "media_kind": "video"}}})
        self.assertEqual(adopted.status_code, 201, adopted.data)
        self.artifact = GeneratedArtifact.objects.get(production_run=self.run, role="adopted_media")
        self.assertEqual(self.artifact.asset_id, asset.pk)
        self.assertIsNone(self.artifact.generation_attempt_id); self.assertIsNone(self.artifact.generation_task_id)
        review = manual_media_review_detail(self.run)
        approved = self.post({"action": "approve", "stage_key": "artifact_selection",
            "scope": {"kind": "project", "id": str(self.project.pk)}, "unit_key": f"project:{self.project.pk}",
            "expected_run_plan_sha256": self.plan.sha256, "expected_selection_revision": review["approval"]["revision"],
            "idempotency_key": str(uuid4()), "payload": {"selection": review["selection"],
                "expected_selection_sha256": review["selection_sha256"], "comment": "Isolated video explicitly reviewed by test actor"}})
        self.assertEqual(approved.status_code, 201, approved.data)
        count = ArtifactSelectionDecision.objects.count()
        authors = self.authors()
        for invalid_id in (str(asset.pk), str(uuid4())):
            bad = deepcopy(authors); raw = json.loads(bad["assembly_media"]["source_text"])
            raw["片段"]["episodes"][0]["shots"][0]["artifact_id"] = invalid_id
            bad["assembly_media"]["source_text"] = json.dumps(raw)
            result = self.preview(bad); self.assertEqual(result.status_code, 400, result.data)
        preview = self.preview(authors); self.assertEqual(preview.status_code, 200, preview.data)
        body = self.authored_request(preview.data)
        first = self.post(body); self.assertEqual(first.status_code, 202, first.data)
        source = AssemblyJob.objects.get().source_items.get()
        self.assertEqual((source.generated_artifact_id, source.media_kind, source.sha256),
            (self.artifact.pk, "video", self.artifact.sha256))
        self.assertEqual(source.duration_ms, 1000)
        changed = deepcopy(body); changed["payload"]["authors"]["assembly_media"]["source_text"] += " "
        self.assertEqual(self.post(changed).status_code, 409)
        BusinessOutputVersion.objects.filter(pk__in=[rule["output_version_id"] for rule in authors.values()]).update(status="retired")
        with patch("studio.services.production_manual_assembly._inventory", side_effect=AssertionError("No repeated adapter")):
            replay = self.post(body); self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(AssemblyJob.objects.count(), 1)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), count)
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), (attempts, tasks))
