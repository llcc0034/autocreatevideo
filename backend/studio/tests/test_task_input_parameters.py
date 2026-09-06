import json
from copy import deepcopy
from uuid import uuid4

from django.test import TestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from studio.models import (
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.serializers import GenerationTaskSerializer
from studio.services.json_contracts import json_contract
from studio.tests.factories import build_story


class GenerationTaskInputParametersTests(TestCase):
    def setUp(self):
        (
            self.user,
            self.project,
            self.character,
            self.scene,
            self.episode,
            self.shot1,
            self.shot2,
        ) = build_story("task-input-parameters")
        self.prompt = PromptArtifact.objects.create(
            project=self.project,
            episode=self.episode,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            model="fictional-video-model",
            conditioning_mode="OrbitRefs",
            is_multi_shot=True,
            include_audio=False,
            total_duration_ms=7000,
            prompt_text="archived authoring prompt",
            structured_payload={
                "effective_conditioning_mode": "OrbitRefs",
                "prompt_template_key": "fictional:classified:v1",
            },
            request_options={
                "shot_ids": [str(self.shot1.id), str(self.shot2.id)],
                "whole_episode": True,
            },
            content_hash="a" * 64,
        )
        self.template = WorkflowTemplate.objects.create(
            name="task-input-template",
            slug="task-input-template",
            output_kind=PromptArtifact.PromptType.VIDEO,
            model="fictional-video-model",
            is_system=True,
            definition={"1": {"class_type": "SaveVideo", "inputs": {}}},
            input_schema={},
        )
        self.workflow = GeneratedWorkflow.objects.create(
            project=self.project,
            episode=self.episode,
            prompt_artifact=self.prompt,
            template=self.template,
            created_by=self.user,
            workflow_json=self.template.definition,
            injected_values={},
            content_hash=json_contract(self.template.definition).sha256,
            compiler_id="semantic_test_compiler",
            compiler_version=1,
            route_snapshot_sha256="r" * 64,
            workflow_contract_sha256="c" * 64,
        )
        graph_contract = json_contract(self.template.definition)
        input_contract = {"schema_version": 2}
        input_contract_json = json_contract(input_contract)
        self.version = WorkflowTemplateVersion.objects.create(
            template=self.template,
            version=1,
            graph=self.template.definition,
            graph_size_bytes=graph_contract.size_bytes,
            graph_sha256=graph_contract.sha256,
            input_contract=input_contract,
            input_contract_size_bytes=input_contract_json.size_bytes,
            input_contract_sha256=input_contract_json.sha256,
            compiler_id="semantic_test_compiler",
            compiler_version=1,
            semantic_bindings_sha256=json_contract([]).sha256,
            model_requirements_sha256=json_contract([]).sha256,
            execution_contract_sha256=self.workflow.workflow_contract_sha256,
        )
        self.workflow.workflow_template_version = self.version
        self.workflow.model_requirements_sha256 = json_contract([]).sha256
        self.workflow.save(update_fields=(
            "workflow_template_version", "model_requirements_sha256", "updated_at",
        ))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def create_task(
        self,
        *,
        kind=GenerationTask.Kind.VIDEO,
        mode="OrbitRefs",
        reference_assets=None,
        continuity_assets=None,
        snapshot_overrides=None,
    ) -> GenerationTask:
        is_image = kind == GenerationTask.Kind.IMAGE
        references = deepcopy(reference_assets or [])
        continuity = deepcopy(continuity_assets or [])
        semantics = {
            "prompt.positive": "exact frozen prompt",
            "image.width": 672,
            "image.height": 1216,
            "sampling.seed": 123456,
        }
        if not is_image:
            semantics.update({
                "video.fps": 30,
                "video.duration_ms": 7000,
                "video.frames": 211,
                "audio.enabled": False,
                "sampling.steps": 7,
            })
        selected_mode = None if is_image else {
            "key": mode,
            "input_kind": "classified_references",
        }
        conditioning_contract = {
            "schema_version": 1,
            "modes": [] if is_image else [selected_mode],
        }
        monitoring = {"schema_version": 1, "stages": []}
        execution_metadata = {
            "schema_version": 1,
            "workflow_version_id": str(self.version.id),
            "workflow_version": self.version.version,
            "template": {
                "id": str(self.template.id),
                "slug": self.template.slug,
                "name": self.template.name,
                "output_kind": kind,
            },
            "model_identifier": (
                "fictional-image-model" if is_image else "fictional-video-model"
            ),
            "workflow_mode": "semantic",
            "source_workflow": "fictional/current-workflow.json",
            "required_dimension_multiple": 16,
            "conditioning_contract": conditioning_contract,
            "conditioning_contract_sha256": json_contract(conditioning_contract).sha256,
            "conditioning_mode_contract": selected_mode,
            "conditioning_mode_contract_sha256": (
                json_contract(selected_mode).sha256 if selected_mode else ""
            ),
            "execution_monitoring": monitoring,
            "execution_monitoring_sha256": json_contract(monitoring).sha256,
        }
        execution_metadata_sha256 = json_contract(execution_metadata).sha256
        semantics_sha256 = json_contract(semantics).sha256
        bindings_sha256 = json_contract(references).sha256
        self.workflow.injected_values = {
            "schema_version": 2,
            "resolved_semantics": semantics,
            "asset_bindings": references,
            "model_requirements": [],
            "output_contract": {
                "schema_version": 1,
                "primary": {"node_id": "1", "kind": kind},
            },
            "execution_metadata": execution_metadata,
            "execution_metadata_sha256": execution_metadata_sha256,
        }
        self.workflow.resolved_semantics_sha256 = semantics_sha256
        self.workflow.asset_bindings_sha256 = bindings_sha256
        self.workflow.save(update_fields=(
            "injected_values", "resolved_semantics_sha256",
            "asset_bindings_sha256", "updated_at",
        ))
        self.prompt.prompt_type = kind
        self.prompt.conditioning_mode = "" if is_image else mode
        self.prompt.structured_payload = {
            "effective_conditioning_mode": "TEXT_ONLY" if is_image else mode,
            "prompt_template_key": (
                "fictional:image:v1" if is_image else "fictional:classified:v1"
            ),
        }
        self.prompt.save(update_fields=(
            "prompt_type", "conditioning_mode", "structured_payload", "updated_at",
        ))
        self.template.output_kind = kind
        self.template.save(update_fields=("output_kind", "updated_at"))

        snapshot = {
            "generated_workflow_id": str(self.workflow.id),
            "workflow_hash": self.workflow.content_hash,
            "prompt_artifact_id": str(self.prompt.id),
            "prompt_content_hash": self.prompt.content_hash,
            "template_id": str(self.template.id),
            "template_slug": self.template.slug,
            "model": execution_metadata["model_identifier"],
            "conditioning_mode": "" if is_image else mode,
            "effective_conditioning_mode": "TEXT_ONLY" if is_image else mode,
            "prompt_template_key": self.prompt.structured_payload["prompt_template_key"],
            "workflow_mode": execution_metadata["workflow_mode"],
            "source_workflow": execution_metadata["source_workflow"],
            "workflow_parameters": semantics,
            "resolution_profile": "semantic_contract",
            "resolution_multiple": 16,
            "width": 672,
            "height": 1216,
            "shot_ids": [str(self.shot1.id), str(self.shot2.id)],
            "shot_count": 2,
            "whole_episode": True,
            "workflow_template_version_id": str(self.version.id),
            "workflow_contract_sha256": self.workflow.workflow_contract_sha256,
            "route_snapshot_sha256": self.workflow.route_snapshot_sha256,
            "asset_bindings": references,
            "asset_bindings_sha256": bindings_sha256,
            "continuity_assets": continuity,
            "continuity_assets_sha256": json_contract(continuity).sha256,
            "resolved_semantics_sha256": semantics_sha256,
            "model_requirements": [],
            "model_requirements_sha256": self.workflow.model_requirements_sha256,
            "output_contract": self.workflow.injected_values["output_contract"],
            "execution_metadata": execution_metadata,
            "execution_metadata_sha256": execution_metadata_sha256,
        }
        if not is_image:
            snapshot.update({
                "fps": 30,
                "requested_duration_ms": 7000,
                "include_audio": False,
            })
        snapshot.update(snapshot_overrides or {})
        return GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            generated_workflow=self.workflow,
            kind=kind,
            backend="comfyui_local",
            model_profile=execution_metadata["model_identifier"],
            workflow_version=1,
            seed=123456,
            effective_frames=0 if is_image else 211,
            effective_duration_ms=0 if is_image else 7033,
            max_attempts=3,
            capability_snapshot=snapshot,
        )

    def test_current_contract_returns_path_free_distinct_reference_roles(self):
        video_asset_id = uuid4()
        paired_audio_asset_id = uuid4()
        task = self.create_task(
            reference_assets=[{
                "asset_id": str(video_asset_id),
                "name": "动作参考",
                "category": "video",
                "media_kind": "video",
                "input_slot": 1,
                "input_label": "<Video 1>",
                "audio_asset_id": str(paired_audio_asset_id),
                "audio_input_label": "<Audio 1>",
                "local_path": "E:/secret/reference.mp4",
            }],
            continuity_assets=[{
                "asset_id": str(self.character.anchor_asset_id),
                "name": "角色一致性参考",
                "category": "character",
                "media_kind": "image",
                "local_path": "C:/private/character.png",
            }],
        )
        task.staged_inputs = [{
            "asset_id": str(video_asset_id),
            "media_role": "reference_video",
            "source_fps": 29.97003,
            "effective_fps": 24,
            "effective_duration_ms": 5042,
            "cfr_guaranteed": True,
        }, {
            "asset_id": str(paired_audio_asset_id),
            "media_role": "paired_reference_audio",
            "duration_aligned": True,
        }]
        task.save(update_fields=("staged_inputs", "updated_at"))

        parameters = GenerationTaskSerializer(task).data["input_parameters"]

        self.assertEqual(parameters["model"], "fictional-video-model")
        self.assertEqual(parameters["resolution_profile"], "semantic_contract")
        self.assertEqual(parameters["resolution_multiple"], 16)
        self.assertEqual(parameters["reference_asset_count"], 1)
        self.assertEqual(parameters["continuity_asset_count"], 1)
        self.assertEqual(parameters["reference_assets"][0]["display_name"], "动作参考")
        self.assertEqual(
            parameters["continuity_assets"][0]["display_name"], "角色一致性参考",
        )
        self.assertTrue(parameters["reference_assets"][0]["cfr_guaranteed"])
        self.assertTrue(
            parameters["reference_assets"][0]["paired_audio_duration_aligned"],
        )
        encoded = json.dumps(parameters, ensure_ascii=False)
        self.assertNotIn("local_path", encoded)
        self.assertNotIn("E:/secret", encoded)
        self.assertNotIn("C:/private", encoded)

    def test_mode_name_never_reclassifies_explicit_reference_roles(self):
        task = self.create_task(
            mode="TextOnlyLookingName",
            reference_assets=[{
                "asset_id": str(uuid4()), "name": "模型输入", "category": "image",
                "media_kind": "image",
            }],
            continuity_assets=[{
                "asset_id": str(uuid4()), "name": "一致性素材", "category": "image",
                "media_kind": "image",
            }],
        )

        parameters = GenerationTaskSerializer(task).data["input_parameters"]

        self.assertEqual(
            [row["display_name"] for row in parameters["reference_assets"]],
            ["模型输入"],
        )
        self.assertEqual(
            [row["display_name"] for row in parameters["continuity_assets"]],
            ["一致性素材"],
        )

    def test_reference_projection_requires_frozen_media_kind(self):
        task = self.create_task(reference_assets=[{
            "asset_id": str(uuid4()),
            "name": "缺失类型的素材",
            "category": "video",
        }])

        with self.assertRaisesRegex(ValidationError, "media_kind"):
            GenerationTaskSerializer(task).data

    def test_missing_current_contract_fails_closed_in_detail_api(self):
        task = self.create_task()
        task.capability_snapshot = {"resolution_profile": "semantic_contract"}
        task.save(update_fields=("capability_snapshot", "updated_at"))

        response = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("不能从其他记录推断", str(response.data))

    def test_execution_metadata_hash_drift_fails_closed(self):
        task = self.create_task()
        snapshot = deepcopy(task.capability_snapshot)
        snapshot["execution_metadata"]["required_dimension_multiple"] = 32
        task.capability_snapshot = snapshot
        task.save(update_fields=("capability_snapshot", "updated_at"))

        with self.assertRaises(ValidationError):
            GenerationTaskSerializer(task).data

    def test_missing_resolution_profile_fails_closed(self):
        task = self.create_task()
        task.capability_snapshot = {
            **task.capability_snapshot,
            "resolution_profile": "",
        }
        task.save(update_fields=("capability_snapshot", "updated_at"))

        with self.assertRaises(ValidationError):
            GenerationTaskSerializer(task).data

    def test_frames_fps_only_video_contract_uses_frozen_snapshot_and_effective_plan(self):
        task = self.create_task()
        semantics = deepcopy(task.generated_workflow.injected_values["resolved_semantics"])
        semantics.pop("video.duration_ms")
        semantics.pop("audio.enabled")
        semantics["video.frames"] = 22
        semantics["video.fps"] = 24
        semantics_contract = json_contract(semantics)
        task.generated_workflow.injected_values = {
            **task.generated_workflow.injected_values,
            "resolved_semantics": semantics,
        }
        task.generated_workflow.resolved_semantics_sha256 = semantics_contract.sha256
        task.generated_workflow.save(update_fields=(
            "injected_values", "resolved_semantics_sha256", "updated_at",
        ))
        snapshot = deepcopy(task.capability_snapshot)
        snapshot.update({
            "workflow_parameters": semantics,
            "resolved_semantics_sha256": semantics_contract.sha256,
            "fps": 24,
            "requested_duration_ms": 1000,
            "include_audio": True,
        })
        task.capability_snapshot = snapshot
        task.effective_frames = 22
        task.effective_duration_ms = 917
        task.save(update_fields=(
            "capability_snapshot", "effective_frames", "effective_duration_ms", "updated_at",
        ))

        parameters = GenerationTaskSerializer(task).data["input_parameters"]

        self.assertEqual(parameters["requested_duration_ms"], 1000)
        self.assertTrue(parameters["include_audio"])
        self.assertEqual(parameters["effective_frames"], 22)
        self.assertEqual(parameters["effective_duration_ms"], 917)

    def test_frames_fps_only_video_contract_rejects_effective_plan_drift(self):
        task = self.create_task()
        task.effective_frames = 210
        task.save(update_fields=("effective_frames", "updated_at"))

        with self.assertRaisesRegex(ValidationError, "视频时钟"):
            GenerationTaskSerializer(task).data

    def test_explicit_video_duration_and_audio_semantics_still_reject_drift(self):
        task = self.create_task(snapshot_overrides={
            "requested_duration_ms": 6999,
            "include_audio": True,
        })

        with self.assertRaisesRegex(ValidationError, "视频请求时长"):
            GenerationTaskSerializer(task).data

    def test_mixed_task_list_preserves_unavailable_audits_without_inventing_inputs(self):
        current_task = self.create_task()

        def unsupported_task(*, injected_values, status):
            workflow = GeneratedWorkflow.objects.create(
                project=self.project,
                episode=self.episode,
                prompt_artifact=self.prompt,
                template=self.template,
                workflow_template_version=self.version,
                created_by=self.user,
                workflow_json={"legacy_path": "C:/private/legacy-workflow.json"},
                injected_values=injected_values,
                content_hash=json_contract(injected_values).sha256,
                compiler_id="retired_test_compiler",
                compiler_version=0,
            )
            return GenerationTask.objects.create(
                owner=self.user,
                project=self.project,
                episode=self.episode,
                generated_workflow=workflow,
                kind=GenerationTask.Kind.VIDEO,
                status=status,
                max_attempts=3,
                capability_snapshot={
                    "local_path": "E:/private/legacy-input.mp4",
                    "prompt": "must not be projected",
                },
            )

        legacy_task = unsupported_task(
            injected_values={"legacy_path": "C:/private/legacy-input.json"},
            status=GenerationTask.Status.SUCCEEDED,
        )
        invalid_task = unsupported_task(
            injected_values={"schema_version": 2, "partial": True},
            status=GenerationTask.Status.FAILED,
        )

        rows = {}
        next_url = "/api/v1/tasks/"
        while next_url:
            response = self.client.get(next_url)
            self.assertEqual(response.status_code, 200, response.data)
            rows.update({
                str(row["id"]): row
                for row in response.data["results"]
            })
            next_url = response.data["next"]

        self.assertEqual(set(rows), {
            str(current_task.id), str(legacy_task.id), str(invalid_task.id),
        })
        self.assertEqual(rows[str(current_task.id)]["input_contract_status"], "available")
        self.assertEqual(rows[str(current_task.id)]["input_contract_code"], "")
        self.assertIsInstance(rows[str(current_task.id)]["input_parameters"], dict)

        self.assertEqual(
            rows[str(legacy_task.id)]["input_contract_code"],
            "legacy_schema_unsupported",
        )
        self.assertEqual(
            rows[str(invalid_task.id)]["input_contract_code"],
            "frozen_contract_invalid",
        )
        for task in (legacy_task, invalid_task):
            row = rows[str(task.id)]
            self.assertEqual(row["input_contract_status"], "unavailable")
            self.assertIsNone(row["input_parameters"])
            self.assertEqual(row["status"], task.status)
        encoded = json.dumps(response.data, ensure_ascii=False, default=str)
        self.assertNotIn("C:/private", encoded)
        self.assertNotIn("E:/private", encoded)
        self.assertNotIn("must not be projected", encoded)

        legacy_detail = self.client.get(f"/api/v1/tasks/{legacy_task.id}/")
        invalid_prompt = self.client.get(
            f"/api/v1/tasks/{invalid_task.id}/input-prompt/",
        )
        self.assertEqual(legacy_detail.status_code, 400, legacy_detail.data)
        self.assertEqual(invalid_prompt.status_code, 400, invalid_prompt.data)
        self.assertIn("不能从其他记录推断", str(legacy_detail.data))
        self.assertIn("不能从其他记录推断", str(invalid_prompt.data))

    def test_image_task_uses_current_contract_and_has_no_video_parameters(self):
        task = self.create_task(kind=GenerationTask.Kind.IMAGE)

        parameters = GenerationTaskSerializer(task).data["input_parameters"]

        self.assertEqual(parameters["conditioning_mode"], "")
        self.assertEqual(parameters["effective_conditioning_mode"], "TEXT_ONLY")
        for video_only_key in (
            "fps", "requested_duration_ms", "effective_duration_ms",
            "effective_frames", "include_audio",
        ):
            self.assertNotIn(video_only_key, parameters)

    def test_production_group_identity_is_separate_from_input_contract(self):
        production_run_id = uuid4()
        task = self.create_task(snapshot_overrides={
            "production_run_id": str(production_run_id),
            "stage_run_id": str(uuid4()),
            "generation_attempt_id": str(uuid4()),
            "execution_identity": {
                "schema_version": 1,
                "variant_number": 2,
                "output_prefix": "C:/private/must-not-leak",
            },
        })

        payload = GenerationTaskSerializer(task).data

        self.assertEqual(payload["production_run_id"], str(production_run_id))
        self.assertEqual(payload["production_variant_number"], 2)
        self.assertNotIn("production_run_id", payload["input_parameters"])
        self.assertNotIn("output_prefix", str(payload))
