"""Mapping is deterministic, preserves review semantics and creates no calls."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.production_reference_mapping import (
    expand_reviewed_reference_sources, reference_use_records, validate_reference_mapping,
)


def spec(key="images", kind="image", *, parent=None, purposes=None, required=True, maximum=4):
    return {"key": key, "kind": kind, "required": required, "min_items": 1 if required else 0,
        "max_items": maximum, "semantic_contract": {"allowed_scope_kinds": ["shot"],
            "allowed_purposes": purposes or [], "allowed_subject_kinds": ["character", "scene", "prop"],
            "paired_with_input_key": parent}}


def mapping(*keys, purpose=None):
    return {"kind": "reviewed_references", "stage_key": "storyboard_review", "scope": {"kind": "shot"},
        "groups": [{"source_key": key, "purposes": None, "subject_kinds": None,
            **({"purpose": purpose} if purpose is not None else {})} for key in keys]}


def record(key, source_key, kind, asset_id, *, purpose=None, subject=None, parent=None):
    """Explicit mapper-protocol fixture; never derive uses from legacy Shot JSON."""
    return {"reference_key": key, "source_key": source_key, "kind": kind,
        "source": {"kind": "asset", "asset_id": asset_id}, "purpose": purpose,
        "subject_ref": subject, "paired_reference_key": parent}


class ReferenceMappingTests(SimpleTestCase):
    def test_preview_uses_execution_source_policy_without_losing_order_or_identity(self):
        from studio.services.production_recipe_media_preparations import _preview_media_sources
        from studio.tests.test_execution_media_source_policy import choices
        declared = spec()
        expanded, _ = self.expand([declared], {"images": mapping("character_images", "prop_images")})
        items = expanded["images"]["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(_preview_media_sources([declared], expanded), {"images": items})
        declared["media_source_policy"] = choices()
        wrapped = {"images": {"selection": {"source": "asset_library", "items": items}}}
        before = deepcopy(wrapped)
        self.assertEqual(_preview_media_sources([declared], wrapped), {"images": items})
        self.assertEqual(wrapped, before)
        replacement = deepcopy(items[1]); replacement["order"] = 0
        wrapped["images"]["override"] = {"source": "asset_library", "items": [replacement]}
        self.assertEqual(_preview_media_sources([declared], wrapped), {"images": [replacement]})
        declared["media_source_policy"]["allow_override"] = False
        with self.assertRaises(ValidationError):
            _preview_media_sources([declared], wrapped)

    def test_preview_policy_preserves_selected_outputs_and_rejects_source_mismatch(self):
        from studio.services.production_recipe_media_preparations import _preview_media_sources
        from studio.tests.test_execution_media_source_policy import choices
        declared = {**spec(), "media_source_policy": choices(default_source="selected_output")}
        selected = {"kind": "selected_output", "decision_id": str(uuid4()), "artifact_id": str(uuid4()),
            "item_key": "explicit-selected-output", "purpose": None, "order": 0,
            "scope_ref": {"kind": "shot", "id": self.shot_id},
            "subject_ref": {"kind": "character", "id": self.character_id}, "paired_to": None}
        wrapped = {"images": {"selection": {"source": "selected_output", "items": [selected]}}}
        self.assertEqual(_preview_media_sources([declared], wrapped), {"images": [selected]})
        wrapped["images"]["selection"]["source"] = "asset_library"
        with self.assertRaises(ValidationError):
            _preview_media_sources([declared], wrapped)

    def test_model_purpose_is_per_invocation_not_an_asset_or_creative_property(self):
        records = reference_use_records([{"use_key": "one", "media_kind": "video", "purpose": "导演原文：跟随背影",
            "setting": None, "asset": {"asset_id": self.c}}])
        before = deepcopy(records)
        for model_purpose in ("motion", "composition"):
            rule = mapping("videos", purpose=model_purpose)
            rule["groups"][0]["purposes"] = ["导演原文：跟随背影"]
            result, _ = self.expand([spec("video", "video", purposes=["motion", "composition"])],
                {"video": rule}, records)
            item = result["video"]["items"][0]
            self.assertEqual(item["purpose"], model_purpose)
            self.assertEqual(item["asset_id"], self.c)
        self.assertEqual(records, before)

    def test_required_model_purpose_is_never_inferred_even_when_description_matches(self):
        for invalid in (None, "undeclared", ""):
            rule = mapping("videos", purpose=invalid)
            with self.subTest(invalid=invalid), self.assertRaisesMessage(ValidationError, "模型用途"):
                self.expand([spec("video", "video", purposes=["motion"])], {"video": rule})
        with self.assertRaisesMessage(ValidationError, "没有额外用途"):  # A slot alone already describes its role.
            self.expand([spec()], {"images": mapping("character_images", purpose="appearance")})

    def setUp(self):
        self.enterContext(patch("studio.services.production_reference_mapping.execution_configuration_max_bytes", return_value=65536))
        self.shot_id = str(uuid4())
        self.character_id, self.prop_id = str(uuid4()), str(uuid4())
        self.a, self.b, self.c, self.d, self.e = [str(uuid4()) for _ in range(5)]
        self.records = [record("person", "character_images", "image", self.a,
                subject={"kind": "character", "id": self.character_id}),
            record("prop", "prop_images", "image", self.b, subject={"kind": "prop", "id": self.prop_id}),
            record("clip", "videos", "video", self.c, purpose="motion"),
            record("paired", "paired_audio", "audio", self.d, purpose="source_sound", parent="clip"),
            record("music", "audios", "audio", self.e, purpose="music")]

    def test_exact_uses_preserve_purpose_subject_order_and_repeated_asset_identity(self):
        uses = [{"use_key": key, "media_kind": "image", "purpose": purpose,
            "setting": setting, "asset": {"asset_id": self.a}} for key, purpose, setting in (
                ("clothes", "自定义服装参考", {"entity_kind": "character", "entity_id": self.character_id}),
                ("lighting", "first_frame", None))]
        before = deepcopy(uses)
        records = reference_use_records(uses)
        mapped, excluded = self.expand(specs=[spec()], sources={"images": mapping("images")}, records=records)
        items = mapped["images"]["items"]
        self.assertEqual([item["asset_id"] for item in items], [self.a, self.a])
        self.assertEqual([item["purpose"] for item in items], [None, None])
        self.assertEqual([record["purpose"] for record in records], ["自定义服装参考", "first_frame"])
        self.assertEqual(items[0]["subject_ref"], {"kind": "character", "id": self.character_id})
        self.assertIsNone(items[1]["subject_ref"])
        self.assertNotEqual(items[0]["item_key"], items[1]["item_key"])
        self.assertEqual(excluded, [])
        self.assertEqual(uses, before)
        reversed_items = self.expand(specs=[spec()], sources={"images": mapping("images")},
            records=reference_use_records(list(reversed(uses))))[0]["images"]["items"]
        self.assertEqual(items[0]["item_key"], reversed_items[1]["item_key"])
        with self.assertRaises(ValidationError):
            self.expand(sources={"images": mapping("first_frame")}, records=records)

    def test_exact_uses_never_infer_audio_pairing_or_extract_video_audio(self):
        records = reference_use_records([{"use_key": kind, "media_kind": kind,
            "purpose": "source_sound", "setting": None, "asset": {"asset_id": pk}}
            for kind, pk in (("video", self.c), ("audio", self.d))])
        self.assertEqual([row["source_key"] for row in records], ["videos", "audios"])
        self.assertTrue(all(row["paired_reference_key"] is None for row in records))
        with self.assertRaises(ValidationError):
            self.expand(specs=[spec("audio", "audio", parent="video")],
                sources={"audio": mapping("paired_audio")}, records=records)

    def expand(self, specs=None, sources=None, records=None):
        return expand_reviewed_reference_sources(specs=specs or [spec()],
            sources=sources if sources is not None else {"images": mapping("prop_images", "character_images")},
            records=records if records is not None else self.records, shot_id=self.shot_id)

    def test_group_order_semantics_and_source_data_preserved(self):
        before = deepcopy(self.records)
        mapped, unused = self.expand()
        rows = mapped["images"]["items"]
        self.assertEqual([row["asset_id"] for row in rows], [self.b, self.a])
        self.assertEqual([row["order"] for row in rows], [0, 1])
        self.assertEqual(rows[1]["subject_ref"], {"kind": "character", "id": self.character_id})
        self.assertIsNone(rows[1]["purpose"])
        self.assertEqual(rows[0]["scope_ref"], {"kind": "shot", "id": self.shot_id})
        self.assertEqual(len(unused), 3)
        self.assertEqual(before, self.records)
        self.assertEqual(self.expand()[0], mapped)

    def test_within_group_review_order_is_not_uuid_sort(self):
        self.records.append(record("second-person", "character_images", "image", str(uuid4()),
            subject={"kind": "character", "id": str(uuid4())}))
        rows = self.expand()[0]["images"]["items"]
        self.assertEqual([row["asset_id"] for row in rows[1:]],
            [row["source"]["asset_id"] for row in self.records if row["source_key"] == "character_images"])

    def test_pairing_works_when_child_input_precedes_parent(self):
        specs = [spec("sound", "audio", parent="video", purposes=["source_sound"]), spec("video", "video", purposes=["motion"])]
        rows, _ = self.expand(specs, {"sound": mapping("paired_audio", purpose="source_sound"), "video": mapping("videos", purpose="motion")})
        self.assertEqual(rows["sound"]["items"][0]["paired_to"], rows["video"]["items"][0]["item_key"])
        self.assertEqual(rows["sound"]["items"][0]["purpose"], "source_sound")
        self.assertEqual(rows["sound"]["items"][0]["asset_id"], self.d)

    def test_orphan_pair_does_not_pair_to_another_video_by_index(self):
        specs = [spec("sound", "audio", parent="video", purposes=["source_sound"]), spec("video", "video", required=False, purposes=["other"])]
        rule = mapping("videos", purpose="other")
        rule["groups"][0]["purposes"] = ["other"]
        with self.assertRaisesMessage(ValidationError, "未映射到父输入"):
            self.expand(specs, {"sound": mapping("paired_audio", purpose="source_sound"), "video": rule})

    def test_filters_select_without_overwriting_reviewed_purpose(self):
        self.records.append(record("second-video", "videos", "video", str(uuid4()), purpose="composition"))
        rule = mapping("videos", purpose="motion")
        rule["groups"][0]["purposes"] = ["composition"]
        mapped, _ = self.expand([spec("video", "video", purposes=["motion", "composition"])], {"video": rule})
        self.assertEqual([row["purpose"] for row in mapped["video"]["items"]], ["motion"])
        self.assertEqual(self.records[-1]["purpose"], "composition")

    def test_overflow_and_required_empty_never_truncate_or_fill(self):
        with self.assertRaisesMessage(ValidationError, "实际匹配2项"):
            self.expand([spec(maximum=1)])
        with self.assertRaisesMessage(ValidationError, "实际匹配0项"):
            self.expand(records=[])
        self.assertEqual(self.expand([spec(required=False)], records=[])[0], {"images": {"items": []}})

    def test_unknown_keys_duplicates_and_invalid_filters_fail(self):
        changes = [{"stage_key": "media_generation"}, {"scope": {"kind": "project"}}, {"groups": []},
            {"groups": mapping("character_images", "character_images")["groups"]},
            {"groups": mapping("videos")["groups"]}, {"rewrite_purpose": "first_allowed"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_reference_mapping({**mapping("character_images"), **change}, spec=spec(), prior_stages=["storyboard_review"], path="input")
        for bad in ([], ["unknown"], ["character", "character"], "character", True):
            rule = mapping("character_images")
            rule["groups"][0]["subject_kinds"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                validate_reference_mapping(rule, spec=spec(), prior_stages=["storyboard_review"], path="input")

    def test_unreviewed_future_stage_and_wrong_pair_input_rejected(self):
        with self.assertRaises(ValidationError):
            validate_reference_mapping(mapping("character_images"), spec=spec(), prior_stages=[], path="input")
        with self.assertRaises(ValidationError):
            validate_reference_mapping(mapping("audios"), spec=spec("audio", "audio", parent="video"), prior_stages=["storyboard_review"], path="input")

    def test_embedded_audio_is_not_a_fake_audio_asset(self):
        self.records[3]["source"] = {"kind": "video_audio", "asset_id": self.d}
        with self.assertRaises(ValidationError):
            self.expand()
        self.records[3]["source"] = {"kind": "video_audio"}
        specs = [spec("sound", "audio", parent="video", purposes=["source_sound"]), spec("video", "video", purposes=["motion"])]
        result, _ = self.expand(specs, {"sound": mapping("paired_audio", purpose="source_sound"), "video": mapping("videos", purpose="motion")})
        audio = result["sound"]["items"][0]
        self.assertEqual(audio["kind"], "video_audio")
        self.assertNotIn("asset_id", audio)
        self.assertEqual(audio["paired_to"], result["video"]["items"][0]["item_key"])
        self.assertEqual(audio["purpose"], "source_sound")

    def test_malformed_records_rejected_not_defaulted(self):
        rows = deepcopy(self.records)
        for change in ({"subject_ref": {"kind": [], "id": "x"}}, {"source": {"kind": "asset", "asset_id": "bad"}},
                {"purpose": False}, {"source_key": "unknown"}, {"paired_reference_key": "missing"}):
            bad = deepcopy(rows)
            bad[0].update(change)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.expand(records=bad)

    def test_frame_and_continuity_sources_remain_distinct(self):
        rows = [record(key, key, "image", asset_id) for key, asset_id in
            (("first_frame", self.a), ("last_frame", self.b), ("continuity_images", self.c))]
        for row in rows:
            mapped, unused = self.expand(sources={"images": mapping(row["source_key"])}, records=rows)
            self.assertEqual(mapped["images"]["items"][0]["asset_id"], row["source"]["asset_id"])
            self.assertEqual(len(unused), 2)

    def test_same_asset_use_in_different_inputs_has_distinct_use_identity(self):
        mapped, _ = self.expand([spec("one"), spec("two")], {"one": mapping("character_images"), "two": mapping("character_images")})
        self.assertNotEqual(mapped["one"]["items"][0]["item_key"], mapped["two"]["items"][0]["item_key"])
        # Whether repetition is permitted is still checked by the shared resolver.
