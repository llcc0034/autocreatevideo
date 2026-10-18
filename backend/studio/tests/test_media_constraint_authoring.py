from copy import deepcopy
from random import Random
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from studio.models import ModelProfileVersion, PlatformLifecycleAuditEvent, ProviderProfile
from studio.services.json_contracts import assign_json_contract
from studio.services.media_capabilities import (
    MEDIA_RESOLUTION_FIELDS,
    normalize_model_media_constraints,
)
from studio.services.model_profile_contracts import verified_model_profile_specification


def specification(resolution):
    return {"schema_version": 1, "capabilities": ["image.text_to_image"],
            "model_family": "media-authoring-test", "media_constraints": {
                "schema_version": 1, "resolution": resolution,
                "fps": {"allowed": [24, 30]},
                "duration_ms": {"minimum": 1000, "maximum": 2000}}}


class MediaResolutionFeasibilityTests(SimpleTestCase):
    def setUp(self):
        # No database, provider, or physical model is involved in this oracle.
        self.boundaries = patch("studio.services.media_capabilities.boundary_int", side_effect=lambda key: {
            "security.media.max_dimension": 2048, "security.media.max_fps": 60,
            "security.media.max_duration_ms": 10000, "security.reference.max_bindings": 4,
        }[key])
        self.boundaries.start()
        self.addCleanup(self.boundaries.stop)

    def test_feasibility_does_not_round_or_require_aligned_endpoints(self):
        for resolution in ({}, {"width_multiple": 32},
                {"minimum_width": 353, "maximum_width": 400, "width_multiple": 32},
                {"minimum_height": 609, "maximum_height": 640, "height_multiple": 32},
                {"minimum_width": 352, "minimum_height": 608, "maximum_short_side": 352},
                {"maximum_short_side": 608, "maximum_long_side": 352}):
            with self.subTest(resolution=resolution):
                original = specification(resolution)
                snapshot = deepcopy(original)
                self.assertEqual(normalize_model_media_constraints(original), original["media_constraints"])
                self.assertEqual(original, snapshot)

    def test_rejects_no_aligned_point_and_coupled_side_limits(self):
        for resolution in (
                {"minimum_width": 352, "maximum_width": 352, "width_multiple": 64},
                {"minimum_width": 353, "maximum_width": 383, "width_multiple": 32},
                {"minimum_height": 609, "maximum_height": 639, "height_multiple": 32},
                {"maximum_width": 31, "width_multiple": 32},
                {"minimum_width": 33, "minimum_height": 33, "width_multiple": 32,
                 "height_multiple": 32, "maximum_short_side": 63},
                {"minimum_width": 33, "width_multiple": 32, "maximum_long_side": 63}):
            with self.subTest(resolution=resolution):
                original = specification(resolution)
                snapshot = deepcopy(original)
                with self.assertRaises(ValidationError) as raised:
                    normalize_model_media_constraints(original)
                self.assertIn("specification.media_constraints.resolution", raised.exception.message_dict)
                self.assertIn("无共同可用尺寸", str(raised.exception))
                self.assertEqual(original, snapshot)

    def test_matches_independent_exhaustive_size_oracle_for_600_declarations(self):
        random = Random(3701)
        for _ in range(600):
            resolution = {key: random.randint(1, 12) for key in MEDIA_RESOLUTION_FIELDS if random.random() < 0.6}
            # With bounds/multiples <=12, the smallest aligned point is <24.
            def allowed(width, height):
                return all(value >= resolution.get(f"minimum_{axis}", 1)
                    and value <= resolution.get(f"maximum_{axis}", 24)
                    and value % resolution.get(f"{axis}_multiple", 1) == 0
                    for axis, value in (("width", width), ("height", height))) and (
                        min(width, height) <= resolution.get("maximum_short_side", 24)
                        and max(width, height) <= resolution.get("maximum_long_side", 24))
            expected = any(allowed(w, h) for w in range(1, 25) for h in range(1, 25))
            try:
                normalize_model_media_constraints(specification(resolution))
                actual = True
            except ValidationError:
                actual = False
            self.assertEqual(actual, expected, resolution)


class ModelMediaAuthoringApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_superuser(
            username="media-authoring-test", password="IsolatedTestOnly123!"))
        self.url = "/api/v1/model-profile-versions/"
        self.payload = {"profile_key": "media-authoring-test",
            "provider": str(ProviderProfile.objects.get(key="local-comfyui").pk),
            "modality": "image", "model_identifier": "isolated-test.safetensors"}

    def counts(self):
        return ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()

    def create(self, resolution):
        response = self.client.post(self.url, {**self.payload, "specification": specification(resolution)}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def assert_stored(self, row, expected):
        response = self.client.get(f"{self.url}{row['id']}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["specification"], expected)
        model = ModelProfileVersion.objects.get(pk=row["id"])
        self.assertEqual(verified_model_profile_specification(model), expected)
        self.assertEqual(response.data["specification_sha256"], model.specification_sha256)
        return response.data

    def test_api_create_edit_publish_and_next_version_round_trip(self):
        bad = specification({"minimum_width": 352, "maximum_width": 352, "width_multiple": 64})
        counts = self.counts()
        response = self.client.post(self.url, {**self.payload, "specification": bad}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("无共同可用尺寸", str(response.data))
        self.assertEqual(self.counts(), counts)

        row = self.create({"minimum_width": 353, "maximum_width": 400, "width_multiple": 32})
        original = deepcopy(row["specification"])
        self.assert_stored(row, original)  # Endpoints remain 353 and 400.
        counts = self.counts()
        response = self.client.patch(f"{self.url}{row['id']}/", {"specification": bad}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.assert_stored(row, original)["specification_sha256"], row["specification_sha256"])

        fixed = specification({"minimum_width": 352, "maximum_width": 352,
            "minimum_height": 608, "maximum_height": 608, "width_multiple": 32,
            "height_multiple": 32, "maximum_short_side": 352, "maximum_long_side": 608})
        response = self.client.patch(f"{self.url}{row['id']}/", {"specification": fixed}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assert_stored(row, fixed)
        response = self.client.post(f"{self.url}{row['id']}/publish/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        published = self.assert_stored(row, fixed)
        self.assertEqual(published["status"], "published")
        counts = self.counts()
        response = self.client.patch(f"{self.url}{row['id']}/", {"specification": original}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        response = self.client.post(f"{self.url}{row['id']}/draft-next/", {"specification": bad}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.assert_stored(row, fixed)["specification_sha256"], published["specification_sha256"])

        response = self.client.post(f"{self.url}{row['id']}/draft-next/", {"specification": original}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["version"], 2)
        self.assertEqual(response.data["status"], "draft")
        self.assert_stored(response.data, original)
        self.assert_stored(row, fixed)

    def test_publish_revalidates_an_impossible_stored_draft_without_repairing_it(self):
        row = self.create({"width_multiple": 32})
        model = ModelProfileVersion.objects.get(pk=row["id"])
        # An isolated stored-fixture anomaly, not an operational data repair.
        bad = specification({"minimum_width": 33, "width_multiple": 32, "maximum_long_side": 63})
        assign_json_contract(model, "specification", bad)
        model.save()
        counts = self.counts()
        response = self.client.post(f"{self.url}{row['id']}/publish/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("无共同可用尺寸", str(response.data))
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.assert_stored(row, bad)["status"], "draft")
        model.refresh_from_db()
        self.assertIsNone(model.published_at)
        self.assertIsNone(model.published_by)
