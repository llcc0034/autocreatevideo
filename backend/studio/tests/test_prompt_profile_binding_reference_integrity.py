import hashlib
from contextlib import contextmanager
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase

from studio.models import (
    GenerationAttempt,
    PromptArtifact,
    PromptProfileBinding,
    PromptProfileVersion,
    SeriesProject,
)
from studio.platform_serializers import PromptProfileBindingSerializer
from studio.services.production_pipeline import materialize_prompt_artifact
from studio.services.production_runs import (
    _create_generation_attempt,
    create_production_run,
)
from studio.services.prompt_profiles import (
    PromptProfileResolutionError,
    refresh_prompt_profile_contract,
)


class PromptProfileBindingReferenceIntegrityTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(
            username="binding-reference-admin",
            password="StrongPass123!",
            email="binding-reference@example.test",
        )
        self.profile = PromptProfileVersion.objects.create(
            profile_key="test.binding-reference",
            version=1,
            name="Binding reference fixture",
            model_family="fixture_family",
            operation="image.generate",
            mode="",
            review_state=PromptProfileVersion.ReviewState.READY,
        )
        refresh_prompt_profile_contract(self.profile)
        self.profile.status = PromptProfileVersion.Status.PUBLISHED
        self.profile.save(update_fields=("status", "updated_at"))
        self.binding = PromptProfileBinding.objects.create(
            key="test.binding-reference.image",
            name="Binding reference image fixture",
            operation="image.generate",
            model_family="fixture_family",
            mode="",
            prompt_profile_version=self.profile,
            is_active=False,
        )
        self.project = SeriesProject.objects.create(
            owner=self.actor,
            title="Binding reference project",
            language="Chinese",
            aspect_ratio="1:1",
            width=1024,
            height=1024,
            fps=24,
            default_shot_duration_ms=5000,
            continuity_seed=1,
        )

    def _prompt_values(self, *, binding_key: str) -> dict:
        prompt_text = "frozen prompt"
        return {
            "project": self.project,
            "episode": None,
            "shot": None,
            "created_by": self.actor,
            "prompt_profile_version": self.profile,
            "prompt_profile_binding_key": binding_key,
            "prompt_profile_contract_sha256": self.profile.contract_sha256,
            "prompt_type": PromptArtifact.PromptType.IMAGE,
            "model": "fixture-model",
            "conditioning_mode": "",
            "revision": 1,
            "is_multi_shot": False,
            "include_audio": False,
            "total_duration_ms": 0,
            "prompt_text": prompt_text,
            "negative_prompt": "",
            "structured_payload": {},
            "consistency_snapshot": {},
            "request_options": {},
            "content_hash": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        }

    def test_binding_serializer_rejects_key_change(self):
        serializer = PromptProfileBindingSerializer(
            self.binding,
            data={"key": "test.binding-reference.changed"},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("创建后不可修改", str(serializer.errors["key"][0]))
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.key, "test.binding-reference.image")

    def test_prompt_materialization_rejects_missing_binding_before_write(self):
        with self.assertRaises(PromptProfileResolutionError) as captured:
            with transaction.atomic():
                materialize_prompt_artifact(
                    **self._prompt_values(binding_key="test.binding-reference.missing"),
                )

        self.assertEqual(
            captured.exception.code,
            "prompt_profile_binding_reference_missing",
        )
        self.assertFalse(PromptArtifact.objects.exists())

    def test_prompt_materialization_locks_binding_row_and_accepts_frozen_inactive_binding(self):
        manager = PromptProfileBinding.objects
        with patch.object(
            manager,
            "select_for_update",
            wraps=manager.select_for_update,
        ) as select_for_update:
            with transaction.atomic():
                artifact = materialize_prompt_artifact(
                    **self._prompt_values(binding_key=self.binding.key),
                )

        select_for_update.assert_called_once_with()
        self.assertEqual(artifact.prompt_profile_binding_key, self.binding.key)
        self.assertEqual(artifact.prompt_profile_version_id, self.profile.id)

    def test_generation_attempt_helper_rejects_missing_binding_before_write(self):
        with patch.object(GenerationAttempt.objects, "create") as create:
            with self.assertRaises(PromptProfileResolutionError) as captured:
                _create_generation_attempt(
                    prompt_profile_version=self.profile,
                    prompt_profile_binding_key="test.binding-reference.missing",
                    prompt_profile_contract_sha256=self.profile.contract_sha256,
                )

        self.assertEqual(
            captured.exception.code,
            "prompt_profile_binding_reference_missing",
        )
        create.assert_not_called()

    def test_generation_attempt_creation_runs_inside_shared_reference_guard(self):
        events: list[str] = []
        sentinel = object()

        @contextmanager
        def tracked_guard():
            events.append("guard_enter")
            try:
                yield
            finally:
                events.append("guard_exit")

        def validate(**_values):
            self.assertEqual(events, ["guard_enter"])

        def create(**_values):
            self.assertEqual(events, ["guard_enter"])
            return sentinel

        with (
            patch(
                "studio.services.production_runs.registry_reference_write_guard",
                tracked_guard,
            ),
            patch(
                "studio.services.production_runs."
                "validate_frozen_prompt_profile_binding_reference",
                side_effect=validate,
            ),
            patch(
                "studio.services.production_runs.GenerationAttempt.objects.create",
                side_effect=create,
            ),
        ):
            result = _create_generation_attempt(
                prompt_profile_version=self.profile,
                prompt_profile_binding_key=self.binding.key,
                prompt_profile_contract_sha256=self.profile.contract_sha256,
            )

        self.assertIs(result, sentinel)
        self.assertEqual(events, ["guard_enter", "guard_exit"])

    def test_production_run_creation_acquires_reference_guard_first(self):
        events: list[str] = []

        @contextmanager
        def tracked_guard():
            events.append("guard_enter")
            try:
                yield
            finally:
                events.append("guard_exit")

        with patch(
            "studio.services.registry_reference_lock.registry_reference_write_guard",
            tracked_guard,
        ):
            with self.assertRaises(ValidationError):
                create_production_run(
                    user=None,
                    project=None,
                    script_revision=None,
                    recipe=None,
                    idempotency_key="",
                    allow_external_calls=None,
                    budget_limit=None,
                    max_generation_tasks=None,
                )

        self.assertEqual(events, ["guard_enter", "guard_exit"])
