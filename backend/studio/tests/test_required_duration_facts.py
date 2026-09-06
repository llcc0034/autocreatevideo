from django.db.models import NOT_PROVIDED
from django.test import SimpleTestCase

from studio.models import PromptArtifact, SeriesProject, Shot


class RequiredDurationFactsTests(SimpleTestCase):
    def test_runtime_duration_facts_have_no_model_default(self):
        self.assertIs(
            Shot._meta.get_field("duration_ms").default,
            NOT_PROVIDED,
        )
        self.assertIs(
            PromptArtifact._meta.get_field("total_duration_ms").default,
            NOT_PROVIDED,
        )

    def test_business_creation_facts_have_no_model_defaults(self):
        for model, field_names in (
            (SeriesProject, ("language", "aspect_ratio")),
            (Shot, ("media_type", "include_audio")),
            (PromptArtifact, ("is_multi_shot", "include_audio")),
        ):
            for field_name in field_names:
                with self.subTest(model=model.__name__, field=field_name):
                    self.assertIs(
                        model._meta.get_field(field_name).default,
                        NOT_PROVIDED,
                    )
