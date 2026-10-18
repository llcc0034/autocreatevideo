from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import IntegrityError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import ReleaseBundle, ProductionRun
from studio.services.release_bundles import (
    PRODUCTION_RELEASE_BUNDLE_SCHEMA,
    RELEASE_BUNDLE_SIGNING_SALT,
    ReleaseBundleError,
    _canonical_sha256,
    asset_is_release_evidence,
    mark_release_bundle_released,
    normalize_release_selection,
    verified_release_bundle,
)
from studio.tests.factories import build_story


def assert_metadata_batch_reads(test, bundle):
    """Exercise repeated rows without inventing a second delivery for one Run."""
    from studio.serializers import ReleaseBundleSerializer
    from studio.views import ReleaseBundleViewSet
    view = ReleaseBundleViewSet()
    view.request = type("Request", (), {"user": bundle.owner})()
    query = view.get_queryset().filter(pk=bundle.pk).order_by("version")
    with patch("studio.services.release_bundles._delivery_metadata_contract", side_effect=AssertionError("No live reads")), \
            CaptureQueriesContext(connection) as captured:
        data = ReleaseBundleSerializer(list(query) * 2, many=True).data
    test.assertEqual(len(captured), 2, [row["sql"] for row in captured])
    test.assertEqual([row["verification_status"] for row in data], ["verified", "verified"])
    for row in captured:
        test.assertNotIn('"studio_stagerun"', row["sql"])
        test.assertNotIn('"config_snapshot"', row["sql"])


class ReleaseBundleServiceTests(TestCase):
    def setUp(self):
        self.user, self.project, character, _, _, _, _ = build_story("release-service")
        self.source_asset = character.anchor_asset
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _bundle(self, *, version=1, status=ReleaseBundle.Status.DRAFT):
        run = ProductionRun.objects.create(owner=self.user, project=self.project, plan_source="manual", status="awaiting_input",
            idempotency_key=str(uuid4()), request_sha256="a" * 64)
        from studio.services.production_run_plans import freeze_run_execution_plan
        from studio.services.production_stage_catalog import production_stage_catalog
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        return ReleaseBundle.objects.create(
            production_run=run,
            owner=self.user,
            project=self.project,
            created_by=self.user,
            version=version,
            title=f"Release v{version}",
            status=status,
            selection={"shots": [], "episodes": [], "series_artifact_id": None},
        )

    def _seal_document(self, bundle):
        from studio.services.production_run_plans import get_run_execution_plan
        body = {
            "schema": PRODUCTION_RELEASE_BUNDLE_SCHEMA,
            "production_run_id": str(bundle.production_run_id),
            "run_plan_sha256": get_run_execution_plan(bundle.production_run).sha256,
            "delivery_scope": "episode_masters",
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
        detail = self.client.get(f"/api/v1/release-bundles/{bundle.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["production_run"], bundle.production_run_id)
        self.assertEqual(detail.data["selection"], bundle.selection)
        self.assertEqual(detail.data["verification_status"], "draft")
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

    def test_broken_run_plan_is_reported_as_invalid_evidence_not_server_error(self):
        from studio.models import ProductionRunPlan
        bundle = self._seal_document(self._bundle())
        # Simulate storage corruption in this isolated DB. The normal write
        # manager correctly forbids editing immutable RunPlan snapshots.
        ProductionRunPlan._base_manager.filter(run_id=bundle.production_run_id).update(definition_sha256="0" * 64)
        with self.assertRaisesMessage(ReleaseBundleError, "冻结执行计划无效"):
            verified_release_bundle(bundle)
        response = self.client.get(f"/api/v1/release-bundles/{bundle.pk}/verify/")
        self.assertEqual(response.status_code, 400)

    def test_list_batches_current_run_plans_without_per_row_reads_or_masking_corruption(self):
        from studio.models import ProductionRunPlan
        from studio.serializers import ReleaseBundleSerializer
        from studio.views import ReleaseBundleViewSet
        sealed = [self._seal_document(self._bundle(version=number)) for number in range(1, 4)]
        self._bundle(version=4)
        ProductionRunPlan._base_manager.filter(run_id=sealed[1].production_run_id).update(definition_sha256="0" * 64)
        view = ReleaseBundleViewSet()
        view.request = type("Request", (), {"user": self.user})()
        query = view.get_queryset().order_by("version")
        with CaptureQueriesContext(connection) as captured:
            data = ReleaseBundleSerializer(query, many=True).data
        self.assertEqual(len(captured), 2, [row["sql"] for row in captured])
        self.assertEqual([row["verification_status"] for row in data], ["verified", "invalid", "verified", "draft"])
        self.assertEqual(str(data[1]["production_run"]), str(sealed[1].production_run_id))
        self.assertEqual(data[1]["selection"], sealed[1].selection)
        for row in captured:
            self.assertNotIn('"config_snapshot"', row["sql"])
            self.assertNotIn('"input_snapshot"', row["sql"])
        self.assertIn("INDEX", query.explain().upper())

    def test_existing_seal_is_live_verified_before_reusing_delivery_result(self):
        from studio.services.production_runs import _seal_run_release_bundle, StageBlocked
        bundle = self._seal_document(self._bundle())
        with patch("studio.services.production_runs.live_verified_release_bundle", side_effect=ReleaseBundleError("Media changed")):
            with self.assertRaisesMessage(StageBlocked, "Media changed"):
                _seal_run_release_bundle(bundle.production_run, None, [])

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

    def test_retired_release_schema_and_missing_run_are_rejected_even_when_signed(self):
        bundle = self._seal_document(self._bundle())
        body = deepcopy(bundle.manifest)
        body.pop("integrity")
        body["schema"] = "autoprompt-release-bundle-v1"
        digest = _canonical_sha256(body)
        bundle.manifest = {**body, "integrity": {"payload_sha256": digest,
            "signature": signing.Signer(salt=RELEASE_BUNDLE_SIGNING_SALT).sign(digest)}}
        bundle.manifest_sha256 = digest
        with self.assertRaises(ReleaseBundleError):
            verified_release_bundle(bundle)
        bundle = self._seal_document(self._bundle(version=2))
        bundle.production_run = None
        with self.assertRaises(ReleaseBundleError):
            verified_release_bundle(bundle)
