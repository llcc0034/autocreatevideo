"""Internal M5 choice transaction; real disposable files, no live execution."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models.query import QuerySet
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from PIL import Image

from studio.models import (
    ApprovalDecision, Asset, AssetReferenceSeries, AssetReferenceVersion, BibleVariant,
    BibleVariantRevision, ExecutionPreparation, GenerationAttempt, Shot, ShotReferenceUse,
)
from studio.services.asset_references import attach_asset
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.shot_reference_choices import apply_shot_reference_choices, read_shot_reference_choices
from studio.services.shot_reference_sources import resolve_shot_reference_sources
from studio.tests import test_shot_reference_sources as fixtures


class ShotReferenceChoiceTests(TestCase):
    # Reuse fixture helpers, not the TestCase itself (no duplicated discovered tests).
    setUp = fixtures.ShotReferenceSourceTests.setUp
    media = fixtures.ShotReferenceSourceTests.media
    use = fixtures.ShotReferenceSourceTests.use
    setting = fixtures.ShotReferenceSourceTests.setting
    version = fixtures.ShotReferenceSourceTests.version

    def pending(self, *, shot=None, key="clothes", position=0, setting=None):
        return ShotReferenceUse.objects.create(shot=shot or self.shot, use_key=key, position=position,
            media_kind="image", purpose=f"configured {key}", setting_revision=setting)

    def state(self, **changes):
        return read_shot_reference_choices(**{"actor": self.owner, "project_id": self.project.pk,
            "shot_id": self.shot.pk, **changes})

    def choice(self, use, *, asset=None, version=None, mode="existing"):
        selected = version.asset if version else (asset or self.asset)
        return {"use_id": str(use.pk), "source_mode": mode,
            "reference_version_id": str(version.pk) if version else None,
            "asset_id": None if version else str(selected.pk), "asset_sha256": selected.checksum}

    def apply(self, choices, **changes):
        expected = changes.pop("expected_uses_sha256", None)
        return apply_shot_reference_choices(**{"actor": self.owner, "project_id": self.project.pk,
            "shot_id": self.shot.pk, "expected_uses_sha256": expected or self.state().sha256,
            "choices": choices, **changes})

    @staticmethod
    def writes(queries):
        return [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]

    def test_pending_baseline_is_narrow_read_only_and_not_media_readiness(self):
        self.pending()
        with CaptureQueriesContext(connection) as queries, patch(
                "studio.services.shot_reference_sources.resolve_execution_asset_media",
                side_effect=AssertionError("Baseline must not read files")):
            state = self.state()
        self.assertFalse(self.writes(queries))
        self.assertEqual(state.value["uses"][0]["source_mode"], "")
        self.assertIsNone(state.value["uses"][0]["asset_id"])
        sql = next(q["sql"] for q in queries if 'FROM "studio_shotreferenceuse"' in q["sql"])
        for field in ("SELECT *", "narrative", "metadata", "updated_at", "appearance_anchor"):
            self.assertNotIn(field, sql)
        original_hash = state.sha256
        changed = state.value
        changed["uses"][0]["purpose"] = "caller changed copy"
        self.assertEqual(state.sha256, original_hash)
        self.assertNotEqual(state.value, changed)
        with self.assertRaisesMessage(ValidationError, "尚未选择"):
            resolve_shot_reference_sources(actor=self.owner, project_id=self.project.pk, shot_ids=[self.shot.pk])

    def test_one_use_change_preserves_bible_other_shot_and_original_facts(self):
        primary = self.setting("primary")
        base = self.version(primary)
        outfit = self.setting("coat", primary)
        selected = self.version(outfit, base.series)
        row = self.pending(setting=outfit)
        self.use(shot=self.second, setting=primary, version=base)
        other = self.state(shot_id=self.second.pk).value
        models = (BibleVariant, BibleVariantRevision, Asset, AssetReferenceSeries, AssetReferenceVersion,
            ApprovalDecision, ExecutionPreparation, GenerationAttempt, Shot)
        before = {model: list(model.objects.order_by("pk").values()) for model in models}
        baseline = self.state()
        result = self.apply([self.choice(row, version=selected)], expected_uses_sha256=baseline.sha256)
        self.assertEqual(result.before_sha256, baseline.sha256)
        self.assertNotEqual(result.state.sha256, baseline.sha256)
        self.assertEqual(result.changed_use_ids, (str(row.pk),))
        self.assertEqual(result.state.value, self.state().value)
        self.assertEqual(other, self.state(shot_id=self.second.pk).value)
        self.assertEqual(before, {model: list(model.objects.order_by("pk").values()) for model in models})
        old, new = baseline.value["uses"][0], result.state.value["uses"][0]
        self.assertEqual({k: v for k, v in old.items() if k not in {
            "source_mode", "reference_version_id", "asset_id", "asset_sha256"}},
            {k: v for k, v in new.items() if k not in {
            "source_mode", "reference_version_id", "asset_id", "asset_sha256"}})
        self.assertEqual(result.selected.value["shots"][0]["uses"][0]["source"]["reference_version_id"], str(selected.pk))

    def test_subset_does_not_claim_whole_shot_ready_or_fill_pending_uses(self):
        first = self.pending()
        waiting = self.pending(key="composition", position=1)
        result = self.apply([self.choice(first, mode="upload")])
        self.assertEqual(len(result.selected.value["shots"][0]["uses"]), 1)
        self.assertEqual(len(result.state.value["uses"]), 2)
        waiting.refresh_from_db()
        self.assertIsNone(waiting.asset_id)
        self.assertEqual(waiting.source_mode, "")
        with self.assertRaisesMessage(ValidationError, "尚未选择"):
            resolve_shot_reference_sources(actor=self.owner, project_id=self.project.pk, shot_ids=[self.shot.pk])

    def test_two_choices_use_saved_order_and_exact_different_bytes(self):
        first, second = self.pending(), self.pending(key="pose", position=1)
        red = BytesIO()
        Image.new("RGB", (8, 8), color="red").save(red, format="PNG")
        second_asset = self.media(red.getvalue())
        choices = [self.choice(second, asset=second_asset), self.choice(first, mode="upload")]
        unchanged = deepcopy(choices)
        result = self.apply(choices)
        self.assertEqual(choices, unchanged)
        self.assertEqual(result.changed_use_ids, (str(first.pk), str(second.pk)))
        uses = result.selected.value["shots"][0]["uses"]
        self.assertEqual([row["use_key"] for row in uses], ["clothes", "pose"])
        self.assertEqual([row["asset"]["sha256"] for row in uses], [self.asset.checksum, second_asset.checksum])
        self.assertNotEqual(self.asset.checksum, second_asset.checksum)
        self.assertEqual(result.selected.value, resolve_shot_reference_sources(actor=self.owner,
            project_id=self.project.pk, shot_ids=[self.shot.pk]).value)

    def test_new_version_and_setting_revision_do_not_redirect_an_explicit_choice(self):
        setting = self.setting("primary")
        version = self.version(setting)
        row = self.pending(setting=setting)
        before = self.state()
        newer = attach_asset(actor=self.owner, series_id=version.series_id, asset_id=self.media().pk,
            source="existing", setting_revision_id=setting.pk)[0]
        revise_variant(actor=self.owner, variant=setting.variant, expected_revision=1,
            value={**setting_payload(setting), "appearance_anchor": "later setting"})
        result = self.apply([self.choice(row, version=version)], expected_uses_sha256=before.sha256)
        selected = result.selected.value["shots"][0]["uses"][0]
        self.assertEqual(selected["source"]["reference_version_id"], str(version.pk))
        self.assertNotEqual(selected["source"]["reference_version_id"], str(newer.pk))
        row.refresh_from_db()
        self.assertEqual(row.setting_revision_id, setting.pk)

    def test_invalid_second_media_is_checked_before_any_choice_write(self):
        first, second = self.pending(), self.pending(key="pose", position=1)
        before = self.state().value
        invalid = self.choice(second)
        invalid["asset_sha256"] = "f" * 64
        with CaptureQueriesContext(connection) as queries, self.assertRaisesMessage(ValidationError, "偏离本次选择"):
            self.apply([self.choice(first), invalid])
        self.assertFalse(self.writes(queries))
        self.assertEqual(before, self.state().value)

    def test_late_write_error_rolls_back_first_choice(self):
        first, second = self.pending(), self.pending(key="pose", position=1)
        before = self.state().value
        update = QuerySet.update
        calls = []
        def broken_update(query, **kwargs):
            if query.model is ShotReferenceUse:
                calls.append(kwargs)
                if len(calls) == 2:
                    self.assertEqual(ShotReferenceUse.objects.get(pk=first.pk).asset_id, self.asset.pk)
                    raise RuntimeError("isolated second write failure")
            return update(query, **kwargs)
        with patch.object(QuerySet, "update", broken_update), self.assertRaisesMessage(RuntimeError, "second write failure"):
            self.apply([self.choice(first), self.choice(second)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(before, self.state().value)

    def test_original_outer_transaction_failure_rolls_back_selection(self):
        row = self.pending()
        before = self.state().value
        with self.assertRaisesMessage(RuntimeError, "original command receipt"):
            with transaction.atomic():
                self.apply([self.choice(row)])
                self.assertEqual(ShotReferenceUse.objects.get(pk=row.pk).asset_id, self.asset.pk)
                raise RuntimeError("isolated original command receipt failure")
        self.assertEqual(before, self.state().value)

    def test_missing_write_target_is_not_reported_as_success(self):
        row = self.pending()
        before = self.state().value
        with patch.object(QuerySet, "update", return_value=0), self.assertRaisesMessage(ValidationError, "写入目标已变化"):
            self.apply([self.choice(row)])
        self.assertEqual(before, self.state().value)

    def test_stale_selection_is_conflict_even_when_desired_values_match(self):
        row = self.pending()
        baseline = self.state().sha256
        self.apply([self.choice(row)], expected_uses_sha256=baseline)
        with CaptureQueriesContext(connection) as queries, self.assertRaises(ValidationError) as error:
            self.apply([self.choice(row)], expected_uses_sha256=baseline)
        self.assertFalse(self.writes(queries))
        self.assertEqual(error.exception.error_dict["expected_uses_sha256"][0].code, "shot_reference_choices_stale")

    def test_any_changed_use_declaration_invalidates_baseline(self):
        row = self.pending()
        changes = ({"purpose": "changed"}, {"use_key": "changed"}, {"position": 3}, {"media_kind": "video"},
            {"setting_revision_id": self.setting("setting").pk})
        for fields in changes:
            with self.subTest(fields=fields):
                baseline = self.state().sha256
                original = {key: getattr(row, key) for key in fields}
                ShotReferenceUse.objects.filter(pk=row.pk).update(**fields)
                with self.assertRaisesMessage(ValidationError, "用途或选材已改变"):
                    self.apply([self.choice(row)], expected_uses_sha256=baseline)
                ShotReferenceUse.objects.filter(pk=row.pk).update(**original)
        baseline = self.state().sha256
        added = self.pending(key="extra", position=1)
        with self.assertRaisesMessage(ValidationError, "用途或选材已改变"):
            self.apply([self.choice(row)], expected_uses_sha256=baseline)
        baseline = self.state().sha256
        added.delete()  # Isolated unselected fixture only.
        with self.assertRaisesMessage(ValidationError, "用途或选材已改变"):
            self.apply([self.choice(row)], expected_uses_sha256=baseline)

    def test_current_identical_choice_rechecks_bytes_and_never_touches_timestamps(self):
        row = self.use()
        before, modified = self.state(), row.updated_at
        with CaptureQueriesContext(connection) as queries:
            result = self.apply([self.choice(row)])
        self.assertFalse(self.writes(queries))
        self.assertEqual(result.changed_use_ids, ())
        self.assertEqual(result.state.sha256, before.sha256)
        row.refresh_from_db()
        self.assertEqual(row.updated_at, modified)
        Path(self.asset.file.path).write_bytes(b"isolated byte change")
        with self.assertRaisesMessage(ValidationError, "哈希"):
            self.apply([self.choice(row)], expected_uses_sha256=before.sha256)
        self.assertEqual(self.state().sha256, before.sha256)

    def test_bad_input_unknown_and_foreign_uses_never_write(self):
        row = self.pending()
        foreign = self.pending(shot=self.second)
        valid = self.choice(row)
        payloads = [None, {}, [], "bad", [None], [valid, valid], [self.choice(foreign)],
            [{**valid, "use_id": str(uuid4())}], [{**valid, "purpose": "must not change"}],
            [{k: v for k, v in valid.items() if k != "asset_id"}]]
        for field, values in {"use_id": (None, "bad"), "source_mode": (None, [], "", "automatic"),
                "asset_id": (None, "", "bad"), "asset_sha256": (None, [], "bad", "F" * 64)}.items():
            payloads.extend([{**valid, field: value}] for value in values)
        payloads.append([{**valid, "reference_version_id": str(uuid4())}])
        before = self.state().value
        for payload in payloads:
            with self.subTest(payload=payload), CaptureQueriesContext(connection) as queries, self.assertRaises(ValidationError):
                self.apply(payload)
            self.assertFalse(self.writes(queries))
            self.assertEqual(before, self.state().value)
        for invalid in (None, [], "bad", "F" * 64):
            with self.subTest(expected=invalid), self.assertRaises(ValidationError):
                apply_shot_reference_choices(actor=self.owner, project_id=self.project.pk, shot_id=self.shot.pk,
                    expected_uses_sha256=invalid, choices=[valid])

    def test_authorization_is_fresh_for_baseline_and_write(self):
        row = self.pending()
        baseline = self.state().sha256
        cases = ({"actor": AnonymousUser()}, {"actor": self.other}, {"project_id": uuid4()},
            {"project_id": "bad"}, {"shot_id": uuid4()}, {"shot_id": "bad"}, {"shot_id": self.foreign_shot.pk})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError): self.state(**changes)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.apply([self.choice(row)], expected_uses_sha256=baseline, **changes)
        self.project.owner = self.other
        self.project.save(update_fields=["owner"])
        with self.assertRaises(ValidationError):
            self.apply([self.choice(row)], expected_uses_sha256=baseline)

    def test_staff_cannot_attach_foreign_media_or_version_to_owner_project(self):
        row = self.pending()
        self.other.is_staff = True
        self.other.save(update_fields=["is_staff"])
        foreign_asset = self.media(owner=self.other)
        foreign_series = AssetReferenceSeries.objects.create(owner=self.other, role="primary", name="foreign")
        version = AssetReferenceVersion.objects.create(series=foreign_series, version=1,
            source="existing", asset=foreign_asset)
        for choice in (self.choice(row, asset=foreign_asset), self.choice(row, version=version)):
            with self.subTest(choice=choice), self.assertRaises(ValidationError):
                self.apply([choice], actor=self.other)
        self.apply([self.choice(row)], actor=self.other)
        row.refresh_from_db()
        self.assertEqual(row.asset_id, self.asset.pk)

    def test_setting_mismatch_inactive_media_and_invalid_origin_fail_closed(self):
        primary = self.setting("primary")
        version = self.version(primary)
        variant = self.setting("other", primary)
        row = self.pending(setting=variant)
        for choice in (self.choice(row, version=version), self.choice(row, mode="generated"),
                self.choice(row, version=version, mode="generated")):
            with self.subTest(choice=choice), self.assertRaises(ValidationError): self.apply([choice])
        Asset.objects.filter(pk=self.asset.pk).update(is_active=False)
        with self.assertRaises(ValidationError): self.apply([self.choice(row)])
        Asset.objects.filter(pk=self.asset.pk).update(is_active=True, source_info="not an upload")
        with self.assertRaisesMessage(ValidationError, "正式上传来源"):
            self.apply([self.choice(row, mode="upload")])
        row.refresh_from_db()
        self.assertIsNone(row.asset_id)
        self.assertEqual(row.source_mode, "")

    def test_repeated_asset_is_checked_once_but_distinct_uses_are_preserved(self):
        first, second = self.pending(), self.pending(key="pose", position=1)
        from studio.services.shot_reference_sources import resolve_execution_asset_media
        with patch("studio.services.shot_reference_sources.resolve_execution_asset_media",
                wraps=resolve_execution_asset_media) as read:
            result = self.apply([self.choice(first), self.choice(second)])
        read.assert_called_once()
        self.assertEqual(len(result.changed_use_ids), 2)
        self.assertEqual(len(result.selected.value["shots"][0]["uses"]), 2)
