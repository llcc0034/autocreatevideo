"""Read-only M9-04 filters over isolated records, without opening any media files."""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import Asset, AssetReferenceSeries, AssetReferenceVersion, BibleVariant
from studio.tests.factories import build_story


class AssetReferenceBrowseTests(TestCase):
    def setUp(self):
        self.owner, self.project, character, *_ = build_story("reference-browse")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.setting = BibleVariant.objects.create(project=self.project, character=character, name="主图",
            reference_name="主图", reference_key="primary", appearance_role="primary", created_by=self.owner)
        self.primary = AssetReferenceSeries.objects.create(owner=self.owner, role="primary", name="主图",
            bible_variant=self.setting)
        self.variant = AssetReferenceSeries.objects.create(owner=self.owner, role="variant", name="校服 / Uniform",
            primary_series=self.primary)
        self.video = AssetReferenceSeries.objects.create(owner=self.owner, role="primary", name="视频")
        self.empty = AssetReferenceSeries.objects.create(owner=self.owner, role="primary", name="无版本")
        self.assets = []
        for series, mimes in ((self.primary, ["image/png"]), (self.variant, ["image/png", "image/jpeg", "audio/wav"]),
                (self.video, ["video/mp4"])):
            for number, mime in enumerate(mimes, 1):
                asset = Asset.objects.create(owner=self.owner, name=f"fixture {mime}", kind="reference", mime_type=mime,
                    metadata={"large": "not a list field"})
                self.assets.append(asset)
                AssetReferenceVersion.objects.create(series=series, asset=asset, version=number, source="existing")

    def query(self, **values):
        response = self.client.get("/api/v1/asset-reference-series/", values)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_empty_search_and_media_filters_count_series_not_join_rows(self):
        self.assertEqual(self.query(search="")["count"], 4)
        rows = self.query(media_type="image")
        self.assertEqual(rows["count"], 2)
        variant = next(row for row in rows["results"] if row["id"] == str(self.variant.pk))
        self.assertEqual(variant["version_count"], 3)  # Filtering never truncates version history/counts.
        for media, series in (("audio", self.variant), ("video", self.video)):
            self.assertEqual([row["id"] for row in self.query(media_type=media)["results"]], [str(series.pk)])

    def test_name_role_bible_and_exact_asset_filters_compose_before_pagination(self):
        rows = self.query(search="Uniform", media_type="image", role="variant", bible_linked="false", page_size=1)
        self.assertEqual(rows["count"], 1)
        self.assertEqual(rows["results"][0]["id"], str(self.variant.pk))
        self.assertEqual(self.query(bible_linked="true")["results"][0]["id"], str(self.primary.pk))
        self.assertEqual(self.query(asset_id=str(self.assets[1].pk), media_type="audio")["count"], 0)
        first = self.query(media_type="image", page_size=1)
        second = self.query(media_type="image", page_size=1, page=2)
        self.assertIsNotNone(first["next"])
        self.assertEqual({first["results"][0]["id"], second["results"][0]["id"]}, {str(self.primary.pk), str(self.variant.pk)})

    def test_invalid_filters_and_owner_scope_are_explicit(self):
        other = get_user_model().objects.create_user(username="private-reference-browse")
        foreign = AssetReferenceSeries.objects.create(owner=other, role="primary", name="private")
        AssetReferenceVersion.objects.create(series=foreign, asset=self.assets[0], version=1, source="existing")
        self.assertEqual(self.query(media_type="image")["count"], 2)
        for values in ({"media_type": "script"}, {"bible_linked": "guess"}, {"asset_id": "bad"}):
            response = self.client.get("/api/v1/asset-reference-series/", values)
            self.assertEqual(response.status_code, 400, response.data)

    def test_filtered_reads_are_bounded_narrow_and_do_not_write(self):
        self.query(media_type="image", bible_linked="false")  # Warm request-level configuration reads.
        with CaptureQueriesContext(connection) as queries:
            result = self.query(media_type="image", bible_linked="false")
        self.assertEqual(result["count"], 1)
        relationship_queries = [q for q in queries if '"studio_assetreferenceseries"' in q["sql"]]
        self.assertLessEqual(len(relationship_queries), 2)
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries))
        self.assertNotIn('"metadata"', " ".join(q["sql"] for q in queries))
        self.assertNotIn("large", str(result))
