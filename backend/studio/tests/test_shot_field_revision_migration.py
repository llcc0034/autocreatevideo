"""0230 -> 0231: independently prove both keys, without rewriting old shots."""
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, connection, transaction

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ShotFieldRevisionKeyMigrationTests(_MigrationTestCase):
    migrate_from = "0230_retire_shot_mixed_reference_fields"
    migrate_to = "0231_shot_field_revision"

    def set_up_before_migration(self, apps):
        owner = apps.get_model(settings.AUTH_USER_MODEL).objects.create(username="isolated-field-key-upgrade")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=owner, title="Key migration fixture",
            width=160, height=96, fps=24, default_shot_duration_ms=1000, continuity_seed=1)
        episode = apps.get_model("studio", "Episode").objects.create(project=project, number=1)
        Shot = apps.get_model("studio", "Shot")
        self.shots = [Shot.objects.create(episode=episode, number=number, duration_ms=1000,
            media_type="video", include_audio=False) for number in (1, 2)]
        self.owner_id = owner.pk
        self.before = list(Shot.objects.order_by("pk").values())

    def test_each_unique_key_rejects_independently_and_is_scoped_to_shot(self):
        Shot = self.apps.get_model("studio", "Shot")
        Revision = self.apps.get_model("studio", "ShotFieldRevision")
        self.assertEqual(list(Shot.objects.order_by("pk").values()), self.before)
        self.assertFalse(Revision.objects.exists())
        values = {"shot_id": self.shots[0].pk, "revision": 1, "request_id": uuid4(),
            "source_sha256": "a" * 64, "overrides": [], "content_size_bytes": 2,
            "content_sha256": "b" * 64, "request_sha256": "c" * 64, "created_by_id": self.owner_id}
        original = Revision.objects.create(**values)
        for changes in ({"revision": 2}, {"request_id": uuid4()}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                Revision.objects.create(**{**values, **changes})
        Revision.objects.create(**{**values, "revision": 2, "request_id": uuid4(), "previous_id": original.pk})
        Revision.objects.create(**{**values, "shot_id": self.shots[1].pk})
        self.assertEqual(Revision.objects.count(), 3)
        self.assertEqual(list(Shot.objects.order_by("pk").values()), self.before)
        connection.check_constraints()
