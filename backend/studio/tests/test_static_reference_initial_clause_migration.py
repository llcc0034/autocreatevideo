from __future__ import annotations

import hashlib
from importlib import import_module
import json
from copy import deepcopy

from django.utils import timezone

from studio.tests.test_configuration_migrations import _MigrationTestCase


class StaticReferenceInitialClauseMigrationTests(_MigrationTestCase):
    migrate_from = "0097_visual_rules_advisory_capacity"
    migrate_to = "0098_static_reference_initial_clause_policy"

    @staticmethod
    def _contract(value):
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return len(encoded), hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _profile_contract(profile, rules):
        return {
            "schema_version": 4,
            "profile_key": profile.profile_key,
            "version": profile.version,
            "model_family": profile.model_family,
            "operation": profile.operation,
            "mode": profile.mode,
            "source_projection_key": profile.source_projection_key,
            "channel_limits": profile.channel_limits,
            "source_projection_config": profile.source_projection_config,
            "rules": [{
                "field_key": row.field_definition.key,
                "field_value_type": row.field_definition.value_type,
                "field_default_value": row.field_definition.default_value,
                "field_value_options": row.field_definition.value_options,
                "field_required": row.field_definition.is_required,
                "source_selector": row.source_selector,
                "channel": row.channel,
                "position": row.position,
                "label_style": row.label_style,
                "separator": row.separator,
                "required": row.required,
                "omit_if_empty": row.omit_if_empty,
                "max_chars": row.max_chars,
                "generation_mode": row.generation_mode,
                "generation_source_selector": row.generation_source_selector,
                "generation_instruction": row.generation_instruction,
                "generation_output_semantics": row.generation_output_semantics,
                "generation_required": row.generation_required,
                "language_policy": row.language_policy,
                "length_unit": row.length_unit,
                "generation_min": row.generation_min,
                "generation_max": row.generation_max,
                "generation_validator_keys": row.generation_validator_keys,
            } for row in rules],
        }

    def set_up_before_migration(self, apps):
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Field = apps.get_model("studio", "PromptFieldDefinition")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        source_config = deepcopy(import_module(
            "studio.migrations.0077_prompt_profile_static_reference_policy_v4",
        ).SOURCE_PROJECTION_CONFIG)
        source = Profile.objects.create(
            profile_key="z-image.bible.reference",
            version=2,
            name="Static initial v2",
            description="Historical immutable source",
            model_family="fixture-image-family",
            operation="image.generate",
            mode="",
            source_projection_key="direct_v1",
            source_projection_config=source_config,
            official_reference="",
            channel_limits={},
            review_state="ready",
            status="published",
            contract_schema_version=4,
            contract_size_bytes=2,
            contract_sha256="a" * 64,
            published_at=timezone.now(),
        )
        for position, (key, selector) in enumerate((
            ("migration_static_subject", "request.subject"),
            ("migration_static_theme", "request.theme"),
            ("migration_static_composition", "request.composition"),
            ("migration_static_environment", "request.environment"),
            ("migration_static_lighting", "request.lighting"),
            ("migration_static_color", "request.color_palette"),
            ("migration_static_style", "request.style"),
            ("migration_static_negative", "request.negative_constraints"),
        ), start=1):
            field = Field.objects.create(
                key=key,
                label=key,
                category="migration",
                description="Static initial clause migration fixture",
                impact="Static initial clause migration fixture",
                value_type="long_text",
                default_value=None,
                value_options=[],
                is_required=False,
                is_active=True,
                display_order=position,
            )
            Rule.objects.create(
                profile_version=source,
                field_definition=field,
                source_selector=selector,
                channel=(
                    "negative_prompt"
                    if selector == "request.negative_constraints"
                    else "positive_prompt"
                ),
                position=position,
                label_style="label_colon",
                separator="newline",
                required=position == 1,
                omit_if_empty=True,
                max_chars=4000,
                generation_mode="passthrough",
                generation_source_selector=selector,
                generation_instruction="",
                generation_output_semantics="final",
                generation_required=False,
                language_policy="inherit",
                length_unit="none",
                generation_min=None,
                generation_max=None,
                generation_validator_keys=[],
            )
        source_rules = list(
            Rule.objects.filter(profile_version_id=source.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        size, sha256 = self._contract(self._profile_contract(
            source,
            source_rules,
        ))
        source.contract_size_bytes = size
        source.contract_sha256 = sha256
        source.save(update_fields=(
            "contract_size_bytes",
            "contract_sha256",
            "updated_at",
        ))
        Binding.objects.create(
            key="z-image-turbo-bible-v2",
            name="Static initial v2",
            operation="image.generate",
            model_family=source.model_family,
            mode="",
            prompt_profile_version=source,
            priority=200,
            is_active=True,
        )
        self.source_id = source.id
        self.source_config = source_config
        self.source_contract_size = size
        self.source_contract_sha256 = sha256

    def test_v3_adds_generic_clause_policy_without_mutating_v2(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(
            profile_key="z-image.bible.reference",
            version=3,
        )

        self.assertEqual(source.status, "retired")
        self.assertEqual(source.source_projection_config, self.source_config)
        self.assertEqual(source.contract_size_bytes, self.source_contract_size)
        self.assertEqual(source.contract_sha256, self.source_contract_sha256)
        self.assertEqual(target.status, "published")
        self.assertEqual(target.contract_schema_version, 4)
        self.assertEqual(target.source_projection_config["schema_version"], 2)
        clause_policy = target.source_projection_config[
            "static_reference_policy"
        ]["initial_snapshot_clause_policy"]
        self.assertEqual(clause_policy["continuity_source_selector"], "request.action")
        self.assertEqual(
            clause_policy["trailing_clause_action"],
            "relocate_to_continuity",
        )
        self.assertIn("initially", clause_policy["initial_state_markers"])
        self.assertNotIn(
            "pulses once with glow",
            clause_policy["initial_state_markers"],
        )
        source_rules = list(source.field_rules.order_by("position").values(
            "field_definition_id",
            "source_selector",
            "channel",
            "position",
        ))
        target_rules = list(target.field_rules.order_by("position").values(
            "field_definition_id",
            "source_selector",
            "channel",
            "position",
        ))
        self.assertEqual(target_rules, source_rules)
        contract = self._profile_contract(
            target,
            target.field_rules.select_related(
                "field_definition",
            ).order_by("position", "id"),
        )
        size, sha256 = self._contract(contract)
        self.assertEqual(target.contract_size_bytes, size)
        self.assertEqual(target.contract_sha256, sha256)
        self.assertFalse(Binding.objects.get(
            key="z-image-turbo-bible-v2",
        ).is_active)
        self.assertTrue(Binding.objects.get(
            key="z-image-turbo-bible-v3",
        ).is_active)
