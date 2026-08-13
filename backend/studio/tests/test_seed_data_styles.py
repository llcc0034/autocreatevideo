from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from studio.models import Asset, Style


EXPECTED_SYSTEM_STYLE_SLUGS = {
    "wuxia",
    "xianxia",
    "anime",
    "thriller",
    "detective",
    "urban-drama",
    "cyberpunk",
    "palace-intrigue",
    "post-apocalypse",
    "healing-fantasy",
    "glamour-wuxia",
}


class SeedDataStyleTests(TestCase):
    def test_seed_keeps_all_eleven_system_styles_active(self):
        Style.objects.create(
            owner=None,
            slug="retired-system-style",
            name="Retired system style",
            is_system=True,
            is_active=True,
        )

        call_command("seed_data", stdout=StringIO(), verbosity=0)

        active_slugs = set(Style.objects.filter(
            owner=None,
            is_system=True,
            is_active=True,
        ).values_list("slug", flat=True))
        self.assertEqual(active_slugs, EXPECTED_SYSTEM_STYLE_SLUGS)
        self.assertTrue(Style.objects.get(slug="glamour-wuxia", owner=None).is_active)
        self.assertFalse(Style.objects.get(slug="retired-system-style", owner=None).is_active)

    def test_seeded_glamour_wuxia_style_is_recognized_by_asset_import(self):
        call_command("seed_data", stdout=StringIO(), verbosity=0)
        call_command("import_asset_library", stdout=StringIO(), verbosity=0)

        glamour_assets = Asset.objects.filter(
            owner=None,
            style__slug="glamour-wuxia",
            style__is_active=True,
            is_active=True,
        )
        self.assertEqual(glamour_assets.count(), 9)
        self.assertEqual(set(glamour_assets.values_list("kind", flat=True)), {
            Asset.Kind.CHARACTER,
            Asset.Kind.SCENE,
            Asset.Kind.PROP,
        })
