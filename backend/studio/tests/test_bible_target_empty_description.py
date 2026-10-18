"""Isolated original candidate promotion with an explicitly blank description."""
from django.db import transaction
from django.test import TestCase, override_settings

from studio.tests import test_execution_bible_asset_promotion as fixtures
from studio.services.production_runs import _StoredAssetFileGuard


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class BibleTargetEmptyDescriptionTests(TestCase):
    _Fixture = fixtures.BibleAssetPromotionTests._Fixture
    setUp = fixtures.BibleAssetPromotionTests.setUp
    mark_submitted = fixtures.BibleAssetPromotionTests.mark_submitted
    archive = fixtures.BibleAssetPromotionTests.archive
    resolve = fixtures.BibleAssetPromotionTests.resolve
    promote = fixtures.BibleAssetPromotionTests.promote
    target_description = ""

    def test_real_archived_bytes_can_be_promoted_without_filling_creative_text(self):
        with _StoredAssetFileGuard() as guard, transaction.atomic():
            asset = self.promote(guard)
        self.assertEqual(asset.description, "")
        self.assertEqual(asset.prompt_hint, "")
        self.assertEqual(asset.name, self.entity.name)
        self.assertEqual(asset.checksum, self.artifact.sha256)
        self.entity.refresh_from_db()
        self.assertEqual(self.entity.description, "")
