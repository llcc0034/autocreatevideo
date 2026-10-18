"""The old data migration must be reversible when its forward path was empty."""
from importlib import import_module

from studio.tests.test_configuration_migrations import _MigrationTestCase


class DeclarativeProfileMigrationReverseTests(_MigrationTestCase):
    migrate_from = "0132_h3_declarative_prompt_profile_v19"
    migrate_to = migrate_from

    def set_up_before_migration(self, apps):
        self.Profile = apps.get_model("studio", "PromptProfileVersion")
        self.AuditEvent = apps.get_model("studio", "PlatformLifecycleAuditEvent")

    def test_empty_forward_and_reverse_do_not_create_route_or_audit(self):
        migration = import_module("studio.migrations.0132_h3_declarative_prompt_profile_v19")
        before = (self.Profile.objects.count(), self.AuditEvent.objects.count())
        migration.publish_declarative_successor(self.apps, None)
        migration.restore_v18_route(self.apps, None)
        migration.restore_v18_route(self.apps, None)
        self.assertEqual(
            (self.Profile.objects.count(), self.AuditEvent.objects.count()),
            before,
        )

    def test_partial_route_still_refuses_reverse_without_changing_facts(self):
        migration = import_module("studio.migrations.0132_h3_declarative_prompt_profile_v19")
        source = self.Profile.objects.create(
            profile_key=migration.PROFILE_KEY, version=migration.SOURCE_VERSION,
            name="Isolated incomplete historical migration fixture",
            model_family="minimax_h3", operation="video.generate", mode="Ref2VA",
        )
        before = (source.status, source.contract_sha256, self.AuditEvent.objects.count())
        with self.assertRaisesMessage(RuntimeError, "migrated facts are missing"):
            migration.restore_v18_route(self.apps, None)
        source.refresh_from_db()
        self.assertEqual(
            (source.status, source.contract_sha256, self.AuditEvent.objects.count()),
            before,
        )
