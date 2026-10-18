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
    def test_seed_creates_defaults_without_disabling_other_shared_styles(self):
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
        self.assertEqual(active_slugs, EXPECTED_SYSTEM_STYLE_SLUGS | {"retired-system-style"})
        self.assertTrue(Style.objects.get(slug="glamour-wuxia", owner=None).is_active)
        self.assertTrue(Style.objects.get(slug="retired-system-style", owner=None).is_active)

    def test_seed_preserves_edits_and_disabled_state(self):
        call_command("seed_data", stdout=StringIO(), verbosity=0)
        row = Style.objects.get(slug="wuxia", owner=None)
        row.name = "自定名称"
        row.description = "自定简介"
        row.visual_prompt = "authored prompt"
        row.negative_prompt = "authored exclusions"
        row.is_active = False
        row.save()
        call_command("seed_data", stdout=StringIO(), verbosity=0)
        row.refresh_from_db()
        self.assertEqual((row.name, row.description, row.visual_prompt, row.negative_prompt, row.is_active),
            ("自定名称", "自定简介", "authored prompt", "authored exclusions", False))

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
        self.assertEqual(
            set(glamour_assets.values_list("source_info", flat=True)),
            {"系统素材清单导入"},
        )
