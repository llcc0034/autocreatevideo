"""Real DB contracts and input resolution, no provider calls or invented approvals."""
from copy import deepcopy

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import SeriesProject, Style, ModelDeploymentSmokeAttempt
from studio.services.execution_definitions import build_execution_definition, read_execution_input_contract
from studio.services.execution_instances import publish_execution_version
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_input_suggestions import preview_execution_input_suggestion
from studio.services.execution_profile_inputs import project_execution_profile_sources
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_profile_inputs as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionProjectSourceTests(TestCase):
    write_contract = staticmethod(fixtures.ExecutionProfileInputTests.write_contract)
    setUpTestData = classmethod(fixtures.ExecutionProfileInputTests.setUpTestData.__func__)
    profile = fixtures.ExecutionProfileInputTests.profile
    configure = fixtures.ExecutionProfileInputTests.configure

    def setUp(self):
        fixtures.ExecutionProfileInputTests.setUp(self)
        self.style = Style.objects.create(name="风格", slug="governed-style", owner=self.actor,
            visual_prompt="Quiet cinematic suspense. 自然光。", description="DO NOT INCLUDE", negative_prompt="NOT INCLUDED")
        self.project = SeriesProject.objects.create(owner=self.actor, title="Source project", logline="一句话 story.",
            master_script="DO NOT READ " * 10000, style=self.style, language="zh", aspect_ratio="9:16", width=720,
            height=1280, fps=24, default_shot_duration_ms=2000, continuity_seed=1)

    def contract(self, selector="project.logline"):
        self.configure(self.profile(selector=selector), [])
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.key = selector
        return read_execution_input_contract(self.version.pk)

    def preview(self, contract, **kwargs):
        return preview_execution_input_suggestion(contract, owner=self.actor, project_id=kwargs.get("project_id", self.project.pk),
            input_key=self.key, suggestion_key=self.key)

    def resolve(self, source, **kwargs):
        return resolve_execution_input_sources(kwargs.get("definition", self.definition), {self.key: source},
            owner=kwargs.get("owner", self.actor), subject_facts=[], max_media_bytes=65536,
            project_id=kwargs.get("project_id", self.project.pk)).value

    def test_preview_and_snapshot_are_explicit_frozen_and_do_not_load_whole_script(self):
        contract = self.contract()
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        with self.assertRaises(ValidationError):
            self.resolve({"kind": "literal", "value": "pretend project content"})
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            preview = self.preview(contract)
        self.assertFalse(any('"master_script"' in row["sql"] for row in queries))
        self.project.logline = "New unconfirmed edit"; self.project.save()
        source = {"kind": "project_snapshot", "reference": preview["reference"]}
        result = self.resolve(source)
        self.assertEqual(result["logical_values"][self.key], "一句话 story.")
        provenance = result["input_provenance"][0]
        self.assertEqual(provenance["scope"], {"kind": "project", "id": str(self.project.pk)})
        self.assertFalse(provenance["edited"])
        self.assertEqual(provenance["content_sha256"], provenance["value_sha256"])
        self.assertEqual(provenance["source_reference"]["location"], "连续剧项目 → 项目设定 → 一句话梗概")
        projected = project_execution_profile_sources(self.definition, result["logical_values"])
        self.assertEqual(projected["assembly_sources"][self.key], "一句话 story.")
        self.assertNotIn("DO NOT READ", str(projected))
        self.assertEqual(read_execution_input_contract(self.version.pk)["sha256"], contract["sha256"])
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())

    def test_style_only_reads_visual_prompt_and_freezes_original_style_identity(self):
        contract = self.contract("style.visual_prompt")
        preview = self.preview(contract)
        self.assertEqual(preview["content"], self.style.visual_prompt)
        self.style.visual_prompt = "New style"; self.style.save()
        self.project.style = None; self.project.save()
        result = self.resolve({"kind": "project_snapshot", "reference": preview["reference"]})
        self.assertEqual(result["logical_values"][self.key], "Quiet cinematic suspense. 自然光。")
        self.assertEqual(result["input_provenance"][0]["source_reference"]["source_id"], str(self.style.pk))
        with self.assertRaisesMessage(ValidationError, "未选择视觉风格"):
            self.preview(contract)

    def test_empty_missing_and_foreign_projects_are_explicit_errors(self):
        contract = self.contract()
        with self.assertRaises(ValidationError): self.preview(contract, project_id=None)
        self.project.logline = " "; self.project.save()
        with self.assertRaisesMessage(ValidationError, "为空"): self.preview(contract)
        other = get_user_model().objects.create_user(username="project-source-other")
        self.project.owner = other; self.project.save()
        with self.assertRaisesMessage(ValidationError, "无权读取"): self.preview(contract)

    def test_rechecks_access_after_preview_even_for_staff_and_projectless_smoke(self):
        contract = self.contract()
        preview = self.preview(contract)
        source = {"kind": "project_snapshot", "reference": preview["reference"]}
        self.assertEqual(self.resolve(source, project_id=None)["logical_values"][self.key], preview["content"])
        other = get_user_model().objects.create_user(username="lost-source-owner")
        self.project.owner = other; self.project.save()
        with self.assertRaisesMessage(ValidationError, "无权读取"): self.resolve(source, project_id=None)

    def test_rejects_cross_project_wrong_contract_tampering_and_value_injection(self):
        contract = self.contract()
        preview = self.preview(contract)
        source = {"kind": "project_snapshot", "reference": preview["reference"]}
        for bad in ({**source, "value": preview["content"]}, {**source, "reference": preview["reference"] + "bad"},
                {"kind": "context_field"}, {"kind": "literal", "value": preview["content"], "reference": preview["reference"]}):
            with self.subTest(source=bad["kind"]), self.assertRaises(ValidationError): self.resolve(bad)
        self.project.pk = None; self.project.save()
        with self.assertRaisesMessage(ValidationError, "来源项目与本次准备项目不一致"): self.resolve(source)
        with self.assertRaises(ValidationError): self.resolve(source, project_id=None, definition=json_contract({
            **deepcopy(self.definition.value), "version_id": "different-definition"}))

    def test_formal_preview_api_keeps_model_tasks_unchanged(self):
        contract = self.contract()
        api = APIClient(); api.force_authenticate(self.actor)
        url = f"/api/v1/model-execution-instance-versions/{self.version.pk}/input-suggestion/"
        response = api.get(url, {"project_id": str(self.project.pk), "input_key": self.key, "suggestion_key": self.key})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["definition_sha256"], contract["sha256"])
        self.assertIn("reference", response.data)
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        api.force_authenticate(None)
        self.assertIn(api.get(url).status_code, (401, 403))
