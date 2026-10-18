"""0233 changes authoring choices, never rewrites existing request contracts."""
from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class RetiredPromptSourceMigrationTests(_MigrationTestCase):
    migrate_from = "0232_retire_shot_fixed_creative_columns"
    migrate_to = "0233_alter_promptprofilefieldrule_source_selector"
    retired = {"shot." + key for key in (
        "narrative", "detailed_description", "camera", "action", "sound_effects", "music", "negative_prompt",
    )}

    def set_up_before_migration(self, apps):
        self.assertIn("memory", str(connection.settings_dict["NAME"]))
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "studio.services.provider_registry.resolve_credential"):
            blocked = self.enterContext(patch(target, side_effect=AssertionError("No external calls")))
            self.addCleanup(blocked.assert_not_called)
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Field = apps.get_model("studio", "FieldDefinition")
        profile = Profile.objects.create(profile_key="migration-0233", version=1,
            name="Isolated choices migration", model_family="fixture", operation="script.generate",
            status="published", contract_sha256="a" * 64, contract_size_bytes=123,
            source_projection_config={"fixture": "Do not recompile published contracts"})
        self.old_choices = dict(Rule._meta.get_field("source_selector").choices)
        for index, selector in enumerate(sorted(self.retired) + ["request.structured_input", "shot.dialogue"], 1):
            definition = Field.objects.create(key=f"migration_0233_{index}")
            Rule.objects.create(profile_version=profile, field_definition=definition,
                source_selector=selector, generation_source_selector=selector,
                channel="positive_prompt", position=index)
        # Compare whole business tables, not only the rows created above. Exclude
        # Django's migration journal, the sole intended persisted change.
        self.before = self.business_snapshot()

    def business_snapshot(self):
        with connection.cursor() as cursor:
            tables = sorted(name for name in connection.introspection.table_names(cursor)
                            if name.startswith("studio_"))
            snapshot = {}
            for table in tables:
                cursor.execute("SELECT * FROM " + connection.ops.quote_name(table))
                snapshot[table] = sorted(cursor.fetchall(), key=repr)
            return snapshot

    def test_forward_reapply_and_reverse_preserve_all_business_rows(self):
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        self.assertEqual(dict(Rule._meta.get_field("source_selector").choices),
                         {key: value for key, value in self.old_choices.items() if key not in self.retired})
        self.assertEqual(self.business_snapshot(), self.before)
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assertEqual(self.business_snapshot(), self.before)
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        old_apps = executor.loader.project_state([("studio", self.migrate_from)]).apps
        self.assertEqual(dict(old_apps.get_model("studio", "PromptProfileFieldRule")
                              ._meta.get_field("source_selector").choices), self.old_choices)
        self.assertEqual(self.business_snapshot(), self.before)
