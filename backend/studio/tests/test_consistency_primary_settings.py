"""Consistency projections use one verified setting source, not legacy identity text."""
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.consistency_serializers import ConsistencySnapshotSerializer
from studio.models import (
    Asset, BibleVariant, CharacterBible, ConsistencyLock,
    Episode, ExecutionPreparation, GenerationAttempt, PropBible, SceneBible, SeriesProject, Shot, ShotReferenceUse,
)
from studio.services.bible_setting_evidence import primary_setting_content_reference
from studio.services.bible_variants import (
    BibleVariantConflict, create_variant, revise_variant, review_variant, setting_payload,
)
from studio.services.consistency import capture_consistency_snapshot, sync_consistency_locks


class ConsistencyPrimarySettingsTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="consistency-owner")
        self.other = get_user_model().objects.create_user(username="consistency-other")
        self.project = SeriesProject.objects.create(owner=self.owner, title="Isolated consistency",
            language="English", aspect_ratio="9:16", width=720, height=1280, fps=24,
            default_shot_duration_ms=1000, continuity_seed=5)
        self.entities = {}
        for kind, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible)):
            self.entities[kind] = model.objects.create(project=self.project, slug=kind, name=kind,
                **({"stable_speaker_id": "S1"} if kind == "character" else {}))
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def primary(self, kind, *, blank=False):
        return create_variant(actor=self.owner, project_id=self.project.pk,
            entity_kind=kind, entity_id=self.entities[kind].pk, appearance_role="primary",
            primary_setting_revision_id=None, primary_setting_sha256="", value={
                "name": "Primary", "reference_name": f"{kind} primary",
                "identity_anchor": "" if blank else f"identity {kind}",
                "appearance_anchor": "" if blank else f"appearance {kind}",
                "basis_asset_id": None, "basis_asset_sha256": ""})

    def lock_values(self):
        return list(self.project.consistency_locks.order_by("id").values(
            "id", "scope", "target_id", "lock_key", "locked_value", "version", "is_active"))

    def identity_lock(self, kind):
        return self.project.consistency_locks.get(scope=kind, target_id=self.entities[kind].pk, lock_key="identity")

    def test_absence_blank_and_empty_project_are_explicit_and_read_only(self):
        absent = capture_consistency_snapshot(self.project)
        for section in ("characters", "scenes", "props"):
            self.assertIsNone(absent[section][0]["primary_setting"])
            self.assertNotIn("visual_identity", absent[section][0])
        primary = self.primary("character", blank=True)
        value = capture_consistency_snapshot(self.project)["characters"][0]["primary_setting"]
        self.assertEqual(value["appearance_id"], str(primary.pk))
        self.assertEqual((value["identity_anchor"], value["appearance_anchor"]), ("", ""))
        self.assertFalse(ConsistencyLock.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        empty = SeriesProject.objects.create(owner=self.owner, title="Empty", language="English",
            aspect_ratio="9:16", width=720, height=1280, fps=24, default_shot_duration_ms=1000, continuity_seed=5)
        with CaptureQueriesContext(connection) as queries:
            result = capture_consistency_snapshot(empty)
        self.assertEqual([result[key] for key in ("characters", "scenes", "props", "locks")], [[], [], [], []])
        self.assertFalse(any('"studio_biblevariant' in query["sql"] for query in queries))

    def test_snapshot_and_lock_reference_share_exact_content_without_copying_bodies(self):
        primaries = {kind: self.primary(kind) for kind in self.entities}
        snapshot = capture_consistency_snapshot(self.project)
        with CaptureQueriesContext(connection) as queries:
            sync_consistency_locks(self.project)
        for query in queries:
            self.assertNotIn('"visual_identity"', query["sql"])
        for kind, section in (("character", "characters"), ("scene", "scenes"), ("prop", "props")):
            setting = primaries[kind].revisions.get(revision=1)
            content = snapshot[section][0]["primary_setting"]
            reference = primary_setting_content_reference(setting)
            self.assertEqual({key: content[key] for key in reference}, reference)
            self.assertEqual(content["identity_anchor"], f"identity {kind}")
            self.assertEqual(content["appearance_anchor"], f"appearance {kind}")
            value = self.identity_lock(kind).locked_value
            self.assertEqual(value["primary_setting_ref"], reference)
            for key in ("visual_identity", "identity_anchor", "appearance_anchor"):
                self.assertNotIn(key, value)
                self.assertNotIn(key, value["primary_setting_ref"])

    def test_sync_is_idempotent_and_revision_change_is_explicit_not_a_read_side_repair(self):
        primary = self.primary("character")
        original = primary.revisions.get(revision=1)
        sync_consistency_locks(self.project)
        frozen = capture_consistency_snapshot(self.project)
        frozen_copy = deepcopy(frozen)
        before = self.lock_values()
        sync_consistency_locks(self.project)
        self.assertEqual(self.lock_values(), before)
        revise_variant(actor=self.owner, variant=primary, expected_revision=1,
            value={**setting_payload(original), "appearance_anchor": "Different outfit"})
        fresh = capture_consistency_snapshot(self.project)
        self.assertEqual(fresh["characters"][0]["primary_setting"]["revision"], 2)
        self.assertEqual(self.lock_values(), before)  # GET did not resync the old locks.
        sync_consistency_locks(self.project)
        changed = [row for row in self.lock_values() if row not in before]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["version"], 2)
        self.assertEqual(changed[0]["locked_value"]["primary_setting_ref"]["revision"], 2)
        self.assertEqual(frozen, frozen_copy)
        self.assertEqual(original.identity_anchor, "identity character")

    def test_review_does_not_change_content_identity_and_policy_is_still_separate(self):
        primary = self.primary("character")
        original = primary.revisions.get(revision=1)
        sync_consistency_locks(self.project)
        before = capture_consistency_snapshot(self.project)
        locks = self.lock_values()
        review_variant(actor=self.owner, variant=primary, expected_revision=1,
            expected_sha256=original.content_sha256, decision="approve", comment="Isolated test review")
        self.assertEqual(capture_consistency_snapshot(self.project), before)
        sync_consistency_locks(self.project)
        self.assertEqual(self.lock_values(), locks)
        policy = capture_consistency_snapshot(self.project)["characters"][0]
        self.assertTrue(policy["visual_reference_policy"]["eligible"])
        self.assertEqual(policy["primary_setting"], before["characters"][0]["primary_setting"])
        CharacterBible.objects.filter(pk=self.entities["character"].pk).update(metadata={
            "visual_reference_policy": {"schema_version": 1, "mode": "exclude"}})
        self.assertFalse(capture_consistency_snapshot(self.project)["characters"][0]["visual_reference_policy"]["eligible"])
        sync_consistency_locks(self.project)
        self.assertEqual(self.lock_values(), locks)

    def test_broken_pointer_or_body_fails_without_partial_lock_writes(self):
        primary = self.primary("prop")
        sync_consistency_locks(self.project)
        before = self.lock_values()
        for changes in ({"revision": 999}, {"revision": 1, "name": "Broken name"}):
            BibleVariant.objects.filter(pk=primary.pk).update(**changes)
            for action in (capture_consistency_snapshot, sync_consistency_locks):
                with self.subTest(changes=changes, action=action.__name__), self.assertRaises(BibleVariantConflict):
                    action(self.project)
                self.assertEqual(self.lock_values(), before)
        BibleVariant.objects.filter(pk=primary.pk).update(revision=1, name="Primary")
        # Deliberate corruption in the isolated DB, bypassing append-only protection.
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_biblevariantrevision SET identity_anchor = %s WHERE variant_id = %s",
                ["Corrupted body", primary.pk.hex])
        with self.assertRaises(BibleVariantConflict):
            sync_consistency_locks(self.project)
        self.assertEqual(self.lock_values(), before)

    def test_failed_lock_write_rolls_back_prior_updates_and_deactivation(self):
        sync_consistency_locks(self.project)
        obsolete = ConsistencyLock.objects.create(project=self.project, scope="style", lock_key="old", locked_value={})
        before = self.lock_values()
        SeriesProject.objects.filter(pk=self.project.pk).update(width=800)
        SceneBible.objects.filter(pk=self.entities["scene"].pk).update(lighting="Changed light")
        save = ConsistencyLock.save

        def fail_scene(lock, *args, **kwargs):
            if lock.scope == "scene":
                raise RuntimeError("isolated lock write failure")
            return save(lock, *args, **kwargs)

        with patch.object(ConsistencyLock, "save", fail_scene), self.assertRaisesMessage(RuntimeError, "isolated lock"):
            sync_consistency_locks(self.project)
        self.assertEqual(self.lock_values(), before)
        obsolete.refresh_from_db()
        self.assertTrue(obsolete.is_active)

    def test_three_entity_kinds_batch_settings_and_do_not_read_unrelated_project_bodies(self):
        primaries = [self.primary(kind) for kind in self.entities]
        foreign = SeriesProject.objects.create(owner=self.other, title="Other project", language="English",
            aspect_ratio="9:16", width=720, height=1280, fps=24, default_shot_duration_ms=1000, continuity_seed=5)
        entity = PropBible.objects.create(project=foreign, slug="foreign", name="Foreign")
        other_primary = create_variant(actor=self.other, project_id=foreign.pk, entity_kind="prop", entity_id=entity.pk,
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="", value={
                "name": "Foreign", "reference_name": "Foreign primary", "identity_anchor": "Foreign identity",
                "appearance_anchor": "Foreign appearance", "basis_asset_id": None, "basis_asset_sha256": ""})
        BibleVariant.objects.filter(pk=other_primary.pk).update(revision=999)
        with CaptureQueriesContext(connection) as queries:
            snapshot = capture_consistency_snapshot(self.project)
        for query in queries:
            self.assertNotIn('"visual_identity"', query["sql"])
        self.assertNotIn("Foreign", str(snapshot))
        pointer = [q["sql"] for q in queries if 'FROM "studio_biblevariant"' in q["sql"]]
        body = [q["sql"] for q in queries if 'FROM "studio_biblevariantrevision"' in q["sql"]]
        self.assertEqual(len(pointer), 1)
        self.assertEqual(len(body), 1)
        self.assertNotIn('"visual_identity"', body[0])
        self.assertNotIn('"metadata"', body[0])
        self.assertNotIn('"studio_scriptrevision"."content"', body[0])
        self.assertIn(self.project.pk.hex, pointer[0].replace("-", ""))
        for primary in primaries:
            self.assertIn(primary.pk.hex, body[0].replace("-", ""))
        self.assertNotIn(other_primary.pk.hex, body[0].replace("-", ""))
        for sql in pointer + body:
            if connection.vendor == "sqlite":
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + sql)
                    plan = " ".join(str(row[-1]) for row in cursor.fetchall()).upper()
                    self.assertIn("SEARCH", plan)
                    self.assertNotIn("SCAN STUDIO_", plan)

    def test_stored_old_lock_is_not_used_as_setting_or_silently_rewritten_on_read(self):
        old = ConsistencyLock.objects.create(project=self.project, scope="prop", target_id=self.entities["prop"].pk,
            lock_key="identity", locked_value={"visual_identity": "Previously saved lock"})
        snapshot = capture_consistency_snapshot(self.project)
        self.assertIsNone(snapshot["props"][0]["primary_setting"])
        self.assertEqual(snapshot["locks"][0]["value"], old.locked_value)
        old.refresh_from_db()
        self.assertEqual(old.version, 1)
        sync_consistency_locks(self.project)
        old.refresh_from_db()
        self.assertEqual(old.version, 2)
        self.assertNotIn("visual_identity", old.locked_value)
        self.assertIsNone(old.locked_value["primary_setting_ref"])
        self.assertEqual(snapshot["locks"][0]["value"], {"visual_identity": "Previously saved lock"})

    def test_original_reference_slots_and_absent_primary_locks_stay_stable(self):
        character = self.entities["character"]
        anchor = Asset.objects.create(owner=self.owner, name="Isolated anchor", kind="image")
        extra = Asset.objects.create(owner=self.owner, name="Isolated reference", kind="image")
        character.anchor_asset = anchor
        character.save(update_fields=["anchor_asset"])
        character.reference_assets.add(extra)
        sync_consistency_locks(self.project)
        slot_lock = self.project.consistency_locks.get(lock_key="reference_slots")
        slots = deepcopy(slot_lock.locked_value)
        self.assertEqual(slots["subject"][f"character:{character.pk}"]["picture_asset_id"], str(anchor.pk))
        self.assertEqual(set(slots["picture"]), {str(anchor.pk), str(extra.pk)})
        self.assertIsNone(self.identity_lock("character").locked_value["primary_setting_ref"])
        self.primary("character")
        sync_consistency_locks(self.project)
        slot_lock.refresh_from_db()
        self.assertEqual(slot_lock.locked_value, slots)
        self.assertEqual(slot_lock.version, 1)
        snapshot = capture_consistency_snapshot(self.project)["characters"][0]
        self.assertEqual(snapshot["anchor_asset"]["id"], str(anchor.pk))
        self.assertEqual([row["id"] for row in snapshot["reference_assets"]], [str(extra.pk)])

    def test_per_use_material_choices_do_not_allocate_global_picture_slots(self):
        sync_consistency_locks(self.project)
        lock = self.project.consistency_locks.get(lock_key="reference_slots")
        before = (deepcopy(lock.locked_value), lock.version)
        episode = Episode.objects.create(project=self.project, number=1, title="Fixture")
        shot = Shot.objects.create(episode=episode, number=1, duration_ms=1000, media_type="image", include_audio=False)
        asset = Asset.objects.create(owner=self.owner, name="Storage-only use fixture", kind="reference", checksum="a" * 64)
        use = ShotReferenceUse.objects.create(shot=shot, use_key="image", position=0, media_kind="image",
            source_mode="existing", asset=asset, asset_sha256=asset.checksum)
        original_use = ShotReferenceUse.objects.values().get(pk=use.pk)
        with CaptureQueriesContext(connection) as queries:
            sync_consistency_locks(self.project)
        lock.refresh_from_db()
        self.assertEqual((lock.locked_value, lock.version), before)
        self.assertEqual(ShotReferenceUse.objects.values().get(pk=use.pk), original_use)
        self.assertFalse(any('studio_shot_reference_assets' in row['sql'] or 'studio_shotreferenceuse' in row['sql'] for row in queries))

    def test_existing_http_actions_return_primary_contract_and_permission_safe_errors(self):
        primary = self.primary("character")
        url = f"/api/v1/projects/{self.project.pk}/"
        response = self.client.get(url + "continuity/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["characters"][0]["primary_setting"]["appearance_id"], str(primary.pk))
        contract = ConsistencySnapshotSerializer(data=response.data)
        self.assertTrue(contract.is_valid(), contract.errors)
        self.assertFalse(ConsistencyLock.objects.exists())
        response = self.client.post(url + "sync-locks/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        before = self.lock_values()
        BibleVariant.objects.filter(pk=primary.pk).update(revision=999)
        for suffix, method in (("continuity/", self.client.get), ("sync-locks/", self.client.post)):
            response = method(url + suffix)
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(response.data["error"]["code"], "bible_variant_conflict")
        self.assertEqual(self.lock_values(), before)
        self.client.force_authenticate(self.other)
        for suffix, method in (("continuity/", self.client.get), ("sync-locks/", self.client.post)):
            response = method(url + suffix)
            self.assertEqual(response.status_code, 404, response.data)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(url + "continuity/").status_code, (401, 403))
        self.assertEqual(self.lock_values(), before)
