"""INT-01: new exact contracts and independent Runs, with no dispatch."""
from copy import deepcopy
from hashlib import sha256

from django.test import TestCase, override_settings

from studio.models import (ApprovalDecision, Asset, AssetGenerationJob, BibleVariant,
    ExecutionPreparation, GenerationAttempt, GenerationTask, ProductionRun, Shot)
from studio.services.execution_definitions import verify_current_execution_definition
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.script_revision_structures import read_script_revision_structure
from studio.tests.int_contract_fixtures import IntegrationContracts


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class IntegrationContractConfigurationTests(IntegrationContracts, TestCase):
    def setUp(self):
        self.int_setup()

    def tearDown(self):
        for guard in self.network_guards:
            guard.assert_not_called()

    def test_new_layouts_and_explicit_policies_freeze_new_runs_before_dispatch(self):
        self.assertFalse(ProductionRun.objects.exists())
        self.assertFalse(Asset.objects.exists())
        samples = [self.int_sample(layout=layout, mode=mode)
            for layout, mode in (("a", "automatic"), ("b", "mixed"))]
        for sample in samples:
            with self.subTest(layout=sample.layout, mode=sample.mode):
                run, recipe = sample.run, sample.recipe
                self.assertEqual((recipe.status, run.status), ("published", "queued"))
                self.assertEqual(run.script_revision_id, sample.revision.pk)
                self.assertEqual(sample.revision.content, sample.text)
                self.assertEqual(sample.revision.content_sha256, sha256(sample.text.encode("utf-8")).hexdigest())
                detail = read_script_revision_structure(revision_id=sample.revision.pk,
                    project_id=sample.project.pk, max_bytes=1_000_000)
                self.assertEqual(detail["document"]["projection"]["source_value"], sample.raw)
                self.assertEqual(sample.raw["创作补充"], {"count": 0, "enabled": False,
                    "optional": None, "note": "原文 🙂\r\nNo translation"})
                plan = get_run_execution_plan(run)
                self.assertEqual(plan.value["recipe"]["definition"], recipe.definition)
                self.assertTrue(plan.value["structure_source_schemas"]["shot_field"])
                stage = next(row for row in recipe.definition["stages"] if row["key"] == "bible_image_generation")
                self.assertEqual(stage["material_sources"], {
                    "primary": "generate" if sample.mode == "automatic" else "existing", "variant": "generate"})
                reviews = [row for row in recipe.definition["stages"] if row["mode"] == "review"]
                self.assertEqual({row["approval"] for row in reviews}, {"policy" if sample.mode == "automatic" else "required"})
                self.assertEqual(run.stages.filter(status="pending").count(), len(recipe.definition["stages"]))
                video = next(row for row in recipe.definition["stages"] if row["key"] == "media_generation")["routing"]["routes"][0]
                self.assertEqual(video["input_sources"]["references"]["kind"], "reviewed_references")
                self.assertEqual(video["execution_instance_version_id"], str(self.video[0].pk))
                frozen = deepcopy(run.execution_plan.definition)
                replay = self.api("post", "/api/v1/production-runs/", sample.create_body)
                self.assertEqual(replay["id"], str(run.pk))
                self.assertEqual(run.execution_plan.definition, frozen)
        for version, expected in (self.primary, self.variant, self.video):
            actual = verify_current_execution_definition(version.contract)
            self.assertEqual(actual.sha256, expected["sha256"])
            self.assertEqual(actual.value["configuration"]["auxiliaries"], [])
            self.assertEqual(actual.value["configuration"]["prompt_mode"], "profile")
        self.assertEqual(ProductionRun.objects.count(), 2)
        self.assertEqual(len({sample.recipe.pk for sample in samples}), 2)
        self.assertEqual(len({sample.project.pk for sample in samples}), 2)
        self.assertEqual(len({sample.structure.pk for sample in samples}), 2)
        for model in (Asset, BibleVariant, Shot, ApprovalDecision, ExecutionPreparation,
                AssetGenerationJob, GenerationAttempt, GenerationTask):
            self.assertFalse(model.objects.exists(), model.__name__)
