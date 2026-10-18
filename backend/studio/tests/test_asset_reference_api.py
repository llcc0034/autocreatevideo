"""M9-02 isolated relationship APIs; no model execution, adoption or current media writes."""
from uuid import uuid4
import hashlib
from pathlib import Path
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.asset_reference_serializers import AssetReferenceSeriesSummary, AssetReferenceVersionSummary
from studio.models import (Asset, AssetGenerationJob, AssetReferenceSeries, AssetReferenceVersion,
    BibleVariant, BibleVariantRevision, CharacterBible, GeneratedArtifact)
from studio.services.asset_provenance import ASSET_UPLOAD_SOURCE_INFO
from studio.services.asset_references import series_summaries, version_summaries
from studio.services.bible_variants import create_variant
from studio.tests.factories import build_story
from studio.tests import test_execution_workflow_request_media as workflow_fixtures
from studio.tests.test_asset_management_api import image_upload


SERIES = "/api/v1/asset-reference-series/"
VERSIONS = "/api/v1/asset-reference-versions/"


class AssetReferenceApiTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.media_root = Path(directory.name)
        override = override_settings(MEDIA_ROOT=self.media_root, ASSET_LIBRARY_ROOT=self.media_root / "library")
        override.enable()
        self.addCleanup(override.disable)
        self.owner = get_user_model().objects.create_user(username="reference-api-owner")
        self.other = get_user_model().objects.create_user(username="reference-api-other")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.asset = self.make_asset(owner=self.owner, name="原始素材", kind="reference", mime_type="image/png",
            source_info=ASSET_UPLOAD_SOURCE_INFO,
            metadata={"private": "not a list payload"}, description="not a list field")
        self.primary = self.series("沈念 / Shen Nian")

    def make_asset(self, **values):
        data = image_upload().read()
        values.setdefault("mime_type", "image/png")
        asset = Asset.objects.create(checksum=hashlib.sha256(data).hexdigest(), **values)
        if asset.owner_id is None:
            path = self.media_root / "library" / f"{asset.pk}.png"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
            asset.local_path = str(path)
        else:
            asset.file.save("fixture.png", ContentFile(data), save=False)
        asset.save()
        return asset

    def series(self, name, **values):
        response = self.client.post(SERIES, {"name": name, "role": "primary", **values}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def attach(self, series=None, asset=None, **values):
        return self.client.post(VERSIONS, {"series_id": (series or self.primary)["id"],
            "asset_id": str((asset or self.asset).pk), "source": "existing", **values}, format="json")

    def test_independent_series_variants_and_exact_paginated_versions(self):
        uniform = self.series("沈念·校服", role="variant", primary_series_id=self.primary["id"])
        coat = self.series("沈念·外套", role="variant", primary_series_id=self.primary["id"])
        count = Asset.objects.count()
        first = self.attach(uniform)
        self.assertEqual(first.status_code, 201, first.data)
        for name in ("candidate 2", "candidate 3"):
            asset = self.make_asset(owner=self.owner, name=name, kind="reference", mime_type="image/png")
            self.assertEqual(self.attach(uniform, asset).status_code, 201)
        self.assertEqual(Asset.objects.count(), count + 2)  # No relationship operation duplicates an Asset.
        response = self.client.get(VERSIONS, {"series_id": uniform["id"], "page_size": 2})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual([v["version"] for v in response.data["results"]], [3, 2])
        page = self.client.get(VERSIONS, {"series_id": uniform["id"], "page_size": 2, "page": 2})
        self.assertEqual(page.data["results"][0]["id"], first.data["id"])
        detail = self.client.get(f"{VERSIONS}{first.data['id']}/")
        self.assertEqual(detail.data["asset"]["id"], str(self.asset.pk))
        children = self.client.get(SERIES, {"primary_series_id": self.primary["id"]})
        self.assertEqual({v["id"] for v in children.data["results"]}, {uniform["id"], coat["id"]})
        parent = self.client.get(f"{SERIES}{self.primary['id']}/")
        self.assertEqual((parent.data["variant_count"], parent.data["version_count"]), (2, 0))

    def test_repeated_exact_association_reuses_version_and_rejection_does_not_consume_number(self):
        first = self.attach()
        second = self.attach()
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.data, second.data)
        bad = self.attach(asset_id=str(uuid4()))
        self.assertEqual(bad.status_code, 400)
        self.assertIn("asset_id", str(bad.data))
        asset = self.make_asset(owner=self.owner, name="next", kind="reference")
        self.assertEqual(self.attach(asset=asset).data["version"], 2)

    def test_relationships_reject_missing_multilevel_self_and_incompatible_roles(self):
        variant = self.series("变体", role="variant", primary_series_id=self.primary["id"])
        for values in (
            {"role": "variant", "primary_series_id": str(uuid4())},
            {"role": "variant", "primary_series_id": variant["id"]},
            {"role": "primary", "primary_series_id": self.primary["id"]},
            {"role": "variant"}, {"role": "primary", "bible_variant_id": str(uuid4())},
        ):
            with self.subTest(values=values):
                response = self.client.post(SERIES, {"name": "invalid", **values}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
        for pk, parent in ((self.primary["id"], variant["id"]), (variant["id"], variant["id"])):
            response = self.client.patch(f"{SERIES}{pk}/", {"primary_series_id": parent}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(AssetReferenceSeries.objects.count(), 2)

    def test_access_scope_and_global_assets_do_not_grant_cross_owner_links(self):
        foreign_series = AssetReferenceSeries.objects.create(owner=self.other, name="private", role="primary")
        foreign_asset = Asset.objects.create(owner=self.other, name="private", kind="reference")
        self.assertEqual(self.attach(asset=foreign_asset).status_code, 400)
        self.assertEqual(self.attach(series={"id": str(foreign_series.pk)}).status_code, 404)
        response = self.client.post(SERIES, {"name": "bad", "role": "variant",
            "primary_series_id": str(foreign_series.pk)}, format="json")
        self.assertEqual(response.status_code, 400)
        for method in (self.client.get, self.client.delete):
            self.assertEqual(method(f"{SERIES}{foreign_series.pk}/").status_code, 404)
        self.assertEqual(self.client.get(SERIES).data["count"], 1)
        global_asset = self.make_asset(name="global", kind="reference")
        self.assertEqual(self.attach(asset=global_asset).status_code, 201)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(SERIES).status_code, (401, 403))

    def test_staff_edit_uses_series_owner_not_staff_identity(self):
        self.other.is_staff = True
        self.other.save(update_fields=["is_staff"])
        self.client.force_authenticate(self.other)
        self.assertEqual(self.attach().status_code, 201)
        staff_asset = Asset.objects.create(owner=self.other, name="staff private", kind="reference")
        self.assertEqual(self.attach(asset=staff_asset).status_code, 400)

    def test_foreign_versions_are_hidden_and_version_identity_cannot_be_patched(self):
        version = self.attach().data
        self.assertEqual(self.client.patch(f"{VERSIONS}{version['id']}/",
            {"asset_id": str(uuid4())}, format="json").status_code, 405)
        self.client.force_authenticate(self.other)
        for method in (self.client.get, self.client.delete):
            self.assertEqual(method(f"{VERSIONS}{version['id']}/").status_code, 404)
        self.assertEqual(self.client.get(VERSIONS, {"series_id": self.primary["id"]}).status_code, 404)
        self.assertEqual(AssetReferenceVersion.objects.get(pk=version["id"]).asset_id, self.asset.pk)

    def test_upload_is_original_asset_provenance_not_a_generation_assertion(self):
        uploaded = self.attach(source="upload")
        self.assertEqual(uploaded.status_code, 201, uploaded.data)
        for key in ("generated_from_id", "generation_job", "generated_artifact"):
            self.assertIsNone(uploaded.data[key])
        for values in ({"source": "generated"}, {"generated_from_id": str(uuid4())},
                {"generation_job_id": str(uuid4())}, {"generated_artifact_id": str(uuid4())},
                {"version": 99}, {"owner_id": self.other.pk}):
            with self.subTest(values=values):
                self.assertEqual(self.attach(**values).status_code, 400)
        ordinary = Asset.objects.create(owner=self.owner, name="not upload", kind="reference")
        self.assertEqual(self.attach(asset=ordinary, source="upload").status_code, 400)
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)

    def test_inactive_and_smoke_assets_cannot_be_attached(self):
        self.asset.is_active = False
        self.asset.save(update_fields=["is_active"])
        self.assertEqual(self.attach().status_code, 400)
        self.asset.is_active = True
        self.asset.metadata = {"fixture_scope": "workflow_smoke"}
        self.asset.save(update_fields=["is_active", "metadata"])
        self.assertEqual(self.attach().status_code, 400)

    def test_bible_binding_is_exact_optional_and_does_not_copy_anchors(self):
        owner, project, character, *_ = build_story("reference-api-bible")
        self.client.force_authenticate(owner)
        setting = create_variant(actor=owner, project_id=project.pk, entity_kind="character", entity_id=character.pk,
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="",
            value={"name": "主图", "reference_name": "主图", "identity_anchor": "身份",
                "appearance_anchor": "PRIVATE_ANCHOR", "basis_asset_id": None, "basis_asset_sha256": ""})
        revision = setting.revisions.get(revision=1)
        row = self.series("独立引用名", bible_variant_id=str(setting.pk))
        asset = self.make_asset(owner=owner, name="reference", kind="reference")
        version = self.attach(row, asset, setting_revision_id=str(revision.pk))
        self.assertEqual(version.status_code, 201, version.data)
        self.assertEqual(version.data["setting_revision_id"], str(revision.pk))
        self.assertNotIn("PRIVATE_ANCHOR", str(version.data) + str(self.client.get(SERIES).data))
        wrong = self.series("no binding")
        self.assertEqual(self.attach(wrong, asset, setting_revision_id=str(revision.pk)).status_code, 400)
        self.assertEqual(self.attach(row, asset, setting_revision_id=str(uuid4())).status_code, 400)
        self.assertEqual(self.client.patch(f"{SERIES}{row['id']}/", {"bible_variant_id": None}, format="json").status_code, 400)
        self.assertEqual(self.client.delete(f"{VERSIONS}{version.data['id']}/").status_code, 204)
        self.assertEqual(self.client.patch(f"{SERIES}{row['id']}/", {"bible_variant_id": None}, format="json").status_code, 200)
        self.assertEqual(BibleVariantRevision.objects.get(pk=revision.pk).appearance_anchor, "PRIVATE_ANCHOR")

    def test_bible_role_and_owner_must_match_series(self):
        owner, project, character, *_ = build_story("reference-api-bible-scope")
        setting = BibleVariant.objects.create(project=project, character=character, name="variant", reference_name="variant",
            reference_key="variant", appearance_role="variant", created_by=owner)
        response = self.client.patch(f"{SERIES}{self.primary['id']}/", {"bible_variant_id": str(setting.pk)}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.client.force_authenticate(owner)
        primary = self.series("same owner wrong role")
        response = self.client.patch(f"{SERIES}{primary['id']}/", {"bible_variant_id": str(setting.pk)}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("bible_variant_id", str(response.data))

    def test_reads_report_current_asset_state_without_rewriting_relationship(self):
        version = self.attach().data
        self.asset.is_active = False
        self.asset.save(update_fields=["is_active"])
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(f"{VERSIONS}{version['id']}/")
        self.assertFalse(response.data["asset"]["is_active"])
        self.assertEqual(response.data["id"], version["id"])
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))

    def test_reassociation_and_rename_preserve_version_identity(self):
        other = self.series("new primary")
        variant = self.series("旧名", role="variant", primary_series_id=self.primary["id"])
        first = self.attach(variant)
        response = self.client.patch(f"{SERIES}{variant['id']}/", {"name": "新名",
            "primary_series_id": other["id"]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.client.get(f"{VERSIONS}{first.data['id']}/").data, first.data)
        self.assertEqual(self.client.patch(f"{SERIES}{variant['id']}/", {"role": "primary"}, format="json").status_code, 400)

    def test_generated_source_read_and_deletion_dependencies_never_delete_media(self):
        base = self.attach()
        variant = self.series("generated variant", role="variant", primary_series_id=self.primary["id"])
        artifact = GeneratedArtifact.objects.create(asset=self.asset, manifest_key=str(uuid4()), status="invalid",
            kind="image", role="isolated")
        row = AssetReferenceVersion.objects.create(series_id=variant["id"], version=1, asset=self.asset,
            source="generated", generated_artifact=artifact, generated_from_id=base.data["id"])
        detail = self.client.get(f"{VERSIONS}{row.pk}/")
        self.assertEqual(detail.data["generated_artifact"]["status"], "invalid")
        self.assertEqual(detail.data["generated_from_id"], base.data["id"])
        self.assertIsNone(detail.data["generation_job"])
        other = self.series("another")
        self.assertEqual(self.client.patch(f"{SERIES}{variant['id']}/",
            {"primary_series_id": other["id"]}, format="json").status_code, 400)
        self.assertEqual(self.client.delete(f"{VERSIONS}{base.data['id']}/").status_code, 409)
        self.assertEqual(self.client.delete(f"{SERIES}{self.primary['id']}/").status_code, 409)
        self.assertEqual(self.client.delete(f"{SERIES}{variant['id']}/").status_code, 204)
        self.assertEqual(self.client.delete(f"{SERIES}{self.primary['id']}/").status_code, 204)
        self.assertTrue(Asset.objects.filter(pk=self.asset.pk).exists())
        self.assertTrue(GeneratedArtifact.objects.filter(pk=artifact.pk).exists())
        self.assertEqual(str(Asset.objects.get(pk=self.asset.pk).file), str(self.asset.file))
        self.assertTrue(Path(self.asset.file.path).is_file())

    def test_missing_version_series_and_invalid_pagination_are_explicit(self):
        self.assertEqual(self.client.get(VERSIONS).status_code, 400)
        for params, code in (({"series_id": str(uuid4())}, 404),
                ({"series_id": self.primary["id"], "page_size": 0}, 400),
                ({"series_id": "invalid"}, 400)):
            self.assertEqual(self.client.get(VERSIONS, params).status_code, code)
        self.assertEqual(self.client.get(f"{VERSIONS}{uuid4()}/").status_code, 404)
        self.assertEqual(self.client.delete(f"{VERSIONS}{uuid4()}/").status_code, 404)
        for endpoint in (SERIES, VERSIONS):
            self.assertEqual(self.client.delete(f"{endpoint}invalid/").status_code, 404)

    def test_removal_impact_counts_exact_scope_without_writing_or_deleting_assets(self):
        first = self.attach().data
        second_asset = self.make_asset(owner=self.owner, name="second", kind="reference")
        second = self.attach(asset=second_asset).data
        variant = self.series("child", role="variant", primary_series_id=self.primary["id"])
        artifact = GeneratedArtifact.objects.create(asset=self.asset, manifest_key=str(uuid4()), status="invalid", kind="image", role="isolated")
        dependent = AssetReferenceVersion.objects.create(series_id=variant["id"], version=1, asset=self.asset,
            source="generated", generated_artifact=artifact, generated_from_id=first["id"])
        with CaptureQueriesContext(connection) as queries:
            impact = self.client.get(f"{SERIES}{self.primary['id']}/removal-impact/")
        self.assertEqual(impact.status_code, 200, impact.data)
        self.assertEqual({key: impact.data[key] for key in ("version_count", "asset_count", "variant_count", "dependent_version_count", "can_remove")},
            dict(version_count=2, asset_count=2, variant_count=1, dependent_version_count=1, can_remove=False))
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        independent = self.client.get(f"{VERSIONS}{second['id']}/removal-impact/").data
        self.assertTrue(independent["can_remove"])
        self.assertEqual(independent["version_count"], 1)
        self.assertEqual(self.client.delete(f"{VERSIONS}{second['id']}/").status_code, 204)
        self.assertTrue(AssetReferenceVersion.objects.filter(pk=first["id"]).exists())
        self.assertTrue(AssetReferenceVersion.objects.filter(pk=dependent.pk).exists())
        self.assertTrue(Path(second_asset.file.path).is_file())

    def test_deletion_rechecks_dependencies_added_after_preview(self):
        first = self.attach().data
        preview = self.client.get(f"{SERIES}{self.primary['id']}/removal-impact/").data
        self.assertTrue(preview["can_remove"])
        self.series("new child", role="variant", primary_series_id=self.primary["id"])
        response = self.client.delete(f"{SERIES}{self.primary['id']}/")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertTrue(AssetReferenceVersion.objects.filter(pk=first["id"]).exists())
        self.client.force_authenticate(self.other)
        for path in (f"{SERIES}{self.primary['id']}/removal-impact/", f"{SERIES}{self.primary['id']}/bible-options/", f"{VERSIONS}{first['id']}/removal-impact/"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_bible_options_use_series_owner_role_pagination_search_and_narrow_fields(self):
        owner, project, character, *_ = build_story("reference-management-bible")
        self.client.force_authenticate(owner)
        series = self.series("日本語・衣装")
        characters = [character] + [CharacterBible.objects.create(project=project, name=f"{character.name} {n}",
            slug=f"fixture-{n}", stable_speaker_id=f"S{n + 10}") for n in range(1, 3)]
        settings = [BibleVariant.objects.create(project=project, character=characters[n], name=f"主图 {n}",
            reference_name=f"n{n}", reference_key=f"main{n}", appearance_role="primary", created_by=owner) for n in range(3)]
        BibleVariant.objects.create(project=project, character=character, name="wrong-role", reference_name="wrong-role",
            reference_key="wrong-role", appearance_role="variant", created_by=owner)
        url = f"{SERIES}{series['id']}/bible-options/"
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url, {"page_size": 1, "page": 2})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(response.data["results"][0]["id"], str(settings[1].pk))
        self.assertEqual(response.data["results"][0]["character_name"], characters[1].name)
        self.assertEqual(response.data["results"][0]["project_title"], project.title)
        setting_queries = [q["sql"] for q in queries if 'FROM "studio_biblevariant"' in q["sql"]]
        self.assertEqual(len(setting_queries), 2)
        self.assertNotIn('"appearance_anchor"', " ".join(setting_queries))
        from studio.services.asset_references import bible_options
        self.assertIn("SEARCH", bible_options(AssetReferenceSeries.objects.get(pk=series["id"])).explain())
        self.assertEqual(self.client.get(url, {"search": character.name}).data["count"], 3)
        self.assertEqual(self.client.get(url, {"id": str(settings[2].pk)}).data["count"], 1)
        self.assertEqual(self.client.get(url, {"search": "no-match"}).data["count"], 0)
        self.assertEqual(self.client.get(url, {"id": "bad"}).status_code, 400)
        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(url).data["count"], 3)
        patch = self.client.patch(f"{SERIES}{series['id']}/", {"bible_variant_id": str(settings[0].pk), "name": "任意语言 / tenue"}, format="json")
        self.assertEqual(patch.status_code, 200, patch.data)
        self.assertEqual(patch.data["id"], series["id"])
        self.assertEqual(self.client.patch(f"{SERIES}{series['id']}/", {"bible_variant_id": None}, format="json").status_code, 200)

    def test_asset_delete_explains_reference_relation_and_preserves_shared_file(self):
        version = self.attach().data
        response = self.client.delete(f"/api/v1/assets/{self.asset.pk}/")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("主图/变体版本关联", str(response.data))
        self.client.delete(f"{VERSIONS}{version['id']}/")
        shared = Asset.objects.create(owner=self.owner, name="shared object", kind="reference",
            file=self.asset.file.name, mime_type=self.asset.mime_type, checksum=self.asset.checksum)
        path = Path(self.asset.file.path)
        with self.captureOnCommitCallbacks(execute=True):
            removed = self.client.delete(f"/api/v1/assets/{self.asset.pk}/")
        self.assertEqual(removed.status_code, 204, removed.data)
        self.assertTrue(Asset.objects.filter(pk=shared.pk).exists())
        self.assertTrue(path.is_file())
        with self.captureOnCommitCallbacks(execute=True):
            removed = self.client.delete(f"/api/v1/assets/{shared.pk}/")
        self.assertEqual(removed.status_code, 204, removed.data)
        self.assertFalse(path.exists())

    def test_narrow_projections_are_constant_query_and_use_relation_indexes(self):
        for index in range(6):
            asset = self.make_asset(owner=self.owner, name=str(index), kind="reference",
                metadata={"big": "z" * 8192}, description="PRIVATE_DESCRIPTION")
            self.attach(asset=asset)
            self.series(f"variant{index}", role="variant", primary_series_id=self.primary["id"])
        with CaptureQueriesContext(connection) as captured:
            data = AssetReferenceVersionSummary(version_summaries(self.owner).filter(series_id=self.primary["id"]), many=True).data
        self.assertEqual(len(captured), 1)
        self.assertNotIn("PRIVATE_DESCRIPTION", str(data))
        select = captured[0]["sql"].split(" FROM ")[0]
        for field in ("metadata", "description", "response_payload", "appearance_anchor", "local_path", "request_snapshot"):
            self.assertNotIn(f'"{field}"', select)
        with self.assertNumQueries(1):
            rows = AssetReferenceSeriesSummary(series_summaries(self.owner), many=True).data
        primary = next(row for row in rows if row["id"] == self.primary["id"])
        self.assertEqual((primary["version_count"], primary["variant_count"]), (6, 6))
        plan = version_summaries(self.owner).filter(series_id=self.primary["id"]).explain()
        self.assertIn("INDEX", plan.upper())
        self.assertIn("idx_asset_series_owner_role", series_summaries(self.owner).filter(role="primary").explain())


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class AssetReferenceJobProjectionTests(TestCase):
    _create_version = workflow_fixtures.ExecutionWorkflowMediaRequestTests._create_version
    setUp = workflow_fixtures.ExecutionWorkflowMediaRequestTests.setUp

    def test_original_standalone_job_is_read_without_fabricating_artifact(self):
        job = AssetGenerationJob.objects.create(owner=self.user, model_deployment=self.deployment,
            execution_instance_version_id=self.definition.value["version_id"], workflow_template_version=self.workflow,
            execution_kind="comfy_workflow", job_type="asset", media_type="image", status="succeeded",
            operation="image.generate", idempotency_key=str(uuid4()), request_snapshot_sha256="a" * 64,
            output_asset=self.asset)
        series = AssetReferenceSeries.objects.create(owner=self.user, name="主图", role="primary")
        row = AssetReferenceVersion.objects.create(series=series, version=1, asset=self.asset, source="generated", generation_job=job)
        client = APIClient()
        client.force_authenticate(self.user)
        before = GeneratedArtifact.objects.count()
        response = client.get(f"{VERSIONS}{row.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["generation_job"], {"id": str(job.pk), "status": "succeeded"})
        self.assertIsNone(response.data["generated_artifact"])
        self.assertEqual(GeneratedArtifact.objects.count(), before)
