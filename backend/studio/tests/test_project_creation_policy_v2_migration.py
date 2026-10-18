from copy import deepcopy
from importlib import import_module

from studio.services.json_contracts import json_contract
from studio.tests.test_configuration_migrations import _MigrationTestCase


class ProjectCreationPolicyV2MigrationTests(_MigrationTestCase):
    migrate_from = "0122_remove_prompt_and_shot_duration_defaults"
    migrate_to = "0123_configurable_project_and_shot_creation_defaults"

    def set_up_before_migration(self, apps):
        User = apps.get_model("auth", "User")
        SeriesProject = apps.get_model("studio", "SeriesProject")
        Episode = apps.get_model("studio", "Episode")
        Shot = apps.get_model("studio", "Shot")
        Revision = apps.get_model("studio", "ConfigRevision")
        Value = apps.get_model("studio", "ConfigValue")

        active = Revision.objects.get(
            scope_type="system", status="published", activation_state="active",
        )
        self.previous_revision_id = active.id
        policy_row = Value.objects.get(
            revision=active,
            definition__key="production.project_creation_policy",
        )
        # Schema staging does not reverse later data-only migrations. In
        # particular, 0181 deliberately removed the call allowance from v3.
        # Build an explicit v1 fixture instead of deriving history from today's
        # active policy or teaching the current runtime to read the retired key.
        self.previous_policy = {
            "schema_version": 1,
            "width": 768,
            "height": 1344,
            "fps": 30,
            "default_shot_duration_ms": 5000,
            "default_episode_count": 2,
            "default_shots_per_episode": 3,
            "default_episode_duration_seconds": 45,
            "default_max_generation_tasks": 7,
            "continuity_seed_policy": {"mode": "explicit", "value": 123},
        }
        policy_contract = json_contract(self.previous_policy)
        Value.objects.filter(pk=policy_row.pk).update(
            value=self.previous_policy,
            value_schema_version=1,
            value_size_bytes=policy_contract.size_bytes,
            value_sha256=policy_contract.sha256,
        )
        Definition = apps.get_model("studio", "ConfigDefinition")
        historical = import_module("studio.migrations.0120_runtime_execution_policy")
        policy_schema = deepcopy(historical.STANDARD_CONFIG[
            "production.project_creation_policy"
        ][4])
        schema_contract = json_contract(policy_schema)
        Definition.objects.filter(pk=policy_row.definition_id).update(
            value_schema=policy_schema,
            value_schema_version=1,
            value_schema_size_bytes=schema_contract.size_bytes,
            value_schema_sha256=schema_contract.sha256,
            initial_value=self.previous_policy,
            initial_value_schema_version=1,
            initial_value_size_bytes=policy_contract.size_bytes,
            initial_value_sha256=policy_contract.sha256,
        )
        self.previous_values = list(Value.objects.filter(revision=active).order_by(
            "definition_id",
        ).values("definition_id", "value", "value_schema_version", "value_size_bytes",
            "value_sha256", "credential_version_id"))
        user = User.objects.create(username="creation-v2-migration")
        project = SeriesProject.objects.create(
            owner=user, title="创建策略迁移", width=768, height=1344, fps=24,
            default_shot_duration_ms=5000, continuity_seed=1,
        )
        episode = Episode.objects.create(
            project=project, number=1, title="第一集", prompt_settings={},
        )
        shot = Shot.objects.create(
            episode=episode, number=1, narrative="静帧。", duration_ms=1000,
            media_type="image", include_audio=True,
        )
        self.episode_id = episode.id
        self.shot_id = shot.id
        explicit_episode = Episode.objects.create(
            project=project, number=2, title="第二集", prompt_settings={"include_audio": False},
        )
        video_shot = Shot.objects.create(
            episode=explicit_episode, number=1, narrative="有声视频。", duration_ms=1000,
            media_type="video", include_audio=True,
        )
        self.explicit_episode_id = explicit_episode.id
        self.video_shot_id = video_shot.id

    def test_forward_materializes_creation_semantics_and_clones_active_revision(self):
        Definition = self.apps.get_model("studio", "ConfigDefinition")
        Revision = self.apps.get_model("studio", "ConfigRevision")
        Value = self.apps.get_model("studio", "ConfigValue")
        Episode = self.apps.get_model("studio", "Episode")
        Shot = self.apps.get_model("studio", "Shot")

        active = Revision.objects.get(
            scope_type="system", status="published", activation_state="active",
        )
        self.assertNotEqual(active.id, self.previous_revision_id)
        self.assertEqual(active.based_on_revision_id, self.previous_revision_id)
        policy_row = Value.objects.get(
            revision=active,
            definition__key="production.project_creation_policy",
        )
        policy = policy_row.value
        self.assertEqual(policy["schema_version"], 2)
        self.assertEqual(policy["default_language"], "Chinese")
        self.assertEqual(policy["default_aspect_ratio"], "9:16")
        self.assertEqual(policy["default_shot_media_type"], "video")
        for key in ("fps", "default_episode_count", "default_shots_per_episode",
                "default_episode_duration_seconds", "default_max_generation_tasks",
                "continuity_seed_policy"):
            self.assertEqual(policy[key], self.previous_policy[key], key)
        self.assertNotIn("width", policy)
        self.assertNotIn("default_shot_duration_ms", policy)
        self.assertEqual(policy_row.value_schema_version, 2)
        contract = json_contract(policy)
        self.assertEqual(policy_row.value_size_bytes, contract.size_bytes)
        self.assertEqual(policy_row.value_sha256, contract.sha256)

        # Cloning must retain the old revision's bytes and every unrelated value.
        self.assertEqual(list(Value.objects.filter(revision_id=self.previous_revision_id).order_by(
            "definition_id",
        ).values("definition_id", "value", "value_schema_version", "value_size_bytes",
            "value_sha256", "credential_version_id")), self.previous_values)
        expected_other_values = [row for row in self.previous_values
            if row["definition_id"] != policy_row.definition_id]
        self.assertEqual(list(Value.objects.filter(revision=active).exclude(
            definition_id=policy_row.definition_id,
        ).order_by("definition_id").values("definition_id", "value", "value_schema_version",
            "value_size_bytes", "value_sha256", "credential_version_id")), expected_other_values)
        previous_revision = Revision.objects.get(pk=self.previous_revision_id)
        self.assertEqual(previous_revision.status, "superseded")
        self.assertEqual(previous_revision.activation_state, "inactive")

        definition = Definition.objects.get(key="production.project_creation_policy")
        self.assertEqual(definition.value_schema_version, 2)
        self.assertTrue(definition.value_schema["closed_contract"])
        self.assertEqual(definition.allowed_scopes, "system")
        self.assertEqual(definition.initial_value, policy)
        self.assertEqual(definition.initial_value_sha256, contract.sha256)
        self.assertTrue(
            Episode.objects.get(pk=self.episode_id).prompt_settings["include_audio"]
        )
        self.assertFalse(Shot.objects.get(pk=self.shot_id).include_audio)
        self.assertFalse(Episode.objects.get(pk=self.explicit_episode_id).prompt_settings["include_audio"])
        self.assertTrue(Shot.objects.get(pk=self.video_shot_id).include_audio)
