"""Saved M2 structures -> original Bible writer, isolated DB and no provider."""
from copy import deepcopy
from hashlib import sha256
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.exceptions import NotFound
from rest_framework.test import APIClient

from studio.models import (BibleEntityIdentity, BibleVariant, BibleVariantRevision, BibleVariantReview, CharacterBible,
    GeneratedArtifact, GenerationAttempt, SceneBible, ScriptBibleEntityBinding, ScriptBibleAppearanceBinding, ScriptRevisionStructure)
from studio.services.bible_variants import BibleVariantConflict, revise_variant, setting_payload, verify_setting
from studio.services.script_bible_materialization import freeze_script_reference_bible, materialize_script_reference_bible
from studio.tests.factories import build_story
from studio.tests.structured_bible_fixtures import confirmed_structured_bible_source, entities_fixture


class ScriptReferenceMaterializationTests(TestCase):
    def setUp(self):
        self.actor, self.project, *_ = build_story("decl-writer")
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.declarations = {"entities": entities_fixture()}
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("No provider")))
        self.enterContext(patch("studio.services.provider_registry.resolve_credential", side_effect=AssertionError("No credentials")))
        self.revision = self.source_revision()

    def source_revision(self):
        return confirmed_structured_bible_source(self, entities=self.declarations["entities"])

    def frozen(self):
        return freeze_script_reference_bible(revision=self.revision, project_id=self.project.pk)

    def apply(self, **changes):
        return materialize_script_reference_bible(**{"actor": self.actor, "project_id": self.project.pk,
            "script_revision_id": self.revision.pk, "expected_source": self.frozen(), **changes})

    def manual_primary_before_appearance_declarations(self, entity_key="person_a"):
        authored = deepcopy(self.declarations["entities"])
        self.declarations["entities"] = [{key: row[key] for key in ("key", "kind", "name")} for row in authored]
        self.revision = self.source_revision()
        self.apply()
        entity = next(row for row in authored if row["key"] == entity_key)
        identity = BibleEntityIdentity.objects.get(project=self.project, entity_key=entity_key)
        response = self.client.post("/api/v1/bible-variants/", {
            "project_id": str(self.project.pk), "entity_kind": entity["kind"],
            "entity_id": str(getattr(identity, f"{entity['kind']}_id")), "appearance_role": "primary",
            "primary_setting_revision_id": None, "primary_setting_sha256": "",
            "name": "人工主图", "reference_name": f"人工主图·{entity_key}", "identity_anchor": "人工登记的共同身份",
            "appearance_anchor": "人工登记的基础外观", "basis_asset_id": None, "basis_asset_sha256": "",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.declarations["entities"] = authored
        self.revision = self.source_revision()
        return BibleVariant.objects.get(pk=response.data["id"])

    def test_new_declaration_reuses_manual_primary_without_relabeling_or_overwriting_it(self):
        primary = self.manual_primary_before_appearance_declarations()
        original = primary.revisions.get(revision=1)
        result = self.apply()
        person = next(row for row in result["entities"] if row["entity_key"] == "person_a")
        self.assertEqual(person["settings"][0]["setting_revision_id"], str(original.pk))
        self.assertEqual(BibleVariant.objects.count(), 4)
        primary.refresh_from_db()
        self.assertEqual(primary.revision, 1)
        self.assertIsNone(primary.source_binding_id)
        self.assertEqual((primary.name, primary.source_variant_key), ("人工主图", ""))
        self.assertEqual(primary.revisions.get().content_sha256, original.content_sha256)
        self.assertEqual(primary.revisions.get().source_declaration_sha256, "")
        children = BibleVariantRevision.objects.filter(variant__character_id=primary.character_id,
            variant__appearance_role="variant")
        self.assertEqual(children.count(), 2)
        self.assertTrue(all(row.primary_setting_revision_id == original.pk and row.identity_anchor == "" for row in children))
        self.assertEqual(self.apply(), result)
        self.assertFalse(BibleVariantReview.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        # Later explicit edits and another source cannot change the first receipt.
        first_source = self.revision
        revise_variant(actor=self.actor, variant=primary, expected_revision=1,
            value={**setting_payload(original), "identity_anchor": "人工新修订"})
        self.assertEqual(self.apply(), result)
        self.revision = self.source_revision()
        second = self.apply()
        next_person = next(row for row in second["entities"] if row["entity_key"] == "person_a")
        self.assertEqual(next_person["settings"][0]["setting_revision_id"], str(primary.revisions.get(revision=2).pk))
        self.revision = first_source
        self.assertEqual(self.apply(), result)

    def test_manual_primary_reuse_applies_to_scene_without_name_matching(self):
        primary = self.manual_primary_before_appearance_declarations("station")
        result = self.apply()
        entity = next(row for row in result["entities"] if row["entity_key"] == "station")
        self.assertEqual(entity["settings"][0]["appearance_id"], str(primary.pk))
        self.assertIsNone(primary.source_binding_id)

    def test_manual_primary_reuse_applies_to_prop_without_name_matching(self):
        self.declarations["entities"].append({"key": "key_prop", "kind": "prop", "name": "同名对象",
            "primary": {"reference_name": "声明道具", "anchor": "旧描述"}, "variants": []})
        primary = self.manual_primary_before_appearance_declarations("key_prop")
        result = self.apply()
        entity = next(row for row in result["entities"] if row["entity_key"] == "key_prop")
        self.assertEqual(entity["settings"][0]["appearance_id"], str(primary.pk))
        self.assertIsNone(primary.source_binding_id)

    def test_manual_variant_with_matching_name_is_not_implicitly_declared_and_collision_rolls_back(self):
        from studio.services.bible_variants import create_variant
        primary = self.manual_primary_before_appearance_declarations()
        setting = primary.revisions.get()
        declared = self.declarations["entities"][0]["variants"][0]
        manual = create_variant(actor=self.actor, project_id=self.project.pk, entity_kind="character",
            entity_id=primary.character_id, appearance_role="variant", primary_setting_revision_id=setting.pk,
            primary_setting_sha256=setting.content_sha256, value={**setting_payload(setting), "identity_anchor": "",
                "name": declared["name"], "reference_name": declared["reference_name"]})
        counts = (ScriptBibleEntityBinding.objects.count(), ScriptBibleAppearanceBinding.objects.count(), BibleVariantRevision.objects.count())
        with self.assertRaises(BibleVariantConflict): self.apply()
        self.assertEqual(counts, (ScriptBibleEntityBinding.objects.count(), ScriptBibleAppearanceBinding.objects.count(), BibleVariantRevision.objects.count()))
        manual.refresh_from_db()
        self.assertIsNone(manual.source_binding_id)
        self.assertEqual(manual.source_variant_key, "")

    def test_manual_primary_missing_current_revision_is_not_recreated_from_declaration(self):
        primary = self.manual_primary_before_appearance_declarations()
        counts = (ScriptBibleEntityBinding.objects.count(), ScriptBibleAppearanceBinding.objects.count(),
            BibleVariant.objects.count(), BibleVariantRevision.objects.count())
        with patch("studio.services.script_bible_materialization.setting_revision_rows", return_value=BibleVariantRevision.objects.none()):
            with self.assertRaisesMessage(BibleVariantConflict, "缺少当前确切修订"):
                self.apply()
        self.assertEqual(counts, (ScriptBibleEntityBinding.objects.count(), ScriptBibleAppearanceBinding.objects.count(),
            BibleVariant.objects.count(), BibleVariantRevision.objects.count()))
        primary.refresh_from_db()
        self.assertIsNone(primary.source_binding_id)
        self.assertEqual(primary.revision, 1)

    def test_manual_primary_selection_is_batched_and_project_indexed(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.manual_primary_before_appearance_declarations()
        with CaptureQueriesContext(connection) as queries:
            self.apply()
        lookup = [row["sql"] for row in queries if row["sql"].startswith(
            'SELECT "studio_biblevariant"."id"') and ' FROM "studio_biblevariant" WHERE ' in row["sql"]]
        self.assertEqual(len(lookup), 1, [row["sql"] for row in queries])
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + lookup[0])
                plan = "\n".join(str(row[-1]) for row in cursor.fetchall())
            self.assertIn("SEARCH studio_biblevariant USING INDEX", plan)
            self.assertNotIn("SCAN studio_biblevariant", plan)

    def test_read_only_receipt_and_source_reader_failure_never_create(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries, patch(
                "studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError("no reparse")):
            receipt = self.frozen()
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for row in queries))
        self.assertNotIn("entities", receipt)
        self.assertEqual(len(receipt["declaration_sha256"]), 64)
        ScriptRevisionStructure.objects.filter(revision=self.revision).update(document_sha256="0" * 64)
        with self.assertRaises(ValidationError): self.apply(expected_source=receipt)
        self.assertFalse(ScriptBibleEntityBinding.objects.exists())

    def test_primary_and_siblings_have_exact_same_dependency_without_images_or_review(self):
        result = self.apply()
        binding = ScriptBibleEntityBinding.objects.get(entity_key="person_a")
        primary = BibleVariantRevision.objects.get(variant__source_binding=binding, variant__appearance_role="primary")
        siblings = list(BibleVariantRevision.objects.filter(variant__source_binding=binding, variant__appearance_role="variant"))
        self.assertEqual(len(result["entities"]), 2)
        self.assertEqual(len(siblings), 2)
        for item in siblings:
            self.assertEqual(item.primary_setting_revision_id, primary.pk)
            self.assertEqual(item.identity_anchor, "")
            self.assertIsNone(item.basis_asset_id)
            self.assertEqual(verify_setting(item).pk, item.pk)
        self.assertIsNone(primary.primary_setting_revision_id)
        self.assertEqual(primary.identity_anchor, "黑色短发")
        self.assertEqual(BibleVariant.objects.count(), 4)
        self.assertEqual(BibleVariantReview.objects.count(), 0)
        self.assertEqual(GenerationAttempt.objects.count(), 0)
        self.assertEqual(GeneratedArtifact.objects.count(), 0)
        self.assertIsNone(binding.character.anchor_asset_id)
        self.assertFalse(binding.character.is_locked)

    def test_alternative_layout_preserves_saved_fields_and_rejects_changed_structure_receipt(self):
        self.revision = confirmed_structured_bible_source(self, layout="b")
        row = ScriptRevisionStructure.objects.get(revision=self.revision)
        original = deepcopy(row.document)
        frozen = self.frozen()
        self.assertEqual(frozen["structure_source"]["document_sha256"], row.document_sha256)
        self.assertEqual(frozen["schema_version"], 3)
        wrong = deepcopy(frozen["structure_source"])
        wrong["configuration_sha256"] = "0" * 64
        with self.assertRaises(ValidationError):
            freeze_script_reference_bible(revision=self.revision, project_id=self.project.pk, expected_structure_source=wrong)
        with patch("studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError("no parse")):
            self.apply()
        row.refresh_from_db()
        self.assertEqual(row.document, original)
        self.assertEqual(original["projection"]["source_value"]["额外创作"], {"zero": 0, "enabled": False, "note": None})

    def test_renamed_entity_primary_and_variant_fields_use_original_writer(self):
        self.revision = confirmed_structured_bible_source(self, layout="renamed")
        source = ScriptRevisionStructure.objects.get(revision=self.revision)
        self.assertEqual(source.document["projection"]["value"]["entities"], entities_fixture())
        before = deepcopy(source.document)
        with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                side_effect=AssertionError("saved source must not be reparsed")):
            result = self.apply()
            self.assertEqual(self.apply(), result)
        binding = ScriptBibleEntityBinding.objects.get(script_revision=self.revision, entity_key="person_a")
        self.assertEqual((binding.character.name, binding.character.description), ("海遥", "车站工作人员"))
        settings = {setting.variant.source_variant_key: setting for setting in BibleVariantRevision.objects.filter(
            variant__source_binding=binding).select_related("variant")}
        self.assertEqual(set(settings), {"", "uniform", "raincoat"})
        self.assertEqual(settings[""].identity_anchor, "黑色短发")
        self.assertEqual(settings[""].appearance_anchor, "制服")
        self.assertEqual(settings["raincoat"].appearance_anchor, "雨夜披着雨衣")
        self.assertEqual(settings["raincoat"].primary_setting_revision_id, settings[""].pk)
        self.assertTrue(all(setting.basis_asset_id is None for setting in settings.values()))
        source.refresh_from_db()
        self.assertEqual(source.document, before)

    def test_absent_optional_text_and_appearances_are_not_invented(self):
        entities = [{"key": "minimal", "kind": "prop", "name": "未设外观的道具"}]
        self.revision = confirmed_structured_bible_source(self, entities=entities)
        first = self.apply()
        self.assertEqual(first["entities"][0]["settings"], [])
        self.assertEqual(self.apply(), first)
        self.assertFalse(BibleVariant.objects.exists())
        binding = ScriptBibleEntityBinding.objects.get()
        self.assertEqual(binding.prop.description, "")
        self.assertEqual(ScriptRevisionStructure.objects.get(revision=self.revision).document["projection"]["value"]["entities"], entities)

    def test_configured_multilingual_initial_names_and_manual_renames_keep_identity_and_source(self):
        from studio.models import PromptFieldTranslation
        entities = entities_fixture()
        entities[0]["name"] = "海遥 Éclaireur"
        entities[1]["name"] = "東京駅"
        entities.append({"key": "lantern", "kind": "prop", "name": "مصباح",
            "description": "旧灯", "identity_anchor": "黄铜提灯",
            "primary": {"reference_name": "提灯·基準", "anchor": "灯罩完整"}, "variants": []})
        identities = {}
        originals = []
        for layout in ("a", "renamed"):
            self.revision = confirmed_structured_bible_source(self, entities=entities, layout=layout)
            source = ScriptRevisionStructure.objects.get(revision=self.revision)
            originals.append((source, deepcopy(source.document)))
            self.apply()
            for entity in entities:
                binding = ScriptBibleEntityBinding.objects.get(script_revision=self.revision, entity_key=entity["key"])
                obj = getattr(binding, entity["kind"])
                self.assertEqual(obj.name, entity["name"])
                self.assertEqual(identities.setdefault(entity["key"], obj.pk), obj.pk)
        self.project.refresh_from_db()
        original_script = self.project.master_script
        for entity in entities:
            entity_id = identities[entity["key"]]
            name = f"人工·{entity['name']}"
            response = self.client.patch(f"/api/v1/{entity['kind']}s/{entity_id}/", {"name": name}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["name"], name)
            identity = BibleEntityIdentity.objects.get(project=self.project, entity_key=entity["key"])
            self.assertEqual(getattr(identity, f"{entity['kind']}_id"), entity_id)
            primary = BibleVariant.objects.get(project=self.project, appearance_role="primary",
                **{f"{entity['kind']}_id": entity_id})
            old = primary.revisions.get(revision=1)
            response = self.client.post(f"/api/v1/bible-variants/{primary.pk}/revise/", {
                **setting_payload(old), "expected_revision": 1, "name": f"基準·{entity['key']}",
                "reference_name": f"Référence·{entity['key']}"}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            primary.refresh_from_db()
            self.assertEqual(primary.revision, 2)
            self.assertEqual(primary.reference_name, f"Référence·{entity['key']}")
            old.refresh_from_db()
            self.assertEqual(old.reference_name, entity["primary"]["reference_name"])
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, original_script)
        # A later configured source is not a rename or global-replacement command.
        self.revision = confirmed_structured_bible_source(self, entities=entities, layout="renamed")
        self.apply()
        for entity in entities:
            identity = BibleEntityIdentity.objects.get(project=self.project, entity_key=entity["key"])
            self.assertEqual(getattr(identity, entity["kind"]).name, f"人工·{entity['name']}")
        for source, original in originals:
            source.refresh_from_db()
            self.assertEqual(source.document, original)
        self.project.refresh_from_db()
        # No name operation rewrites the authoritative script or its dialogue.
        # The fixture's explicit imports above create revisions but are not model calls.
        self.assertEqual(self.project.master_script, original_script)
        self.assertFalse(PromptFieldTranslation.objects.filter(project=self.project).exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_missing_primary_dependency_or_reference_name_is_explicit_and_atomic(self):
        for missing in ("primary", "reference_name"):
            entities = entities_fixture()
            if missing == "primary":
                del entities[0]["primary"]
            else:
                del entities[0]["primary"]["reference_name"]
            self.revision = confirmed_structured_bible_source(self, entities=entities)
            with self.assertRaises(ValidationError):
                self.apply()
            self.assertFalse(ScriptBibleEntityBinding.objects.exists())
            self.assertFalse(BibleVariant.objects.exists())

    def test_replay_reuses_exact_initial_revisions_and_does_not_restore_edited_facts(self):
        first = self.apply()
        variant = BibleVariant.objects.get(source_variant_key="uniform")
        original = variant.revisions.get(revision=1)
        revise_variant(actor=self.actor, variant=variant, expected_revision=1,
            value={**setting_payload(original), "appearance_anchor": "人工修订雨衣"})
        binding = variant.source_binding
        binding.character.name = "人工更名"
        binding.character.save(update_fields=("name",))
        self.assertEqual(self.apply(), first)
        variant.refresh_from_db()
        binding.character.refresh_from_db()
        self.assertEqual(variant.revision, 2)
        self.assertEqual(binding.character.name, "人工更名")
        latest = verify_setting(variant.revisions.get(revision=2))
        self.assertEqual(latest.identity_anchor, "")
        self.assertEqual(latest.appearance_anchor, "人工修订雨衣")
        self.assertEqual(latest.primary_setting_revision_id, original.primary_setting_revision_id)
        self.assertEqual(latest.source_declaration_sha256, "")  # Human edit is not the authored declaration.
        self.assertEqual(BibleVariantRevision.objects.count(), 5)

    def test_frozen_source_mismatch_rolls_back_without_using_current_project_script(self):
        frozen = self.frozen()
        frozen["declaration_sha256"] = "0" * 64
        with self.assertRaises(BibleVariantConflict): self.apply(expected_source=frozen)
        self.assertFalse(ScriptBibleEntityBinding.objects.exists())

    def test_name_collision_rolls_back_whole_batch_not_merge_by_name(self):
        from studio.services.bible_variants import create_variant
        create_variant(actor=self.actor, project_id=self.project.pk, entity_kind="scene",
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="",
            entity_id=self.project.scenes.first().pk, value={"name": "已有设定", "reference_name": "車站ロビー",
                "identity_anchor": "不属于声明的旧图", "appearance_anchor": "另一场景", "basis_asset_id": None,
                "basis_asset_sha256": ""})
        counts = (CharacterBible.objects.count(), SceneBible.objects.count(), BibleVariantRevision.objects.count())
        with self.assertRaises(BibleVariantConflict): self.apply()
        self.assertEqual(counts, (CharacterBible.objects.count(), SceneBible.objects.count(), BibleVariantRevision.objects.count()))
        self.assertFalse(ScriptBibleEntityBinding.objects.exists())

    def test_same_project_keys_in_different_revisions_reuse_entities_and_settings(self):
        first = self.apply()
        for entity in self.declarations["entities"]:
            entity["primary"]["reference_name"] += " · autre"
            for variant in entity["variants"]: variant["reference_name"] += " · autre"
        self.revision = self.source_revision()
        second = self.apply()
        self.assertEqual(first["entities"][0]["entity_id"], second["entities"][0]["entity_id"])
        self.assertNotEqual(first["entities"][0]["binding_id"], second["entities"][0]["binding_id"])
        self.assertEqual(first["entities"][0]["settings"], second["entities"][0]["settings"])
        self.assertEqual(ScriptBibleEntityBinding.objects.filter(entity_key="person_a").count(), 2)
        self.assertEqual(BibleEntityIdentity.objects.count(), 2)
        self.assertEqual(BibleVariant.objects.count(), 4)
        self.assertEqual(ScriptBibleAppearanceBinding.objects.count(), 8)
        self.assertEqual(second, self.apply())

    def test_new_source_preserves_manual_content_and_pins_its_own_exact_setting_use(self):
        first = self.apply()
        first_revision = self.revision
        appearance = BibleVariant.objects.get(source_variant_key="uniform")
        initial = appearance.revisions.get(revision=1)
        revise_variant(actor=self.actor, variant=appearance, expected_revision=1,
            value={**setting_payload(initial), "appearance_anchor": "人工蓝衣"})
        self.declarations["entities"][0]["variants"][0]["anchor"] = "新剧本提出红衣"
        self.revision = self.source_revision()
        second_revision = self.revision
        second = self.apply()
        second_settings = {row["variant_key"]: row for row in second["entities"][0]["settings"]}
        manual = appearance.revisions.get(revision=2)
        self.assertEqual(second_settings["uniform"]["setting_revision_id"], str(manual.pk))
        self.assertEqual(manual.appearance_anchor, "人工蓝衣")
        self.assertEqual(manual.source_declaration_sha256, "")
        revise_variant(actor=self.actor, variant=appearance, expected_revision=2,
            value={**setting_payload(manual), "appearance_anchor": "更晚的人工绿衣"})
        self.assertEqual(self.apply(), second)
        self.revision = first_revision
        self.assertEqual(self.apply(), first)
        self.revision = second_revision
        self.assertEqual(self.apply(), second)
        self.assertEqual(BibleVariant.objects.count(), 4)

    def test_new_variant_uses_same_entity_current_primary_without_changing_old_looks(self):
        self.apply()
        primary = BibleVariant.objects.get(source_binding__entity_key="person_a", appearance_role="primary")
        initial = primary.revisions.get(revision=1)
        revise_variant(actor=self.actor, variant=primary, expected_revision=1,
            value={**setting_payload(initial), "identity_anchor": "人工补充共同身份"})
        self.declarations["entities"][0]["variants"].append({"key": "coat", "name": "外套",
            "reference_name": "海遥·外套", "anchor": "穿外套"})
        self.revision = self.source_revision()
        result = self.apply()
        coat = BibleVariantRevision.objects.get(variant__source_variant_key="coat")
        self.assertEqual(coat.primary_setting_revision_id, primary.revisions.get(revision=2).pk)
        self.assertEqual(coat.variant.character_id, primary.character_id)
        self.assertEqual(BibleVariantRevision.objects.get(variant__source_variant_key="uniform").primary_setting_revision_id, initial.pk)
        self.assertEqual(self.apply(), result)

    def test_existing_project_key_cannot_change_entity_kind_and_transaction_rolls_back(self):
        self.apply()
        before = (BibleEntityIdentity.objects.count(), ScriptBibleEntityBinding.objects.count(), BibleVariant.objects.count())
        self.declarations["entities"][0]["kind"] = "prop"
        self.revision = self.source_revision()
        with self.assertRaises(BibleVariantConflict): self.apply()
        self.assertEqual(before, (BibleEntityIdentity.objects.count(), ScriptBibleEntityBinding.objects.count(), BibleVariant.objects.count()))

    def test_same_name_same_kind_different_declared_keys_create_distinct_entities(self):
        other = deepcopy(self.declarations["entities"][0])
        other["key"] = "person_b"
        other["primary"]["reference_name"] += " B"
        for variant in other["variants"]:
            variant["reference_name"] += " B"
        self.declarations["entities"].append(other)
        self.revision = self.source_revision()
        result = self.apply()
        ids = {row["entity_key"]: row["entity_id"] for row in result["entities"]}
        self.assertNotEqual(ids["person_a"], ids["person_b"])
        self.assertEqual(CharacterBible.objects.filter(project=self.project, name=other["name"]).count(), 2)
        self.assertEqual(self.apply(), result)

    def test_same_declared_key_in_another_project_does_not_reuse_entity(self):
        first = self.apply()
        original_project = self.project
        _, self.project, *_ = build_story("project-key-other")
        self.project.owner = self.actor
        self.project.save(update_fields=("owner",))
        self.revision = self.source_revision()
        second = self.apply()
        self.assertNotEqual(first["entities"][0]["entity_id"], second["entities"][0]["entity_id"])
        self.assertEqual(BibleEntityIdentity.objects.filter(project=original_project).count(), 2)
        self.assertEqual(BibleEntityIdentity.objects.filter(project=self.project).count(), 2)

    def test_unconfirmed_cross_project_and_unauthorized_sources_rejected(self):
        frozen = self.frozen()
        other, other_project, *_ = build_story("decl-other")
        with self.assertRaises(NotFound): self.apply(actor=other)
        with self.assertRaises(ValidationError):
            self.apply(project_id=other_project.pk, actor=other, expected_source=frozen)
        self.revision.status = "preview"
        self.revision.save(update_fields=("status",))
        with self.assertRaises(ValidationError): self.apply(expected_source=frozen)
        self.assertFalse(ScriptBibleEntityBinding.objects.exists())

    def test_explicit_optional_anchors_and_zero_variants_are_not_filled(self):
        for entity in self.declarations["entities"]:
            entity["identity_anchor"] = ""
            entity["primary"]["anchor"] = ""
            entity["variants"] = []
        self.revision = self.source_revision()
        self.apply()
        self.assertEqual(BibleVariant.objects.count(), 2)
        self.assertFalse(BibleVariantRevision.objects.exclude(identity_anchor="", appearance_anchor="").exists())

    def test_database_source_uniqueness_target_exclusivity_and_primary_uniqueness(self):
        self.apply()
        binding = ScriptBibleEntityBinding.objects.get(entity_key="person_a")
        for change in ({}, {"entity_key": "another"}, {"entity_key": "another", "scene": self.project.scenes.first()}):
            with self.subTest(change=change), self.assertRaises(IntegrityError), transaction.atomic():
                ScriptBibleEntityBinding.objects.create(script_revision=self.revision, **{
                    "entity_key": "person_a", "character": binding.character, "declaration_sha256": "a" * 64, **change})
        primary = binding.appearances.get(appearance_role="primary")
        with self.assertRaises(IntegrityError), transaction.atomic():
            BibleVariant.objects.create(project=self.project, character=binding.character, appearance_role="primary",
                name="重复主图", reference_name="重复主图", reference_key="other", created_by=self.actor)
        primary.appearance_role = "variant"
        with self.assertRaises(ValidationError): primary.save()
        with self.assertRaises(ValidationError): binding.save()
        with self.assertRaises(ValidationError): ScriptBibleEntityBinding.objects.filter(pk=binding.pk).delete()

    def test_cross_entity_or_sibling_dependencies_fail_closed(self):
        self.apply()
        setting = BibleVariantRevision.objects.get(variant__source_variant_key="uniform")
        for invalid in (BibleVariantRevision.objects.get(variant__scene__isnull=False),
                        BibleVariantRevision.objects.get(variant__source_variant_key="raincoat")):
            setting.primary_setting_revision = invalid
            with self.assertRaises(BibleVariantConflict): verify_setting(setting)

    def test_partial_binding_fails_explicitly(self):
        self.apply()
        # Simulate a partial read without rewriting an immutable source.
        with patch("studio.services.script_bible_materialization._declarations",
                return_value=self.declarations["entities"][:1]):
            with self.assertRaises(BibleVariantConflict): self.apply()

    def test_source_key_lookup_uses_unique_index(self):
        self.apply()
        plan = ScriptBibleEntityBinding.objects.filter(script_revision=self.revision, entity_key="person_a").explain()
        self.assertIn("INDEX", plan.upper())
        self.assertNotIn("SCAN STUDIO_SCRIPTBIBLEENTITYBINDING", plan.upper())

    def test_declared_variant_can_edit_only_appearance_via_original_api(self):
        self.apply()
        variant = BibleVariant.objects.get(source_variant_key="uniform")
        original = variant.revisions.get(revision=1)
        response = self.client.post(f"/api/v1/bible-variants/{variant.pk}/revise/", {
            **setting_payload(original), "expected_revision": 1, "appearance_anchor": "只改外套"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        latest = variant.revisions.get(revision=2)
        self.assertEqual(latest.identity_anchor, "")
        self.assertEqual(latest.primary_setting_revision_id, original.primary_setting_revision_id)
        self.assertEqual(verify_setting(latest).pk, latest.pk)

    def test_bound_entities_cannot_move_projects_through_existing_api(self):
        _, target, *_ = build_story("decl-move")
        target.owner = self.actor
        target.save(update_fields=("owner",))
        self.apply()
        for binding in ScriptBibleEntityBinding.objects.all():
            kind = "character" if binding.character_id else "scene"
            obj = getattr(binding, kind)
            response = self.client.patch(f"/api/v1/{kind}s/{obj.pk}/", {"project": str(target.pk)}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
            obj.refresh_from_db()
            self.assertEqual(obj.project_id, self.project.pk)
        self.apply()

    def test_same_display_names_can_be_edited_without_becoming_identity_keys(self):
        self.declarations["entities"][1]["name"] = self.declarations["entities"][0]["name"]
        self.revision = self.source_revision()
        self.apply()
        for binding in ScriptBibleEntityBinding.objects.all():
            kind = "character" if binding.character_id else "scene"
            obj = getattr(binding, kind)
            response = self.client.patch(f"/api/v1/{kind}s/{obj.pk}/", {"description": "人工独立修改"}, format="json")
            self.assertEqual(response.status_code, 200, response.data)

    def test_replay_reads_are_batched_without_writes(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.apply()
        with CaptureQueriesContext(connection) as queries:
            self.apply()
        reads = [row["sql"] for row in queries if row["sql"].lstrip().upper().startswith("SELECT ")]
        self.assertLessEqual(len(reads), 20)
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for row in queries))

    def test_sequential_edits_can_clear_creative_identity_without_overwriting_previous_source(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from PIL import Image
        from studio.models import Asset
        self.apply()
        primary = BibleVariant.objects.get(source_binding__entity_key="person_a", appearance_role="primary")
        path = Path(self.enterContext(TemporaryDirectory())) / "identity.png"
        Image.new("RGB", (12, 12), "white").save(path)
        digest = sha256(path.read_bytes()).hexdigest()
        asset = Asset.objects.create(owner=self.actor, kind="character", name="identity", mime_type="image/png",
            local_path=str(path), checksum=digest)
        first = {**setting_payload(primary.revisions.get(revision=1)), "identity_anchor": "",
            "basis_asset_id": str(asset.pk), "basis_asset_sha256": digest, "expected_revision": 1}
        url = f"/api/v1/bible-variants/{primary.pk}/revise/"
        response = self.client.post(url, first, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.post(url, {**first, "expected_revision": 2,
            "basis_asset_id": None, "basis_asset_sha256": ""}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(primary.revisions.count(), 3)
        self.assertEqual(response.data["setting"]["identity_anchor"], "")
        self.assertIsNone(response.data["setting"]["basis_asset_id"])
        self.assertEqual(primary.revisions.get(revision=2).basis_asset_id, asset.pk)
        self.assertNotEqual(primary.revisions.get(revision=1).identity_anchor, "")

    def test_missing_original_revision_is_explicit_conflict_not_reconstruction(self):
        self.apply()
        with patch("studio.services.script_bible_materialization.setting_revision_rows", return_value=BibleVariantRevision.objects.none()):
            with self.assertRaises(BibleVariantConflict): self.apply()
