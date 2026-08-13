import re

from django.conf import settings
from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator

from studio.models import EpisodeCreativeDraft, ShotDetailDraft


class OpenApiEnumNamingTests(SimpleTestCase):
    def test_creative_draft_status_has_stable_component_name(self):
        overrides = settings.SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]
        self.assertEqual(
            overrides["CreativeDraftStatusEnum"],
            "studio.models.EpisodeCreativeDraft.Status",
        )

        self.assertEqual(
            list(EpisodeCreativeDraft.Status.values),
            list(ShotDetailDraft.Status.values),
        )

        schema = SchemaGenerator().get_schema(request=None, public=True)
        components = schema["components"]["schemas"]
        self.assertEqual(
            components["CreativeDraftStatusEnum"]["enum"],
            list(EpisodeCreativeDraft.Status.values),
        )
        self.assertEqual(
            components["EpisodeCreativeDraft"]["properties"]["status"]["allOf"][0]["$ref"],
            "#/components/schemas/CreativeDraftStatusEnum",
        )
        self.assertEqual(
            components["ShotDetailDraft"]["properties"]["status"]["allOf"][0]["$ref"],
            "#/components/schemas/CreativeDraftStatusEnum",
        )
        self.assertFalse(
            any(re.fullmatch(r"Status[0-9A-Fa-f]+Enum", name) for name in components),
        )
