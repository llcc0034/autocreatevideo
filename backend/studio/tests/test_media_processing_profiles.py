from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import ConfigDefinition, ConfigRevision
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.media_processing_profiles import (
    ASSEMBLY_PROFILE_KEY,
    DISTRIBUTION_PROFILE_KEY,
    REFERENCE_PROFILE_KEY,
    resolve_assembly_media_profile,
    resolve_distribution_media_profile,
    resolve_reference_media_profile,
)
from studio.tests.factories import build_story


class MediaProcessingProfileConfigurationTests(TestCase):
    def setUp(self):
        self.user, self.project, *_rest = build_story()

    def test_profiles_are_visible_versioned_standard_configuration(self):
        definitions = {
            row.key: row
            for row in ConfigDefinition.objects.filter(key__in=(
                ASSEMBLY_PROFILE_KEY,
                DISTRIBUTION_PROFILE_KEY,
                REFERENCE_PROFILE_KEY,
            ))
        }
        self.assertEqual(set(definitions), {
            ASSEMBLY_PROFILE_KEY,
            DISTRIBUTION_PROFILE_KEY,
            REFERENCE_PROFILE_KEY,
        })
        for definition in definitions.values():
            self.assertEqual(definition.configuration_class, "standard")
            self.assertEqual(definition.value_type, "object")
            self.assertEqual(definition.allowed_scopes, "system,project")
            self.assertTrue(definition.is_required)
            self.assertTrue(definition.value_schema["security_ceiling_paths"])

        self.assertEqual(resolve_assembly_media_profile().value["video_crf"], 18)
        self.assertEqual(
            resolve_distribution_media_profile().value["image_overlay"]["minimum_margin_px"],
            6,
        )
        self.assertEqual(
            resolve_reference_media_profile().value["video_duration_tolerance_ms"],
            60,
        )
        probe = ConfigDefinition.objects.get(key="artifact.media_probe_timeout_seconds")
        self.assertEqual(probe.configuration_class, "standard")
        self.assertEqual(probe.allowed_scopes, "system")
        self.assertEqual(
            probe.value_schema["security_ceiling_key"],
            "security.runtime.max_timeout_seconds",
        )

    def test_project_profile_override_changes_effective_versioned_value(self):
        baseline = resolve_assembly_media_profile(project=self.project)
        changed = deepcopy(baseline.value)
        changed["video_codec"] = "libx265"
        changed["video_preset"] = "slow"
        changed["video_crf"] = 21
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=self.project,
            values={ASSEMBLY_PROFILE_KEY: changed},
            created_by=self.user,
            change_summary="test project assembly profile override",
            clone_published=True,
        )
        publish_config_revision(revision, published_by=self.user)

        effective = resolve_assembly_media_profile(project=self.project)
        self.assertEqual(effective.value["video_codec"], "libx265")
        self.assertEqual(effective.value["video_preset"], "slow")
        self.assertEqual(effective.value["video_crf"], 21)
        self.assertNotEqual(effective.sha256, baseline.sha256)

    def test_nested_profile_value_cannot_exceed_superadmin_security_boundary(self):
        profile = deepcopy(resolve_reference_media_profile(project=self.project).value)
        profile["probe_timeout_seconds"] = 86_401
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=self.project,
            values={REFERENCE_PROFILE_KEY: profile},
            created_by=self.user,
            change_summary="test unsafe nested profile value",
            clone_published=True,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "security.runtime.max_timeout_seconds",
        ):
            publish_config_revision(revision, published_by=self.user)

    def test_closed_profile_contract_rejects_unknown_or_missing_fields_before_publish(self):
        profile = deepcopy(resolve_assembly_media_profile(project=self.project).value)
        profile["hidden_fallback"] = "not-allowed"
        with self.assertRaisesRegex(ValidationError, "未知字段"):
            create_config_revision(
                scope_type=ConfigRevision.ScopeType.PROJECT,
                project=self.project,
                values={ASSEMBLY_PROFILE_KEY: profile},
                created_by=self.user,
                change_summary="test invalid closed media contract",
                clone_published=True,
            )

    def test_media_processing_boundaries_are_system_only_security_configuration(self):
        keys = {
            "security.delivery.disclosure_text_max_characters",
            "security.media.duration_verification_tolerance_max_ms",
            "security.media.audio_bitrate_max_kbps",
            "security.media.audio_sample_rate_max_hz",
            "security.media.processing_argument_max_characters",
        }
        definitions = {
            row.key: row for row in ConfigDefinition.objects.filter(key__in=keys)
        }
        self.assertEqual(set(definitions), keys)
        for definition in definitions.values():
            self.assertEqual(definition.configuration_class, "security_boundary")
            self.assertEqual(definition.allowed_scopes, "system")
            self.assertEqual(definition.domain, "security")
