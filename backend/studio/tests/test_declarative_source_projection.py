from __future__ import annotations

from studio.tests.reference_rendering_fixtures import LEGACY_H3_SELECTOR_TO_PRIMITIVE

from copy import deepcopy

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import (
    PromptFieldDefinition,
    PromptProfileFieldRule,
    PromptProfileVersion,
)
from studio.services.prompt_normalization_contract import (
    PromptNormalizationContractError,
    _validated_profile_contract,
)
from studio.services.prompt_profiles import (
    CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY,
    DECLARATIVE_SOURCE_PROJECTION_KEY,
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    PromptProfileError,
    clone_prompt_profile_successor,
    normalize_prompt_profile_source_projection_config,
    prompt_profile_contract_payload,
    refresh_prompt_profile_contract,
    validate_prompt_profile_source_projection,
    validate_profile_rule,
)
from studio.services.source_projection_primitives import (
    REFERENCE_CONTEXT_ENGINE_KEY,
    SOURCE_PROJECTION_PRIMITIVES,
)
from studio.tests.factories import H3_REF2VA_OFFICIAL_RULE_SPECS


def _declarative_projection_config() -> dict:
    from studio.tests.reference_rendering_fixtures import rendering_config
    return rendering_config()


class DeclarativeSourceProjectionContractTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(
            username="declarative-projection-admin",
            password="StrongPass123!",
            email="declarative-projection@example.test",
        )

    def _profile(self, *, key: str = "qa.minimax.declarative-ref2va"):
        return PromptProfileVersion.objects.create(
            profile_key=key,
            version=1,
            name="Declarative Ref2VA",
            model_family="minimax_h3",
            operation="video.generate",
            mode="Ref2VA",
            source_projection_key=DECLARATIVE_SOURCE_PROJECTION_KEY,
            source_projection_config=_declarative_projection_config(),
            review_state=PromptProfileVersion.ReviewState.READY,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )

    def _six_rules(self, profile: PromptProfileVersion):
        rules = []
        contributions = {
            "detailed_description": (
                PromptProfileFieldRule.SourceSelector.SHOT_DETAIL
            ),
            "overall_soundscape": (
                PromptProfileFieldRule.SourceSelector.SHOT_SOUND_EFFECTS
            ),
            "non_diegetic_music": (
                PromptProfileFieldRule.SourceSelector.SHOT_MUSIC
            ),
        }
        for position, (legacy_selector, field_key, label_style) in enumerate(
            H3_REF2VA_OFFICIAL_RULE_SPECS,
            start=1,
        ):
            contribution_source = contributions.get(field_key)
            rule = PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=PromptFieldDefinition.objects.get(key=field_key),
                source_selector=(
                    PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
                ),
                projection_primitive=(
                    LEGACY_H3_SELECTOR_TO_PRIMITIVE[legacy_selector]
                ),
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=position,
                label_style=label_style,
                separator=PromptProfileFieldRule.Separator.NEWLINE,
                required=True,
                omit_if_empty=False,
                max_chars=7000,
                generation_mode=(
                    PromptProfileFieldRule.GenerationMode.MODEL_GENERATED
                    if contribution_source
                    else PromptProfileFieldRule.GenerationMode.SERVER_LOCKED
                ),
                generation_source_selector=(
                    contribution_source
                    or PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
                ),
                generation_instruction=(
                    "Return only this reviewed contribution."
                    if contribution_source else ""
                ),
                generation_output_semantics=(
                    PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
                    if contribution_source
                    else PromptProfileFieldRule.GenerationOutputSemantics.FINAL
                ),
                generation_required=True,
                language_policy="en" if contribution_source else "any",
                length_unit=(
                    PromptProfileFieldRule.LengthUnit.CHARACTERS
                    if contribution_source
                    else PromptProfileFieldRule.LengthUnit.NONE
                ),
                generation_min=1 if contribution_source else None,
                generation_max=1000 if contribution_source else None,
                generation_validator_keys=(
                    [PromptProfileFieldRule.GenerationValidator.NON_EMPTY]
                    if contribution_source else []
                ),
            )
            rules.append(rule)
        return rules

    def test_schema_v5_config_and_registered_primitives_validate_strictly(self):
        config = _declarative_projection_config()
        self.assertEqual(
            normalize_prompt_profile_source_projection_config(config),
            config,
        )
        profile = self._profile()
        rules = self._six_rules(profile)

        for rule in rules:
            validate_profile_rule(rule)
        validate_prompt_profile_source_projection(profile, rules=rules)

        invalid = deepcopy(config)
        invalid["primitive_options"][REFERENCE_CONTEXT_ENGINE_KEY][
            "unknown"
        ] = {}
        with self.assertRaises(PromptProfileError) as unknown_config:
            normalize_prompt_profile_source_projection_config(invalid)
        self.assertEqual(
            unknown_config.exception.code,
            "prompt_profile_source_projection_config_invalid",
        )

        detail = next(
            rule
            for rule in rules
            if rule.field_definition.key == "detailed_description"
        )
        detail.generation_source_selector = (
            PromptProfileFieldRule.SourceSelector.SHOT_MUSIC
        )
        with self.assertRaises(PromptProfileError) as incompatible:
            validate_prompt_profile_source_projection(profile, rules=rules)
        self.assertEqual(
            incompatible.exception.code,
            "prompt_profile_projection_contribution_unsupported",
        )
        detail.generation_source_selector = (
            PromptProfileFieldRule.SourceSelector.SHOT_DETAIL
        )
        detail.projection_primitive = "reference_context.unknown"
        with self.assertRaises(PromptProfileError) as unknown_primitive:
            validate_profile_rule(detail)
        self.assertEqual(
            unknown_primitive.exception.code,
            "prompt_profile_projection_primitive_unknown",
        )

    def test_projection_primitive_is_part_of_schema_v6_contract_hash(self):
        profile = self._profile(key="qa.minimax.declarative-contract")
        rule = self._six_rules(profile)[0]
        refresh_prompt_profile_contract(profile)
        first_hash = profile.contract_sha256
        first_payload = prompt_profile_contract_payload(profile)

        self.assertEqual(
            first_payload["schema_version"],
            PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        self.assertEqual(
            first_payload["rules"][0]["projection_primitive"],
            rule.projection_primitive,
        )
        self.assertEqual(
            _validated_profile_contract(first_payload, label="profile"),
            first_payload,
        )

        for invalid_value in ({"nested": {"unexpected": "value"}}, "\x00"):
            malformed_config = deepcopy(first_payload)
            malformed_config["source_projection_config"]["primitive_options"]["reference_context_ir_v1"]["rendering"]["reference_rules"][0]["usage"] = invalid_value
            with self.subTest(invalid_value=invalid_value), self.assertRaises(PromptNormalizationContractError):
                _validated_profile_contract(malformed_config, label="profile")

        invalid_payload = deepcopy(first_payload)
        invalid_payload["rules"][0]["projection_primitive"] = (
            "reference_context.unknown"
        )
        with self.assertRaises(PromptNormalizationContractError):
            _validated_profile_contract(invalid_payload, label="profile")

        rule.projection_primitive = "reference_context.summary"
        rule.save(update_fields=("projection_primitive", "updated_at"))
        refresh_prompt_profile_contract(profile)

        self.assertNotEqual(profile.contract_sha256, first_hash)
        self.assertEqual(
            prompt_profile_contract_payload(profile)["rules"][0][
                "projection_primitive"
            ],
            "reference_context.summary",
        )

    def test_clone_successor_copies_exact_contract_inputs_and_rejects_stale_hash(self):
        source = self._profile(key="qa.minimax.declarative-clone")
        source.description = "Frozen declarative source"
        source.channel_limits = {
            "positive_prompt": {"max_chars": 7000, "warn_at_chars": 6200},
        }
        source.official_reference = "https://example.test/model-card"
        source.save(update_fields=(
            "description",
            "channel_limits",
            "official_reference",
            "updated_at",
        ))
        source_rules = self._six_rules(source)
        refresh_prompt_profile_contract(source)
        source.status = PromptProfileVersion.Status.PUBLISHED
        source.save(update_fields=("status", "updated_at"))

        with self.assertRaises(PromptProfileError) as stale:
            clone_prompt_profile_successor(
                source,
                expected_source_contract_sha256="0" * 64,
                name="Must not exist",
            )
        self.assertEqual(stale.exception.code, "prompt_profile_clone_source_stale")
        self.assertEqual(
            PromptProfileVersion.objects.filter(
                profile_key=source.profile_key,
            ).count(),
            1,
        )

        target = clone_prompt_profile_successor(
            source,
            expected_source_contract_sha256=source.contract_sha256,
            name="Declarative Ref2VA successor",
        )
        target_rules = list(target.field_rules.order_by("position", "id"))

        self.assertEqual(target.version, 2)
        self.assertEqual(target.status, PromptProfileVersion.Status.DRAFT)
        self.assertEqual(
            target.review_state,
            PromptProfileVersion.ReviewState.NEEDS_REVIEW,
        )
        self.assertEqual(target.source_projection_key, source.source_projection_key)
        self.assertEqual(target.source_projection_config, source.source_projection_config)
        self.assertEqual(target.channel_limits, source.channel_limits)
        self.assertEqual(target.official_reference, source.official_reference)
        self.assertEqual(len(target_rules), 6)
        self.assertEqual(
            prompt_profile_contract_payload(target)["rules"],
            prompt_profile_contract_payload(source)["rules"],
        )
        self.assertEqual(
            [
                (
                    rule.field_definition_id,
                    rule.source_selector,
                    rule.projection_primitive,
                    rule.channel,
                    rule.position,
                    rule.label_style,
                    rule.generation_mode,
                    rule.generation_source_selector,
                    rule.generation_output_semantics,
                    rule.generation_context_selectors,
                )
                for rule in target_rules
            ],
            [
                (
                    rule.field_definition_id,
                    rule.source_selector,
                    rule.projection_primitive,
                    rule.channel,
                    rule.position,
                    rule.label_style,
                    rule.generation_mode,
                    rule.generation_source_selector,
                    rule.generation_output_semantics,
                    rule.generation_context_selectors,
                )
                for rule in source_rules
            ],
        )
        self.assertTrue(target.contract_sha256)
        self.assertNotEqual(target.contract_sha256, source.contract_sha256)


class DeclarativeSourceProjectionApiTests(TestCase):
    def setUp(self):
        actor = get_user_model().objects.create_superuser(
            username="declarative-projection-api-admin",
            password="StrongPass123!",
            email="declarative-projection-api@example.test",
        )
        self.client = APIClient()
        self.client.force_authenticate(actor)

    def _create_current_profile_via_api(self):
        """Synthetic schema-5 configuration; no live registry objects or media."""
        config = _declarative_projection_config()
        created = self.client.post("/api/v1/prompt-profile-versions/", {
            "profile_key": "qa.generic.api-acceptance",
            "name": "Synthetic generic API acceptance",
            "description": "Isolated interface test, not real model acceptance.",
            "model_family": "minimax_h3",
            "operation": "video.generate",
            "mode": "Ref2VA",
            "source_projection_key": DECLARATIVE_SOURCE_PROJECTION_KEY,
            "source_projection_config": config,
            "official_reference": "",
            "channel_limits": {
                "positive_prompt": {"max_chars": 7000, "warn_at_chars": 6200},
            },
            "review_state": "needs_review",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        profile_id = created.data["id"]
        self.assertEqual(created.data["source_projection_config"], config)
        contributions = {
            "detailed_description": PromptProfileFieldRule.SourceSelector.SHOT_DETAIL,
            "overall_soundscape": PromptProfileFieldRule.SourceSelector.SHOT_SOUND_EFFECTS,
            "non_diegetic_music": PromptProfileFieldRule.SourceSelector.SHOT_MUSIC,
        }
        for position, (selector, field_key, label_style) in enumerate(
            H3_REF2VA_OFFICIAL_RULE_SPECS, start=1,
        ):
            contribution = contributions.get(field_key)
            created_rule = self.client.post("/api/v1/prompt-profile-field-rules/", {
                "profile_version": profile_id,
                "field_definition": str(PromptFieldDefinition.objects.get(key=field_key).id),
                "source_selector": "projection.output",
                "projection_primitive": LEGACY_H3_SELECTOR_TO_PRIMITIVE[selector],
                "channel": "positive_prompt", "position": position,
                "label_style": label_style, "separator": "newline",
                "required": True, "omit_if_empty": False, "max_chars": 7000,
                "generation_mode": "model_generated" if contribution else "server_locked",
                "generation_source_selector": contribution or "projection.output",
                "generation_instruction": "Return only this reviewed contribution." if contribution else "",
                "generation_output_semantics": "contribution" if contribution else "final",
                "generation_required": True,
                "language_policy": "en" if contribution else "any",
                "length_unit": "characters" if contribution else "none",
                "generation_min": 1 if contribution else None,
                "generation_max": 1000 if contribution else None,
                "generation_validator_keys": ["non_empty"] if contribution else [],
                "generation_context_selectors": [],
            }, format="json")
            self.assertEqual(created_rule.status_code, 201, created_rule.data)
        return profile_id, config

    def test_current_config_api_publish_preview_and_successor_are_consistent(self):
        profile_id, config = self._create_current_profile_via_api()
        url = f"/api/v1/prompt-profile-versions/{profile_id}/"
        not_reviewed = self.client.post(f"{url}publish/", {}, format="json")
        self.assertEqual(not_reviewed.status_code, 400, not_reviewed.data)
        ready = self.client.patch(url, {"review_state": "ready"}, format="json")
        self.assertEqual(ready.status_code, 200, ready.data)
        published = self.client.post(f"{url}publish/", {}, format="json")
        self.assertEqual(published.status_code, 200, published.data)
        self.assertEqual(published.data["status"], "published")
        frozen_sha = published.data["contract_sha256"]
        detail = self.client.get(url)
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["source_projection_config"], config)
        self.assertEqual(detail.data["field_rule_count"], 6)
        sources = {
            key: f"Synthetic reviewed {key} output."
            for _, key, _ in H3_REF2VA_OFFICIAL_RULE_SPECS
        }
        preview = self.client.post(f"{url}preview/", {
            "sources": {"projection.output": sources},
        }, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        for value in sources.values():
            self.assertIn(value, preview.data["channels"]["positive_prompt"])
        changed = deepcopy(config)
        changed["primitive_options"][REFERENCE_CONTEXT_ENGINE_KEY]["rendering"][
            "unreferenced_task"
        ] = "explicit successor task"
        immutable = self.client.patch(url, {"source_projection_config": changed}, format="json")
        self.assertEqual(immutable.status_code, 400, immutable.data)
        self.assertEqual(self.client.get(url).data["contract_sha256"], frozen_sha)
        cloned = self.client.post(f"{url}clone-successor/", {
            "expected_source_contract_sha256": frozen_sha,
            "name": "Synthetic successor",
        }, format="json")
        self.assertEqual(cloned.status_code, 201, cloned.data)
        self.assertEqual(cloned.data["status"], "draft")
        self.assertEqual(cloned.data["review_state"], "needs_review")
        self.assertEqual(cloned.data["field_rule_count"], 6)
        self.assertEqual(cloned.data["source_projection_config"], config)
        successor_url = f"/api/v1/prompt-profile-versions/{cloned.data['id']}/"
        saved = self.client.patch(successor_url, {"source_projection_config": changed}, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(saved.data["source_projection_config"], changed)
        self.assertNotEqual(saved.data["contract_sha256"], frozen_sha)
        self.assertEqual(self.client.get(url).data["source_projection_config"], config)

    def test_current_config_api_rejects_unknown_template_tokens_without_mutation(self):
        profile_id, config = self._create_current_profile_via_api()
        url = f"/api/v1/prompt-profile-versions/{profile_id}/"
        before = self.client.get(url).data["contract_sha256"]
        for invalid_token in ("{unknown}", "{mode.__class__}", "{mode[0]}"):
            changed = deepcopy(config)
            changed["primitive_options"][REFERENCE_CONTEXT_ENGINE_KEY]["rendering"][
                "templates"
            ]["summary"] += invalid_token
            with self.subTest(token=invalid_token):
                response = self.client.patch(url, {"source_projection_config": changed}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                current = self.client.get(url)
                self.assertEqual(current.data["contract_sha256"], before)
                self.assertEqual(current.data["source_projection_config"], config)

    def test_new_profile_rejects_legacy_runtime_only_projector(self):
        historical = PromptProfileVersion.objects.create(
            profile_key="qa.minimax.legacy-read-compatible",
            version=1,
            name="Historical legacy projector",
            model_family="minimax_h3",
            operation="video.generate",
            mode="Ref2VA",
            source_projection_key=CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY,
            source_projection_config={},
            review_state=PromptProfileVersion.ReviewState.READY,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        refresh_prompt_profile_contract(historical)
        historical.status = PromptProfileVersion.Status.RETIRED
        historical.save(update_fields=("status", "updated_at"))

        detail = self.client.get(
            f"/api/v1/prompt-profile-versions/{historical.id}/",
        )
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(
            detail.data["source_projection_key"],
            CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY,
        )

        response = self.client.post(
            "/api/v1/prompt-profile-versions/",
            {
                "profile_key": "qa.minimax.legacy-write-rejected",
                "version": 1,
                "name": "Legacy projector must stay read-only",
                "description": "Regression fixture",
                "model_family": "minimax_h3",
                "operation": "video.generate",
                "mode": "Ref2VA",
                "source_projection_key": (
                    CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY
                ),
                "source_projection_config": {},
                "official_reference": "",
                "channel_limits": {},
                "review_state": PromptProfileVersion.ReviewState.NEEDS_REVIEW,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(
            PromptProfileVersion.objects.filter(
                profile_key="qa.minimax.legacy-write-rejected",
            ).exists(),
        )

    def test_options_hide_legacy_projector_and_return_primitive_catalog(self):
        response = self.client.get("/api/v1/prompt-profile-versions/options/")

        self.assertEqual(response.status_code, 200, response.data)
        projection_keys = {
            row["value"] for row in response.data["source_projection_keys"]
        }
        self.assertEqual(
            projection_keys,
            {"direct_v1", DECLARATIVE_SOURCE_PROJECTION_KEY},
        )
        self.assertNotIn(
            "classified_reference_context_ir_v1",
            projection_keys,
        )
        source_selectors = {
            row["value"] for row in response.data["source_selectors"]
        }
        self.assertTrue(
            set(LEGACY_H3_SELECTOR_TO_PRIMITIVE).isdisjoint(source_selectors),
        )

        primitive_rows = response.data["source_projection_primitives"]
        self.assertEqual(
            {row["value"] for row in primitive_rows},
            set(SOURCE_PROJECTION_PRIMITIVES),
        )
        self.assertEqual(len(primitive_rows), 6)
        from studio.services.reference_context import rendering_options
        self.assertEqual(response.data["reference_rendering_fields"], rendering_options())
        for row in primitive_rows:
            self.assertEqual(row["engine_key"], REFERENCE_CONTEXT_ENGINE_KEY)
            self.assertTrue(row["label"])
            self.assertTrue(row["description"])
            self.assertIsInstance(row["contribution_source_selectors"], list)

    def test_legacy_runtime_only_projector_cannot_be_published(self):
        legacy_draft = PromptProfileVersion.objects.create(
            profile_key="qa.minimax.legacy-publish-rejected",
            version=1,
            name="Legacy projector publish must be rejected",
            model_family="minimax_h3",
            operation="video.generate",
            mode="Ref2VA",
            source_projection_key=CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY,
            source_projection_config={},
            review_state=PromptProfileVersion.ReviewState.READY,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        refresh_prompt_profile_contract(legacy_draft)

        response = self.client.post(
            f"/api/v1/prompt-profile-versions/{legacy_draft.id}/publish/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        legacy_draft.refresh_from_db()
        self.assertEqual(legacy_draft.status, PromptProfileVersion.Status.DRAFT)

    def test_clone_successor_endpoint_reports_copied_rule_count_and_rejects_stale_hash(self):
        definition = PromptFieldDefinition.objects.create(
            key="qa_clone_endpoint_text",
            label="Clone endpoint text",
            category="qa",
            description="Clone endpoint fixture",
            impact="Verifies exact successor creation",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )
        source = PromptProfileVersion.objects.create(
            profile_key="qa.generic.clone-endpoint",
            version=1,
            name="Clone endpoint source",
            model_family="generic",
            operation="prompt.normalize",
            source_projection_key="direct_v1",
            source_projection_config={},
            review_state=PromptProfileVersion.ReviewState.READY,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        PromptProfileFieldRule.objects.create(
            profile_version=source,
            field_definition=definition,
            source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_TEXT,
            channel=PromptProfileFieldRule.Channel.STRUCTURED_INPUT,
            position=1,
            label_style=PromptProfileFieldRule.LabelStyle.NONE,
            separator=PromptProfileFieldRule.Separator.NEWLINE,
            max_chars=4000,
            generation_mode=PromptProfileFieldRule.GenerationMode.PASSTHROUGH,
            generation_source_selector=(
                PromptProfileFieldRule.SourceSelector.REQUEST_TEXT
            ),
        )
        refresh_prompt_profile_contract(source)
        source.status = PromptProfileVersion.Status.PUBLISHED
        source.save(update_fields=("status", "updated_at"))

        created = self.client.post(
            f"/api/v1/prompt-profile-versions/{source.id}/clone-successor/",
            {
                "expected_source_contract_sha256": source.contract_sha256,
                "name": "Clone endpoint successor",
            },
            format="json",
        )

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["version"], 2)
        self.assertEqual(created.data["status"], PromptProfileVersion.Status.DRAFT)
        self.assertEqual(
            created.data["review_state"],
            PromptProfileVersion.ReviewState.NEEDS_REVIEW,
        )
        self.assertEqual(created.data["field_rule_count"], 1)
        target = PromptProfileVersion.objects.get(pk=created.data["id"])
        self.assertEqual(target.field_rules.count(), 1)

        stale = self.client.post(
            f"/api/v1/prompt-profile-versions/{source.id}/clone-successor/",
            {
                "expected_source_contract_sha256": "0" * 64,
                "name": "Must not exist",
            },
            format="json",
        )
        self.assertEqual(stale.status_code, 409, stale.data)
        self.assertEqual(
            PromptProfileVersion.objects.filter(
                profile_key=source.profile_key,
            ).count(),
            2,
        )
