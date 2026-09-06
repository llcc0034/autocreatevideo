import hashlib
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    ApprovalDecision,
    Asset,
    CharacterBible,
    Episode,
    GeneratedArtifact,
    GenerationAttempt,
    PropBible,
    ProductionRecipeVersion,
    ProductionRun,
    PromptFieldTranslation,
    SceneBible,
    SeriesProject,
    Shot,
    StageRun,
)
from studio.services.json_contracts import (
    assign_json_contract,
    canonical_json_bytes,
    json_contract,
)
from studio.services.reference_bindings import (
    ReferenceBindingError,
    validate_reference_bindings_against_mode_contract,
)
from studio.tests.factories import conditioning_contract
from studio.services.prompt_normalization import PROMPT_NORMALIZE_OPERATION
from studio.services.prompt_translation import prepare_prompt_normalization_item
from studio.services.prompt_translation import confirm_prompt_normalization_candidates
from studio.services.production_runs import (
    ProductionRunCreateRejected,
    StageBlocked,
    UNATTENDED_DECISION_POLICY_REGISTRY,
    _execute_unattended_policy_review,
    _freeze_unattended_reference_inputs,
    _recipe_stages,
    _replace_unattended_identity_tokens,
    _sha256,
    _unattended_prompt_normalization_overrides,
    _verified_unattended_reference_inputs,
    create_production_run,
)
from studio.services.security_boundaries import boundary_int


class UnattendedProductionRunTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="unattended-owner",
            password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16", 
            owner=self.user,
            title="无人值守短剧",
            width=768, height=1344, fps=24, default_shot_duration_ms=5000,
            continuity_seed=1,
            production_settings={"default_conditioning_mode": "Ref2VA"},
        )

    @staticmethod
    def _routing(raw, *, stage_key):
        if raw is None:
            return {
                "schema_version": 1,
                "mode": "unconfigured",
                "routes": [],
                "variant_count": 1,
                "evaluator": None,
            }
        return {
            "schema_version": 1,
            "mode": raw["mode"],
            "routes": [{"index": 0}],
            "variant_count": raw.get("variant_count", 1),
            "evaluator": raw.get("evaluator"),
        }

    def _definition(self):
        review = UNATTENDED_DECISION_POLICY_REGISTRY
        return {
            "schema_version": 2,
            "interaction_mode": "unattended",
            "stages": [
                {
                    "key": "script_generate",
                    "mode": "ai",
                    "approval": "auto",
                    "routing": {
                        "mode": "fixed",
                        "routes": [{"model_deployment_id": "placeholder"}],
                        "variant_count": 1,
                        "evaluator": None,
                    },
                },
                {
                    "key": "script_review",
                    "mode": "review",
                    "approval": "policy",
                    "decision_policy": review["script_review"],
                },
                {"key": "bible_materialize", "mode": "deterministic", "approval": "auto"},
                {
                    "key": "bible_review",
                    "mode": "review",
                    "approval": "policy",
                    "decision_policy": review["bible_review"],
                },
                {"key": "storyboard", "mode": "deterministic", "approval": "auto"},
                {
                    "key": "script_normalize",
                    "mode": "ai",
                    "approval": "auto",
                    "routing": {
                        "mode": "fixed",
                        "routes": [{"model_deployment_id": "placeholder"}],
                        "variant_count": 1,
                        "evaluator": None,
                    },
                },
                {
                    "key": "script_normalize_review",
                    "mode": "review",
                    "approval": "policy",
                    "decision_policy": review["script_normalize_review"],
                },
                {
                    "key": "bible_image_generation",
                    "mode": "existing_pipeline",
                    "approval": "auto",
                    "routing": {
                        "mode": "fixed",
                        "routes": [{
                            "workflow_version_id": "placeholder",
                            "model_deployment_id": "placeholder",
                        }],
                        "variant_count": 1,
                        "evaluator": None,
                    },
                },
                {
                    "key": "bible_image_review",
                    "mode": "review",
                    "approval": "policy",
                    "decision_policy": review["bible_image_review"],
                },
                {
                    "key": "storyboard_review",
                    "mode": "review",
                    "approval": "policy",
                    "decision_policy": review["storyboard_review"],
                },
                {"key": "prompt_compile", "mode": "existing_pipeline", "approval": "auto"},
                {
                    "key": "media_generation",
                    "mode": "existing_pipeline",
                    "approval": "auto",
                    "routing": {
                        "mode": "fixed",
                        "routes": [{"model_deployment_id": "placeholder"}],
                        "variant_count": 1,
                        "evaluator": None,
                    },
                },
                {
                    "key": "artifact_selection",
                    "mode": "review",
                    "approval": "policy",
                    "decision_policy": review["artifact_selection"],
                },
                {"key": "assembly", "mode": "existing_pipeline", "approval": "auto"},
                {"key": "delivery", "mode": "existing_pipeline", "approval": "auto"},
            ],
        }

    def _recipe(self, definition=None):
        recipe = ProductionRecipeVersion(
            recipe_key=f"unattended-{ProductionRecipeVersion.objects.count() + 1}",
            version=1,
            name="Unattended policy recipe",
        )
        assign_json_contract(recipe, "definition", definition or self._definition())
        recipe.save()
        return recipe

    def test_schema_v2_registers_exact_policy_hashes_and_rejects_human_gates(self):
        self.assertEqual(
            UNATTENDED_DECISION_POLICY_REGISTRY["script_normalize_review"],
            "accept_all_valid_frozen_prompt_translations_v2",
        )
        self.assertEqual(
            UNATTENDED_DECISION_POLICY_REGISTRY["bible_image_review"],
            "select_single_bible_candidate_per_entity_v1",
        )
        self.assertEqual(
            UNATTENDED_DECISION_POLICY_REGISTRY["storyboard_review"],
            "bind_frozen_reference_inputs_by_contract_v2",
        )
        recipe = self._recipe()
        with patch(
            "studio.services.production_runs._freeze_stage_routing",
            side_effect=self._routing,
        ):
            stages = _recipe_stages(recipe)

        reviews = {item["key"]: item for item in stages if item["mode"] == "review"}
        self.assertEqual(
            set(reviews),
            set(UNATTENDED_DECISION_POLICY_REGISTRY) - {"visual_rules_review"},
        )
        self.assertTrue(all(len(item["decision_policy_sha256"]) == 64 for item in reviews.values()))

        required = self._definition()
        required["stages"][1]["approval"] = "required"
        required_recipe = self._recipe(required)
        with patch(
            "studio.services.production_runs._freeze_stage_routing",
            side_effect=self._routing,
        ), self.assertRaisesMessage(ValidationError, "禁止 approval=required"):
            _recipe_stages(required_recipe)

        manual_choice = self._definition()
        manual_choice["stages"][0]["routing"]["mode"] = "manual_choice"
        manual_recipe = self._recipe(manual_choice)
        with patch(
            "studio.services.production_runs._freeze_stage_routing",
            side_effect=self._routing,
        ), self.assertRaisesMessage(ValidationError, "禁止 routing.mode=manual_choice"):
            _recipe_stages(manual_recipe)

        missing_normalization = self._definition()
        missing_normalization["stages"] = [
            item for item in missing_normalization["stages"]
            if item["key"] not in {"script_normalize", "script_normalize_review"}
        ]
        missing_recipe = self._recipe(missing_normalization)
        with patch(
            "studio.services.production_runs._freeze_stage_routing",
            side_effect=self._routing,
        ), self.assertRaisesMessage(ValidationError, "完整且唯一"):
            _recipe_stages(missing_recipe)

        wrong_normalization_policy = self._definition()
        next(
            item for item in wrong_normalization_policy["stages"]
            if item["key"] == "script_normalize_review"
        )["decision_policy"] = "unsafe-pass-through"
        wrong_policy_recipe = self._recipe(wrong_normalization_policy)
        with patch(
            "studio.services.production_runs._freeze_stage_routing",
            side_effect=self._routing,
        ), self.assertRaisesMessage(ValidationError, "未注册或不匹配"):
            _recipe_stages(wrong_policy_recipe)

        retired_storyboard_policy = self._definition()
        next(
            item for item in retired_storyboard_policy["stages"]
            if item["key"] == "storyboard_review"
        )["decision_policy"] = "bind_single_dynamic_reference_video_v1"
        retired_storyboard_recipe = self._recipe(retired_storyboard_policy)
        with patch(
            "studio.services.production_runs._freeze_stage_routing",
            side_effect=self._routing,
        ), self.assertRaisesMessage(ValidationError, "未注册或不匹配"):
            _recipe_stages(retired_storyboard_recipe)

        multiple = self._definition()
        next(
            item for item in multiple["stages"]
            if item["key"] == "media_generation"
        )["routing"]["variant_count"] = 2
        multiple_recipe = self._recipe(multiple)
        with patch(
            "studio.services.production_runs._freeze_stage_routing",
            side_effect=self._routing,
        ), self.assertRaisesMessage(ValidationError, "单 route、单候选"):
            _recipe_stages(multiple_recipe)

    def test_unattended_new_script_rejects_a_project_with_existing_bible(self):
        CharacterBible.objects.create(
            project=self.project,
            slug="legacy-character",
            name="历史人物",
            stable_speaker_id="S1",
        )
        recipe = self._recipe()
        recipe.status = ProductionRecipeVersion.Status.PUBLISHED
        recipe.save(update_fields=("status", "updated_at"))

        with patch(
            "studio.services.production_runs.validate_production_recipe_executability",
            return_value=[{"key": "script_generate", "mode": "ai"}],
        ), self.assertRaises(ProductionRunCreateRejected) as captured:
            create_production_run(
                user=self.user,
                project=self.project,
                script_revision=None,
                recipe=recipe,
                idempotency_key="unattended-existing-bible",
                allow_external_calls=None,
                budget_limit=None,
                max_generation_tasks=None,
                idea="生成一部新短剧",
                shots_per_episode=2,
                episode_duration_seconds=4,
                unattended_confirmed=True,
                reference_inputs=[],
            )

        self.assertEqual(
            captured.exception.code,
            "unattended_existing_bible_requires_new_project",
        )

    def _normalization_policy_fixture(self, *, translation_glossary=None):
        if translation_glossary is not None:
            settings_value = dict(self.project.production_settings or {})
            settings_value["translation_glossary"] = dict(translation_glossary)
            self.project.production_settings = settings_value
            self.project.save(update_fields=("production_settings", "updated_at"))
        recipe = self._recipe()
        policy = UNATTENDED_DECISION_POLICY_REGISTRY["script_normalize_review"]
        policy_hash = _sha256({
            "schema_version": 1,
            "interaction_mode": "unattended",
            "stage_key": "script_normalize_review",
            "decision_policy": policy,
            "recipe_definition_sha256": recipe.definition_sha256,
        })
        config = json_contract({"schema_version": 1})
        run_input = json_contract({
            "schema_version": 3,
            "interaction_mode": "unattended",
            "unattended_confirmed": True,
        })
        run = ProductionRun.objects.create(
            owner=self.user,
            project=self.project,
            recipe_version=recipe,
            idempotency_key=f"normalization-policy-{ProductionRun.objects.count() + 1}",
            request_sha256="a" * 64,
            config_snapshot=config.value,
            config_snapshot_schema_version=config.schema_version,
            config_snapshot_size_bytes=config.size_bytes,
            config_snapshot_sha256=config.sha256,
            input_snapshot=run_input.value,
            input_snapshot_schema_version=run_input.schema_version,
            input_snapshot_size_bytes=run_input.size_bytes,
            input_snapshot_sha256=run_input.sha256,
        )
        episode = Episode.objects.create(prompt_settings={"include_audio": True}, 
            project=self.project,
            number=1,
            title="雨夜来客",
            script="林遥在旧公寓门口举起便携录音机。",
        )
        character = CharacterBible.objects.create(
            project=self.project,
            slug="lin-yao",
            name="林遥",
            stable_speaker_id="S1",
            visual_identity="年轻男快递员，身穿黄色雨衣，神情紧张。",
        )
        scene = SceneBible.objects.create(
            project=self.project,
            slug="scene-e2a2-real-shape",
            name="旧公寓门口",
            visual_identity="雨夜中的老旧公寓入口，绿色门灯忽明忽暗。",
        )
        prop = PropBible.objects.create(
            project=self.project,
            slug="prop-b884-real-shape",
            name="便携录音机",
            visual_identity="复古便携式磁带录音机，黄铜按键有明显划痕。",
        )
        shot = Shot.objects.create(include_audio=True, 
            episode=episode,
            number=1,
            narrative="林遥在旧公寓门口举起便携录音机。",
            duration_ms=1000,
            media_type=Shot.MediaType.VIDEO,
            conditioning_mode="Ref2VA",
        )
        routing = json_contract({
            "schema_version": 1,
            "mode": "unconfigured",
            "routes": [],
            "variant_count": 1,
            "evaluator": None,
        })
        empty = json_contract({"schema_version": 1})

        def create_stage(key, ordinal, mode, status, *, decision_policy="", policy_sha=""):
            return StageRun.objects.create(
                production_run=run,
                stage_key=key,
                ordinal=ordinal,
                mode=mode,
                status=status,
                idempotency_key=f"{run.id}:{ordinal}:{key}",
                decision_policy=decision_policy,
                decision_policy_sha256=policy_sha,
                routing=routing.value,
                routing_schema_version=routing.schema_version,
                routing_size_bytes=routing.size_bytes,
                routing_sha256=routing.sha256,
                routing_decision=empty.value,
                routing_decision_schema_version=empty.schema_version,
                routing_decision_size_bytes=empty.size_bytes,
                routing_decision_sha256=empty.sha256,
                input_snapshot=empty.value,
                input_snapshot_schema_version=empty.schema_version,
                input_snapshot_size_bytes=empty.size_bytes,
                input_snapshot_sha256=empty.sha256,
                output_snapshot=empty.value,
                output_snapshot_schema_version=empty.schema_version,
                output_snapshot_size_bytes=empty.size_bytes,
                output_snapshot_sha256=empty.sha256,
            )

        normalize_stage = create_stage(
            "script_normalize",
            1,
            "ai",
            StageRun.Status.SUCCEEDED,
        )
        attempt = GenerationAttempt.objects.create(
            stage_run=normalize_stage,
            unit_key="prompt-normalize",
            attempt_number=1,
            status=GenerationAttempt.Status.SUCCEEDED,
            operation=PROMPT_NORMALIZE_OPERATION,
            request_sha256="b" * 64,
            result_sha256="c" * 64,
        )
        specs = (
            (
                PromptFieldTranslation.ScopeType.CHARACTER,
                character.id,
                "character.name",
                character.name,
                "S1",
            ),
            (
                PromptFieldTranslation.ScopeType.CHARACTER,
                character.id,
                "character.visual_identity",
                character.visual_identity,
                "S1, a young male courier in a yellow raincoat with a tense expression.",
            ),
            (
                PromptFieldTranslation.ScopeType.SCENE,
                scene.id,
                "scene.name",
                scene.name,
                "scene:scene-e2a2-real-shape",
            ),
            (
                PromptFieldTranslation.ScopeType.SCENE,
                scene.id,
                "scene.visual_identity",
                scene.visual_identity,
                (
                    "scene:scene-e2a2-real-shape, an old apartment entrance at night "
                    "under a flickering green door light."
                ),
            ),
            (
                PromptFieldTranslation.ScopeType.PROP,
                prop.id,
                "prop.name",
                prop.name,
                "prop:prop-b884-real-shape",
            ),
            (
                PromptFieldTranslation.ScopeType.PROP,
                prop.id,
                "prop.visual_identity",
                prop.visual_identity,
                (
                    "prop:prop-b884-real-shape, a vintage portable cassette recorder "
                    "with scratched brass buttons."
                ),
            ),
            (
                PromptFieldTranslation.ScopeType.SHOT,
                shot.id,
                "shot.narrative",
                shot.narrative,
                (
                    "[0.000-0.500] S1 waits at scene:scene-e2a2-real-shape.\n"
                    "[0.500-1.000] S1 raises prop:prop-b884-real-shape."
                ),
            ),
        )
        candidates = []
        requests = []
        targets = []
        for index, (scope_type, scope_id, field_key, source, translated) in enumerate(
            specs,
            start=1,
        ):
            request_id = f"unattended-real-shape-{index}"
            request, target = prepare_prompt_normalization_item(
                project=self.project,
                scope_type=scope_type,
                scope_id=scope_id,
                field_key=field_key,
                source_text=source,
                label=request_id,
                request_id=request_id,
            )
            requests.append(request)
            targets.append(target)
            candidates.append({
                key: value for key, value in target.items()
                if key in {
                    "request_id", "scope_type", "scope_id", "field_key",
                    "source_text", "source_hash", "context_hash",
                }
            } | {"translated_text": translated})
        if translation_glossary:
            glossary_replacements = {
                "S1": translation_glossary.get(character.name),
                f"scene:{scene.slug}": translation_glossary.get(scene.name),
                f"prop:{prop.slug}": translation_glossary.get(prop.name),
            }
            glossary_replacements = {
                source: target
                for source, target in glossary_replacements.items()
                if target
            }
            for candidate in candidates:
                for source, target in glossary_replacements.items():
                    candidate["translated_text"] = candidate["translated_text"].replace(
                        source,
                        target,
                    )
        frozen_payload = json_contract({"schema_version": 1, "items": requests})
        frozen_request = json_contract({
            "schema_version": 1,
            "payload": frozen_payload.value,
            "payload_sha256": frozen_payload.sha256,
            "targets": targets,
        })
        attempt.request_sha256 = frozen_payload.sha256
        attempt.request_snapshot = frozen_request.value
        attempt.request_snapshot_schema_version = frozen_request.schema_version
        attempt.request_snapshot_size_bytes = frozen_request.size_bytes
        attempt.request_snapshot_sha256 = frozen_request.sha256
        attempt.save(update_fields=(
            "request_sha256", "request_snapshot", "request_snapshot_schema_version",
            "request_snapshot_size_bytes", "request_snapshot_sha256", "updated_at",
        ))
        assign_json_contract(normalize_stage, "output_snapshot", {
            "schema_version": 1,
            "review_payload": {
                "kind": "prompt_normalization_candidates",
                "attempt_id": str(attempt.id),
                "result_sha256": attempt.result_sha256,
                "provider": "test-provider",
                "model": "strict-normalizer",
                "submission_semantics": "durable_single_submit_manual_resolution",
                "candidates": candidates,
            },
        })
        normalize_stage.save(update_fields=(
            "output_snapshot", "output_snapshot_schema_version",
            "output_snapshot_size_bytes", "output_snapshot_sha256", "updated_at",
        ))
        review_stage = create_stage(
            "script_normalize_review",
            2,
            "review",
            StageRun.Status.RUNNING,
            decision_policy=policy,
            policy_sha=policy_hash,
        )
        return run, shot, review_stage, policy, policy_hash

    def test_prompt_normalization_policy_materializes_frozen_candidates_and_api_audit(self):
        run, _shot, review_stage, policy, policy_hash = (
            self._normalization_policy_fixture()
        )

        reference = _execute_unattended_policy_review(run, review_stage)

        translation = PromptFieldTranslation.objects.get(
            project=self.project,
            field_key="shot.narrative",
        )
        translations = list(PromptFieldTranslation.objects.filter(project=self.project))
        names = {
            item.source_text: item.translated_text
            for item in translations
            if item.field_key.endswith(".name")
        }
        decision = review_stage.approval_decisions.get()
        self.assertTrue(reference.startswith("approval:"))
        self.assertEqual(translation.status, PromptFieldTranslation.Status.CONFIRMED)
        self.assertIsNone(translation.confirmed_by)
        self.assertEqual(names["林遥"], "Young Male Courier Yellow")
        self.assertEqual(names["旧公寓门口"], "Old Apartment Entrance Night")
        self.assertEqual(names["便携录音机"], "Vintage Portable Cassette Recorder")
        self.assertEqual(
            translation.translated_text,
            (
                "[0.000-0.500] Young Male Courier Yellow waits at "
                "Old Apartment Entrance Night.\n"
                "[0.500-1.000] Young Male Courier Yellow raises "
                "Vintage Portable Cassette Recorder."
            ),
        )
        self.assertEqual(translation.translated_text.count("\n"), 1)
        serialized_translations = "\n".join(item.translated_text for item in translations)
        for provisional in (
            "S1", "scene:scene-e2a2-real-shape", "prop:prop-b884-real-shape",
        ):
            self.assertNotIn(provisional, serialized_translations)
        self.project.refresh_from_db()
        self.assertEqual(
            self.project.production_settings["translation_glossary"],
            names,
        )
        self.assertEqual(decision.actor, ApprovalDecision.Actor.RECIPE_POLICY)
        self.assertIsNone(decision.decided_by)
        self.assertEqual(decision.decision_policy, policy)
        self.assertEqual(decision.decision_policy_sha256, policy_hash)
        evidence = review_stage.output_snapshot["policy_evidence"]
        self.assertEqual(evidence["kind"], "unattended_prompt_normalization_policy")
        self.assertEqual(evidence["candidate_count"], 7)
        self.assertEqual(len(evidence["canonical_identity_rewrites"]), 3)
        self.assertEqual(
            {item["canonical_identity"] for item in evidence["canonical_identity_rewrites"]},
            set(names.values()),
        )
        self.assertTrue(all(
            item["derivation"] == "visual_identity"
            for item in evidence["canonical_identity_rewrites"]
        ))

        client = APIClient()
        client.force_authenticate(self.user)
        detail = client.get(f"/api/v1/production-runs/{run.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        review_fact = next(
            item for item in detail.data["stages"]
            if item["stage_key"] == "script_normalize_review"
        )["approval_decision_facts"][0]
        self.assertEqual(review_fact["actor"], ApprovalDecision.Actor.RECIPE_POLICY)
        self.assertIsNone(review_fact["decided_by"])
        self.assertEqual(review_fact["decision_policy"], policy)
        self.assertEqual(review_fact["decision_policy_sha256"], policy_hash)
        review_projection = next(
            item for item in detail.data["stages"]
            if item["stage_key"] == "script_normalize_review"
        )
        self.assertEqual(
            review_projection["output_snapshot"]["policy_evidence"],
            evidence,
        )

    def test_prompt_normalization_policy_accepts_stage_snapshot_above_64_kib(self):
        run, _shot, review_stage, _policy, _policy_hash = (
            self._normalization_policy_fixture()
        )
        normalize_stage = run.stages.get(stage_key="script_normalize")
        snapshot = deepcopy(normalize_stage.output_snapshot)
        review_payload = snapshot["review_payload"]
        review_payload["batch_count"] = 31
        review_payload["batch_results"] = [
            {
                "batch_index": index,
                "request_ids": [f"batch-{index}-field-{item}" for item in range(4)],
                "result_excerpt": "cinematic continuity evidence " * 160,
            }
            for index in range(31)
        ]
        contract = assign_json_contract(
            normalize_stage,
            "output_snapshot",
            snapshot,
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
        )
        normalize_stage.save(update_fields=(
            "output_snapshot", "output_snapshot_schema_version",
            "output_snapshot_size_bytes", "output_snapshot_sha256", "updated_at",
        ))

        self.assertGreater(contract.size_bytes, 64 * 1024)
        self.assertLess(contract.size_bytes, boundary_int("security.snapshot.stage_max_bytes"))

        reference = _execute_unattended_policy_review(run, review_stage)

        decision = review_stage.approval_decisions.get()
        self.assertTrue(reference.startswith("approval:"))
        self.assertEqual(decision.actor, ApprovalDecision.Actor.RECIPE_POLICY)
        self.assertIsNone(decision.decided_by)
        self.assertEqual(PromptFieldTranslation.objects.count(), 7)

    def test_prompt_normalization_policy_rejects_stage_snapshot_above_8_mib(self):
        run, _shot, review_stage, _policy, _policy_hash = (
            self._normalization_policy_fixture()
        )
        normalize_stage = run.stages.get(stage_key="script_normalize")
        snapshot = deepcopy(normalize_stage.output_snapshot)
        snapshot["review_payload"]["oversized_evidence"] = (
            "x" * boundary_int("security.snapshot.stage_max_bytes")
        )
        encoded = canonical_json_bytes(snapshot)
        self.assertGreater(len(encoded), boundary_int("security.snapshot.stage_max_bytes"))
        normalize_stage.output_snapshot = snapshot
        normalize_stage.output_snapshot_schema_version = 1
        normalize_stage.output_snapshot_size_bytes = len(encoded)
        normalize_stage.output_snapshot_sha256 = hashlib.sha256(encoded).hexdigest()
        normalize_stage.save(update_fields=(
            "output_snapshot", "output_snapshot_schema_version",
            "output_snapshot_size_bytes", "output_snapshot_sha256", "updated_at",
        ))

        with self.assertRaisesMessage(
            ValidationError,
            f"JSON 载荷超过 {boundary_int('security.snapshot.stage_max_bytes')} 字节上限",
        ):
            _execute_unattended_policy_review(run, review_stage)

        self.assertFalse(review_stage.approval_decisions.exists())
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_prompt_normalization_policy_rejects_non_authoritative_provisional(self):
        run, _shot, review_stage, _policy, _policy_hash = (
            self._normalization_policy_fixture()
        )
        normalize_stage = run.stages.get(stage_key="script_normalize")
        snapshot = deepcopy(normalize_stage.output_snapshot)
        character_name = next(
            item for item in snapshot["review_payload"]["candidates"]
            if item["field_key"] == "character.name"
        )
        character_name["translated_text"] = "S99"
        assign_json_contract(normalize_stage, "output_snapshot", snapshot)
        normalize_stage.save(update_fields=(
            "output_snapshot", "output_snapshot_schema_version",
            "output_snapshot_size_bytes", "output_snapshot_sha256", "updated_at",
        ))

        with self.assertRaises(StageBlocked) as raised:
            _execute_unattended_policy_review(run, review_stage)

        self.assertEqual(
            raised.exception.code,
            "unattended_prompt_identity_authority_mismatch",
        )
        self.assertFalse(review_stage.approval_decisions.exists())
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_prompt_normalization_policy_rejects_extended_token_without_prefix_rewrite(self):
        run, _shot, review_stage, _policy, _policy_hash = (
            self._normalization_policy_fixture()
        )
        normalize_stage = run.stages.get(stage_key="script_normalize")
        snapshot = deepcopy(normalize_stage.output_snapshot)
        narrative = next(
            item for item in snapshot["review_payload"]["candidates"]
            if item["field_key"] == "shot.narrative"
        )
        authoritative = "scene:scene-e2a2-real-shape"
        extended = f"{authoritative}-extra"
        narrative["translated_text"] = narrative["translated_text"].replace(
            authoritative,
            extended,
        )
        assign_json_contract(normalize_stage, "output_snapshot", snapshot)
        normalize_stage.save(update_fields=(
            "output_snapshot", "output_snapshot_schema_version",
            "output_snapshot_size_bytes", "output_snapshot_sha256", "updated_at",
        ))

        with self.assertRaises(StageBlocked) as raised:
            _execute_unattended_policy_review(run, review_stage)

        self.assertEqual(
            raised.exception.code,
            "unattended_prompt_identity_unknown_provisional",
        )
        self.assertEqual(
            _replace_unattended_identity_tokens(
                f"{extended}\n{authoritative}.",
                {authoritative: "Old Apartment Entrance"},
            ),
            f"{extended}\nOld Apartment Entrance.",
        )
        self.assertFalse(review_stage.approval_decisions.exists())
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_prompt_normalization_policy_is_order_independent_and_collision_safe(self):
        run, _shot, _review_stage, _policy, _policy_hash = (
            self._normalization_policy_fixture()
        )
        candidates = deepcopy(
            run.stages.get(stage_key="script_normalize")
            .output_snapshot["review_payload"]["candidates"]
        )
        for candidate in candidates:
            if candidate["field_key"] == "character.visual_identity":
                candidate["translated_text"] = "S1, a shared visual anchor."
            if candidate["field_key"] == "scene.visual_identity":
                candidate["translated_text"] = (
                    "scene:scene-e2a2-real-shape, a shared visual anchor."
                )
        raw = {item["request_id"]: item["translated_text"] for item in candidates}

        overrides, evidence = _unattended_prompt_normalization_overrides(
            self.project,
            candidates,
            raw_overrides=raw,
        )
        reversed_overrides, reversed_evidence = (
            _unattended_prompt_normalization_overrides(
                self.project,
                list(reversed(candidates)),
                raw_overrides=dict(reversed(list(raw.items()))),
            )
        )

        self.assertEqual(reversed_overrides, overrides)
        self.assertEqual(reversed_evidence, evidence)
        canonicals = {
            item["provisional_identity"]: item["canonical_identity"]
            for item in evidence
        }
        self.assertEqual(canonicals["S1"], "Shared Visual Anchor")
        self.assertEqual(
            canonicals["scene:scene-e2a2-real-shape"],
            "Shared Visual Anchor 2",
        )

    def test_prompt_normalization_policy_preserves_configured_glossary(self):
        run, _shot, review_stage, _policy, _policy_hash = (
            self._normalization_policy_fixture(
                translation_glossary={"林遥": "Rain Courier"},
            )
        )

        _execute_unattended_policy_review(run, review_stage)

        self.project.refresh_from_db()
        self.assertEqual(
            self.project.production_settings["translation_glossary"]["林遥"],
            "Rain Courier",
        )
        character_evidence = next(
            item for item in review_stage.output_snapshot["policy_evidence"][
                "canonical_identity_rewrites"
            ]
            if item["scope_type"] == PromptFieldTranslation.ScopeType.CHARACTER
        )
        self.assertEqual(character_evidence["canonical_identity"], "Rain Courier")
        self.assertEqual(character_evidence["derivation"], "project_glossary")

    def test_prompt_normalization_policy_can_be_verified_inside_outer_rollback(self):
        run, _shot, review_stage, _policy, _policy_hash = (
            self._normalization_policy_fixture()
        )

        with transaction.atomic():
            _execute_unattended_policy_review(run, review_stage)
            self.assertEqual(PromptFieldTranslation.objects.count(), 7)
            self.assertEqual(
                len(review_stage.output_snapshot["policy_evidence"][
                    "canonical_identity_rewrites"
                ]),
                3,
            )
            transaction.set_rollback(True)

        self.assertFalse(review_stage.approval_decisions.exists())
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_prompt_normalization_policy_fails_closed_on_source_drift(self):
        _run, shot, review_stage, _policy, _policy_hash = (
            self._normalization_policy_fixture()
        )
        shot.narrative = "另一位操作者已修改镜头。"
        shot.save(update_fields=("narrative", "updated_at"))

        with self.assertRaises(StageBlocked) as raised:
            _execute_unattended_policy_review(review_stage.production_run, review_stage)

        self.assertEqual(
            raised.exception.code,
            "unattended_prompt_normalization_validation_failed",
        )
        self.assertFalse(review_stage.approval_decisions.exists())
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_prompt_normalization_policy_rolls_back_materialization_before_audit_failure(self):
        _run, _shot, review_stage, _policy, _policy_hash = (
            self._normalization_policy_fixture()
        )

        with patch(
            "studio.services.production_runs._assert_stage_output_integrity",
            side_effect=ValidationError("post-materialization integrity failure"),
        ), self.assertRaisesMessage(
            ValidationError,
            "post-materialization integrity failure",
        ):
            _execute_unattended_policy_review(review_stage.production_run, review_stage)

        self.assertFalse(review_stage.approval_decisions.exists())
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_prompt_translation_validation_errors_keep_stable_codes(self):
        run, _shot, _review_stage, _policy, _policy_hash = (
            self._normalization_policy_fixture()
        )
        normalize_stage = run.stages.get(stage_key="script_normalize")
        attempt = normalize_stage.generation_attempts.get()
        candidates = normalize_stage.output_snapshot["review_payload"]["candidates"]
        character_name = next(
            item for item in candidates
            if item["field_key"] == "character.name"
        )

        with self.assertRaises(ValidationError) as invalid:
            confirm_prompt_normalization_candidates(
                project=self.project,
                attempt=attempt,
                candidates=[character_name],
                user=self.user,
                provider="test-provider",
                model="strict-normalizer",
                result_sha256=attempt.result_sha256,
                submission_semantics="durable_single_submit_manual_resolution",
            )
        invalid_error = invalid.exception.error_dict["candidate_overrides"][0]
        self.assertEqual(invalid_error.code, "prompt_translation_candidate_invalid")
        self.assertIn(character_name["request_id"], invalid_error.message)

        colliding = []
        for candidate in candidates:
            if candidate["field_key"] in {"character.name", "prop.name"}:
                colliding.append({**candidate, "translated_text": "Shared Anchor"})
        with self.assertRaises(ValidationError) as collision:
            confirm_prompt_normalization_candidates(
                project=self.project,
                attempt=attempt,
                candidates=colliding,
                user=self.user,
                provider="test-provider",
                model="strict-normalizer",
                result_sha256=attempt.result_sha256,
                submission_semantics="durable_single_submit_manual_resolution",
            )
        collision_error = collision.exception.error_dict["candidate_overrides"][0]
        self.assertEqual(collision_error.code, "canonical_target_collision")
        self.assertEqual(len(collision_error.params["collisions"]), 1)
        self.assertFalse(PromptFieldTranslation.objects.exists())

    def test_reference_video_requirement_comes_from_selected_mode_contract(self):
        optional = conditioning_contract(
            "NebulaTransfer",
            input_kind="classified_references",
            reference_videos=(0, 3),
            video_purposes=("orbit_rhythm_v7",),
        )["modes"][0]
        self.assertEqual(
            _freeze_unattended_reference_inputs(
                project=self.project,
                reference_inputs=[],
                conditioning_mode_contract=optional,
            ),
            [],
        )

        required = conditioning_contract(
            "NebulaTransfer",
            input_kind="classified_references",
            reference_videos=(1, 3),
            video_purposes=("orbit_rhythm_v7",),
        )["modes"][0]
        with self.assertRaises(ProductionRunCreateRejected) as missing:
            _freeze_unattended_reference_inputs(
                project=self.project,
                reference_inputs=[],
                conditioning_mode_contract=required,
            )
        self.assertEqual(
            missing.exception.code,
            "unattended_reference_binding_contract_invalid",
        )

        unsupported = conditioning_contract("NebulaText")["modes"][0]
        with self.assertRaises(ProductionRunCreateRejected) as unexpected:
            _freeze_unattended_reference_inputs(
                project=self.project,
                reference_inputs=[{
                    "asset_id": "00000000-0000-0000-0000-000000000001",
                    "purpose": "orbit_rhythm_v7",
                    "use_embedded_audio": False,
                }],
                conditioning_mode_contract=unsupported,
            )
        self.assertEqual(
            unexpected.exception.code,
            "unattended_reference_binding_contract_invalid",
        )

    def test_reference_revalidation_uses_frozen_project_conditioning_mode(self):
        frozen_conditioning = conditioning_contract(
            "Ref2VA",
            input_kind="classified_references",
            reference_videos=(0, 1),
            video_purposes=("motion_camera_reference",),
        )
        route = {
            "conditioning_contract": frozen_conditioning,
            "conditioning_contract_sha256": _sha256(frozen_conditioning),
        }
        selection = {
            "schema_version": 2,
            "mode": "uniform",
            "route": route,
        }
        execution_contract = {
            "schema_version": 1,
            "default_conditioning_mode": "Ref2VA",
        }
        run = SimpleNamespace(
            project=self.project,
            input_snapshot={
                "schema_version": 3,
                "workflow_selection": selection,
                "workflow_selection_sha256": _sha256(selection),
                "project_execution_contract": execution_contract,
                "project_execution_contract_sha256": _sha256(execution_contract),
                "reference_inputs": [],
                "reference_inputs_sha256": _sha256([]),
            },
        )
        self.project.production_settings = {
            "default_conditioning_mode": "ChangedAfterCreation",
        }
        self.project.save(update_fields=("production_settings", "updated_at"))

        with patch(
            "studio.services.production_runs._freeze_unattended_reference_inputs",
            return_value=[],
        ) as freeze:
            self.assertEqual(_verified_unattended_reference_inputs(run), [])

        selected_mode = freeze.call_args.kwargs["conditioning_mode_contract"]
        self.assertEqual(selected_mode["key"], "Ref2VA")

    def test_storyboard_video_cardinality_uses_the_same_mode_contract(self):
        optional = conditioning_contract(
            "NebulaTransfer",
            input_kind="classified_references",
            reference_videos=(0, 3),
            video_purposes=("orbit_rhythm_v7",),
        )["modes"][0]
        validate_reference_bindings_against_mode_contract(
            {},
            optional,
        )

        required = conditioning_contract(
            "NebulaTransfer",
            input_kind="classified_references",
            reference_videos=(1, 1),
            video_purposes=("orbit_rhythm_v7",),
        )["modes"][0]
        with self.assertRaisesMessage(ReferenceBindingError, "冻结范围 1–1"):
            validate_reference_bindings_against_mode_contract(
                {},
                required,
            )

        forbidden = conditioning_contract("NebulaText")["modes"][0]
        with self.assertRaisesMessage(ReferenceBindingError, "冻结范围 0–0"):
            validate_reference_bindings_against_mode_contract(
                {"videos": [{
                    "asset_id": "00000000-0000-0000-0000-000000000001",
                    "purpose": "orbit_rhythm_v7",
                    "use_embedded_audio": False,
                }]},
                forbidden,
            )

    def _storyboard_policy_fixture(self):
        recipe = self._recipe()
        policy = UNATTENDED_DECISION_POLICY_REGISTRY["storyboard_review"]
        policy_hash = _sha256({
            "schema_version": 1,
            "interaction_mode": "unattended",
            "stage_key": "storyboard_review",
            "decision_policy": policy,
            "recipe_definition_sha256": recipe.definition_sha256,
        })
        config = json_contract({"schema_version": 1})
        run_input = json_contract({
            "schema_version": 3,
            "interaction_mode": "unattended",
            "unattended_confirmed": True,
        })
        run = ProductionRun.objects.create(
            owner=self.user,
            project=self.project,
            recipe_version=recipe,
            idempotency_key=f"storyboard-policy-{ProductionRun.objects.count() + 1}",
            request_sha256="a" * 64,
            config_snapshot=config.value,
            config_snapshot_schema_version=config.schema_version,
            config_snapshot_size_bytes=config.size_bytes,
            config_snapshot_sha256=config.sha256,
            input_snapshot=run_input.value,
            input_snapshot_schema_version=run_input.schema_version,
            input_snapshot_size_bytes=run_input.size_bytes,
            input_snapshot_sha256=run_input.sha256,
        )
        episode = Episode.objects.create(prompt_settings={"include_audio": True}, 
            project=self.project,
            number=1,
            title="合同驱动分镜",
            script="两个镜头共享同一组冻结动态参考输入。",
        )
        shots = [
            Shot.objects.create(include_audio=True, 
                episode=episode,
                number=number,
                narrative=f"合同驱动镜头 {number}",
                duration_ms=1000,
                media_type=Shot.MediaType.VIDEO,
                conditioning_mode="Ref2VA",
            )
            for number in (1, 2)
        ]
        routing = json_contract({
            "schema_version": 1,
            "mode": "unconfigured",
            "routes": [],
            "variant_count": 1,
            "evaluator": None,
        })
        empty = json_contract({"schema_version": 1})

        def create_stage(key, ordinal, status, *, decision_policy="", policy_sha=""):
            stage = StageRun.objects.create(
                production_run=run,
                stage_key=key,
                ordinal=ordinal,
                mode="review" if key == "storyboard_review" else "deterministic",
                status=status,
                idempotency_key=f"{run.id}:{ordinal}:{key}",
                decision_policy=decision_policy,
                decision_policy_sha256=policy_sha,
                routing=routing.value,
                routing_schema_version=routing.schema_version,
                routing_size_bytes=routing.size_bytes,
                routing_sha256=routing.sha256,
                routing_decision=empty.value,
                routing_decision_schema_version=empty.schema_version,
                routing_decision_size_bytes=empty.size_bytes,
                routing_decision_sha256=empty.sha256,
                input_snapshot=empty.value,
                input_snapshot_schema_version=empty.schema_version,
                input_snapshot_size_bytes=empty.size_bytes,
                input_snapshot_sha256=empty.sha256,
                output_snapshot=empty.value,
                output_snapshot_schema_version=empty.schema_version,
                output_snapshot_size_bytes=empty.size_bytes,
                output_snapshot_sha256=empty.sha256,
            )
            return stage

        storyboard_stage = create_stage(
            "storyboard",
            1,
            StageRun.Status.SUCCEEDED,
        )
        assign_json_contract(storyboard_stage, "output_snapshot", {
            "schema_version": 1,
            "formal_object_type": "storyboard",
        })
        storyboard_stage.save(update_fields=(
            "output_snapshot", "output_snapshot_schema_version",
            "output_snapshot_size_bytes", "output_snapshot_sha256", "updated_at",
        ))
        review_stage = create_stage(
            "storyboard_review",
            2,
            StageRun.Status.RUNNING,
            decision_policy=policy,
            policy_sha=policy_hash,
        )
        return run, episode, shots, review_stage, policy

    def _execute_storyboard_policy_with_inputs(self, reference_inputs):
        run, episode, shots, review_stage, policy = self._storyboard_policy_fixture()
        with patch(
            "studio.services.production_runs._verified_unattended_reference_inputs",
            return_value=reference_inputs,
        ), patch(
            "studio.services.production_runs._selected_episodes",
            return_value=[episode],
        ), patch(
            "studio.services.production_runs._shot_requires_entity_reference_bible",
            return_value=True,
        ), patch(
            "studio.services.production_runs._approve_storyboard_reference_bindings",
        ) as approve, patch(
            "studio.services.production_runs._assert_stage_output_integrity",
        ), patch(
            "studio.services.production_runs._formal_snapshot",
            return_value={"schema_version": 1},
        ):
            reference = _execute_unattended_policy_review(run, review_stage)
        return reference, shots, review_stage, policy, approve.call_args.kwargs["selections"]

    def test_storyboard_policy_allows_zero_contractual_reference_inputs(self):
        reference, shots, review_stage, policy, selections = (
            self._execute_storyboard_policy_with_inputs([])
        )

        self.assertTrue(reference.startswith("approval:"))
        self.assertEqual(len(selections), len(shots))
        self.assertTrue(all(
            item["reference_bindings"]["videos"] == []
            for item in selections
        ))
        decision = review_stage.approval_decisions.get()
        self.assertEqual(decision.decision_policy, policy)
        evidence = review_stage.output_snapshot["policy_evidence"]
        self.assertEqual(evidence["kind"], "unattended_storyboard_reference_policy")
        self.assertEqual(evidence["reference_input_count"], 0)
        self.assertEqual(evidence["shot_count"], 2)

    def test_storyboard_policy_preserves_all_frozen_reference_inputs(self):
        reference_inputs = [
            {
                "asset_id": "00000000-0000-0000-0000-000000000001",
                "purpose": "motion_camera_reference",
                "use_embedded_audio": False,
            },
            {
                "asset_id": "00000000-0000-0000-0000-000000000002",
                "purpose": "audio_reference",
                "use_embedded_audio": True,
            },
        ]

        reference, shots, review_stage, _policy, selections = (
            self._execute_storyboard_policy_with_inputs(reference_inputs)
        )

        self.assertTrue(reference.startswith("approval:"))
        self.assertEqual(len(selections), len(shots))
        self.assertTrue(all(
            item["reference_bindings"]["videos"] == reference_inputs
            for item in selections
        ))
        self.assertEqual(
            review_stage.output_snapshot["policy_evidence"]["reference_input_count"],
            2,
        )

    def test_reference_video_is_byte_verified_and_frozen(self):
        payload = b"verified-video-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        observed = {
            "schema_version": 1,
            "observation_source": "ffprobe",
            "width": 352,
            "height": 608,
            "fps": 24.0,
            "frame_rate": "24/1",
            "frames": 96,
            "duration_ms": 4000,
            "has_audio": False,
        }
        required_contract = conditioning_contract(
            "NebulaTransfer",
            input_kind="classified_references",
            reference_videos=(1, 3),
            video_purposes=("orbit_rhythm_v7",),
        )["modes"][0]
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root):
            asset = Asset(
                owner=self.user,
                kind=Asset.Kind.REFERENCE_VIDEO,
                name="动态参考",
                mime_type="video/mp4",
                checksum=digest,
                metadata={"duration_ms": 4000, "source_fps": 24, "has_audio": False},
            )
            asset.file.save("reference.mp4", ContentFile(payload), save=False)
            asset.save()
            facts = {
                "mime_type": "video/mp4",
                "size_bytes": len(payload),
                "sha256": digest,
                "observed_media": observed,
            }
            with patch(
                "studio.services.production_runs.verified_local_media_facts",
                return_value=facts,
            ):
                frozen = _freeze_unattended_reference_inputs(
                    project=self.project,
                    reference_inputs=[{
                        "asset_id": str(asset.id),
                        "purpose": "orbit_rhythm_v7",
                        "use_embedded_audio": False,
                    }],
                    conditioning_mode_contract=required_contract,
                )
            self.assertEqual(frozen[0]["asset_id"], str(asset.id))
            self.assertEqual(frozen[0]["purpose"], "orbit_rhythm_v7")
            self.assertEqual(frozen[0]["checksum"], digest)
            self.assertEqual(frozen[0]["observed_media"]["duration_ms"], 4000)
            self.assertEqual(len(frozen[0]["facts_sha256"]), 64)

            asset.metadata = {"duration_ms": 4000, "fps": 24, "has_audio": False}
            asset.save(update_fields=("metadata", "updated_at"))
            with patch(
                "studio.services.production_runs.verified_local_media_facts",
                return_value=facts,
            ):
                reprobed = _freeze_unattended_reference_inputs(
                    project=self.project,
                    reference_inputs=[{
                        "asset_id": str(asset.id),
                        "purpose": "orbit_rhythm_v7",
                        "use_embedded_audio": False,
                    }],
                    conditioning_mode_contract=required_contract,
                )
            self.assertEqual(reprobed, frozen)

            asset.metadata = {"duration_ms": 4000, "source_fps": 24, "has_audio": False}
            asset.checksum = "f" * 64
            asset.save(update_fields=("metadata", "checksum", "updated_at"))
            with patch(
                "studio.services.production_runs.verified_local_media_facts",
                return_value=facts,
            ), self.assertRaisesMessage(ValidationError, "checksum"):
                _freeze_unattended_reference_inputs(
                    project=self.project,
                    reference_inputs=[{
                        "asset_id": str(asset.id),
                        "purpose": "orbit_rhythm_v7",
                        "use_embedded_audio": False,
                    }],
                    conditioning_mode_contract=required_contract,
                )

    def test_workflow_smoke_fixture_cannot_be_frozen_for_unattended_production(self):
        fixture = Asset.objects.create(
            owner=None,
            kind=Asset.Kind.REFERENCE_VIDEO,
            name="Workflow smoke only",
            local_path="workflow-smoke-only.mp4",
            mime_type="video/mp4",
            checksum="a" * 64,
            metadata={
                "fixture_scope": "workflow_smoke",
                "duration_ms": 4000,
                "source_fps": 24,
            },
        )
        required_contract = conditioning_contract(
            "NebulaTransfer",
            input_kind="classified_references",
            reference_videos=(1, 3),
            video_purposes=("orbit_rhythm_v7",),
        )["modes"][0]

        with self.assertRaises(ProductionRunCreateRejected) as captured:
            _freeze_unattended_reference_inputs(
                project=self.project,
                reference_inputs=[{
                    "asset_id": str(fixture.id),
                    "purpose": "orbit_rhythm_v7",
                    "use_embedded_audio": False,
                }],
                conditioning_mode_contract=required_contract,
            )

        self.assertEqual(captured.exception.code, "unattended_reference_video_forbidden")
        self.assertIn("Workflow Smoke 专用素材", str(captured.exception))

    def test_policy_decision_is_a_non_human_audit_fact(self):
        recipe = self._recipe()
        policy = UNATTENDED_DECISION_POLICY_REGISTRY["artifact_selection"]
        policy_hash = _sha256({
            "schema_version": 1,
            "interaction_mode": "unattended",
            "stage_key": "artifact_selection",
            "decision_policy": policy,
            "recipe_definition_sha256": recipe.definition_sha256,
        })
        config = json_contract({"schema_version": 1})
        run_input = json_contract({
            "schema_version": 3,
            "interaction_mode": "unattended",
            "unattended_confirmed": True,
        })
        run = ProductionRun.objects.create(
            owner=self.user,
            project=self.project,
            recipe_version=recipe,
            idempotency_key="policy-audit",
            request_sha256="a" * 64,
            config_snapshot=config.value,
            config_snapshot_schema_version=config.schema_version,
            config_snapshot_size_bytes=config.size_bytes,
            config_snapshot_sha256=config.sha256,
            input_snapshot=run_input.value,
            input_snapshot_schema_version=run_input.schema_version,
            input_snapshot_size_bytes=run_input.size_bytes,
            input_snapshot_sha256=run_input.sha256,
        )
        routing = json_contract({
            "schema_version": 1,
            "mode": "fixed",
            "routes": [{"index": 0}],
            "variant_count": 1,
            "evaluator": None,
        })
        empty = json_contract({"schema_version": 1})

        def stage(key, ordinal, status, *, decision_policy="", policy_sha=""):
            return StageRun.objects.create(
                production_run=run,
                stage_key=key,
                ordinal=ordinal,
                mode="review" if key == "artifact_selection" else "existing_pipeline",
                status=status,
                idempotency_key=f"{run.id}:{ordinal}:{key}",
                decision_policy=decision_policy,
                decision_policy_sha256=policy_sha,
                routing=routing.value,
                routing_schema_version=routing.schema_version,
                routing_size_bytes=routing.size_bytes,
                routing_sha256=routing.sha256,
                routing_decision=empty.value,
                routing_decision_schema_version=empty.schema_version,
                routing_decision_size_bytes=empty.size_bytes,
                routing_decision_sha256=empty.sha256,
                input_snapshot=empty.value,
                input_snapshot_schema_version=empty.schema_version,
                input_snapshot_size_bytes=empty.size_bytes,
                input_snapshot_sha256=empty.sha256,
                output_snapshot=empty.value,
                output_snapshot_schema_version=empty.schema_version,
                output_snapshot_size_bytes=empty.size_bytes,
                output_snapshot_sha256=empty.sha256,
            )

        media_stage = stage("media_generation", 1, StageRun.Status.SUCCEEDED)
        review_stage = stage(
            "artifact_selection",
            2,
            StageRun.Status.RUNNING,
            decision_policy=policy,
            policy_sha=policy_hash,
        )
        manifest = json_contract({"schema_version": 1})
        GeneratedArtifact.objects.create(
            manifest_key=f"run:{run.id}:video:1",
            production_run=run,
            stage_run=media_stage,
            kind=GeneratedArtifact.Kind.VIDEO,
            role="shot_video",
            variant_group="shot:test",
            variant_index=1,
            status=GeneratedArtifact.Status.SELECTED,
            storage_backend="local",
            object_key="outputs/test.mp4",
            mime_type="video/mp4",
            size_bytes=1,
            sha256="b" * 64,
            manifest_metadata=manifest.value,
            manifest_schema_version=manifest.schema_version,
            manifest_metadata_size_bytes=manifest.size_bytes,
            manifest_metadata_sha256=manifest.sha256,
        )

        reference = _execute_unattended_policy_review(run, review_stage)

        decision = review_stage.approval_decisions.get()
        self.assertTrue(reference.startswith("approval:"))
        self.assertEqual(decision.actor, ApprovalDecision.Actor.RECIPE_POLICY)
        self.assertIsNone(decision.decided_by)
        self.assertEqual(decision.decision_policy, policy)
        self.assertEqual(decision.decision_policy_sha256, policy_hash)
        self.assertEqual(decision.output_snapshot_sha256, review_stage.output_snapshot_sha256)

        with self.assertRaises(IntegrityError), transaction.atomic():
            ApprovalDecision.objects.create(
                stage_run=review_stage,
                revision=2,
                decision=ApprovalDecision.Decision.APPROVE,
                actor=ApprovalDecision.Actor.HUMAN,
                decided_by=None,
            )
