import hashlib
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ApprovalDecision,
    GeneratedArtifact,
    GenerationAttempt,
    GenerationAttemptResolution,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    ProviderProfile,
    ScriptRevision,
    ScriptSource,
    SeriesProject,
    StageRun,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.production_runs import (
    StageBlocked,
    _structured_script_route_duration_policy,
    _validate_storyboard_duration_policy,
)
from studio.services.deployment_smoke import record_deployment_capability_snapshot
from studio.services.deployment_smoke_queue import (
    claim_next_deployment_smoke,
    process_deployment_smoke,
)
from studio.services.provider_registry import ProviderDriverError
from studio.services.script_structure import (
    SCRIPT_STRUCTURE_SCHEMA_VERSION,
    ScriptStructureError,
    build_script_structure_plan,
    canonical_no_submission_result,
    finalize_script_structure,
    intersect_script_structure_duration_policy,
    is_canonical_script,
    parse_canonical_script,
    structure_provider_payload,
)
from studio.services.series_concept_drafts import render_compatible_master_script
from studio.services.script_sources import (
    confirm_script_revision,
    create_pasted_script_source,
)
from studio.services.text_generation_queue import (
    claim_next_text_attempt,
    process_text_attempt,
)
from studio.tests.factories import (
    create_test_prompt_profile_binding,
    script_structure_field_contract,
)


def _structure_chat_request_options() -> dict:
    return {"temperature": 0, "response_format": {"type": "json_object"}}


class ScriptStructureContractTests(TestCase):
    def test_all_frozen_structure_routes_share_one_duration_intersection(self):
        minimum, maximum, policies = _structured_script_route_duration_policy(
            [{
                "key": "script_structure",
                "mode": "ai",
                "routing": {
                    "routes": [
                        {
                            "index": 0,
                            "model_specification": {
                                "script_structure": {
                                    "minimum_shot_duration_ms": 1000,
                                    "maximum_shot_duration_ms": 6000,
                                },
                            },
                        },
                        {
                            "index": 1,
                            "model_specification": {
                                "script_structure": {
                                    "minimum_shot_duration_ms": 2000,
                                    "maximum_shot_duration_ms": 4000,
                                },
                            },
                        },
                    ],
                },
            }],
            stage_key="script_structure",
            stage_mode="ai",
            model_policy_key="script_structure",
        )

        self.assertEqual((minimum, maximum), (2000, 4000))
        self.assertEqual(policies, [
            {
                "route_index": 0,
                "minimum_duration_ms": 1000,
                "maximum_duration_ms": 6000,
            },
            {
                "route_index": 1,
                "minimum_duration_ms": 2000,
                "maximum_duration_ms": 4000,
            },
        ])

    def setUp(self):
        self.bible = {
            "characters": [{
                "name": "林雾",
                "description": "追查失踪案的记者",
                "visual_identity_en": "Chinese investigative reporter in a dark raincoat",
            }],
            "scenes": [{
                "name": "雨夜旅馆",
                "description": "停电后的旧旅馆走廊",
                "visual_identity_en": "old hotel corridor during a rainy blackout",
            }],
            "props": [],
        }
        self.episodes = [{
            "title": "回声",
            "shots": [
                {
                    "setting": "雨夜旅馆",
                    "action": "林雾推开房门，旧磁带开始自行转动。",
                    "duration_ms": 2_000,
                    "dialogue": {
                        "speaker": "", "delivery": "", "text": "",
                        "start_ms": 0, "end_ms": 0,
                    },
                },
                {
                    "setting": "雨夜旅馆",
                    "action": "林雾拿起磁带。",
                    "duration_ms": 2_000,
                    "dialogue": {
                        "speaker": "林雾",
                        "delivery": "quietly",
                        "text": "谁在那里？",
                        "start_ms": 250,
                        "end_ms": 1_750,
                    },
                },
            ],
        }]
        self.canonical = render_compatible_master_script(self.bible, self.episodes)

    def _revision(self, content: str):
        encoded = content.encode("utf-8")
        import hashlib

        return SimpleNamespace(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            revision=1,
            content=content,
            content_size_bytes=len(encoded),
            content_sha256=hashlib.sha256(encoded).hexdigest(),
            status=ScriptRevision.Status.CONFIRMED,
        )

    def _plan(self, content: str, *, field_contract=None):
        return build_script_structure_plan(
            revision=self._revision(content),
            model_identifier="strict-structure-model",
            policy={
                "minimum_shot_duration_ms": 1_000,
                "maximum_shot_duration_ms": 10_000,
                "max_input_bytes": 64_000,
                "max_output_tokens": 8_192,
                "timeout_seconds": 30,
                "field_contract": (
                    field_contract
                    if field_contract is not None
                    else script_structure_field_contract()
                ),
                "chat_request_options": _structure_chat_request_options(),
            },
        )

    def test_exact_renderer_output_is_the_only_deterministic_no_submission_form(self):
        parsed = parse_canonical_script(self.canonical)

        self.assertTrue(is_canonical_script(self.canonical))
        self.assertEqual(parsed["master_script"], self.canonical)
        self.assertEqual(len(parsed["episodes"][0]["shots"]), 2)
        self.assertEqual(
            parsed["episodes"][0]["shots"][1]["dialogue"],
            {
                "speaker": "林雾",
                "delivery": "quietly",
                "text": "谁在那里？",
                "start_ms": 250,
                "end_ms": 1_750,
            },
        )
        self.assertIn("[0.25-1.75秒] 林雾", self.canonical)
        self.assertNotIn("[1-2秒] 林雾", self.canonical)
        self.assertFalse(is_canonical_script(self.canonical.replace("【尾帧连续=是】", "【尾帧连续=否】", 1)))
        self.assertFalse(is_canonical_script(self.canonical + "\n"))
        self.assertFalse(is_canonical_script("\ufeff" + self.canonical))

        revision = self._revision(self.canonical)
        no_op = canonical_no_submission_result(
            revision,
            title="雾港回声",
            logline="记者在雨夜旅馆追查一盘会预告失踪的磁带。",
        )
        self.assertEqual(no_op["source_revision"]["id"], str(revision.id))
        self.assertEqual(no_op["candidate"]["master_script"], self.canonical)
        self.assertEqual(
            no_op["evidence"]["submission_policy"],
            "canonical_input_no_submission",
        )

    def test_storyboard_rejects_frozen_duration_policy_that_would_rewrite_canonical_script(self):
        with self.assertRaises(StageBlocked) as raised:
            _validate_storyboard_duration_policy(
                self.canonical,
                minimum_duration_ms=1_000,
                maximum_duration_ms=1_000,
            )

        self.assertEqual(
            raised.exception.code,
            "script_duration_policy_mismatch",
        )
        self.assertIn("第1集镜头1=2000ms", str(raised.exception))

        _validate_storyboard_duration_policy(
            self.canonical,
            minimum_duration_ms=1_000,
            maximum_duration_ms=10_000,
        )

    def test_full_source_is_frozen_in_provider_payload_without_slicing(self):
        source = "第一幕\n林雾进入旅馆。\n结尾标记-不可丢失"
        plan = self._plan(source)

        payload = structure_provider_payload(plan)
        user = json.loads(payload["messages"][1]["content"])

        self.assertEqual(user["source_text"], source)
        self.assertTrue(user["source_text"].endswith("结尾标记-不可丢失"))
        self.assertEqual(user["non_empty_source_line_numbers"], [1, 2, 3])
        self.assertEqual(
            plan["resource_estimate"]["submission_policy"],
            "full_source_or_fail_no_truncation",
        )
        self.assertEqual(payload["timeout_seconds"], 30.0)
        self.assertEqual(
            payload["chat_request_options"],
            _structure_chat_request_options(),
        )
        self.assertNotIn("temperature", payload)
        self.assertNotIn("response_format", payload)
        contract = json.loads(payload["messages"][0]["content"].split("CONTRACT=", 1)[1])
        self.assertEqual(contract["schema_version"], SCRIPT_STRUCTURE_SCHEMA_VERSION)
        self.assertEqual(
            contract["episodes"][0]["shots"][0]["dialogue"],
            {
                "speaker": "", "delivery": "", "text": "",
                "start_ms": 0, "end_ms": 0,
            },
        )
        self.assertIn("0 <= start_ms < end_ms", payload["messages"][0]["content"])

    def test_field_contract_is_frozen_listed_and_finalize_ignores_live_mutation(self):
        source = "第一幕\n林雾进入旅馆。"
        contract = script_structure_field_contract()
        contract["shot.action"]["max_chars"] = 12
        contract["bible.entity.description"]["max_chars"] = 20
        plan = self._plan(source, field_contract=contract)
        contract["shot.action"]["max_chars"] = 1
        contract["bible.entity.description"]["max_chars"] = 1

        self.assertEqual(plan["field_contract"]["shot.action"]["max_chars"], 12)
        system_prompt = structure_provider_payload(plan)["messages"][0]["content"]
        self.assertIn("FIELD_CONTRACT", system_prompt)
        self.assertIn(
            "- shot.action: required/non-empty; single-line; max 12 characters",
            system_prompt,
        )
        self.assertIn(
            "- bible.entity.description: optional; single-line; max 20 characters",
            system_prompt,
        )

        candidate = {
            "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
            "source_revision_id": plan["source_revision"]["id"],
            "source_sha256": plan["source_revision"]["content_sha256"],
            "source_size_bytes": plan["source_revision"]["content_size_bytes"],
            "title": "雾港回声",
            "logline": "林雾在旧旅馆追查失踪案。",
            "bible": self.bible,
            "episodes": [{
                "title": "第一幕",
                "shots": [{
                    "source_lines": [1, 2],
                    "setting": "雨夜旅馆",
                    "action": "林雾进入旅馆。",
                    "duration_ms": 2_000,
                    "dialogue": {
                        "speaker": "", "delivery": "", "text": "",
                        "start_ms": 0, "end_ms": 0,
                    },
                }],
            }],
        }
        result = finalize_script_structure(plan=plan, payload=candidate)
        self.assertTrue(is_canonical_script(result["candidate"]["master_script"]))

    def test_response_count_limits_read_current_security_boundaries(self):
        plan = self._plan("第一幕\n林雾进入旅馆。")
        shot = {
            "source_lines": [1, 2],
            "setting": "雨夜旅馆",
            "action": "林雾进入旅馆。",
            "duration_ms": 2_000,
            "dialogue": {
                "speaker": "", "delivery": "", "text": "",
                "start_ms": 0, "end_ms": 0,
            },
        }
        candidate = {
            "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
            "source_revision_id": plan["source_revision"]["id"],
            "source_sha256": plan["source_revision"]["content_sha256"],
            "source_size_bytes": plan["source_revision"]["content_size_bytes"],
            "title": "雾港回声",
            "logline": "林雾在旧旅馆追查失踪案。",
            "bible": self.bible,
            "episodes": [{"title": "第一幕", "shots": [shot]}],
        }
        cases = (
            (
                "episodes",
                [{"title": "第一幕", "shots": [shot]}] * 2,
                {
                    "security.production.max_episodes": 1,
                    "security.production.max_shots_per_episode": 2,
                    "security.production.max_total_shots": 2,
                },
                "episodes",
            ),
            (
                "shots_per_episode",
                [{"title": "第一幕", "shots": [shot, shot]}],
                {
                    "security.production.max_episodes": 1,
                    "security.production.max_shots_per_episode": 1,
                    "security.production.max_total_shots": 2,
                },
                "每集镜头",
            ),
            (
                "total_shots",
                [
                    {"title": "第一幕", "shots": [shot]},
                    {"title": "第二幕", "shots": [shot]},
                ],
                {
                    "security.production.max_episodes": 2,
                    "security.production.max_shots_per_episode": 1,
                    "security.production.max_total_shots": 1,
                },
                "总镜头",
            ),
        )
        for label, episodes, boundaries, message in cases:
            with self.subTest(label=label), patch(
                "studio.services.script_structure.boundary_int",
                side_effect=lambda key: boundaries[key],
            ), self.assertRaises(ScriptStructureError) as raised:
                finalize_script_structure(
                    plan=plan,
                    payload={**candidate, "episodes": episodes},
                )
            self.assertEqual(raised.exception.code, "script_structure_ir_invalid")
            self.assertIn(message, str(raised.exception))

    def test_strict_ir_requires_exact_source_identity_and_every_non_empty_line_once(self):
        source = "第一幕\n林雾进入旅馆。"
        plan = self._plan(source)
        candidate = {
            "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
            "source_revision_id": plan["source_revision"]["id"],
            "source_sha256": plan["source_revision"]["content_sha256"],
            "source_size_bytes": plan["source_revision"]["content_size_bytes"],
            "title": "雾港回声",
            "logline": "林雾在旧旅馆追查失踪案。",
            "bible": self.bible,
            "episodes": [{
                "title": "第一幕",
                "shots": [{
                    "source_lines": [1, 2],
                    "setting": "雨夜旅馆",
                    "action": "林雾进入旅馆。",
                    "duration_ms": 2_000,
                    "dialogue": {
                        "speaker": "", "delivery": "", "text": "",
                        "start_ms": 0, "end_ms": 0,
                    },
                }],
            }],
        }

        result = finalize_script_structure(plan=plan, payload=candidate)
        self.assertEqual(result["evidence"]["covered_non_empty_line_count"], 2)
        self.assertTrue(is_canonical_script(result["candidate"]["master_script"]))

        missing = {**candidate, "episodes": [{
            **candidate["episodes"][0],
            "shots": [{**candidate["episodes"][0]["shots"][0], "source_lines": [1]}],
        }]}
        with self.assertRaises(ScriptStructureError) as raised:
            finalize_script_structure(plan=plan, payload=missing)
        self.assertEqual(raised.exception.code, "script_structure_source_coverage_invalid")

        duplicated = {**candidate, "episodes": [{
            **candidate["episodes"][0],
            "shots": [{**candidate["episodes"][0]["shots"][0], "source_lines": [1, 1, 2]}],
        }]}
        with self.assertRaises(ScriptStructureError) as raised:
            finalize_script_structure(plan=plan, payload=duplicated)
        self.assertEqual(raised.exception.code, "script_structure_source_coverage_invalid")

        wrong_source = {**candidate, "source_sha256": "0" * 64}
        with self.assertRaises(ScriptStructureError) as raised:
            finalize_script_structure(plan=plan, payload=wrong_source)
        self.assertEqual(raised.exception.code, "script_structure_source_mismatch")

    def test_strict_ir_rejects_missing_or_out_of_shot_dialogue_clocks(self):
        plan = self._plan("林雾说出警告。")
        base = {
            "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
            "source_revision_id": plan["source_revision"]["id"],
            "source_sha256": plan["source_revision"]["content_sha256"],
            "source_size_bytes": plan["source_revision"]["content_size_bytes"],
            "title": "雾港回声",
            "logline": "林雾在旧旅馆追查失踪案。",
            "bible": self.bible,
            "episodes": [{
                "title": "第一幕",
                "shots": [{
                    "source_lines": [1],
                    "setting": "雨夜旅馆",
                    "action": "林雾说出警告。",
                    "duration_ms": 2_000,
                    "dialogue": {
                        "speaker": "林雾",
                        "delivery": "firmly",
                        "text": "不要进来。",
                        "start_ms": 200,
                        "end_ms": 1_400,
                    },
                }],
            }],
        }

        result = finalize_script_structure(plan=plan, payload=base)
        self.assertIn("[0.2-1.4秒] 林雾", result["candidate"]["master_script"])

        missing = json.loads(json.dumps(base, ensure_ascii=False))
        missing["episodes"][0]["shots"][0]["dialogue"].pop("start_ms")
        outside = json.loads(json.dumps(base, ensure_ascii=False))
        outside["episodes"][0]["shots"][0]["dialogue"]["end_ms"] = 2_001
        for candidate in (missing, outside):
            with self.subTest(dialogue=candidate["episodes"][0]["shots"][0]["dialogue"]):
                with self.assertRaises(ScriptStructureError) as raised:
                    finalize_script_structure(plan=plan, payload=candidate)
                self.assertEqual(raised.exception.code, "script_structure_ir_invalid")

    def test_input_larger_than_profile_limit_blocks_instead_of_truncating(self):
        revision = self._revision("完整剧本不可截断")
        with self.assertRaises(ScriptStructureError) as raised:
            build_script_structure_plan(
                revision=revision,
                model_identifier="strict-structure-model",
                policy={
                    "minimum_shot_duration_ms": 1_000,
                    "maximum_shot_duration_ms": 10_000,
                    "max_input_bytes": 3,
                    "max_output_tokens": 8_192,
                    "timeout_seconds": 30,
                    "field_contract": script_structure_field_contract(),
                    "chat_request_options": _structure_chat_request_options(),
                },
            )
        self.assertEqual(raised.exception.code, "script_structure_input_too_large")

    def test_model_and_frozen_production_duration_policies_are_intersected(self):
        effective = intersect_script_structure_duration_policy(
            {
                "minimum_shot_duration_ms": 1_000,
                "maximum_shot_duration_ms": 10_000,
                "max_input_bytes": 64_000,
                "max_output_tokens": 8_192,
                "timeout_seconds": 30.0,
                "chat_request_options": _structure_chat_request_options(),
            },
            production_minimum_duration_ms=5_000,
            production_maximum_duration_ms=15_000,
        )

        self.assertEqual(effective["minimum_shot_duration_ms"], 5_000)
        self.assertEqual(effective["maximum_shot_duration_ms"], 10_000)

        with self.assertRaises(ScriptStructureError) as raised:
            intersect_script_structure_duration_policy(
                effective,
                production_minimum_duration_ms=12_000,
                production_maximum_duration_ms=15_000,
            )
        self.assertEqual(
            raised.exception.code,
            "script_structure_duration_policy_incompatible",
        )


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ScriptStructureProductionRunTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="structure-owner",
            password="StrongPass123!",
            is_staff=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        provider = ProviderProfile(
            key="structure-provider",
            name="Structure provider",
            driver_type=ProviderProfile.DriverType.OPENAI_COMPATIBLE,
            modalities="text",
            operations="chat_completion,script.structure",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "http://127.0.0.1:9901",
            "chat_path": "/v1/chat/completions",
            "timeout_seconds": 30,
            "capability_ttl_seconds": 3_600,
            "pricing": {"currency": "USD", "fixed_per_request": "0.01"},
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key="strict-structure-model",
            version=1,
            provider=provider,
            model_identifier="strict-structure-model",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "model_family": "strict_structure_model",
            "capabilities": ["script.structure"],
            "script_structure": {
                "minimum_shot_duration_ms": 1_000,
                "maximum_shot_duration_ms": 10_000,
                "max_input_bytes": 256_000,
                "max_output_tokens": 8_192,
                "timeout_seconds": 30,
                "field_contract": script_structure_field_contract(),
                "chat_request_options": _structure_chat_request_options(),
            },
        })
        profile.save()
        self.deployment = ModelDeployment(
            key="strict-structure-deployment",
            name="Strict structure deployment",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(self.deployment, "runtime_config", {})
        self.deployment.save()
        create_test_prompt_profile_binding(
            actor=self.user,
            model_profile_version=profile,
            operation="script.structure",
        )
        self.structure_smoke = record_deployment_capability_snapshot(
            self.deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["script.structure"],
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "request_sha256": "1" * 64,
                "response_sha256": "2" * 64,
                "result_sha256": "3" * 64,
            },
        )
        self.recipe = ProductionRecipeVersion(
            recipe_key="import-and-structure",
            version=1,
            name="Import and structure",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(self.recipe, "definition", {
            "schema_version": 1,
            "stages": [
                {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
                {
                    "key": "script_structure",
                    "mode": "ai",
                    "approval": "auto",
                    "routing": {
                        "mode": "fixed",
                        "variant_count": 1,
                        "evaluator": None,
                        "routes": [{"model_deployment_id": str(self.deployment.id)}],
                    },
                },
                {"key": "script_review", "mode": "review", "approval": "required"},
            ],
        })
        self.recipe.save()

    def _project_revision(self, content: str, *, suffix: str = ""):
        project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16",
            owner=self.user,
            title=f"人工项目{suffix}",
            width=768, height=1344, fps=24, default_shot_duration_ms=5000,
            continuity_seed=1,
            logline=f"人工梗概{suffix}",
        )
        _source, revision = create_pasted_script_source(
            project=project,
            user=self.user,
            content=content,
        )
        confirm_script_revision(revision, user=self.user)
        revision.refresh_from_db()
        project.master_script = revision.content
        project.save(update_fields=("master_script", "updated_at"))
        return project, revision

    def _additional_structure_deployment(
        self,
        *,
        minimum_duration_ms: int,
        maximum_duration_ms: int,
    ) -> ModelDeployment:
        profile = ModelProfileVersion(
            profile_key=f"strict-structure-model-{ModelProfileVersion.objects.count() + 1}",
            version=1,
            provider=self.deployment.profile_version.provider,
            model_identifier="strict-structure-fallback-model",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "model_family": "strict_structure_fallback_model",
            "capabilities": ["script.structure"],
            "script_structure": {
                "minimum_shot_duration_ms": minimum_duration_ms,
                "maximum_shot_duration_ms": maximum_duration_ms,
                "max_input_bytes": 256_000,
                "max_output_tokens": 8_192,
                "timeout_seconds": 30,
                "field_contract": script_structure_field_contract(),
                "chat_request_options": _structure_chat_request_options(),
            },
        })
        profile.save()
        deployment = ModelDeployment(
            key=f"strict-structure-deployment-{ModelDeployment.objects.count() + 1}",
            name="Strict structure fallback deployment",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        create_test_prompt_profile_binding(
            actor=self.user,
            model_profile_version=profile,
            operation="script.structure",
        )
        record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["script.structure"],
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "request_sha256": "4" * 64,
                "response_sha256": "5" * 64,
                "result_sha256": "6" * 64,
            },
        )
        return deployment

    def _create_run(self, project, revision, *, key: str, **overrides):
        payload = {
            "project_id": str(project.id),
            "script_revision_id": str(revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": key,
            "allow_external_calls": False,
        }
        payload.update(overrides)
        response = self.client.post(
            "/api/v1/production-runs/",
            payload,
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return ProductionRun.objects.get(pk=response.data["id"])

    def test_import_structure_plan_freezes_one_second_duration_before_submission(self):
        project, revision = self._project_revision(
            "第1集：倒数\n镜头1：雨夜电梯内，周岚举起门禁卡。\n"
            "0.15至0.85秒，周岚说：倒数开始了。",
            suffix="-一秒合同",
        )

        run = self._create_run(
            project,
            revision,
            key="structure-one-second-contract",
            episode_count=1,
            shots_per_episode=1,
            episode_duration_seconds=1,
        )

        expected_values = {
            "production.video_shot_min_duration_ms": 1000,
            "production.default_shot_duration_ms": 1000,
            "production.video_shot_max_duration_ms": 1000,
        }
        self.assertEqual(
            {key: run.config_snapshot["values"][key] for key in expected_values},
            expected_values,
        )
        self.assertEqual(
            {key: run.input_snapshot["config_overrides"][key] for key in expected_values},
            expected_values,
        )
        contract = run.input_snapshot["structured_shot_duration_contract"]
        self.assertEqual(contract["source"], "confirmed_revision_structure_plan")
        self.assertEqual(contract["episode_count"], 1)
        self.assertEqual(contract["shots_per_episode"], 1)
        self.assertEqual(contract["episode_duration_ms"], 1000)
        self.assertEqual(contract["minimum_duration_ms"], 1000)
        self.assertEqual(contract["default_duration_ms"], 1000)
        self.assertEqual(contract["maximum_duration_ms"], 1000)
        self.assertEqual(contract["route_stage_key"], "script_structure")
        self.assertEqual(contract["model_policy_key"], "script_structure")
        self.assertEqual(contract["route_policies"], [{
            "route_index": 0,
            "minimum_duration_ms": 1000,
            "maximum_duration_ms": 10000,
        }])
        self.assertEqual(
            run.input_snapshot["structured_shot_duration_contract_sha256"],
            hashlib.sha256(
                json.dumps(
                    contract,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )
        attempt = GenerationAttempt.objects.get(
            stage_run__production_run=run,
            operation="script.structure",
        )
        self.assertEqual(attempt.status, GenerationAttempt.Status.QUEUED)
        self.assertIsNone(attempt.claimed_at)
        self.assertEqual(
            attempt.request_snapshot["plan"]["request"]["options"][
                "minimum_shot_duration_ms"
            ],
            1000,
        )
        self.assertEqual(
            attempt.request_snapshot["plan"]["request"]["options"][
                "maximum_shot_duration_ms"
            ],
            1000,
        )

    def test_import_structure_plan_conflict_is_rejected_before_run_or_attempt(self):
        project, revision = self._project_revision(
            "第1集：过载\n三个镜头在一秒内完成。",
            suffix="-冲突合同",
        )
        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(project.id),
            "script_revision_id": str(revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "structure-duration-conflict",
            "allow_external_calls": False,
            "episode_count": 1,
            "shots_per_episode": 3,
            "episode_duration_seconds": 1,
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("最短镜头时长", str(response.data))
        self.assertFalse(ProductionRun.objects.filter(
            idempotency_key="structure-duration-conflict",
        ).exists())
        self.assertFalse(GenerationAttempt.objects.filter(
            stage_run__production_run__idempotency_key="structure-duration-conflict",
        ).exists())

    def test_import_structure_plan_requires_both_count_and_duration(self):
        project, revision = self._project_revision(
            "第1集：缺项\n一个镜头。",
            suffix="-缺项合同",
        )
        cases = (
            ("missing-duration", {"shots_per_episode": 1}, "episode_duration_seconds"),
            ("missing-count", {"episode_duration_seconds": 1}, "shots_per_episode"),
        )
        for suffix, partial_plan, expected_field in cases:
            with self.subTest(suffix=suffix):
                response = self.client.post("/api/v1/production-runs/", {
                    "project_id": str(project.id),
                    "script_revision_id": str(revision.id),
                    "recipe_version_id": str(self.recipe.id),
                    "idempotency_key": f"structure-plan-{suffix}",
                    "allow_external_calls": False,
                    "episode_count": 1,
                    **partial_plan,
                }, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(expected_field, str(response.data))
                self.assertFalse(ProductionRun.objects.filter(
                    idempotency_key=f"structure-plan-{suffix}",
                ).exists())
        self.assertFalse(GenerationAttempt.objects.filter(
            stage_run__production_run__idempotency_key__startswith="structure-plan-missing-",
        ).exists())

    def test_import_structure_fallback_conflict_is_rejected_before_attempt(self):
        fallback = self._additional_structure_deployment(
            minimum_duration_ms=2000,
            maximum_duration_ms=10000,
        )
        definition = json.loads(json.dumps(self.recipe.definition))
        routing = definition["stages"][1]["routing"]
        routing["mode"] = "ordered_fallback"
        routing["routes"].append({
            "model_deployment_id": str(fallback.id),
        })
        assign_json_contract(self.recipe, "definition", definition)
        self.recipe.save(update_fields=(
            "definition",
            "definition_schema_version",
            "definition_size_bytes",
            "definition_sha256",
            "updated_at",
        ))
        project, revision = self._project_revision(
            "第1集：回退冲突\n一个一秒镜头。",
            suffix="-回退冲突",
        )

        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(project.id),
            "script_revision_id": str(revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "structure-fallback-duration-conflict",
            "allow_external_calls": False,
            "episode_count": 1,
            "shots_per_episode": 1,
            "episode_duration_seconds": 1,
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("最短镜头时长", str(response.data))
        self.assertFalse(ProductionRun.objects.filter(
            idempotency_key="structure-fallback-duration-conflict",
        ).exists())
        self.assertFalse(GenerationAttempt.objects.filter(
            stage_run__production_run__idempotency_key=(
                "structure-fallback-duration-conflict"
            ),
        ).exists())

    def test_import_structure_override_conflict_is_rejected_before_attempt(self):
        project, revision = self._project_revision(
            "第1集：覆盖冲突\n一个一秒镜头。",
            suffix="-覆盖冲突",
        )
        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(project.id),
            "script_revision_id": str(revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "structure-override-duration-conflict",
            "allow_external_calls": False,
            "episode_count": 1,
            "shots_per_episode": 1,
            "episode_duration_seconds": 1,
            "config_overrides": {
                "production.video_shot_min_duration_ms": 2000,
            },
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("单集", str(response.data))
        self.assertFalse(ProductionRun.objects.filter(
            idempotency_key="structure-override-duration-conflict",
        ).exists())
        self.assertFalse(GenerationAttempt.objects.filter(
            stage_run__production_run__idempotency_key=(
                "structure-override-duration-conflict"
            ),
        ).exists())

    def test_import_plan_is_rejected_when_recipe_has_no_structure_stage(self):
        direct_recipe = ProductionRecipeVersion(
            recipe_key="confirmed-without-structure",
            version=1,
            name="Confirmed without structure",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(direct_recipe, "definition", {
            "schema_version": 1,
            "stages": [{
                "key": "script_input",
                "mode": "confirmed_revision",
                "approval": "auto",
            }],
        })
        direct_recipe.save()
        project, revision = self._project_revision(
            "已规范剧本正文。",
            suffix="-无结构化阶段",
        )

        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(project.id),
            "script_revision_id": str(revision.id),
            "recipe_version_id": str(direct_recipe.id),
            "idempotency_key": "structure-plan-without-stage",
            "allow_external_calls": False,
            "episode_count": 1,
            "shots_per_episode": 1,
            "episode_duration_seconds": 1,
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("script_structure", str(response.data))
        self.assertFalse(ProductionRun.objects.filter(
            idempotency_key="structure-plan-without-stage",
        ).exists())

    def test_run_creation_fails_closed_without_script_structure_profile_binding(self):
        project, revision = self._project_revision(
            "第一集\n记者进入雨夜旅馆，寻找失踪者留下的线索。",
            suffix="-missing-profile",
        )
        self.deployment.profile_version.prompt_profile_bindings.update(is_active=False)

        response = self.client.post("/api/v1/production-runs/", {
            "project_id": str(project.id),
            "script_revision_id": str(revision.id),
            "recipe_version_id": str(self.recipe.id),
            "idempotency_key": "structure-missing-profile",
            "allow_external_calls": False,
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("PromptProfile binding", str(response.data))
        self.assertFalse(ProductionRun.objects.filter(
            idempotency_key="structure-missing-profile",
        ).exists())

    @staticmethod
    def _response_for_attempt(attempt):
        plan = attempt.request_snapshot["plan"]
        return json.dumps({
            "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
            "source_revision_id": plan["source_revision"]["id"],
            "source_sha256": plan["source_revision"]["content_sha256"],
            "source_size_bytes": plan["source_revision"]["content_size_bytes"],
            "title": "AI 冻结标题",
            "logline": "AI 冻结梗概",
            "bible": {"characters": [], "scenes": [], "props": []},
            "episodes": [{
                "title": "第一集",
                "shots": [{
                    "source_lines": plan["request"]["non_empty_source_line_numbers"],
                    "setting": "雨夜旅馆",
                    "action": "林雾进入旅馆，发现自行转动的磁带。",
                    "duration_ms": int(
                        plan["request"]["options"]["minimum_shot_duration_ms"]
                    ),
                    "dialogue": {
                        "speaker": "", "delivery": "", "text": "",
                        "start_ms": 0, "end_ms": 0,
                    },
                }],
            }],
        }, ensure_ascii=False)

    def test_noncanonical_import_persists_attempt_preview_and_adopts_only_frozen_metadata(self):
        project, original = self._project_revision(
            "林雾进入雨夜旅馆。\n她发现一盘自行转动的磁带。",
        )
        run = self._create_run(project, original, key="structure-success")
        self.assertEqual(run.current_stage_key, "script_structure")
        self.assertEqual(run.input_snapshot["project_base_title"], project.title)
        self.assertEqual(run.input_snapshot["project_base_logline"], project.logline)

        attempt = claim_next_text_attempt("structure-worker")
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.operation, "script.structure")
        self.assertEqual(
            attempt.request_snapshot["plan"]["request"]["options"],
            {
                "minimum_shot_duration_ms": 5_000,
                "maximum_shot_duration_ms": 10_000,
                "max_output_tokens": 8_192,
                "timeout_seconds": 30.0,
                "chat_request_options": _structure_chat_request_options(),
            },
        )
        driver = Mock()
        driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "structure-request-1",
            "text": self._response_for_attempt(attempt),
        }
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(attempt)

        run.refresh_from_db()
        original.refresh_from_db()
        preview = ScriptRevision.objects.get(pk=run.script_revision_id)
        self.assertEqual(original.status, ScriptRevision.Status.CONFIRMED)
        self.assertEqual(preview.status, ScriptRevision.Status.PREVIEW)
        self.assertNotEqual(preview.id, original.id)
        self.assertEqual(preview.origin_generation_attempt_id, attempt.id)
        self.assertEqual(run.current_stage_key, "script_review")
        self.assertEqual(run.status, ProductionRun.Status.AWAITING_APPROVAL)

        approved = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_review",
            "decision": "approve",
            "comment": "结构化候选与原稿逐项核对通过",
            "adopt_generated_metadata": True,
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        preview.refresh_from_db()
        original.refresh_from_db()
        project.refresh_from_db()
        self.assertEqual(preview.status, ScriptRevision.Status.CONFIRMED)
        self.assertEqual(original.status, ScriptRevision.Status.CONFIRMED)
        self.assertEqual(project.master_script, preview.content)
        self.assertEqual(project.title, "AI 冻结标题")
        self.assertEqual(project.logline, "AI 冻结梗概")
        decision = ApprovalDecision.objects.get(stage_run__production_run=run)
        self.assertTrue(decision.output_snapshot_sha256)
        review = StageRun.objects.get(production_run=run, stage_key="script_review")
        evidence = review.output_snapshot["review_payload"]
        self.assertEqual(evidence["metadata_adoption"]["title"]["status"], "adopted")
        self.assertEqual(evidence["metadata_adoption"]["logline"]["status"], "adopted")

    @staticmethod
    def _safe_length_failure() -> ProviderDriverError:
        failure = ProviderDriverError(
            "raw provider message TOP-SECRET",
            code="provider_response_invalid",
            submission_uncertain=False,
        )
        failure.metadata = {
            "contract_error_code": "response_output_budget_exhausted",
            "finish_reason": "length",
            "usage": {
                "prompt_tokens": 321,
                "completion_tokens": 8_192,
                "total_tokens": 8_513,
                "cached_tokens": 77,
            },
            "response_id_sha256": hashlib.sha256(
                b"provider-request-id",
            ).hexdigest(),
            "raw_message": "TOP-SECRET metadata",
            "provider_payload": {"secret": "must-not-persist"},
        }
        return failure

    def _assert_safe_failure_evidence(self, attempt: GenerationAttempt) -> None:
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, GenerationAttempt.Status.FAILED)
        self.assertEqual(attempt.error_code, "provider_response_invalid")
        resolution = GenerationAttemptResolution.objects.get(attempt=attempt)
        self.assertEqual(
            resolution.outcome,
            GenerationAttemptResolution.Outcome.KNOWN_RESPONSE_REJECTED,
        )
        evidence = resolution.evidence
        self.assertEqual(
            evidence["contract_error_code"],
            "response_output_budget_exhausted",
        )
        self.assertEqual(evidence["finish_reason"], "length")
        self.assertEqual(evidence["usage"], {
            "prompt_tokens": 321,
            "completion_tokens": 8_192,
            "total_tokens": 8_513,
        })
        self.assertEqual(
            evidence["response_id_sha256"],
            hashlib.sha256(b"provider-request-id").hexdigest(),
        )
        persisted = json.dumps(
            {"evidence": evidence, "note": resolution.note},
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn("TOP-SECRET", persisted)
        self.assertNotIn("must-not-persist", persisted)
        self.assertNotIn("cached_tokens", persisted)
        self.assertNotIn("raw_message", persisted)
        self.assertNotIn("provider_payload", persisted)

    def test_script_structure_driver_failure_persists_only_safe_metadata(self):
        project, revision = self._project_revision(
            "林雾进入雨夜旅馆。\n她发现一盘自行转动的磁带。",
            suffix="-结构失败元数据",
        )
        self._create_run(
            project,
            revision,
            key="structure-safe-failure-metadata",
        )
        attempt = claim_next_text_attempt("structure-safe-failure-worker")
        driver = Mock()
        driver.invoke.side_effect = self._safe_length_failure()

        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(attempt)

        self._assert_safe_failure_evidence(attempt)

    def test_visual_rules_driver_failure_persists_only_safe_metadata(self):
        project, revision = self._project_revision(
            "林雾进入雨夜旅馆。\n她发现一盘自行转动的磁带。",
            suffix="-视觉规则失败元数据",
        )
        self._create_run(
            project,
            revision,
            key="visual-rules-safe-failure-metadata",
        )
        queued = GenerationAttempt.objects.get(
            stage_run__production_run__idempotency_key=(
                "visual-rules-safe-failure-metadata"
            ),
        )
        request_snapshot = dict(queued.request_snapshot)
        request_snapshot["plan"] = {
            "schema_version": 1,
            "operation": "visual_rules.generate",
        }
        assign_json_contract(queued, "request_snapshot", request_snapshot)
        queued.operation = "visual_rules.generate"
        queued.save(update_fields=(
            "operation",
            "request_snapshot",
            "request_snapshot_schema_version",
            "request_snapshot_size_bytes",
            "request_snapshot_sha256",
            "updated_at",
        ))
        attempt = claim_next_text_attempt("visual-rules-safe-failure-worker")
        driver = Mock()
        driver.invoke.side_effect = self._safe_length_failure()

        with (
            patch("studio.services.text_generation_queue._verify_attempt_prompt_profile"),
            patch("studio.services.text_generation_queue.verify_visual_rules_smoke"),
            patch(
                "studio.services.text_generation_queue.frozen_text_deployment",
                return_value=self.deployment,
            ),
            patch(
                "studio.services.text_generation_queue.visual_rules_provider_payload",
                return_value={"messages": []},
            ),
            patch("studio.services.text_generation_queue.driver_for", return_value=driver),
        ):
            process_text_attempt(attempt)

        self._assert_safe_failure_evidence(attempt)

    def test_concurrent_project_metadata_edits_are_preserved_and_audited(self):
        project, original = self._project_revision(
            "林雾进入雨夜旅馆。\n她听见磁带中的求救声。",
            suffix="-并发",
        )
        run = self._create_run(project, original, key="structure-concurrent")
        attempt = claim_next_text_attempt("structure-worker-concurrent")
        driver = Mock()
        driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "structure-request-2",
            "text": self._response_for_attempt(attempt),
        }
        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(attempt)
        project.title = "人工并发标题"
        project.logline = "人工并发梗概"
        project.save(update_fields=("title", "logline", "updated_at"))

        approved = self.client.post(f"/api/v1/production-runs/{run.id}/approve/", {
            "stage_key": "script_review",
            "decision": "approve",
            "adopt_generated_metadata": True,
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        project.refresh_from_db()
        self.assertEqual(project.title, "人工并发标题")
        self.assertEqual(project.logline, "人工并发梗概")
        evidence = StageRun.objects.get(
            production_run=run,
            stage_key="script_review",
        ).output_snapshot["review_payload"]
        self.assertEqual(evidence["metadata_adoption"]["title"]["status"], "not_adopted")
        self.assertEqual(evidence["metadata_adoption"]["title"]["reason"], "concurrent_edit")
        self.assertEqual(evidence["metadata_adoption"]["logline"]["status"], "not_adopted")
        self.assertEqual(evidence["metadata_adoption"]["logline"]["reason"], "concurrent_edit")

    def test_canonical_import_creates_review_snapshot_without_generation_attempt(self):
        # Exact canonical input is intentionally executable without a model
        # route or smoke; only non-canonical text crosses the submission gate.
        self.deployment.capability_snapshots.all().delete()
        definition = dict(self.recipe.definition)
        definition["stages"] = [
            {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
            {"key": "script_structure", "mode": "ai", "approval": "auto"},
            {"key": "script_review", "mode": "review", "approval": "required"},
        ]
        assign_json_contract(self.recipe, "definition", definition)
        self.recipe.save(update_fields=(
            "definition", "definition_schema_version", "definition_size_bytes",
            "definition_sha256", "updated_at",
        ))
        canonical = render_compatible_master_script(
            {"characters": [], "scenes": [], "props": []},
            [{
                "title": "已规范",
                "shots": [{
                    "setting": "空镜走廊",
                    "action": "灯光逐盏熄灭。",
                    "duration_ms": 2_000,
                    "dialogue": {
                        "speaker": "", "delivery": "", "text": "",
                        "start_ms": 0, "end_ms": 0,
                    },
                }],
            }],
        )
        project, original = self._project_revision(canonical, suffix="-规范")
        run = self._create_run(project, original, key="structure-no-op")

        self.assertFalse(GenerationAttempt.objects.filter(stage_run__production_run=run).exists())
        self.assertEqual(run.script_revision_id, original.id)
        self.assertEqual(run.current_stage_key, "script_review")
        stage = StageRun.objects.get(production_run=run, stage_key="script_structure")
        self.assertEqual(
            stage.output_snapshot["review_payload"]["submission_semantics"],
            "no_submission",
        )
        self.assertEqual(stage.output_snapshot["review_payload"]["script_revision_id"], str(original.id))
        self.assertEqual(
            GeneratedArtifact.objects.filter(
                production_run=run,
                kind=GeneratedArtifact.Kind.SCRIPT,
            ).count(),
            1,
        )

    def test_dedicated_structure_smoke_is_api_configurable_and_hash_only(self):
        self.deployment.capability_snapshots.all().delete()
        driver = Mock()
        driver.estimate.return_value = {
            "schema_version": 1,
            "amount": "0.01",
            "currency": "USD",
            "basis": "fixed_per_request",
        }

        def invoke(_deployment, operation, payload):
            self.assertEqual(operation, "chat_completion")
            request_body = json.loads(payload["messages"][1]["content"])
            source = request_body["source_revision"]
            return {
                "schema_version": 1,
                "request_id": "structure-smoke-request-plaintext",
                "text": json.dumps({
                    "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
                    "source_revision_id": source["id"],
                    "source_sha256": source["content_sha256"],
                    "source_size_bytes": source["content_size_bytes"],
                    "title": "灯塔来信",
                    "logline": "旅人在灯塔中发现一封旧信。",
                    "bible": {"characters": [], "scenes": [], "props": []},
                    "episodes": [{
                        "title": "来信",
                        "shots": [{
                            "source_lines": request_body["non_empty_source_line_numbers"],
                            "setting": "风雨灯塔",
                            "action": "旅人进入灯塔并发现旧信。",
                            "duration_ms": 1_000,
                            "dialogue": {
                                "speaker": "", "delivery": "", "text": "",
                                "start_ms": 0, "end_ms": 0,
                            },
                        }],
                    }],
                }, ensure_ascii=False),
            }

        driver.invoke.side_effect = invoke
        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            response = self.client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/smoke-test/",
                {"operation": "script.structure"},
                format="json",
            )
            attempt = claim_next_deployment_smoke("structure-smoke-test-worker")
            finished = process_deployment_smoke(attempt)

        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(finished.status, ModelDeploymentSmokeAttempt.Status.SUCCEEDED)
        snapshot = finished.final_snapshot
        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.PASSED)
        self.assertEqual(snapshot.operations, ["script.structure"])
        persisted = json.dumps(snapshot.evidence, ensure_ascii=False)
        self.assertNotIn("旅人在灯塔", persisted)
        self.assertNotIn("structure-smoke-request-plaintext", persisted)
        self.assertEqual(len(snapshot.evidence["result_sha256"]), 64)

    def test_script_generate_smoke_does_not_authorize_structure_submission(self):
        self.deployment.capability_snapshots.all().delete()
        record_deployment_capability_snapshot(
            self.deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["script.generate"],
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "request_sha256": "4" * 64,
                "response_sha256": "5" * 64,
                "result_sha256": "6" * 64,
            },
        )
        project, original = self._project_revision(
            "旅人进入灯塔。\n旅人发现一封旧信。",
            suffix="-错误烟测",
        )

        run = self._create_run(project, original, key="structure-wrong-smoke")

        self.assertFalse(GenerationAttempt.objects.filter(
            stage_run__production_run=run,
            operation="script.structure",
        ).exists())
        stage = StageRun.objects.get(production_run=run, stage_key="script_structure")
        self.assertEqual(stage.reason_code, "script_structure_smoke_missing_or_stale")

    def test_model_profile_publish_rejects_incomplete_structure_contract(self):
        draft = ModelProfileVersion(
            profile_key="incomplete-structure-model",
            version=1,
            provider=self.deployment.profile_version.provider,
            model_identifier="incomplete-structure-model",
            modality=ModelProfileVersion.Modality.TEXT,
        )
        assign_json_contract(draft, "specification", {
            "schema_version": 1,
            "capabilities": ["script.structure"],
            "script_structure": {
                "minimum_shot_duration_ms": 1_000,
                "maximum_shot_duration_ms": 10_000,
                # max_input_bytes is intentionally absent.
                "max_output_tokens": 8_192,
                "timeout_seconds": 30,
                "field_contract": script_structure_field_contract(),
                "chat_request_options": _structure_chat_request_options(),
            },
        })
        draft.save()

        response = self.client.post(
            f"/api/v1/model-profile-versions/{draft.id}/publish/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("script_structure policy", str(response.data))
        draft.refresh_from_db()
        self.assertEqual(draft.status, ModelProfileVersion.Status.DRAFT)

    def test_worker_rechecks_structure_smoke_at_submission_boundary(self):
        project, original = self._project_revision(
            "旅人进入灯塔。\n旅人发现一封旧信。",
            suffix="-烟测过期",
        )
        run = self._create_run(project, original, key="structure-smoke-expired")
        attempt = claim_next_text_attempt("structure-expired-worker")
        self.assertIsNotNone(attempt)
        self.structure_smoke.expires_at = timezone.now() - timedelta(seconds=1)
        self.structure_smoke.save(update_fields=("expires_at", "updated_at"))
        driver = Mock()

        with patch("studio.services.text_generation_queue.driver_for", return_value=driver):
            process_text_attempt(attempt)

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, GenerationAttempt.Status.FAILED)
        self.assertEqual(
            attempt.error_code,
            "script_structure_smoke_missing_or_stale",
        )
        driver.invoke.assert_not_called()
        self.assertEqual(run.artifacts.filter(role="structured_script").count(), 0)
