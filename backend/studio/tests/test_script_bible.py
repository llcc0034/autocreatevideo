from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from studio.models import CharacterBible, PropBible, SceneBible, SeriesProject
from studio.services.script_bible import (
    SCRIPT_BIBLE_BEGIN_MARKER,
    SCRIPT_BIBLE_END_MARKER,
    ScriptBibleParseError,
    parse_script_bible,
    render_script_bible,
)
from studio.services.script_bible_materialization import (
    apply_frozen_script_bible,
    freeze_script_bible_candidates,
)


def bible_ir():
    return {
        "characters": [{
            "name": "王强",
            "description": "大学新闻社学生",
            "visual_identity_en": "college reporter in a navy jacket",
        }],
        "scenes": [{
            "name": "大学图书馆",
            "description": "闭馆前的安静阅览室",
            "visual_identity_en": "quiet university library under warm lamps",
        }],
        "props": [{
            "name": "红伞",
            "description": "雨夜留下的关键物证",
            "visual_identity_en": "bright red umbrella with a worn wooden handle",
        }],
    }


def master_script(*, heading: bool = True) -> str:
    body = ("第1集：雨夜相遇\n" if heading else "") + (
        "镜头1【时长=6秒】【首帧连续=否】【尾帧连续=否】：大学图书馆\n"
        "[0-3秒] 动作：王强在门边捡起红伞。\n"
        "[3-6秒] 王强（softly）：这不是我的伞。"
    )
    return f"{render_script_bible(bible_ir())}\n\n{body}"


class ScriptBibleParserTests(SimpleTestCase):
    def test_bom_crlf_round_trip_and_longest_non_overlapping_evidence(self):
        script = "\ufeff" + master_script().replace("\n", "\r\n")

        parsed = parse_script_bible(script)

        self.assertTrue(parsed.has_block)
        self.assertNotIn("\r", parsed.body)
        self.assertTrue(parsed.body.startswith("第1集"))
        self.assertEqual(len(parsed.source_fingerprint), 64)
        by_name = {item["name"]: item for item in parsed.candidates}
        self.assertEqual(by_name["王强"]["occurrence"], 2)
        self.assertTrue(by_name["大学图书馆"]["recommended"])
        self.assertTrue(by_name["红伞"]["recommended"])
        self.assertEqual(
            render_script_bible(bible_ir()),
            "\n".join(master_script().splitlines()[:5]),
        )

    def test_no_block_returns_original_body_and_no_candidates(self):
        source = "没有集标题的第一段。\r\n\r\n第二段。"

        parsed = parse_script_bible(source)

        self.assertFalse(parsed.has_block)
        self.assertEqual(parsed.body, source.replace("\r\n", "\n"))
        self.assertEqual(parsed.candidates, ())
        self.assertEqual(parsed.diagnostics, ())

    def test_half_nested_and_body_pseudo_markers_are_explicit_errors(self):
        invalid_sources = (
            f"{SCRIPT_BIBLE_BEGIN_MARKER}\n【人物】王强｜描述｜identity",
            (
                f"{SCRIPT_BIBLE_BEGIN_MARKER}\n{SCRIPT_BIBLE_BEGIN_MARKER}\n"
                f"{SCRIPT_BIBLE_END_MARKER}"
            ),
            f"第1集：正文写到 {SCRIPT_BIBLE_BEGIN_MARKER} 但不是独立前置块",
        )
        for source in invalid_sources:
            with self.subTest(source=source):
                with self.assertRaises(ScriptBibleParseError):
                    parse_script_bible(source)

    def test_renderer_rejects_reserved_controls_and_nfkc_cross_type_duplicate(self):
        for value in (
            "坏｜名称", "坏\n名称", "\t坏名称", "\n坏名称", "坏\u202e名称",
        ):
            payload = bible_ir()
            payload["characters"][0]["name"] = value
            with self.subTest(value=repr(value)):
                with self.assertRaises(ScriptBibleParseError):
                    render_script_bible(payload)

        duplicate = bible_ir()
        duplicate["props"][0]["name"] = "ＷＡＮＧ"
        duplicate["characters"][0]["name"] = "wang"
        with self.assertRaises(ScriptBibleParseError) as raised:
            render_script_bible(duplicate)
        self.assertEqual(raised.exception.code, "script_bible_name_conflict")


class ScriptBibleProductionServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="script-bible-owner",
            password="StrongPass123!",
        )
        self.other = get_user_model().objects.create_user(
            username="script-bible-other",
            password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(
            owner=self.user,
            title="雨夜大学",
            master_script=master_script(),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_read_only_candidate_preview_is_owned_and_does_not_write(self):
        url = f"/api/v1/projects/{self.project.pk}/script-bible-candidates/"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["schema_version"], 1)
        self.assertEqual(len(response.data["candidates"]), 3)
        self.assertFalse(CharacterBible.objects.exists())
        self.assertFalse(SceneBible.objects.exists())
        self.assertFalse(PropBible.objects.exists())

        other_client = APIClient()
        other_client.force_authenticate(self.other)
        self.assertEqual(other_client.get(url).status_code, 404)

    def test_frozen_apply_is_atomic_locked_and_idempotent(self):
        frozen = freeze_script_bible_candidates(self.project.master_script)

        first = apply_frozen_script_bible(project=self.project, frozen=frozen)
        second = apply_frozen_script_bible(project=self.project, frozen=frozen)

        self.assertEqual(len(first["created_ids"]), 3)
        self.assertEqual(first["reused_ids"], [])
        self.assertEqual(second["created_ids"], [])
        self.assertEqual(len(second["reused_ids"]), 3)
        self.assertEqual(CharacterBible.objects.filter(project=self.project).count(), 1)
        self.assertEqual(SceneBible.objects.filter(project=self.project).count(), 1)
        self.assertEqual(PropBible.objects.filter(project=self.project).count(), 1)
        for entity in (
            CharacterBible.objects.get(project=self.project),
            SceneBible.objects.get(project=self.project),
            PropBible.objects.get(project=self.project),
        ):
            self.assertTrue(entity.is_locked)
            provenance = entity.metadata["script_bible_v1"]
            self.assertRegex(provenance["reviewed_content_sha256"], r"^[0-9a-f]{64}$")

    def test_stale_frozen_candidate_set_rolls_back_without_partial_bibles(self):
        frozen = freeze_script_bible_candidates(self.project.master_script)
        self.project.master_script = self.project.master_script.replace(
            "大学新闻社学生",
            "毕业后的调查记者",
        )
        self.project.save(update_fields=("master_script", "updated_at"))

        with self.assertRaises(ValidationError):
            apply_frozen_script_bible(project=self.project, frozen=frozen)

        self.assertFalse(CharacterBible.objects.exists())
        self.assertFalse(SceneBible.objects.exists())
        self.assertFalse(PropBible.objects.exists())

    def test_cross_type_name_conflict_rolls_back_the_whole_batch(self):
        frozen = freeze_script_bible_candidates(self.project.master_script)
        PropBible.objects.create(
            project=self.project,
            slug="manual-wang",
            name="王强",
            description="人工道具",
            visual_identity="manual prop",
        )

        with self.assertRaises(ValidationError):
            apply_frozen_script_bible(project=self.project, frozen=frozen)

        self.assertFalse(CharacterBible.objects.exists())
        self.assertFalse(SceneBible.objects.exists())
        self.assertEqual(PropBible.objects.count(), 1)

    def test_reserved_provenance_cannot_be_forged_or_removed_by_crud(self):
        forged = self.client.post("/api/v1/characters/", {
            "project": str(self.project.id),
            "slug": "forged",
            "name": "伪造人物",
            "metadata": {"script_bible_v1": {"schema_version": 1}},
        }, format="json")
        self.assertEqual(forged.status_code, 400, forged.data)

        frozen = freeze_script_bible_candidates(self.project.master_script)
        apply_frozen_script_bible(project=self.project, frozen=frozen)
        saved = CharacterBible.objects.get(project=self.project)
        removed = self.client.patch(
            f"/api/v1/characters/{saved.id}/",
            {"metadata": {}},
            format="json",
        )
        self.assertEqual(removed.status_code, 400, removed.data)
        saved.refresh_from_db()
        self.assertIn("script_bible_v1", saved.metadata)

    def test_freeze_requires_all_three_bible_collections(self):
        payload = bible_ir()
        payload["props"] = []
        incomplete = f"{render_script_bible(payload)}\n\n第1集：测试"

        with self.assertRaises(ValidationError):
            freeze_script_bible_candidates(incomplete)
