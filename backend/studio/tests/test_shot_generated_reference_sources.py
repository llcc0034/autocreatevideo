"""Existing isolated worker/registration facts feed M5; no live provider."""
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import AssetReferenceVersion, Episode, Shot, ShotReferenceUse
from studio.services.shot_reference_choices import apply_shot_reference_choices, read_shot_reference_choices
from studio.services.shot_reference_sources import resolve_shot_reference_sources
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_generated_reuse as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class ShotGeneratedReferenceSourceTests(StructureFixture):
    setUp = fixtures.GeneratedMaterialReuseTests.setUp
    tearDown = fixtures.GeneratedMaterialReuseTests.tearDown
    create_job = fixtures.GeneratedMaterialReuseTests.create_job
    primary_version = fixtures.GeneratedMaterialReuseTests.primary_version
    write = fixtures.GeneratedMaterialReuseTests.write
    input = staticmethod(fixtures.GeneratedMaterialReuseTests.input)
    configure_variant_sources = fixtures.GeneratedMaterialReuseTests.configure_variant_sources
    configure_declaration = fixtures.GeneratedMaterialReuseTests.configure_declaration
    variant_anchors = fixtures.GeneratedMaterialReuseTests.variant_anchors
    select = fixtures.GeneratedMaterialReuseTests.select
    allow_calls = fixtures.GeneratedMaterialReuseTests.allow_calls
    advance = fixtures.GeneratedMaterialReuseTests.advance
    fill_fields = fixtures.GeneratedMaterialReuseTests.fill_fields
    collect = fixtures.GeneratedMaterialReuseTests.collect
    start_primary = fixtures.GeneratedMaterialReuseTests.start_primary
    primary_ready = fixtures.GeneratedMaterialReuseTests.primary_ready
    start_variants = fixtures.GeneratedMaterialReuseTests.start_variants
    library = fixtures.GeneratedMaterialReuseTests.library

    def test_generated_version_reuse_preserves_origin_and_rejects_damaged_basis(self):
        primary, versions = self.library()
        # The original M4 fixture has no storyboard. These explicit storage
        # fixtures exercise resolution, not the later M5 selection API/writer.
        episode = Episode.objects.create(project=self.project, number=1, title="Isolated references")
        shots = [Shot.objects.create(episode=episode, number=number, duration_ms=1000,
            media_type="video", include_audio=False) for number in (1, 2)]
        uses = [ShotReferenceUse.objects.create(shot=shot, use_key="clothes", position=0, media_kind="image",
            purpose="outfit reference", setting_revision=version.setting_revision, reference_version=version,
            source_mode="existing", asset_sha256=version.asset.checksum) for shot, version in zip(shots, versions)]
        def resolve():
            return resolve_shot_reference_sources(actor=self.user, project_id=self.project.pk,
                shot_ids=[shot.pk for shot in shots]).value
        with CaptureQueriesContext(connection) as queries:
            result = resolve()
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        for shot, version in zip(result["shots"], versions):
            value = shot["uses"][0]
            self.assertEqual(value["source_mode"], "existing")
            self.assertEqual(value["source"]["acquisition_source"], "generated")
            self.assertEqual(value["source"]["reference_version_id"], str(version.pk))
            self.assertEqual(value["asset"]["asset_id"], str(version.asset_id))
            version.refresh_from_db()
            self.assertEqual(version.generated_from_id, primary.pk)
        def choose(mode):
            baseline = read_shot_reference_choices(actor=self.user, project_id=self.project.pk, shot_id=shots[0].pk)
            return apply_shot_reference_choices(actor=self.user, project_id=self.project.pk, shot_id=shots[0].pk,
                expected_uses_sha256=baseline.sha256, choices=[{"use_id": str(uses[0].pk), "source_mode": mode,
                    "reference_version_id": str(versions[0].pk), "asset_id": None,
                    "asset_sha256": versions[0].asset.checksum}])
        applied = choose("generated")
        self.assertEqual(applied.changed_use_ids, (str(uses[0].pk),))
        self.assertEqual(applied.selected.value["shots"][0]["uses"][0]["source"]["acquisition_source"], "generated")
        self.assertEqual(resolve()["shots"][0]["uses"][0]["source_mode"], "generated")
        AssetReferenceVersion.objects.filter(pk=versions[0].pk).update(generated_from_id=self.primary.pk)
        with self.assertRaises(ValidationError) as error: resolve()
        self.assertIn(f"reference_uses.{shots[0].pk}.clothes", error.exception.message_dict)
        with CaptureQueriesContext(connection) as queries, self.assertRaises(ValidationError): choose("existing")
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        uses[0].refresh_from_db()
        self.assertEqual(uses[0].source_mode, "generated")
        AssetReferenceVersion.objects.filter(pk=versions[0].pk).update(generated_from_id=primary.pk)
        reused = choose("existing")
        self.assertEqual(reused.selected.value["shots"][0]["uses"][0]["source"]["acquisition_source"], "generated")
        self.assertEqual(resolve()["shots"][0]["uses"][0]["source"]["reference_version_id"], str(versions[0].pk))
