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
        current = policy_row.value
        self.previous_policy = {
            "schema_version": 1,
            "width": 768,
            "height": 1344,
            "fps": current["fps"],
            "default_shot_duration_ms": 5000,
            "default_episode_count": current["default_episode_count"],
            "default_shots_per_episode": current["default_shots_per_episode"],
            "default_episode_duration_seconds": current[
                "default_episode_duration_seconds"
            ],
            "default_max_generation_tasks": current[
                "default_max_generation_tasks"
            ],
            "continuity_seed_policy": current["continuity_seed_policy"],
        }
        from studio.services.json_contracts import json_contract

        policy_contract = json_contract(self.previous_policy)
        Value.objects.filter(pk=policy_row.pk).update(
            value=self.previous_policy,
            value_schema_version=1,
            value_size_bytes=policy_contract.size_bytes,
            value_sha256=policy_contract.sha256,
        )
        Definition = apps.get_model("studio", "ConfigDefinition")
        Definition.objects.filter(pk=policy_row.definition_id).update(
            initial_value=self.previous_policy,
            initial_value_schema_version=1,
            initial_value_size_bytes=policy_contract.size_bytes,
            initial_value_sha256=policy_contract.sha256,
        )
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
        self.assertEqual(
            policy["default_episode_count"],
            self.previous_policy["default_episode_count"],
        )
        self.assertNotIn("width", policy)
        self.assertNotIn("default_shot_duration_ms", policy)
        self.assertEqual(policy_row.value_schema_version, 2)

        definition = Definition.objects.get(key="production.project_creation_policy")
        self.assertEqual(definition.value_schema_version, 2)
        self.assertTrue(definition.value_schema["closed_contract"])
        self.assertEqual(definition.allowed_scopes, "system")
        self.assertTrue(
            Episode.objects.get(pk=self.episode_id).prompt_settings["include_audio"]
        )
        self.assertFalse(Shot.objects.get(pk=self.shot_id).include_audio)
