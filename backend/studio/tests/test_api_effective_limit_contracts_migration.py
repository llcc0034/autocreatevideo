from __future__ import annotations

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ApiEffectiveLimitContractsMigrationTests(_MigrationTestCase):
    migrate_from = "0113_text_output_field_contracts"
    migrate_to = "0114_api_effective_limit_contracts"

    boundary_defaults = {
        "security.production.max_bible_reference_assets_per_entity": 32,
        "security.production.max_bible_regeneration_entities_per_request": 32,
    }

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "ConfigDefinition")
        Revision = apps.get_model("studio", "ConfigRevision")
        Value = apps.get_model("studio", "ConfigValue")
        # 0114 intentionally keeps historical configuration facts on reverse.
        # MigrationExecutor can therefore expose leaf data while presenting the
        # 0113 schema; rebuild the exact pre-0114 registry facts for this test.
        boundary_ids = Definition.objects.filter(
            key__in=self.boundary_defaults,
        ).values_list("id", flat=True)
        Value.objects.filter(definition_id__in=boundary_ids).delete()
        Definition.objects.filter(key__in=self.boundary_defaults).delete()
        schema_keys = (
            "production.recommended_max_pending",
            "production.recommended_max_active_per_user",
            "production.script_auto_split_trigger_chars",
            "production.script_auto_split_target_chars",
            "runtime.task_health_failure_limit",
        )
        for definition in Definition.objects.filter(key__in=schema_keys):
            schema = dict(definition.value_schema or {})
            schema["maximum"] = 999_999
            definition.value_schema = schema
            definition.save(update_fields=("value_schema", "updated_at"))
        for key, field in (
            ("delivery.ai_label_text", "maxLength"),
            ("recommendation.vram_tiers", "maxItems"),
        ):
            definition = Definition.objects.get(key=key)
            schema = dict(definition.value_schema or {})
            schema[field] = 999_999
            definition.value_schema = schema
            definition.save(update_fields=("value_schema", "updated_at"))
        for definition in Definition.objects.filter(key__in=(
            "production.default_shot_duration_ms",
            "production.video_shot_min_duration_ms",
            "production.video_shot_max_duration_ms",
        )):
            schema = dict(definition.value_schema or {})
            schema["minimum"] = 100
            definition.value_schema = schema
            definition.save(update_fields=("value_schema", "updated_at"))
        active = Revision.objects.get(
            scope_type="system",
            status="published",
            activation_state="active",
        )
        self.previous_revision_id = active.id
        self.previous_value_count = Value.objects.filter(revision=active).count()
        self.previous_values = dict(
            Value.objects.filter(revision=active).values_list(
                "definition__key",
                "value",
            )
        )

    @staticmethod
    def _validator_limits(field) -> set[int]:
        return {
            int(validator.limit_value)
            for validator in field.validators
            if isinstance(getattr(validator, "limit_value", None), int)
        }

    def test_forward_registers_boundaries_and_preserves_active_configuration(self):
        Definition = self.apps.get_model("studio", "ConfigDefinition")
        Revision = self.apps.get_model("studio", "ConfigRevision")
        Value = self.apps.get_model("studio", "ConfigValue")

        active = Revision.objects.get(
            scope_type="system",
            status="published",
            activation_state="active",
        )
        self.assertNotEqual(active.id, self.previous_revision_id)
        self.assertEqual(active.based_on_revision_id, self.previous_revision_id)
        values = dict(
            Value.objects.filter(revision=active).values_list(
                "definition__key",
                "value",
            )
        )
        self.assertEqual(
            len(values),
            self.previous_value_count + len(self.boundary_defaults),
        )
        for key, value in self.previous_values.items():
            self.assertEqual(values[key], value)
        for key, value in self.boundary_defaults.items():
            self.assertEqual(values[key], value)
            definition = Definition.objects.get(key=key)
            self.assertEqual(definition.domain, "security")
            self.assertEqual(definition.configuration_class, "security_boundary")
            self.assertEqual(definition.allowed_scopes, "system")
            self.assertEqual(definition.allowed_default_sources, "")
            self.assertTrue(definition.is_required)
            self.assertFalse(definition.is_secret)
            self.assertTrue(definition.is_active)

    def test_forward_removes_duplicate_schema_and_model_maxima(self):
        Definition = self.apps.get_model("studio", "ConfigDefinition")
        schemas = dict(Definition.objects.filter(key__in=(
            "production.recommended_max_pending",
            "production.recommended_max_active_per_user",
            "production.script_auto_split_trigger_chars",
            "production.script_auto_split_target_chars",
            "runtime.task_health_failure_limit",
            "delivery.ai_label_text",
            "recommendation.vram_tiers",
        )).values_list("key", "value_schema"))
        for key in (
            "production.recommended_max_pending",
            "production.recommended_max_active_per_user",
            "production.script_auto_split_trigger_chars",
            "production.script_auto_split_target_chars",
            "runtime.task_health_failure_limit",
        ):
            self.assertNotIn("maximum", schemas[key])
        self.assertNotIn("maxLength", schemas["delivery.ai_label_text"])
        self.assertNotIn("maxItems", schemas["recommendation.vram_tiers"])

        for key in (
            "production.default_shot_duration_ms",
            "production.video_shot_min_duration_ms",
            "production.video_shot_max_duration_ms",
        ):
            self.assertEqual(
                Definition.objects.get(key=key).value_schema["minimum"],
                1,
            )

        SeriesProject = self.apps.get_model("studio", "SeriesProject")
        Shot = self.apps.get_model("studio", "Shot")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        self.assertIn(
            1,
            self._validator_limits(
                SeriesProject._meta.get_field("default_shot_duration_ms"),
            ),
        )
        self.assertNotIn(
            100,
            self._validator_limits(Shot._meta.get_field("duration_ms")),
        )
        for field_name in ("max_chars", "generation_min", "generation_max"):
            limits = self._validator_limits(Rule._meta.get_field(field_name))
            self.assertIn(1, limits)
            self.assertNotIn(30_000, limits)
