"""M4 original fulfillment -> read-only M5 exact-use defaults, isolated files.

The fixture has an original downstream review stage, not a new scheduler/UI.
Explicit Shot rows model declared uses; no live model or business adoption runs.
"""
from copy import deepcopy
import re
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import AssetReferenceVersion, Episode, Shot, ShotReferenceUse, StageRun
from studio.services.asset_references import attach_asset
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.production_material_references import read_upstream_material_catalog
from studio.services.production_shot_reference_defaults import plan_upstream_shot_reference_choices
from studio.services.production_runs import _save_stage_snapshot, _sha256
from studio.services.shot_reference_choices import apply_shot_reference_choices
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_production_material_references as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class ShotReferenceDefaultTests(StructureFixture):
    Fixture = fixtures.ProductionMaterialReferenceTests
    tearDown = Fixture.tearDown
    facts = Fixture.facts
    attach = Fixture.attach
    choose = Fixture.choose
    create_job = Fixture.create_job
    primary_version = Fixture.primary_version
    write = Fixture.write
    input = staticmethod(Fixture.input)
    configure_variant_sources = Fixture.configure_variant_sources
    configure_declaration = Fixture.configure_declaration
    target = Fixture.target
    prepare = Fixture.prepare
    complete = Fixture.complete

    def setUp(self):
        self.Fixture.setUp(self)
        self.fulfillments = self.complete()
        episode = Episode.objects.create(project=self.project, number=91, title="Isolated exact-use defaults")
        self.shots = [Shot.objects.create(episode=episode, number=number, duration_ms=1000,
            media_type="video", include_audio=False) for number in (1, 2)]

    def use(self, *, setting=None, shot=None, key="clothes", position=0, **changes):
        return ShotReferenceUse.objects.create(shot=shot or self.shots[0], use_key=key, position=position,
            media_kind="image", purpose="本镜校服参考", setting_revision=setting, **changes)

    def plan_defaults(self, **changes):
        return plan_upstream_shot_reference_choices(**{"stage_run_id": self.review.pk, "actor": self.user,
            "shot_id": self.shots[0].pk, "source_stage_id": self.stage.pk,
            "expected_source_output_sha256": self.stage.output_snapshot_sha256, **changes})

    def save_output(self, value):
        value["canonical_sha256"] = _sha256(value["canonical"])
        _save_stage_snapshot(self.stage, "output_snapshot", value)

    @staticmethod
    def writes(queries):
        return [row["sql"] for row in queries if row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]

    def test_exact_clothes_match_is_read_only_and_original_writer_preserves_siblings(self):
        first = self.use(setting=self.settings[0])
        second = self.use(setting=self.settings[1], shot=self.shots[1])
        before = (self.facts(), list(AssetReferenceVersion.objects.values()), self.character.anchor_asset_id)
        with CaptureQueriesContext(connection) as queries:
            left, right = self.plan_defaults(), self.plan_defaults(shot_id=self.shots[1].pk)
        self.assertFalse(self.writes(queries))
        self.assertTrue(left.ready and right.ready)
        self.assertEqual(left.choices[0]["reference_version_id"], str(self.fulfillments[1].reference_version_id))
        self.assertEqual(right.choices[0]["reference_version_id"], str(self.fulfillments[2].reference_version_id))
        self.assertNotEqual(left.choices, right.choices)
        result = apply_shot_reference_choices(actor=self.user, project_id=self.project.pk, shot_id=self.shots[0].pk,
            expected_uses_sha256=left.state.sha256, choices=left.choices)
        self.assertEqual(result.changed_use_ids, (str(first.pk),))
        second.refresh_from_db()
        self.assertIsNone(second.reference_version_id)
        self.assertEqual(before, (self.facts(), list(AssetReferenceVersion.objects.values()), self.character.anchor_asset_id))
        with CaptureQueriesContext(connection) as queries:
            retained = self.plan_defaults()
        self.assertFalse(self.writes(queries))
        self.assertEqual(retained.choices, [])
        self.assertEqual(retained.items[0]["status"], "retained")
        self.assertEqual(first.purpose, "本镜校服参考")

    def test_duplicate_use_of_one_setting_resolves_original_result_once_and_keeps_order(self):
        first = self.use(setting=self.settings[0])
        second = self.use(setting=self.settings[0], key="profile", position=1)
        from studio.services.production_shot_reference_defaults import read_upstream_material_result
        with patch("studio.services.production_shot_reference_defaults.read_upstream_material_result",
                wraps=read_upstream_material_result) as reader:
            plan = self.plan_defaults()
        self.assertEqual(reader.call_count, 1)
        self.assertEqual([row["use_id"] for row in plan.choices], [str(first.pk), str(second.pk)])
        self.assertEqual(len({row["reference_version_id"] for row in plan.choices}), 1)
        copy = plan.items
        copy[0]["source"]["reference"]["source"] = "forged"
        self.assertEqual(plan.items[0]["source"]["reference"]["source"], "existing")

    def test_catalog_is_narrow_read_only_and_does_not_probe_media(self):
        with CaptureQueriesContext(connection) as queries, patch(
                "studio.services.production_material_references.read_material_result_reference",
                side_effect=AssertionError("Catalog is metadata only")):
            entries = read_upstream_material_catalog(stage=self.review, source_stage_id=str(self.stage.pk),
                expected_output_sha256=self.stage.output_snapshot_sha256)
        self.assertEqual(len(entries), 3)
        self.assertFalse(self.writes(queries))
        self.use(setting=self.settings[0])
        with CaptureQueriesContext(connection) as queries:
            self.plan_defaults()
        metadata = [row["sql"] for row in queries if row["sql"].startswith("SELECT ")
            and ' FROM "studio_assetreferenceversion"' in row["sql"]
            and re.findall(r'"studio_assetreferenceversion"\."([^"]+)"',
                row["sql"].partition(" FROM ")[0]) == ["id", "setting_revision_id"]]
        self.assertEqual(len(metadata), 1)
        self.assertNotIn("appearance_anchor", metadata[0])
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + metadata[0])
            explain = str(cursor.fetchall()).upper()
        self.assertIn("INDEX", explain)
        self.assertNotIn("SCAN STUDIO_ASSETREFERENCEVERSION", explain)
        self.assertNotIn("TEMP B-TREE", explain)

    def test_no_setting_or_new_exact_revision_waits_without_library_or_name_fallback(self):
        self.use()
        variant = revise_variant(actor=self.user, variant=self.settings[0].variant, expected_revision=1,
            value={**setting_payload(self.settings[0]), "appearance_anchor": "new version"})
        self.use(setting=variant.revisions.get(revision=2), key="new", position=1)
        with CaptureQueriesContext(connection) as queries:
            result = self.plan_defaults()
        self.assertEqual([row["status"] for row in result.items], ["manual", "missing"])
        self.assertFalse(result.ready)
        self.assertEqual(result.choices, [])
        self.assertFalse(self.writes(queries))

    def test_current_manual_choice_is_retained_and_corrupt_choice_does_not_redefault(self):
        version = self.fulfillments[2].reference_version
        row = self.use(setting=self.settings[1], reference_version=version, source_mode="existing",
            asset_sha256=version.asset.checksum)
        self.assertEqual(self.plan_defaults().items[0]["status"], "retained")
        ShotReferenceUse.objects.filter(pk=row.pk).update(asset_sha256="0" * 64)
        with CaptureQueriesContext(connection) as queries, self.assertRaisesMessage(ValidationError, "指纹"):
            self.plan_defaults()
        self.assertFalse(self.writes(queries))

    def test_later_library_version_does_not_replace_original_selected_output(self):
        self.use(setting=self.settings[0])
        original = self.fulfillments[1].reference_version
        different_asset = next(asset for asset in self.assets if asset.pk != original.asset_id)
        later, _ = attach_asset(actor=self.user, series_id=original.series_id, asset_id=different_asset.pk,
            source="existing", setting_revision_id=original.setting_revision_id)
        self.assertNotEqual(later.pk, original.pk)
        self.assertEqual(self.plan_defaults().choices[0]["reference_version_id"], str(original.pk))

    def test_duplicate_or_ambiguous_upstream_receipts_never_choose_first(self):
        self.use(setting=self.settings[0])
        original = deepcopy(self.stage.output_snapshot)
        version_id = str(self.fulfillments[1].reference_version_id)
        ref = next(row for row in original["canonical"]["material_fulfillments"] if row["reference_version_id"] == version_id)
        duplicate = deepcopy(original)
        duplicate["canonical"]["material_fulfillments"].append(deepcopy(ref))
        self.save_output(duplicate)
        with self.assertRaisesMessage(ValidationError, "重复选择"):
            self.plan_defaults()
        # Malformed competing identity is not authorized as a choice: ambiguity
        # stops before resolving either candidate, never fabricating success.
        ambiguous = deepcopy(original)
        ambiguous["canonical"]["material_fulfillments"].append({**ref, "fulfillment_id": str(uuid4())})
        self.save_output(ambiguous)
        plan = self.plan_defaults()
        self.assertFalse(plan.ready)
        self.assertEqual(plan.items[0]["status"], "ambiguous")
        self.assertEqual(plan.choices, [])

    def test_source_topology_hash_and_permissions_are_rechecked(self):
        self.use(setting=self.settings[0])
        for changes in ({"expected_source_output_sha256": "0" * 64}, {"source_stage_id": self.review.pk},
                {"shot_id": uuid4()}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.plan_defaults(**changes)
        with self.assertRaises(PermissionDenied):
            self.plan_defaults(actor=AnonymousUser())
        StageRun.objects.filter(pk=self.stage.pk).update(status="running")
        with self.assertRaisesMessage(ValidationError, "已完成的上游"):
            self.plan_defaults()

    def test_invalid_original_file_is_an_error_and_never_falls_back_to_primary(self):
        self.use(setting=self.settings[0])
        self.fulfillments[1].reference_version.asset.file.delete(save=False)
        before = list(ShotReferenceUse.objects.values())
        with self.assertRaises(ValidationError):
            self.plan_defaults()
        self.assertEqual(list(ShotReferenceUse.objects.values()), before)

    def test_empty_uses_are_ready_without_writing_or_inventing_primary_uses(self):
        with CaptureQueriesContext(connection) as queries:
            result = self.plan_defaults()
        self.assertTrue(result.ready)
        self.assertEqual(result.items, [])
        self.assertEqual(result.choices, [])
        self.assertFalse(self.writes(queries))
