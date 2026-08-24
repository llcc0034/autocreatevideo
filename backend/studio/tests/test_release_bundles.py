from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import ReleaseBundle
from studio.services.release_bundles import (
    RELEASE_BUNDLE_SCHEMA,
    RELEASE_BUNDLE_SIGNING_SALT,
    ReleaseBundleError,
    _canonical_sha256,
    _ordered_assembly_source_pairs,
    asset_is_release_evidence,
    mark_release_bundle_released,
    normalize_release_selection,
    verified_release_bundle,
)
from studio.tests.factories import build_story


class ReleaseBundleServiceTests(TestCase):
    def setUp(self):
        self.user, self.project, character, _, _, _, _ = build_story("release-service")
        self.source_asset = character.anchor_asset
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _bundle(self, *, version=1, status=ReleaseBundle.Status.DRAFT):
        return ReleaseBundle.objects.create(
            owner=self.user,
            project=self.project,
            created_by=self.user,
            version=version,
            title=f"Release v{version}",
            status=status,
            selection={"shots": [], "episodes": [], "series_artifact_id": None},
        )

    def _seal_document(self, bundle):
        body = {
            "schema": RELEASE_BUNDLE_SCHEMA,
            "bundle_id": str(bundle.id),
            "project_id": str(bundle.project_id),
            "owner_id": str(bundle.owner_id),
            "version": bundle.version,
            "title": bundle.title,
            "sealed_at": timezone.now().isoformat(),
            "selection": deepcopy(bundle.selection),
            "artifacts": [],
            "assemblies": [],
        }
        digest = _canonical_sha256(body)
        bundle.manifest = {
            **body,
            "integrity": {
                "payload_sha256": digest,
                "signature": signing.Signer(
                    salt=RELEASE_BUNDLE_SIGNING_SALT,
                ).sign(digest),
            },
        }
        bundle.manifest_sha256 = digest
        bundle.status = ReleaseBundle.Status.SEALED
        bundle.sealed_at = timezone.now()
        bundle.save(update_fields=(
            "manifest", "manifest_sha256", "status", "sealed_at", "updated_at",
        ))
        return bundle

    def test_public_surface_is_read_only_and_verify_remains_available(self):
        bundle = self._bundle()

        self.assertEqual(self.client.get("/api/v1/release-bundles/").status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/v1/release-bundles/{bundle.id}/").status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/api/v1/release-bundles/", {}, format="json").status_code,
            405,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/v1/release-bundles/{bundle.id}/", {"title": "forbidden"}, format="json",
            ).status_code,
            405,
        )
        self.assertEqual(
            self.client.delete(f"/api/v1/release-bundles/{bundle.id}/").status_code,
            405,
        )
        for action in ("seal", "release"):
            self.assertEqual(
                self.client.post(
                    f"/api/v1/release-bundles/{bundle.id}/{action}/", {}, format="json",
                ).status_code,
                404,
            )

        with patch(
            "studio.views.live_verified_release_bundle",
            return_value={"verification": {"status": "verified"}},
        ):
            verified = self.client.get(f"/api/v1/release-bundles/{bundle.id}/verify/")
        self.assertEqual(verified.status_code, 200, verified.data)
        self.assertEqual(verified.data["verification"]["status"], "verified")

    def test_signed_manifest_verification_detects_tampering(self):
        bundle = self._seal_document(self._bundle())

        document = verified_release_bundle(bundle)
        self.assertEqual(document["verification"]["status"], "verified")

        tampered = deepcopy(bundle.manifest)
        tampered["title"] = "tampered"
        ReleaseBundle.objects.filter(pk=bundle.pk).update(manifest=tampered)
        bundle.refresh_from_db()
        with self.assertRaises(ReleaseBundleError):
            verified_release_bundle(bundle)

    def test_newer_release_prevents_old_version_rollback(self):
        older = self._seal_document(self._bundle(version=1))
        newer = self._seal_document(self._bundle(version=2))

        with patch(
            "studio.services.release_bundles.live_verified_release_bundle",
            return_value={},
        ):
            released = mark_release_bundle_released(newer)
            with self.assertRaises(ReleaseBundleError) as raised:
                mark_release_bundle_released(older)

        self.assertEqual(released.status, ReleaseBundle.Status.RELEASED)
        self.assertIn("更高版本", str(raised.exception))

    def test_unique_release_race_has_stable_service_error(self):
        bundle = self._seal_document(self._bundle())
        with patch(
            "studio.services.release_bundles._mark_release_bundle_released_transaction",
            side_effect=IntegrityError("one_released_bundle_per_project"),
        ):
            with self.assertRaises(ReleaseBundleError) as raised:
                mark_release_bundle_released(bundle)
        self.assertEqual(raised.exception.details["code"], "release_transition_conflict")

    def test_global_asset_in_sealed_manifest_remains_release_evidence(self):
        bundle = self._bundle(status=ReleaseBundle.Status.SEALED)
        bundle.manifest = {
            "artifacts": [{
                "source_media": [{"asset_id": str(self.source_asset.id)}],
            }],
        }
        bundle.save(update_fields=("manifest", "updated_at"))
        self.source_asset.owner = None
        self.source_asset.save(update_fields=("owner", "updated_at"))

        self.assertIs(asset_is_release_evidence(self.source_asset), True)

    def test_selection_contract_rejects_retired_task_id_shape(self):
        with self.assertRaises(ReleaseBundleError):
            normalize_release_selection({
                "shots": [{
                    "shot_id": str(uuid4()),
                    "task_id": str(uuid4()),
                    "output_index": 0,
                }],
                "episodes": [],
                "series_artifact_id": None,
            })

    def test_historical_assembly_rows_bind_exact_shot_order(self):
        first_shot_id = str(uuid4())
        second_shot_id = str(uuid4())
        task_id = str(uuid4())
        normal, error = _ordered_assembly_source_pairs({"source_outputs": [
            {"clip_index": 0, "shot_id": first_shot_id, "task_id": task_id, "output_index": 0},
            {"clip_index": 1, "shot_id": second_shot_id, "task_id": task_id, "output_index": 0},
        ]})
        self.assertIsNone(error)

        swapped, error = _ordered_assembly_source_pairs({"source_outputs": [
            {"clip_index": 0, "shot_id": second_shot_id, "task_id": task_id, "output_index": 0},
            {"clip_index": 1, "shot_id": first_shot_id, "task_id": task_id, "output_index": 0},
        ]})
        self.assertIsNone(error)
        self.assertNotEqual(swapped, normal)

        historical, error = _ordered_assembly_source_pairs({"source_outputs": [
            {"clip_index": 0, "task_id": task_id, "output_index": 0},
        ]})
        self.assertIsNone(historical)
        self.assertEqual(error, "assembly_source_shot_ids_missing_reassemble")
